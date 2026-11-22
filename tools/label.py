"""Labels patches the way the organisers describe: apply each to the pinned
checkout, run the whole suite, record what fails. Local evaluation only; its
output never goes into the image or the map.

    python label.py <dir with commits/ and tests.json> [jobs]

Writes <dir>/results/<id>.json per commit (reused on reruns) and
<dir>/answers.json in the kit's format.
"""

import glob
import json
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import selector  # noqa: E402

SRC = os.environ.get("BR_ROOT", "/src")
TMP = os.environ.get("BR_LABTMP", "/tmp/label")


def apply(root, text):
    for entry in selector.parse_patch(text):
        old, new = entry["old"], entry["new"]
        lines = []
        if old and not entry["made"]:
            with open(os.path.join(root, old), encoding="utf-8", newline="") as handle:
                lines = handle.read().split("\n")
            os.remove(os.path.join(root, old))  # break the hard link
        if entry["gone"] or not new:
            continue
        out, cursor = [], 0
        for after, removed, added in entry["blocks"]:
            out.extend(lines[cursor:after])
            out.extend(added)
            cursor = after + len(removed)
        out.extend(lines[cursor:])
        os.makedirs(os.path.dirname(os.path.join(root, new)) or root, exist_ok=True)
        with open(os.path.join(root, new), "w", encoding="utf-8", newline="") as handle:
            handle.write("\n".join(out))


def label(job):
    name, patch, result, delay = job
    if os.path.exists(result):
        return name
    time.sleep(delay)  # stagger the first wave: the memory-hungry tests would align
    root = os.path.join(TMP, name)
    shutil.rmtree(root, ignore_errors=True)
    os.makedirs(root)
    for part in ("sqlglot", "tests"):
        subprocess.run(["cp", "-al", os.path.join(SRC, part), os.path.join(root, part)], check=True)
    for extra in ("setup.cfg", "pyproject.toml", "setup.py", "sqlglotc"):
        if os.path.exists(os.path.join(SRC, extra)):
            subprocess.run(["cp", "-al", os.path.join(SRC, extra), os.path.join(root, extra)], check=True)
    try:
        with open(patch, "rb") as handle:
            apply(root, handle.read().decode("utf-8", "surrogateescape"))
        env = dict(os.environ)
        env.update(
            SKIP_INTEGRATION="0",
            PYTHONPATH=root + os.pathsep + HERE,
            PYTHONDONTWRITEBYTECODE="1",
            PYTHON_CPU_COUNT="2",
            BR_RESULT=result,
        )
        env.pop("BR_TEST", None)
        command = [sys.executable, "-m", "pytest", "tests", "-q", "-p", "brlabel"]
        command += ["-p", "no:cacheprovider", "--timeout=300"]
        with open(result + ".log", "wb") as log:
            subprocess.run(command, cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=2400)
    except Exception as error:  # noqa: BLE001
        print("FAILED TO LABEL", name, repr(error), file=sys.stderr, flush=True)
    shutil.rmtree(root, ignore_errors=True)
    print("labelled", name, "ok" if os.path.exists(result) else "NO RESULT", file=sys.stderr, flush=True)
    return name


def main():
    base = sys.argv[1]
    jobs = int(sys.argv[2]) if len(sys.argv) > 2 else 12
    with open(os.path.join(base, "tests.json"), encoding="utf-8") as handle:
        tests = json.load(handle)["tests"]
    by_id = {test["id"]: test["number"] for test in tests}
    by_file = {}
    for test in tests:
        by_file.setdefault(test["file"], []).append(test["number"])
    os.makedirs(os.path.join(base, "results"), exist_ok=True)
    work = []
    pending = 0
    for patch in sorted(glob.glob(os.path.join(base, "commits", "*.patch"))):
        name = os.path.basename(patch)[:-6]
        result = os.path.join(base, "results", name + ".json")
        if not os.path.exists(result):
            pending += 1
        work.append((name, patch, result, 10 * pending if pending <= jobs else 0))
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        list(pool.map(label, work))
    kinds = {}
    if os.path.exists(os.path.join(base, "kinds.json")):
        with open(os.path.join(base, "kinds.json"), encoding="utf-8") as handle:
            kinds = json.load(handle)
    commits = {}
    for name, _, result, _ in work:
        if not os.path.exists(result):
            continue
        with open(result, encoding="utf-8") as handle:
            record = json.load(handle)
        failing = {by_id[node] for node in record["failed"] if node in by_id}
        for node in record["collect"]:
            failing.update(by_file.get(node, []))
            if not node.endswith(".py"):  # a package or the session failed to collect
                failing.update(n for f, ns in by_file.items() if f.startswith(node) for n in ns)
        commits[name] = {"failing": sorted(failing), "kind": kinds.get(name, "code")}
    with open(os.path.join(base, "answers.json"), "w", encoding="utf-8") as handle:
        json.dump({"commits": commits}, handle)
    print(len(commits), "of", len(work), "labelled", file=sys.stderr)


if __name__ == "__main__":
    main()
