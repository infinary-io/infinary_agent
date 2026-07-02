"""The 5-stage major-upgrade pipeline, exercised end-to-end via the DryDriver
(the same path `INFINARY_DRYRUN=1` uses for manual smoke tests)."""
from __future__ import annotations


def stages(events):
    return [(e["stage"], e["stageStatus"]) for e in events if e.get("kind") == "stage"]


def terminal(events):
    return [e for e in events if e.get("kind") == "terminal"][-1]


def test_successful_upgrade_walks_all_stages_in_order(agent, events):
    agent.run_upgrade({"id": "u1", "payload": {"toVersion": "16"}})
    expected = []
    for stage, _msg, _m in agent.STAGES:
        expected += [(stage, "started"), (stage, "completed")]
    assert stages(events) == expected
    assert terminal(events)["outcome"] == "success"
    assert agent._dry_version == "16"  # the dry box visibly reports the new version


def test_failed_stage_triggers_rollback_and_reports_rolled_back(agent, events, monkeypatch):
    rolled = []
    monkeypatch.setattr(agent.DryDriver, "install", lambda self, ctx: (_ for _ in ()).throw(RuntimeError("install exploded")))
    monkeypatch.setattr(agent.DryDriver, "rollback", lambda self, ctx: rolled.append(True))
    agent.run_upgrade({"id": "u1", "payload": {"toVersion": "16"}})
    assert rolled == [True]
    t = terminal(events)
    assert t["outcome"] == "rolled_back"
    assert "install exploded" in t["message"]
    assert agent._dry_version == "15"  # never bumped


def test_rollback_error_still_reports_rolled_back_terminal(agent, events, monkeypatch):
    monkeypatch.setattr(agent.DryDriver, "migrate", lambda self, ctx: (_ for _ in ()).throw(RuntimeError("migrate exploded")))
    monkeypatch.setattr(agent.DryDriver, "rollback", lambda self, ctx: (_ for _ in ()).throw(RuntimeError("rollback also exploded")))
    agent.run_upgrade({"id": "u1", "payload": {"toVersion": "16"}})
    # the job must still terminate — a hung/eventless job would wedge single-flight
    assert terminal(events)["outcome"] == "rolled_back"


def test_unknown_driver_is_blocked_not_attempted(agent, events, monkeypatch):
    monkeypatch.setattr(agent, "DRYRUN", False)
    monkeypatch.setattr(agent, "UPGRADE_DRIVER", "kubernetes")
    agent.run_upgrade({"id": "u1", "payload": {"toVersion": "16"}})
    t = terminal(events)
    assert t["outcome"] == "blocked"
    assert "Unknown upgrade driver" in t["message"]
