"""
infinary_agent.api — whitelisted methods that run INSIDE Frappe context.

The outbound-only sidecar (../sidecar/infinary_agent.py) calls
`infinary_agent.api.fingerprint` via `bench --site <site> execute` to read the
site's version vector + dual drift fingerprint. Nothing here listens on the
network and nothing here writes: the app observes, the sidecar reports home.
"""
import hashlib
import json
from pathlib import Path

import frappe

# UI-driven customizations a filesystem hash CANNOT see — the false-green source.
DRIFT_DOCTYPES = [
    "Custom Field",
    "Property Setter",
    "Server Script",
    "Client Script",
    "Custom DocType",
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
    custom_apps = [a for a in frappe.get_installed_apps() if a not in BASELINE_APPS]
    detected = {
        "customFields": counts.get("Custom Field", 0),
        "serverScripts": counts.get("Server Script", 0),
        "clientScripts": counts.get("Client Script", 0),
        "customDoctypes": counts.get("Custom DocType", 0),
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


@frappe.whitelist()
def fingerprint() -> str:
    """Returns the version vector + the dual drift fingerprint as a JSON string
    (the sidecar reads the last stdout line from `bench execute`)."""
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
    return json.dumps(
        {
            "versionVector": _version_vector(),
            "drift": {
                "filesystemHash": fs_hash,
                "dbCustomizationsHash": db_hash,
                "hasDrift": has_drift,
                "detected": detected,
            },
            "lastUpdateOutcome": "none",
        }
    )
