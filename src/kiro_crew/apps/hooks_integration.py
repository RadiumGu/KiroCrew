"""Hooks Integration — wires the hooks system into the gateway lifecycle.

This module provides the glue functions that connect:
- RouteRegistry into the enable/disable flow
- LifecycleDispatcher into gateway startup/shutdown
- CronSDK cleanup into disable/uninstall

These functions are called from routes.py and server.py at the appropriate
lifecycle points. An app that declares no hooks is a no-op.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from aiohttp import web

from kiro_crew.apps.backend import spawned_backend_names, stop_app_backend
from kiro_crew.apps.bridges import (
    disarm_app_crons_for_execution,
    register_app,
    register_app_crons_with_service,
)
from kiro_crew.apps.context import AppContext, build_app_context
from kiro_crew.apps.cron_sdk import CronSDK
from kiro_crew.apps.execution import (
    app_execution_denied,
    shipped_builtin_app_root,
)
from kiro_crew.apps.job_routes import register_job_routes
from kiro_crew.apps.job_sdk import forget_sdk, get_sdk, reconcile_all, register_sdk
from kiro_crew.apps.job_sdk import registered_apps as job_registered_apps
from kiro_crew.apps.lifecycle import LifecycleDispatcher, apps_with_retained_startup_hooks
from kiro_crew.apps.manager import (
    app_dir,
    app_enabled_state,
    app_lifecycle_lock,
    get_app,
    list_apps,
)
from kiro_crew.apps.route_registry import RouteRegistry
from kiro_crew.cron import CronStoreBusy, CronStoreUnreadable
from kiro_crew.executors import subprocess_executor
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

# Module-level singletons (initialized at gateway startup)
_route_registry: RouteRegistry | None = None
_lifecycle_dispatcher: LifecycleDispatcher | None = None

#: How often the gateway re-reads ``installed.json`` to find apps that were torn
#: down out-of-process. Matches ``backend._HEALTH_WATCH_INTERVAL``, which sweeps
#: the same metadata for the same reason on the backend-process side.
_TEARDOWN_SWEEP_INTERVAL = 15.0

#: The sweep task, armed at gateway startup and cancelled at cleanup.
_teardown_sweep_task: asyncio.Task[None] | None = None

#: Set to ask the sweep loop to stop. It exits from its IDLE gap only, never
#: mid-pass -- see stop_teardown_sweep for why cancelling a pass is unsafe.
_teardown_sweep_stop: asyncio.Event | None = None

#: How long a graceful stop waits for a pass already in flight. Sized against the
#: gateway's cooperative shutdown budget, which ``on_gateway_shutdown`` already
#: spends 6s of on its own backend sweep.
_SWEEP_STOP_BUDGET_SECS = 3.0

#: Apps whose sweep teardown did not complete and whose retry has been reported.
#: A residual startup hook is unrecoverable for the life of the process, so
#: without this the same warning is written every interval forever.
_sweep_retry_reported: set[str] = set()

#: App name -> the metadata GENERATION it was last successfully swept at. Keyed on
#: the generation, never the name alone: a name-only "already done" marker cannot
#: tell a swept app from one since re-enabled and disabled again, or one since
#: UNINSTALLED and now needing its hook registries dropped, and skipping either is
#: the bug this sweep exists to fix.
_sweep_done: dict[str, str] = {}

#: Apps whose last sweep teardown did not finish. Two jobs, both in the safe
#: direction: it forces another attempt, and it keeps the app a CANDIDATE at all --
#: some residue leaves no registry entry (an adopted backend still listening on its
#: port), so an app with nothing left to find by would otherwise be dropped while
#: its process kept running.
_sweep_unfinished: set[str] = set()


#: App name -> the ``installedAt`` stamp of the installation this gateway last
#: registered a runtime for. A DIFFERENT installation under the same name means every
#: in-process hook closure left over from the old one is garbage.
_seen_install_id: dict[str, str] = {}


def forget_sweep_state(app_name: str, *, install_id: str = "") -> None:
    """Forget everything the teardown sweep memoized about *app_name*.

    Called wherever this gateway REGISTERS an app's runtime, which is what makes
    ``_sweep_done``'s meaning exact: "nothing has been registered since I last swept
    this app". The generation token alone cannot carry that, because ``"absent"`` is
    a constant -- an uninstall, a reinstall-and-enable, then a second uninstall
    produce the same token, so a sweep that memoized the first uninstall would skip
    the second and leave the reinstalled app's routes callable.

    ``install_id`` (the record's ``installedAt``) additionally detects a REPLACEMENT:
    a CLI uninstall followed by a same-name reinstall and enable inside one sweep
    interval never lets the sweep see a not-enabled state, so ``forget_app_hooks``
    never runs and the OLD installation's hook closures stay registered under the new
    app's name -- closures over a store whose files were deleted, which
    ``notify_slot_closed`` then reports as a failure and ``api_chat_slot_delete``
    turns into an undismissable tab. Dropping them here is consistent with the model
    those registries already document: they are process memory that a gateway restart
    empties, and every app is required to re-register from its own watchdog.

    Only fires on a CHANGED id, never on a first sighting, so an ordinary
    enable/disable cycle -- which rewrites ``updatedAt`` and leaves ``installedAt``
    alone -- does not clear hooks the running app still owns. ``installedAt`` has
    second resolution, so an uninstall and reinstall inside the same second are
    indistinguishable; that residual is narrower than the interval-wide window it
    replaces.
    """
    _sweep_done.pop(app_name, None)
    _sweep_unfinished.discard(app_name)
    _sweep_retry_reported.discard(app_name)
    if not install_id:
        return
    previous = _seen_install_id.get(app_name)
    _seen_install_id[app_name] = install_id
    if previous is not None and previous != install_id:
        from kiro_crew.apps import teardown as teardown_mod

        logger.info(
            "App %s was replaced by a new installation; dropping the previous "
            "installation's in-process hooks",
            app_name,
        )
        teardown_mod.forget_app_hooks(app_name)


def _metadata_generation(name: str, *, uninstalled: bool) -> str:
    """A token that changes whenever an app's ``installed.json`` is rewritten.

    Every lifecycle write goes through that file -- ``enable_app`` and
    ``disable_app`` both rewrite it, and an uninstall removes it -- so the token
    distinguishes states a name cannot: swept-and-still-disabled from
    disabled-again-since, and either from uninstalled.

    ``st_size`` rides along with ``st_mtime_ns`` because a filesystem with coarse
    timestamp granularity could otherwise hand two writes in the same tick the same
    token; the ``enabled`` flag flipping changes the length. An unreadable stat is
    reported as its own token so a swept marker is not matched on a guess.
    """
    if uninstalled:
        return "absent"
    try:
        st = (app_dir(name) / "installed.json").stat()
    except OSError:
        return "unreadable"
    return f"{st.st_mtime_ns}:{st.st_size}"


# Last hook-wiring health per app, for apps whose hooks did NOT come up clean.
# ``AppHealthStatus`` lives on the AppContext, which both wiring paths drop as
# soon as they finish, so the reason a hook failed had no reader on the startup
# path -- an app that failed to load was indistinguishable from one that was
# never installed. Published here so ``GET /api/apps`` can report it under the
# same ``hooks.health_status`` spelling the enable response already uses.
_hook_health: dict[str, dict[str, Any]] = {}


def _publish_hook_health(app_name: str, ctx: AppContext) -> dict[str, Any] | None:
    """Record (or clear) one app's hook-wiring health and return the snapshot.

    Both wiring paths funnel through here so they cannot drift: whatever
    ``register_app_routes`` and the lifecycle dispatcher marked on the context is
    what an operator reads back. A healthy wire-up clears any earlier entry, so a
    fixed app stops reporting a stale failure after a re-enable.
    """
    if ctx.health.status == "healthy":
        _hook_health.pop(app_name, None)
        return None
    snapshot = ctx.health.to_dict()
    _hook_health[app_name] = snapshot
    return snapshot


def clear_hook_health(app_name: str) -> None:
    """Forget an app's recorded hook health (disable / teardown)."""
    _hook_health.pop(app_name, None)


def get_all_hook_health() -> dict[str, dict[str, Any]]:
    """Return every recorded non-healthy hook-wiring snapshot, by app name."""
    return {name: dict(snapshot) for name, snapshot in _hook_health.items()}


def init_hooks_system(
    app: web.Application,
    *,
    cron_service: Any = None,
    broadcast_fn: Any = None,
    spawn_impl: Any = None,
) -> None:
    """Initialize the hooks system at gateway startup.

    Called from server.py after all core routes are registered.
    """
    global _route_registry, _lifecycle_dispatcher

    # BEFORE the catch-all, not after: aiohttp matches in registration order, so
    # /api/apps/{app_name}/{path:.*} would otherwise swallow every _jobs request
    # and answer it from the app's own dispatch table.
    register_job_routes(app)

    _route_registry = RouteRegistry(app)
    _route_registry.ensure_catch_all()

    _lifecycle_dispatcher = LifecycleDispatcher(
        cron_service=cron_service,
        broadcast_fn=broadcast_fn,
        spawn_impl=spawn_impl,
    )

    logger.info("Hooks system initialized")


def get_route_registry() -> RouteRegistry | None:
    """Get the global RouteRegistry instance."""
    return _route_registry


def get_lifecycle_dispatcher() -> LifecycleDispatcher | None:
    """Get the global LifecycleDispatcher instance."""
    return _lifecycle_dispatcher


async def stop_retained_startup_hooks(app_name: str, *, bounded: bool) -> bool:
    """Wait for retained startup execution, failing closed on ownership errors."""
    if _lifecycle_dispatcher is None:
        return True
    try:
        return await _lifecycle_dispatcher.stop_detached_startup_hooks(app_name, bounded=bounded)
    except Exception:  # noqa: BLE001 - destructive lifecycle work must fail closed
        logger.exception("Could not verify detached startup-hook cleanup for %s", app_name)
        return False


def _app_hook_root(app_name: str) -> Path:
    """Return the immutable shipped root when one owns this app name."""
    return shipped_builtin_app_root(app_name) or app_dir(app_name)


def _build_app_context_from_info(
    app_info: dict[str, Any],
    cron_service: Any = None,
    broadcast_fn: Any = None,
    spawn_impl: Any = None,
) -> Any:
    """Build an AppContext from app info dict — shared helper for consistent context."""
    name = app_info.get("name", "")
    manifest = app_info.get("manifest", {})
    permissions = manifest.get("permissions", {})
    data_path = app_dir(name) / "data"
    data_path.mkdir(parents=True, exist_ok=True)
    ctx = build_app_context(
        app_name=name,
        data_dir=data_path,
        permissions=permissions,
        cron_service=cron_service,
        broadcast_fn=broadcast_fn,
        spawn_impl=spawn_impl,
        app_config=manifest.get("extra", {}),
    )
    # The shared _jobs routes are mounted once for every app and resolve the app
    # from the URL, so they need a name -> SDK lookup. Publishing happens here,
    # in the gateway wiring, rather than inside build_app_context, so building a
    # context in a test does not put an SDK behind the live routes.
    if ctx.job is not None:
        register_sdk(ctx.job)
    return ctx


async def on_app_enable(
    app_name: str,
    app_info: dict[str, Any],
    *,
    cron_service: Any = None,
    broadcast_fn: Any = None,
    spawn_impl: Any = None,
) -> dict[str, Any]:
    """Called after an app is enabled — register routes and invoke startup hook.

    Returns dict with hook results to include in the enable response.
    """
    result: dict[str, Any] = {}
    denied = app_execution_denied(
        app_name,
        action="hook_enable_register",
        app_root=_app_hook_root(app_name),
        caller="gateway",
    )
    if denied:
        logger.warning(
            "App %s: skipping enable-time hooks and crons: %s",
            app_name,
            denied,
        )
        if cron_service is not None:
            await disarm_app_crons_for_execution(app_name, cron_service)
        if _route_registry:
            _route_registry.deregister_app_routes(app_name)
        return result

    manifest = app_info.get("manifest", {})
    backend = manifest.get("backend", {})
    hooks = backend.get("hooks", {})

    # Promote app-declared crons into the running scheduler.
    try:
        # register_app_crons_with_service is async: it awaits the async CronSDK
        # mutation API, which offloads each bounded store-lock spin (whose
        # contention spin does time.sleep) to a worker thread. Awaiting it here
        # never parks the gateway loop, and timer arming is owned by CronService
        # (no caller-side re-arm needed).
        registered = await register_app_crons_with_service(app_name, cron_service)
        if registered:
            result["crons_registered"] = registered
        sel().log_api_access(
            caller="gateway",
            operation="app_crons_register",
            outcome="completed",
            resources=f"app={app_name} crons={registered}",
        )
    except Exception as exc:
        logger.warning("Cron registration failed for %s: %s", app_name, exc)
        sel().log_api_access(
            caller="gateway",
            operation="app_crons_register",
            outcome="failed",
            resources=app_name,
            error=str(exc),
        )

    if not hooks:
        return result

    # A runtime is about to exist for this app again, so anything the teardown sweep
    # concluded about the last one is stale. See forget_sweep_state.
    forget_sweep_state(app_name, install_id=str(app_info.get("installedAt") or ""))

    sel().log_api_access(
        caller="gateway",
        operation="app_hooks_enable",
        outcome="started",
        resources=app_name,
    )

    # Build AppContext for this app (shared helper ensures consistency)
    ctx = _build_app_context_from_info(app_info, cron_service, broadcast_fn, spawn_impl)

    # Register routes if declared
    routes_hook = hooks.get("routes", "")
    if routes_hook and _route_registry:
        app_root = _app_hook_root(app_name)
        registered = await _route_registry.register_app_routes(app_name, app_root, routes_hook, ctx)
        if registered:
            result["hooks_routes"] = registered

    # Invoke on_startup hook if declared
    startup_hook = hooks.get("on_startup", "")
    if startup_hook and _lifecycle_dispatcher:
        success = await _lifecycle_dispatcher._invoke(app_name, startup_hook, ctx, phase="startup")
        result["hooks_startup"] = "ok" if success else "failed"

    # Reconcile this app's job records now that its startup hook has registered
    # the runners. The boot-time pass runs once, after the enable LOOP, so an app
    # enabled later in the gateway's life never got one -- and reconciliation is
    # only decidable once the runners are known, since "no runner for this kind"
    # is one of the two outcomes it reports. Without this a record left
    # non-terminal by a previous process stayed that way until the next restart,
    # and `list_active` kept reporting work that had already stopped, which is the
    # exact symptom this SDK exists to remove.
    #
    # Scoped to the hooks path on purpose: an app with no backend hooks returned
    # above, and it can register no runners, so there is nothing here to decide
    # against -- the boot-time pass already owns that case.
    if getattr(ctx, "job", None) is not None:
        try:
            flipped = await asyncio.to_thread(ctx.job.reconcile)
            if flipped:
                result["job_reconcile"] = f"resolved {flipped} interrupted run(s)"
        except Exception as exc:  # noqa: BLE001 - enable must not fail on this
            logger.warning(
                "App %s: job reconciliation after enable did not complete: %s", app_name, exc
            )
            result["job_reconcile"] = "failed: stale run records may remain"

    # Report health status
    health_snapshot = _publish_hook_health(app_name, ctx)
    if health_snapshot:
        result["health_status"] = health_snapshot

    sel().log_api_access(
        caller="gateway",
        operation="app_hooks_enable",
        outcome="completed",
        resources=app_name,
    )
    return result


async def stop_app_startup_hooks(app_name: str, *, bounded: bool = False) -> bool:
    """Prove retained startup ownership clear before teardown mutates state."""
    if not _lifecycle_dispatcher:
        return True
    return await _lifecycle_dispatcher.stop_detached_startup_hooks(app_name, bounded=bounded)


async def _cleanup_app_jobs(app_name: str, result: dict[str, Any]) -> None:
    """Stop and drop an app's durable job runs, mirroring the cron contract:
    idempotent, and a failure is REPORTED rather than crashing the disable.

    Signalling is all the SDK can do about the WORK -- a runner that never polls
    its handle within the deadline is reported -- and the RECORDS stay deleted:
    the SDK marks every live handle discarded under the same lock its guarded
    writer takes, so a worker returning mid-cleanup cannot write its record back,
    and a partial delete is reported rather than read as clean.

    Keyed off the REGISTRY, not the manifest grant. Gating this on
    ``permissions.get("jobs")`` meant revoking the grant and then disabling took
    the one path that skips it entirely: the SDK stays registered from the enable
    that DID have the grant, its workers keep executing, and the lookup entry is
    dropped afterwards -- so nothing can ever reach them again. The grant governs
    whether an app may START jobs; whether it HAS any running is a fact about the
    registry, and that is what teardown has to ask.

    A separate function so that grant-independence is testable on its own; it was
    unreachable while the logic sat inline behind the condition it must ignore.
    """
    job_sdk = get_sdk(app_name)
    if job_sdk is None:
        return
    try:
        cleanup = await job_sdk.remove_all_async()
        if not cleanup.is_clean:
            # Reported, not swallowed: a cleanup that left records behind OR left
            # app code executing must not read as clean.
            parts = []
            if cleanup.failed:
                parts.append(f"{cleanup.failed} run record(s) remain")
            if cleanup.still_running:
                parts.append(f"{cleanup.still_running} worker(s) still running")
            # ``failed:`` when app code is STILL EXECUTING, ``partial:`` when only
            # records were left behind. The marker is the contract teardown.py reads
            # to tell residual third-party execution (a failure the caller retries
            # and must not report as a clean teardown) from data left unwritten (a
            # warning). Without the distinction a stubborn worker was neither
            # failed nor even warned about, because job_cleanup was absent from the
            # key set teardown.py inspects at all.
            marker = "failed" if cleanup.still_running else "partial"
            result["job_cleanup"] = f"{marker}: removed {cleanup.removed}, " + "; ".join(parts)
            sel().log_api_access(
                caller="gateway",
                operation="jobs.deregister",
                outcome="partial",
                resources=(
                    f"app={app_name} removed={cleanup.removed} "
                    f"failed={cleanup.failed} running={cleanup.still_running}"
                ),
            )
        elif cleanup.removed:
            result["job_cleanup"] = f"removed {cleanup.removed} run record(s)"
    except OSError as exc:
        logger.warning("App %s: job cleanup could not complete on disable: %s", app_name, exc)
        result["job_cleanup"] = "failed: run records may remain"
        sel().log_api_access(
            caller="gateway",
            operation="jobs.deregister",
            outcome="failed",
            resources=app_name,
            error=str(exc),
        )


async def on_app_disable(
    app_name: str,
    app_info: dict[str, Any],
    *,
    run_app_hooks: bool = True,
    bounded_startup_cleanup: bool = False,
    startup_stopped: bool | None = None,
) -> dict[str, Any]:
    """Called before an app is disabled — deregister routes and invoke shutdown hook.

    ``run_app_hooks=False`` skips the app's OWN ``on_shutdown`` hook while still
    doing everything the GATEWAY owns (route deregistration, cron cleanup). The
    caller passes it when there is no reason to believe the app is running: its
    shutdown hook is third-party code, and *starting* that code as part of
    withdrawing its permission to run would turn the security operation into an
    execution vector. Nothing that STOPS something is ever skipped by this flag.

    ``bounded_startup_cleanup`` distinguishes trust withdrawal from ordinary
    disable. Ordinary disable waits until an owned detached startup task exits;
    trust withdrawal stays bounded and reports residual execution as a hard
    failure so the grant remains in place for a retry. Shared teardown passes a
    pre-established ``startup_stopped`` result so this function cannot repeat the
    ownership wait after other teardown work has begun.
    """
    result: dict[str, Any] = {}
    manifest = app_info.get("manifest", {})
    backend = manifest.get("backend", {})
    hooks = backend.get("hooks", {})

    # A startup hook may have been detached after its readiness deadline. It is
    # still third-party code with a live AppContext, so disable/revocation must
    # stop it even if the current manifest no longer declares hooks. A resistant
    # task becomes a hard teardown failure; callers keep trust in place rather
    # than falsely claiming all app code stopped.
    if startup_stopped is None:
        startup_stopped = await stop_app_startup_hooks(app_name, bounded=bounded_startup_cleanup)
    if not startup_stopped:
        result["startup_cleanup"] = (
            "failed: detached startup hook is still running; teardown not started"
        )
        return result

    if hooks:
        sel().log_api_access(
            caller="gateway",
            operation="app_hooks_disable",
            outcome="started",
            resources=app_name,
        )

    # Invoke on_shutdown only after retained startup ownership is proven clear.
    # Trust withdrawal must stay bounded and must never overlap partially
    # initialized startup state with an unbounded third-party shutdown hook.
    shutdown_hook = hooks.get("on_shutdown", "")
    if shutdown_hook and _lifecycle_dispatcher and run_app_hooks and startup_stopped:
        success = await _lifecycle_dispatcher._invoke(
            app_name,
            shutdown_hook,
            _lifecycle_dispatcher._build_context(app_info),
            phase="shutdown",
        )
        result["hooks_shutdown"] = "ok" if success else "failed"

    # Deregister routes
    if _route_registry:
        _route_registry.deregister_app_routes(app_name)

    # A disabled app has no live hooks, so a recorded failure would linger as a
    # stale claim about an app that is no longer wired up at all.
    clear_hook_health(app_name)

    # Clean up cron jobs owned by this app
    permissions = manifest.get("permissions", {})
    if permissions.get("cron"):
        # We need the cron_service — get it from the lifecycle dispatcher
        if _lifecycle_dispatcher and _lifecycle_dispatcher._cron_service:
            cron_service = _lifecycle_dispatcher._cron_service
            sdk = CronSDK(app_name, cron_service)
            # remove_all_async removes every owned job in ONE atomic
            # CronService.remove_jobs transaction (store-lock spin offloaded to
            # a worker thread; timer arming owned by CronService) — all-or-
            # nothing, never a partial removal that orphans still-enabled jobs.
            # A contended store raises CronStoreBusy; REPORT it (rather than
            # crash the disable or claim a false success) so the caller sees the
            # cleanup did not complete and the app's jobs may still be enabled.
            try:
                removed = await sdk.remove_all_async()
                if removed:
                    result["cron_cleanup"] = f"removed {removed} job(s)"
            except CronStoreUnreadable as exc:
                # Sibling class of CronStoreBusy, so it escaped the arm below
                # entirely and would CRASH the disable — the outcome the comment
                # above forbids. Reported rather than retried: an unreadable store
                # does not heal on its own.
                logger.warning(
                    "App %s: cron cleanup could not complete on disable — " "store unreadable: %s",
                    app_name,
                    exc,
                )
                result["cron_cleanup"] = "failed: cron store unreadable — jobs may still be enabled"
                sel().log_api_access(
                    caller="gateway",
                    operation="app_crons_deregister",
                    outcome="failed",
                    resources=app_name,
                    error=str(exc),
                )
            except CronStoreBusy as exc:
                logger.warning(
                    "App %s: cron cleanup could not complete on disable — " "store busy: %s",
                    app_name,
                    exc,
                )
                result["cron_cleanup"] = "failed: cron store busy — jobs may still be enabled"
                sel().log_api_access(
                    caller="gateway",
                    operation="app_crons_deregister",
                    outcome="failed",
                    resources=app_name,
                    error=str(exc),
                )

    # Stop and drop this app's durable job runs. Keyed off the registry, not
    # the manifest grant -- see _cleanup_app_jobs for why that distinction is
    # load-bearing on a revoked grant.
    await _cleanup_app_jobs(app_name, result)

    # Drop the lookup entry unconditionally, for the same reason the cleanup
    # above ignores the grant: a revoked capability must not survive in the
    # registry, and a conditional forget would leave the SDK published for the
    # rest of the gateway's life. The route guard re-reads the manifest so it
    # would refuse anyway, but the registry must not disagree with it.
    forget_sdk(app_name)

    return result


def _teardown_sweep_candidates() -> list[str]:
    """Every app with in-process runtime state this gateway would have to undo.

    Keyed on routes ALONE this would miss an app whose only backend surface is
    ``hooks.on_startup``: it never enters the route registry, so its privileged
    background work would keep running after a CLI disable with nothing looking at
    it. The sources cover the ways such an app is still visible in here -- the
    gateway-owned residue below, plus the off-switch an app registers for work it
    spawned itself.

    Deliberately BROADER than :func:`_gateway_owned_runtime_residue`: an app's own
    hook registries survive a disable by design, so membership here is "worth
    looking at", not "still not torn down".
    """
    from kiro_crew.apps import teardown as teardown_mod

    names = set(_gateway_owned_runtime_residue())
    names.update(teardown_mod.apps_with_in_process_hooks())
    # An unfinished teardown has, by definition, no reliable trace to be found by:
    # a backend still listening on an adopted port leaves every registry clean. So
    # the app has to carry itself forward, or the retry it is owed never happens.
    names.update(_sweep_unfinished)
    return sorted(names)


def _gateway_owned_runtime_residue() -> set[str]:
    """Apps still holding runtime state that ``teardown_app_runtime`` removes.

    This is the sweep's POST-CONDITION, so it must contain only state the teardown
    is responsible for clearing: the route table, a retained startup task, a job
    SDK registration, a recorded hook-wiring failure. The app's own disable and
    slot-close hooks are excluded on purpose -- ``forget_app_hooks`` is
    uninstall-only, so counting them would make every disable look like a teardown
    that never landed.
    """
    names: set[str] = set(_hook_health)
    if _route_registry is not None:
        names.update(_route_registry.get_registered_apps())
    names.update(apps_with_retained_startup_hooks())
    names.update(job_registered_apps())
    return names


async def reconcile_torn_down_apps() -> list[str]:
    """Undo the runtime registration of every app that is no longer enabled on disk.

    ``RouteRegistry`` is an in-memory table in the GATEWAY process, and
    ``deregister_app_routes`` is reached only from ``on_app_disable`` on the
    gateway's own teardown path. ``kirocrew app disable`` and ``kirocrew app
    uninstall`` run in a DIFFERENT process: they write ``installed.json`` (and,
    for uninstall, delete the app), report success, and never reach that registry.
    The app's routes therefore keep dispatching -- and its module stays in
    ``sys.modules``, and its ``on_startup`` task keeps running -- for the rest of
    the gateway's life (#7926).

    This closes it the way the backend-process side already does: re-read the
    authoritative metadata and undo the registration, rather than leave the entry
    in place behind a per-request check. ``backend._set_backend_health`` reads the
    same ``app_enabled_state`` on every health sweep and calls
    ``_drop_disabled_app_resources`` on a confirmed disable, whose docstring
    already records that it is "idempotent with the CLI's own deregistration" --
    that mechanism exists precisely because the CLI is out of process. It only
    ever visits apps with a spawned backend, so an app whose backend surface is
    hooks alone is never swept by it.

    A hidden route is not a removed route, so this REMOVES the registration:
    the routing table entry is popped, the AppContext dropped, the app's modules
    unloaded, its hook health cleared and its job SDK forgotten.

    Runs the WHOLE shared teardown sequence, not the one step that fixes routes.
    ``teardown.teardown_app_runtime`` exists because the revoke path once carried
    a hand-maintained copy of the disable handler's steps, and its docstring calls
    a copy "a defect with a delay on it" -- reaching past it to ``on_app_disable``
    would be a third partial spelling, so a step added there later would silently
    not run on this path. Going through it also reaches the siblings of this same
    root cause: ``notify_app_disabled`` (the in-process off-switch
    ``register_app_disable_hook`` documents as existing for a disable "this
    process never saw"), ``stop_app_backend`` for a child that
    ``on_gateway_shutdown`` notes it would otherwise miss when an app was
    "disabled cross-process, metadata-only", and -- for an app whose ``resources``
    is ``gateway``, which is the default and the only value the gateway owns --
    ``deregister_app``. An ``app``-resources app keeps its own agents, skills and
    MCP servers here, exactly as it does on the dashboard's disable.

    No third-party code is started. That is not hardcoded here: for a record whose
    ``enabled`` is not True and with no backend port observed,
    ``teardown_app_runtime`` computes ``app_may_be_running`` False, which skips the
    app's ``onDisable`` script and passes ``run_app_hooks=False`` so its
    ``on_shutdown`` hook is not invoked either. Deriving it from the record rather
    than asserting it is what keeps an app that IS still live (an observed port)
    on its proper shutdown path. Nothing that STOPS something is skipped.

    Acts only on a CONFIRMED ``False``, read UNDER the app's lifecycle lock.
    ``app_enabled_state`` is tri-state and answers ``None`` when ``installed.json``
    cannot be READ (EMFILE, EIO, a Windows AV lock); tearing down on that would
    take a live app's routes offline over a momentary fault, so an unreadable state
    leaves the registration standing and is retried on the next sweep. A MISSING
    metadata file is a definite ``False`` -- that is the uninstall case.

    The lock is what makes a concurrent RE-ENABLE safe rather than merely unlikely.
    ``handle_enable_app`` writes ``enabled=True`` and registers the app's runtime
    under ``app_lifecycle_lock(name)``, so without holding it this sweep could read
    a stale ``False``, yield at an await, and then tear down a runtime that had just
    been legitimately re-registered -- leaving the app enabled in metadata and
    serving nothing, the mirror of the bug this fixes. Taking the same lock and
    re-reading the state inside it makes that interleaving impossible. The lock
    order matches the handler's (lifecycle lock, then anything below it), so this
    introduces no new ordering.

    That lock is in-process, so it does NOT serialize against a CLI ``app enable``,
    which is a different OS process. The same unsynchronized
    read-then-``deregister_app`` already exists in ``backend._set_backend_health``,
    whose own comment records it ("a disable in the CLI's process can complete in
    between"), and the repository's established answer there is a post-write
    re-verify rather than an interprocess lock. This function does the same, in the
    mirror direction, at the end -- and RESTORES the registrations it removed, using
    ``register_app``, the exact inverse of the step that removed them. Serializing
    the two processes properly means routing CLI lifecycle operations through the
    gateway, which is #7903's scope.

    Returns the app names torn down, for logging and for tests.
    """
    # Function-local: teardown.py imports on_app_disable and stop_app_startup_hooks
    # FROM this module, so a module-level import here is a cycle. Bound as the
    # module, not the name, so `patch("kiro_crew.apps.teardown.teardown_app_runtime")`
    # still reaches this call site.
    from kiro_crew.apps import teardown as teardown_mod

    torn_down: list[str] = []
    for name in _teardown_sweep_candidates():
        async with app_lifecycle_lock(name):
            # Re-read INSIDE the lock. A read taken before acquiring it can be
            # stale by the time the teardown runs.
            state = await asyncio.to_thread(app_enabled_state, name)
            if state is not False:
                _sweep_unfinished.discard(name)
                _sweep_retry_reported.discard(name)
                continue
            # get_app returns None once the app is uninstalled and its manifest is
            # gone. teardown_app_runtime reads the record for the app's onDisable
            # script (skipped on this path), its `enabled` flag (absent reads as
            # "not running", which is what we want) and its `resources` contract.
            # That last one is the only thing an empty record loses -- an
            # `app`-resources app would have its bridges deregistered here -- and it
            # is moot: the CLI's uninstall branch already calls deregister_app
            # unconditionally, so this repeats a removal that has happened rather
            # than causing a new one.
            loaded = await asyncio.to_thread(get_app, name)
            uninstalled = not isinstance(loaded, dict)
            record: dict[str, Any] = (
                {"name": name, "manifest": {}} if uninstalled else dict(loaded or {})
            )
            # WHETHER THERE IS STILL WORK is decided by the metadata GENERATION, not
            # by the app name. A name-only "already swept" marker is unsafe in exactly
            # the direction that matters: it cannot tell this app from one re-enabled
            # and disabled again since, nor from one since UNINSTALLED and now needing
            # its hook registries dropped, and it would skip both. Every one of those
            # transitions rewrites (or removes) installed.json, so the token moves and
            # the sweep runs again.
            #
            # The generation is also why the decision is not derived from residue. An
            # app whose only trace is its own off-switch has no gateway-owned residue
            # to look at, yet firing that off-switch is precisely the work -- once per
            # disable, not once per interval.
            generation = await asyncio.to_thread(
                _metadata_generation, name, uninstalled=uninstalled
            )
            if _sweep_done.get(name) == generation and name not in _sweep_unfinished:
                continue
            try:
                # bounded_startup: this is a BACKGROUND sweep, and the ordinary
                # disable contract waits without a deadline for a detached startup
                # hook to exit. A hook that never terminates would park this loop
                # forever, so every app disabled after it would stay registered and
                # served -- the sweep silently ceasing to exist, which is worse than
                # the bug it fixes. A bounded refusal comes back as a failure and is
                # retried instead.
                #
                # Deliberately NOT a cancel-based timeout around the whole call:
                # teardown.py records that cancelling makes an asyncio wrapper
                # awaiting to_thread look terminal while the worker still runs app
                # code, which sets a process-lifetime residual marker and makes every
                # later cleanup fail closed. Bounding the one unbounded wait is the
                # fix; cancelling the teardown is not.
                result = await teardown_mod.teardown_app_runtime(name, record, bounded_startup=True)
            except Exception as exc:  # noqa: BLE001 - one bad app must not stop the sweep
                _sweep_unfinished.add(name)
                _sweep_done.pop(name, None)
                logger.exception(
                    "App %s: could not undo the runtime registration of a torn-down app",
                    name,
                )
                sel().log_api_access(
                    caller="gateway",
                    operation="app_hooks_teardown_sweep",
                    outcome="failed",
                    resources=name,
                    error=str(exc),
                )
                continue
            for note in result.failures:
                logger.warning("App %s: teardown sweep step did not complete: %s", name, note)
            # BOTH conditions, because they catch different residue. A reported
            # failure is a postcondition teardown_app_runtime itself could not reach
            # -- residual third-party execution, which is the whole point of the sweep
            # -- and a backend still listening on an adopted port produces one while
            # leaving every registry clean, so the registry check alone would read
            # that as complete. The registry check catches the other direction:
            # teardown_app_runtime returns before deregistering anything when a
            # detached startup hook refuses to stop, so "it was called" is not proof
            # the runtime is gone.
            #
            # Warned ONCE per app: an unrecoverable residual would otherwise write the
            # same line every interval for the life of the gateway.
            if result.failures or name in _gateway_owned_runtime_residue():
                _sweep_unfinished.add(name)
                _sweep_done.pop(name, None)
                if name not in _sweep_retry_reported:
                    _sweep_retry_reported.add(name)
                    logger.warning(
                        "App %s was torn down out-of-process but this gateway could "
                        "not finish stopping it; retrying on each sweep",
                        name,
                    )
                continue
            _sweep_unfinished.discard(name)
            _sweep_retry_reported.discard(name)
            _sweep_done[name] = generation
            # VERIFY, because the pre-check could not be atomic with the teardown: an
            # app ENABLE in the CLI's process can land in between, and it registers
            # agents, skills and MCP servers that the deregistration above then
            # removes -- leaving an app that reads enabled with none of its resources.
            # This mirrors backend._set_backend_health, which re-reads the same state
            # after its own write for the mirror-image reason ("a disable in the CLI's
            # process can complete in between").
            #
            # Only a CONFIRMED True acts. Unknown is a momentary read fault, and the
            # pre-check had already confirmed not-enabled.
            #
            # RESTORED, not merely reported. register_app is the exact inverse of the
            # deregister_app this teardown ran: pure resource registration from the
            # installed snapshot, no app hooks and no third-party code (it re-checks
            # the execution policy itself), and it skips a `resources="app"` app under
            # the same condition deregistration did. It cannot loop, either -- once the
            # app reads enabled the pre-check skips it entirely on the next pass.
            if await asyncio.to_thread(app_enabled_state, name) is True:
                _sweep_done.pop(name, None)
                restored = await asyncio.to_thread(register_app, name)
                errors = list(getattr(restored, "errors", None) or [])
                if errors:
                    logger.warning(
                        "App %s was enabled by another process while this gateway was "
                        "tearing it down, and its registrations could not be restored "
                        "(%s). Restart the gateway, or disable and re-enable the app",
                        name,
                        "; ".join(errors),
                    )
                else:
                    logger.warning(
                        "App %s was enabled by another process while this gateway was "
                        "tearing it down; its registrations have been restored. Its "
                        "backend hooks are not loaded until the gateway restarts",
                        name,
                    )
                sel().log_api_access(
                    caller="gateway",
                    operation="app_hooks_teardown_sweep",
                    outcome="raced_enable_restored" if not errors else "raced_enable_failed",
                    resources=name,
                    error="; ".join(errors) if errors else "",
                )
            if uninstalled:
                # UNINSTALL only, matching the asymmetry forget_app_hooks documents:
                # these registries are repopulated by each app's own watchdog, not
                # by the gateway, so clearing them on a disable would leave a window
                # after a re-enable in which a dismissal silently fails to reach a
                # live worker. Nothing re-registers behind an uninstall, and a hook
                # left behind makes the app's leftover tabs undismissable.
                teardown_mod.forget_app_hooks(name)
        torn_down.append(name)
        logger.warning(
            "App %s was disabled or uninstalled by another process; the gateway has "
            "stopped serving it and unloaded its modules",
            name,
        )
        sel().log_api_access(
            caller="gateway",
            operation="app_hooks_teardown_sweep",
            outcome="completed",
            resources=name,
        )
    return torn_down


async def _teardown_sweep_loop(stop: asyncio.Event) -> None:
    """Run :func:`reconcile_torn_down_apps` once per interval until asked to stop.

    Never exits on a failed sweep: the whole point is that the exposure lasts
    until something removes the registration, so a loop that died on a transient
    fault would leave a torn-down app dispatchable for the rest of the gateway's
    life -- exactly the bug it exists to close.

    Exits ONLY from the idle gap. A stop request arriving mid-pass is honoured at
    the end of that pass, because a pass cannot be interrupted safely -- see
    :func:`stop_teardown_sweep`. The idle wait is on the stop event rather than a
    bare sleep so a stop does not have to wait out the whole interval.
    """
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=_TEARDOWN_SWEEP_INTERVAL)
            return  # asked to stop while idle, which is the clean exit
        except asyncio.TimeoutError:
            pass
        try:
            await reconcile_torn_down_apps()
        # No CancelledError arm: it derives from BaseException, so `except
        # Exception` already lets a cancellation out.
        except Exception:  # noqa: BLE001 - the sweep must outlive its own failures
            logger.exception("App hook teardown sweep failed; retrying next interval")


