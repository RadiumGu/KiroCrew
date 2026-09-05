"""Interleaved A/B timing of _nested_shell_payloads: main vs branch.

Runs each of the 3 failing CI shapes in fresh subprocesses, alternating
main/branch per round so runner drift hits both arms equally. Prints a
per-shape median table and the branch/main ratio.
"""

import json
import statistics
import subprocess
import sys
import tempfile

MAIN_SRC, BRANCH_SRC = sys.argv[1], sys.argv[2]

WORKER = r"""
import json
import sys
import time

sys.path.insert(0, sys.argv[1])
from kiro_crew.security import _nested_shell_payloads

shapes = {
    "eval-join@16000": (["eval", "a", "b"] * 16000),
    "interp-run@16000": (["bash", "x"] * 16000),
    "dash-run@8000": (["$0"] * 8000 + ["-c"] + ["--"] * 8000),
}
out = {}
for label, tokens in shapes.items():
    _nested_shell_payloads(list(tokens[:1500]))  # warm
    best = min(
        (lambda t0=time.perf_counter(): (_nested_shell_payloads(list(tokens)), time.perf_counter() - t0)[1])()
        for _ in range(3)
    )
    out[label] = best
print(json.dumps(out))
"""

with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
    f.write(WORKER)
    worker_path = f.name

results = {"main": {}, "branch": {}}
ROUNDS = 10
for i in range(ROUNDS):
    for arm, src in (("main", MAIN_SRC), ("branch", BRANCH_SRC)):
        raw = subprocess.run(
            [sys.executable, worker_path, src], capture_output=True, text=True, check=True
        ).stdout
        for label, secs in json.loads(raw).items():
            results[arm].setdefault(label, []).append(secs)

print(f"{'shape':22s} {'main med':>10s} {'branch med':>10s} {'ratio':>7s}")
worst = 0.0
for label in results["main"]:
    m = statistics.median(results["main"][label])
    b = statistics.median(results["branch"][label])
    r = b / m if m else float("inf")
    worst = max(worst, r)
    print(f"{label:22s} {m:10.4f} {b:10.4f} {r:7.3f}")

print(f"WORST-RATIO {worst:.3f}")
