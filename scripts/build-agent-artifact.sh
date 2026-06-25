#!/usr/bin/env bash
#
# Build (and optionally publish) the SERVER-PINNED sidecar self-update artifact.
#
# The control plane offers blue/green self-update only when AGENT_ARTIFACT_URL +
# AGENT_ARTIFACT_SHA256 are set on it. The agent then downloads the artifact, verifies the
# sha256, health-gates it with `--selfcheck` in a child process, and only then flips its
# `current` symlink (the OS supervisor owns the restart). This script produces that artifact
# from the sidecar in THIS repo and prints the exact env to set — so the artifact source is
# pinned by Infinary, never supplied by a job payload.
#
# Usage:
#   scripts/build-agent-artifact.sh                       # build + checksum only → ./dist
#   scripts/build-agent-artifact.sh gs://my-bucket/agent  # also upload (public-read) + print URL
#
# Requires: tar, sha256sum|shasum; gsutil only if a gs:// destination is given.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SIDECAR_DIR="$REPO_ROOT/sidecar"
OUT_DIR="$REPO_ROOT/dist"
DEST="${1:-}" # optional gs://bucket/prefix

VERSION="$(grep -oE 'AGENT_VERSION = "[^"]+"' "$SIDECAR_DIR/infinary_agent.py" | head -1 | sed -E 's/.*"([^"]+)".*/\1/')"
[ -n "$VERSION" ] || { echo "ERROR: could not read AGENT_VERSION from sidecar/infinary_agent.py" >&2; exit 1; }

mkdir -p "$OUT_DIR"
ARTIFACT="$OUT_DIR/infinary-agent-$VERSION.tgz"
# Tar the sidecar tree (infinary_agent.py + requirements.txt), excluding build noise. The agent's
# self-update unpacker (_find_entrypoint) locates infinary_agent.py inside whatever it unpacks to.
tar --exclude='__pycache__' --exclude='*.pyc' -czf "$ARTIFACT" -C "$SIDECAR_DIR" .

if command -v sha256sum >/dev/null 2>&1; then
  SHA="$(sha256sum "$ARTIFACT" | awk '{print $1}')"
else
  SHA="$(shasum -a 256 "$ARTIFACT" | awk '{print $1}')"
fi

echo "built    $ARTIFACT"
echo "version  $VERSION"
echo "sha256   $SHA"

URL=""
if [ -n "$DEST" ]; then
  OBJ="${DEST%/}/infinary-agent-$VERSION.tgz"
  gsutil cp "$ARTIFACT" "$OBJ"
  # Public-read so the agent can fetch it without GCS credentials. (Prefer `gsutil signurl`
  # below if you'd rather not expose a public object.)
  gsutil acl ch -u AllUsers:R "$OBJ"
  URL="https://storage.googleapis.com/${OBJ#gs://}"
  echo "uploaded $OBJ"
  echo "url      $URL"
fi

cat <<EOF

Set on the control plane (Cloud Run) to enable blue/green self-update:

  gcloud run services update infinary-control-plane --region us-central1 --project infinary-api \\
    --update-env-vars '^@^AGENT_ARTIFACT_URL=${URL:-<https-url-where-you-host-the-tgz>}@AGENT_ARTIFACT_SHA256=$SHA@LATEST_AGENT_VERSION=$VERSION'

(The ^@^ prefix makes '@' the delimiter so URLs/values containing commas or '=' aren't split.)

On each box, lay out the two-slot dir + point the service at the symlink:
  INFINARY_SELF_UPDATE_DIR=/opt/infinary-agent        # holds releases/<v>/ + the 'current' symlink
  systemd ExecStart=/usr/bin/python3 /opt/infinary-agent/current/infinary_agent.py
  # and allow the restart the agent requests:  %agent% ALL=(root) NOPASSWD: /bin/systemctl restart infinary-agent

NOTE: the agent uses one HTTP session, so its control-plane bearer token rides on the artifact
GET too. Host the artifact on Google Cloud Storage (the token is instance-scoped + only valid
against the control plane, and GCS ignores it). Use a signed URL if you don't want a public object.
EOF
