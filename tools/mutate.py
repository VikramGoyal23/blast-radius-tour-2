"""Generates practice patches against the pinned checkout for local evaluation
(operator flips, constant edits, fixture corruptions, comment insertions).
Evaluation data only: nothing it writes goes into the image or the map.

    python mutate.py <out dir> <seed> name=count ...
"""

import ast
import difflib
import glob
import gzip
import json
import os
import random
import re
import sys

SRC = os.environ.get("BR_ROOT", "/src")
WORK = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAP = os.path.join(WORK, "map")


def bump(match):
    return str(int(match.group(0)) + 1)


OPS = [
    (r" == ", " != "),
    (r" != ", " == "),
    (r" is not ", " is "),
    (r" is (?!not )", " == "),
    (r" and ", " or "),
    (r" or ", " and "),
    (r" < ", " <= "),
    (r" <= ", " < "),
    (r" > ", " >= "),
    (r" >= ", " > "),
    (r"\bTrue\b", "False"),
    (r"\bFalse\b", "True"),
    (r"\bif not ", "if "),
    (r"(?<![\w.\"'])\d+(?![\w.\"'])", bump),
    (r" \+ ", " - "),
    (r" - ", " + "),
]
COMMENTS = [
    "# Cheap check first",
    "# Kept for backwards compatibility",
    "# Intentionally not cached",
    "# Keep in sync with the generator",
    "# Mirrors the behavior of the reference implementation",
]
JUNK = [" 42", " x", " NULL", "0 ", "a"]
DIALECT_DIRS = ("sqlglot/dialects/", "sqlglot/parsers/", "sqlglot/generators/", "sqlglot/typing/")


def load():
    with open(os.path.join(MAP, "index.json"), encoding="utf-8") as handle:
        index = json.load(handle)
    with open(os.path.join(WORK, "sample", "tests.json"), encoding="utf-8") as handle:
        seconds = {test["id"]: test["seconds"] for test in json.load(handle)["tests"]}
    slow = 0
    for bit, name in enumerate(index["tests"]):
        if seconds.get(name, 0) > 3:
            slow |= 1 << bit
    files = {}
    for name in index["files"]:
        with gzip.open(os.path.join(MAP, name.replace("/", "__") + ".json.gz"), "rb") as handle:
            payload = json.loads(handle.read().decode("utf-8"))
        payload["masks"] = [int(payload["sets"][i], 16) for i in payload["line"]]
        files[name] = payload
    return files, slow


def mutate_line(rng, text):
    stripped = text.strip()
    if not stripped or stripped[0] in "#@\"'" or stripped.startswith(("import ", "from ")):
        return None
    options = []
    for pattern, replacement in OPS:
        for match in re.finditer(pattern, text):
            before = text[: match.start()]
            if before.count('"') % 2 or before.count("'") % 2 or "#" in before:
                continue
            options.append((replacement, match))
    if not options:
        return None
    replacement, match = rng.choice(options)
    new = replacement(match) if callable(replacement) else replacement
    return text[: match.start()] + new + text[match.end() :]


def diff(name, old, new):
    body = difflib.unified_diff(old, new, "a/" + name, "b/" + name, lineterm="", n=3)
    return "diff --git a/%s b/%s\n" % (name, name) + "\n".join(body) + "\n"


def code_patch(rng, files, pick):
    for _ in range(5000):
        name = rng.choice(pick["names"])
        payload = files[name]
        lines = payload["src"].split("\n")
        if len(payload["masks"]) < 3:
            continue
        number = rng.randrange(1, len(payload["masks"]))
        if not pick["want"](payload, number):
            continue
        if any(a < number <= b for a, b in payload["strs"]):
            continue
        new = mutate_line(rng, lines[number - 1])
        if new is None or new == lines[number - 1]:
            continue
        changed = lines[: number - 1] + [new] + lines[number:]
        try:
            ast.parse("\n".join(changed))
        except SyntaxError:
            continue
        return diff(name, lines, changed)
    raise RuntimeError("no site found")