def start_teardown_sweep() -> None:
    """Arm the out-of-process teardown sweep (idempotent)."""
    global _teardown_sweep_task, _teardown_sweep_stop
    if _teardown_sweep_task is not None and not _teardown_sweep_task.done():
        return
    _teardown_sweep_stop = asyncio.Event()
    _teardown_sweep_task = asyncio.create_task(_teardown_sweep_loop(_teardown_sweep_stop))
    logger.info("App hook teardown sweep armed (every %.0fs)", _TEARDOWN_SWEEP_INTERVAL)


async def stop_teardown_sweep() -> None:
    """Ask the sweep to stop and wait for it, so an in-process restart leaks no task.

    NEVER cancels a pass in flight, which is the whole reason this is an event and
    not a ``task.cancel()``. A teardown offloads ``deregister_app`` to the
    subprocess executor, and cancelling the awaiting coroutine does not stop that
    worker thread: the await would return while the thread kept deleting an app's
    agents, skills and MCP servers, and a gateway re-armed behind it could register
    resources the abandoned worker then removes. ``teardown.py`` records the same
    hazard for app code -- a cancelled wrapper looks terminal while the worker runs
    on. So the loop is asked to leave from its idle gap, which it always reaches
    because every pass is bounded (``bounded_startup``).

    If a pass is still running when the budget expires the task is LEFT ALONE
    rather than cancelled: it exits at the end of that pass because the stop event
    is already set, and the alternative is precisely the stranded half-teardown
    above. A newly armed loop can briefly overlap it, which is harmless -- both
    take the same per-app lifecycle lock, so they cannot interleave on one app.
    """
    global _teardown_sweep_task, _teardown_sweep_stop
    task = _teardown_sweep_task
    stop = _teardown_sweep_stop
    _teardown_sweep_task = None
    _teardown_sweep_stop = None
    # All three are memoized judgements about apps in THIS generation's registries.
    # An in-process restart rebuilds those from disk, so carrying them over would let
    # a stale one misjudge an app that has since been re-registered.
    _sweep_done.clear()
    _sweep_unfinished.clear()
    _sweep_retry_reported.clear()
    if task is None or task.done():
        return
    if stop is not None:
        stop.set()
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=_SWEEP_STOP_BUDGET_SECS)
    except asyncio.TimeoutError:
        logger.warning(
            "App hook teardown sweep is mid-pass after %.0fs; leaving it to finish "
            "rather than cancelling a teardown in progress",
            _SWEEP_STOP_BUDGET_SECS,
        )


