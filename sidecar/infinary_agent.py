#!/usr/bin/env python3
"""
Infinary agent — the OUTBOUND-ONLY sidecar that runs on each customer's ERPNext box.

It (1) heartbeats the control plane with the version vector + dual drift fingerprint,
(2) long-polls for jobs, and (3) executes a major upgrade locally, emitting a stage
event at each step (and rolling back on failure). It authenticates with the
per-instance bearer token issued at provisioning. NOTHING listens for inbound
connections — the only network traffic is outbound HTTPS to the control plane.

The drift + version logic runs INSIDE Frappe via the companion `infinary_agent`
Frappe app (frappe_app_api.py); this process orchestrates and speaks HTTP.

Env:
  INFINARY_CONTROL_PLANE   e.g. https://api.infinary.io
  INFINARY_INSTANCE_ID     e.g. inst_abc123
  INFINARY_AGENT_TOKEN     per-instance bearer token (from the provision response)
  INFINARY_SITE            the Frappe site name (required unless dry-run)
  INFINARY_BENCH           bench path (default /home/frappe/frappe-bench)
  INFINARY_BENCH_CMD       bench invocation (default "bench"); set to e.g.
                           "docker compose exec -T backend bench" for frappe_docker
  INFINARY_HEARTBEAT_SEC   loop cadence, default 45
  INFINARY_DRYRUN          "1" → fake the bench (heartbeat + upgrade) for testing
  INFINARY_DRYRUN_VERSION  dry-run ERPNext version (default "15" — a step behind LATEST so a dry-run demos an upgrade)
  INFINARY_UPGRADE_DRIVER  "bench" (bare bench) or "compose" (frappe_docker: image swap)
  INFINARY_TARGET_IMAGE    compose driver: image to upgrade to (or job payload "targetImage")
  INFINARY_COMPOSE_DIR     compose driver: dir with the compose file (default /opt/erpnext)
  INFINARY_COMPOSE_SERVICE compose driver: the frappe service name (default backend)
  INFINARY_DB_ROOT_PASSWORD  compose driver: DB root password for restore-on-rollback

  Auto-update engine (image-targeted point updates + always-on security patches):
  (the target image is curated control-plane-side via APPROVED_FAC_IMAGE and delivered as
   `approvedImage` on the heartbeat; the agent swaps the compose image to it when it differs.
   Requires INFINARY_UPGRADE_DRIVER=compose + INFINARY_DB_ROOT_PASSWORD for the fail-closed probe.)
  INFINARY_DRYRUN_IMAGE          dry-run: the fake "current image" reported (default v16-2.6.2)
  INFINARY_SECURITY_WINDOW       default daily window for always-on security patches (HH:MM, default 01:00)
  INFINARY_SECURITY_WINDOW_MIN   security window length in minutes (default 180)
  INFINARY_FORCE_WINDOW          "1" → ignore the clock window (testing only)

  Blue/green self-update (the OS supervisor owns the restart — never in-process):
  INFINARY_SELF_UPDATE_DIR       base dir holding releases/<v> + the `current` symlink the
                                 service runs; unset ⇒ self-update is skipped
  INFINARY_SELF_UPDATE_RESTART   restart command (default "sudo systemctl restart infinary-agent")

  Run with `--selfcheck` to validate a staged release (imports + required env) → exit 0/1.
"""
from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

try:  # Python 3.9+ stdlib; the box ships 3.11
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore

import requests  # pip install requests

CP = os.environ["INFINARY_CONTROL_PLANE"].rstrip("/")
IID = os.environ["INFINARY_INSTANCE_ID"]
TOKEN = os.environ["INFINARY_AGENT_TOKEN"]
SITE = os.environ.get("INFINARY_SITE", "")
BENCH = os.environ.get("INFINARY_BENCH", "/home/frappe/frappe-bench")
# bench invocation — override for a containerised bench, e.g. on frappe_docker:
#   INFINARY_BENCH_CMD="docker compose exec -T backend bench"  (run from the compose dir)
BENCH_CMD = shlex.split(os.environ.get("INFINARY_BENCH_CMD", "bench"))
# Major-upgrade driver: "bench" (bare bench) or "compose" (frappe_docker image swap).
UPGRADE_DRIVER = os.environ.get("INFINARY_UPGRADE_DRIVER", "bench")
COMPOSE_CMD = shlex.split(os.environ.get("INFINARY_COMPOSE_CMD", "docker compose"))
COMPOSE_DIR = os.environ.get("INFINARY_COMPOSE_DIR", "/opt/erpnext")
COMPOSE_FILE = os.environ.get("INFINARY_COMPOSE_FILE", "compose.yml")
COMPOSE_SERVICE = os.environ.get("INFINARY_COMPOSE_SERVICE", "backend")
TARGET_IMAGE_ENV = os.environ.get("INFINARY_TARGET_IMAGE", "")
# compose driver: DB root password so `bench restore` on rollback is non-interactive.
DB_ROOT_PASSWORD = os.environ.get("INFINARY_DB_ROOT_PASSWORD", "")
PERIOD = int(os.environ.get("INFINARY_HEARTBEAT_SEC", "45"))
DRYRUN = os.environ.get("INFINARY_DRYRUN") == "1"
AGENT_VERSION = "0.7.2"
# The Frappe-app version is read from the live site fingerprint; this is only the
# dry-run fallback (kept in sync with infinary_agent/__init__.py for local demos).
AGENT_APP_VERSION_DRYRUN = "0.3.2"

