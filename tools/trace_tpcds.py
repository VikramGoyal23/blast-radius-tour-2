"""Builds map_q/: which lines each query of test_execute_tpcds executes.

    python trace_tpcds.py            trace everything, then build the map
    python trace_tpcds.py <index>    trace one query (used by the above)
    python trace_tpcds.py common     trace what happens before any query

Each query is traced in a fresh process, after the test's own set-up, with
probe/replay_tpcds.py doing exactly what the selector will later do. The
result has the same shape as the main map, with pseudo-tests in place of
tests: "common" (imports and set-up, shared by every query) and "tpcds::<i>".
Built from the pinned commit only.
"""

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.realpath(os.environ.get("BR_ROOT", "/src"))
PROBE = os.path.join(os.path.dirname(HERE), "probe")
WORK = os.environ.get("BR_QWORK", "/tmp/brqwork")
OUT = os.path.join(WORK, "tests__test_executor.py")
MAP = os.environ.get("BR_QMAP", "/out")


def cpu():
    times = os.times()
    return times[0] + times[1] + times[2] + times[3]


def trace_one(which):
    os.environ.update(BR_ROOT=ROOT, BR_OUT=OUT, SKIP_INTEGRATION="0")
    sys.path[:0] = [ROOT, PROBE, HERE]
    os.chdir(ROOT)
    import brcore

    brcore.install()
    import replay_tpcds

    module, case = replay_tpcds.prepare()
    if which == "common":
        payload = brcore.snapshot()
        payload.update(id="common", failed=False, queries=len(case.tpcds_sqls))
        brcore._write("t00000.json", payload)
        return
    index = int(which)
    brcore.reset()
    started = cpu()
    failed = False
    try:
        replay_tpcds.replay(module, case, index)
    except BaseException:  # noqa: BLE001
        failed = True
    spent = cpu() - started
    payload = brcore.snapshot()
    payload.update(id="tpcds::%d" % index, failed=failed, cpu=round(spent, 3))
    brcore._write("t%05d.json" % (index + 1), payload)


def child(which):
    result = subprocess.run([sys.executable, os.path.abspath(__file__), str(which)], capture_output=True)
    if result.returncode:
        print("FAILED to trace", which, result.stderr.decode("utf-8", "replace")[-400:], file=sys.stderr)


def main():
    os.makedirs(OUT, exist_ok=True)
    child("common")
    with open(os.path.join(OUT, "t00000.json"), encoding="utf-8") as handle:
        count = json.load(handle)["queries"]
    with ThreadPoolExecutor(max_workers=int(os.environ.get("BR_JOBS") or 8)) as pool:
        list(pool.map(child, range(count)))
    with open(os.path.join(OUT, "parent.json"), "w", encoding="utf-8") as handle:
        json.dump({"lines": {}, "classes": [], "opens": [], "dirs": [], "tests": count + 1}, handle)
    costs = {}
    for index in range(count):
        with open(os.path.join(OUT, "t%05d.json" % (index + 1)), encoding="utf-8") as handle:
            record = json.load(handle)
        if record["failed"]:
            print("query", index, "FAILS at the pin", file=sys.stderr)
        costs[str(index)] = record["cpu"]
    os.environ.update(BR_WORK=WORK, BR_MAP=MAP, BR_SKIP_TRACE="1")
    sys.path.insert(0, HERE)
    import build_map

    build_map.main()
    with open(os.path.join(MAP, "costs.json"), "w", encoding="utf-8") as handle:
        json.dump(costs, handle)
    print("per-query map written to", MAP, "- total replay cpu", round(sum(costs.values()), 1), file=sys.stderr)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        trace_one(sys.argv[1])
    else:
        main()