async def on_gateway_startup(
    *, cron_service: Any = None, broadcast_fn: Any = None, spawn_impl: Any = None
) -> None:
    """Called during gateway startup — register routes then invoke on_startup hooks.

    Order matches on_app_enable: routes first, then startup hooks.
    Should be called after init_hooks_system() and after all apps are registered.
    """
    if not _lifecycle_dispatcher:
        return

    # list_apps() walks the apps dir (two file reads per app) — off the loop.
    installed = await asyncio.to_thread(list_apps)
    enabled = [a for a in installed if a.get("enabled")]
    if not enabled:
        return

    # Step 1 & 2: Share a single AppContext per app for both routes and startup hooks.
    # This ensures health status changes made by the startup hook are visible to
    # route handlers (and vice versa), matching the on_app_enable approach.
    for app_info in sorted(enabled, key=lambda a: a.get("name", "")):
        name = app_info.get("name", "")
        denied = app_execution_denied(
            name,
            action="hook_boot_register",
            app_root=_app_hook_root(name),
            caller="gateway",
        )
        if denied:
            logger.warning(
                "Startup: skipping hooks and crons for denied app %s: %s",
                name,
                denied,
            )
            if cron_service is not None:
                await disarm_app_crons_for_execution(name, cron_service)
            if _route_registry:
                _route_registry.deregister_app_routes(name)
            continue

        # Reconcile app-declared crons into the running scheduler.
        if cron_service is not None:
            try:
                # Async register: awaits the CronSDK mutation API (bounded
                # store-lock spin offloaded to a worker thread), so the gateway
                # loop is never parked; timer arming is owned by CronService.
                registered = await register_app_crons_with_service(name, cron_service)
                if registered:
                    logger.info(
                        "Startup: registered %d cron(s) for app %s: %s",
                        len(registered),
                        name,
                        ", ".join(registered),
                    )
                sel().log_api_access(
                    caller="gateway",
                    operation="app_crons_register",
                    outcome="completed",
                    resources=f"app={name} crons={registered}",
                )
            except Exception as exc:
                logger.exception("Startup: cron registration failed for %s", name)
                sel().log_api_access(
                    caller="gateway",
                    operation="app_crons_register",
                    outcome="failed",
                    resources=name,
                    error=str(exc),
                )

        manifest = app_info.get("manifest", {})
        hooks = manifest.get("backend", {}).get("hooks", {})
        if not hooks:
            continue

        # Same reason as the enable path: a runtime is about to exist again.
        forget_sweep_state(name, install_id=str(app_info.get("installedAt") or ""))

        ctx = _build_app_context_from_info(app_info, cron_service, broadcast_fn, spawn_impl)

        # Register routes (if declared)
        routes_hook = hooks.get("routes", "")
        if routes_hook and _route_registry:
            await _route_registry.register_app_routes(name, _app_hook_root(name), routes_hook, ctx)

        # Invoke on_startup hook (if declared)
        startup_hook = hooks.get("on_startup", "")
        if startup_hook and _lifecycle_dispatcher:
            success = await _lifecycle_dispatcher._invoke(name, startup_hook, ctx, phase="startup")
            if success:
                logger.info("Startup hook invoked for: %s", name)

        # Same publication as on_app_enable: the reason a hook failed must outlive
        # the context, or boot is the one path where it is collected and dropped.
        # No log line here on purpose: every site that marks the context degraded
        # already logs at ERROR itself (route_registry.py:142,151,159 and
        # lifecycle.py:352,396, plus the cancelled site via
        # _mark_cancelled_startup_residual at lifecycle.py:66), so an aggregate
        # would only re-log them, and only ever for this one caller.
        _publish_hook_health(name, ctx)

    # AFTER the loop, deliberately: only here has every enabled app registered
    # its runners, so a run whose kind has no runner can be told apart from an
    # app that has simply not loaded yet. Reconciliation resolves runs that a
    # previous gateway process left mid-flight -- a run must never be left
    # `running` forever, and must never silently vanish.
    try:
        interrupted = await asyncio.to_thread(reconcile_all)
        if interrupted:
            logger.info("Startup: reconciled %d interrupted job run(s)", interrupted)
            sel().log_api_access(
                caller="gateway",
                operation="jobs.reconcile",
                outcome="completed",
                resources=f"interrupted={interrupted}",
            )
    except Exception as exc:  # noqa: BLE001 - boot must not fail on a bad run store
        logger.exception("Startup: job reconciliation failed")
        sel().log_api_access(
            caller="gateway",
            operation="jobs.reconcile",
            outcome="failed",
            resources="startup",
            error=str(exc),
        )


