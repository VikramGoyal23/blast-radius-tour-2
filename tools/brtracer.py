"""pytest plugin that records what each test depends on, at the pinned commit.

Used only while building the map (see build_map.py), never at scoring time.

One pytest process is started per test file. The parent only imports and
collects; every test then runs in its own forked child, so each test starts
from the same cold state (no caches warmed by an earlier test). Processes a
test spawns (sqlglot's optimizer and executor tests use spawned process pools)
trace themselves through sitecustomize.py and are folded into that test.

The parent's own record (imports and collection) is written to parent.json and
belongs to every test of that file.
"""

import os
import sys

import pytest

import brcore
from brcore import OUT, _write, snapshot

MAIN_PID = os.getpid()
brcore.install()

_counter = [0]


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_protocol(item, nextitem):
    index = _counter[0]
    _counter[0] += 1
    name = "t%05d.json" % index
    sys.stdout.flush()
    sys.stderr.flush()
    pid = os.fork()
    if pid == 0:
        code = 1
        try:
            tag = "%05d" % index
            os.environ["BR_TEST"] = tag
            brcore.reset()
            from _pytest.runner import runtestprotocol

            reports = runtestprotocol(item, nextitem=None, log=False)
            payload = snapshot()
            brcore.merge_workers(payload, tag)
            payload["id"] = item.nodeid
            payload["failed"] = any(report.failed for report in reports)
            _write(name, payload)
            code = 0
        except BaseException as error:  # noqa: BLE001
            try:
                _write(name, {"id": item.nodeid, "crashed": repr(error)})
            except BaseException:  # noqa: BLE001
                pass
        finally:
            os._exit(code)
    _, status = os.waitpid(pid, 0)
    if status != 0 or not os.path.exists(os.path.join(OUT, name)):
        _write(name, {"id": item.nodeid, "crashed": "exit status %s" % status})
    return True


def pytest_sessionfinish(session, exitstatus):
    if os.getpid() != MAIN_PID:
        return
    payload = snapshot()
    payload["tests"] = _counter[0]
    _write("parent.json", payload)
