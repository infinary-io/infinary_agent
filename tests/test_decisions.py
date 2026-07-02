"""Pure decision logic: version compare, update windows, job dispatch, and
the auto-update engine's choice of run kind."""
from __future__ import annotations

from datetime import timedelta, timezone


# ── _cmp_version ────────────────────────────────────────────────────────────

def test_cmp_version_orders_numerically(agent):
    assert agent._cmp_version("1.2", "1.10") == -1
    assert agent._cmp_version("2.0.1", "2.0.0") == 1
    assert agent._cmp_version("2", "2.0.0") == 0


def test_cmp_version_treats_non_numeric_chunks_as_zero(agent):
    assert agent._cmp_version("abc", "0") == 0
    assert agent._cmp_version("1.beta", "1.0") == 0


def test_agent_update_available_only_when_newer(agent, monkeypatch):
    monkeypatch.setattr(agent, "_latest_agent_version", agent.AGENT_VERSION)
    assert agent._agent_update_available() is None
    monkeypatch.setattr(agent, "_latest_agent_version", "999.0.0")
    assert agent._agent_update_available() == "999.0.0"


# ── _within_window ──────────────────────────────────────────────────────────

def _hhmm(dt):
    return dt.strftime("%H:%M")


def test_window_open_at_start(agent):
    now = agent.datetime.now(timezone.utc)
    assert agent._within_window(_hhmm(now), 60, None) is True


def test_window_closed_after_it_ends(agent):
    now = agent.datetime.now(timezone.utc)
    assert agent._within_window(_hhmm(now - timedelta(minutes=120)), 60, None) is False


def test_window_closed_before_it_starts(agent):
    now = agent.datetime.now(timezone.utc)
    assert agent._within_window(_hhmm(now + timedelta(minutes=30)), 60, None) is False


def test_window_malformed_time_is_closed(agent):
    assert agent._within_window("bogus", 60, None) is False
    assert agent._within_window("", 60, None) is False


def test_window_force_override(agent, monkeypatch):
    monkeypatch.setattr(agent, "FORCE_WINDOW", True)
    assert agent._within_window("bogus", 60, None) is True


def test_window_bad_timezone_falls_back_to_utc(agent):
    now = agent.datetime.now(timezone.utc)
    assert agent._within_window(_hhmm(now), 60, "Not/AZone") is True


# ── run_job dispatch ────────────────────────────────────────────────────────

def terminal(events):
    terms = [e for e in events if e.get("kind") == "terminal"]
    assert terms, f"no terminal event in {events}"
    return terms[-1]


def test_unknown_job_type_is_blocked(agent, events):
    agent.run_job({"id": "j1", "type": "frobnicate"})
    t = terminal(events)
    assert t["outcome"] == "blocked"
    assert "Unsupported job type" in t["message"]


def test_app_install_is_reported_not_attempted(agent, events):
    agent.run_job({"id": "j1", "type": "app_install"})
    t = terminal(events)
    assert t["outcome"] == "skipped"
    assert "rebuilt image" in t["message"]


def test_agent_update_without_artifact_is_skipped(agent, events):
    agent.run_job({"id": "j1", "type": "agent_update"})
    assert terminal(events)["outcome"] == "skipped"


def test_update_now_without_any_target_is_skipped(agent, events):
    agent.run_job({"id": "j1", "type": "update_now", "payload": {}})
    t = terminal(events)
    assert t["outcome"] == "skipped"
    assert "No target image" in t["message"]


def test_update_now_already_on_target_is_a_noop_success(agent, events):
    agent.run_job({"id": "j1", "type": "update_now",
                   "payload": {"targetImage": agent._dry_image}})
    assert terminal(events)["outcome"] == "success"
    assert "Already on" in terminal(events)["message"]


def test_update_now_applies_the_payload_target(agent, events):
    agent.run_job({"id": "j1", "type": "update_now",
                   "payload": {"targetImage": "new:image"}})
    assert terminal(events)["outcome"] == "success"
    assert agent._dry_image == "new:image"


def test_update_now_falls_back_to_the_heartbeat_approved_image(agent, events, monkeypatch):
    monkeypatch.setattr(agent, "_approved_image", "approved:image")
    agent.run_job({"id": "j1", "type": "update_now", "payload": {}})
    assert terminal(events)["outcome"] == "success"
    assert agent._dry_image == "approved:image"


# ── _maybe_auto_update ──────────────────────────────────────────────────────

class Recorder:
    def __init__(self):
        self.managed: list[tuple] = []
        self.self_updates: list[tuple] = []

    def run_managed_update(self, kind, run_type, target, scope):
        self.managed.append((kind, run_type, target, scope))

    def begin(self, kind, scope, summary):
        return "job-from-begin"

    def run_self_update(self, artifact, jid, run_type):
        self.self_updates.append((artifact, jid, run_type))


def wire(agent, monkeypatch, *, current="old:img", window_open=True):
    rec = Recorder()
    monkeypatch.setattr(agent, "run_managed_update", rec.run_managed_update)
    monkeypatch.setattr(agent, "_begin_run", rec.begin)
    monkeypatch.setattr(agent, "run_agent_self_update", rec.run_self_update)
    monkeypatch.setattr(agent, "_current_image_safe", lambda: current)
    monkeypatch.setattr(agent, "_within_window", lambda *a, **k: window_open)
    return rec


def test_customer_window_runs_an_auto_update(agent, monkeypatch):
    rec = wire(agent, monkeypatch)
    monkeypatch.setattr(agent, "_approved_image", "new:img")
    agent._maybe_auto_update({"updatePolicy": {"enabled": True, "scope": "both", "time": "01:00"}})
    assert rec.managed == [("auto_update", "auto", "new:img", "both")]


def test_security_patching_runs_without_a_customer_schedule(agent, monkeypatch):
    rec = wire(agent, monkeypatch)
    monkeypatch.setattr(agent, "_approved_image", "new:img")
    agent._maybe_auto_update({"securityPatching": True})
    assert rec.managed == [("security_patch", "security", "new:img", "both")]


def test_nothing_runs_without_schedule_or_security_flag(agent, monkeypatch):
    rec = wire(agent, monkeypatch)
    monkeypatch.setattr(agent, "_approved_image", "new:img")
    agent._maybe_auto_update({})
    assert rec.managed == []


def test_nothing_runs_when_already_on_the_approved_image(agent, monkeypatch):
    rec = wire(agent, monkeypatch, current="new:img")
    monkeypatch.setattr(agent, "_approved_image", "new:img")
    agent._maybe_auto_update({"updatePolicy": {"enabled": True, "scope": "both"}, "securityPatching": True})
    assert rec.managed == []


def test_agent_scope_excludes_image_updates_but_self_updates(agent, monkeypatch):
    rec = wire(agent, monkeypatch)
    monkeypatch.setattr(agent, "_approved_image", "new:img")
    monkeypatch.setattr(agent, "_agent_artifact", {"version": "999.0.0"})
    monkeypatch.setattr(agent, "_latest_agent_version", "999.0.0")
    agent._maybe_auto_update({"updatePolicy": {"enabled": True, "scope": "agent", "time": "01:00"}})
    assert rec.managed == []
    assert rec.self_updates == [({"version": "999.0.0"}, "job-from-begin", "auto")]


def test_closed_window_defers_everything(agent, monkeypatch):
    rec = wire(agent, monkeypatch, window_open=False)
    monkeypatch.setattr(agent, "_approved_image", "new:img")
    agent._maybe_auto_update({"updatePolicy": {"enabled": True, "scope": "both"}, "securityPatching": True})
    assert rec.managed == []
    assert rec.self_updates == []