#: One shared deadline for the whole backend-stop sweep at gateway shutdown.
#: The sweep runs inside the gateway's cooperative shutdown, which
#: ``GRACEFUL_SHUTDOWN_SECS`` caps at 10s before the supervisor force-exits;
#: each individual stop can spend up to the 5s SIGTERM grace before its SIGKILL
#: escalation, so the stops run concurrently and this bound keeps the sweep as
#: a whole from consuming the budget the rest of cleanup still needs.
_BACKEND_STOP_BUDGET_SECS = 6.0


async def on_gateway_shutdown() -> None:
    """Called during gateway shutdown — invoke on_shutdown hooks for enabled
    apps, then stop the backend processes this gateway spawned.

    The backend stop is the load-bearing half. Spawned backends are child
    processes of the gateway: without an explicit stop they reparent to PID 1
    when the gateway exits and keep listening on their ports, so anything
    probing those ports reads a stopped gateway as "connected". The next boot
    reaps them only lazily (``_reap_stale_app_backends``), which does not help
    a gateway that stays down. Hooks run FIRST so an app's ``on_shutdown``
    still has its own backend alive.

    Stop targets come from the runtime tracking table
    (:func:`spawned_backend_names`), never from persisted ``enabled`` metadata:
    the metadata filter is wrong in both directions here (it would signal an
    ADOPTED externally-managed backend whose contract is to survive gateway
    exit, and it would miss a still-running child whose app was disabled
    cross-process, metadata-only). It also keeps :func:`stop_app_backend` — and
    its pidfile-record erasure — away from apps with nothing running, so a
    retained prior-generation orphan record stays recoverable by the next
    boot's stale-reap.
    """
    # list_apps() walks the apps dir (two file reads per app) — off the loop.
    installed = await asyncio.to_thread(list_apps)
    enabled = [a for a in installed if a.get("enabled")]

    try:
        if _lifecycle_dispatcher and enabled:
            invoked = await _lifecycle_dispatcher.dispatch_shutdown(enabled)
            if invoked:
                logger.info("Shutdown hooks invoked for: %s", ", ".join(invoked))
    finally:
        # The sweep MUST run even when hook dispatch hangs or raises: hooks are
        # third-party code, and dispatch awaits an invoked on_shutdown hook to
        # completion — so a wedged hook would hold this coroutine until the
        # gateway's graceful-shutdown deadline CANCELS it right here. Running
        # the sweep from the finally block means that cancellation still stops
        # the spawned backends (a task may keep awaiting during cleanup after
        # a single cancel request), instead of force-exiting with every one of
        # them orphaned — the exact defect this function exists to fix.
        await _stop_spawned_backends()