# ── Auto-update engine config ─────────────────────────────────────────────
# Point updates apply via a PINNED image swap (compose) — the agent never runs an unbounded
# `bench update --pull`. The TARGET image is curated by the control plane (APPROVED_FAC_IMAGE,
# delivered as `approvedImage` on the heartbeat); the agent swaps to it when it differs from the
# running image. Security patches are always-on (the covenant). Absent a customer window they apply inside
# this conservative default daily window (instance-local time).
SECURITY_WINDOW = os.environ.get("INFINARY_SECURITY_WINDOW", "01:00")
SECURITY_WINDOW_MIN = int(os.environ.get("INFINARY_SECURITY_WINDOW_MIN", "180"))
# Blue/green self-update: releases unpack under RELEASE_DIR/<version>; CURRENT is the symlink
# the service runs (ExecStart=python $CURRENT/infinary_agent.py). Restart is owned by the OS
# supervisor — the agent never restarts itself in-process (the rollback-executor hazard).
SELF_UPDATE_DIR = os.environ.get("INFINARY_SELF_UPDATE_DIR", "")  # e.g. /opt/infinary-agent
SELF_UPDATE_RESTART = shlex.split(os.environ.get("INFINARY_SELF_UPDATE_RESTART", "sudo systemctl restart infinary-agent"))

S = requests.Session()
S.headers["Authorization"] = f"Bearer {TOKEN}"

# In dry-run the agent keeps an in-process version so a successful upgrade visibly
# bumps what subsequent heartbeats report.
_dry_version = os.environ.get("INFINARY_DRYRUN_VERSION", "15")
_dry_image = os.environ.get("INFINARY_DRYRUN_IMAGE", "infinary/erpnext-fac:v16-2.6.2")

# Desired-state captured from the most recent heartbeat response, so collect_reported_state()
# can compute availableAgentVersion + echo pending updates for portal visibility.
_latest_agent_version: str | None = None
_pending_updates: list[dict] = []
_agent_artifact: dict | None = None
_approved_image: str | None = None  # the control-plane-curated target container image (compose/fac)


def _current_image_safe() -> str | None:
    """The running compose image, or None if not a compose box / can't determine it."""
    if DRYRUN:
        return _dry_image
    if UPGRADE_DRIVER != "compose":
        return None
    try:
        return _compose_image()
    except Exception:
        return None


def log(msg: str) -> None:
    print(f"[agent] {msg}", flush=True)


def _run(cmd: list[str], timeout: int = 3600, cwd: str | None = None) -> str:
    """Run a command (raise on non-zero), logging a trimmed form of it."""
    shown = " ".join(cmd)
    log("$ " + (shown if len(shown) < 200 else " ".join(cmd[:6]) + " …"))
    out = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if out.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:2])} failed: {(out.stderr or out.stdout).strip()[:300]}")
    return out.stdout


def _bench(*args: str, timeout: int = 3600) -> str:
    """Run a bench command. INFINARY_BENCH_CMD lets bench live behind a wrapper —
    e.g. `docker compose exec -T backend bench` on a frappe_docker box, where bench
    runs inside the container rather than on the host."""
    cwd = BENCH if BENCH_CMD == ["bench"] else None
    return _run([*BENCH_CMD, *args], timeout=timeout, cwd=cwd)


def _bench_json(method: str) -> dict:
    """Call a whitelisted infinary_agent method and parse its JSON stdout. `bench
    execute` serialises the return value to JSON on the last line; some versions
    JSON-encode it a second time, so decode again if we still hold a string."""
    out = _bench("--site", SITE, "execute", method, timeout=180)
    data = json.loads(out.strip().splitlines()[-1])
    if isinstance(data, str):
        data = json.loads(data)
    return data


def _health() -> str:
    if DRYRUN:
        return "healthy"
    try:
        _bench("--site", SITE, "execute", "frappe.ping", timeout=60)
        return "healthy"
    except Exception:
        return "degraded"


def _cmp_version(a: str, b: str) -> int:
    """Numeric dotted-version compare: -1 / 0 / 1. Non-numeric chunks count as 0."""
    def parts(v: str) -> list[int]:
        out = []
        for chunk in str(v).split("."):
            try:
                out.append(int(chunk))
            except ValueError:
                out.append(0)
        return out
    pa, pb = parts(a), parts(b)
    for i in range(max(len(pa), len(pb))):
        d = (pa[i] if i < len(pa) else 0) - (pb[i] if i < len(pb) else 0)
        if d:
            return -1 if d < 0 else 1
    return 0


def _agent_update_available() -> str | None:
    """The newer sidecar version the control plane offers (null = on latest / unknown)."""
    if _latest_agent_version and _cmp_version(AGENT_VERSION, _latest_agent_version) < 0:
        return _latest_agent_version
    return None


def collect_reported_state() -> dict:
    if DRYRUN:
        return {
            "versionVector": {"frappe": _dry_version, "erpnext": _dry_version, "apps": {}},
            "health": "healthy",
            "drift": {
                "filesystemHash": "sha256:dryrun",
                "dbCustomizationsHash": "sha256:dryrun",
                "hasDrift": False,
                "detected": {
                    "customFields": 0, "serverScripts": 0, "clientScripts": 0,
                    "customDoctypes": 0, "customApps": [],
                },
            },
            "lastUpdateOutcome": "none",
            "agentVersion": AGENT_VERSION,
            "agentAppVersion": AGENT_APP_VERSION_DRYRUN,
            "platform": {"python": "3.11.0", "mariadb": "11.8.0"},
            "dataHealth": {"pendingPatches": 0, "failedPatches": 0},
            "aiSpendCents": 0,
            "availableAppUpdates": list(_pending_updates),
            "availableAgentVersion": _agent_update_available(),
            "currentImage": _current_image_safe(),
        }
    fp = _bench_json("infinary_agent.api.fingerprint")
    return {
        "versionVector": fp["versionVector"],
        "health": _health(),
        "drift": fp["drift"],
        # platform / dataHealth / aiSpend are optional in the contract — pass through when present.
        "platform": fp.get("platform"),
        "dataHealth": fp.get("dataHealth"),
        "aiSpendCents": fp.get("aiSpendCents", 0),
        "lastUpdateOutcome": fp.get("lastUpdateOutcome", "none"),
        "agentVersion": AGENT_VERSION,
        "agentAppVersion": fp.get("agentAppVersion"),
        # Echo the control-plane-curated pending updates (visibility) + the self-update signal.
        "availableAppUpdates": list(_pending_updates),
        "availableAgentVersion": _agent_update_available(),
        "currentImage": _current_image_safe(),
    }


