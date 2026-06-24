"""
infinary_agent.api — whitelisted methods that run INSIDE Frappe context.

The outbound-only sidecar (../sidecar/infinary_agent.py) calls
`infinary_agent.api.fingerprint` via `bench --site <site> execute` to read the
site's version vector + dual drift fingerprint. Nothing here listens on the
network and nothing here writes: the app observes, the sidecar reports home.
"""
import hashlib
import sys
from pathlib import Path

import frappe

from infinary_agent import __version__ as APP_VERSION

# UI-driven customizations a filesystem hash CANNOT see — the false-green source.
# (User-created DocTypes are handled separately: there is no "Custom DocType"
# doctype — they are DocType rows flagged custom=1.)
DRIFT_DOCTYPES = [
    "Custom Field",
    "Property Setter",
    "Server Script",
    "Client Script",
]

# Apps that make up a clean Infinary instance. Anything else installed is itself
# drift (a customer-added app the golden manifest doesn't account for).
BASELINE_APPS = {"frappe", "erpnext", "infinary_agent"}


def _apps_dir() -> Path:
    # .../frappe-bench/apps  (frappe lives at apps/frappe/frappe)
    return Path(frappe.get_app_path("frappe")).parent.parent


def _filesystem_hash() -> str:
    """Hash the apps tree. The control plane compares this to the GOLDEN hash for
    the running version to flag filesystem drift (modified core / non-golden apps)."""
    h = hashlib.sha256()
    root = _apps_dir()
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        if ".git" in p.parts or "__pycache__" in p.parts or p.suffix == ".pyc":
            continue
        h.update(p.relative_to(root).as_posix().encode())
        h.update(p.read_bytes())
    return "sha256:" + h.hexdigest()


def _db_customizations() -> tuple[str, dict]:
    """Hash + count DB-stored customizations. Self-evident drift: any row here means
    the instance was customized, regardless of what the filesystem looks like."""
    h = hashlib.sha256()
    counts: dict[str, int] = {}
    for dt in DRIFT_DOCTYPES:
        rows = frappe.get_all(dt, fields=["name", "modified"], order_by="name")
        counts[dt] = len(rows)
        for r in rows:
            h.update(f"{dt}|{r['name']}|{r['modified']}".encode())
    # User-created DocTypes are DocType rows flagged custom=1 (there is no
    # "Custom DocType" doctype). Hash them under a stable label.
    custom_dts = frappe.get_all(
        "DocType", filters={"custom": 1}, fields=["name", "modified"], order_by="name"
    )
    for r in custom_dts:
        h.update(f"Custom DocType|{r['name']}|{r['modified']}".encode())
    custom_apps = [a for a in frappe.get_installed_apps() if a not in BASELINE_APPS]
    detected = {
        "customFields": counts.get("Custom Field", 0),
        "serverScripts": counts.get("Server Script", 0),
        "clientScripts": counts.get("Client Script", 0),
        "customDoctypes": len(custom_dts),
        "customApps": custom_apps,
    }
    return "sha256:" + h.hexdigest(), detected


def _version_vector() -> dict:
    vv = {"frappe": "unknown", "erpnext": "unknown", "apps": {}}
    for app in frappe.get_installed_apps():
        try:
            ver = str(frappe.get_attr(f"{app}.__version__"))
        except Exception:
            ver = "unknown"
        if app in ("frappe", "erpnext"):
            vv[app] = ver
        else:
            vv["apps"][app] = ver
    return vv


def _platform() -> dict:
    """Engine versions the control plane's `platform` gate checks against per-target floors."""
    python = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    try:
        mariadb = str(frappe.db.sql("SELECT VERSION()")[0][0]).split("-")[0]
    except Exception:
        mariadb = "unknown"
    return {"python": python, "mariadb": mariadb}


def _data_health() -> dict:
    """Migration health the `data_health` gate reads: pending patches (in each app's
    patches.txt but not yet in Patch Log) warn; failed patches block. Failed-patch
    detection isn't first-class in Frappe, so it stays 0 until a migrate dry-run surfaces it."""
    pending = 0
    try:
        applied = {r.patch for r in frappe.get_all("Patch Log", fields=["patch"])}
        for app in frappe.get_installed_apps():
            try:
                txt = Path(frappe.get_app_path(app)).parent / "patches.txt"
            except Exception:
                continue
            if not txt.exists():
                continue
            for line in txt.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or line.startswith("["):
                    continue
                if line not in applied:
                    pending += 1
    except Exception:
        pending = 0
    return {"pendingPatches": pending, "failedPatches": 0}


def _ai_spend_cents() -> int:
    """Action-mode AI spend this period (cents). 0 until the AI module accrues usage —
    the field exists now so the control plane's metering reads a real value once it does."""
    return 0


@frappe.whitelist()
def fingerprint() -> dict:
    """Returns the version vector + the dual drift fingerprint, plus the platform,
    data-health and AI-spend signals the control plane's gates + billing read. `bench
    execute` serialises this dict to one line of JSON, which the sidecar parses; it is
    also directly callable over Frappe's whitelisted-method API."""
    fs_hash = _filesystem_hash()
    db_hash, detected = _db_customizations()
    # DB customizations are self-evident drift; filesystem drift is decided by the
    # control plane comparing fs_hash to the golden hash for the running version.
    has_drift = bool(
        detected["customFields"]
        or detected["serverScripts"]
        or detected["clientScripts"]
        or detected["customDoctypes"]
        or detected["customApps"]
    )
    return {
        "versionVector": _version_vector(),
        "drift": {
            "filesystemHash": fs_hash,
            "dbCustomizationsHash": db_hash,
            "hasDrift": has_drift,
            "detected": detected,
        },
        "platform": _platform(),
        "dataHealth": _data_health(),
        "aiSpendCents": _ai_spend_cents(),
        "lastUpdateOutcome": "none",
        # The installed Frappe-app version, reported separately from the sidecar's own version.
        "agentAppVersion": APP_VERSION,
    }
