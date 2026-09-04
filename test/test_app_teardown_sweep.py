"""The gateway stops serving an app that another process tore down (#7926).

``kirocrew app disable`` and ``kirocrew app uninstall`` run in a different OS
process from the gateway. They write ``installed.json`` and report success, but
``RouteRegistry`` is an in-memory table in the gateway, so the app's routes keep
dispatching until something in THAT process removes the registration. These
tests drive a real aiohttp app through a real ``RouteRegistry`` and assert the
route is genuinely gone -- ``get_registered_apps`` no longer lists the app, and
the request 404s -- not merely hidden behind a check.

Every leak test asserts BOTH directions: the route answers 200 before the sweep
(so the test would fail on a base commit for the right reason) and 404 after.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.apps import hooks_integration, teardown
from kiro_crew.apps.manager import APP_MANIFEST_FILENAME, enable_app, install_app

ROUTES_MODULE = """\
from aiohttp import web

from kiro_crew.apps.route_registry import AppRoute


async def _probe(request, ctx):
    return web.json_response({"served": True})


def register(ctx):
    return [AppRoute(method="GET", path="/probe", handler=_probe)]
"""


def _make_hooks_app_source(tmp_path: Path, name: str) -> Path:
    """An app whose only backend surface is a ``hooks.routes`` module."""
    src = tmp_path / "source" / name
    (src / "backend").mkdir(parents=True, exist_ok=True)
    (src / APP_MANIFEST_FILENAME).write_text(
        json.dumps(
            {
                "name": name,
                "version": "1.0.0",
                "displayName": name,
                "description": "route teardown probe",
                "author": "tester",
                "backend": {"hooks": {"routes": "backend.routes:register"}},
            },
            indent=2,
        )
    )
    (src / "backend" / "routes.py").write_text(ROUTES_MODULE)
    return src


@pytest.fixture()
def sweep_env(tmp_path, monkeypatch):
    """A temp KIROCREW_HOME with the hooks system initialized on a real aiohttp app."""
    home = tmp_path / "kirocrew-home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    monkeypatch.setattr("kiro_crew.apps.execution.third_party_execution_allowed", lambda: True)
    monkeypatch.setattr(hooks_integration, "_route_registry", None)
    monkeypatch.setattr(hooks_integration, "_lifecycle_dispatcher", None)
    monkeypatch.setattr(hooks_integration, "_teardown_sweep_task", None)
    monkeypatch.setattr(hooks_integration, "_sweep_retry_reported", set())
    monkeypatch.setattr(hooks_integration, "_sweep_unfinished", set())
    monkeypatch.setattr(hooks_integration, "_sweep_done", {})
    monkeypatch.setattr(hooks_integration, "_seen_install_id", {})
    # Module-global and process-lifetime: a residual leaked by one test would make a
    # phantom sweep candidate in every later one.
    from kiro_crew.apps import lifecycle as _lifecycle

    monkeypatch.setattr(_lifecycle, "_DETACHED_HOOK_RESIDUALS", set())
    monkeypatch.setattr(_lifecycle, "_DETACHED_HOOK_TASKS", {})

    aio = web.Application()
    hooks_integration.init_hooks_system(aio)
    return {"home": home, "aio": aio, "tmp_path": tmp_path}


async def _install_enable_and_register(
    env: dict[str, Any], name: str, *, bump_install: bool = False
) -> None:
    """Bring an app up exactly as the gateway does: metadata, then hook wiring.

    ``bump_install`` advances ``installedAt`` past the previous installation's, which
    a real reinstall does by wall clock. The field has second resolution, so a test
    reinstalling in the same second would otherwise be indistinguishable from a
    re-enable.
    """
    from kiro_crew.apps.manager import get_app

    src = _make_hooks_app_source(env["tmp_path"], name)
    result = install_app(str(src))
    assert result.ok, result.error
    result = enable_app(name)
    assert result.ok, result.error
    record = get_app(name)
    if bump_install:
        meta_path = env["home"] / "apps" / name / "installed.json"
        data = json.loads(meta_path.read_text(encoding="utf-8"))
        data["installedAt"] = "2099-01-01T00:00:00Z"
        meta_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        record = get_app(name)
    await hooks_integration.on_app_enable(name, record)
    assert name in hooks_integration.get_route_registry().get_registered_apps()


async def _register_routes_only(env: dict[str, Any], name: str) -> None:
    """Re-wire an already-installed app's hooks, as a gateway-side enable does."""
    from kiro_crew.apps.manager import get_app

    await hooks_integration.on_app_enable(name, get_app(name))


async def _probe_route(client: TestClient, name: str) -> int:
    resp = await client.get(f"/api/apps/{name}/probe")
    await resp.release()
    return resp.status


def _write_enabled_flag(home: Path, name: str, *, enabled: bool) -> None:
    """Flip ``installed.json`` the way the CLI's ``disable_app`` does."""
    meta_path = home / "apps" / name / "installed.json"
    data = json.loads(meta_path.read_text(encoding="utf-8"))
    data["enabled"] = enabled
    meta_path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _remove_installed_metadata(home: Path, name: str) -> None:
    """Leave the state a CLI ``uninstall`` leaves: no metadata file at all."""
    (home / "apps" / name / "installed.json").unlink()


class _StubRegistration:
    """Stand-in for ``bridges.RegistrationResult``, whose errors are reported softly."""

    def __init__(self, errors: list[str] | None = None) -> None:
        self.errors = errors or []


