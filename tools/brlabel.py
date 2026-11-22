"""pytest plugin for tools/label.py: writes which tests failed to BR_RESULT."""

import json
import os

FAILED = set()
COLLECT = set()


def pytest_runtest_logreport(report):
    if report.failed:
        FAILED.add(report.nodeid)


def pytest_collectreport(report):
    if report.failed:
        COLLECT.add(report.nodeid)


def pytest_sessionfinish(session, exitstatus):
    path = os.environ["BR_RESULT"]
    with open(path + ".tmp", "w", encoding="utf-8") as handle:
        json.dump({"failed": sorted(FAILED), "collect": sorted(COLLECT), "exit": int(exitstatus)}, handle)
    os.replace(path + ".tmp", path)