def heartbeat() -> dict:
    r = S.post(f"{CP}/v1/agent/{IID}/heartbeat", json=collect_reported_state(), timeout=30)
    r.raise_for_status()
    # The control plane returns desired-state — the kill-switch (`paused`) and the customer's
    # auto-update schedule (`updatePolicy`). Older control planes returned 204; decode defensively.
    try:
        return r.json() if r.content else {}
    except Exception:
        return {}


def poll_jobs() -> list[dict]:
    r = S.get(f"{CP}/v1/agent/{IID}/jobs", timeout=70)  # long-poll
    r.raise_for_status()
    return r.json().get("jobs", [])


def emit(job_id: str, **event) -> None:
    S.post(f"{CP}/v1/agent/{IID}/jobs/{job_id}/events", json=event, timeout=30).raise_for_status()


# ── Major-upgrade drivers ─────────────────────────────────────────────────
# A major upgrade is topology-specific. The five stages are uniform (backing_up,
# offline, installing, migrating, final_checks) but the DRIVER decides what each
# one does:
#   bench   — a bare bench: `bench switch-to-branch version-N --upgrade`, migrate.
#   compose — frappe_docker: the app code lives in the IMAGE (not a volume), so the
#             version change is an IMAGE SWAP + recreate; the persistent DB/sites
#             volume is then migrated. Rollback reverts the image and restores the
#             pre-upgrade DB backup.
# `ctx` is a per-job dict that carries rollback state (old image, backup path).


def _compose(*args: str, timeout: int = 3600) -> str:
    return _run([*COMPOSE_CMD, *args], timeout=timeout, cwd=COMPOSE_DIR)


def _compose_image() -> str:
    """The image the frappe service currently runs (the rollback target)."""
    cfg = json.loads(_compose("config", "--format", "json", timeout=120))
    return cfg["services"][COMPOSE_SERVICE]["image"]


def _compose_set_image(old: str, new: str) -> None:
    """Swap the frappe image across every service that uses it (mariadb/redis run
    other images and are left untouched). Edits the compose file in place."""
    path = os.path.join(COMPOSE_DIR, COMPOSE_FILE)
    with open(path, encoding="utf-8") as f:
        content = f.read()
    if old not in content:
        raise RuntimeError(f"image {old!r} not found in {path}")
    with open(path, "w", encoding="utf-8") as f:
        f.write(content.replace(old, new))


class _Driver:
    """Default stage implementations, shared by the real drivers."""

    def backup(self, ctx: dict) -> None:
        _bench("--site", SITE, "backup", "--with-files")

    def offline(self, ctx: dict) -> None:
        _bench("--site", SITE, "set-maintenance-mode", "on")

    def install(self, ctx: dict) -> None:
        raise NotImplementedError

    def migrate(self, ctx: dict) -> None:
        _bench("--site", SITE, "migrate")

    def checks(self, ctx: dict) -> None:
        _bench("--site", SITE, "execute", "frappe.ping", timeout=120)

    def online(self, ctx: dict) -> None:
        _bench("--site", SITE, "set-maintenance-mode", "off")

    def rollback(self, ctx: dict) -> None:
        self.online(ctx)


class BenchDriver(_Driver):
    """Bare bench: switch branches + migrate. Rollback is limited here — production
    layers disk-snapshot rollback on top, orchestrated by the control plane."""

    def install(self, ctx: dict) -> None:
        _bench("switch-to-branch", f"version-{ctx['to']}", "frappe", "erpnext", "--upgrade")