class TestTornDownAppStopsBeingServed:
    """Both CLI verbs, and the still-installed app must be untouched."""

    @pytest.mark.asyncio
    async def test_disabled_out_of_process_app_route_is_removed(self, sweep_env):
        await _install_enable_and_register(sweep_env, "leak-probe")
        registry = hooks_integration.get_route_registry()

        client = TestClient(TestServer(sweep_env["aio"]))
        await client.start_server()
        try:
            # POSITIVE CONTROL: without the sweep this is the whole bug -- the
            # metadata says disabled and the route still answers.
            _write_enabled_flag(sweep_env["home"], "leak-probe", enabled=False)
            assert await _probe_route(client, "leak-probe") == 200
            assert "leak-probe" in registry.get_registered_apps()

            torn_down = await hooks_integration.reconcile_torn_down_apps()

            assert torn_down == ["leak-probe"]
            # REMOVED, not hidden: the table itself no longer carries the app.
            assert "leak-probe" not in registry.get_registered_apps()
            assert await _probe_route(client, "leak-probe") == 404
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_uninstalled_out_of_process_app_route_is_removed(self, sweep_env):
        """An absent metadata file is a definite not-installed, not an unknown."""
        await _install_enable_and_register(sweep_env, "leak-probe")
        registry = hooks_integration.get_route_registry()

        client = TestClient(TestServer(sweep_env["aio"]))
        await client.start_server()
        try:
            _remove_installed_metadata(sweep_env["home"], "leak-probe")
            assert await _probe_route(client, "leak-probe") == 200

            torn_down = await hooks_integration.reconcile_torn_down_apps()

            assert torn_down == ["leak-probe"]
            assert "leak-probe" not in registry.get_registered_apps()
            assert await _probe_route(client, "leak-probe") == 404
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_sweep_does_not_touch_a_still_enabled_app(self, sweep_env):
        """The inverse assertion: teardown must not over-reach past its target."""
        await _install_enable_and_register(sweep_env, "leak-probe")
        await _install_enable_and_register(sweep_env, "keeper-app")
        registry = hooks_integration.get_route_registry()

        client = TestClient(TestServer(sweep_env["aio"]))
        await client.start_server()
        try:
            _write_enabled_flag(sweep_env["home"], "leak-probe", enabled=False)

            torn_down = await hooks_integration.reconcile_torn_down_apps()

            assert torn_down == ["leak-probe"]
            assert registry.get_registered_apps() == ["keeper-app"]
            assert await _probe_route(client, "keeper-app") == 200
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_sweep_unloads_the_app_modules(self, sweep_env):
        """Deregistration must take ``sys.modules`` with it, or the code stays live."""
        import sys

        await _install_enable_and_register(sweep_env, "leak-probe")
        assert any(k.startswith("_kirocrew_app_leak-probe") for k in sys.modules)

        _write_enabled_flag(sweep_env["home"], "leak-probe", enabled=False)
        await hooks_integration.reconcile_torn_down_apps()

        assert not any(k.startswith("_kirocrew_app_leak-probe") for k in sys.modules)

    @pytest.mark.asyncio
    async def test_repeated_sweeps_report_the_teardown_once(self, sweep_env):
        """Nothing is left registered, so a second sweep has nothing to report."""
        await _install_enable_and_register(sweep_env, "leak-probe")
        _write_enabled_flag(sweep_env["home"], "leak-probe", enabled=False)

        assert await hooks_integration.reconcile_torn_down_apps() == ["leak-probe"]
        assert await hooks_integration.reconcile_torn_down_apps() == []


class TestSweepRefusesToActOnAnUnknownState:
    """``app_enabled_state`` is tri-state; only a CONFIRMED False may tear down."""

    @pytest.mark.asyncio
    async def test_unreadable_metadata_leaves_the_registration_standing(self, sweep_env):
        """A transient read fault must not take a live app's routes offline."""
        await _install_enable_and_register(sweep_env, "leak-probe")
        registry = hooks_integration.get_route_registry()

        client = TestClient(TestServer(sweep_env["aio"]))
        await client.start_server()
        try:
            # None, not False: the app may well still be enabled and the file
            # merely unreadable this instant.
            (sweep_env["home"] / "apps" / "leak-probe" / "installed.json").write_text(
                "{ not json", encoding="utf-8"
            )

            assert await hooks_integration.reconcile_torn_down_apps() == []
            assert "leak-probe" in registry.get_registered_apps()
            assert await _probe_route(client, "leak-probe") == 200
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_sweep_with_no_registry_is_a_no_op(self, sweep_env, monkeypatch):  # noqa: E501
        """Called before ``init_hooks_system``, the sweep must not raise."""
        monkeypatch.setattr(hooks_integration, "_route_registry", None)
        assert await hooks_integration.reconcile_torn_down_apps() == []


