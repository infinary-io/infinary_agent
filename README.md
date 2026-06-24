# Infinary Agent

The **outbound-only** site agent that runs on every ERPNext instance Infinary
manages. It is how a customer's box reports home so the control plane
(`api.infinary.io`) can offer **safe, one-click major upgrades** — without ever
opening an inbound port on the customer's machine.

It comes in two halves:

| Part | Runs | Job |
|------|------|-----|
| **Frappe app** (`infinary_agent/`) | inside Frappe | Exposes one whitelisted read method, `infinary_agent.api.fingerprint`, returning the version vector + a **dual drift fingerprint**. Stores nothing, writes nothing, listens on nothing. |
| **Sidecar** (`sidecar/infinary_agent.py`) | as a system process next to the bench | Heartbeats the fingerprint to the control plane, long-polls for jobs, and executes a major upgrade locally (backup → offline → install → migrate → checks, with rollback). The **only** network traffic is outbound HTTPS. |

## Why a dual fingerprint

A filesystem hash alone gives a **false green**: a site can look pristine on disk
while being heavily customized through the Desk UI (Custom Fields, Server/Client
Scripts, Property Setters, Custom DocTypes). Those live in the **database**, not
the filesystem. The agent hashes **both**:

- `filesystemHash` — the apps tree (control plane compares to the golden hash for the running version).
- `dbCustomizationsHash` — every UI-driven customization row. Any of these ⇒ `hasDrift: true`.

Drift is the signal that an upgrade needs a human in the loop instead of running unattended.

## Install on a site

```bash
# in the bench (or `docker compose exec backend` on a frappe_docker box):
bench get-app https://github.com/infinary-io/infinary_agent
bench --site <site> install-app infinary_agent
```

Verify the fingerprint:

```bash
bench --site <site> execute infinary_agent.api.fingerprint
```

### Drift test

```bash
# Before: hasDrift=false on a clean site.
bench --site <site> execute infinary_agent.api.fingerprint | python -m json.tool

# Add a Custom Field through Desk (or bench console), then re-run — hasDrift flips true
# and detected.customFields increments. That's the false-green the fingerprint closes.
```

## Run the sidecar

The sidecar is configured entirely by environment, authenticates with the
**per-instance bearer token** issued at provisioning, and is safe to leave
running (it never dies; a stale heartbeat fails closed).

```bash
pip install -r sidecar/requirements.txt
INFINARY_CONTROL_PLANE=https://api.infinary.io \
INFINARY_INSTANCE_ID=inst_xxx \
INFINARY_AGENT_TOKEN=agt_xxx \
INFINARY_SITE=<site> \
python sidecar/infinary_agent.py
```

| Env | Default | Notes |
|-----|---------|-------|
| `INFINARY_CONTROL_PLANE` | — | e.g. `https://api.infinary.io` |
| `INFINARY_INSTANCE_ID` | — | from the provision response |
| `INFINARY_AGENT_TOKEN` | — | per-instance bearer token (provision response) |
| `INFINARY_SITE` | — | the Frappe site name (required unless dry-run) |
| `INFINARY_BENCH` | `/home/frappe/frappe-bench` | bench path |
| `INFINARY_HEARTBEAT_SEC` | `45` | loop cadence |
| `INFINARY_DRYRUN` | — | `1` fakes the bench (heartbeat + upgrade) for local testing |
| `INFINARY_DRYRUN_VERSION` | `15` | dry-run ERPNext version — a step behind LATEST so a dry-run demos an upgrade |

### Quick local test (no Frappe)

```bash
INFINARY_CONTROL_PLANE=http://localhost:8080 INFINARY_INSTANCE_ID=inst_demo \
INFINARY_AGENT_TOKEN=agt_demo INFINARY_DRYRUN=1 INFINARY_HEARTBEAT_SEC=2 \
python sidecar/infinary_agent.py
```

## Default install on managed instances

This app is installed on **every** Infinary-provisioned ERPNext instance. The
provisioning flow bakes it into the site's image via frappe_docker's custom-app
build (so it survives container recreate) and runs `install-app infinary_agent`
after the site is created. See `infra/erpnext-instance` in the `infinary.io`
repo.

## License

MIT — see [license.txt](license.txt).
