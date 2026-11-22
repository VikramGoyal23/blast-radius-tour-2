"""Loaded at the start of every Python process of a test run (the run itself
and any process a test spawns). Serves the patched source for the modules
named in BR_PATCH in place of the pinned checkout's."""

import json
import os
import sys


def install():
    """Puts the modules named in BR_PATCH ahead of the checkout's own."""
    import importlib.abc
    import importlib.util

    modules = json.loads(os.environ["BR_PATCH"])

    class Patched(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            entry = modules.get(fullname)
            if entry is None:
                return None
            source, package_dir = entry
            if os.path.basename(source) == "__init__.py":
                return importlib.util.spec_from_file_location(
                    fullname, source, submodule_search_locations=[package_dir]
                )
            return importlib.util.spec_from_file_location(fullname, source)

    sys.meta_path.insert(0, Patched())


if os.environ.get("BR_PATCH"):
    install()