class TestSweepRunsTheSharedTeardownSequence:
    """Reaching past ``teardown_app_runtime`` would be a third partial copy of it."""

    @pytest.mark.asyncio
    async def test_sweep_goes_through_teardown_app_runtime(self, sweep_env, monkeypatch):
        """Pinned so a step added to the shared sequence cannot skip this path."""
        await _install_enable_and_register(sweep_env, "leak-probe")
        _write_enabled_flag(sweep_env["home"], "leak-probe", enabled=False)

        seen: list[tuple[str, Any]] = []
        real_teardown = teardown.teardown_app_runtime

        async def _record(name, record, **kwargs):
            seen.append((name, record.get("enabled")))
            return await real_teardown(name, record, **kwargs)

        monkeypatch.setattr(teardown, "teardown_app_runtime", _record)

        assert await hooks_integration.reconcile_torn_down_apps() == ["leak-probe"]
        assert seen == [("leak-probe", False)]

    @pytest.mark.asyncio
    async def test_sweep_starts_no_third_party_code(self, sweep_env, monkeypatch):
        """A disabled record with no observed port must not launch the app's own code."""
        await _install_enable_and_register(sweep_env, "leak-probe")
        _write_enabled_flag(sweep_env["home"], "leak-probe", enabled=False)

        ran_app_code: list[str] = []
        monkeypatch.setattr(
            teardown,
            "run_lifecycle_script",
            lambda *a, **k: ran_app_code.append("onDisable"),
        )
        real_disable = hooks_integration.on_app_disable

        async def _capture(name, info, **kwargs):
            if kwargs.get("run_app_hooks"):
                ran_app_code.append("on_shutdown")
            return await real_disable(name, info, **kwargs)

        monkeypatch.setattr(teardown, "on_app_disable", _capture)

        assert await hooks_integration.reconcile_torn_down_apps() == ["leak-probe"]
        assert ran_app_code == []

    @pytest.mark.asyncio
    async def test_uninstall_drops_the_apps_in_process_hook_registries(self, sweep_env):
        """A surviving slot-close hook makes an uninstalled app's tabs undismissable."""
        await _install_enable_and_register(sweep_env, "leak-probe")

        async def _hook(_key: str) -> None:
            raise RuntimeError("store is gone")

        teardown.register_slot_close_hook("leak-probe", _hook)
        teardown.register_app_disable_hook("leak-probe", _hook)
        try:
            _remove_installed_metadata(sweep_env["home"], "leak-probe")

            assert await hooks_integration.reconcile_torn_down_apps() == ["leak-probe"]

            # No hook registered is the path that returns True and lets a close through.
            assert await teardown.notify_slot_closed("leak-probe", "slot-1") is True
        finally:
            teardown.forget_app_hooks("leak-probe")

    @pytest.mark.asyncio
    async def test_disable_keeps_the_hook_registries(self, sweep_env):
        """Only uninstall clears them: nothing re-registers behind an uninstall, but a
        re-enable would leave a window before the app's own watchdog runs again."""
        await _install_enable_and_register(sweep_env, "leak-probe")
        calls: list[str] = []

        async def _hook(key: str) -> None:
            calls.append(key)

        teardown.register_slot_close_hook("leak-probe", _hook)
        try:
            _write_enabled_flag(sweep_env["home"], "leak-probe", enabled=False)

            assert await hooks_integration.reconcile_torn_down_apps() == ["leak-probe"]

            assert await teardown.notify_slot_closed("leak-probe", "slot-1") is True
            assert calls == ["slot-1"]
        finally:
            teardown.forget_app_hooks("leak-probe")