class ComposeDriver(_Driver):
    """frappe_docker: the version change is an image swap + recreate; the DB/sites
    volume persists and is migrated. Rollback reverts the image and restores the
    pre-upgrade DB backup."""

    def _ensure_image(self, img: str) -> None:
        try:
            _run(["docker", "image", "inspect", img], timeout=60)
        except Exception:
            log(f"target image not local; pulling {img}")
            _run(["docker", "pull", img], timeout=1800)

    def _wait_ready(self, tries: int = 30) -> None:
        for _ in range(tries):
            try:
                _bench("--site", SITE, "execute", "frappe.ping", timeout=30)
                return
            except Exception:
                time.sleep(5)
        raise RuntimeError("backend did not come ready after recreate")

    def _target(self, ctx: dict) -> str:
        img = ctx.get("target_image")
        if not img:
            raise RuntimeError(
                "compose driver needs a target image (job payload 'targetImage' or "
                "INFINARY_TARGET_IMAGE) — refusing to upgrade without one")
        return img

    def backup(self, ctx: dict) -> None:
        ctx["old_image"] = _compose_image()
        _bench("--site", SITE, "backup", "--with-files")
        try:
            # Newest DB dump. The inner `*` is REQUIRED: sites with backup encryption write
            # `<ts>-<site>-database-enc.sql.gz`, so a bare `*-database.sql.gz` finds nothing and we'd
            # wrongly bail "no restorable backup" (bench auto-decrypts the -enc dump on restore).
            out = _compose("exec", "-T", COMPOSE_SERVICE, "bash", "-lc",
                           f"ls -t sites/{SITE}/private/backups/*-database*.sql.gz | head -1",
                           timeout=120)
            ctx["db_backup"] = out.strip() or None
        except Exception as e:
            log(f"could not locate the DB backup (rollback will revert image only): {e}")
        log(f"pre-upgrade: image={ctx['old_image']} db_backup={ctx.get('db_backup')}")

    def install(self, ctx: dict) -> None:
        target, old = self._target(ctx), ctx["old_image"]
        self._ensure_image(target)
        if target != old:
            _compose_set_image(old, target)
            ctx["image_swapped"] = True
            log(f"image {old} -> {target}")
        else:
            log(f"target image == current ({target}); recreate-only")
        _compose("up", "-d", timeout=1800)
        self._wait_ready()

    def rollback(self, ctx: dict) -> None:
        if ctx.get("image_swapped") and ctx.get("old_image"):
            try:
                _compose_set_image(_compose_image(), ctx["old_image"])
                _compose("up", "-d", timeout=1800)
                self._wait_ready()
                log(f"image reverted to {ctx['old_image']}")
            except Exception as e:
                log(f"image revert failed: {e}")
        db = ctx.get("db_backup")
        if db and DB_ROOT_PASSWORD:
            log(f"restoring the DB from {db}")
            _bench("--site", SITE, "restore", db,
                   "--db-root-password", DB_ROOT_PASSWORD, "--force", timeout=3600)
        elif db:
            log("DB restore SKIPPED: set INFINARY_DB_ROOT_PASSWORD to enable it "
                "(reverted the image only)")
        self.online(ctx)


class DryDriver(_Driver):
    """Fakes the bench/docker so the whole pipeline is testable without Frappe."""

    def backup(self, ctx: dict) -> None: time.sleep(2)
    def offline(self, ctx: dict) -> None: time.sleep(1)
    def install(self, ctx: dict) -> None: time.sleep(3)
    def migrate(self, ctx: dict) -> None: time.sleep(3)
    def checks(self, ctx: dict) -> None: time.sleep(2)

    def online(self, ctx: dict) -> None:
        global _dry_version
        if ctx.get("to"):
            _dry_version = ctx["to"]

    def rollback(self, ctx: dict) -> None: time.sleep(2)


_DRIVERS = {"bench": BenchDriver, "compose": ComposeDriver}

STAGES = [
    ("backing_up", "Taking a full snapshot of your data and code", "backup"),
    ("offline", "Taking your system offline", "offline"),
    ("installing", "Installing the new version", "install"),
    ("migrating", "Applying the new version to your records", "migrate"),
    ("final_checks", "Running the smoke checks", "checks"),
]


def run_upgrade(job: dict) -> None:
    jid = job["id"]
    payload = job.get("payload", {}) or {}
    to = str(payload.get("toVersion") or "")
    if DRYRUN:
        driver: _Driver = DryDriver()
    else:
        cls = _DRIVERS.get(UPGRADE_DRIVER)
        if cls is None:
            emit(jid, kind="terminal", outcome="blocked", message=f"Unknown upgrade driver: {UPGRADE_DRIVER}")
            return
        driver = cls()
    ctx = {"to": to, "target_image": payload.get("targetImage") or TARGET_IMAGE_ENV or None}
    log(f"executing major_upgrade -> v{to} via {'dry' if DRYRUN else UPGRADE_DRIVER} driver (job {jid})")
    try:
        for stage, msg, method in STAGES:
            emit(jid, kind="stage", stage=stage, stageStatus="started", message=msg)
            getattr(driver, method)(ctx)
            emit(jid, kind="stage", stage=stage, stageStatus="completed")
        driver.online(ctx)
        emit(jid, kind="terminal", outcome="success", message=f"You're now on ERPNext {to}")
        log(f"upgrade to v{to} complete")
    except Exception as e:
        log(f"upgrade failed: {e}")
        try:
            driver.rollback(ctx)
        except Exception as re:
            log(f"rollback error: {re}")
        emit(jid, kind="terminal", outcome="rolled_back", message=str(e)[:200])


# ── Simple instance actions (backup / restart / clear-cache) ───────────────
# These are jobs the control plane enqueues and the agent PULLS — same outbound-only
# transport as upgrades. Each emits a single "working" stage + a terminal, tagged
# runType="action" so the control plane records them in the action ledger (and they
# never count toward upgrade gating).


def _emit_action(jid: str, **event) -> None:
    emit(jid, runType="action", **event)


def _run_simple_action(job: dict, *, label: str, done: str, cmd: tuple[str, ...]) -> None:
    jid = job["id"]
    _emit_action(jid, kind="stage", stage="working", stageStatus="started", message=label)
    try:
        if not DRYRUN:
            _bench(*cmd)
        _emit_action(jid, kind="terminal", outcome="success", message=done)
    except Exception as e:
        log(f"action {job.get('type')} failed: {e}")
        # No TerminalOutcome for plain failure; 'blocked' maps to the ledger's 'failed' status.
        _emit_action(jid, kind="terminal", outcome="blocked", message=str(e)[:200])


ACTION_HANDLERS = {
    "backup": lambda job: _run_simple_action(
        job, label="Taking a backup of your data", done="Backup complete",
        cmd=("--site", SITE, "backup", "--with-files")),
    "restart": lambda job: _run_simple_action(
        job, label="Restarting your services", done="Services restarted", cmd=("restart",)),
    "clear_cache": lambda job: _run_simple_action(
        job, label="Clearing caches", done="Caches cleared",
        cmd=("--site", SITE, "clear-cache")),
}


