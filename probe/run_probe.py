"""Runs the named tests (pytest-style ids) against the patched checkout.

Exit 0: every test passed and every patched module was really the one loaded.
Exit 1: a test failed or errored. Exit 2: the run proves nothing (a patched
module was never imported, or came from the wrong file). Anything but 0 makes
the selector keep its full selection.
"""

import io
import json
import os
import sys
import unittest


def main():
    names = []
    for ident in sys.argv[1:]:
        path, cls, method = ident.split("::")
        names.append(path[:-3].replace("/", ".") + "." + cls + "." + method)
    suite = unittest.TestLoader().loadTestsFromNames(names)
    result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(suite)
    if not result.wasSuccessful() or result.testsRun < len(names):
        for case, _ in (result.failures + result.errors)[:3]:
            print("PROBE FAILED:", case, file=sys.stderr)
        return 1
    for module, (source, _) in json.loads(os.environ["BR_PATCH"]).items():
        loaded = sys.modules.get(module)
        if loaded is None or os.path.realpath(loaded.__file__) != os.path.realpath(source):
            return 2
    return 0


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)