class TestSweepReachesHookOwnersWithoutRoutes:
    """Keyed on routes alone, an ``on_startup``-only app is never visited at all."""

    @pytest.mark.asyncio
    async def test_a_retained_startup_hook_makes_an_app_a_candidate(self, sweep_env, monkeypatch):
        """A detached startup task is live privileged work with no registry entry."""
        from kiro_crew.apps import lifecycle

        task = asyncio.get_running_loop().create_future()
        monkeypatch.setitem(lifecycle._DETACHED_HOOK_TASKS, "startup-only", {task})
        try:
            assert "startup-only" in hooks_integration._teardown_sweep_candidates()
        finally:
            task.cancel()

    @pytest.mark.asyncio
    async def test_an_empty_retained_task_set_is_not_a_candidate(self, sweep_env, monkeypatch):
        """The entry is popped once the last task is terminal; empty tracks nothing."""
        from kiro_crew.apps import lifecycle

        monkeypatch.setitem(lifecycle._DETACHED_HOOK_TASKS, "startup-only", set())
        assert "startup-only" not in hooks_integration._teardown_sweep_candidates()

    @pytest.mark.asyncio
    async def test_a_routeless_hook_owner_is_torn_down(self, sweep_env, monkeypatch):
        """An app whose on_startup spawned its own work leaves only its off-switch."""
        await _install_enable_and_register(sweep_env, "leak-probe")
        registry = hooks_integration.get_route_registry()
        # No routes and clean hook health: exactly the shape a route-keyed sweep missed.
        registry.deregister_app_routes("leak-probe")
        hooks_integration._hook_health.pop("leak-probe", None)
        stopped: list[str] = []

        async def _off_switch(app: str) -> None:
            stopped.append(app)

        teardown.register_app_disable_hook("leak-probe", _off_switch)
        try:
            assert "leak-probe" not in registry.get_registered_apps()
            assert "leak-probe" in hooks_integration._teardown_sweep_candidates()
            _write_enabled_flag(sweep_env["home"], "leak-probe", enabled=False)

            assert await hooks_integration.reconcile_torn_down_apps() == ["leak-probe"]
            # The gateway fired the app's own off-switch instead of waiting for the
            # app's watchdog to notice a disable this process never saw.
            assert stopped == ["leak-probe"]
        finally:
            teardown.forget_app_hooks("leak-probe")

    @pytest.mark.asyncio
    async def test_a_swept_app_is_not_torn_down_again_every_interval(self, sweep_env, monkeypatch):
        """A disable keeps the off-switch entry, so the candidate set alone repeats."""
        await _install_enable_and_register(sweep_env, "leak-probe")

        async def _off_switch(app: str) -> None:
            return None

        teardown.register_app_disable_hook("leak-probe", _off_switch)
        try:
            _write_enabled_flag(sweep_env["home"], "leak-probe", enabled=False)

            assert await hooks_integration.reconcile_torn_down_apps() == ["leak-probe"]
            assert "leak-probe" in hooks_integration._teardown_sweep_candidates()
            assert await hooks_integration.reconcile_torn_down_apps() == []
        finally:
            teardown.forget_app_hooks("leak-probe")

    @pytest.mark.asyncio
    async def test_a_reenable_clears_the_swept_marker(self, sweep_env):
        """So a second disable is torn down again rather than skipped as done."""
        await _install_enable_and_register(sweep_env, "leak-probe")
        _write_enabled_flag(sweep_env["home"], "leak-probe", enabled=False)
        assert await hooks_integration.reconcile_torn_down_apps() == ["leak-probe"]

        _write_enabled_flag(sweep_env["home"], "leak-probe", enabled=True)
        await _register_routes_only(sweep_env, "leak-probe")
        assert await hooks_integration.reconcile_torn_down_apps() == []

        _write_enabled_flag(sweep_env["home"], "leak-probe", enabled=False)
        assert await hooks_integration.reconcile_torn_down_apps() == ["leak-probe"]

    @pytest.mark.asyncio
    async def test_a_reenable_and_disable_between_sweeps_is_torn_down(self, sweep_env):
        """No sweep observes the enabled state, so nothing could clear a name memo.

        This is the sequence a remembered "already torn down" gets wrong: the app is
        swept, then re-enabled and registered, then disabled again, all before the
        next sweep runs. Deciding from live residue instead is self-correcting.
        """
        await _install_enable_and_register(sweep_env, "leak-probe")
        registry = hooks_integration.get_route_registry()
        _write_enabled_flag(sweep_env["home"], "leak-probe", enabled=False)
        assert await hooks_integration.reconcile_torn_down_apps() == ["leak-probe"]
        assert "leak-probe" not in registry.get_registered_apps()

        # Re-enabled THROUGH THE GATEWAY, so the runtime really is re-registered,
        # then disabled again -- with no sweep in between.
        _write_enabled_flag(sweep_env["home"], "leak-probe", enabled=True)
        await _register_routes_only(sweep_env, "leak-probe")
        assert "leak-probe" in registry.get_registered_apps()
        _write_enabled_flag(sweep_env["home"], "leak-probe", enabled=False)

        assert await hooks_integration.reconcile_torn_down_apps() == ["leak-probe"]
        assert "leak-probe" not in registry.get_registered_apps()

    @pytest.mark.asyncio
    async def test_an_uninstall_after_a_swept_disable_still_drops_the_hooks(self, sweep_env):
        """The other half of the same hole: the SHAPE changed, not just the flag."""
        await _install_enable_and_register(sweep_env, "leak-probe")

        async def _hook(_key: str) -> None:
            raise RuntimeError("store is gone")

        teardown.register_slot_close_hook("leak-probe", _hook)
        try:
            _write_enabled_flag(sweep_env["home"], "leak-probe", enabled=False)
            assert await hooks_integration.reconcile_torn_down_apps() == ["leak-probe"]
            # Deliberately KEPT by a disable, so it is still there to be dropped.
            assert await teardown.notify_slot_closed("leak-probe", "slot-1") is False

            # Now uninstalled, with no sweep having seen it enabled in between.
            _remove_installed_metadata(sweep_env["home"], "leak-probe")

            assert await hooks_integration.reconcile_torn_down_apps() == ["leak-probe"]
            assert await teardown.notify_slot_closed("leak-probe", "slot-1") is True
        finally:
            teardown.forget_app_hooks("leak-probe")

    @pytest.mark.asyncio
    async def test_sweep_with_no_hook_state_is_a_no_op(self, sweep_env, monkeypatch):
        monkeypatch.setattr(hooks_integration, "_route_registry", None)
        monkeypatch.setattr(hooks_integration, "_hook_health", {})
        assert await hooks_integration.reconcile_torn_down_apps() == []

    def test_generation_tokens_distinguish_the_three_states(self, sweep_env, monkeypatch):
        """An unreadable stat gets its OWN token, so it never matches a stored one."""
        gen = hooks_integration._metadata_generation
        assert gen("leak-probe", uninstalled=True) == "absent"

        monkeypatch.setattr(
            hooks_integration, "app_dir", lambda _n: sweep_env["home"] / "no-such-app"
        )
        assert gen("leak-probe", uninstalled=False) == "unreadable"

    def test_generation_moves_when_the_metadata_is_rewritten(self, sweep_env):
        """The property the skip decision rests on."""
        meta = sweep_env["home"] / "apps" / "gen-probe"
        meta.mkdir(parents=True)
        (meta / "installed.json").write_text('{"enabled": true}', encoding="utf-8")
        before = hooks_integration._metadata_generation("gen-probe", uninstalled=False)

        (meta / "installed.json").write_text('{"enabled": false}', encoding="utf-8")
        after = hooks_integration._metadata_generation("gen-probe", uninstalled=False)

        assert before != after

    @pytest.mark.asyncio
    async def test_a_second_uninstall_is_torn_down_despite_the_shared_token(self, sweep_env):
        """``"absent"`` is a CONSTANT, so two uninstalls generate the same token.

        Registering a runtime is what invalidates the memo, which is the only thing
        that can distinguish the second uninstall from the first.
        """
        await _install_enable_and_register(sweep_env, "leak-probe")
        registry = hooks_integration.get_route_registry()
        _remove_installed_metadata(sweep_env["home"], "leak-probe")
        assert await hooks_integration.reconcile_torn_down_apps() == ["leak-probe"]
        assert hooks_integration._sweep_done["leak-probe"] == "absent"

        # Reinstalled, enabled and registered THROUGH THE GATEWAY, then uninstalled
        # again -- with no sweep in between, and the same "absent" token.
        await _install_enable_and_register(sweep_env, "leak-probe")
        assert "leak-probe" in registry.get_registered_apps()
        _remove_installed_metadata(sweep_env["home"], "leak-probe")

        assert await hooks_integration.reconcile_torn_down_apps() == ["leak-probe"]
        assert "leak-probe" not in registry.get_registered_apps()

    @pytest.mark.asyncio
    async def test_registering_a_runtime_forgets_the_sweep_memos(self, sweep_env):
        hooks_integration._sweep_done["leak-probe"] = "absent"
        hooks_integration._sweep_unfinished.add("leak-probe")
        hooks_integration._sweep_retry_reported.add("leak-probe")

        await _install_enable_and_register(sweep_env, "leak-probe")

        assert "leak-probe" not in hooks_integration._sweep_done
        assert "leak-probe" not in hooks_integration._sweep_unfinished
        assert "leak-probe" not in hooks_integration._sweep_retry_reported