def run_job(job: dict) -> None:
    """Dispatch a pulled job by type: the 5-stage major upgrade, a simple action, or a
    staff-forced blue/green agent self-update."""
    jtype = job.get("type")
    if jtype == "major_upgrade":
        run_upgrade(job)
        return
    if jtype == "agent_update":
        # Staff-forced self-update (the canary/expedited path) — uses the server-pinned artifact
        # from the latest heartbeat; the run is reported on the pulled job's id (runType=action).
        if _agent_artifact:
            run_agent_self_update(_agent_artifact, job["id"], "action")
        else:
            _emit_action(job["id"], kind="terminal", outcome="skipped", message="No server-pinned agent artifact configured")
        return
    if jtype in ("update_now", "app_update"):
        # Image-targeted: swap the compose image to the control-plane-CURATED target
        # (APPROVED_FAC_IMAGE, delivered as `approvedImage` on the heartbeat), under snapshot +
        # rollback. No approved image ⇒ nothing to apply; already on it ⇒ no-op success.
        if not _approved_image:
            _emit_action(job["id"], kind="terminal", outcome="skipped", message="No approved image configured (set APPROVED_FAC_IMAGE)")
            return
        if _current_image_safe() == _approved_image:
            _emit_action(job["id"], kind="terminal", outcome="success", message="Already on the approved image")
            return
        _apply_image(job["id"], "action", _approved_image)
        return
    if jtype == "app_install":
        # Installing a NEW app means a rebuilt image on the compose/image topology — not something
        # the in-place outbound agent can do. Report it clearly rather than blocking opaquely.
        _emit_action(job["id"], kind="terminal", outcome="skipped",
                     message="Installing a new app requires a rebuilt image on this deployment — handled by Infinary, not the in-place agent")
        return
    handler = ACTION_HANDLERS.get(jtype)
    if handler is None:
        _emit_action(job["id"], kind="terminal", outcome="blocked", message=f"Unsupported job type: {jtype}")
        return
    handler(job)


# ── Auto-update engine (scheduled point updates + always-on security patches) ──
# Outbound-pure: at window time the agent ANNOUNCES the run to the control plane (which
# re-checks the kill-switch / plan / single-flight and opens a durable ledger record), then
# applies the control-plane-CURATED point updates under a LOCAL snapshot + auto-rollback.
# Real per-app "what's available" detection from a locked-down box is an open problem; the
# apply-list is curated centrally (APPROVED_POINT_TARGETS) so rollout is reviewable + haltable.
FORCE_WINDOW = os.environ.get("INFINARY_FORCE_WINDOW") == "1"  # testing: bypass the clock window


def _now_local(tz_name: str | None) -> datetime:
    if tz_name and ZoneInfo is not None:
        try:
            return datetime.now(ZoneInfo(tz_name))
        except Exception:
            pass
    return datetime.now(timezone.utc)


def _within_window(time_str: str, window_min: int, tz_name: str | None) -> bool:
    if FORCE_WINDOW:
        return True
    try:
        hh, mm = (int(x) for x in str(time_str).split(":"))
    except Exception:
        return False
    now = _now_local(tz_name)
    start = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    minutes_since = (now - start).total_seconds() / 60.0
    return 0 <= minutes_since < max(1, window_min)


def _wait_ready(tries: int = 60) -> None:
    # ~5 min budget: a freshly-pulled image's FIRST boot (asset check, cold caches) is much slower
    # than a warm recreate — too small a budget turns a viable upgrade into a spurious abort.
    for _ in range(tries):
        try:
            _bench("--site", SITE, "execute", "frappe.ping", timeout=30)
            return
        except Exception:
            time.sleep(5)
    raise RuntimeError("backend did not come ready after recreate")


def _begin_run(kind: str, scope: str | None, summary: str | None) -> str | None:
    """Announce a run; the control plane re-checks paused/plan/single-flight and returns a jobId
    (or refuses → None, so we retry next cycle)."""
    try:
        r = S.post(f"{CP}/v1/agent/{IID}/auto-update/begin", timeout=30,
                   json={"kind": kind, "scope": scope, "summary": summary})
        if r.status_code == 202:
            return r.json().get("jobId")
        log(f"auto-update/begin refused ({r.status_code}): {r.text[:160]}")
    except Exception as e:
        log(f"auto-update/begin error: {e}")
    return None


def _capability_ready() -> tuple[bool, str]:
    """Fail-CLOSED pre-flight: only auto-apply when we can genuinely roll back."""
    if DRYRUN:
        return True, ""
    if UPGRADE_DRIVER != "compose":
        return False, "image-targeted update requires the compose driver (set INFINARY_UPGRADE_DRIVER=compose)"
    if not DB_ROOT_PASSWORD:
        return False, "INFINARY_DB_ROOT_PASSWORD not set (cannot restore on rollback)"
    return True, ""


def run_managed_update(kind: str, run_type: str, target_image: str, scope: str | None) -> None:
    """Agent-INITIATED run: announce it (the control plane re-checks paused / plan / single-flight +
    opens a durable ledger record), then apply the curated target image on the returned jobId."""
    if not target_image:
        return
    jid = _begin_run(kind, scope, f"image → {target_image}"[:480])
    if not jid:
        return  # refused (paused / single-flight / plan) — retry next cycle
    _apply_image(jid, run_type, target_image)


