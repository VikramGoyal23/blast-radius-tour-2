"""Loaded at the start of every Python process of a probe run (the probe
itself and any worker process a test spawns). Serves the patched source for
the modules named in BR_PATCH in place of the pinned checkout's."""

import json
import os
import sys

_PATCH = os.environ.get("BR_PATCH")

if _PATCH:
    import importlib.abc
    import importlib.util

    _MODULES = json.loads(_PATCH)

    class _Patched(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            entry = _MODULES.get(fullname)
            if entry is None:
                return None
            source, package_dir = entry
            if os.path.basename(source) == "__init__.py":
                return importlib.util.spec_from_file_location(
                    fullname, source, submodule_search_locations=[package_dir]
                )
            return importlib.util.spec_from_file_location(fullname, source)

    sys.meta_path.insert(0, _Patched())