class TestSweepDoesNotRaceAReEnable:
    """The sweep reads and acts under the same lock the enable handler holds."""

    @pytest.mark.asyncio
    async def test_sweep_waits_for_the_lifecycle_lock(self, sweep_env):
        """``handle_enable_app`` holds it across the metadata write AND registration."""
        from kiro_crew.apps.manager import app_lifecycle_lock

        await _install_enable_and_register(sweep_env, "leak-probe")
        registry = hooks_integration.get_route_registry()
        _write_enabled_flag(sweep_env["home"], "leak-probe", enabled=False)

        lock = app_lifecycle_lock("leak-probe")
        await lock.acquire()
        try:
            task = asyncio.create_task(hooks_integration.reconcile_torn_down_apps())
            await asyncio.sleep(0.05)
            assert not task.done()
            assert "leak-probe" in registry.get_registered_apps()
        finally:
            lock.release()
        assert await task == ["leak-probe"]

    @pytest.mark.asyncio
    async def test_a_reenable_committed_under_the_lock_is_not_torn_down(self, sweep_env):
        """The state read happens INSIDE the lock, so it cannot be stale on use.

        Reddens if the read moves before the acquire: the sweep would then hold a
        ``False`` taken before the enable committed and tear down a live runtime,
        leaving the app enabled in metadata and serving nothing.
        """
        from kiro_crew.apps.manager import app_lifecycle_lock

        await _install_enable_and_register(sweep_env, "leak-probe")
        registry = hooks_integration.get_route_registry()
        _write_enabled_flag(sweep_env["home"], "leak-probe", enabled=False)

        lock = app_lifecycle_lock("leak-probe")
        await lock.acquire()
        task = asyncio.create_task(hooks_integration.reconcile_torn_down_apps())
        try:
            await asyncio.sleep(0.05)
            # The "enable handler" commits while it still holds the lock.
            _write_enabled_flag(sweep_env["home"], "leak-probe", enabled=True)
        finally:
            lock.release()

        assert await task == []
        assert "leak-probe" in registry.get_registered_apps()


