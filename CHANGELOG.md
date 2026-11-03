# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/). Keep the version in
`.claude-plugin/plugin.json` and `.claude-plugin/marketplace.json` in sync.

## [1.1.0] - 2026-11-03

### Changed

- The skill runs without any permission prompt in Claude Code. All of its
  file work goes through one bundled script, which `allowed-tools`
  pre-approves together with `WebSearch` and `WebFetch`; `date -u` and
  `git rev-parse` are no longer pre-approved. On Claude Code 2.1.317 these
  pre-approvals also cover the subagents. The skill no longer uses the
  Write and Edit tools, `date`, `git`, or the Read tool on its own files.
- `check_kb.py` is renamed to `kb.py`. `check_kb.py` remains as a thin
  wrapper. CI scripts written for 1.0.0 keep working if they run inside
  the git checkout. **Breaking:** `check` and `stats` now refuse bundles
  outside a git repository and paths through symbolic links.
- The skill now requires a git repository. `kb.py` writes only into a
  bundle directory inside the project whose path has no hidden directories.
- Relative bundle paths are resolved from the working directory.
- Phase 0 checks the environment and every tool (once in a subagent)
  before any research. It stops there, and only there, if something is
  missing. Without a topic, it asks and asks to be invoked again.
- Writing and reviewing alternate in separate steps. Reviews are guarded
  by snapshots instead of the 1.0.0 fingerprint comparison.
- Shared Sources are extended by appending quotes, not by rewriting the
  file. Review responses are appended, too. Every heredoc gets its own
  random terminator.
- `kb.py init` adds a `.evidence-kb` marker (also to 1.0.0 bundles) and a
  `.gitignore` for `.evidence-kb-state/`.

### Added

- `kb.py` commands:
  - `init`: creates the bundle with its marker and refuses foreign
    directories;
  - `write`: confined to the bundle layout, atomic, never through
    symlinks or into instruction files, refuses broken frontmatter, new
    `human:` events, and dropped `verified` events; `--new` never
    overwrites;
  - `append`: adds text to a section;
  - `mark`: sets status, adds verified events (never `human:`), updates
    `generated.at`, and refuses to change anything else;
  - `log` (entry from stdin), `guide`, `now`, `root`, `preflight`;
  - `snapshot`, `compare`, `restore`: verified, byte-exact review
    snapshots, which also detect new or changed bundles elsewhere in the
    project.
- A lock that serializes all writes to a bundle (`fcntl.flock` on POSIX,
  so a crashed writer never leaves it behind).
- `tests/permission_check.py`: a live check that a run needs no
  permission, in the main session and in subagents, for the plugin and
  the plain-skill install.

### Fixed

- `check`, `stats`, `index`, and `fingerprint` are confined to the
  project, and refuse symbolic links.
- Validation patterns no longer accept a trailing newline.
- Frontmatter with control characters or Unicode line separators is
  refused, so that no YAML reader sees different keys than the checker.
- Absolute paths through a symlinked prefix (such as `/tmp` on macOS) are
  accepted; symbolic links inside the project are still refused.
- Deeply nested YAML is an error instead of a crash.

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