def _apply_image(jid: str, run_type: str, target_image: str) -> None:
    """Snapshot → swap the compose image to `target_image` → migrate → verify, rolling back on any
    failure. The jobId is already open — via /begin (auto/security) OR a pulled update_now job.
    The target is the control-plane-curated image (APPROVED_FAC_IMAGE), not a version template."""
    summary = f"→ {target_image}"

    def ev(**e):
        emit(jid, runType=run_type, **e)

    ctx: dict = {}
    try:
        ev(kind="stage", stage="working", stageStatus="started", message=f"Checking update safety: {summary}")
        ok, why = _capability_ready()
        if not ok:
            log(f"managed update skipped (fail-closed): {why}")
            ev(kind="terminal", outcome="skipped", message=f"Skipped: {why}")
            return
        if DRYRUN:
            time.sleep(2)
            global _dry_image
            _dry_image = target_image
            ev(kind="terminal", outcome="success", message=f"Applied image {target_image}")
            return
        # 1) snapshot — the rollback source (fail-closed if we can't capture a restorable dump)
        ev(kind="stage", stage="backing_up", stageStatus="started", message="Taking a snapshot first")
        ctx["old_image"] = _compose_image()
        if ctx["old_image"] == target_image:
            ev(kind="terminal", outcome="success", message="Already on the target image")
            return
        _bench("--site", SITE, "backup", "--with-files")
        try:
            out = _compose("exec", "-T", COMPOSE_SERVICE, "bash", "-lc",
                           f"ls -t sites/{SITE}/private/backups/*-database*.sql.gz | head -1", timeout=120)
            ctx["db_backup"] = out.strip() or None
        except Exception as e:
            ctx["db_backup"] = None
            log(f"could not locate DB backup: {e}")
        if not ctx.get("db_backup"):
            ev(kind="terminal", outcome="skipped", message="Skipped: could not capture a restorable backup")
            return
        ev(kind="stage", stage="backing_up", stageStatus="completed")
        # 2) apply: swap the compose image to the curated target (bounded — a specific pinned image)
        target = target_image
        ev(kind="stage", stage="installing", stageStatus="started", message=f"Applying {summary}")
        try:
            _run(["docker", "image", "inspect", target], timeout=60)
        except Exception:
            _run(["docker", "pull", target], timeout=1800)
        _compose_set_image(ctx["old_image"], target)
        ctx["image_swapped"] = True
        _compose("up", "-d", timeout=1800)
        _wait_ready()  # generous budget — a freshly-pulled image's first boot is slow
        ev(kind="stage", stage="installing", stageStatus="completed")
        # Hold writes for the duration of migrate: anything a user writes now would be LOST if we
        # roll back to the pre-migrate snapshot. maintenance_mode lives in site_config (a file), not
        # the DB, so a DB restore won't clear it — the `finally` below guarantees it's turned off.
        _bench("--site", SITE, "set-maintenance-mode", "on")
        ctx["maint"] = True
        ev(kind="stage", stage="migrating", stageStatus="started", message="Applying to your records")
        ctx["migrated"] = True  # from here a failure means the schema may be partly migrated
        _bench("--site", SITE, "migrate")
        ev(kind="stage", stage="migrating", stageStatus="completed")
        # 3) verify — RETRY, not a single ping: a transient post-migrate/cold-boot hiccup must not
        # trigger a needless destructive rollback of a migration that actually succeeded.
        ev(kind="stage", stage="final_checks", stageStatus="started", message="Running smoke checks")
        _wait_ready()
        _bench("--site", SITE, "set-maintenance-mode", "off")
        ctx["maint"] = False
        ev(kind="stage", stage="final_checks", stageStatus="completed")
        ev(kind="terminal", outcome="success", message=f"Applied {summary}")
        log(f"managed update applied: {summary}")
    except Exception as e:
        log(f"managed update failed, rolling back: {e}")
        db = ctx.get("db_backup")
        migrated = ctx.get("migrated")
        old = ctx.get("old_image") if ctx.get("image_swapped") else None

        def _revert_image():
            if old:
                _compose_set_image(_compose_image(), old)
                _compose("up", "-d", timeout=1800)
                _wait_ready()

        try:
            if migrated:
                # The schema may be partly migrated. COVENANT: never downgrade code against a
                # migrated DB. Restore the snapshot FIRST (the safe direction), then revert the
                # image. If the restore fails we must NOT revert the code — leave the new image up
                # and fail loudly for manual recovery.
                if not (db and DB_ROOT_PASSWORD):
                    log("CANNOT roll back safely: migration ran but no restorable snapshot — leaving new image up")
                    ev(kind="terminal", outcome="failed",
                       message="Update failed mid-migration with no restorable snapshot — left on the new version; manual recovery needed")
                    return
                try:
                    _bench("--site", SITE, "restore", db, "--db-root-password", DB_ROOT_PASSWORD, "--force", timeout=3600)
                except Exception as re:
                    log(f"DB restore FAILED during rollback: {re} — leaving new image up (refusing to downgrade code onto a migrated DB)")
                    ev(kind="terminal", outcome="failed",
                       message="Update failed and the snapshot could not be restored — left on the new version; manual recovery needed")
                    return
                _revert_image()
            else:
                # Migration never ran → schema untouched → safe to just revert the image.
                _revert_image()
            ev(kind="terminal", outcome="rolled_back", message=str(e)[:200])
        except Exception as re:
            log(f"rollback error: {re}")
            try:
                ev(kind="terminal", outcome="failed", message=f"Rollback error: {str(re)[:160]}")
            except Exception:
                pass
    finally:
        # maintenance_mode persists in site_config across a DB restore — guarantee it's cleared so a
        # rolled-back (or left-on-new-version for recovery) site is never left dark.
        if ctx.get("maint"):
            try:
                _bench("--site", SITE, "set-maintenance-mode", "off")
            except Exception:
                pass


