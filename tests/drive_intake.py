#!/usr/bin/env python3
"""
Expect-style driver for interactive intake runs in tests.

Usage:
    venv/bin/python tests/drive_intake.py RULES_FILE [-- intake args...]

RULES_FILE: JSON list of [pattern, answer] pairs. When an input prompt
("  > " marker) appears, the most recent output is matched against the
patterns in order; the first match's answer is sent. A pattern of "*"
matches anything (useful as a default-accepting fallback: answer "").

Exits with intake's exit code. Full transcript goes to stdout.
"""

import json
import re
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PROMPT = "  > "


def main():
    rules_file = sys.argv[1]
    extra = sys.argv[2:]
    if extra and extra[0] == "--":
        extra = extra[1:]
    rules = [(re.compile(p, re.S), a) for p, a in json.loads(Path(rules_file).read_text())]

    proc = subprocess.Popen(
        [sys.executable, "-u", str(REPO / "intake" / "intake.py"), *extra],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=0,
    )
    window = ""
    transcript = []
    ansi = re.compile(r"\x1b\[[0-9;]*m")

    BEETS_ENDINGS = ("abort?", "release id:", "search:", "search terms:",
                     "merge all?", "(y/n)?", "keep both?")

    def at_prompt(w: str) -> bool:
        if w.endswith(PROMPT):
            return True
        # beets TUI prompts have well-known literal endings (case-insensitive)
        plain = ansi.sub("", w).rstrip(" ").lower()
        return plain.endswith(BEETS_ENDINGS)

    try:
        while True:
            ch = proc.stdout.read(1)
            if ch == "":
                break
            window += ch
            transcript.append(ch)
            sys.stdout.write(ch)
            sys.stdout.flush()
            if at_prompt(window):
                # Match only the current question block (last 8 lines), not
                # stale output from earlier phases.
                context = ansi.sub("", "\n".join(window.splitlines()[-8:]))
                answer = None
                for pattern, ans in rules:
                    if pattern.search(context):
                        answer = ans
                        break
                if answer is None:
                    answer = ""
                sys.stdout.write(f"<<sending: {answer!r}>>\n")
                proc.stdin.write(answer + "\n")
                proc.stdin.flush()
                window = ""  # each question matches against fresh context
    finally:
        proc.stdin.close()
        proc.wait()
    sys.exit(proc.returncode)


if __name__ == "__main__":
    main()
