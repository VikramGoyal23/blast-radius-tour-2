"""Builds the dependency map the selector reads. Run with `make map`.

Step 1 runs every test file of the pinned sqlglot checkout under brtracer.
Step 2 turns the traces into, for every line of every Python file, the set of
tests a change to that line can affect, and writes one gzip'd JSON per file.

Everything here is derived from the pinned commit only: no patch, and no kind
of edit, is ever looked at.

Rules for a line's set (tests are bits in an int):

  line with recorded executions      the tests that executed it. Lines that
                                     run at import (module and class bodies)
                                     are executed by every test that loads the
                                     module, so they select all of those.
  line without (continuation, ...)   union over its innermost statement, else
                                     over the innermost enclosing block.
  def header (decorators, signature) import-time set, plus every test that
                                     runs any line of the function ("dcall").
  class body of an expression node   import-time set, cut down to the tests
                                     that instantiate that class or a subclass,
                                     read `Class.attr` in executed code, or
                                     walk the class registries.
"""

import ast
import glob
import gzip
import importlib
import io
import json
import os
import pkgutil
import re
import shutil
import subprocess
import sys
import tokenize
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.realpath(os.environ.get("BR_ROOT", "/src"))
TOOLS = os.path.dirname(os.path.abspath(__file__))
WORK = os.environ.get("BR_WORK", "/tmp/brwork")
OUT = os.environ.get("BR_MAP", "/out")

REGISTRY = re.compile(r"ALL_FUNCTIONS|FUNCTION_BY_NAME|__subclasses__|\bsubclasses\(")
ATTR_OF = re.compile(r"\b([A-Za-z_]\w*)\s*\.")


def log(*parts):
    print(*parts, file=sys.stderr, flush=True)


def flat(path):
    return path.replace("/", "__")


# ---------------------------------------------------------------- step 1


def test_files():
    found = glob.glob(os.path.join(ROOT, "tests", "**", "test_*.py"), recursive=True)
    return sorted(os.path.relpath(path, ROOT).replace(os.sep, "/") for path in found)


def trace(test_file):
    out = os.path.join(WORK, flat(test_file))
    shutil.rmtree(out, ignore_errors=True)
    os.makedirs(out)
    env = dict(os.environ)
    env.update(
        BR_ROOT=ROOT,
        BR_OUT=out,
        SKIP_INTEGRATION="0",
        PYTHONPATH=ROOT + os.pathsep + TOOLS,
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONHASHSEED="0",
    )
    command = [sys.executable, "-m", "pytest", test_file, "-p", "brtracer", "-q"]
    command += ["-p", "no:cacheprovider"]
    with open(os.path.join(out, "pytest.log"), "wb") as handle:
        result = subprocess.run(command, cwd=ROOT, env=env, stdout=handle, stderr=subprocess.STDOUT)
    log("traced", test_file, "exit", result.returncode)
    return out


# ---------------------------------------------------------------- step 2


