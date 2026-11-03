---
type: regex
pattern: '(?<![0-9])0 error\(s\)'
match: contains
target: trace
---

The agent ran `kb.py check` and at least one run reported exactly 0 errors. Read it together with final-report.md: the final report must not claim success if the last check failed.
