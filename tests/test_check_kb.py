"""Tests for skills/evidence-kb/scripts/check_kb.py.

Run from the repository root:  python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "skills" / "evidence-kb" / "scripts" / "check_kb.py"

_spec = importlib.util.spec_from_file_location("check_kb", SCRIPT)
check_kb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_kb)

TODAY = dt.date(2026, 11, 3)
GEN = "generated: { by: evidence-kb/claude-opus-5-5, at: 2026-11-03T10:00:00Z }"
VER = ("verified: { by: evidence-kb-reviewer/claude-opus-5-5, "
       "at: 2026-11-03T11:00:00Z }")


def d(text: str) -> str:
    return textwrap.dedent(text).lstrip("\n")


BRIEF = d("""
    ---
    type: Research Brief
    title: "Brief: retry policies"
    description: Goal, context, scope, and assumptions.
    kb_profile: evidence-kb/1
    %s
    ---

    # Request

    > Research retry policies for our HTTP client.

    # Goal

    Choose a retry policy.

    # Possible readings

    1. Client-side retries (chosen).

    # Context

    Library used worldwide.

    # Scope

    Client retries only.

    # Assumptions

    | ID | Assumption |
    |---|---|
    | A1 | Idempotent requests only. |

    # How to use this knowledge base

    Cite recommendation paths in pull requests.
    """ % GEN)

PLAN = d("""
    ---
    type: Research Plan
    title: Research plan
    description: Topics, questions, and source strategy.
    %s
    decisions:
      - { id: D1, title: "Which requests to retry" }
    topics:
      - id: T1
        title: Retry semantics
        priority: must
        decisions: [D1]
        max_age_months: 120
        questions:
          - { id: T1.1, text: "Which methods are idempotent?" }
          - id: T1.2
            text: >-
              When may a client
              retry automatically?
      - id: T2
        title: Backoff
        priority: nice
        decisions: [D1]
        questions:
          - { id: T2.1, text: "Which backoff is recommended?" }
    ---

    # Topic map

    T1 (T1.1, T1.2) and T2 (T2.1).

    # Source strategy

    Standards first.

    # Coverage matrix

    | Topic | standard |
    |---|---|
    | T1 | expected |

    # Risks and stop conditions

    Vendor bias.
    """ % GEN)

SOURCE = d("""
    ---
    type: Source
    title: "RFC 9110: HTTP Semantics"
    description: IETF standard that defines HTTP semantics.
    resource: https://www.rfc-editor.org/rfc/rfc9110
    publisher: IETF
    publisher_type: standards-body
    source_type: standard
    evidence_tier: primary
    commercial_interest: false
    country: INT
    language: en
    published: 2022-06
    updated: unknown
    retrieved: 2026-11-03T10:20:00Z
    access: open
    status: stable
    %s
    %s
    ---

    # Evidence

    > A request method is considered "idempotent" if the intended effect
    > on the server of multiple identical requests ... is the same.
    > (Section 9.2.2)
    """ % (GEN, VER))

VENDOR = d("""
    ---
    type: Source
    title: Vendor retry guide
    description: A vendor's guide to retries in its SDK.
    resource: https://vendor.example.com/docs/retries
    publisher: Example Corp
    publisher_type: company
    source_type: vendor
    evidence_tier: secondary
    original: /references/rfc-9110.md
    commercial_interest: true
    commercial_interest_note: Sells the SDK it recommends.
    country: US
    language: en
    published: unknown
    updated: 2025
    retrieved: 2026-11-03T10:25:00Z
    access: open
    status: stable
    %s
    %s
    ---

    # Evidence

    > Retry idempotent requests up to three times.
    """ % (GEN, VER))

FINDING = d("""
    ---
    type: Finding
    title: Which methods are idempotent
    description: GET, HEAD, OPTIONS, TRACE, PUT, and DELETE are idempotent.
    topic: T1
    question: [T1.1, T1.2]
    confidence: high
    stale_after: 2036-11-03T10:00:00Z
    status: stable
    %s
    verified:
      - { by: evidence-kb-reviewer/claude-opus-5-5, at: 2026-11-03T11:00:00Z }
    sources:
      - { id: rfc-9110, resource: /references/rfc-9110.md, title: RFC 9110 }
      - id: vendor-retries
        resource: ../../references/vendor-retries.md
        title: Vendor retry guide
    ---

    # Answer

    Safe methods plus PUT and DELETE are idempotent.[^rfc-9110]

    - Clients may retry them automatically.[^rfc-9110][^vendor-retries]
    - **Inference:** POST should not be retried without a key.

    | Claim | Source |
    |---|---|
    | PUT is idempotent[^rfc-9110] | standard |

    ```text
    Code is not a claim.
    ```

    [^rfc-9110]: RFC 9110, Section 9.2.2
    [^vendor-retries]: Vendor retry guide
    """ % GEN)

RECOMMENDATION = d("""
    ---
    type: Recommendation
    title: Retry only idempotent requests
    description: Automatic retries are limited to idempotent methods.
    strength: must
    decisions: [D1]
    based_on: [/findings/t1-retry-semantics/idempotent-methods.md]
    status: stable
    %s
    %s
    sources:
      - { id: rfc-9110, resource: /references/rfc-9110.md }
    ---

    # Recommendation

    Retry automatically only GET, HEAD, OPTIONS, TRACE, PUT, DELETE.

    # Rationale

    These methods are idempotent by definition.[^rfc-9110]

    # How to check

    The retry predicate rejects POST and PATCH; a test covers it.

    [^rfc-9110]: RFC 9110
    """ % (GEN, VER))


REVIEWED = {
    "plan": "[/brief.md, /plan.md]",
    "research:T1": "[/findings/t1-retry-semantics/]",
    "synthesis": "[/recommendations/]",
    "final": "[/]",
}


def review(gate, rnd, verdict, perspective=None, objections="None.",
           cycle=None):
    extra = ""
    if perspective:
        extra += "\nperspective: %s" % perspective
    if cycle:
        extra += "\ncycle: %d" % cycle
    return d("""
        ---
        type: Review
        title: "%s review, round %d"
        description: Independent review of the %s gate.
        gate: "%s"%s
        round: %d
        verdict: %s
        reviewed: %s
        generated: { by: evidence-kb-reviewer/claude-opus-5-5, at: 2026-11-03T10:10:00Z }
        ---

        # Verdict

        %s
        Reviewed.

        # Objections

        """ % (gate, rnd, gate, gate, extra.replace("\n", "\n        "),
               rnd, verdict, REVIEWED.get(gate, "[/plan.md]"), verdict)
        ) + objections + "\n"


AUDIT = d("""
    ---
    type: Research Audit
    title: Audit
    description: Coverage, source mix, and gaps compared with the plan.
    %s
    gaps:
      - { id: T2, reason: "Out of budget.", to_close: "Research backoff." }
    unresolved: []
    ---

    # Coverage

    T1 complete.

    # Source mix

    Table from stats.

    # Gaps and weak spots

    T2 not researched.

    # Unresolved objections

    None.

    # Review independence

    Fresh subagents for every gate.
    """ % GEN)

LOG = d("""
    # Log

    ## 2026-11-03

    * **Initialization**: Started the knowledge base.

    ## 2026-11-02

    * **Note**: Earlier entry.
    """)


def base_files():
    return {
        "brief.md": BRIEF,
        "plan.md": PLAN,
        "references/rfc-9110.md": SOURCE,
        "references/vendor-retries.md": VENDOR,
        "findings/t1-retry-semantics/idempotent-methods.md": FINDING,
        "recommendations/retry-idempotent-only.md": RECOMMENDATION,
        "reviews/plan-intent-r1.md": review("plan", 1, "pass", "intent"),
        "reviews/plan-domain-r1.md": review("plan", 1, "revise", "domain"),
        "reviews/plan-domain-r2.md": review("plan", 2, "pass", "domain"),
        "reviews/research-t1-r1.md": review("research:T1", 1, "revise"),
        "reviews/research-t1-r2.md": review("research:T1", 2, "pass"),
        "reviews/synthesis-r1.md": review("synthesis", 1, "pass"),
        "reviews/final-r1.md": review("final", 1, "pass"),
        "audit.md": AUDIT,
        "log.md": LOG,
    }


class BundleCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="evidence-kb-test-"))
        self.addCleanup(shutil.rmtree, self.tmp)

    def write(self, files):
        for rel, text in files.items():
            path = self.tmp / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")

    def make(self, overrides=None, remove=(), index=True):
        files = base_files()
        files.update(overrides or {})
        for rel in remove:
            files.pop(rel, None)
        self.write(files)
        if index:
            self.run_cli("index", str(self.tmp))

    def problems(self, final=False):
        bundle = check_kb.load_bundle(self.tmp)
        check_kb.Checker(bundle, TODAY, final=final).run()
        return bundle.problems

    def errors(self, final=False):
        return [str(p) for p in self.problems(final) if p.level == "error"]

    def warnings(self, final=False):
        return [str(p) for p in self.problems(final)
                if p.level == "warning"]

    def assertError(self, fragment, final=False):
        errs = self.errors(final)
        self.assertTrue(any(fragment in e for e in errs),
                        "no error containing %r in:\n%s"
                        % (fragment, "\n".join(errs)))

    def assertWarning(self, fragment, final=False):
        warns = self.warnings(final)
        self.assertTrue(any(fragment in w for w in warns),
                        "no warning containing %r in:\n%s"
                        % (fragment, "\n".join(warns)))

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = check_kb.main(list(args))
        return code, out.getvalue(), err.getvalue()


# --------------------------------------------------------------------------

class YamlSubset(unittest.TestCase):
    def parse(self, text):
        return check_kb.parse_yaml(d(text))[0]

    def test_scalars(self):
        data = self.parse("""
            a: plain text
            b: "double \\"quoted\\""
            c: 'single ''quoted'''
            d: 42
            e: true
            f: ~
            g: 2024-05-01
            h: 2024
            i: value # comment
            j: https://example.com/a#frag
            """)
        self.assertEqual(data["a"], "plain text")
        self.assertEqual(data["b"], 'double "quoted"')
        self.assertEqual(data["c"], "single 'quoted'")
        self.assertEqual(data["d"], 42)
        self.assertIs(data["e"], True)
        self.assertIsNone(data["f"])
        self.assertEqual(data["g"], "2024-05-01")
        self.assertEqual(data["h"], 2024)
        self.assertEqual(data["i"], "value")
        self.assertEqual(data["j"], "https://example.com/a#frag")

    def test_flow_collections(self):
        data = self.parse("""
            a: [x, "y, z", 3]
            b: { by: agent/v1, at: 2026-01-01T00:00:00Z }
            c: [{ id: T1.1, text: "A: b" }, { id: T1.2 }]
            d: []
            e: {}
            """)
        self.assertEqual(data["a"], ["x", "y, z", 3])
        self.assertEqual(data["b"], {"by": "agent/v1",
                                     "at": "2026-01-01T00:00:00Z"})
        self.assertEqual(data["c"], [{"id": "T1.1", "text": "A: b"},
                                     {"id": "T1.2"}])
        self.assertEqual(data["d"], [])
        self.assertEqual(data["e"], {})

    def test_multiline_flow(self):
        data = self.parse("""
            a: [one,
                two]
            b: after
            """)
        self.assertEqual(data["a"], ["one", "two"])
        self.assertEqual(data["b"], "after")

    def test_block_collections(self):
        data = self.parse("""
            topics:
              - id: T1
                questions:
                  - { id: T1.1, text: q }
                  - id: T1.2
                    text: r
              - id: T2
            same_indent:
            - a
            - b
            nested:
              key:
                deeper: 1
            """)
        self.assertEqual(data["topics"][0]["id"], "T1")
        self.assertEqual(data["topics"][0]["questions"][1],
                         {"id": "T1.2", "text": "r"})
        self.assertEqual(data["topics"][1], {"id": "T2"})
        self.assertEqual(data["same_indent"], ["a", "b"])
        self.assertEqual(data["nested"], {"key": {"deeper": 1}})

    def test_block_scalars(self):
        data = self.parse("""
            folded: >-
              one
              two

              three
            literal: |
              line 1
                indented
            keep: |+
              x

            after: done
            """)
        self.assertEqual(data["folded"], "one two\nthree")
        self.assertEqual(data["literal"], "line 1\n  indented\n")
        self.assertEqual(data["keep"], "x\n\n")
        self.assertEqual(data["after"], "done")

    def test_plain_continuation(self):
        data = self.parse("""
            description: a long
              wrapped value
            next: 1
            """)
        self.assertEqual(data["description"], "a long wrapped value")

    def test_key_lines(self):
        _, lines = check_kb.parse_yaml("a: 1\n\nb:\n  - x\nc: 2", 2)
        self.assertEqual(lines, {"a": 2, "b": 4, "c": 6})

    def test_errors(self):
        cases = {
            "a: 1\na: 2": "duplicate key",
            "a: &x 1": "unsupported",
            "a: *x": "unsupported",
            "a: !!str 1": "unsupported",
            "a: b: c": "put the value in quotes",
            "a: [1, 2": "unterminated",
            "a: \"open": "unterminated",
            "a:\n\t- x": "tab",
            "- a": "must be a mapping",
            "a:\n  b: 1\n c: 2": "unexpected indentation",
            "a: 1\n  b: 2": "put the value in quotes",
            "just text": "expected 'key: value'",
            "a: { b }": "expected ':'",
        }
        for text, fragment in cases.items():
            with self.subTest(text=text):
                with self.assertRaises(check_kb.YamlError) as ctx:
                    check_kb.parse_yaml(text)
                self.assertIn(fragment, ctx.exception.message)

    def test_split_frontmatter(self):
        fm, body, line = check_kb.split_frontmatter("---\na: 1\n---\nbody\n")
        self.assertEqual((fm, body, line), ("a: 1", "body\n", 4))
        self.assertEqual(check_kb.split_frontmatter("no fm")[0], None)
        with self.assertRaises(check_kb.YamlError):
            check_kb.split_frontmatter("---\na: 1\n")


class DateHelpers(unittest.TestCase):
    def test_partial_dates(self):
        self.assertEqual(check_kb.partial_date_end("2024"),
                         dt.date(2024, 12, 31))
        self.assertEqual(check_kb.partial_date_end("2024-02"),
                         dt.date(2024, 2, 29))
        self.assertEqual(check_kb.partial_date_end("2024-12"),
                         dt.date(2024, 12, 31))
        self.assertIsNone(check_kb.partial_date_end("2024-13"))
        self.assertIsNone(check_kb.partial_date_end("2024-02-30"))
        self.assertIsNone(check_kb.partial_date_end("May 2024"))

    def test_timestamps(self):
        self.assertIsNotNone(check_kb.parse_timestamp("2026-01-01T00:00:00Z"))
        self.assertIsNotNone(
            check_kb.parse_timestamp("2026-01-01T00:00+02:00"))
        self.assertIsNone(check_kb.parse_timestamp("2026-01-01"))
        self.assertIsNone(check_kb.parse_timestamp("2026-01-01T00:00:00"))
        self.assertIsNone(check_kb.parse_timestamp("2026-02-30T00:00:00Z"))


# --------------------------------------------------------------------------

class ValidBundle(BundleCase):
    def test_valid_bundle_has_no_problems(self):
        self.make()
        self.assertEqual([str(p) for p in self.problems(final=True)], [])

    def test_cli_exit_codes(self):
        self.make()
        code, _, err = self.run_cli("check", str(self.tmp), "--final",
                                    "--strict", "--today", "2026-11-03")
        self.assertEqual(code, 0, err)
        code, out, _ = self.run_cli("check", str(self.tmp), "--json",
                                    "--today", "2026-11-03")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["errors"], 0)

    def test_usage_errors_exit_2(self):
        self.assertEqual(self.run_cli("check", str(self.tmp / "nope"))[0], 2)
        self.assertEqual(self.run_cli()[0], 2)
        self.assertEqual(self.run_cli("bogus")[0], 2)
        self.assertEqual(self.run_cli("check", str(self.tmp), "--today",
                                      "03.11.2026")[0], 2)

    def test_runs_as_script(self):
        self.make()
        proc = subprocess.run(
            [sys.executable, "-I", str(SCRIPT), "check", str(self.tmp),
             "--today", "2026-11-03"], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("0 error(s)", proc.stderr)

    def test_unknown_types_and_keys_are_tolerated(self):
        self.make({"notes/extra.md": "---\ntype: Glossary\ntitle: G\n"
                   "description: d\ncustom_key: 1\n---\n\nText.\n"})
        errs = self.errors()
        self.assertEqual(errs, [])
        self.assertWarning("not part of the evidence-kb profile")


class OkfConformance(BundleCase):
    def test_concept_without_frontmatter(self):
        self.make({"findings/t1-retry-semantics/bare.md": "# Just text\n"})
        self.assertError("has no YAML frontmatter")

    def test_concept_without_type(self):
        self.make({"references/x.md": "---\ntitle: X\n---\n"})
        self.assertError("no non-empty 'type'")

    def test_unparseable_frontmatter_reports_line(self):
        self.make({"references/x.md": "---\ntype: Source\ntitle: a: b\n---\n"})
        self.assertError("references/x.md:3: error: frontmatter")

    def test_unclosed_frontmatter(self):
        self.make({"references/x.md": "---\ntype: Source\n"})
        self.assertError("never closed")

    def test_index_frontmatter_rules(self):
        self.make()
        (self.tmp / "reviews/index.md").write_text("---\na: 1\n---\n# R\n")
        (self.tmp / "index.md").write_text(
            '---\nokf_version: "0.2"\nextra: 1\n---\n# S\n')
        self.assertError("below the bundle root must not have frontmatter")
        self.assertError("may only contain okf_version")

    def test_log_rules(self):
        self.make({"log.md": "# Log\n\n## 2026-11-01\n\nx\n\n## 2026-11-02\n"
                   "\ny\n\n## Nov 3\n\nz\n\n## 2027-01-01\n"})
        self.assertError("newest first")
        self.assertError("not an ISO 8601")
        self.assertError("in the future")

    def test_broken_and_escaping_links_warn(self):
        self.make({"brief.md": BRIEF.replace(
            "Choose a retry policy.",
            "Choose [x](/nope.md), see [y](../../../etc/passwd), "
            "`[z](ignored.md)`, [w](https://example.com).")})
        self.assertWarning("broken link '/nope.md'")
        self.assertWarning("points outside the bundle")
        self.assertFalse(any("ignored.md" in w for w in self.warnings()))


class ProfileRules(BundleCase):
    def test_misplaced_documents(self):
        self.make({"findings/stray.md": FINDING,
                   "plan2.md": PLAN,
                   "notes/source.md": SOURCE.replace(
                       "rfc9110", "rfc9111")})
        self.assertError("findings/stray.md:1: error: a Finding belongs in")
        self.assertError("belongs at the bundle root as plan.md")
        self.assertError("a Source belongs in references/")

    def test_missing_brief_and_plan(self):
        self.make(remove=("brief.md", "plan.md"))
        self.assertError("missing brief.md")
        self.assertError("missing plan.md")

    def test_brief_profile_and_sections(self):
        self.make({"brief.md": BRIEF.replace("kb_profile: evidence-kb/1\n", "")
                   .replace("# Scope\n\nClient retries only.\n", "")})
        self.assertError("kb_profile must be")
        self.assertError("missing section '# Scope'")

    def test_empty_section(self):
        self.make({"brief.md": BRIEF.replace("Client retries only.\n", "")})
        self.assertError("section '# Scope' is empty")

    def test_plan_rules(self):
        bad = PLAN.replace("priority: nice", "priority: optional") \
            .replace("decisions: [D1]\n    max_age", "decisions: [D9]\n"
                     "    max_age").replace("id: T1.2", "id: T2.9") \
            .replace("max_age_months: 120", "max_age_months: -1")
        self.make({"plan.md": bad})
        self.assertError("priority must be must or nice")
        self.assertError("unknown decision 'D9'")
        self.assertError("must look like T1.1")
        self.assertError("max_age_months must be a positive integer")

    def test_plan_ids_must_appear_in_body(self):
        self.make({"plan.md": PLAN.replace("T1 (T1.1, T1.2) and T2 (T2.1).",
                                           "Topics.")})
        self.assertWarning("question T1.1 is not mentioned")

    def test_generated_and_verified(self):
        f = FINDING.replace("by: evidence-kb/claude-opus-5-5, at: "
                            "2026-11-03T10:00:00Z",
                            "by: some agent, at: 2026-11-03")
        f = f.replace("at: 2026-11-03T11:00:00Z", "at: 2027-11-03T11:00:00Z")
        self.make({"findings/t1-retry-semantics/idempotent-methods.md": f})
        self.assertError("is not an actor")
        self.assertError("generated.at must be an ISO 8601 datetime")
        self.assertError("verified.at 2027-11-03T11:00:00Z is in the future")

    def test_missing_status_and_generated(self):
        f = FINDING.replace("status: stable\n", "") \
            .replace(GEN + "\n", "")
        self.make({"findings/t1-retry-semantics/idempotent-methods.md": f})
        self.assertError("missing 'status'")
        self.assertError("missing 'generated")

    def test_stale_concept_warns(self):
        f = FINDING.replace("stale_after: 2036-11-03T10:00:00Z",
                            "stale_after: 2026-01-01T00:00:00Z")
        self.make({"findings/t1-retry-semantics/idempotent-methods.md": f})
        self.assertWarning("concept is stale")

    def test_source_required_fields_and_values(self):
        s = SOURCE.replace("published: 2022-06\n", "") \
            .replace("publisher_type: standards-body", "publisher_type: ngo") \
            .replace("country: INT", "country: Germany") \
            .replace("language: en", "language: English") \
            .replace("access: open", "access: free") \
            .replace("resource: https://www.rfc-editor.org/rfc/rfc9110",
                     "resource: rfc9110.html")
        self.make({"references/rfc-9110.md": s})
        self.assertError("Source needs 'published' (write \"unknown\"")
        self.assertError("publisher_type must be one of")
        self.assertError("country 'Germany' is not an ISO 3166-1 alpha-2 code")
        self.assertError("language 'English' is not valid")
        self.assertError("access must be one of")
        self.assertError("resource must be an absolute URI")

    def test_source_dates(self):
        s = SOURCE.replace("published: 2022-06", "published: 2027-01") \
            .replace("updated: unknown", "updated: 2021") \
            .replace("retrieved: 2026-11-03T10:20:00Z", "retrieved: today")
        self.make({"references/rfc-9110.md": s})
        self.assertError("published 2027-01 is in the future")
        self.assertError("updated is before published")
        self.assertError("retrieved must be an ISO 8601 datetime")

    def test_bad_partial_date(self):
        s = SOURCE.replace("published: 2022-06", "published: June 2022")
        self.make({"references/rfc-9110.md": s})
        self.assertError("must be YYYY, YYYY-MM, YYYY-MM-DD, or unknown")

    def test_commercial_interest_needs_note(self):
        s = VENDOR.replace("commercial_interest_note: Sells the SDK it "
                           "recommends.\n", "")
        self.make({"references/vendor-retries.md": s})
        self.assertError("needs commercial_interest_note")

    def test_secondary_without_original_warns(self):
        s = VENDOR.replace("original: /references/rfc-9110.md\n", "")
        self.make({"references/vendor-retries.md": s})
        self.assertWarning("should name its original")

    def test_source_needs_verbatim_quote(self):
        s = SOURCE.split("# Evidence")[0] + "# Evidence\n\nParaphrase.\n"
        self.make({"references/rfc-9110.md": s})
        self.assertError("needs at least one verbatim quote")

    def test_duplicate_source_resource(self):
        dup = SOURCE.replace("rfc9110\n", "rfc9110/\n")
        self.make({"references/rfc-9110-copy.md": dup})
        self.assertError("same resource as")

    def test_orphan_source_warns(self):
        orphan = SOURCE.replace("rfc9110", "rfc9111")
        self.make({"references/rfc-9111.md": orphan})
        self.assertWarning("references/rfc-9111.md:1: warning: source is "
                           "not cited")

    def test_finding_rules(self):
        f = FINDING.replace("topic: T1", "topic: T7") \
            .replace("confidence: high", "confidence: certain")
        self.make({"findings/t1-retry-semantics/idempotent-methods.md": f})
        self.assertError("topic 'T7' is not in the plan")
        self.assertError("confidence must be one of")

    def test_finding_question_must_belong_to_topic(self):
        f = FINDING.replace("question: [T1.1, T1.2]", "question: T2.1")
        self.make({"findings/t1-retry-semantics/idempotent-methods.md": f})
        self.assertError("question T2.1 belongs to topic T2")

    def test_finding_directory_must_match_topic(self):
        self.make({"findings/t2-backoff/idempotent-methods.md": FINDING})
        self.assertError("belongs in findings/t1-<slug>/")

    def test_claims_need_footnote_or_marker(self):
        f = FINDING.replace("- **Inference:** POST should not be retried "
                            "without a key.",
                            "- POST should not be retried.\n"
                            "- Gap: no data on PATCH.")
        self.make({"findings/t1-retry-semantics/idempotent-methods.md": f})
        errs = [e for e in self.errors() if "has no source footnote" in e]
        self.assertEqual(len(errs), 1, errs)

    def test_unsourced_table_row(self):
        f = FINDING.replace("| PUT is idempotent[^rfc-9110] | standard |",
                            "| PUT is idempotent[^rfc-9110] | standard |\n"
                            "| DELETE is idempotent | standard |")
        self.make({"findings/t1-retry-semantics/idempotent-methods.md": f})
        self.assertError("has no source footnote")

    def test_footnote_and_source_consistency(self):
        f = FINDING.replace("[^rfc-9110]: RFC 9110, Section 9.2.2",
                            "[^unused]: x") \
            .replace("automatically.[^rfc-9110]", "automatically.[^ghost]")
        self.make({"findings/t1-retry-semantics/idempotent-methods.md": f})
        self.assertError("footnote [^ghost] does not match any sources id")
        self.assertError("footnote [^rfc-9110] has no definition")
        self.assertWarning("footnote [^unused] is defined but never used")

    def test_sources_must_resolve_to_source_records(self):
        f = FINDING.replace("resource: /references/rfc-9110.md",
                            "resource: https://www.rfc-editor.org/rfc/rfc9110")
        f2 = FINDING.replace("resource: /references/rfc-9110.md",
                             "resource: /references/missing.md")
        self.make({"findings/t1-retry-semantics/idempotent-methods.md": f,
                   "findings/t1-retry-semantics/other.md": f2})
        self.assertError("cite external material through a Source record")
        self.assertError("does not resolve to a Source document")

    def test_source_id_must_match_file_name(self):
        f = FINDING.replace("id: rfc-9110,", "id: rfc,") \
            .replace("[^rfc-9110]", "[^rfc]")
        self.make({"findings/t1-retry-semantics/idempotent-methods.md": f})
        self.assertError("must equal the Source file name 'rfc-9110'")

    def test_finding_needs_sources(self):
        f = FINDING.split("sources:")[0] + "---\n\n# Answer\n\nGap: none.\n"
        self.make({"findings/t1-retry-semantics/idempotent-methods.md": f})
        self.assertError("Finding needs at least one entry in 'sources'")

    def test_recommendation_needs_independent_source(self):
        r = RECOMMENDATION.replace(
            "{ id: rfc-9110, resource: /references/rfc-9110.md }",
            "{ id: vendor-retries, resource: /references/vendor-retries.md }"
        ).replace("[^rfc-9110]", "[^vendor-retries]")
        self.make({"recommendations/retry-idempotent-only.md": r})
        self.assertError("at least one source with commercial_interest: "
                         "false")
        self.assertError("strength 'must' needs at least one primary-tier")

    def test_recommendation_links(self):
        r = RECOMMENDATION.replace("decisions: [D1]", "decisions: [D4]") \
            .replace("/findings/t1-retry-semantics/idempotent-methods.md",
                     "/findings/missing.md") \
            .replace("strength: must", "strength: required")
        self.make({"recommendations/retry-idempotent-only.md": r})
        self.assertError("decision 'D4' is not in the plan")
        self.assertError("does not resolve to a Finding")
        self.assertError("strength must be one of")

    def test_review_rules(self):
        self.make({"reviews/x.md": review("research:T9", 0, "ok")})
        self.assertError("names a topic that is not in the plan")
        self.assertError("round must be an integer >= 1")
        self.assertError("verdict must be pass or revise")

    def test_audit_rules(self):
        a = AUDIT.replace('{ id: T2, reason: "Out of budget.", to_close: '
                          '"Research backoff." }',
                          "{ id: T5, reason: x, to_close: y }\n"
                          "  - { id: T1 }") \
            .replace("unresolved: []", "unresolved: [{ gate: misc }]")
        self.make({"audit.md": a})
        self.assertError("gap 'T5' is not a topic or question id")
        self.assertError("each gap needs id, reason, and to_close")
        self.assertError("each unresolved entry needs gate and summary")


class FinalMode(BundleCase):
    def test_audit_required_only_in_final(self):
        self.make(remove=("audit.md",))
        self.assertFalse(any("audit.md" in e for e in self.errors()))
        self.assertError("missing audit.md", final=True)

    def test_must_question_without_finding(self):
        f = FINDING.replace("question: [T1.1, T1.2]", "question: T1.1")
        self.make({"findings/t1-retry-semantics/idempotent-methods.md": f})
        self.assertError("must-have question T1.2 has no Finding", final=True)

    def test_gap_declaration_covers_question(self):
        f = FINDING.replace("question: [T1.1, T1.2]", "question: T1.1")
        a = AUDIT.replace("unresolved: []",
                          "unresolved: []").replace(
            '{ id: T2, reason: "Out of budget.", to_close: "Research '
            'backoff." }',
            '{ id: T1.2, reason: "No source found.", to_close: "Ask '
            'maintainers." }')
        self.make({"findings/t1-retry-semantics/idempotent-methods.md": f,
                   "audit.md": a})
        self.assertEqual(self.errors(final=True), [])

    def test_failed_gate_needs_unresolved_entry(self):
        self.make({"reviews/synthesis-r2.md": review("synthesis", 2,
                                                     "revise")})
        self.assertError("last round (2) of gate synthesis did not pass",
                         final=True)
        a = AUDIT.replace("unresolved: []", "unresolved:\n  - { gate: "
                          "synthesis, summary: \"Strength disputed.\" }")
        self.make({"reviews/synthesis-r2.md": review("synthesis", 2,
                                                     "revise"),
                   "audit.md": a})
        self.assertEqual(self.errors(final=True), [])

    def test_missing_gate(self):
        self.make(remove=("reviews/research-t1-r1.md",
                          "reviews/research-t1-r2.md"))
        self.assertError("gate research:T1 has no Review record", final=True)

    def test_duplicate_round(self):
        self.make({"reviews/plan-intent-r1b.md": review("plan", 1, "pass",
                                                        "intent")})
        self.assertError("gate plan (intent) has 2 reviews for round 1")

    def test_plan_gate_needs_both_perspectives(self):
        self.make(remove=("reviews/plan-intent-r1.md",))
        self.assertError("gate plan (intent) has no Review record",
                         final=True)

    def test_plan_review_needs_perspective(self):
        self.make({"reviews/plan-x-r1.md": review("plan", 3, "pass")})
        self.assertError("needs perspective: domain or intent")

    def test_failed_perspective_fails_gate(self):
        self.make({"reviews/plan-domain-r3.md": review("plan", 3, "revise",
                                                       "domain")})
        self.assertError("last round (3) of gate plan (domain) did not pass",
                         final=True)

    def test_stable_needs_verified(self):
        r = RECOMMENDATION.replace(VER + "\n", "")
        self.make({"recommendations/retry-idempotent-only.md": r})
        self.assertError("status: stable needs a 'verified' entry",
                         final=True)

    def test_draft_without_verified_is_fine(self):
        r = RECOMMENDATION.replace(VER + "\n", "").replace(
            "status: stable", "status: draft")
        self.make({"recommendations/retry-idempotent-only.md": r})
        self.assertEqual(self.errors(final=True), [])


class TrustRules(BundleCase):
    def test_country_codes(self):
        for code, ok in (("GB", True), ("EU", True), ("INT", True),
                         ("unknown", True), ("UK", False), ("XX", False)):
            with self.subTest(code=code):
                self.make({"references/rfc-9110.md": SOURCE.replace(
                    "country: INT", "country: %s" % code)})
                errs = [e for e in self.errors() if "country" in e]
                self.assertEqual(errs == [], ok, errs)

    def test_changed_after_verification(self):
        f = FINDING.replace("by: evidence-kb/claude-opus-5-5, at: "
                            "2026-11-03T10:00:00Z",
                            "by: evidence-kb/claude-opus-5-5, at: "
                            "2026-11-03T12:00:00Z")
        self.make({"findings/t1-retry-semantics/idempotent-methods.md": f})
        self.assertError("changed after its last verification")

    def test_selfcheck_cannot_make_stable(self):
        s = SOURCE.replace("evidence-kb-reviewer/", "evidence-kb-selfcheck/")
        self.make({"references/rfc-9110.md": s})
        self.assertError("self-checks keep a document draft")

    def test_human_verification_counts(self):
        s = SOURCE.replace("evidence-kb-reviewer/claude-opus-5-5",
                           "human:frank")
        self.make({"references/rfc-9110.md": s})
        self.assertEqual(self.errors(final=True), [])

    def test_stable_must_be_covered_by_passing_review(self):
        self.make({"reviews/synthesis-r1.md": review("synthesis", 1,
                                                     "revise"),
                   "audit.md": AUDIT.replace(
                       "unresolved: []", "unresolved:\n  - { gate: "
                       "synthesis, summary: \"Disputed.\" }"),
                   "reviews/final-r1.md": review("final", 1, "pass").replace(
                       "reviewed: [/]", "reviewed: [/audit.md]")})
        self.assertError("retry-idempotent-only.md:8: error: status: "
                         "stable, but no passing round of its own gate",
                         final=True)

    def test_directory_and_source_coverage(self):
        self.make()
        bundle = check_kb.load_bundle(self.tmp)
        checker = check_kb.Checker(bundle, TODAY, final=True)
        checker.run()
        covered = checker.covered_by_passing_reviews()
        self.assertIn("findings/t1-retry-semantics/idempotent-methods.md",
                      covered)
        self.assertIn("references/vendor-retries.md", covered)

    def test_verdict_must_match_report(self):
        r = review("synthesis", 1, "pass").replace(
            "# Verdict\n\npass", "# Verdict\n\nrevise")
        self.make({"reviews/synthesis-r1.md": r})
        self.assertError("does not match the report's verdict 'revise'")

    def test_pass_with_open_major_objection(self):
        table = ("| ID | Severity | Location | Objection | Required change |\n"
                 "|----|----------|----------|-----------|-----------------|\n"
                 "| S1 | minor | /x.md | wording | reword |\n"
                 "| S2 | **major** | /y.md | overreach | weaken |\n")
        self.make({"reviews/synthesis-r1.md": review(
            "synthesis", 1, "pass", objections=table)})
        errs = [e for e in self.errors() if "blocking or major" in e]
        self.assertEqual(len(errs), 1, errs)

    def test_review_needs_verdict_section(self):
        r = review("synthesis", 1, "pass").replace("# Verdict", "# Summary")
        self.make({"reviews/synthesis-r1.md": r})
        self.assertError("missing section '# Verdict'")

    def test_final_gate_required(self):
        self.make(remove=("reviews/final-r1.md",))
        self.assertError("gate final has no Review record", final=True)

    def test_cycles(self):
        self.make({"reviews/plan-intent-c2-r1.md": review(
            "plan", 1, "revise", "intent", cycle=2)})
        # Same round number in another cycle is no duplicate ...
        self.assertFalse(any("reviews for" in e for e in self.errors()))
        # ... and the latest cycle decides.
        self.assertError("last round (1) of gate plan (intent) did not pass",
                         final=True)
        self.make({"reviews/plan-intent-c2-r1.md": review(
                       "plan", 1, "revise", "intent", cycle=2),
                   "reviews/plan-intent-c2-r1b.md": review(
                       "plan", 1, "pass", "intent", cycle=2)})
        self.assertError("has 2 reviews for cycle 2, round 1")

    def test_recommendation_sources_from_findings(self):
        extra = SOURCE.replace("rfc9110", "rfc9111").replace(
            "RFC 9110: HTTP Semantics", "RFC 9111: HTTP Caching")
        r = RECOMMENDATION.replace(
            "  - { id: rfc-9110, resource: /references/rfc-9110.md }",
            "  - { id: rfc-9110, resource: /references/rfc-9110.md }\n"
            "  - { id: rfc-9111, resource: /references/rfc-9111.md }"
        ).replace("by definition.[^rfc-9110]",
                  "by definition.[^rfc-9110][^rfc-9111]").replace(
            "[^rfc-9110]: RFC 9110", "[^rfc-9110]: RFC 9110\n"
            "[^rfc-9111]: RFC 9111")
        self.make({"references/rfc-9111.md": extra,
                   "recommendations/retry-idempotent-only.md": r})
        self.assertError("source rfc-9111 is not a source of any based_on "
                         "Finding")

    def test_stale_after_expected_for_recency_topics(self):
        f = FINDING.replace("stale_after: 2036-11-03T10:00:00Z\n", "")
        self.make({"findings/t1-retry-semantics/idempotent-methods.md": f})
        self.assertWarning("set stale_after to generated.at plus 120 months")

    def test_no_recommendations_warns(self):
        self.make(remove=("recommendations/retry-idempotent-only.md",
                          "reviews/synthesis-r1.md"))
        self.assertWarning("has no Recommendations", final=True)


class Stats(BundleCase):
    def test_stats_numbers(self):
        self.make()
        bundle = check_kb.load_bundle(self.tmp)
        stats = check_kb.compute_stats(bundle, TODAY)
        ov = stats["overall"]
        self.assertEqual(ov["sources"], 2)
        self.assertEqual(ov["dated"], 2)
        self.assertEqual(ov["country"], {"INT": 1, "US": 1})
        self.assertEqual(ov["commercial_interest"], {"false": 1, "true": 1})
        # "2025" counts from its period end (2025-12-31): under a year.
        self.assertEqual(ov["age"]["<1y"], 1)
        self.assertEqual(ov["age"]["1-3y"], 0)
        self.assertEqual(ov["age"]["3-5y"], 1)   # published 2022-06
        t1 = stats["topics"]["T1"]
        self.assertEqual(t1["answered"], ["T1.1", "T1.2"])
        self.assertEqual(t1["unanswered"], [])
        self.assertEqual(t1["older_than_max_age"], [])
        t2 = stats["topics"]["T2"]
        self.assertEqual(t2["unanswered"], ["T2.1"])
        self.assertEqual(stats["gates"]["research:T1"],
                         {"rounds": 2, "last_verdict": "pass"})
        self.assertEqual(stats["gates"]["plan (domain)"],
                         {"rounds": 2, "last_verdict": "pass"})
        self.assertEqual(list(stats["gates"])[:2],
                         ["plan (domain)", "plan (intent)"])

    def test_max_age(self):
        self.make({"plan.md": PLAN.replace("max_age_months: 120",
                                           "max_age_months: 24")})
        stats = check_kb.compute_stats(check_kb.load_bundle(self.tmp), TODAY)
        self.assertEqual(stats["topics"]["T1"]["older_than_max_age"],
                         ["references/rfc-9110.md"])

    def test_undated_source(self):
        self.make({"references/rfc-9110.md": SOURCE.replace(
            "published: 2022-06", "published: unknown")})
        stats = check_kb.compute_stats(check_kb.load_bundle(self.tmp), TODAY)
        self.assertEqual(stats["overall"]["age"]["unknown"], 1)
        self.assertEqual(stats["overall"]["dated"], 1)

    def test_cli_markdown_and_json(self):
        self.make()
        code, out, _ = self.run_cli("stats", str(self.tmp), "--today",
                                    "2026-11-03")
        self.assertEqual(code, 0)
        self.assertIn("| T1 Retry semantics | must | 2 of 2 |", out)
        code, out, _ = self.run_cli("stats", str(self.tmp), "--json")
        self.assertEqual(json.loads(out)["overall"]["sources"], 2)


class Fingerprint(BundleCase):
    def test_changes_with_content_only(self):
        self.make()
        code, first, _ = self.run_cli("fingerprint", str(self.tmp))
        self.assertEqual(code, 0)
        self.assertEqual(self.run_cli("fingerprint", str(self.tmp))[1], first)
        p = self.tmp / "references/rfc-9110.md"
        p.write_text(p.read_text() + "x")
        self.assertNotEqual(self.run_cli("fingerprint", str(self.tmp))[1],
                            first)
        self.assertEqual(self.run_cli("fingerprint",
                                      str(self.tmp / "nope"))[0], 2)
        code, out, _ = self.run_cli("fingerprint", str(self.tmp), "--files")
        self.assertEqual(code, 0)
        self.assertIn("  references/rfc-9110.md\n", out)
        self.assertRegex(out.splitlines()[0], r"^[0-9a-f]{64}  \S")


class Index(BundleCase):
    def test_index_generation(self):
        self.make()
        root = (self.tmp / "index.md").read_text()
        self.assertTrue(root.startswith('---\nokf_version: "0.2"\n---\n'))
        self.assertIn("* [Brief: retry policies](brief.md) - Goal", root)
        self.assertLess(root.index("brief.md"), root.index("plan.md"))
        self.assertLess(root.index("plan.md"), root.index("audit.md"))
        self.assertIn("* [Log](log.md)", root)
        findings = (self.tmp / "findings/index.md").read_text()
        self.assertIn("* [T1 Retry semantics](t1-retry-semantics/index.md) - "
                      "Findings for T1 (must-have).", findings)
        reviews = (self.tmp / "reviews/index.md").read_text()
        self.assertLess(reviews.index("plan-intent-r1.md"),
                        reviews.index("research-t1-r1.md"))
        self.assertLess(reviews.index("research-t1-r2.md"),
                        reviews.index("synthesis-r1.md"))

    def test_draft_marked_in_index(self):
        r = RECOMMENDATION.replace("status: stable", "status: draft")
        self.make({"recommendations/retry-idempotent-only.md": r})
        text = (self.tmp / "recommendations/index.md").read_text()
        self.assertIn("(draft)", text)

    def test_index_check_mode(self):
        self.make()
        self.assertEqual(self.run_cli("index", str(self.tmp), "--check")[0], 0)
        (self.tmp / "references/index.md").write_text("# Sources\n")
        code, _, err = self.run_cli("index", str(self.tmp), "--check")
        self.assertEqual(code, 1)
        self.assertIn("references/index.md", err)
        self.assertEqual((self.tmp / "references/index.md").read_text(),
                         "# Sources\n")
        self.assertWarning("index.md is out of date")

    def test_generated_indexes_are_conformant(self):
        self.make()
        self.assertEqual(self.errors(), [])
        self.assertEqual(self.warnings(), [])

    def test_symlinks_are_skipped(self):
        self.make()
        outside = Path(tempfile.mkdtemp(prefix="evidence-kb-outside-"))
        self.addCleanup(shutil.rmtree, outside)
        (outside / "secret.md").write_text("---\ntype: Source\n---\n")
        try:
            (self.tmp / "references/link.md").symlink_to(outside / "secret.md")
        except (OSError, NotImplementedError):
            self.skipTest("symlinks not supported")
        self.assertWarning("symbolic link skipped")


class YamlRegressions(unittest.TestCase):
    """Inputs that agents write and that earlier versions mis-parsed."""

    def parse(self, text):
        return check_kb.parse_yaml(d(text))[0]

    def test_comment_with_apostrophe_after_flow(self):
        data = self.parse("""
            tags: [retries, http]  # don't rename
            stale_after: 2026-01-01T00:00:00Z
            title: Don't retry POST
            generated: { by: x/y, at: 2026-11-03T10:00:00Z }  # Claude's
            jurisdiction: EU # it's EU law
            """)
        self.assertEqual(list(data), ["tags", "stale_after", "title",
                                      "generated", "jurisdiction"])
        self.assertEqual(data["title"], "Don't retry POST")
        self.assertEqual(data["jurisdiction"], "EU")

    def test_comment_inside_multiline_flow(self):
        data = self.parse("""
            sources:
              - { id: a, resource: /references/a.md,  # primary
                  title: A }
            """)
        self.assertEqual(data["sources"], [{"id": "a",
                                            "resource": "/references/a.md",
                                            "title": "A"}])

    def test_apostrophes_in_flow_plain_values(self):
        data = self.parse("""
            a: { id: a, title: WHO's guidance }
            b: [O'Reilly, "x, y"]
            """)
        self.assertEqual(data["a"]["title"], "WHO's guidance")
        self.assertEqual(data["b"], ["O'Reilly", "x, y"])

    def test_multiline_quoted_scalars(self):
        data = self.parse("""
            title: "A very long title: with a colon
              wraps onto the next line"
            other: 'single
              quoted'
            next: 1
            """)
        self.assertEqual(data["title"], "A very long title: with a colon "
                                        "wraps onto the next line")
        self.assertEqual(data["other"], "single quoted")
        self.assertEqual(data["next"], 1)

    def test_scalar_on_next_line(self):
        data = self.parse("""
            description:
              This is a long description
              on two lines
            status:
              draft
            tags:
              [a, b]
            title:
              "Quoted: title"
            """)
        self.assertEqual(data["description"],
                         "This is a long description on two lines")
        self.assertEqual(data["status"], "draft")
        self.assertEqual(data["tags"], ["a", "b"])
        self.assertEqual(data["title"], "Quoted: title")

    def test_blank_line_in_plain_scalar(self):
        data = self.parse("""
            a: one
              two

              three
            b: 2
            """)
        self.assertEqual(data["a"], "one two\nthree")

    def test_folded_with_more_indented_line(self):
        data = self.parse("""
            a: >
              x

                y
              z
            """)
        self.assertEqual(data["a"], "x\n\n  y\nz\n")

    def test_new_errors_instead_of_silent_misparse(self):
        cases = {
            "a:\n  - - x": "nested lists",
            "a: - b": "list cannot start",
            "a: one # c\n  two": "unexpected indentation",
            "a: " + "[" * 100 + "]" * 100: "nested too deeply",
            "a: |\n    x\n  y": "indented less",
        }
        for text, fragment in cases.items():
            with self.subTest(text=text[:20]):
                with self.assertRaises(check_kb.YamlError) as ctx:
                    check_kb.parse_yaml(text)
                self.assertIn(fragment, ctx.exception.message)

    def test_more_escapes(self):
        data = self.parse(r"""
            a: "\x41\u00e9\U0001F600\e"
            """)
        self.assertEqual(data["a"], "A\u00e9\U0001F600\x1b")

    def test_yaml11_boolean_notes(self):
        _, _, _, notes = check_kb.parse_yaml_full("country: NO\n"
                                                  "language: 'no'\n"
                                                  "x: [yes]\n")
        self.assertEqual([n[0] for n in notes], [1, 3])

    def test_item_lines(self):
        data, _, items, _ = check_kb.parse_yaml_full(
            "topics:\n  - id: T1\n    title: a\n  - { id: T2 }\n", 2)
        self.assertEqual(items[id(data["topics"][0])], 3)
        self.assertEqual(items[id(data["topics"][1])], 5)

    def test_same_result_as_pyyaml(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML not installed")
        corpus = [
            'title: "RFC 9110: HTTP Semantics"\nx: plain # c\n',
            "tags: [a, 'b c', \"d\"]  # don't\nnext: v\n",
            "s:\n  - { id: a, resource: /r/a.md, title: \"T: x\" }\n"
            "  - id: b\n    resource: /r/b.md\n",
            "d: >-\n  one\n  two\n\n  three\n",
            "l: |\n  a\n   b\n\nz: end\n",
            "q: 'it''s'\nr: \"tab\\tx\"\n",
            "t:\n  - id: T1\n    questions:\n      - { id: T1.1, text: q }\n",
            "m: { a: [1, 2], b: { c: d } }\n",
            "long: \"first line\n  second line\"\n",
            "u: https://example.com/a#frag\nv: a#b\n",
            "e: []\nf: {}\ng: ~\n",
            "next:\n  value on next line\n",
            "multi: [a,\n  b, # c\n  c]\n",
            "esc: \"x\\\n  y\"\n",
            "n: [1, 2.5, -3, 08]\n",
            "f: { a: 'it''s', b: \"q\\\"x\" }\n",
            "folded: >\n  a\n  b\n\n    c\n  d\nnext: 1\n",
            "wrapped: one\n  two\n\n  three\n",
        ]
        for text in corpus:
            with self.subTest(text=text):
                ours = json.loads(json.dumps(check_kb.parse_yaml(text)[0]))
                theirs = json.loads(json.dumps(yaml.safe_load(text)))
                self.assertEqual(ours, theirs)


class Robustness(BundleCase):
    """Wrongly typed values give errors, never tracebacks."""

    MUTATIONS = [
        ("plan.md", "title: Research plan", "title: 1984"),
        ("plan.md", "title: Research plan", "title: [a]"),
        ("plan.md", "decisions: [D1]\n    max_age",
         "decisions: [[D1]]\n    max_age"),
        ("plan.md", "    questions:\n      - { id: T1.1",
         "    questions: 3\n    q:\n      - { id: T1.1"),
        ("findings/t1-retry-semantics/idempotent-methods.md",
         "question: [T1.1, T1.2]", "question: [{a: 1}]"),
        ("recommendations/retry-idempotent-only.md", "decisions: [D1]",
         "decisions: [{D1: x}]"),
        ("audit.md", '{ id: T2, reason: "Out of budget.", to_close: '
         '"Research backoff." }', "{ id: [T2], reason: x, to_close: y }"),
        ("audit.md", "unresolved: []", "unresolved: none"),
        ("references/rfc-9110.md", "commercial_interest: false",
         "commercial_interest: 0"),
        ("references/rfc-9110.md", "title: \"RFC 9110: HTTP Semantics\"",
         "title: 9110"),
        ("findings/t1-retry-semantics/idempotent-methods.md",
         "sources:\n", "sources: 7\nx:\n"),
    ]

    def test_mutations_do_not_crash(self):
        for rel, old, new in self.MUTATIONS:
            with self.subTest(rel=rel, new=new):
                files = base_files()
                self.assertIn(old, files[rel])
                files[rel] = files[rel].replace(old, new)
                shutil.rmtree(self.tmp)
                self.tmp.mkdir()
                self.write(files)
                for cmd in (["check", "--final"], ["stats"], ["index"]):
                    code, out, err = self.run_cli(cmd[0], str(self.tmp),
                                                  *cmd[1:])
                    self.assertNotIn("internal error", err)
                    self.assertNotIn("Traceback", err)
                    self.assertIn(code, (0, 1))

    def test_commercial_interest_zero_rejected(self):
        self.make({"references/rfc-9110.md": SOURCE.replace(
            "commercial_interest: false", "commercial_interest: 0")})
        self.assertError("commercial_interest must be true, false, or "
                         "unknown")

    def test_duplicate_round_without_verdict(self):
        broken = review("synthesis", 1, "pass").replace("verdict: pass\n", "")
        self.make({"reviews/synthesis-r1b.md": broken})
        self.assertError("has 2 reviews for round 1")

    def test_internal_errors_are_reported_not_raised(self):
        original = check_kb.compute_stats
        check_kb.compute_stats = lambda *a: 1 / 0
        try:
            self.make()
            code, _, err = self.run_cli("stats", str(self.tmp))
        finally:
            check_kb.compute_stats = original
        self.assertEqual(code, 2)
        self.assertIn("internal error (ZeroDivisionError", err)

    def test_fifo_is_skipped(self):
        self.make()
        try:
            os.mkfifo(self.tmp / "references" / "pipe.md")
        except (AttributeError, OSError):
            self.skipTest("mkfifo not available")
        self.assertWarning("not a regular file; skipped")

    def test_long_lines_are_fast(self):
        import time
        body = "# a" + " " * 20000 + "x\n" + "[a](" * 5000 + "\n"
        self.make({"brief.md": BRIEF + body})
        start = time.time()
        self.problems()
        self.assertLess(time.time() - start, 5)

    def test_timestamp_fraction_on_all_versions(self):
        self.assertIsNotNone(
            check_kb.parse_timestamp("2026-01-31T12:00:00.5Z"))
        self.assertIsNotNone(
            check_kb.parse_timestamp("2026-01-31T12:00:00.1234567+01:00"))

    def test_item_line_in_diagnostics(self):
        bad = PLAN.replace("priority: nice", "priority: later")
        self.make({"plan.md": bad})
        line = next(n for n, text in enumerate(bad.split("\n"), 1)
                    if text.strip() == "- id: T2")
        self.assertError("plan.md:%d: error: topic T2 priority" % line)

    def test_yaml11_warning(self):
        self.make({"references/rfc-9110.md": SOURCE.replace(
            "language: en", "language: no")})
        self.assertWarning("read as a boolean by YAML 1.1")


class NamingAndReviews(BundleCase):
    def test_file_names_must_be_kebab_case(self):
        self.make({"references/My Source.md": SOURCE.replace(
            "rfc9110", "rfc9112")})
        self.assertError("must be lowercase kebab-case ASCII")

    def test_review_file_name_warns(self):
        files = {"reviews/synthesis-r7.md": review("synthesis", 1, "pass")}
        self.make(files, remove=("reviews/synthesis-r1.md",))
        self.assertWarning("Review file name should be synthesis-r1.md")

    def test_reviewed_paths_must_exist(self):
        self.make({"reviews/synthesis-r1.md": review(
            "synthesis", 1, "pass").replace("reviewed: [/recommendations/]",
                                            "reviewed: [/nonexistent.md]")})
        self.assertError("reviewed path '/nonexistent.md' does not exist")

    def test_perspective_only_for_plan_gate(self):
        self.make({"reviews/synthesis-r1.md": review(
            "synthesis", 1, "pass", "domain")})
        self.assertError("perspective is only used by the plan gate")


class IndexSafety(BundleCase):
    def test_index_does_not_write_through_symlinks(self):
        self.make()
        outside = Path(tempfile.mkdtemp(prefix="evidence-kb-victim-"))
        self.addCleanup(shutil.rmtree, outside)
        victim = outside / "victim.txt"
        victim.write_text("keep me")
        target = self.tmp / "references" / "index.md"
        target.unlink()
        try:
            target.symlink_to(victim)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks not supported")
        code, _, err = self.run_cli("index", str(self.tmp))
        self.assertEqual(code, 1)
        self.assertIn("refusing to write", err)
        self.assertEqual(victim.read_text(), "keep me")

    def test_index_refuses_non_bundle(self):
        (self.tmp / "docs").mkdir()
        page = "---\ntitle: Home\n---\n\nHand-written.\n"
        (self.tmp / "docs" / "index.md").write_text(page)
        (self.tmp / "docs" / "guide.md").write_text(
            "---\ntype: Guide\ntitle: G\n---\n")
        code, _, err = self.run_cli("index", str(self.tmp))
        self.assertEqual(code, 2)
        self.assertIn("not an evidence-kb bundle", err)
        self.assertEqual((self.tmp / "docs" / "index.md").read_text(), page)
        self.assertFalse((self.tmp / "index.md").exists())

    def test_index_stops_on_parse_errors(self):
        self.make()
        before = (self.tmp / "references" / "index.md").read_text()
        broken = SOURCE.replace('title: "RFC 9110: HTTP Semantics"',
                                "title: RFC 9110: HTTP")
        (self.tmp / "references" / "rfc-9110.md").write_text(broken)
        for args in (["index"], ["index", "--check"]):
            code, _, err = self.run_cli(args[0], str(self.tmp), *args[1:])
            self.assertEqual(code, 1, args)
            self.assertIn("not written", err)
        self.assertEqual((self.tmp / "references" / "index.md").read_text(),
                         before)

    def test_no_temp_files_left(self):
        self.make()
        leftovers = [p for p in self.tmp.rglob(".*.tmp")]
        self.assertEqual(leftovers, [])



class RoundTwoRegressions(BundleCase):
    def test_later_gate_cannot_promote_failed_gate_documents(self):
        failed = review("research:T1", 2, "revise", objections=(
            "| ID | Severity | Location | Objection | Required change |\n"
            "|---|---|---|---|---|\n"
            "| F1 | blocking | /findings/t1-retry-semantics/ | wrong | fix |"))
        audit = AUDIT.replace("unresolved: []", "unresolved:\n  - { gate: "
                              "\"research:T1\", summary: \"F1 open.\" }")
        self.make({"reviews/research-t1-r2.md": failed, "audit.md": audit})
        # final reviewed [/] and synthesis passed, but neither may promote
        # the topic's finding.
        self.assertError("idempotent-methods.md:9: error: status: stable, "
                         "but no passing round of its own gate", final=True)

    def test_review_older_than_document_does_not_cover(self):
        f = FINDING.replace("by: evidence-kb/claude-opus-5-5, at: "
                            "2026-11-03T10:00:00Z", "by: evidence-kb/"
                            "claude-opus-5-5, at: 2026-11-03T10:30:00Z")
        self.make({"findings/t1-retry-semantics/idempotent-methods.md": f})
        self.assertError("no passing round of its own gate", final=True)

    def test_only_reviewers_and_humans_verify(self):
        for actor in ("evidence-kb/claude-opus-5-5",
                      "evidence-kb-researcher/claude-opus-5-5"):
            with self.subTest(actor=actor):
                s = SOURCE.replace("evidence-kb-reviewer/claude-opus-5-5",
                                   actor)
                self.make({"references/rfc-9110.md": s})
                self.assertError("authors and self-checks keep a document "
                                 "draft")

    def test_inline_code_is_linear(self):
        import time
        start = time.time()
        check_kb.strip_inline_code("`" * 5000 + "a")
        check_kb.strip_inline_code("`a" * 5000)
        self.assertLess(time.time() - start, 1)
        self.assertEqual(check_kb.strip_inline_code("a `x` b ``y`z`` c `"),
                         "a  b  c `")

    def test_more_wrong_types(self):
        cases = [
            ("reviews/synthesis-r1.md", 'gate: "synthesis"', "gate: []"),
            ("plan.md", "max_age_months: 120", 'max_age_months: "24"'),
            ("references/rfc-9110.md", "country: INT", "country: [DE, AT]"),
        ]
        for rel, old, new in cases:
            with self.subTest(new=new):
                files = base_files()
                files[rel] = files[rel].replace(old, new)
                shutil.rmtree(self.tmp)
                self.tmp.mkdir()
                self.write(files)
                for cmd in ("check", "stats", "index"):
                    code, _, err = self.run_cli(cmd, str(self.tmp))
                    self.assertNotIn("internal error", err, cmd)
                    self.assertNotIn("could not check", err, cmd)
        self.make({"references/rfc-9110.md": SOURCE.replace(
            "country: INT", "country: [DE, AT]")})
        self.assertError("is not an ISO 3166-1 alpha-2 code")

    def test_bold_severity_header(self):
        table = ("| ID | **Severity** | Location | Objection | Change |\n"
                 "|---|---|---|---|---|\n"
                 "| S1 | major | /x.md | overreach | weaken |\n")
        self.make({"reviews/synthesis-r1.md": review(
            "synthesis", 1, "pass", objections=table)})
        self.assertError("verdict is pass, but this objection is blocking")

    def test_stray_perspective_has_no_follow_up_errors(self):
        self.make({"reviews/research-t1-r2.md": review(
            "research:T1", 2, "pass", "domain")})
        errs = self.errors(final=True)
        self.assertTrue(any("perspective is only used" in e for e in errs))
        self.assertFalse(any("did not pass" in e or "no Review record" in e
                             for e in errs), errs)

    def test_numbers_like_pyyaml(self):
        data = check_kb.parse_yaml("a: 08\nb: 1e3\nc: 0\nd: -7\ne: 1.5\n")[0]
        self.assertEqual(data, {"a": "08", "b": "1e3", "c": 0, "d": -7,
                                "e": 1.5})

    def test_escaped_line_break(self):
        data = check_kb.parse_yaml('a: "x\\\n  y"\nb: "p\\\\\n  q"\n')[0]
        self.assertEqual(data["a"], "xy")
        self.assertEqual(data["b"], "p\\ q")


if __name__ == "__main__":
    unittest.main()
