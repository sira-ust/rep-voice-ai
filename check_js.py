#!/usr/bin/env python3
"""
Catch the JavaScript mistakes that get past reading it: unbalanced brackets,
and a quote that never closes on its line.

    python check_js.py public/app.js

There is no node on this machine, so nothing here parses JavaScript properly.
This walks the file as a tokeniser would -- tracking whether it is inside a
comment, a string or a template literal -- which is enough to catch the two
faults that have actually shipped from this repo. A raw newline inside a '' or
"" string is the dangerous one: it is a syntax error that takes the whole file
down, and it reads as perfectly ordinary in a diff.
"""

from __future__ import annotations

import sys
from pathlib import Path


def check(path: Path) -> list[str]:
    s = path.read_text(encoding="utf-8")
    problems, stack = [], []
    i, n, line = 0, len(s), 1
    while i < n:
        c = s[i]
        if c == "\n":
            line += 1
            i += 1
            continue
        if s.startswith("//", i):
            i = s.find("\n", i)
            i = n if i < 0 else i
            continue
        if s.startswith("/*", i):
            j = s.find("*/", i)
            line += s.count("\n", i, j if j > 0 else n)
            i = n if j < 0 else j + 2
            continue
        if c in "\"'`":
            quote, opened, i = c, line, i + 1
            while i < n:
                if s[i] == "\\":
                    i += 2
                    continue
                if s[i] == "\n":
                    line += 1
                    if quote != "`":
                        problems.append(
                            "line %d: %s string is not closed before the end of "
                            "the line" % (opened, quote))
                        break
                if s[i] == quote:
                    break
                if quote == "`" and s.startswith("${", i):
                    depth, i = 1, i + 2
                    while i < n and depth:
                        if s[i] == "{":
                            depth += 1
                        elif s[i] == "}":
                            depth -= 1
                        elif s[i] == "\n":
                            line += 1
                        i += 1
                    continue
                i += 1
            i += 1
            continue
        if c in "([{":
            stack.append((c, line))
        elif c in ")]}":
            if not stack:
                problems.append("line %d: stray %s" % (line, c))
            else:
                op, at = stack.pop()
                if "([{".index(op) != ")]}".index(c):
                    problems.append("line %d: %s opened on line %d, closed by %s"
                                    % (line, op, at, c))
        i += 1
    if stack:
        problems.append("line %d: %s is never closed" % (stack[-1][1], stack[-1][0]))
    return problems


def main() -> int:
    paths = [Path(a) for a in sys.argv[1:]] or [Path("public/app.js")]
    bad = 0
    for path in paths:
        if not path.is_file():
            print("  %s not found" % path)
            bad += 1
            continue
        problems = check(path)
        print("  %-22s %s" % (path.name, "ok" if not problems
                              else "%d problem(s)" % len(problems)))
        for p in problems:
            print("      " + p)
        bad += bool(problems)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
