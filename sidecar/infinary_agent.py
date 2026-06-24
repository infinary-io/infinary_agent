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
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import time

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
AGENT_VERSION = "0.4.0"

S = requests.Session()
S.headers["Authorization"] = f"Bearer {TOKEN}"

# In dry-run the agent keeps an in-process version so a successful upgrade visibly
# bumps what subsequent heartbeats report.
_dry_version = os.environ.get("INFINARY_DRYRUN_VERSION", "15")


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
            "platform": {"python": "3.11.0", "mariadb": "11.8.0"},
            "dataHealth": {"pendingPatches": 0, "failedPatches": 0},
            "aiSpendCents": 0,
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
    }


def heartbeat() -> None:
    r = S.post(f"{CP}/v1/agent/{IID}/heartbeat", json=collect_reported_state(), timeout=30)
    r.raise_for_status()


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
            out = _compose("exec", "-T", COMPOSE_SERVICE, "bash", "-lc",
                           f"ls -t sites/{SITE}/private/backups/*-database.sql.gz | head -1",
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


def run_job(job: dict) -> None:
    jid = job["id"]
    if job.get("type") != "major_upgrade":
        emit(jid, kind="terminal", outcome="blocked", message=f"Unsupported job type: {job.get('type')}")
        return
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


def main() -> None:
    if not DRYRUN and not SITE:
        raise SystemExit("INFINARY_SITE is required (or set INFINARY_DRYRUN=1)")
    log(f"{IID} -> {CP} every {PERIOD}s (outbound-only{', DRY-RUN' if DRYRUN else ''})")
    fails = 0
    while True:
        try:
            heartbeat()
            for job in poll_jobs():
                run_job(job)
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
    main()