class Structure:
    """What the syntax tree says about each line of one file."""

    def __init__(self, source):
        self.source = source
        self.n = n = source.count("\n") + 1
        self.kind = ["c"] * (n + 2)  # c: module/class level, d: def header, f: in a function
        self.cls = [None] * (n + 2)
        self.unit = [None] * (n + 2)
        self.encl = [None] * (n + 2)
        self.dcall = {}
        self.lamfirst = {}
        self.strs = []
        tree = ast.parse(source)
        self.visit(tree.body, False, "")
        lambdas = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Lambda) and node.end_lineno > node.lineno
        ]
        for node in sorted(lambdas, key=lambda node: (node.lineno, -node.end_lineno)):
            for line in range(node.lineno + 1, min(node.end_lineno, n) + 1):
                self.kind[line] = "f"
                self.unit[line] = (node.lineno + 1, node.end_lineno)
                self.lamfirst[line] = node.lineno
        self.strings()

    def fill(self, array, start, end, value):
        for line in range(start, min(end, self.n) + 1):
            array[line] = value

    @staticmethod
    def first(node):
        lines = [decorator.lineno for decorator in getattr(node, "decorator_list", [])]
        return min(lines + [node.lineno])

    def visit(self, statements, infunc, qual):
        for node in statements:
            start, end = self.first(node), node.end_lineno
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.fill(self.encl, start, end, (start, end))
                head = max(start, self.first(node.body[0]) - 1)
                self.fill(self.unit, start, head, (start, head))
                if not infunc:
                    self.fill(self.kind, start, head, "d")
                    body = (head + 1, end) if head < end else (start, end)
                    for line in range(start, head + 1):
                        self.dcall[line] = body
                self.fill(self.kind, head + 1, end, "f")
                self.visit(node.body, True, qual)
            elif isinstance(node, ast.ClassDef):
                self.fill(self.encl, start, end, (start, end))
                inner = (qual + "." if qual else "") + node.name
                head = max(start, self.first(node.body[0]) - 1)
                self.fill(self.unit, start, head, (start, head))
                if not infunc:
                    self.fill(self.cls, start, end, inner)
                self.visit(node.body, infunc, inner)
            else:
                blocks = []
                for field in ("body", "orelse", "finalbody"):
                    value = getattr(node, field, None)
                    if isinstance(value, list) and value:
                        blocks.append(value)
                handlers = getattr(node, "handlers", None) or []
                cases = getattr(node, "cases", None) or []
                if not (blocks or handlers or cases):
                    self.fill(self.unit, start, end, (start, end))
                    continue
                self.fill(self.encl, start, end, (start, end))
                firsts = [self.first(block[0]) for block in blocks]
                firsts += [handler.lineno for handler in handlers]
                firsts += [case.pattern.lineno for case in cases]
                head = max(start, min(firsts) - 1)
                self.fill(self.unit, start, head, (start, head))
                for block in blocks:
                    self.visit(block, infunc, qual)
                for handler in handlers:
                    top = max(handler.lineno, self.first(handler.body[0]) - 1)
                    self.fill(self.unit, handler.lineno, top, (handler.lineno, top))
                    self.visit(handler.body, infunc, qual)
                for case in cases:
                    self.visit(case.body, infunc, qual)

    def strings(self):
        """Line ranges of string tokens that span lines: a `#` in there is text."""
        stack = []
        fstart = getattr(tokenize, "FSTRING_START", -1)
        fend = getattr(tokenize, "FSTRING_END", -1)
        try:
            for token in tokenize.generate_tokens(io.StringIO(self.source).readline):
                if token.type == tokenize.STRING:
                    if token.end[0] > token.start[0]:
                        self.strs.append([token.start[0], token.end[0]])
                elif token.type == fstart:
                    stack.append(token.start[0])
                elif token.type == fend and stack:
                    begin = stack.pop()
                    if token.end[0] > begin:
                        self.strs.append([begin, token.end[0]])
        except (tokenize.TokenError, IndentationError, SyntaxError):
            pass


class Masks:
    """Per-line test sets for one file, on top of its Structure."""

    def __init__(self, structure, hits):
        self.s = structure
        self.hits = hits  # line -> mask of tests that executed it
        self.cache = {}
        self.everyone = 0
        for mask in hits.values():
            self.everyone |= mask

    def union(self, span):
        try:
            return self.cache[span]
        except KeyError:
            pass
        result = 0
        get = self.hits.get
        for line in range(span[0], span[1] + 1):
            result |= get(line, 0)
        self.cache[span] = result
        return result

    def base(self, line):
        # A whole statement, not one line of it: in a condition split over two
        # lines the second runs only for some tests, yet changing it can change
        # what the others do.
        unit = self.s.unit[line]
        if unit:
            result = self.union(unit) | self.hits.get(line, 0)
            if not result and line in self.s.lamfirst:
                result = self.hits.get(self.s.lamfirst[line], 0)
            return result
        encl = self.s.encl[line]
        return self.union(encl) if encl else self.everyone

    def callers(self, line):
        span = self.s.dcall.get(line)
        return self.union(span) if span else 0


def expression_classes():
    """{(file, qualname): simple name} for every sqlglot expression node class."""
    sys.path.insert(0, ROOT)
    import sqlglot  # noqa: F401

    for info in pkgutil.walk_packages(sqlglot.__path__, prefix="sqlglot."):
        if info.name.endswith("__main__"):
            continue
        try:
            importlib.import_module(info.name)
        except Exception as error:  # noqa: BLE001
            log("could not import", info.name, repr(error))
    from sqlglot.expressions.core import Expr

    found = {}
    todo = [Expr]
    seen = set()
    while todo:
        kind = todo.pop()
        if kind in seen:
            continue
        seen.add(kind)
        todo.extend(kind.__subclasses__())
        module = sys.modules.get(kind.__module__)
        path = getattr(module, "__file__", None)
        if not path:
            continue
        path = os.path.realpath(path)
        if path.startswith(ROOT + os.sep):
            name = path[len(ROOT) + 1 :].replace(os.sep, "/")
            found[(name, kind.__qualname__)] = kind.__name__
    return found


def tohex(mask):
    return "%x" % mask


