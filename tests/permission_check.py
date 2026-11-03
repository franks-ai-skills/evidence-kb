#!/usr/bin/env python3
"""Live check: does the skill run without a single permission prompt?

This starts real Claude Code sessions (a few cents of tokens each), so it
is not part of the unit tests. Run it from the repository root after
changing SKILL.md's frontmatter or the way the skill uses tools, and after
Claude Code updates:

    python3 tests/permission_check.py            # plugin and plain skill
    python3 tests/permission_check.py plugin     # only one variant

It copies the skill, keeps its frontmatter (including `allowed-tools`)
unchanged, and replaces the body with a probe that makes every kind of
call the real skill makes, in the main session and in a subagent, with
realistic content (Markdown tables, backticks, `$`, `$(...)`, quotes,
non-ASCII text). Each session runs in a throwaway git project, in the
default permission mode, with only an empty project settings file, so
none of your own permission rules apply.

Variants:
  plugin  the skill in a throwaway plugin (--plugin-dir), as installed
          from the marketplace;
  plain   the skill copied into the project's .claude/skills/, as a plain
          skill.

The check fails if any call is denied (in the session's reported denials
or in the probe's own OK/DENIED lines), or if a file the probe should
have written is missing.

Exit status: 0 = no permission needed, 1 = something was denied or did not
happen, 2 = a session could not be run.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "skills" / "evidence-kb"
B = "docs/knowledge/probe"

CONTENT = """---
type: Source
title: "Probe: quotes, colons, and ümlauts"
description: A source with shell-like text in it.
generated: { by: probe/x, at: 2026-01-01T00:00:00Z }
---

# Evidence

> Use `bcrypt` with cost $COST; never $(rm -rf ~) or "unquoted" text's
> | a | b | table & pipes; semicolons; and && ampersands — Gänsefüßchen
"""

STEPS = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11", "12",
         "13", "14", "15a", "15b", "15c", "15d", "15e", "15f", "15g", "15h",
         "15i", "16"]

PROBE = """
This is a permission probe. Do each step exactly, never ask anything, and
keep going if a step is denied. KB is a placeholder for this command,
which you always write out in full (never as a shell variable, never with
cd, never piped):
python3 ${CLAUDE_SKILL_DIR}/scripts/kb.py

1. Run `KB preflight %(b)s` and note the absolute `bundle:` path.
2. Run `KB init %(b)s`.
3. Run `KB guide format` (only note whether it printed something).
4. Run `KB now`.
5. Write this exact content with a quoted heredoc (the terminator at the
   start of its line):
KB write %(b)s references/main-probe.md --new <<'KB_EOF_9c3e1a7b'
%(content)sKB_EOF_9c3e1a7b
6. Append with a heredoc: `KB append %(b)s references/main-probe.md
   --section Notes` with the text `Note with `code` and $HOME.`
7. Run `KB mark %(b)s references/main-probe.md --status draft --touch`.
8. Run `KB mark %(b)s references/main-probe.md --verified-by
   evidence-kb-reviewer/probe`.
9. Log with a heredoc: `KB log %(b)s --kind Probe` with the text
   `main session: "quoted" `ticks` $(not run)`.
10. Run `KB snapshot %(b)s` and keep the id.
11. Use the Read tool on `%(b)s/references/main-probe.md`. Then search
    for "Probe" in `%(b)s` and list its .md files: with the Grep and Glob
    tools if you have them, otherwise with plain `grep -rn` and `find`
    commands (no pipes).
12. Run `KB index %(b)s` and `KB stats %(b)s`.
13. Use WebSearch for "IANA example domain".
14. Use WebFetch on https://example.com.
15. Start ONE general-purpose subagent with the Agent tool. Give it the
    exact KB command with the absolute skill path, and the absolute bundle
    path from step 1, and tell it to do, without asking, each with the
    command written out in full: (a) `KB guide prompts`; (b) `KB now`;
    (c) write references/sub-probe.md with --new and a quoted heredoc,
    with the same content as in step 5 but the title "Sub probe";
    (d) append to it, section Evidence, the text `> sub quote with $VAR`;
    (e) `KB check <bundle>`; (f) Read references/sub-probe.md; (g) search
    for "Sub probe" in the bundle (Grep tool if available, otherwise a
    plain `grep -rn` command); (h) WebSearch "IANA example domain";
    (i) WebFetch https://example.org. It reports each as OK or DENIED,
    with the error text for DENIED.
