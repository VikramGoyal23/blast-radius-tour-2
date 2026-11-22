"""Blast Radius selector: which tests can a patch break, and how to split them.

The map in /app/map (built once from the pinned commit by tools/build_map.py)
says, for every line of every Python file, which tests a change to that line
can affect, and which tests open each file as data. For each patch we:

  1. drop edits that cannot change behaviour (blank lines and whole-line
     comments outside string literals);
  2. look every other touched line up in the map and take the union;
  3. add every test that loads the file if the patched file no longer parses;
  4. add the tests that read a touched file as data (fixtures, source text);
  5. split the selection over the number of workers with the lowest cost.

Anything the selector cannot reason about selects every test: a miss costs far
more than a full run. Logs go to stderr; stdout carries only the answers.
"""

import ast
import gzip
import json
import os
import re
import sys

DATA = os.environ.get("BR_DATA", "/data")
MAP = os.environ.get("BR_MAP") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "map")
WORKER_MS = 2000
MAX_WORKERS = 8
ALL = -1  # sentinel mask: every test

# Files that configure the test run itself.
GLOBAL_NAMES = {"conftest.py", "pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml"}
HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
DEF = re.compile(r"\s*(?:async\s+)?def\s+(\w+)")


def log(*parts):
    print(*parts, file=sys.stderr)


class FileMap:
    def __init__(self, payload):
        self.old = payload["src"].split("\n")
        self.kind = payload["kind"]
        self.hexsets = payload["sets"]
        self.line = payload["line"]
        self.dcall = payload["dcall"]
        self.users = int(payload["users"], 16)
        self.strs = payload["strs"]
        self.ints = {}

    def set(self, index):
        value = self.ints.get(index)
        if value is None:
            value = self.ints[index] = int(self.hexsets[index], 16)
        return value

    def mask(self, line):
        if 1 <= line <= len(self.line):
            return self.set(self.line[line - 1])
        return self.users

    def callers(self, line):
        index = self.dcall.get(str(line))
        return self.set(index) if index is not None else self.mask(line)

    def kind_of(self, line):
        return self.kind[line - 1] if 1 <= line <= len(self.kind) else "c"

    def in_string(self, line):
        """Is old line `line` the inside or the end of a multi-line string?"""
        return any(start < line <= end for start, end in self.strs)

    def gap_in_string(self, line):
        """Is the gap after old line `line` inside a multi-line string?"""
        return any(start <= line < end for start, end in self.strs)


class Maps:
    def __init__(self):
        with open(os.path.join(MAP, "index.json"), encoding="utf-8") as handle:
            index = json.load(handle)
        self.tests = index["tests"]
        self.files = set(index["files"])
        self.opens = index["opens"]
        self.dirs = index["dirs"]
        self.cache = {}

    def file(self, name):
        if name not in self.files:
            return None
        if name not in self.cache:
            path = os.path.join(MAP, name.replace("/", "__") + ".json.gz")
            with gzip.open(path, "rb") as handle:
                self.cache[name] = FileMap(json.loads(handle.read().decode("utf-8")))
        return self.cache[name]

    def readers(self, name):
        value = self.opens.get(name)
        return int(value, 16) if value else 0

    def listers(self, name):
        value = self.dirs.get(os.path.dirname(name))
        return int(value, 16) if value else 0


# ------------------------------------------------------------------ patches