class TestSweepCannotBeWedgedByOneApp:
    """A background sweep must never block without a deadline."""

    @pytest.mark.asyncio
    async def test_startup_cleanup_is_bounded(self, sweep_env, monkeypatch):
        """The ordinary disable contract waits forever; the sweep cannot afford to."""
        await _install_enable_and_register(sweep_env, "leak-probe")
        _write_enabled_flag(sweep_env["home"], "leak-probe", enabled=False)

        seen: list[bool] = []
        real_teardown = teardown.teardown_app_runtime

        async def _record(name, record, **kwargs):
            seen.append(bool(kwargs.get("bounded_startup")))
            return await real_teardown(name, record, **kwargs)

        monkeypatch.setattr(teardown, "teardown_app_runtime", _record)

        await hooks_integration.reconcile_torn_down_apps()

        assert seen == [True]

    @pytest.mark.asyncio
    async def test_a_stuck_startup_hook_does_not_wedge_later_apps(self, sweep_env, monkeypatch):
        """One app that never finishes must not stop the sweep reaching the rest."""
        from kiro_crew.apps import lifecycle

        await _install_enable_and_register(sweep_env, "aaa-stuck")
        await _install_enable_and_register(sweep_env, "zzz-fine")
        registry = hooks_integration.get_route_registry()
        _write_enabled_flag(sweep_env["home"], "aaa-stuck", enabled=False)
        _write_enabled_flag(sweep_env["home"], "zzz-fine", enabled=False)

        # A detached startup task that never terminates. With an unbounded wait the
        # sweep parks here and never reaches zzz-fine.
        never = asyncio.get_running_loop().create_future()
        monkeypatch.setitem(lifecycle._DETACHED_HOOK_TASKS, "aaa-stuck", {never})
        monkeypatch.setattr(lifecycle, "_HOOK_TIMEOUT_SEC", 0.05)
        try:
            torn_down = await asyncio.wait_for(
                hooks_integration.reconcile_torn_down_apps(), timeout=10
            )
        finally:
            never.cancel()

        assert torn_down == ["zzz-fine"]
        assert "zzz-fine" not in registry.get_registered_apps()
        # The stuck one is reported unfinished and stays a retry candidate.
        assert "aaa-stuck" in hooks_integration._sweep_unfinished
        assert "aaa-stuck" in registry.get_registered_apps()

    @pytest.mark.asyncio
    async def test_bounded_startup_is_independent_of_trust_withdrawal(self, sweep_env):
        """Reusing withdrawing_trust for the bound would launch the app's own code."""
        import inspect

        sig = inspect.signature(teardown.teardown_app_runtime)
        assert sig.parameters["bounded_startup"].default is False
        assert sig.parameters["withdrawing_trust"].default is False

    @pytest.mark.asyncio
    async def test_a_hanging_disable_hook_does_not_wedge_the_teardown(self, sweep_env, monkeypatch):
        """The off-switch is third-party code awaited inside the shared sequence.

        Unbounded, one app whose hook never returns parks the sweep before
        deregistration and every disabled app's routes stay callable. Bounded, the
        teardown proceeds -- but the app is NOT claimed as torn down, because its
        workers cannot be proven stopped.
        """
        await _install_enable_and_register(sweep_env, "leak-probe")
        registry = hooks_integration.get_route_registry()
        _write_enabled_flag(sweep_env["home"], "leak-probe", enabled=False)

        entered = asyncio.Event()
        released = asyncio.Event()

        async def _never_returns(_app: str) -> None:
            entered.set()
            await released.wait()

        monkeypatch.setattr(teardown, "_DISABLE_HOOK_TIMEOUT_SEC", 0.05)
        teardown.register_app_disable_hook("leak-probe", _never_returns)
        try:
            torn_down = await asyncio.wait_for(
                hooks_integration.reconcile_torn_down_apps(), timeout=10
            )
            assert entered.is_set()
            # Teardown continued past the hook, so the routes really are gone.
            assert "leak-probe" not in registry.get_registered_apps()
            # But unproven worker execution is residual, not a completion.
            assert torn_down == []
            assert "leak-probe" in hooks_integration._sweep_unfinished
        finally:
            released.set()
            teardown.forget_app_hooks("leak-probe")

    @pytest.mark.asyncio
    async def test_an_overrunning_disable_hook_is_not_cancelled(self, sweep_env, monkeypatch):
        """Cancelling could cancel a to_thread wrapper inside it while the worker runs."""
        monkeypatch.setattr(teardown, "_DISABLE_HOOK_TIMEOUT_SEC", 0.05)
        released = asyncio.Event()
        finished: list[str] = []

        async def _slow(_app: str) -> None:
            await released.wait()
            finished.append("ran to completion")

        teardown.register_app_disable_hook("leak-probe", _slow)
        try:
            assert await teardown.notify_app_disabled("leak-probe") is False
            assert finished == []
            # Still alive, so it completes on its own rather than being interrupted.
            released.set()
            await asyncio.sleep(0.05)
            assert finished == ["ran to completion"]
        finally:
            teardown.forget_app_hooks("leak-probe")

    @pytest.mark.asyncio
    async def test_a_disable_hook_that_raises_still_counts_as_stood_down(self, sweep_env):
        """It returned control, so it is no longer executing."""

        async def _boom(_app: str) -> None:
            raise RuntimeError("store is gone")

        teardown.register_app_disable_hook("leak-probe", _boom)
        try:
            assert await teardown.notify_app_disabled("leak-probe") is True
        finally:
            teardown.forget_app_hooks("leak-probe")

    @pytest.mark.asyncio
    async def test_no_registered_disable_hook_counts_as_stood_down(self, sweep_env):
        assert await teardown.notify_app_disabled("never-registered") is True

    def test_job_cleanup_is_a_residual_execution_key(self):
        """Absent from that map, a stubborn worker was neither failed nor warned."""
        assert "job_cleanup" in teardown._RESIDUAL_EXECUTION_KEYS
        assert "cron_cleanup" in teardown._RESIDUAL_EXECUTION_KEYS
        assert "startup_cleanup" in teardown._RESIDUAL_EXECUTION_KEYS

    @pytest.mark.asyncio
    async def test_a_stubborn_job_worker_is_not_a_completed_teardown(self, sweep_env, monkeypatch):
        """Still-running workers are residual app code, so the sweep must retry."""
        await _install_enable_and_register(sweep_env, "leak-probe")
        _write_enabled_flag(sweep_env["home"], "leak-probe", enabled=False)

        real_disable = hooks_integration.on_app_disable

        async def _reports_a_stubborn_worker(name, info, **kwargs):
            result = await real_disable(name, info, **kwargs)
            result["job_cleanup"] = "failed: removed 0, 2 worker(s) still running"
            return result

        monkeypatch.setattr(teardown, "on_app_disable", _reports_a_stubborn_worker)

        assert await hooks_integration.reconcile_torn_down_apps() == []
        assert "leak-probe" in hooks_integration._sweep_unfinished

    @pytest.mark.asyncio
    async def test_records_left_behind_are_a_warning_not_a_failure(self, sweep_env, monkeypatch):
        """Unwritten records are data loss, not third-party code still executing."""
        await _install_enable_and_register(sweep_env, "leak-probe")
        _write_enabled_flag(sweep_env["home"], "leak-probe", enabled=False)

        real_disable = hooks_integration.on_app_disable

        async def _reports_records_only(name, info, **kwargs):
            result = await real_disable(name, info, **kwargs)
            result["job_cleanup"] = "partial: removed 1, 2 run record(s) remain"
            return result

        monkeypatch.setattr(teardown, "on_app_disable", _reports_records_only)

        assert await hooks_integration.reconcile_torn_down_apps() == ["leak-probe"]

    @pytest.mark.asyncio
    async def test_a_same_name_reinstall_drops_the_old_installations_hooks(self, sweep_env):
        """No sweep sees a not-enabled state, so forget_app_hooks never ran.

        The stale closure is over a store the uninstall deleted, and
        ``notify_slot_closed`` reporting its failure is what makes a leftover tab
        undismissable.
        """
        await _install_enable_and_register(sweep_env, "leak-probe")

        async def _stale(_key: str) -> None:
            raise RuntimeError("store is gone")

        teardown.register_slot_close_hook("leak-probe", _stale)
        try:
            assert await teardown.notify_slot_closed("leak-probe", "slot-1") is False

            # Uninstalled and reinstalled under the same name, with no sweep between.
            _remove_installed_metadata(sweep_env["home"], "leak-probe")
            await _install_enable_and_register(sweep_env, "leak-probe", bump_install=True)

            assert await teardown.notify_slot_closed("leak-probe", "slot-1") is True
        finally:
            teardown.forget_app_hooks("leak-probe")

    @pytest.mark.asyncio
    async def test_an_ordinary_reenable_keeps_the_apps_hooks(self, sweep_env):
        """installedAt does not move on enable/disable, so the running app keeps them."""
        await _install_enable_and_register(sweep_env, "leak-probe")
        calls: list[str] = []

        async def _live(key: str) -> None:
            calls.append(key)

        teardown.register_slot_close_hook("leak-probe", _live)
        try:
            _write_enabled_flag(sweep_env["home"], "leak-probe", enabled=False)
            _write_enabled_flag(sweep_env["home"], "leak-probe", enabled=True)
            await _register_routes_only(sweep_env, "leak-probe")

            assert await teardown.notify_slot_closed("leak-probe", "slot-1") is True
            assert calls == ["slot-1"]
        finally:
            teardown.forget_app_hooks("leak-probe")

    @pytest.mark.asyncio
    async def test_a_cli_enable_landing_during_the_teardown_restores_registrations(
        self, sweep_env, monkeypatch
    ):
        """register_app is the exact inverse of the deregister_app that ran."""
        await _install_enable_and_register(sweep_env, "leak-probe")
        _write_enabled_flag(sweep_env["home"], "leak-probe", enabled=False)

        real_teardown = teardown.teardown_app_runtime
        restored: list[str] = []

        async def _enable_lands_mid_teardown(name, record, **kwargs):
            result = await real_teardown(name, record, **kwargs)
            # Stand in for `kirocrew app enable` completing in the other process.
            _write_enabled_flag(sweep_env["home"], name, enabled=True)
            return result

        monkeypatch.setattr(teardown, "teardown_app_runtime", _enable_lands_mid_teardown)
        monkeypatch.setattr(
            hooks_integration,
            "register_app",
            lambda n: restored.append(n) or _StubRegistration(),
        )

        assert await hooks_integration.reconcile_torn_down_apps() == ["leak-probe"]
        assert restored == ["leak-probe"]
        # Not recorded as swept, so the next sweep re-evaluates rather than
        # believing a generation a concurrent enable has invalidated.
        assert "leak-probe" not in hooks_integration._sweep_done

    @pytest.mark.asyncio
    async def test_a_failed_restore_is_reported_not_swallowed(self, sweep_env, monkeypatch):
        """register_app reports most problems softly, on .errors rather than raising."""
        await _install_enable_and_register(sweep_env, "leak-probe")
        _write_enabled_flag(sweep_env["home"], "leak-probe", enabled=False)

        real_teardown = teardown.teardown_app_runtime

        async def _enable_lands_mid_teardown(name, record, **kwargs):
            result = await real_teardown(name, record, **kwargs)
            _write_enabled_flag(sweep_env["home"], name, enabled=True)
            return result

        monkeypatch.setattr(teardown, "teardown_app_runtime", _enable_lands_mid_teardown)
        monkeypatch.setattr(
            hooks_integration,
            "register_app",
            lambda _n: _StubRegistration(errors=["registry write failed"]),
        )

        caplogged: list[str] = []
        monkeypatch.setattr(
            hooks_integration.logger,
            "warning",
            lambda msg, *a, **k: caplogged.append(msg % a if a else msg),
        )

        assert await hooks_integration.reconcile_torn_down_apps() == ["leak-probe"]
        assert any("could not be restored" in line for line in caplogged)

    @pytest.mark.asyncio
    async def test_an_unknown_state_after_the_teardown_is_not_reported_as_a_race(
        self, sweep_env, monkeypatch
    ):
        """Unreadable is a momentary fault, and not-enabled was already confirmed."""
        await _install_enable_and_register(sweep_env, "leak-probe")
        _write_enabled_flag(sweep_env["home"], "leak-probe", enabled=False)

        real_teardown = teardown.teardown_app_runtime
        restored: list[str] = []

        async def _corrupts_metadata(name, record, **kwargs):
            result = await real_teardown(name, record, **kwargs)
            (sweep_env["home"] / "apps" / name / "installed.json").write_text(
                "{ not json", encoding="utf-8"
            )
            return result

        monkeypatch.setattr(teardown, "teardown_app_runtime", _corrupts_metadata)
        monkeypatch.setattr(
            hooks_integration,
            "register_app",
            lambda n: restored.append(n) or _StubRegistration(),
        )

        assert await hooks_integration.reconcile_torn_down_apps() == ["leak-probe"]
        assert restored == []
        assert hooks_integration._sweep_done.get("leak-probe") is not None


