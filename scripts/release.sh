#!/usr/bin/env bash
# Stamp a coordinated release — the versions that must move together, in one command.
#
#   scripts/release.sh sidecar 0.7.5   # bump the sidecar (AGENT_VERSION)
#   scripts/release.sh app 0.3.3       # bump the Frappe app (__version__ + the dry-run mirror)
#
# Runs the test suite after stamping (test_release_hygiene pins the app/mirror sync),
# then prints the follow-ups that live outside this repo.
set -euo pipefail
cd "$(dirname "$0")/.."

kind="${1:?usage: release.sh sidecar|app <semver>}"
ver="${2:?usage: release.sh sidecar|app <semver>}"
[[ "$ver" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "not a semver: $ver" >&2; exit 1; }

case "$kind" in
  sidecar)
    sed -i.bak -E "s/^AGENT_VERSION = \"[0-9.]+\"/AGENT_VERSION = \"$ver\"/" sidecar/infinary_agent.py
    rm -f sidecar/infinary_agent.py.bak
    ;;
  app)
    sed -i.bak -E "s/^__version__ = \"[0-9.]+\"/__version__ = \"$ver\"/" infinary_agent/__init__.py
    rm -f infinary_agent/__init__.py.bak
    sed -i.bak -E "s/^AGENT_APP_VERSION_DRYRUN = \"[0-9.]+\"/AGENT_APP_VERSION_DRYRUN = \"$ver\"/" sidecar/infinary_agent.py
    rm -f sidecar/infinary_agent.py.bak
    ;;
  *)
    echo "unknown kind: $kind (sidecar|app)" >&2; exit 1
    ;;
esac

python3 -m pytest tests -q

cat <<EOF

Stamped $kind $ver. Follow-ups outside this repo:
  sidecar releases:
    scripts/build-agent-artifact.sh [gs://bucket/prefix/]   # build + host the self-update tgz
    …then set the control-plane env it prints (AGENT_ARTIFACT_URL / AGENT_ARTIFACT_SHA256 /
    LATEST_AGENT_VERSION) so the fleet is offered the update in-window.
  app releases:
    git tag v$ver && git push --tags
    …then bump infra/golden-image/versions.json agentRef in infinary-io/infinary.io so new
    golden images bake this release, and dispatch the golden-image workflow.
EOF