def parse_patch(text):
    """Yields one dict per file: old and new path, flags, and its change blocks.

    A block is (after, removed, added): `after` is the last old line before
    the block, `removed` is [(old line number, text)], `added` is [text].
    """
    files = []
    current = None
    lines = text.split("\n")
    position = 0
    total = len(lines)
    while position < total:
        line = lines[position]
        if line.startswith("diff --git "):
            current = {"old": None, "new": None, "blocks": [], "odd": False, "gone": False}
            current["made"] = False
            match = re.match(r"diff --git a/(.*) b/(.*)$", line)
            if match:
                current["old"], current["new"] = match.group(1), match.group(2)
            files.append(current)
            position += 1
            continue
        if current is None:
            position += 1
            continue
        if line.startswith("--- "):
            name = line[4:].split("\t")[0].strip()
            current["old"] = None if name == "/dev/null" else name[2:] if name[:2] == "a/" else name
            if name == "/dev/null":
                current["made"] = True
        elif line.startswith("+++ "):
            name = line[4:].split("\t")[0].strip()
            current["new"] = None if name == "/dev/null" else name[2:] if name[:2] == "b/" else name
            if name == "/dev/null":
                current["gone"] = True
        elif line.startswith("rename from "):
            current["old"] = line[12:].strip()
            current["odd"] = True
        elif line.startswith("rename to "):
            current["new"] = line[10:].strip()
            current["odd"] = True
        elif line.startswith(("copy from ", "copy to ")):
            current["odd"] = True
        elif line.startswith("deleted file mode"):
            current["gone"] = True
        elif line.startswith("new file mode"):
            current["made"] = True
        elif line.startswith(("Binary files ", "GIT binary patch", "old mode ", "new mode ")):
            current["odd"] = True
        else:
            match = HUNK.match(line)
            if match:
                old_line = int(match.group(1))
                old_left = int(match.group(2)) if match.group(2) is not None else 1
                new_left = int(match.group(4)) if match.group(4) is not None else 1
                if old_left == 0:
                    old_line += 1  # "-N,0" names the line before the insertion
                position += 1
                block = None
                while position < total and (old_left > 0 or new_left > 0):
                    body = lines[position]
                    tag = body[:1]
                    if tag == "\\":
                        position += 1
                        continue
                    if tag == "-":
                        if block is None:
                            block = (old_line - 1, [], [])
                            current["blocks"].append(block)
                        block[1].append((old_line, body[1:]))
                        old_line += 1
                        old_left -= 1
                    elif tag == "+":
                        if block is None:
                            block = (old_line - 1, [], [])
                            current["blocks"].append(block)
                        block[2].append(body[1:])
                        new_left -= 1
                    else:
                        block = None
                        old_line += 1
                        old_left -= 1
                        new_left -= 1
                    position += 1
                continue
        position += 1
    return files


def noise(text):
    stripped = text.strip()
    return not stripped or stripped[0] == "#"


def indent(text):
    expanded = text.expandtabs(8)
    return len(expanded) - len(expanded.lstrip())


def insertion_mask(fmap, after, code):
    """Tests that can reach code inserted after old line `after`.

    The inserted code runs only if the header of the block it sits in ran, so
    find that header by indentation. Module and class level means every test
    that loads the file.
    """
    depth = indent(code[0])
    if depth == 0:
        return fmap.users
    line = min(after, len(fmap.old))
    while line >= 1:
        text = fmap.old[line - 1]
        if not noise(text) and not fmap.in_string(line) and indent(text) < depth:
            break
        line -= 1
    if line < 1:
        return fmap.users
    kind = fmap.kind_of(line)
    if kind == "f":
        return fmap.mask(line)
    if kind == "d":
        return fmap.callers(line)
    return fmap.users


def python_mask(fmap, blocks):
    mask = 0
    real = False
    for after, removed, added in blocks:
        quoted = fmap.gap_in_string(after) or any(fmap.in_string(n) for n, _ in removed)
        if removed and fmap.gap_in_string(removed[-1][0]):
            quoted = True
        if 1 <= after <= len(fmap.old) and fmap.old[after - 1].rstrip().endswith("\\"):
            quoted = True
        if quoted:
            old_code, new_code = removed, added
        else:
            old_code = [(n, t) for n, t in removed if not noise(t)]
            new_code = [t for t in added if not noise(t)]
        if not quoted and [t for _, t in old_code] == new_code:
            continue  # only comments or blank lines differ
        real = True
        if not old_code:
            if quoted:  # new text inside a string or a continued line
                mask |= fmap.mask(after) | fmap.mask(after + 1)
            else:
                mask |= insertion_mask(fmap, removed[-1][0] if removed else after, new_code)
            continue
        for number, text in old_code:
            if fmap.kind_of(number) == "d" and not text.lstrip().startswith("@"):
                name = DEF.match(text)
                if name is None or any(
                    (other := DEF.match(t)) and other.group(1) == name.group(1) for t in new_code
                ):
                    mask |= fmap.callers(number)  # same function, new signature
                    continue
            mask |= fmap.mask(number)
    if real and not parses(fmap, blocks):
        mask |= fmap.users
    return mask