# ── Blue/green sidecar self-update ─────────────────────────────────────────
# The sidecar must NOT restart itself in-process (it is its own rollback executor — a crash on
# broken new code leaves nothing to recover). Instead: download the SERVER-PINNED artifact,
# verify its checksum, stage it, HEALTH-GATE it with `--selfcheck` in a child process, flip the
# CURRENT symlink atomically (keeping the prior release), then let the OS supervisor restart us.


def _find_entrypoint(root: str) -> str | None:
    for base, _dirs, files in os.walk(root):
        if "infinary_agent.py" in files:
            return os.path.join(base, "infinary_agent.py")
    return None


def _atomic_symlink(target: str, link: str) -> None:
    tmp = link + ".tmp"
    if os.path.islink(tmp) or os.path.exists(tmp):
        os.remove(tmp)
    os.symlink(target, tmp)
    os.replace(tmp, link)  # atomic on POSIX


def _su_path(name: str) -> str:
    return os.path.join(SELF_UPDATE_DIR, name)


def _self_update_boot_guard() -> None:
    """Crash-loop recovery: if the current release keeps restarting WITHOUT ever reaching a healthy
    heartbeat, revert the `current` symlink to the previous release and exit so the supervisor
    restarts into the known-good code. A normal release clears the counter on its first heartbeat."""
    if not SELF_UPDATE_DIR:
        return
    healthy = _su_path(".healthy")
    if os.path.exists(healthy):
        return  # the running release already proved itself
    previous, attempts_file = _su_path("previous"), _su_path(".boot_attempts")
    try:
        n = int(open(attempts_file).read().strip()) if os.path.exists(attempts_file) else 0
    except Exception:
        n = 0
    n += 1
    try:
        with open(attempts_file, "w") as f:
            f.write(str(n))
    except Exception:
        return
    if n >= 3 and os.path.islink(previous):
        target = os.path.realpath(previous)
        log(f"SELF-UPDATE CRASH-LOOP: {n} boots without a healthy heartbeat — reverting to {target}; exiting for supervisor restart")
        try:
            _atomic_symlink(target, _su_path("current"))
            os.remove(attempts_file)
        except Exception as e:
            log(f"crash-loop revert failed: {e}")
        raise SystemExit(1)


def _mark_self_update_healthy() -> None:
    """Called after the first successful heartbeat — the running release is confirmed good."""
    if not SELF_UPDATE_DIR:
        return
    try:
        with open(_su_path(".healthy"), "w") as f:
            f.write(AGENT_VERSION)
        ap = _su_path(".boot_attempts")
        if os.path.exists(ap):
            os.remove(ap)
    except Exception:
        pass


def run_agent_self_update(artifact: dict, jid: str, run_type: str) -> None:
    version = str(artifact.get("version") or "")
    url = artifact.get("url")
    sha = artifact.get("sha256")

    def ev(**e):
        emit(jid, runType=run_type, **e)

    staging = ""
    try:
        ev(kind="stage", stage="working", stageStatus="started", message=f"Updating the agent to {version}")
        if DRYRUN:
            time.sleep(1)
            ev(kind="terminal", outcome="success", message=f"Agent updated to {version} (dry-run)")
            return
        if not SELF_UPDATE_DIR:
            ev(kind="terminal", outcome="skipped", message="Skipped: INFINARY_SELF_UPDATE_DIR not configured")
            return
        if not (url and sha):
            ev(kind="terminal", outcome="skipped", message="Skipped: no server-pinned artifact")
            return
        # download + verify checksum
        tmp = tempfile.mkdtemp(prefix="infinary-agent-")
        archive = os.path.join(tmp, "agent.tgz")
        h = hashlib.sha256()
        with S.get(url, stream=True, timeout=300) as resp:
            resp.raise_for_status()
            with open(archive, "wb") as f:
                for chunk in resp.iter_content(65536):
                    f.write(chunk)
                    h.update(chunk)
        if h.hexdigest() != sha:
            ev(kind="terminal", outcome="blocked", message="Artifact checksum mismatch — refusing")
            return
        # Unpack + validate in a STAGING dir on the SAME filesystem (so the publish is an atomic
        # rename), so a partial/corrupt unpack never pollutes releases/<version>.
        os.makedirs(os.path.join(SELF_UPDATE_DIR, "releases"), exist_ok=True)
        staging = _su_path(f".staging-{version}")
        if os.path.isdir(staging):
            shutil.rmtree(staging)
        os.makedirs(staging)
        shutil.unpack_archive(archive, staging)
        staged_entry = _find_entrypoint(staging)
        if not staged_entry:
            ev(kind="terminal", outcome="blocked", message="Staged release missing infinary_agent.py")
            return
        # HEALTH GATE: run the staged binary in --selfcheck (imports + config) BEFORE publishing
        chk = subprocess.run([sys.executable, staged_entry, "--selfcheck"], capture_output=True, text=True, timeout=120)
        if chk.returncode != 0:
            ev(kind="terminal", outcome="blocked", message=f"Self-check failed: {(chk.stderr or chk.stdout)[:160]}")
            return
        # Atomically publish the validated release (rename within the same FS).
        release = _su_path(os.path.join("releases", version))
        if os.path.isdir(release):
            shutil.rmtree(release)
        os.replace(os.path.dirname(staged_entry), release)
        release_entry = _find_entrypoint(release) or os.path.join(release, "infinary_agent.py")
        # Record the OUTGOING release for crash-loop revert, and require the new one to re-prove
        # health (clear the markers) BEFORE flipping `current`.
        cur_link = _su_path("current")
        if os.path.islink(cur_link):
            _atomic_symlink(os.path.realpath(cur_link), _su_path("previous"))
        for marker in (".healthy", ".boot_attempts"):
            p = _su_path(marker)
            if os.path.exists(p):
                os.remove(p)
        _atomic_symlink(os.path.dirname(release_entry), cur_link)
        log(f"self-update staged to {version}; requesting restart")
        # Hand the restart to the OS supervisor. Only claim success once the launch itself
        # succeeds (a failed launch → 'blocked', not a false 'success'). If the new code is
        # broken at runtime, the boot-guard reverts after repeated crashes + the next healthy
        # heartbeat is the true confirmation.
        try:
            subprocess.Popen(SELF_UPDATE_RESTART)
        except Exception as re:
            ev(kind="terminal", outcome="blocked", message=f"Restart launch failed: {str(re)[:160]}")
            return
        ev(kind="terminal", outcome="success", message=f"Updated to {version}; restarting")
    except Exception as e:
        log(f"self-update failed: {e}")
        try:
            ev(kind="terminal", outcome="blocked", message=str(e)[:200])
        except Exception:
            pass
    finally:
        if staging and os.path.isdir(staging):
            shutil.rmtree(staging, ignore_errors=True)


