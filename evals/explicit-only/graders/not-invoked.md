---
type: tool_used
tool: Skill
input_match: '"skill"\s*:\s*"(?:[\w-]+:)?evidence-kb"'
min: 0
max: 0
---

The skill has `disable-model-invocation: true`, so the agent must not load it on its own, even for a request that matches its purpose.