class TestSweepSurvivesItsOwnFailures:
    """The exposure lasts until something deregisters, so the loop must not die."""

    @pytest.mark.asyncio
    async def test_a_failing_app_teardown_does_not_stop_the_sweep(self, sweep_env, monkeypatch):
        await _install_enable_and_register(sweep_env, "aaa-fails")
        await _install_enable_and_register(sweep_env, "zzz-succeeds")
        registry = hooks_integration.get_route_registry()
        _write_enabled_flag(sweep_env["home"], "aaa-fails", enabled=False)
        _write_enabled_flag(sweep_env["home"], "zzz-succeeds", enabled=False)

        real_teardown = teardown.teardown_app_runtime

        async def _explode_for_one(name, record, **kwargs):
            if name == "aaa-fails":
                raise RuntimeError("teardown blew up")
            return await real_teardown(name, record, **kwargs)

        monkeypatch.setattr(teardown, "teardown_app_runtime", _explode_for_one)

        torn_down = await hooks_integration.reconcile_torn_down_apps()

        assert torn_down == ["zzz-succeeds"]
        assert "aaa-fails" in registry.get_registered_apps()

    @pytest.mark.asyncio
    async def test_an_incomplete_teardown_is_not_reported_as_torn_down(
        self, sweep_env, monkeypatch
    ):
        """``teardown_app_runtime`` returns early when a detached startup hook will not stop."""
        await _install_enable_and_register(sweep_env, "leak-probe")
        registry = hooks_integration.get_route_registry()
        _write_enabled_flag(sweep_env["home"], "leak-probe", enabled=False)

        async def _returns_without_deregistering(name, record, **kwargs):
            return teardown.TeardownResult(
                warnings=[], failures=["startup cleanup incomplete: still running"]
            )

        monkeypatch.setattr(teardown, "teardown_app_runtime", _returns_without_deregistering)

        assert await hooks_integration.reconcile_torn_down_apps() == []
        assert "leak-probe" in registry.get_registered_apps()

    @pytest.mark.asyncio
    async def test_a_reported_failure_keeps_the_app_a_retry_candidate(self, sweep_env, monkeypatch):
        """A backend still listening leaves NO registry entry, only a failure note.

        So the registry re-check alone reads the teardown as complete and the app
        drops out of every future sweep while its process keeps running.
        """
        await _install_enable_and_register(sweep_env, "leak-probe")
        registry = hooks_integration.get_route_registry()
        _write_enabled_flag(sweep_env["home"], "leak-probe", enabled=False)

        real_teardown = teardown.teardown_app_runtime
        attempts: list[str] = []

        async def _clears_registry_but_reports_failure(name, record, **kwargs):
            attempts.append(name)
            result = await real_teardown(name, record, **kwargs)
            return teardown.TeardownResult(
                warnings=result.warnings,
                failures=[
                    "backend still running on port 9101 - the gateway stopped every "
                    "process it was tracking, so this one is not ours to stop"
                ],
            )

        monkeypatch.setattr(teardown, "teardown_app_runtime", _clears_registry_but_reports_failure)

        # Not claimed as torn down, even though every registry is now clean.
        assert await hooks_integration.reconcile_torn_down_apps() == []
        assert "leak-probe" not in registry.get_registered_apps()
        assert attempts == ["leak-probe"]

        # And still retried, which the registry sets alone could not have produced.
        assert await hooks_integration.reconcile_torn_down_apps() == []
        assert attempts == ["leak-probe", "leak-probe"]

    @pytest.mark.asyncio
    async def test_a_retry_that_succeeds_clears_the_unfinished_marker(self, sweep_env, monkeypatch):
        """An adopted backend that later goes away must stop being retried."""
        await _install_enable_and_register(sweep_env, "leak-probe")
        _write_enabled_flag(sweep_env["home"], "leak-probe", enabled=False)

        real_teardown = teardown.teardown_app_runtime
        attempts: list[str] = []

        async def _fails_once(name, record, **kwargs):
            attempts.append(name)
            result = await real_teardown(name, record, **kwargs)
            if len(attempts) == 1:
                return teardown.TeardownResult(warnings=[], failures=["backend still running"])
            return result

        monkeypatch.setattr(teardown, "teardown_app_runtime", _fails_once)

        assert await hooks_integration.reconcile_torn_down_apps() == []
        assert await hooks_integration.reconcile_torn_down_apps() == ["leak-probe"]
        assert await hooks_integration.reconcile_torn_down_apps() == []
        assert attempts == ["leak-probe", "leak-probe"]

    @pytest.mark.asyncio
    async def test_a_raising_teardown_keeps_the_app_a_retry_candidate(self, sweep_env, monkeypatch):
        """An exception is no more proof of completion than a reported failure."""
        await _install_enable_and_register(sweep_env, "leak-probe")
        hooks_integration.get_route_registry().deregister_app_routes("leak-probe")
        hooks_integration._hook_health.pop("leak-probe", None)
        hooks_integration._sweep_unfinished.add("leak-probe")
        _write_enabled_flag(sweep_env["home"], "leak-probe", enabled=False)

        attempts: list[str] = []

        async def _explodes(name, record, **kwargs):
            attempts.append(name)
            raise RuntimeError("teardown blew up")

        monkeypatch.setattr(teardown, "teardown_app_runtime", _explodes)

        assert await hooks_integration.reconcile_torn_down_apps() == []
        assert await hooks_integration.reconcile_torn_down_apps() == []
        assert attempts == ["leak-probe", "leak-probe"]

    @pytest.mark.asyncio
    async def test_loop_keeps_running_after_a_failed_sweep(self, sweep_env, monkeypatch):
        calls: list[int] = []
        done = asyncio.Event()

        async def _fail_then_succeed() -> list[str]:
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("sweep blew up")
            done.set()
            return []

        monkeypatch.setattr(hooks_integration, "_TEARDOWN_SWEEP_INTERVAL", 0.01)
        monkeypatch.setattr(hooks_integration, "reconcile_torn_down_apps", _fail_then_succeed)

        hooks_integration.start_teardown_sweep()
        try:
            await asyncio.wait_for(done.wait(), timeout=5)
        finally:
            await hooks_integration.stop_teardown_sweep()

        assert len(calls) >= 2