def _capture_desired(desired: dict) -> None:
    """Stash desired-state from the heartbeat response so reporting + the engine can use it."""
    global _latest_agent_version, _pending_updates, _agent_artifact, _approved_image
    _latest_agent_version = desired.get("latestAgentVersion")
    _pending_updates = desired.get("availableAppUpdates") or []
    _agent_artifact = desired.get("agentArtifact")
    _approved_image = desired.get("approvedImage")


def _maybe_auto_update(desired: dict) -> None:
    """At most one managed run per cycle (single-flight; the begin endpoint is the server-side guard).
    Image-targeted: swap to the control-plane-curated image when it differs from the running one."""
    policy = desired.get("updatePolicy") or {}
    enabled = bool(policy.get("enabled"))
    scope = policy.get("scope", "both")

    # 1) Image point update (apps) — apply the curated image when it differs from ours.
    if _approved_image and scope in ("frappe_apps", "both"):
        cur = _current_image_safe()
        if cur and cur != _approved_image:
            # Customer's window if scheduled auto-update is on; else the always-on security window
            # (the covenant — a curated image is how we ship fixes on this topology).
            if enabled and _within_window(policy.get("time", ""), policy.get("windowMinutes") or 60, policy.get("timezone")):
                run_managed_update("auto_update", "auto", _approved_image, scope)
                return
            if desired.get("securityPatching") and _within_window(SECURITY_WINDOW, SECURITY_WINDOW_MIN, None):
                run_managed_update("security_patch", "security", _approved_image, scope)
                return

    # 2) Scheduled agent self-update within the customer window (decision #5: agents update in-window).
    if enabled and scope in ("agent", "both") and _agent_artifact and _agent_update_available():
        if _within_window(policy.get("time", ""), policy.get("windowMinutes") or 60, policy.get("timezone")):
            jid = _begin_run("agent_update", "agent", f"sidecar {AGENT_VERSION} -> {_agent_artifact.get('version')}")
            if jid:
                run_agent_self_update(_agent_artifact, jid, "auto")


def _self_check() -> int:
    """Validate the staged release can run: imports already succeeded (we're executing), so just
    confirm the required runtime env is present. Exit 0 = healthy → the caller flips the symlink."""
    missing = [k for k in ("INFINARY_CONTROL_PLANE", "INFINARY_INSTANCE_ID", "INFINARY_AGENT_TOKEN") if not os.environ.get(k)]
    if missing:
        print(f"selfcheck FAILED: missing env {missing}", file=sys.stderr)
        return 1
    print(f"selfcheck OK: infinary-agent {AGENT_VERSION}")
    return 0


def main() -> None:
    if not DRYRUN and not SITE:
        raise SystemExit("INFINARY_SITE is required (or set INFINARY_DRYRUN=1)")
    _self_update_boot_guard()  # crash-loop recovery before we do anything else
    log(f"{IID} -> {CP} every {PERIOD}s (outbound-only{', DRY-RUN' if DRYRUN else ''})")
    fails = 0
    healthy_marked = False
    while True:
        try:
            desired = heartbeat()
            if not healthy_marked:
                _mark_self_update_healthy()  # first successful heartbeat → this release is good
                healthy_marked = True
            _capture_desired(desired)
            if desired.get("paused"):
                # Kill-switch: halt ALL execution (jobs + auto-update) — a bad release can be
                # stopped fleet-wide from the control plane. We still heartbeat so liveness holds.
                log("paused by control plane — skipping jobs + auto-update this cycle")
            else:
                # Pulled jobs first (major upgrade, actions, staff-forced agent update)…
                jobs = poll_jobs()
                for job in jobs:
                    run_job(job)
                # …then the in-window auto-update engine — but ONLY in a cycle with no pulled jobs,
                # so a scheduled update never runs in the same cycle as a major upgrade / action
                # (explicit serialization on top of the control plane's single-flight lock). At
                # most one managed run per idle cycle.
                if not jobs:
                    _maybe_auto_update(desired)
            fails = 0
        except Exception as e:
            # Never die: the control plane treats a stale (>24h) heartbeat as
            # ineligible, so a crashed agent fails closed rather than allowing an
            # unsafe upgrade. Surface persistent failures loudly so they aren't silent.
            fails += 1
            log(f"cycle error ({fails} in a row): {e}")
            if fails == 10 or (fails > 10 and fails % 50 == 0):
                log(f"WARNING: {fails} consecutive failures — check INFINARY_* config, connectivity, and the agent token")
        time.sleep(PERIOD)


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        raise SystemExit(_self_check())
    main()