def parses(fmap, blocks):
    """Does the patched file still parse? If not, nothing that loads it works."""
    out = []
    cursor = 0  # old lines copied so far
    for after, removed, added in blocks:
        out.extend(fmap.old[cursor:after])
        out.extend(added)
        cursor = after + len(removed)
    out.extend(fmap.old[cursor:])
    try:
        ast.parse("\n".join(out))
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return False
    return True


def patch_mask(maps, text):
    mask = 0
    for entry in parse_patch(text):
        old, new = entry["old"], entry["new"]
        names = [name for name in (old, new) if name]
        for name in names:
            base = os.path.basename(name)
            if base in GLOBAL_NAMES or ("/" not in name and name.endswith(".py")):
                return ALL
            mask |= maps.readers(name)
        moved = entry["odd"] or entry["gone"] or entry["made"] or old != new
        if moved:
            for name in names:
                mask |= maps.listers(name)
        fmap = maps.file(old) if old and not entry["made"] else None
        if fmap is None:
            continue
        if entry["odd"] or entry["gone"] or old != new:
            mask |= fmap.users
        else:
            mask |= python_mask(fmap, entry["blocks"])
    return mask


# ------------------------------------------------------------------ workers


def split(numbers, millis):
    """Longest first onto the lightest worker, for the cheapest worker count."""
    if not numbers:
        return []
    items = sorted(numbers, key=lambda number: (-millis[number], number))
    longest = millis[items[0]]
    best = None
    for count in range(1, min(MAX_WORKERS, len(items)) + 1):
        loads = [0] * count
        workers = [[] for _ in range(count)]
        for number in items:
            lightest = loads.index(min(loads))
            workers[lightest].append(number)
            loads[lightest] += millis[number]
        cost = max(loads) + WORKER_MS * count
        if best is None or cost < best[0]:
            best = (cost, workers)
        if max(loads) - longest < WORKER_MS:
            break  # each further worker costs 2 s and cannot save that much
    return [worker for worker in best[1] if worker]


def main():
    with open(os.path.join(DATA, "tests.json"), encoding="utf-8") as handle:
        tests = json.load(handle)["tests"]
    millis = {test["number"]: round(test["seconds"] * 1000) for test in tests}
    everything = sorted(millis)
    by_id = {test["id"]: test["number"] for test in tests}

    try:
        maps = Maps()
        bit_to_number = [by_id.get(name) for name in maps.tests]
        known = set(maps.tests)
        unknown = [test["number"] for test in tests if test["id"] not in known]
        if unknown:
            log(len(unknown), "tests are not in the map; they run on every real change")
    except Exception as error:  # noqa: BLE001
        log("NO USABLE MAP, selecting every test:", repr(error))
        maps = None

    commits = os.path.join(DATA, "commits")
    names = sorted(name for name in os.listdir(commits) if name.endswith(".patch"))
    out = []
    for name in names:
        try:
            if maps is None:
                raise RuntimeError("no map")
            with open(os.path.join(commits, name), "rb") as handle:
                text = handle.read().decode("utf-8", "surrogateescape")
            mask = patch_mask(maps, text)
            if mask == ALL:
                chosen = everything
            else:
                chosen = set()
                while mask:
                    low = mask & -mask
                    number = bit_to_number[low.bit_length() - 1]
                    if number is not None:
                        chosen.add(number)
                    mask ^= low
                if chosen:
                    chosen.update(unknown)
                chosen = sorted(chosen)
        except Exception as error:  # noqa: BLE001
            log(name, "fell back to every test:", repr(error))
            chosen = everything
        workers = split(chosen, millis)
        log(name[:-6], len(chosen), "tests", len(workers), "workers")
        out.append(json.dumps({"commit": name[:-6], "workers": workers}, separators=(",", ":")))
    sys.stdout.write("".join(line + "\n" for line in out))


if __name__ == "__main__":
    main()
