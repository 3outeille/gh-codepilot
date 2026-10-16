# /// script
# dependencies = ["matplotlib", "numpy"]
# ///
"""A worked example of the contract the Tests tab reads.

Nothing here is a framework — it is a plain script you could run by hand. The
Tests tab only asks three things of it, and each one buys something:

  BENCH_PLAN      a dict stating what the run is for. Shown as "what the
                  benchmark says it does". It is a claim, and the tab audits
                  the source separately, so state only what the code does.
  JSON_BEGIN/END  one JSON object printed between those bare lines. Every
                  value that is a list of flat dicts becomes a chart: columns
                  that repeat across rows are read as swept parameters, the
                  near-unique ones as measurements.
  savefig("x")    plain relative paths. The run's working directory is
                  collected and uploaded, so no upload code belongs here.

The source audit looks for a warmup, a repetition count, a monotonic timer, a
seed, `torch.cuda.synchronize` around GPU timing, and an `allclose` between the
arms being compared. This example deliberately fails that last one — it times
three different operations rather than three implementations of one, so there
is nothing to compare. A real A/B benchmark should assert the arms agree.
"""
import json
import time

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

BENCH_PLAN = {
    "question": "how do numpy reductions scale with array size and dtype on this box?",
    "arms": ["sum", "sort", "cumsum"],
    "sweeps": {"n": [4096, 16384, 65536, 262144], "dtype": ["float32", "float64"]},
    "metrics": {"sum_ms": "ms", "sort_ms": "ms", "cumsum_ms": "ms"},
    "method": {"warmup": 1, "reps": "300 / 30 / 80", "timer": "perf_counter", "seed": 0},
    "expect": "sort superlinear in n; sum and cumsum linear; float64 about 2x float32",
}

rng = np.random.default_rng(0)

WARMUP = 1


def timeit(f, reps):
    for _ in range(WARMUP):
        f()
    t0 = time.perf_counter()
    for _ in range(reps):
        f()
    return (time.perf_counter() - t0) / reps * 1e3


rows = []
for n in (4096, 16384, 65536, 262144):
    for dtype in ("float32", "float64"):
        x = rng.standard_normal(n).astype(dtype)
        row = {"n": n, "dtype": dtype,
               "sum_ms": timeit(lambda: x.sum(), 300),
               "sort_ms": timeit(lambda: np.sort(x), 30),
               "cumsum_ms": timeit(lambda: np.cumsum(x), 80)}
        rows.append(row)
        print(f"n={n:>7} {dtype}  sum={row['sum_ms']:.4f}  sort={row['sort_ms']:.4f}  cumsum={row['cumsum_ms']:.4f}")

print("JSON_BEGIN")
print(json.dumps({"host": "hf-job", "numpy": np.__version__, "results": rows}))
print("JSON_END")

fig, ax = plt.subplots(figsize=(5, 3.2), dpi=140)
for dt in ("float32", "float64"):
    sub = [r for r in rows if r["dtype"] == dt]
    ax.plot([r["n"] for r in sub], [r["sort_ms"] for r in sub], marker="o", label=f"sort {dt}")
ax.set_xscale("log", base=2)
ax.set_yscale("log")
ax.set_xlabel("n")
ax.set_ylabel("ms")
ax.set_title("sort time vs n")
ax.legend()
fig.tight_layout()
fig.savefig("sort_vs_n.png")
print("saved sort_vs_n.png")
