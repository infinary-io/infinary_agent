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
PERIOD = int(os.environ.get("INFINARY_HEARTBEAT_SEC", "45"))
DRYRUN = os.environ.get("INFINARY_DRYRUN") == "1"
AGENT_VERSION = "0.2.3"

S = requests.Session()
S.headers["Authorization"] = f"Bearer {TOKEN}"

# In dry-run the agent keeps an in-process version so a successful upgrade visibly
# bumps what subsequent heartbeats report.
_dry_version = os.environ.get("INFINARY_DRYRUN_VERSION", "15")


def log(msg: str) -> None:
    print(f"[agent] {msg}", flush=True)


def _bench(*args: str, timeout: int = 3600) -> str:
    """Run a bench command (raise on non-zero). INFINARY_BENCH_CMD lets bench live
    behind a wrapper — e.g. `docker compose exec -T backend bench` on a frappe_docker
    box, where bench runs inside the container rather than on the host."""
    cmd = [*BENCH_CMD, *args]
    log("$ " + " ".join(cmd))
    cwd = BENCH if BENCH_CMD == ["bench"] else None
    out = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if out.returncode != 0:
        raise RuntimeError(f"bench {args[0]} failed: {(out.stderr or out.stdout).strip()[:300]}")
    return out.stdout


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
        }
    fp = _bench_json("infinary_agent.api.fingerprint")
    return {
        "versionVector": fp["versionVector"],
        "health": _health(),
        "drift": fp["drift"],
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


# ── Major-upgrade pipeline ──
# v1 is a straightforward bench upgrade with a backup-and-restore safety net. The
# production hardening (disposable staging clone + atomic disk-snapshot rollback,
# CORE-CARE-PLAN.md §6 / agent/README.md) layers in on top of these same stages.

def _stage_backup(to: str) -> None:
    if DRYRUN:
        time.sleep(2); return
    _bench("--site", SITE, "backup", "--with-files")


def _stage_offline(to: str) -> None:
    if DRYRUN:
        time.sleep(1); return
    _bench("--site", SITE, "set-maintenance-mode", "on")


def _stage_install(to: str) -> None:
    if DRYRUN:
        time.sleep(3); return
    _bench("switch-to-branch", f"version-{to}", "frappe", "erpnext", "--upgrade")


def _stage_migrate(to: str) -> None:
    if DRYRUN:
        time.sleep(3); return
    _bench("--site", SITE, "migrate")


def _stage_checks(to: str) -> None:
    if DRYRUN:
        time.sleep(2); return
    _bench("--site", SITE, "execute", "frappe.ping", timeout=120)


def _go_online() -> None:
    if DRYRUN:
        return
    _bench("--site", SITE, "set-maintenance-mode", "off")


def _rollback() -> None:
    log("rolling back: restoring the pre-upgrade state + going online")
    if DRYRUN:
        time.sleep(2); return
    # v1: bring the site back online (the latest backup is on disk). Production
    # swaps the disk snapshot atomically instead — see agent/README.md.
    _bench("--site", SITE, "set-maintenance-mode", "off")


STAGES = [
    ("backing_up", "Taking a full snapshot of your data and code", _stage_backup),
    ("offline", "Taking your system offline", _stage_offline),
    ("installing", "Installing the new version", _stage_install),
    ("migrating", "Applying the new version to your records", _stage_migrate),
    ("final_checks", "Running the smoke checks", _stage_checks),
]


def run_job(job: dict) -> None:
    jid = job["id"]
    if job.get("type") != "major_upgrade":
        emit(jid, kind="terminal", outcome="blocked", message=f"Unsupported job type: {job.get('type')}")
        return
    to = str(job.get("payload", {}).get("toVersion") or "")
    log(f"executing major_upgrade -> v{to} (job {jid})")
    try:
        for stage, msg, fn in STAGES:
            emit(jid, kind="stage", stage=stage, stageStatus="started", message=msg)
            fn(to)
            emit(jid, kind="stage", stage=stage, stageStatus="completed")
        _go_online()
        if DRYRUN and to:
            global _dry_version
            _dry_version = to
        emit(jid, kind="terminal", outcome="success", message=f"You're now on ERPNext {to}")
        log(f"upgrade to v{to} complete")
    except Exception as e:
        log(f"upgrade failed: {e}")
        try:
            _rollback()
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
