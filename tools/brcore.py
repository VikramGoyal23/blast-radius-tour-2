"""Shared tracing core: records executed lines, instantiated classes and
opened files of the checkout for the current process. Used by the pytest
plugin (brtracer) and by worker processes that tests spawn (sitecustomize)."""

import glob
import json
import os
import sys


ROOT = os.path.realpath(os.environ.get("BR_ROOT", os.getcwd())) + os.sep
OUT = os.environ["BR_OUT"]

mon = sys.monitoring
TOOL = 3
DISABLE = mon.DISABLE

LINES = set()
TYPES = set()
OPENS = set()
DIRS = set()
_REL = {}


def rel(filename):
    try:
        return _REL[filename]
    except KeyError:
        pass
    result = None
    try:
        if filename and filename[0] != "<":
            path = os.path.realpath(filename)
            if path.startswith(ROOT) and "__pycache__" not in path:
                result = path[len(ROOT) :].replace(os.sep, "/")
    except Exception:
        result = None
    _REL[filename] = result
    return result


def _on_line(code, line):
    try:
        name = rel(code.co_filename)
        if name is not None:
            LINES.add((name, line))
    except Exception:
        pass
    return DISABLE


def _on_start(code, offset):
    try:
        if code.co_name == "__init__" and code.co_argcount and rel(code.co_filename) is not None:
            frame = sys._getframe(1)
            if frame.f_code is code:
                TYPES.add(type(frame.f_locals[code.co_varnames[0]]))
            return None
    except Exception:
        pass
    return DISABLE


_SKIP_OPENERS = ("linecache.py", "tokenize.py")
_WRAPPERS = ("gzip.py", "codecs.py", "_compression.py", "bz2.py", "lzma.py")


def _audit(event, args):
    if event != "open" and event != "os.scandir" and event != "os.listdir":
        return
    try:
        target = args[0]
        if isinstance(target, bytes):
            target = os.fsdecode(target)
        if not isinstance(target, str):
            return
        frame = sys._getframe(1)
        caller = frame.f_code.co_filename
        for _ in range(8):  # look through pathlib, gzip, ... to who asked
            if "pathlib" not in caller and not caller.endswith(_WRAPPERS):
                break
            frame = frame.f_back
            if frame is None:
                break
            caller = frame.f_code.co_filename
        if caller.startswith("<frozen importlib") or caller.endswith(_SKIP_OPENERS):
            return
        if "_pytest" in caller:  # assertion rewriting reads every test module
            return
        if event == "open":
            if target.endswith((".pyc", ".so")):
                return
            name = rel(os.path.abspath(target))
            if name is not None:
                OPENS.add(name)
        else:
            name = rel(os.path.abspath(target or "."))
            if name is not None:
                DIRS.add(name)
            elif os.path.realpath(target or ".") + os.sep == ROOT:
                DIRS.add("")
    except Exception:
        pass


def snapshot():
    files = {}
    for name, line in list(LINES):
        files.setdefault(name, []).append(line)
    classes = set()
    for kind in list(TYPES):
        try:
            for base in kind.__mro__:
                module = sys.modules.get(base.__module__)
                name = rel(getattr(module, "__file__", None) or "")
                if name is not None:
                    classes.add(name + "::" + base.__qualname__)
        except Exception:
            pass
    return {
        "lines": files,
        "classes": sorted(classes),
        "opens": sorted(OPENS),
        "dirs": sorted(DIRS),
    }


def _write(name, payload):
    path = os.path.join(OUT, name)
    with open(path + ".tmp", "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    os.replace(path + ".tmp", path)



def install():
    os.makedirs(OUT, exist_ok=True)
    mon.use_tool_id(TOOL, "blast-radius")
    mon.register_callback(TOOL, mon.events.LINE, _on_line)
    mon.register_callback(TOOL, mon.events.PY_START, _on_start)
    mon.set_events(TOOL, mon.events.LINE | mon.events.PY_START)
    sys.addaudithook(_audit)


def reset():
    LINES.clear()
    TYPES.clear()
    OPENS.clear()
    DIRS.clear()
    mon.restart_events()


def merge_workers(payload, tag):
    """Fold in what processes spawned by this test recorded."""
    for path in glob.glob(os.path.join(OUT, "w_%s_*.json" % tag)):
        try:
            with open(path, encoding="utf-8") as handle:
                extra = json.load(handle)
        except Exception:
            payload["worker_errors"] = payload.get("worker_errors", 0) + 1
            continue
        payload["workers"] = payload.get("workers", 0) + 1
        for name, lines in extra["lines"].items():
            payload["lines"][name] = sorted(set(payload["lines"].get(name, [])) | set(lines))
        for key in ("classes", "opens", "dirs"):
            payload[key] = sorted(set(payload[key]) | set(extra[key]))
        os.remove(path)


def worker_main():
    """A process a test started (spawned pool worker, `python -c ...`)."""
    import atexit

    tag = os.environ["BR_TEST"]
    install()

    def dump():
        try:
            _write("w_%s_%d.json" % (tag, os.getpid()), snapshot())
        except BaseException:  # noqa: BLE001
            pass

    atexit.register(dump)
