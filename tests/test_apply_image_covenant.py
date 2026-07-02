"""The point-update engine's safety covenant (`_apply_image`).

These pin the behaviors that make automatic updates safe to run unattended:
fail-closed pre-flight, snapshot-or-skip, image-revert-only before migrate,
restore-before-revert after migrate, and — the covenant — never reverting
code onto a DB whose migration snapshot could not be restored.
"""
from __future__ import annotations

import pytest

TARGET = "ghcr.io/infinary/erpnext-fac:v16-2.7.0"
OLD = "ghcr.io/infinary/erpnext-fac:v16-2.6.2"


class ComposeBox:
    """Stubs every host-touching helper with an ordered call log and
    scriptable failure triggers."""

    def __init__(self, agent, monkeypatch):
        self.agent = agent
        self.log: list[tuple] = []
        self.fail_on: set[str] = set()
        self.current_image = OLD
        self._ups = 0
        monkeypatch.setattr(agent, "DRYRUN", False)
        monkeypatch.setattr(agent, "UPGRADE_DRIVER", "compose")
        monkeypatch.setattr(agent, "DB_ROOT_PASSWORD", "pw")
        monkeypatch.setattr(agent, "_compose_image", lambda: self.current_image)
        monkeypatch.setattr(agent, "_compose", self._compose)
        monkeypatch.setattr(agent, "_compose_set_image", self._swap)
        monkeypatch.setattr(agent, "_bench", self._bench)
        monkeypatch.setattr(agent, "_run", self._run)
        monkeypatch.setattr(agent, "_wait_ready", lambda tries=60: self.log.append(("wait_ready",)))

    def _compose(self, *args, timeout=0):
        self.log.append(("compose", args))
        if args and args[0] == "exec":
            return "" if "no_backup" in self.fail_on else "sites/erp.test/private/backups/dump-database-enc.sql.gz\n"
        if args[:2] == ("up", "-d"):
            self._ups += 1
            if "first_up" in self.fail_on and self._ups == 1:
                raise RuntimeError("compose up failed")
        return ""

    def _swap(self, old, new):
        self.log.append(("swap", old, new))
        self.current_image = new

    def _bench(self, *args, timeout=0):
        self.log.append(("bench", args))
        if "migrate" in args and "migrate" in self.fail_on:
            raise RuntimeError("migrate blew up")
        if "restore" in args and "restore" in self.fail_on:
            raise RuntimeError("restore blew up")
        return ""

    def _run(self, cmd, timeout=0, cwd=None):
        self.log.append(("run", tuple(cmd)))
        return ""

    # -- assertions ---------------------------------------------------------
    def swaps(self):
        return [e for e in self.log if e[0] == "swap"]

    def bench_calls(self, verb):
        return [e for e in self.log if e[0] == "bench" and verb in e[1]]

    def index_of(self, predicate):
        for i, e in enumerate(self.log):
            if predicate(e):
                return i
        raise AssertionError(f"no log entry matched: {self.log}")


@pytest.fixture()
def box(agent, monkeypatch):
    return ComposeBox(agent, monkeypatch)


def terminal(events):
    terms = [e for e in events if e.get("kind") == "terminal"]
    assert terms, f"no terminal event in {events}"
    return terms[-1]


def test_fail_closed_when_driver_is_not_compose(agent, events, box, monkeypatch):
    monkeypatch.setattr(agent, "UPGRADE_DRIVER", "bench")
    agent._apply_image("j1", "auto", TARGET)
    assert terminal(events)["outcome"] == "skipped"
    assert box.swaps() == []


def test_fail_closed_without_db_root_password(agent, events, box, monkeypatch):
    monkeypatch.setattr(agent, "DB_ROOT_PASSWORD", "")
    agent._apply_image("j1", "auto", TARGET)
    assert terminal(events)["outcome"] == "skipped"
    assert box.swaps() == []


def test_noop_when_already_on_target(agent, events, box):
    box.current_image = TARGET
    agent._apply_image("j1", "auto", TARGET)
    assert terminal(events)["outcome"] == "success"
    assert box.swaps() == []
    assert box.bench_calls("backup") == []


def test_skips_when_no_restorable_backup_is_captured(agent, events, box):
    box.fail_on = {"no_backup"}
    agent._apply_image("j1", "auto", TARGET)
    t = terminal(events)
    assert t["outcome"] == "skipped"
    assert "restorable" in t["message"]
    assert box.swaps() == []  # nothing was touched


def test_pre_migrate_failure_reverts_image_without_db_restore(agent, events, box):
    box.fail_on = {"first_up"}
    agent._apply_image("j1", "auto", TARGET)
    assert terminal(events)["outcome"] == "rolled_back"
    # swap out, then swap straight back — schema was never touched
    assert box.swaps() == [("swap", OLD, TARGET), ("swap", TARGET, OLD)]
    assert box.bench_calls("restore") == []
    assert box.current_image == OLD


def test_migrate_failure_restores_db_before_reverting_image(agent, events, box):
    box.fail_on = {"migrate"}
    agent._apply_image("j1", "auto", TARGET)
    assert terminal(events)["outcome"] == "rolled_back"
    restore_i = box.index_of(lambda e: e[0] == "bench" and "restore" in e[1])
    revert_i = box.index_of(lambda e: e == ("swap", TARGET, OLD))
    assert restore_i < revert_i, "the DB must be restored BEFORE the code is downgraded"
    assert box.current_image == OLD


def test_covenant_restore_failure_never_downgrades_code(agent, events, box):
    box.fail_on = {"migrate", "restore"}
    agent._apply_image("j1", "auto", TARGET)
    t = terminal(events)
    assert t["outcome"] == "failed"
    assert "manual recovery" in t["message"]
    # the image must stay on the NEW version — never old code on a migrated DB
    assert box.swaps() == [("swap", OLD, TARGET)]
    assert box.current_image == TARGET


def test_maintenance_mode_is_cleared_even_when_rollback_fails(agent, events, box):
    box.fail_on = {"migrate", "restore"}
    agent._apply_image("j1", "auto", TARGET)
    on_i = box.index_of(lambda e: e[0] == "bench" and "set-maintenance-mode" in e[1] and "on" in e[1])
    offs = [i for i, e in enumerate(box.log)
            if e[0] == "bench" and "set-maintenance-mode" in e[1] and "off" in e[1]]
    assert offs and offs[-1] > on_i, "the site must never be left dark"


def test_dryrun_applies_without_touching_the_host(agent, events, box, monkeypatch):
    monkeypatch.setattr(agent, "DRYRUN", True)
    agent._apply_image("j1", "auto", TARGET)
    assert terminal(events)["outcome"] == "success"
    assert box.log == []  # capability check passes in dry-run; nothing else runs
    assert agent._dry_image == TARGET
