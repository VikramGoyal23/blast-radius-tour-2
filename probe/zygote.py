"""A warm process that test runs are forked from.

Every test run used to start a new interpreter and import pandas, duckdb and
numpy again, about two thirds of a CPU second each time. This process imports
them once and forks a child per run. sqlglot and its tests are deliberately
NOT imported here: each child imports them fresh, with the patched modules in
place, exactly as a new interpreter would.

Protocol, one request per line on stdin:
    {"patch": <BR_PATCH json>, "tests": <path>, "report": <path>}
and two lines back on stdout:
    pid <child pid>
    exit <run_probe exit code> <CPU seconds the child and its children used>
"""

import io  # noqa: F401
import json
import os
import sys
import unittest  # noqa: F401

try:
    import numpy  # noqa: F401
    import pandas  # noqa: F401
    import duckdb  # noqa: F401
except Exception:  # noqa: BLE001
    pass

import run_probe
import sitecustomize


def say(text):
    os.write(1, (text + "\n").encode())


def serve():
    for line in sys.stdin:
        request = json.loads(line)
        pid = os.fork()
        if pid == 0:
            code = 3
            try:
                os.setsid()  # its own group: a timeout kills it and what it spawned
                null = os.open(os.devnull, os.O_RDWR)
                for fd in (0, 1, 2):
                    os.dup2(null, fd)
                os.environ["BR_PATCH"] = request["patch"]
                os.environ["BR_REPORT"] = request["report"]
                sitecustomize.install()
                sys.argv = ["run_probe.py", request["tests"]]
                code = run_probe.main()
            except BaseException:  # noqa: BLE001
                code = 3
            finally:
                os._exit(code)
        say("pid %d" % pid)
        _, status, usage = os.wait4(pid, 0)
        say("exit %d %.3f" % (os.waitstatus_to_exitcode(status), usage.ru_utime + usage.ru_stime))


if __name__ == "__main__":
    serve()