def main():
    os.makedirs(WORK, exist_ok=True)
    os.makedirs(OUT, exist_ok=True)
    files = test_files()
    log("tracing", len(files), "test files on", os.cpu_count(), "cpus")
    with ThreadPoolExecutor(max_workers=os.cpu_count() or 2) as pool:
        outs = list(pool.map(trace, files))

    tests = []
    worker_counts = []
    untraced = []
    failed = []
    hits = {}  # file -> line -> mask
    classes = {}
    opens = {}
    dirs = {}

    def absorb(record, mask):
        for name, lines in record.get("lines", {}).items():
            target = hits.setdefault(name, {})
            for line in lines:
                target[line] = target.get(line, 0) | mask
        for key, table in (("classes", classes), ("opens", opens), ("dirs", dirs)):
            for name in record.get(key, []):
                table[name] = table.get(name, 0) | mask

    for test_file, out in zip(files, outs):
        parent_path = os.path.join(out, "parent.json")
        if not os.path.exists(parent_path):
            log("NO PARENT RECORD for", test_file, "- see", os.path.join(out, "pytest.log"))
            continue
        group = 0
        for path in sorted(glob.glob(os.path.join(out, "t*.json"))):
            with open(path, encoding="utf-8") as handle:
                record = json.load(handle)
            if "crashed" in record:
                untraced.append(record["id"])
                log("UNTRACED", record["id"], record["crashed"])
                continue
            worker_counts.append(record.get("workers", 0))
            if record.get("worker_errors"):
                log("WORKER RECORD UNREADABLE for", record["id"])
            bit = 1 << len(tests)
            tests.append(record["id"])
            if record.get("failed"):
                failed.append(record["id"])
            group |= bit
            absorb(record, bit)
        with open(parent_path, encoding="utf-8") as handle:
            parent = json.load(handle)
        parent["dirs"] = []  # pytest's own directory walks, not the tests'
        absorb(parent, group)
    log(sum(r for r in worker_counts), "worker processes folded in")
    log(len(tests), "tests traced,", len(untraced), "untraced,", len(failed), "failing at the pin")
    for name in failed:
        log("  fails at the pin:", name)

    sources = set()
    for top in ("sqlglot", "tests"):
        for path in glob.glob(os.path.join(ROOT, top, "**", "*.py"), recursive=True):
            sources.add(os.path.relpath(path, ROOT).replace(os.sep, "/"))
    sources.update(name for name in hits if name.endswith(".py"))

    built = {}
    for name in sorted(sources):
        try:
            with open(os.path.join(ROOT, name), encoding="utf-8", newline="") as handle:
                source = handle.read()
            if "\r" in source:
                log("skipping file with carriage returns:", name)
                continue
            built[name] = Masks(Structure(source), hits.get(name, {}))
        except Exception as error:  # noqa: BLE001
            log("could not analyse", name, repr(error))

    # Tests that touch an expression class other than by instantiating it.
    try:
        expr = expression_classes()
    except Exception as error:  # noqa: BLE001
        log("NO EXPRESSION CLASSES, class bodies stay unrefined:", repr(error))
        expr = {}
    names = set(expr.values())
    refs = {}
    registry = 0
    for name, masks in built.items():
        structure = masks.s
        for number, text in enumerate(structure.source.split("\n"), 1):
            if structure.kind[number] != "f":
                continue
            if REGISTRY.search(text):
                registry |= masks.base(number)
            for word in ATTR_OF.findall(text):
                if word in names:
                    refs[word] = refs.get(word, 0) | masks.base(number)
    log(len(expr), "expression classes; registry walkers:", bin(registry).count("1"))

    total = 0
    written = []
    for name, masks in built.items():
        structure = masks.s
        table = {0: 0}
        order = [0]

        def index(mask):
            position = table.get(mask)
            if position is None:
                position = table[mask] = len(order)
                order.append(mask)
            return position

        line_sets = []
        dcall = {}
        for line in range(1, structure.n + 1):
            mask = masks.base(line)
            kind = structure.kind[line]
            key = (name, structure.cls[line])
            if kind != "f" and key in expr:
                mask &= classes.get(name + "::" + key[1], 0) | refs.get(expr[key], 0) | registry
            if kind == "d":
                called = masks.callers(line)
                mask |= called
                dcall[str(line)] = index(called)
            line_sets.append(index(mask))
        payload = {
            "src": structure.source,
            "kind": "".join(structure.kind[1 : structure.n + 1]),
            "sets": [tohex(mask) for mask in order],
            "line": line_sets,
            "dcall": dcall,
            "users": tohex(masks.everyone),
            "strs": structure.strs,
        }
        data = gzip.compress(json.dumps(payload, separators=(",", ":")).encode("utf-8"), 9, mtime=0)
        with open(os.path.join(OUT, flat(name) + ".json.gz"), "wb") as handle:
            handle.write(data)
        total += len(data)
        written.append(name)

    index_payload = {
        "tests": tests,
        "untraced": untraced,
        "files": written,
        "opens": {name: tohex(mask) for name, mask in sorted(opens.items())},
        "dirs": {name: tohex(mask) for name, mask in sorted(dirs.items())},
    }
    data = json.dumps(index_payload, separators=(",", ":")).encode("utf-8")
    with open(os.path.join(OUT, "index.json"), "wb") as handle:
        handle.write(data)
    total += len(data)
    log("wrote", len(written), "file maps,", round(total / 1e6, 2), "MB, to", OUT)


if __name__ == "__main__":
    main()
