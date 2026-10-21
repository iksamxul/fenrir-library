"""Every pattern a recipe reads with, timed on lines made to be slow (recipes.md section 5). The guard in policy.py
refuses the patterns whose shape can take for ever, such as (a+)+; a shape alone cannot see the slow ones that are
merely polynomial, such as .*a.*b, which takes seconds on one 4 KB line of a's. So each pattern runs, in a worker of its
own that is ended when it takes too long, on lines of the longest length a pattern ever reads (values.LINE_MAX), made
from its own characters, the usual kinds of character, and pairs of them.

    slow(patterns)    [{where, pattern, ms, line}] for each pattern slower than the budget on any line

A pattern that is still running when its time is up never finished: its ms is None."""
import json
import os
import queue
import subprocess
import sys
import threading
import time
from re import _constants as sre_c
from re import _parser as sre_parse

BUDGET_MS = 50.0  # one search on one 4 KB line: quadratic patterns on their worst line stay well under it, cubic ones do not
PER_PATTERN_S = 3.0  # a worker that has not finished one pattern's lines by then is ended, and that pattern is the slow one
KINDS = "a1 .:-_/=,;\t\"'()[]<>"  # a letter, a digit, a space and the marks log lines are made of


def _literals(pattern):
    found = []

    def walk(items):
        for op, av in items:
            if op == sre_c.LITERAL:
                found.append(chr(av))
            elif op in (sre_c.MAX_REPEAT, sre_c.MIN_REPEAT) or op == getattr(sre_c, "POSSESSIVE_REPEAT", None):
                walk(av[2])
            elif op == sre_c.SUBPATTERN:
                walk(av[-1])
            elif op == sre_c.BRANCH:
                for b in av[1]:
                    walk(b)
            elif op in (sre_c.ASSERT, sre_c.ASSERT_NOT):
                walk(av[1])
    try:
        walk(sre_parse.parse(pattern))
    except Exception:  # noqa: BLE001 - a pattern Python cannot read was refused by the policy already: nothing to add
        pass
    return found


def lines(pattern, length=4096):
    """The lines one pattern is timed on: each character it names and each usual kind of character, repeated to the
    length; each pair of them, alternating; and the pattern's own words over and over."""
    lits = _literals(pattern)
    chars = list(dict.fromkeys([*lits, *KINDS]))[:40]
    out = [c * length for c in chars]
    pairs = list(dict.fromkeys([*lits[:6], *"a1 .:"]))[:10]
    out += [((a + b) * (length // 2 + 1))[:length] for a in pairs for b in pairs if a != b]
    words = "".join(lits)
    if words:
        out.append((words * (length // len(words) + 1))[:length])
    return out


def _worker():
    """In the worker: each pattern's slowest search, a line on stdout when one starts and when it is done."""
    import re
    todo = json.loads(sys.stdin.read())
    for i, pattern in enumerate(todo["patterns"]):
        rx = re.compile(pattern)
        print(json.dumps({"start": i}), flush=True)
        worst, at = 0.0, None
        for k, line in enumerate(lines(pattern, todo["length"])):
            t = time.perf_counter()
            rx.search(line)
            ms = (time.perf_counter() - t) * 1000
            if ms > worst:
                worst, at = ms, k
        print(json.dumps({"done": i, "ms": round(worst, 3), "line": at}), flush=True)


def slow(patterns, budget_ms=BUDGET_MS, per_pattern=PER_PATTERN_S, length=None):
    """[(where, pattern, ...)] in, the slow ones out: {where, pattern, ms, line}. Runs in a worker process, so a pattern
    that would take minutes is simply ended."""
    from ..values import LINE_MAX
    length = length or LINE_MAX
    items = [(p[0], p[1]) for p in patterns]
    results, start = {}, 0
    while start < len(items):
        todo = {"patterns": [p for _, p in items[start:]], "length": length}
        from . import tool
        p = subprocess.Popen(tool.command("regex-worker"), cwd=str(_fenrir()), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
                             creationflags=0x08000000 if os.name == "nt" else 0)
        q = queue.Queue()
        threading.Thread(target=lambda: [q.put(x) for x in p.stdout] and None, daemon=True).start()
        p.stdin.write(json.dumps(todo))
        p.stdin.close()
        current = None
        try:
            while True:
                try:
                    msg = json.loads(q.get(timeout=per_pattern))
                except queue.Empty:
                    if current is not None:  # this pattern never finished: it is the slow one; the rest go on in a new worker
                        results[start + current] = {"ms": None, "line": None}
                        start += current + 1
                    else:
                        start = len(items)
                    break
                except ValueError:
                    continue
                if "start" in msg:
                    current = msg["start"]
                elif "done" in msg:
                    results[start + msg["done"]] = {"ms": msg["ms"], "line": msg["line"]}
                    current = None
                    if start + msg["done"] == len(items) - 1:
                        start = len(items)
                        break
        finally:
            if p.poll() is None:
                p.kill()
            p.wait(10)
            p.stdout.close()
    out = []
    for i, (where, pattern) in enumerate(items):
        r = results.get(i) or {"ms": None, "line": None}
        if r["ms"] is None or r["ms"] > budget_ms:
            out.append({"where": where, "pattern": pattern, "ms": r["ms"], "line": r["line"]})
    return out


def _fenrir():
    from pathlib import Path
    return Path(__file__).resolve().parents[3]


if __name__ == "__main__":  # the worker, as tool.command("regex-worker") starts it from source
    _worker()