def comment_patch(rng, files):
    name = rng.choice(sorted(n for n in files if n.startswith("sqlglot/") and len(files[n]["src"]) > 200))
    lines = files[name]["src"].split("\n")
    number = rng.randrange(1, len(lines))
    while not lines[number - 1].strip():
        number = rng.randrange(1, len(lines))
    text = lines[number - 1]
    pad = text[: len(text) - len(text.lstrip())]
    changed = lines[: number - 1] + [pad + rng.choice(COMMENTS)] + lines[number - 1 :]
    return diff(name, lines, changed)


def fixture_patch(rng):
    paths = glob.glob(os.path.join(SRC, "tests", "fixtures", "**", "*.sql"), recursive=True)
    paths = sorted(p for p in paths if "tpc-" not in p)
    path = rng.choice(paths)
    name = os.path.relpath(path, SRC).replace(os.sep, "/")
    with open(path, encoding="utf-8", newline="") as handle:
        lines = handle.read().split("\n")
    while True:
        number = rng.randrange(1, len(lines))
        text = lines[number - 1]
        if text.strip() and not text.startswith(("--", "#")) and " " in text:
            break
    at = rng.choice([i for i, ch in enumerate(text) if ch == " "])
    changed = lines[: number - 1] + [text[:at] + rng.choice(JUNK) + text[at:]] + lines[number:]
    return diff(name, lines, changed)


def main():
    out, seed = sys.argv[1], int(sys.argv[2])
    counts = dict((part.split("=")[0], int(part.split("=")[1])) for part in sys.argv[3:])
    rng = random.Random(seed)
    files, slow = load()
    lib = sorted(n for n in files if n.startswith("sqlglot/"))
    dialect = [n for n in lib if n.startswith(DIALECT_DIRS) and not n.endswith(("__init__.py", "/dialect.py"))]
    core = [n for n in lib if n not in dialect and not n.startswith("sqlglot/expressions/")]
    expr = [n for n in lib if n.startswith("sqlglot/expressions/")]
    tests = sorted(n for n in files if n.startswith("tests/"))

    def kind(payload, number):
        return payload["kind"][number - 1]

    picks = {
        "func": {"names": lib, "want": lambda p, i: kind(p, i) == "f" and p["masks"][i - 1]},
        "slow": {"names": lib, "want": lambda p, i: kind(p, i) == "f" and p["masks"][i - 1] & slow},
        "dead": {"names": lib, "want": lambda p, i: kind(p, i) == "f" and not p["masks"][i - 1]},
        "dialect": {"names": dialect, "want": lambda p, i: kind(p, i) == "c"},
        "expr": {"names": expr, "want": lambda p, i: kind(p, i) == "c"},
        "core": {"names": core, "want": lambda p, i: kind(p, i) == "c"},
        "header": {"names": lib, "want": lambda p, i: kind(p, i) == "d"},
        "test": {"names": tests, "want": lambda p, i: kind(p, i) == "f" and p["masks"][i - 1]},
    }
    os.makedirs(os.path.join(out, "commits"), exist_ok=True)
    kinds = {}
    serial = 0
    for name, count in counts.items():
        made = 0
        while made < count:
            if name == "comment":
                text = comment_patch(rng, files)
            elif name == "fixture":
                text = fixture_patch(rng)
            elif name == "multi":
                parts = [comment_patch(rng, files)]
                if rng.random() < 0.5:
                    parts.append(fixture_patch(rng))
                else:
                    parts.append(code_patch(rng, files, picks["func"]))
                if rng.random() < 0.5:
                    parts.append(code_patch(rng, files, picks["dialect"]))
                seen = [re.match(r"diff --git a/(\S+)", part).group(1) for part in parts]
                if len(set(seen)) != len(seen):
                    continue
                text = "".join(parts)
            else:
                text = code_patch(rng, files, picks[name])
            made += 1
            serial += 1
            ident = "%s%03d" % (name[:2], serial)
            path = os.path.join(out, "commits", ident + ".patch")
            with open(path, "w", encoding="utf-8", newline="") as handle:
                handle.write(text)
            kinds[ident] = name
    with open(os.path.join(out, "kinds.json"), "w", encoding="utf-8") as handle:
        json.dump(kinds, handle)
    print(len(kinds), "patches written to", out, file=sys.stderr)


if __name__ == "__main__":
    main()