class TestSweepLifecycle:
    """Arming and cancelling, so an in-process restart leaks no task."""

    @pytest.mark.asyncio
    async def test_start_is_idempotent(self, sweep_env):
        hooks_integration.start_teardown_sweep()
        first = hooks_integration._teardown_sweep_task
        hooks_integration.start_teardown_sweep()
        try:
            assert hooks_integration._teardown_sweep_task is first
        finally:
            await hooks_integration.stop_teardown_sweep()

    @pytest.mark.asyncio
    async def test_stop_ends_the_loop_from_its_idle_gap(self, sweep_env):
        """Asked, not cancelled -- and it does not wait out the whole interval."""
        hooks_integration.start_teardown_sweep()
        task = hooks_integration._teardown_sweep_task
        assert task is not None

        await hooks_integration.stop_teardown_sweep()

        assert task.done()
        assert not task.cancelled()
        assert task.exception() is None
        assert hooks_integration._teardown_sweep_task is None

    @pytest.mark.asyncio
    async def test_stop_never_cancels_a_pass_in_flight(self, sweep_env, monkeypatch):
        """Cancelling would strand an offloaded deregister_app worker mid-deletion."""
        started = asyncio.Event()
        release = asyncio.Event()
        finished: list[str] = []

        async def _slow_sweep() -> list[str]:
            started.set()
            await release.wait()
            finished.append("completed")
            return []

        monkeypatch.setattr(hooks_integration, "_TEARDOWN_SWEEP_INTERVAL", 0.01)
        monkeypatch.setattr(hooks_integration, "_SWEEP_STOP_BUDGET_SECS", 0.05)
        monkeypatch.setattr(hooks_integration, "reconcile_torn_down_apps", _slow_sweep)

        hooks_integration.start_teardown_sweep()
        task = hooks_integration._teardown_sweep_task
        await asyncio.wait_for(started.wait(), timeout=5)

        # Budget expires while the pass is still running.
        await hooks_integration.stop_teardown_sweep()

        assert task is not None
        assert not task.cancelled()
        assert not task.done()
        assert finished == []

        # It leaves on its own once the pass ends, because the stop is already set.
        release.set()
        await asyncio.wait_for(task, timeout=5)
        assert finished == ["completed"]

    @pytest.mark.asyncio
    async def test_stop_without_start_is_a_no_op(self, sweep_env):
        await hooks_integration.stop_teardown_sweep()
        assert hooks_integration._teardown_sweep_task is None

    @pytest.mark.asyncio
    async def test_stop_clears_the_memoized_markers(self, sweep_env):
        """An in-process restart rebuilds the registries, so the judgements go too."""
        hooks_integration._sweep_unfinished.add("leak-probe")
        hooks_integration._sweep_retry_reported.add("leak-probe")
        hooks_integration._sweep_done["leak-probe"] = "gen"

        await hooks_integration.stop_teardown_sweep()

        assert hooks_integration._sweep_unfinished == set()
        assert hooks_integration._sweep_retry_reported == set()
        assert hooks_integration._sweep_done == {}
