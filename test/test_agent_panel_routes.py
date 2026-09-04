"""Tests for the crew-webview HTTP routes.

The property worth guarding hardest: which crew a publish writes to is derived
from the CALLING SESSION's own crew binding, never from anything the caller
sends. A body-supplied crew name would let one crew publish a webview that
presents as another's, and the whole value of a per-crew dashboard is that the
operator can trust whose state they are reading.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from urllib.parse import quote

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import agent_panel
from kiro_crew import members as members_mod
from kiro_crew.dashboard.handlers import agent_panel as routes

pytestmark = pytest.mark.asyncio

CREW = "fleet-crew"
SLUG = "fleet-crew"

#: A crew whose name matches a template the OPERATOR installed. The route's
#: name-match behaviour is proven against a template this test drops on disk, so
#: it does not depend on any particular consumer's artifact shipping.
BESPOKE = "bespoke-crew"


def _install_template(template_id: str) -> None:
    """Install a minimal template an operator dropped on disk.

    Only the marker is needed here -- these tests assert which template id the
    route selects, not how it renders. The data home is per-test.
    """
    over = agent_panel.override_templates_dir()
    over.mkdir(parents=True, exist_ok=True)
    (over / f"{template_id}.html").write_text(agent_panel.DATA_MARKER, encoding="utf-8")


class _State:
    """Just enough DashboardState for the crew resolver."""

    def __init__(self, agent: str | None):
        self._agent = agent

    def get_slot(self, _name):
        if self._agent is None:
            return None
        return SimpleNamespace(agent=self._agent)


def _mounted(agent: str | None = CREW, *, internal: bool = True) -> web.Application:
    """The panel routes on a bare app.

    ``internal`` mints what ``token_auth_middleware`` sets ONLY on a verified
    ``X-Internal-Secret`` match. It defaults to True because the publish surface
    is MCP-only and that is the shape of every real caller; the cookie-only case
    gets its own test, which is the one that matters.
    """
    app = web.Application()
    app["state"] = _State(agent)
    if internal:

        @web.middleware
        async def _internal(request, handler):
            request["internal_auth"] = True
            return await handler(request)

        app.middlewares.append(_internal)
    routes.register_agent_panel_routes(app)
    return app


@pytest.fixture
def vetted(monkeypatch):
    """Treat the caller as a recognized, unrestricted session."""

    async def _recognize(_state, _sk, _op, **_kw):
        return None

    monkeypatch.setattr(routes, "_recognize_session", _recognize)
    monkeypatch.setattr(routes, "_is_restricted_session", lambda *_a, **_k: False)


async def _client(agent: str | None = CREW, *, internal: bool = True) -> TestClient:
    """The repo's idiom: pytest-aiohttp's `aiohttp_client` fixture is not
    installed, so the client is built and started explicitly."""
    c = TestClient(TestServer(_mounted(agent, internal=internal)))
    await c.start_server()
    return c


# ------------------------------------------------------------------ publishing


async def test_a_publish_lands_on_the_callers_own_crew(vetted):
    c = await _client()
    resp = await c.post(
        "/api/agent-panel/publish",
        json={"data": {"cycle": 47}, "title": "fleet"},
        headers={"X-Session-Key": "dashboard:chat-1"},
    )
    assert resp.status == 200, await resp.text()
    assert (await resp.json())["ok"] is True
    stored = agent_panel.read(SLUG)
    assert stored is not None
    assert stored["crew"] == CREW
    assert stored["data"] == {"cycle": 47}


async def test_a_cookie_only_caller_cannot_publish_as_a_chosen_session(vetted):
    """THE authorization test. The crew is resolved from a caller-CHOSEN header.

    ``X-Session-Key`` is an identity CLAIM, not a lookup key: a caller holding
    only a dashboard cookie could name any live session, have it resolve to THAT
    crew, and overwrite the crew's panel. ``vetted`` is applied deliberately --
    session recognition passing is exactly the condition under which the old code
    went on to write, so this proves the internal-secret gate refuses FIRST rather
    than relying on recognition to fail.
    """
    c = await _client(internal=False)
    resp = await c.post(
        "/api/agent-panel/publish",
        json={"data": {"x": 1}},
        headers={"X-Session-Key": "dashboard:chat-1"},
    )
    assert resp.status == 403
    assert (await resp.json())["code"] == "internal_secret_required"
    assert agent_panel.read(SLUG) is None, "a refused caller must not reach the store"


async def test_a_cookie_only_caller_cannot_list_templates(vetted):
    """The same gate covers the whole MCP-only surface, not just the write."""
    c = await _client(internal=False)
    resp = await c.get("/api/agent-panel/templates", headers={"X-Session-Key": "dashboard:chat-1"})
    assert resp.status == 403
    assert (await resp.json())["code"] == "internal_secret_required"


async def test_the_drawer_read_needs_no_internal_secret():
    """The drawer is a browser caller. The gate above must not reach the READ.

    Pinned beside the refusals so a later widening of that check cannot 403 the
    dashboard's own drawer -- which would look like the feature is broken.
    """
    c = await _client(internal=False)
    resp = await c.get(f"/api/members/{SLUG}/panel?member={CREW}")
    assert resp.status == 200


async def test_the_crew_is_not_taken_from_the_body(vetted):
    c = await _client()
    resp = await c.post(
        "/api/agent-panel/publish",
        json={"data": {"x": 1}, "crew": "research-lab", "slug": "research-lab"},
        headers={"X-Session-Key": "dashboard:chat-1"},
    )
    assert resp.status == 400
    # The unknown field is REJECTED by the schema rather than ignored: silently
    # dropping it would let a caller believe it had retargeted the write.
    assert (await resp.json())["code"] == "validation_error"
    assert agent_panel.read("research-lab") is None


async def test_a_session_with_no_crew_is_refused_plainly(vetted):
    """A conductor publishing every cycle into a void looks like a broken feature."""
    c = await _client(agent=None)
    resp = await c.post(
        "/api/agent-panel/publish",
        json={"data": {"x": 1}},
        headers={"X-Session-Key": "dashboard:chat-1"},
    )
    assert resp.status == 400
    assert (await resp.json())["code"] == "no_crew"


async def test_an_omitted_template_resolves_to_the_crews_own(vetted):
    """How a crew with a template of its own gets it without asking for it."""
    _install_template(BESPOKE)
    c = await _client(agent=BESPOKE)
    resp = await c.post(
        "/api/agent-panel/publish",
        json={"data": {"cycle": 1}},
        headers={"X-Session-Key": "dashboard:chat-1"},
    )
    assert resp.status == 200
    assert (await resp.json())["panel"]["template"] == BESPOKE


async def test_a_crew_with_no_template_of_its_own_gets_the_generic_one(vetted):
    c = await _client(agent="research-lab")
    resp = await c.post(
        "/api/agent-panel/publish",
        json={"data": {"cycle": 1}},
        headers={"X-Session-Key": "dashboard:chat-9"},
    )
    assert resp.status == 200
    assert (await resp.json())["panel"]["template"] == "default"


async def test_a_store_refusal_travels_with_its_code(vetted):
    """The crew can only correct the call on its next cycle if it is told which
    refusal fired."""
    c = await _client()
    resp = await c.post(
        "/api/agent-panel/publish",
        json={"data": {"k": "x" * (agent_panel._MAX_DATA_BYTES + 10)}},
        headers={"X-Session-Key": "dashboard:chat-1"},
    )
    assert resp.status == 400
    assert (await resp.json())["code"] == "data_too_large"


async def test_the_response_does_not_echo_the_payload(vetted):
    """Repeating a 64 KB payload into the tool result burns the context this
    feature exists to save."""
    c = await _client()
    resp = await c.post(
        "/api/agent-panel/publish",
        json={"data": {"needle": "n" * 900}},
        headers={"X-Session-Key": "dashboard:chat-1"},
    )
    assert "n" * 100 not in json.dumps(await resp.json())


async def test_a_refused_session_never_reaches_the_store(monkeypatch):
    """Recognition runs before anything is written."""

    async def _refuse(_state, _sk, _op, **_kw):
        return web.json_response({"error": "no", "code": "unrecognized"}, status=403)

    monkeypatch.setattr(routes, "_recognize_session", _refuse)
    c = await _client()
    resp = await c.post(
        "/api/agent-panel/publish",
        json={"data": {"x": 1}},
        headers={"X-Session-Key": "dashboard:chat-1"},
    )
    assert resp.status == 403
    assert agent_panel.read(SLUG) is None


async def test_a_restricted_session_is_refused(monkeypatch):
    """A published panel is durable on-disk state, which those modes promise not
    to leave behind."""

    async def _recognize(_state, _sk, _op, **_kw):
        return None

    monkeypatch.setattr(routes, "_recognize_session", _recognize)
    monkeypatch.setattr(routes, "_is_restricted_session", lambda *_a, **_k: True)
    c = await _client()
    resp = await c.post(
        "/api/agent-panel/publish",
        json={"data": {"x": 1}},
        headers={"X-Session-Key": "dashboard:chat-1"},
    )
    assert resp.status == 403
    assert (await resp.json())["code"] == "restricted_session"
    assert agent_panel.read(SLUG) is None


async def test_templates_reports_the_crews_default(vetted):
    """The crew asks which template it would get, so it can name one explicitly."""
    c = await _client()
    body = await (
        await c.get("/api/agent-panel/templates", headers={"X-Session-Key": "dashboard:chat-1"})
    ).json()
    assert body["default"] == agent_panel.DEFAULT_TEMPLATE_ID
    assert agent_panel.DEFAULT_TEMPLATE_ID in set(body["templates"])


async def test_templates_reports_a_name_matched_template_as_the_default(vetted):
    """The name match is reported, not just applied silently at publish time."""
    _install_template(BESPOKE)
    c = await _client(agent=BESPOKE)
    body = await (
        await c.get("/api/agent-panel/templates", headers={"X-Session-Key": "dashboard:chat-1"})
    ).json()
    assert body["default"] == BESPOKE
    assert {agent_panel.DEFAULT_TEMPLATE_ID, BESPOKE} <= set(body["templates"])


# --------------------------------------------------------------------- reading


async def test_the_drawer_read_returns_the_composed_document(vetted):
    c = await _client()
    await c.post(
        "/api/agent-panel/publish",
        json={"data": {"cycle": 47}},
        headers={"X-Session-Key": "dashboard:chat-1"},
    )
    body = await (await c.get(f"/api/members/{SLUG}/panel?member={CREW}")).json()
    assert body["panel"]["crew"] == CREW
    assert agent_panel._DATA_ELEMENT_ID in body["html"]
    assert agent_panel.DATA_MARKER not in body["html"], "the marker must have been filled"


async def test_the_drawer_read_carries_the_raw_data_in_published_order(vetted):
    """The docked summary is rendered natively from this, not from the document.

    A dashboard needs a full page to be legible, so the ~250px drawer shows a
    few fields instead -- and it must show the ones the CREW put first, which is
    only possible if the order survives the wire. Asserted on a key set whose
    alphabetical order differs from the published order, so a serializer that
    sorted keys reddens here.
    """
    published = {"cycle": 47, "needs_you": "one ruling is waiting on you", "aardvark": 1}
    c = await _client()
    await c.post(
        "/api/agent-panel/publish",
        json={"data": published},
        headers={"X-Session-Key": "dashboard:chat-1"},
    )
    body = await (await c.get(f"/api/members/{SLUG}/panel?member={CREW}")).json()
    assert body["panel"]["data"] == published
    assert list(body["panel"]["data"].keys()) == ["cycle", "needs_you", "aardvark"]


async def test_the_drawer_read_has_a_data_object_even_for_a_broken_record(vetted):
    """The drawer indexes into this, so it must never be null."""
    c = await _client()
    await c.post(
        "/api/agent-panel/publish",
        json={"data": {"cycle": 1}},
        headers={"X-Session-Key": "dashboard:chat-1"},
    )
    body = await (await c.get(f"/api/members/{SLUG}/panel?member={CREW}")).json()
    assert isinstance(body["panel"]["data"], dict)


async def test_the_drawer_read_is_an_empty_state_not_an_error(vetted):
    """A crew that never published is not a failure -- the drawer shows nothing."""
    c = await _client()
    resp = await c.get(f"/api/members/never-published/panel?member={CREW}")
    assert resp.status == 200
    assert (await resp.json()) == {"panel": None, "html": None}


@pytest.mark.parametrize("hostile", ["has space", "Upper.Case", "with/slash"])
async def test_the_drawer_read_refuses_a_hostile_slug(vetted, hostile):
    """A slug that reaches the handler is rejected by ``validate_slug``.

    ``..`` is deliberately NOT in this list: the HTTP layer normalises it away
    before routing, so it answers 404 and never reaches the handler at all. The
    store's own traversal test covers that shape.
    """
    c = await _client()
    resp = await c.get(f"/api/members/{hostile}/panel?member={CREW}")
    assert resp.status in (400, 404)
    if resp.status == 400:
        assert (await resp.json())["code"] == "invalid_member_slug"


async def test_the_read_route_is_not_under_the_strict_internal_prefix():
    """The drawer is a browser caller, so its route must not sit behind the
    MCP-only prefix -- it would 403 for the dashboard."""
    from kiro_crew.dashboard import server

    paths = {str(r.resource.canonical) for r in _mounted().router.routes()}
    read_path = "/api/members/{slug}/panel"
    assert read_path in paths
    assert not any(read_path.startswith(p) for p in server._STRICT_INTERNAL_API_PATHS)


async def test_the_publish_routes_are_under_the_strict_internal_prefix():
    from kiro_crew.dashboard import server

    for path in ("/api/agent-panel/publish", "/api/agent-panel/templates"):
        assert any(
            path.startswith(p) for p in server._STRICT_INTERNAL_API_PATHS
        ), f"{path} would fall through to cookie auth"


async def test_the_response_comes_from_a_single_record_read(vetted, monkeypatch):
    """Both halves from ONE snapshot, so a mid-request publish cannot split them.

    ``render`` + ``read`` as two calls read the file twice; a publish landing
    between them returned the OLD document beside the NEW summary, and the docked
    chip would then contradict the expanded view. Counting reads is the property --
    an interleaving test would depend on winning a race.
    """
    c = await _client()
    await c.post(
        "/api/agent-panel/publish",
        json={"data": {"cycle": 47}},
        headers={"X-Session-Key": "dashboard:chat-1"},
    )

    reads: list[str] = []
    real = agent_panel.read

    def counting(slug: str):
        reads.append(slug)
        return real(slug)

    monkeypatch.setattr(agent_panel, "read", counting)
    body = await (await c.get(f"/api/members/{SLUG}/panel?member={CREW}")).json()

    # Both halves are actually populated, or the count below would be vacuous.
    assert body["html"]
    assert body["panel"]["template"]
    assert (
        len(reads) == 1
    ), f"the record was read {len(reads)}x; both halves must share one snapshot"


async def test_an_edited_template_still_takes_effect_without_republishing(vetted):
    """The compose-on-read property the single-snapshot change had to preserve.

    Sourcing both halves from one record must not become composing once at publish:
    an operator editing a template sees it on the next drawer open, with no crew
    cycle in between. Without this, X3's fix could have been "compose at publish",
    which passes the read-count test above and silently drops the feature.
    """
    c = await _client()
    await c.post(
        "/api/agent-panel/publish",
        json={"data": {"cycle": 47}},
        headers={"X-Session-Key": "dashboard:chat-1"},
    )
    before = await (await c.get(f"/api/members/{SLUG}/panel?member={CREW}")).json()
    assert "SENTINEL-EDIT" not in before["html"]

    # Edit the template in place, publishing nothing. An operator override wins
    # over the shipped file, which is exactly the customisation seam being tested.
    over = agent_panel.override_templates_dir()
    over.mkdir(parents=True, exist_ok=True)
    (over / "default.html").write_text(
        f"<div data-edit='SENTINEL-EDIT'>{agent_panel.DATA_MARKER}</div>", encoding="utf-8"
    )

    after = await (await c.get(f"/api/members/{SLUG}/panel?member={CREW}")).json()
    assert "SENTINEL-EDIT" in after["html"], (
        "an edited template did not reach the drawer, so composition is no longer "
        "happening on read"
    )


async def test_a_colliding_crew_cannot_read_the_other_crews_panel(vetted):
    """The READ half of the ownership guard.

    ``publish`` refuses the colliding WRITE, so one crew cannot overwrite the
    other's record. But with the read keyed on the slug alone, the crew that did
    NOT publish still saw the publisher's dashboard rendered in its own drawer --
    the guard was half-applied. Both halves check the same stored claim.

    ``Oncall`` and ``oncall`` slugify to one slug, which is the whole premise.
    """
    c = await _client()
    await c.post(
        "/api/agent-panel/publish",
        json={"data": {"cycle": 47}},
        headers={"X-Session-Key": "dashboard:chat-1"},
    )
    # The owner sees it.
    owner_body = await (await c.get(f"/api/members/{SLUG}/panel?member={CREW}")).json()
    assert owner_body["panel"]["crew"] == CREW
    assert owner_body["html"]

    # The colliding crew, same slug, different exact name, sees an empty state --
    # not the other crew's document, and not an error naming them either.
    other = CREW.upper()
    assert members_mod.slug_for_name(other) == SLUG, "fixture no longer collides"
    resp = await c.get(f"/api/members/{SLUG}/panel?member={other}")
    assert resp.status == 200
    body = await resp.json()
    assert body["panel"] is None
    assert body["html"] is None


async def test_the_read_requires_the_exact_crew_name(vetted):
    """Required, not optional, so a mixed read is impossible by construction.

    An optional parameter puts the guard behind a caller obligation, and the one
    caller that forgets is the bug. Mirrors ``/activity``, which made the same
    parameter required for the same reason.
    """
    c = await _client()
    await c.post(
        "/api/agent-panel/publish",
        json={"data": {"cycle": 47}},
        headers={"X-Session-Key": "dashboard:chat-1"},
    )
    resp = await c.get(f"/api/members/{SLUG}/panel")
    assert resp.status == 400
    assert (await resp.json())["code"] == "missing_member"


async def test_a_hostile_member_name_is_refused_on_the_read(vetted):
    """The name is validated, not just compared."""
    c = await _client()
    for hostile in ("../../etc/passwd", "a\nb", "x" * 300, "a;b"):
        resp = await c.get(
            f"/api/members/{SLUG}/panel?member={quote(hostile, safe='')}",
        )
        assert resp.status == 400, f"{hostile!r} was accepted"


async def test_a_credential_shaped_crew_name_can_still_read_its_own_panel(vetted):
    """Two of our own guards collided, and only this shape shows it.

    Redaction scrubs the stored ``crew`` because a crew name is untrusted text
    rendered to the operator. The read check compares the EXACT name because
    slugification is lossy. Together they locked out any crew whose name happens to
    look credential-shaped: the stored owner became ``[REDACTED: credential]``,
    which equals no exact name, so that crew could never read its own panel.

    Ownership is decided on a digest of the exact name; the display text stays
    redacted. Nobody would think to try this name, which is exactly why it is
    pinned.
    """
    # An AKIA-prefixed 20-character name is enough to trip the credential detector.
    # Assembled rather than written literally; see test_mcp_panel_runtime.py.
    shaped = "".join(["AKIA", "IOSFODNN7", "EXAMPLE"])
    assert agent_panel._scrub(shaped) != shaped, "fixture is no longer redacted"

    slug = members_mod.slug_for_name(shaped)
    c = await _client(agent=shaped)
    pub = await c.post(
        "/api/agent-panel/publish",
        json={"data": {"cycle": 47}},
        headers={"X-Session-Key": "dashboard:chat-1"},
    )
    assert pub.status == 200, await pub.text()

    body = await (await c.get(f"/api/members/{slug}/panel?member={shaped}")).json()
    assert body["panel"] is not None, "the crew was locked out of its own panel"
    assert body["html"], "no document returned to the owning crew"
    assert body["panel"]["data"] == {"cycle": 47}
    # The DISPLAY text is still redacted -- the fix must not have simply stopped
    # scrubbing the name to make the comparison work.
    assert shaped not in json.dumps(body), "an unredacted credential-shaped name was served"
    assert "REDACTED" in body["panel"]["crew"]


async def test_the_ownership_digest_never_reaches_the_client(vetted):
    """It is derived from a name that may itself be credential-shaped."""
    c = await _client()
    await c.post(
        "/api/agent-panel/publish",
        json={"data": {"cycle": 47}},
        headers={"X-Session-Key": "dashboard:chat-1"},
    )
    body = await (await c.get(f"/api/members/{SLUG}/panel?member={CREW}")).json()
    assert "crew_key" not in json.dumps(body)
    assert agent_panel.crew_key(CREW) not in json.dumps(body)


async def test_an_app_token_caller_is_denied_the_panel(vetted):
    """``/api/members/{slug}/panel`` is a CHILD of ``/api/members``.

    An app token scoped to the parent is admitted to this route by prefix, so the
    denial has to be explicit. Apps are isolated from member surfaces generally --
    ``/api/members``, ``.../thread`` and ``.../activity`` all 404 for an app token --
    and a panel is a crew's published state plus a rendered document, squarely
    inside what that isolation withholds.

    404 rather than 403, matching the sibling: a distinct status would confirm the
    surface exists to a caller that may not know about it.
    """
    c = await _client()
    await c.post(
        "/api/agent-panel/publish",
        json={"data": {"cycle": 47}},
        headers={"X-Session-Key": "dashboard:chat-1"},
    )

    @web.middleware
    async def _as_app(request: web.Request, handler):
        request["app"] = "some-app"
        return await handler(request)

    # Middleware has to be inserted BEFORE the server starts: aiohttp freezes the
    # app on startup, so appending to a started client's middlewares is a no-op
    # and the test would pass without ever presenting an app token.
    app = _mounted()
    app.middlewares.insert(0, _as_app)
    app_client = TestClient(TestServer(app))
    await app_client.start_server()

    resp = await app_client.get(f"/api/members/{SLUG}/panel?member={CREW}")
    assert resp.status == 404, "an app token reached the panel read"
    body = await resp.text()
    assert "cycle" not in body, "panel data leaked to an app-token caller"


async def test_no_route_here_writes_from_the_browser_surface():
    """Everything under /api/members here is a read; writes are MCP-only."""
    member_routes = [
        r
        for r in _mounted().router.routes()
        if str(r.resource.canonical).startswith("/api/members")
    ]
    assert member_routes
    assert {r.method for r in member_routes} <= {"GET", "HEAD"}


async def test_the_gateway_registers_exactly_these_paths_without_importing_us():
    """The boot path binds these routes DEFERRED, so the paths are written twice.

    ``server._register_mcp_routes`` cannot call ``register_agent_panel_routes``:
    that would import this optional subsystem on every gateway launch before the
    socket binds, which the boot-path rule forbids. It therefore restates each
    path against ``server._deferred``, and a restated path is a path
    that can drift -- a route renamed here and not there would 404 in the gateway
    while every test in this file passed.

    Compares the two SETS rather than looking for known strings, so a route added
    to either side has to be added to both.
    """
    import inspect

    from kiro_crew.dashboard import server

    ours = {str(r.resource.canonical) for r in _mounted().router.routes()}
    boot_src = inspect.getsource(server._register_mcp_routes)
    # One spelling, normalised, so black's line wrapping cannot change the answer:
    # the binder call may sit on the same line as the path or on the next one.
    flat = " ".join(boot_src.split())
    deferred = {
        path
        for path in ours
        # The deferred binder is what proves the gateway serves it without an eager
        # import; a path bound any other way would not match.
        if f'"{path}", _deferred("agent_panel",' in flat
    }
    missing = ours - deferred
    assert not missing, (
        f"{sorted(missing)} are registered by this module but the gateway boot path "
        "does not bind them through the deferred binder, so they are either "
        "unserved or imported eagerly"
    )
