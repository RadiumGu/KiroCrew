"""An orchestrator plan must survive a gateway restart with a resume path.

The autopilot's execution pointer lived only in memory: ``_stage_titles``,
``_plan_goal``, ``_auto_run`` and the ``OrchestrationTracker``'s round /
escalation / stage-result ledger all hang off ``_ChatSlot``, and the dashboard's
slot save serialised none of them. The per-stage result FILES survived a restart,
so the work was on disk, but nothing recorded which stage was next -- a restart
mid-plan lost the run outright, with no way to pick it back up (issue #1783).

Three properties are asserted here, and they are separable:

* the tracker's ledger round-trips through a plain JSON snapshot, and the
  interrupted stage's rounds are dropped so a resumed loop re-runs that stage
  instead of stepping past it;
* the slot save writes the plan record, and both rehydration paths read it back;
* a restored, unfinished plan surfaces a resume offer -- and a plan that
  finished, was cancelled, or never started surfaces nothing.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_ready_kiro_prerequisite

from kiro_crew.context_management import OrchestrationTracker
from kiro_crew.dashboard.chat_persistence import (
    _PLAN_RESUME_MARKER,
    _plan_state_for_save,
    _rehydrate_slot_from_history,
    _restore_plan_state,
    _save_slot_to_history,
    restore_recent_sessions,
)
from kiro_crew.dashboard.state import DashboardState, _ChatSlot
from kiro_crew.history import SLOT_OWNED_META_KEYS, ConversationLog


def _make_state(tmp_path) -> DashboardState:
    sessions = MagicMock(count=0)
    sessions.remove = AsyncMock()
    sessions.recycle_background = AsyncMock()
    sessions.get_pid = MagicMock(return_value=None)
    state = DashboardState(
        sessions=sessions,
        crons=MagicMock(list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})),
        lessons=MagicMock(load_all=MagicMock(return_value=[])),
        start_time=0.0,
        conversation_log=ConversationLog(base_dir=tmp_path),
    )
    state.kiro_prerequisite_service = _make_ready_kiro_prerequisite()
    return state


def _plan_slot(
    state,
    name="plan-slot",
    *,
    titles=("Recon", "Implement", "Verify"),
    results=(1,),
    rounds=None,
    escalations=None,
    auto_run=True,
):
    """A live orchestrator slot mid-plan: *results* stages have finished."""
    slot = state.get_or_create_slot(name)
    slot.mode = "orchestrator"
    slot._stage_titles = list(titles)
    slot._stage_descriptions = [[f"- do {t.lower()}"] for t in titles]
    slot._plan_goal = "Ship the thing"
    slot._auto_run = auto_run
    tracker = OrchestrationTracker()
    for stage in rounds if rounds is not None else range(1, len(results) + 2):
        tracker.record_round(stage)
    for stage in results:
        tracker.record_stage_result(stage, f"/tmp/stage_{stage}_result.md")
    for stage in escalations or ():
        tracker._stage_escalations[stage] = tracker._stage_escalations.get(stage, 0) + 1
    slot._orch_tracker = tracker
    slot.append("user", "plan it")
    slot.drain()
    return slot


# ── The tracker's own snapshot contract ──────────────────────────────────────


class TestTrackerSnapshot:
    def test_snapshot_is_json_shaped(self):
        """Keys are strings: this record round-trips through the history JSONL."""
        tracker = OrchestrationTracker()
        tracker.record_round(1)
        tracker.record_stage_result(1, "/tmp/one.md")

        snap = tracker.snapshot()

        assert snap == {
            "stage_rounds": {"1": 1},
            "stage_escalations": {},
            "stage_results": {"1": "/tmp/one.md"},
        }

    def test_resume_stage_is_the_first_stage_with_no_result(self):
        tracker = OrchestrationTracker()
        tracker.record_stage_result(1, "/tmp/one.md")
        tracker.record_stage_result(2, "/tmp/two.md")

        assert tracker.resume_stage() == 3

    def test_resume_stage_is_one_when_nothing_finished(self):
        assert OrchestrationTracker().resume_stage() == 1

    def test_interrupted_stage_rounds_are_dropped_on_restore(self):
        """RED BEFORE: the resumed loop must re-run the interrupted stage.

        ``_stage_loop`` derives its starting index from ``current_stage`` -- the
        highest stage with a recorded round -- so a restore that carried stage
        3's round through would start the resumed plan at stage 4 and silently
        skip the work that was interrupted.
        """
        live = OrchestrationTracker()
        for stage in (1, 2, 3):
            live.record_round(stage)
        live.record_stage_result(1, "/tmp/one.md")
        live.record_stage_result(2, "/tmp/two.md")

        restored = OrchestrationTracker.from_snapshot(live.snapshot())

        assert restored.resume_stage() == 3
        # current_stage is what the loop turns into its 0-based start index.
        assert restored.current_stage == 2
        assert restored.round_count(3) == 0

    def test_escalations_survive_restore_whole(self):
        """The harder cap must not be launderable by restarting the gateway."""
        live = OrchestrationTracker()
        live.record_round(2)
        live._stage_escalations[2] = 2

        restored = OrchestrationTracker.from_snapshot(live.snapshot())

        assert restored.is_force_failed(2) is True

    def test_stage_results_survive_restore(self):
        live = OrchestrationTracker()
        live.record_stage_result(1, "/tmp/one.md")

        restored = OrchestrationTracker.from_snapshot(live.snapshot())

        assert restored._stage_results == {1: "/tmp/one.md"}

    @pytest.mark.parametrize(
        "payload",
        [
            {},
            {"stage_rounds": "not-a-dict"},
            {"stage_results": {"abc": "/tmp/x.md"}},
            {"stage_results": {"0": "/tmp/x.md"}},
            {"stage_rounds": {"1": "many"}},
        ],
    )
    def test_malformed_snapshot_does_not_raise(self, payload):
        """The record is a file on disk: a hand-edit must degrade, not crash."""
        restored = OrchestrationTracker.from_snapshot(payload)

        assert restored.resume_stage() == 1


# ── What the save writes ─────────────────────────────────────────────────────


class TestPlanStateIsPersisted:
    def test_plan_is_a_slot_owned_metadata_key(self):
        """Absence must mean CLEARED, or a finished plan is re-offered forever."""
        assert "plan" in SLOT_OWNED_META_KEYS

    def test_save_writes_the_plan_record(self, tmp_path, monkeypatch):
        """RED BEFORE: chat_persistence serialised no plan state at all."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = _plan_slot(state)

        _save_slot_to_history(state, slot, closed=False)

        meta = state.conversation_log._read_metadata("dashboard:plan-slot")
        plan = meta.get("plan")
        assert plan, "the plan record was not persisted"
        assert plan["goal"] == "Ship the thing"
        assert plan["stage_titles"] == ["Recon", "Implement", "Verify"]
        assert plan["stage_descriptions"][0] == ["- do recon"]
        assert plan["auto_run"] is True
        assert plan["tracker"]["stage_results"] == {"1": "/tmp/stage_1_result.md"}

    def test_non_orchestrator_slot_writes_nothing(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("plain")
        slot.append("user", "hi")
        slot.drain()

        _save_slot_to_history(state, slot, closed=False)

        assert "plan" not in state.conversation_log._read_metadata("dashboard:plain")

    def test_completed_plan_is_not_persisted(self, tmp_path, monkeypatch):
        """Every stage has a result: re-entering the loop would re-emit the summary."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = _plan_slot(state, titles=("One", "Two"), results=(1, 2), rounds=(1, 2))

        assert _plan_state_for_save(slot) == {}

    def test_cancelled_plan_is_not_persisted(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = _plan_slot(state)
        slot._plan_cancelled = True

        assert _plan_state_for_save(slot) == {}

    def test_stopped_tracker_is_not_persisted(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = _plan_slot(state)
        slot._orch_tracker.stop()

        assert _plan_state_for_save(slot) == {}

    def test_armed_but_unstarted_plan_still_persists_its_titles(self, tmp_path, monkeypatch):
        """The transcript's own Go buttons need the stage list to mean anything."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("armed")
        slot.mode = "orchestrator"
        slot._stage_titles = ["Only"]
        slot._orch_tracker = None

        record = _plan_state_for_save(slot)

        assert record["stage_titles"] == ["Only"]
        assert record["tracker"] == {}

    def test_a_later_save_clears_a_finished_plan(self, tmp_path, monkeypatch):
        """The record must not outlive the plan it describes."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = _plan_slot(state, titles=("One", "Two"), results=(1,), rounds=(1, 2))
        _save_slot_to_history(state, slot, closed=False)
        assert state.conversation_log._read_metadata("dashboard:plan-slot").get("plan")

        slot._orch_tracker.record_stage_result(2, "/tmp/stage_2_result.md")
        slot.append("assistant", "done")
        slot.drain()
        _save_slot_to_history(state, slot, closed=False)

        assert "plan" not in state.conversation_log._read_metadata("dashboard:plan-slot")


# ── What the restore reads back ──────────────────────────────────────────────


class TestPlanStateIsRestored:
    def test_rehydrate_restores_the_execution_pointer(self, tmp_path, monkeypatch):
        """RED BEFORE: a restarted gateway came back with no plan at all."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        _plan_slot(state)
        _save_slot_to_history(state, state._slots["plan-slot"], closed=False)
        del state._slots["plan-slot"]

        restored = _rehydrate_slot_from_history(state, "plan-slot")

        assert restored is not None
        assert restored._stage_titles == ["Recon", "Implement", "Verify"]
        assert restored._plan_goal == "Ship the thing"
        assert restored._stage_descriptions[1] == ["- do implement"]
        assert restored._orch_tracker is not None
        assert restored._orch_tracker.resume_stage() == 2

    def test_bulk_restore_restores_the_execution_pointer(self, tmp_path, monkeypatch):
        """The recent-sessions path is a second, independent reader."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        _plan_slot(state)
        _save_slot_to_history(state, state._slots["plan-slot"], closed=False)
        del state._slots["plan-slot"]

        restore_recent_sessions(state, window_minutes=10_000)

        restored = state._slots.get("plan-slot")
        assert restored is not None
        assert restored._orch_tracker is not None
        assert restored._orch_tracker.resume_stage() == 2

    def test_restore_does_not_rearm_auto_run(self, tmp_path, monkeypatch):
        """A restart must not silently resume unattended execution."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        _plan_slot(state, auto_run=True)
        _save_slot_to_history(state, state._slots["plan-slot"], closed=False)
        del state._slots["plan-slot"]

        restored = _rehydrate_slot_from_history(state, "plan-slot")

        assert restored._auto_run is False

    def test_restore_surfaces_a_resume_offer(self, tmp_path, monkeypatch):
        """RED BEFORE: there was no resume path, so nothing was offered."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        _plan_slot(state)
        _save_slot_to_history(state, state._slots["plan-slot"], closed=False)
        del state._slots["plan-slot"]

        restored = _rehydrate_slot_from_history(state, "plan-slot")

        tail = restored.messages[-1]
        assert _PLAN_RESUME_MARKER in tail["content"]
        # Names the stage that did NOT finish, not the one after it.
        assert "Stage 2: Implement" in tail["content"]
        # The same control the stage gates use, so Go re-enters the stage loop.
        assert "[OPTION: Go | Go All | Cancel]" in tail["content"]
        assert restored._dirty is True

    def test_resume_offer_is_not_stacked_twice(self, tmp_path, monkeypatch):
        """A second restart on the same unfinished plan must not re-offer."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        _plan_slot(state)
        _save_slot_to_history(state, state._slots["plan-slot"], closed=False)
        del state._slots["plan-slot"]
        first = _rehydrate_slot_from_history(state, "plan-slot")
        _save_slot_to_history(state, first, closed=False)
        del state._slots["plan-slot"]

        second = _rehydrate_slot_from_history(state, "plan-slot")

        offers = [m for m in second.messages if _PLAN_RESUME_MARKER in m.get("content", "")]
        assert len(offers) == 1

    def test_armed_but_unstarted_plan_gets_no_resume_offer(self):
        """Nothing was interrupted -- the plan message's own buttons still stand."""
        slot = _ChatSlot("unstarted", mode="orchestrator")

        resume = _restore_plan_state(slot, {"stage_titles": ["Only"], "goal": "g", "tracker": {}})

        assert resume is None
        assert slot._stage_titles == ["Only"]

    @pytest.mark.parametrize("payload", [None, {}, {"stage_titles": []}, "nonsense"])
    def test_absent_or_malformed_record_restores_nothing(self, payload):
        slot = _ChatSlot("empty", mode="orchestrator")

        assert _restore_plan_state(slot, payload) is None
        assert slot._orch_tracker is None
