# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/). Keep the version in
`.claude-plugin/plugin.json` and `.claude-plugin/marketplace.json` in sync.

## [1.0.0] - 2026-11-03

### Added

- `evidence-kb` skill (explicit invocation only): builds an evidence-based
  knowledge base on any topic as an Open Knowledge Format v0.2 bundle in
  five phases (brief and plan, research per topic, synthesis, audit,
  finish). It never stops for approval. Every phase passes a review gate
  run by fresh reviewer subagents, in bounded loops, and unresolved
  objections are recorded in the audit.
- Bundle profile `evidence-kb/1`: Research Brief, Research Plan, Source,
  Finding, Recommendation, Review, and Research Audit documents, with
  source metadata (publisher and source type, evidence tier, commercial
  interest, country, language, published, updated, and retrieved dates,
  verbatim quotes) and per-claim footnote attribution.
- Prompts for the researcher and for the intent, domain, fact-check,
  synthesis, and final reviewers.
- Trust rules: a document is `stable` only after an independent review
  that is newer than its last change; self-checks never make it stable.
  Review records must agree with their reports.
- Cycles for extending or resuming an existing knowledge base.
- `check_kb.py`, a dependency-free checker, with these commands:
  - `check`: OKF conformance, the profile rules, and with `--final` the
    completion rules;
  - `stats`: source mix, dating, age, and coverage tables;
  - `index`: generates the `index.md` files, writes atomically, and
    refuses to write through symlinks or into directories that are not
    evidence-kb bundles;
  - `fingerprint`: detects whether a reviewer changed files.
- Unit tests for the checker.
- Behavioral eval suite.
- Claude Code plugin and single-plugin marketplace manifests.
- Licensed under AGPL-3.0-only.
