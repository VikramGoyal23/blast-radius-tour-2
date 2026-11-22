"""Runs the tests listed in a JSON file (pytest-style ids) against the patched
checkout and writes {"failed": [ids]} to BR_REPORT.

Exit 0: every test passed and every patched module was really the one loaded.
Exit 1: some tests failed or errored; the report names them.
Exit 2: the run proves nothing (a test could not be loaded or attributed, a
patched module was never imported, or it came from the wrong file).
Anything but 0 or 1 makes the selector keep its full selection.
"""

import io
import json
import os
import sys
import unittest


def main():
    with open(sys.argv[1], encoding="utf-8") as handle:
        idents = json.load(handle)
    names = {}
    for ident in idents:
        path, cls, method = ident.split("::")
        names[path[:-3].replace("/", ".") + "." + cls + "." + method] = ident
    suite = unittest.TestLoader().loadTestsFromNames(list(names))
    result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0, failfast=True).run(suite)
    failed = set()
    for case, _ in result.failures + result.errors:
        case = getattr(case, "test_case", case)  # a failing subtest fails its test
        ident = names.get(case.id()) if hasattr(case, "id") else None
        if ident is None:
            return 2  # a class or module level error: cannot say which tests
        failed.add(ident)
    if failed:  # stopped at the first failure: nothing more to learn
        with open(os.environ["BR_REPORT"], "w", encoding="utf-8") as handle:
            json.dump({"failed": sorted(failed)}, handle)
        return 1
    if result.unexpectedSuccesses or result.testsRun < len(names):
        return 2
    for module, (source, _) in json.loads(os.environ["BR_PATCH"]).items():
        loaded = sys.modules.get(module)
        if loaded is None or os.path.realpath(loaded.__file__) != os.path.realpath(source):
            return 2
    with open(os.environ["BR_REPORT"], "w", encoding="utf-8") as handle:
        json.dump({"failed": sorted(failed)}, handle)
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        code = main()
    except BaseException:  # noqa: BLE001
        code = 3
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)
