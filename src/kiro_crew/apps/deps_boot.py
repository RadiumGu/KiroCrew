"""Launch shim for app processes with gateway-provisioned dependencies.

Usage (always via the gateway's own interpreter)::

    python -m kiro_crew.apps.deps_boot <deps_dir> <script.py> [args...]
    python -m kiro_crew.apps.deps_boot <deps_dir> -m <module> [args...]
    python -m kiro_crew.apps.deps_boot <deps_dir> -c <code> [args...]

Why this exists: provisioned dependencies are a ``pip install --target``
tree, and the naive transport — prepending the tree to ``PYTHONPATH`` —
never processes ``.pth`` files, because ``PYTHONPATH`` entries are not site
directories. Packages that ship a ``.pth`` (editable installs, namespace
shims, import hooks) then install "successfully" and crash at import time.
``site.addsitedir`` IS the .pth-processing registration, so the shim runs it
on the deps dir and only then hands control to the real entry point.

``addsitedir`` appends; the app's pinned requirements must win over the
gateway environment's own packages, so the newly added entries are moved to
the FRONT of ``sys.path`` (matching the precedence the PYTHONPATH transport
had). The shim adds no other behavior: argv is rewritten so the target sees
exactly the argv it would have seen launched directly.
"""

from __future__ import annotations

import os
import runpy
import site
import sys


def main(argv: list[str]) -> None:
    # Path-launch support (`python -S /abs/.../deps_boot.py ...`, used when
    # import-machinery flags forbid the -m spelling): CPython puts the
    # SCRIPT's own directory at sys.path[0], which here is kiro_crew/apps/ —
    # leaving it would let an app import gateway modules by their bare names
    # (`import interpreter`). Drop it before anything else resolves.
    _own = os.path.dirname(os.path.abspath(__file__))
    if sys.path and os.path.abspath(sys.path[0] or os.curdir) == _own:
        del sys.path[0]
    if len(argv) < 2 or (argv[1] in ("-m", "-c") and len(argv) < 3):
        sys.stderr.write(
            "usage: python -m kiro_crew.apps.deps_boot <deps_dir> "
            "(<script.py> | -m <module> | -c <code>) [args...]\n"
        )
        raise SystemExit(2)
    deps_dir = argv[0]
    before = len(sys.path)
    site.addsitedir(deps_dir)
    added = sys.path[before:]
    del sys.path[before:]
    sys.path[:0] = added
    if argv[1] == "-m":
        module, rest = argv[2], argv[3:]
        sys.argv = [module, *rest]
        runpy.run_module(module, run_name="__main__", alter_sys=True)
    elif argv[1] == "-c":
        code, rest = argv[2], argv[3:]
        # `python -c` parity: sys.argv[0] is the literal "-c" and the
        # current directory sits at sys.path[0]. Insert it AHEAD of the
        # deps entries — exactly where the plain launch puts it.
        sys.argv = ["-c", *rest]
        sys.path.insert(0, "")
        exec(compile(code, "<string>", "exec"), {"__name__": "__main__"})  # noqa: S102
    else:
        target, rest = argv[1], argv[2:]
        sys.argv = [target, *rest]
        # Direct-script parity: `python script.py` puts the script's own
        # directory at sys.path[0]; runpy.run_path does NOT, so a script
        # importing a sibling module would break under the shim. Insert it
        # ahead of the deps entries — exactly where the plain launch puts it.
        sys.path.insert(0, os.path.dirname(os.path.abspath(target)))
        runpy.run_path(target, run_name="__main__")


if __name__ == "__main__":
    main(sys.argv[1:])
