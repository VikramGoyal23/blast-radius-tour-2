"""Runs at the start of every Python process while the map is being built.
Processes started by a test (BR_TEST is set) trace themselves."""

import os

if os.environ.get("BR_TEST") and os.environ.get("BR_OUT"):
    try:
        import brcore

        brcore.worker_main()
    except Exception:  # noqa: BLE001
        pass
