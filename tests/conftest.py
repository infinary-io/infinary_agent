"""Load the sidecar as an importable module for tests.

The sidecar is a single script (sidecar/infinary_agent.py) whose module name
collides with the Frappe app package, so it's loaded by file path under a
distinct name. Required env is set before exec (import runs no network or
subprocess code — module level only reads config and builds a Session).
"""
from __future__ import annotations

import importlib.util
import os
import pathlib
import sys

import pytest

_SIDECAR = pathlib.Path(__file__).resolve().parents[1] / "sidecar" / "infinary_agent.py"


def _load():
    os.environ.setdefault("INFINARY_CONTROL_PLANE", "https://cp.invalid")
    os.environ.setdefault("INFINARY_INSTANCE_ID", "inst-test")
    os.environ.setdefault("INFINARY_AGENT_TOKEN", "token-test")
    # Import in dry-run so nothing can ever touch bench/docker if a test forgets
    # to stub; covenant tests flip DRYRUN off per-test with monkeypatch.
    os.environ.setdefault("INFINARY_DRYRUN", "1")
    os.environ.setdefault("INFINARY_SITE", "erp.test")
    spec = importlib.util.spec_from_file_location("sidecar_infinary_agent", _SIDECAR)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["sidecar_infinary_agent"] = mod
    spec.loader.exec_module(mod)
    return mod


_MOD = _load()


@pytest.fixture()
def agent(monkeypatch):
    """The sidecar module with fast sleeps and a clean per-test slate."""
    monkeypatch.setattr(_MOD.time, "sleep", lambda _s: None)
    monkeypatch.setattr(_MOD, "_dry_version", "15")
    monkeypatch.setattr(_MOD, "_dry_image", "infinary/erpnext-fac:v16-2.6.2")
    monkeypatch.setattr(_MOD, "_approved_image", None)
    monkeypatch.setattr(_MOD, "_agent_artifact", None)
    monkeypatch.setattr(_MOD, "_latest_agent_version", None)
    monkeypatch.setattr(_MOD, "_pending_updates", [])
    return _MOD


@pytest.fixture()
def events(agent, monkeypatch):
    """Capture every job event the agent would POST to the control plane."""
    captured: list[dict] = []

    def fake_emit(job_id: str, **event):
        captured.append({"jobId": job_id, **event})

    monkeypatch.setattr(agent, "emit", fake_emit)
    return captured
