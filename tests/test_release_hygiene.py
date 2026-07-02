"""Release hygiene: the version constants that must move together, and the
self-update artifact fetch that must NEVER carry the control-plane bearer."""
from __future__ import annotations

import pathlib
import re

_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _app_version() -> str:
    # Read by path — importing `infinary_agent` would collide with the sidecar module name.
    text = (_ROOT / "infinary_agent" / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r'^__version__ = "([0-9.]+)"$', text, re.M)
    assert m, "no __version__ in infinary_agent/__init__.py"
    return m.group(1)


def test_dryrun_app_version_mirrors_the_frappe_app(agent):
    assert agent.AGENT_APP_VERSION_DRYRUN == _app_version(), (
        "sidecar AGENT_APP_VERSION_DRYRUN must track infinary_agent.__version__ — "
        "stamp both via scripts/release.sh app <version>"
    )


class _FakeResponse:
    def __init__(self):
        self.status_code = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def raise_for_status(self):
        pass

    def iter_content(self, _size):
        yield b"not-the-real-artifact"


def test_artifact_download_never_carries_the_bearer_token(agent, events, monkeypatch, tmp_path):
    fetches: list[tuple[str, dict]] = []

    def fake_get(url, **kwargs):
        fetches.append((url, kwargs))
        return _FakeResponse()

    def forbidden_session_get(*_a, **_k):
        raise AssertionError("artifact download used the bearer-carrying session S")

    monkeypatch.setattr(agent.requests, "get", fake_get)
    monkeypatch.setattr(agent.S, "get", forbidden_session_get)
    monkeypatch.setattr(agent, "DRYRUN", False)
    monkeypatch.setattr(agent, "SELF_UPDATE_DIR", str(tmp_path))

    artifact = {"version": "9.9.9", "url": "https://storage.googleapis.com/x/agent.tgz", "sha256": "0" * 64}
    agent.run_agent_self_update(artifact, "j1", "action")

    assert [u for u, _ in fetches] == [artifact["url"]]
    _, kwargs = fetches[0]
    assert "headers" not in kwargs and "auth" not in kwargs  # unauthenticated by design
    # the fake bytes can't match the pinned sha — the update must refuse, checksum-gated
    terminal = [e for e in events if e.get("kind") == "terminal"][-1]
    assert terminal["outcome"] == "blocked"
    assert "checksum" in terminal["message"].lower()
