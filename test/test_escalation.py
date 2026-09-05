"""``session_send target="user"`` — escalating to the human as a peer.

Pins the contract in ``docs/system-specs/modules/session-control.md`` §
"Escalating to the human":

* delivery lands an ``escalation`` row in the OWNING member's DM thread and
  never starts a turn (non-blocking);
* the conversation index gains a pending record, ``needs_you`` is projected
  on the member slot and the roster, and clears on the human's reply or when
  the deadline passes;
* the deadline window is validated, the card is mirrored onto the bell bus
  with a per-goal ``group_key``, and one SEL line is written.

Member-slot file IO goes to ``$KIROCREW_HOME`` isolated by the autouse
``_isolate_kirocrew_home`` fixture.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state

from kiro_crew import crew_conversation as conv
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.dashboard.handlers import session_control as handlers_sc
from kiro_crew.dashboard.slot_projection import _member_needs_you
from kiro_crew.members import member_slot_key

MEMBER = "Radar"
SLUG = "radar"


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setattr(sc, "session_control_enabled", lambda: True)


@pytest.fixture(autouse=True)
def _no_turns(monkeypatch):
    """Escalation must never start a turn: make any attempt loud."""

    async def _boom(*_a, **_k):  # pragma: no cover - the assertion is that it is not called
        raise AssertionError("escalation started a turn")

    monkeypatch.setattr("kiro_crew.dashboard.chat_runner._run_chat", _boom)


def _member_slot(state):
    return state.get_or_create_slot(member_slot_key(SLUG), agent=MEMBER, mode="member")


def _key(slot) -> str:
    return slot_history_key(slot)


def _escalate(state, caller, **kw):
    async def _drive():
        out = await sc.send_to_target(
            state,
            caller_session_key=_key(caller),
            target=kw.pop("target", "user"),
            message=kw.pop(
                "message", "## Blocked on prod access\n\nTried X and Y.\n\nNeed: grant me role Z."
            ),
            **kw,
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return out

    return asyncio.run(_drive())


# ── Delivery ─────────────────────────────────────────────────────────────────


def test_member_escalation_lands_in_its_own_thread_as_a_card(tmp_path):
    state = _make_state(tmp_path)
    member = _member_slot(state)
    before = len(member.messages)

    out = _escalate(
        state,
        member,
        deadline="30m",
        default_action="Push A",
        options=["Push A", "Hold"],
        goal="triage",
    )

    assert out["ok"] is True
    assert out["target"] == member.key
    assert out["escalation_id"].startswith("esc-")
    assert out["deadline"]
    assert "started" not in out  # nothing ran
    row = member.messages[-1]
    assert len(member.messages) == before + 1
    assert row["role"] == sc.ESCALATION_ROLE
    assert row["cls"] == sc.ESCALATION_CLS
    assert row["content"].startswith("## Blocked on prod access")
    meta = row["meta"]
    assert meta["kind"] == "escalation"
    assert meta["from_session"] == member.key
    assert meta["deadline"] == out["deadline"]
    assert meta["default_action"] == "Push A"
    assert meta["options"] == ["Push A", "Hold"]
    assert meta["goal"] == "triage"
    assert meta["state"] == "pending"
    assert meta["mid"]


def test_target_user_is_case_and_space_insensitive(tmp_path):
    state = _make_state(tmp_path)
    member = _member_slot(state)
    out = _escalate(state, member, target="  User ")
    assert out["target"] == member.key


def test_worker_created_by_a_member_escalates_into_that_members_thread(tmp_path):
    state = _make_state(tmp_path)
    member = _member_slot(state)
    worker = state.get_or_create_slot("chat-7")
    worker._created_by = member.key

    out = _escalate(state, worker)

    assert out["target"] == member.key
    assert out["member"] == MEMBER
    assert member.messages[-1]["role"] == sc.ESCALATION_ROLE
    assert member.messages[-1]["meta"]["from_session"] == "chat-7"
    assert worker.messages == []


def test_plain_session_escalates_into_its_own_transcript_without_member_index(tmp_path):
    state = _make_state(tmp_path)
    plain = state.get_or_create_slot("chat-3")

    out = _escalate(state, plain)

    assert out["target"] == "chat-3"
    assert out["member"] == ""
    assert plain.messages[-1]["role"] == sc.ESCALATION_ROLE
    assert not conv.conversation_path(SLUG).exists()


def test_unknown_caller_is_refused(tmp_path):
    state = _make_state(tmp_path)
    with pytest.raises(sc.SessionControlError) as exc:
        asyncio.run(
            sc.send_to_target(
                state, caller_session_key="dashboard:ghost", target="user", message="x"
            )
        )
    assert exc.value.code == "caller_unidentified"


def test_caller_gates_apply_like_any_other_send(tmp_path, monkeypatch):
    """The human is not a session, but the CALLER still is: every caller-side
    refusal of the peer path holds here with the same code."""
    state = _make_state(tmp_path)
    plain = state.get_or_create_slot("chat-3")

    monkeypatch.setattr(sc, "session_control_enabled", lambda: False)
    with pytest.raises(sc.SessionControlError) as exc:
        _escalate(state, plain)
    assert exc.value.code == "session_control_disabled"
    monkeypatch.setattr(sc, "session_control_enabled", lambda: True)

    # A member DM slot keeps its bypass of the config switch.
    monkeypatch.setattr(sc, "session_control_enabled", lambda: False)
    member = _member_slot(state)
    assert _escalate(state, member)["ok"] is True
    monkeypatch.setattr(sc, "session_control_enabled", lambda: True)

    ephemeral = state.get_or_create_slot("chat-4")
    ephemeral.memory_mode = "incognito"
    with pytest.raises(sc.SessionControlError) as exc:
        _escalate(state, ephemeral)
    assert exc.value.code == "ephemeral_caller"
    assert ephemeral.messages == []

    app_slot = state.get_or_create_slot("chat-5")
    app_slot._app = "some-app"
    with pytest.raises(sc.SessionControlError) as exc:
        _escalate(state, app_slot)
    assert exc.value.code == "app_scoped_caller"


def test_caller_denials_are_audited_under_escalate(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    logged = []
    fake_sel = MagicMock()
    fake_sel.log_api_access = lambda **kw: logged.append(kw)
    monkeypatch.setattr(sc, "sel", lambda: fake_sel)
    with pytest.raises(sc.SessionControlError):
        asyncio.run(
            sc.send_to_target(
                state, caller_session_key="dashboard:ghost", target="user", message="x"
            )
        )
    (entry,) = [kw for kw in logged if kw["operation"] == "session_control.escalate"]
    assert entry["outcome"] == "denied"
    assert entry["resources"] == "target=user:caller_unidentified"


def test_index_record_exists_before_the_card_is_surfaced(tmp_path, monkeypatch):
    """A reply that races the card must find the record pending: the index write
    precedes the append, under the id the row will carry."""
    state = _make_state(tmp_path)
    member = _member_slot(state)
    order: list[str] = []
    real_append = sc.append_and_surface

    def _spy_append(*a, **kw):
        record = conv.read_conversation(SLUG)
        order.append(f"append(pending={len(conv.pending_escalations(record))})")
        return real_append(*a, **kw)

    monkeypatch.setattr(sc, "append_and_surface", _spy_append)
    out = _escalate(state, member)
    assert order == ["append(pending=1)"]
    (entry,) = conv.read_conversation(SLUG)["entries"]
    assert entry["mid"] == member.messages[-1]["meta"]["mid"]
    assert entry["id"] == out["escalation_id"]


def test_worker_result_says_where_the_reply_lands(tmp_path):
    state = _make_state(tmp_path)
    member = _member_slot(state)
    worker = state.get_or_create_slot("chat-7")
    worker._created_by = member.key
    assert _escalate(state, worker)["reply_in_caller_thread"] is False
    assert _escalate(state, member)["reply_in_caller_thread"] is True


def test_worker_in_another_workspace_cannot_reach_its_creators_thread(tmp_path):
    """Same boundary the peer path enforces (`workspace_mismatch`)."""
    state = _make_state(tmp_path)
    member = _member_slot(state)
    worker = state.get_or_create_slot("chat-7")
    worker._created_by = member.key
    worker.workspace = "other"
    with pytest.raises(sc.SessionControlError) as exc:
        _escalate(state, worker)
    assert exc.value.code == "workspace_mismatch"
    assert member.messages == []
    assert worker.messages == []
    assert not conv.conversation_path(SLUG).exists()


def test_worker_whose_owner_thread_is_closed_is_refused_not_misrouted(tmp_path):
    """A card in a worker nobody reads, with no index record and no badge,
    would look delivered and be lost — refuse instead."""
    state = _make_state(tmp_path)
    worker = state.get_or_create_slot("chat-7")
    worker._created_by = member_slot_key(SLUG)  # member thread is not open
    with pytest.raises(sc.SessionControlError) as exc:
        _escalate(state, worker)
    assert exc.value.code == "target_gone"
    assert worker.messages == []


def test_two_chip_replies_drained_as_one_row_answer_both_records(tmp_path):
    state = _make_state(tmp_path)
    member = _member_slot(state)
    a = _escalate(state, member, goal="a")
    b = _escalate(state, member, goal="b")
    # The queue drain merges two chip replies into one user row carrying both ids.
    member.append(
        "user",
        "Close it\n\nKeep it open",
        "msg msg-u",
        meta={"escalation_ids": [a["escalation_id"], b["escalation_id"]]},
    )
    states = {e["id"]: e["state"] for e in conv.read_conversation(SLUG)["entries"]}
    assert states == {a["escalation_id"]: "answered", b["escalation_id"]: "answered"}
    assert _member_needs_you(member) is False


def test_peer_delivered_user_row_does_not_answer(tmp_path):
    """A peer's session_send lands as a user row too; it is not the human."""
    state = _make_state(tmp_path)
    member = _member_slot(state)
    _escalate(state, member)
    member.append("user", sc._SEND_PROVENANCE.format(caller="chat-9") + "hello", "msg msg-u")
    assert _member_needs_you(member) is True
    assert conv.read_conversation(SLUG)["entries"][0]["state"] == "pending"


