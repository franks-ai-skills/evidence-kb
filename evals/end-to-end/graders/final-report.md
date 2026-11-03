---
type: llm
weight: 1
---

PASS only if all of the following hold for the agent's final message:
1. It did not stop to ask the user for approval at any point (asking nothing is correct; the topic was given).
2. It names the knowledge base location (kb/rate-limit-responses) and summarizes coverage (topics, findings, recommendations, sources).
3. It lists the most important assumptions and the gaps, and says whether every review gate passed; if one did not pass, it says so and names the open objections.
4. It does not claim that a human reviewed or approved anything.
5. It states that nothing was committed.