16. Run `KB compare %(b)s <id from step 10>` (it should list the
    subagent's file).
Final answer: one line per step, exactly in the form `<step>: OK` or
`<step>: DENIED <error>`, for the steps %(steps)s. DENIED means that a
permission was denied or asked for; a tool that doesn't exist is not
DENIED if the fallback worked. End with the line
PROBE-DONE.
""" % {"b": B, "content": CONTENT, "steps": ", ".join(STEPS)}


def skill_copy(target: Path):
    shutil.copytree(SKILL, target)
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    end = text.index("\n---\n", 4) + len("\n---\n")
    (target / "SKILL.md").write_text(text[:end] + PROBE, encoding="utf-8")


def build(tmp: Path, variant: str):
    project = tmp / "project"
    (project / ".claude").mkdir(parents=True)
    (project / ".claude" / "settings.json").write_text("{}\n")
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    cmd = ["claude", "-p", "", "--setting-sources", "project",
           "--permission-mode", "default", "--output-format", "json"]
    if variant == "plugin":
        plugin = tmp / "plugin"
        (plugin / ".claude-plugin").mkdir(parents=True)
        (plugin / ".claude-plugin" / "plugin.json").write_text(json.dumps(
            {"name": "permprobe", "version": "0.0.0",
             "description": "evidence-kb permission probe"}))
        skill_copy(plugin / "skills" / "evidence-kb")
        cmd[2] = "/permprobe:evidence-kb"
        cmd += ["--plugin-dir", str(plugin)]
    else:
        skill_copy(project / ".claude" / "skills" / "evidence-kb")
        cmd[2] = "/evidence-kb"
    return project, cmd


def run(variant: str) -> int:
    tmp = Path(tempfile.mkdtemp(prefix="evidence-kb-permcheck-"))
    try:
        project, cmd = build(tmp, variant)
        try:
            proc = subprocess.run(cmd, cwd=project, stdin=subprocess.DEVNULL,
                                  capture_output=True, text=True,
                                  timeout=1500)
        except subprocess.TimeoutExpired:
            print("[%s] error: the session timed out" % variant,
                  file=sys.stderr)
            return 2
        try:
            result = json.loads(proc.stdout)
        except json.JSONDecodeError:
            print("[%s] error: no JSON result\n%s" % (variant,
                                                      proc.stderr[-2000:]),
                  file=sys.stderr)
            return 2
        text = result.get("result", "") or ""
        print("[%s] %s" % (variant, text))
        failures = []
        if result.get("num_turns", 0) == 0:
            failures.append("the skill did not start (0 turns)")
        for d in result.get("permission_denials") or []:
            failures.append("denied: %s %s" % (
                d.get("tool_name"), json.dumps(d.get("tool_input"))[:200]))
        for step in STEPS:
            m = re.search(r"(?m)^\W*%s\W*:\s*(OK|DENIED)" % re.escape(step),
                          text)
            if not m:
                failures.append("step %s: no OK/DENIED line" % step)
            elif m.group(1) != "OK":
                failures.append("step %s: DENIED" % step)
        if "PROBE-DONE" not in text:
            failures.append("probe did not finish")
        bundle = project / B
        for rel in ("references/main-probe.md", "references/sub-probe.md",
                    "log.md", "index.md"):
            if not (bundle / rel).is_file():
                failures.append("missing file: %s" % rel)
        main_probe = bundle / "references" / "main-probe.md"
        if main_probe.is_file():
            body = main_probe.read_text(encoding="utf-8")
            for needle in ("$(rm -rf ~)", "`bcrypt`", "Gänsefüßchen",
                           "$HOME", "evidence-kb-reviewer/probe"):
                if needle not in body:
                    failures.append("main-probe.md lost %r" % needle)
        for f in failures:
            print("[%s] FAIL: %s" % (variant, f), file=sys.stderr)
        if failures:
            return 1
        print("[%s] PASS: no permission was needed" % variant)
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv) -> int:
    if shutil.which("claude") is None:
        print("error: the claude CLI is not on PATH", file=sys.stderr)
        return 2
    variants = argv or ["plugin", "plain"]
    bad = [v for v in variants if v not in ("plugin", "plain")]
    if bad:
        print("error: unknown variant %s" % ", ".join(bad), file=sys.stderr)
        return 2
    return max(run(v) for v in variants)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
