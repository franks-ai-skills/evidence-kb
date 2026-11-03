---
type: regex
pattern: '0 error\(s\)'
match: contains
target: trace
---

The agent ran `check_kb.py check` (Phase 5 runs `check --final`), and at least one run reported 0 errors. Read the trace together with final-report.md: the final report must not claim success if the last check failed.