async def _stop_spawned_backends() -> None:
    """Stop every backend this gateway spawned, concurrently, under one budget.

    Deliberately NOT gated on ``_lifecycle_dispatcher`` or on the enabled list:
    backends are started by the boot path independently of the hooks system, so
    neither absence may leave them orphaned. The tracking-table read takes the
    module lock — off the loop like every other blocking step.
    """
    names = await asyncio.to_thread(spawned_backend_names)
    if not names:
        return

    # Each stop signals a process group and waits on it — blocking syscalls,
    # so offloaded to the subprocess executor. All stops start CONCURRENTLY
    # under ONE shared deadline: awaiting them serially would multiply the
    # per-app SIGTERM grace by the number of apps and blow the gateway's
    # cooperative shutdown budget, so the supervisor's force-exit would orphan
    # every backend the sweep had not reached yet.
    loop = asyncio.get_running_loop()
    stops = [loop.run_in_executor(subprocess_executor(), stop_app_backend, name) for name in names]
    gathered = asyncio.gather(*stops, return_exceptions=True)
    try:
        # The gather is SHIELDED so a deadline overrun (or a cancellation of
        # this coroutine) releases the caller WITHOUT cancelling the stops.
        # The executor is shared with the rest of shutdown, so a stop can
        # still be QUEUED when the deadline fires — cancelling it then would
        # drop it before stop_app_backend ever ran, and that backend would
        # never be signalled at all. A running stop's thread cannot be
        # interrupted anyway; the shield extends the same guarantee to the
        # queued ones, which keep running in the background.
        results = await asyncio.wait_for(
            asyncio.shield(gathered),
            timeout=_BACKEND_STOP_BUDGET_SECS,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "app backend stop sweep did not finish within %.0fs at gateway "
            "shutdown; unfinished stops continue in the background: %s",
            _BACKEND_STOP_BUDGET_SECS,
            ", ".join(names),
        )
        return
    for name, result in zip(names, results):
        if isinstance(result, BaseException):
            logger.warning(
                "stopping backend for app %r on gateway shutdown failed: %s",
                name,
                result,
            )
        elif result:
            logger.info("Stopped app backend on gateway shutdown: %s", name)