def test_free_text_counts_only_records_pending_when_it_was_typed(tmp_path):
    """The reply's candidates are snapshotted at append time: a record that
    lands after the reply (executor ordering) is neither counted nor answered."""
    state = _make_state(tmp_path)
    member = _member_slot(state)
    first = _escalate(state, member)
    # Simulate the race: a second record reaches the index before the deferred
    # mark for a reply that was typed while only the first was pending.
    conv.record_escalation(
        SLUG,
        member=MEMBER,
        session_key=member.key,
        mid="m-late",
        escalation_id="esc-late",
        from_session=member.key,
    )
    moved = conv.mark_answered(SLUG, candidates=[first["escalation_id"]])
    assert moved == 1
    states = {e["id"]: e["state"] for e in conv.read_conversation(SLUG)["entries"]}
    assert states[first["escalation_id"]] == "answered"
    assert states["esc-late"] == "pending"


def test_append_failure_retracts_the_index_record(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    member = _member_slot(state)

    def _boom(*_a, **_k):
        raise RuntimeError("append exploded")

    monkeypatch.setattr(sc, "append_and_surface", _boom)
    with pytest.raises(RuntimeError):
        _escalate(state, member)
    assert conv.read_conversation(SLUG)["entries"] == []
    assert _member_needs_you(member) is False


def test_thread_closing_during_delivery_retracts_and_refuses(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    member = _member_slot(state)
    real_record = conv.record_escalation

    def _record_then_close(*a, **kw):
        out = real_record(*a, **kw)
        state._slots.pop(member.key, None)  # the thread closes mid-delivery
        return out

    monkeypatch.setattr(sc.conv, "record_escalation", _record_then_close)
    with pytest.raises(sc.SessionControlError) as exc:
        _escalate(state, member)
    assert exc.value.code == "target_gone"
    assert conv.read_conversation(SLUG)["entries"] == []


# ── Index, needs_you, lifecycle ──────────────────────────────────────────────


def test_index_gets_a_pending_record_and_needs_you_projects(tmp_path):
    state = _make_state(tmp_path)
    member = _member_slot(state)
    assert _member_needs_you(member) is False

    out = _escalate(state, member, goal="triage")

    record = conv.read_conversation(SLUG)
    (entry,) = record["entries"]
    assert entry["type"] == "escalation"
    assert entry["id"] == out["escalation_id"]
    assert entry["mid"] == member.messages[-1]["meta"]["mid"]
    assert entry["session_key"] == member.key
    assert entry["state"] == "pending"
    assert record["participants"][1] == {"kind": "member", "slug": SLUG, "name": MEMBER}
    assert _member_needs_you(member) is True


def test_human_reply_in_the_thread_clears_needs_you(tmp_path):
    state = _make_state(tmp_path)
    member = _member_slot(state)
    _escalate(state, member)
    assert _member_needs_you(member) is True

    # The composer path: a live user row appended to the member DM slot.
    member.append("user", "Go with A", "msg msg-u")

    assert _member_needs_you(member) is False
    assert conv.read_conversation(SLUG)["entries"][0]["state"] == "answered"


def test_replayed_user_row_does_not_answer(tmp_path):
    state = _make_state(tmp_path)
    member = _member_slot(state)
    _escalate(state, member)
    member.append("user", "old row", "msg msg-u", broadcast=False)
    assert _member_needs_you(member) is True


def test_free_text_with_two_pending_answers_neither_but_a_scoped_reply_answers_one(tmp_path):
    state = _make_state(tmp_path)
    member = _member_slot(state)
    first = _escalate(state, member, goal="a")
    second = _escalate(state, member, goal="b")

    member.append("user", "how is it going?", "msg msg-u")
    states = {e["id"]: e["state"] for e in conv.read_conversation(SLUG)["entries"]}
    assert states == {first["escalation_id"]: "pending", second["escalation_id"]: "pending"}
    assert _member_needs_you(member) is True

    # The option chip carries the id on the user row's meta.
    member.append(
        "user", "Keep it open", "msg msg-u", meta={"escalation_id": second["escalation_id"]}
    )
    states = {e["id"]: e["state"] for e in conv.read_conversation(SLUG)["entries"]}
    assert states[second["escalation_id"]] == "answered"
    assert states[first["escalation_id"]] == "pending"
    assert _member_needs_you(member) is True


def test_reply_pushes_a_slots_update_only_when_something_cleared(tmp_path):
    """The badge must clear WITH the reply, not at the next unrelated push."""
    state = _make_state(tmp_path)
    member = _member_slot(state)
    pushed = MagicMock()
    member._on_escalation_answered = pushed

    member.append("user", "nothing pending yet", "msg msg-u")
    pushed.assert_not_called()

    _escalate(state, member)
    member.append("user", "Go with A", "msg msg-u")
    pushed.assert_called_once()


def test_non_member_slot_never_needs_you(tmp_path):
    state = _make_state(tmp_path)
    plain = state.get_or_create_slot("chat-3")
    _escalate(state, plain)
    assert _member_needs_you(plain) is False


def test_deadline_passing_clears_needs_you(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    member = _member_slot(state)
    _escalate(state, member, deadline="60s", default_action="Push A")
    assert _member_needs_you(member) is True

    from datetime import datetime, timedelta, timezone

    later = datetime.now(timezone.utc) + timedelta(minutes=5)
    assert conv.needs_you(SLUG, now=later) is False
    record = conv.read_conversation(SLUG)
    conv.sweep_deadlines(record, now=later)
    assert record["entries"][0]["state"] == "defaulted"


# ── Validation ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("bad", ["10s", "9d", "soon"])
def test_bad_deadline_is_refused_before_anything_lands(tmp_path, bad):
    state = _make_state(tmp_path)
    member = _member_slot(state)
    with pytest.raises(sc.SessionControlError) as exc:
        _escalate(state, member, deadline=bad)
    assert exc.value.code == "deadline_invalid"
    assert exc.value.status == 400
    assert member.messages == []
    assert not conv.conversation_path(SLUG).exists()


def test_too_many_options_refused(tmp_path):
    state = _make_state(tmp_path)
    member = _member_slot(state)
    with pytest.raises(sc.SessionControlError) as exc:
        _escalate(state, member, options=[f"o{i}" for i in range(7)])
    assert exc.value.code == "options_too_many"


def test_options_are_deduplicated_and_blank_dropped(tmp_path):
    state = _make_state(tmp_path)
    member = _member_slot(state)
    _escalate(state, member, options=["A", " ", "A", "B"])
    assert member.messages[-1]["meta"]["options"] == ["A", "B"]


def test_no_deadline_means_open_ended(tmp_path):
    state = _make_state(tmp_path)
    member = _member_slot(state)
    out = _escalate(state, member)
    assert out["deadline"] is None
    assert member.messages[-1]["meta"]["deadline"] is None


# ── Mirror + audit ───────────────────────────────────────────────────────────


def test_mirror_goes_to_the_bell_bus_with_a_per_goal_group_key(tmp_path):
    state = _make_state(tmp_path)
    member = _member_slot(state)
    pushed = []
    state.notification_bus = MagicMock(push=lambda p: pushed.append(p))

    _escalate(state, member, goal="triage", deadline="30m", default_action="Push A")

    (payload,) = pushed
    assert payload.channel == "system.agent"
    assert payload.kind == "escalation"
    assert payload.group_key == f"escalation:{SLUG}:triage"
    assert payload.title == f"{MEMBER} needs you"
    assert payload.url == f"/members?member={MEMBER}"
    assert "Blocked on prod access" in payload.body
    assert "Push A" in payload.body


def test_mirror_failure_does_not_fail_delivery(tmp_path):
    state = _make_state(tmp_path)
    member = _member_slot(state)
    state.notification_bus = MagicMock(push=MagicMock(side_effect=RuntimeError("bus down")))
    out = _escalate(state, member)
    assert out["ok"] is True
    assert member.messages[-1]["role"] == sc.ESCALATION_ROLE


def test_index_write_failure_refuses_and_surfaces_no_card(tmp_path, monkeypatch):
    """The index IS the lifecycle: without it the card would report a delivery
    whose badge, deadline and reply tracking never exist. Refuse instead."""
    state = _make_state(tmp_path)
    member = _member_slot(state)

    def _boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(sc.conv, "record_escalation", _boom)
    with pytest.raises(sc.SessionControlError) as exc:
        _escalate(state, member)
    assert exc.value.code == "escalation_index_unavailable"
    assert exc.value.status == 500
    assert member.messages == []
    assert _member_needs_you(member) is False


def test_one_sel_line_names_the_escalation(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    member = _member_slot(state)
    logged = []
    fake_sel = MagicMock()
    fake_sel.log_tool_invocation = lambda **kw: logged.append(kw)
    monkeypatch.setattr(sc, "sel", lambda: fake_sel)

    out = _escalate(state, member, deadline="30m", default_action="Push A", options=["A"])

    (entry,) = [kw for kw in logged if kw["tool_name"] == "session_escalate"]
    assert entry["outcome"] == "allowed"
    assert entry["resources"] == f"target={member.key}"
    assert entry["metadata"]["escalation_id"] == out["escalation_id"]
    assert entry["metadata"]["has_default"] is True
    assert entry["metadata"]["options"] == 1
    assert entry["metadata"]["member"] == SLUG


# ── Route + roster ───────────────────────────────────────────────────────────


def test_route_passes_escalation_fields_through(tmp_path):
    state = _make_state(tmp_path)
    member = _member_slot(state)
    req = MagicMock()
    req.app = {"state": state}
    req.headers = {"X-Session-Key": _key(member)}
    req.get = lambda k, d=None: True if k == "internal_auth" else d

    async def _json():
        return {
            "target": "user",
            "message": "need a decision",
            "deadline": "2h",
            "default_action": "keep going",
            "options": ["yes", "no"],
            "goal": "g1",
        }

    req.json = _json
    resp = asyncio.run(handlers_sc.api_session_control_send(req))
    body = json.loads(resp.body)
    assert resp.status == 200
    assert body["escalation_id"]
    meta = member.messages[-1]["meta"]
    assert meta["options"] == ["yes", "no"]
    assert meta["goal"] == "g1"
    assert meta["default_action"] == "keep going"


def test_route_surfaces_deadline_refusal_as_400(tmp_path):
    state = _make_state(tmp_path)
    member = _member_slot(state)
    req = MagicMock()
    req.app = {"state": state}
    req.headers = {"X-Session-Key": _key(member)}
    req.get = lambda k, d=None: True if k == "internal_auth" else d

    async def _json():
        return {"target": "user", "message": "x", "deadline": "5s"}

    req.json = _json
    resp = asyncio.run(handlers_sc.api_session_control_send(req))
    assert resp.status == 400
    assert json.loads(resp.body)["code"] == "deadline_invalid"


def test_queued_reply_carries_the_escalation_id_onto_the_queue_entry(tmp_path):
    """A member that just escalated is usually mid-turn, so the chip reply goes
    through the queue; the id must ride the entry so the drained row answers
    the right record."""
    from kiro_crew.dashboard.chat_delivery import queue_for_next_turn

    state = _make_state(tmp_path)
    member = _member_slot(state)
    state.broadcast_ws = MagicMock()
    queue_for_next_turn(state, member, "Keep it open", escalation_id="esc-0123456789abcdef")
    entry = member._queue[-1]
    assert entry["meta"]["escalation_id"] == "esc-0123456789abcdef"


@pytest.mark.asyncio
async def test_roster_reports_needs_you_and_conversation_endpoint(tmp_path):
    from unittest.mock import patch

    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from kiro_crew.config.loader import KiroCrewAgentConfig
    from kiro_crew.dashboard.handlers.members import api_member_conversation, api_members
    from kiro_crew.members import write_dm_binding

    state = _make_state(tmp_path)
    member = _member_slot(state)
    write_dm_binding(SLUG, member=MEMBER, slot_key=member.key)
    conv.record_escalation(
        SLUG,
        member=MEMBER,
        session_key=member.key,
        mid="m-1",
        escalation_id="esc-1",
        from_session=member.key,
    )

    cfg = MagicMock()
    cfg.agents = {MEMBER: KiroCrewAgentConfig(kiro_agent="kirocrew", workspace="default")}
    cfg.agent.default_agent = MEMBER

    @web.middleware
    async def _auth(request, handler):
        request["app"] = ""
        request["user"] = "local-app"
        return await handler(request)

    app = web.Application(middlewares=[_auth])
    app["state"] = state
    app.router.add_get("/api/members", api_members)
    app.router.add_get("/api/members/{slug}/conversation", api_member_conversation)

    with (
        patch("kiro_crew.dashboard.handlers.members.KiroCrewConfig.load", return_value=cfg),
        patch(
            "kiro_crew.dashboard.handlers._shared.require_owner_dashboard_request",
            new=AsyncMock(return_value=None),
        ),
    ):
        async with TestClient(TestServer(app)) as client:
            roster = await (await client.get("/api/members")).json()
            row = {r["name"]: r for r in roster["members"]}[MEMBER]
            assert row["needs_you"] is True
            assert row["pending_escalations"] == 1

            detail = await client.get(f"/api/members/{SLUG}/conversation")
            assert detail.status == 200
            view = await detail.json()
            assert view["conversation_id"] == f"dm:{SLUG}"
            assert view["needs_you"] is True
            assert view["entries"][0]["id"] == "esc-1"

            bad = await client.get("/api/members/Not%20A%20Slug/conversation")
            assert bad.status == 400
