---
max_turns: 400
timeout_seconds: 5400
runs: 1
allowed_tools: [Read, Glob, Grep, Skill, Bash, WebSearch, WebFetch, Agent]
tags: [e2e, expensive]
---

/evidence-kb:evidence-kb Which HTTP status code and response headers should a public REST API send when a client exceeds its rate limit? --out kb/rate-limit-responses --max-rounds 2 --parallel 2
