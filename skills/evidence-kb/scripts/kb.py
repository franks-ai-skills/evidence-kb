#!/usr/bin/env python3
"""The evidence-kb tool: validate, summarize, index, and write a knowledge
base. The skill does every file operation through this one script, so that
a single pre-approved command covers the whole run.

An evidence-kb knowledge base is an Open Knowledge Format (OKF) v0.2 bundle
that follows the profile in ../references/bundle-format.md.

Usage (BUNDLE is a directory inside the current git project):
    kb.py check BUNDLE [--final] [--strict] [--today YYYY-MM-DD] [--json]
    kb.py stats BUNDLE [--today YYYY-MM-DD] [--json]
    kb.py index BUNDLE [--check]
    kb.py fingerprint BUNDLE [--files]
    kb.py snapshot BUNDLE                    prints a snapshot id
    kb.py compare BUNDLE ID
    kb.py restore BUNDLE ID
    kb.py init BUNDLE
    kb.py write BUNDLE PATH [--new]          < content
    kb.py append BUNDLE PATH --section TITLE < content
    kb.py mark BUNDLE PATH... [--status S] [--verified-by ACTOR] [--touch]
    kb.py log BUNDLE --kind KIND [--message TEXT]   (else from stdin)
    kb.py guide format|prompts
    kb.py now | root | preflight BUNDLE

check   OKF conformance plus the evidence-kb profile rules: required fields
        and allowed values, dates, footnote attribution, source resolution,
        independent sources for recommendations, review records. With
        --final it also requires the audit, coverage of every must-have
        question (or a declared gap), a passing or declared-unresolved last
        round for every review gate, and gate-scoped verification for every
        stable concept. --strict turns warnings into errors.
stats   Source mix, dating, age distribution, commercial-interest share and
        question coverage, overall and per topic, as Markdown tables (or JSON).
index   Regenerate every index.md file from frontmatter. With --check,
        write nothing and exit 1 if any index.md is missing or out of date.
fingerprint
        Print one SHA-256 over all file paths and contents, or with --files
        one "<sha256>  <path>" line per file.
snapshot, compare, restore
        Save a copy of the bundle and print its id (the SHA-256 of its
        manifest); list the files added, changed, or deleted since (exit 1
        if any); or undo those changes. compare and restore first verify
        that the snapshot itself is unchanged.
init    Create the bundle directory with its marker and .gitignore, or
        adopt an existing evidence-kb bundle. Refuses any other existing
        directory and names a free one.
write   Write one bundle file from standard input: only .md files with
        kebab-case names inside the bundle, never index.md, never through
        symbolic links, atomically, and only if the frontmatter parses and
        adds no human: event. --new refuses to overwrite an existing file.
append  Add standard input at the end of a level-1 section of a file
        (created if missing), without retyping the file.
mark    Set status, append a verified event (never human:), or set
        generated.at to now; refuses to change anything else.
log     Add a dated entry to log.md, newest first.
guide   Print the skill's format reference or subagent prompts.
now, root, preflight
        Print the UTC time, the project root, or an environment check.
        A bundle that exists but is not an evidence-kb bundle is reported
        as a note with a free name, not as an error.

All writes to a bundle are serialized by a lock. Writing commands accept a
bundle path of plain directory names only (no hidden directories such as
.git or .claude, no component directories such as agents, commands, or
skills, no node_modules) without symbolic links below the project root,
and write only the files of the bundle layout: brief.md, plan.md,
audit.md, findings/t<n>-<slug>/*.md, recommendations/*.md,
references/*.md, reviews/*.md, plus log.md (via log) and index.md (via
index). Read-only commands (check, stats, fingerprint, index --check)
accept any directory inside a git repository.

Frontmatter is parsed with a built-in parser for a YAML subset (block
mappings and lists, flow mappings and lists, plain and quoted scalars,
literal and folded block scalars, comments). Anchors, aliases, tags, and
multiple documents are reported as errors. Dates are kept as strings.

The script reads and writes only inside BUNDLE, which must lie inside the
project (the git work tree of the working directory), and reads its own
reference files for `guide`. It never follows symbolic links and makes no
network requests.

Exit status: 0 = success, 1 = problems found or nothing written (the reason
is printed), 2 = usage error, path outside the project or bundle, read
error, or internal error.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import hashlib
import re
import shutil
import stat
import sys
import time
from collections import Counter, OrderedDict
from pathlib import Path, PurePosixPath

PROFILE = "evidence-kb/1"
OKF_VERSION = "0.2"
RESERVED = ("index.md", "log.md")

SOURCE_TYPES = (
    "law", "regulation", "court-decision", "standard", "peer-reviewed",
    "systematic-review", "preprint", "official-statistics", "official-docs",
    "government-guidance", "institutional-guidance", "book", "dataset",
    "practitioner", "vendor", "news", "community", "other",
)
PUBLISHER_TYPES = (
    "government", "intergovernmental", "standards-body", "academic",
    "non-profit", "company", "individual", "media", "community", "other",
)
TIERS = ("primary", "secondary", "tertiary")
ACCESS = ("open", "registration", "paywalled", "partial", "offline")
CONFIDENCE = ("high", "medium", "low")
STRENGTH = ("must", "should", "may")
STATUS = ("draft", "stable", "deprecated")
VERDICTS = ("pass", "revise")
PRIORITIES = ("must", "nice")
COMMERCIAL = (True, False, "unknown")

PROFILE_TYPES = {
    "Research Brief": "",
    "Research Plan": "",
    "Research Audit": "",
    "Finding": "findings",
    "Recommendation": "recommendations",
    "Source": "references",
    "Review": "reviews",
}
ROOT_TYPES = {"brief.md": "Research Brief", "plan.md": "Research Plan",
              "audit.md": "Research Audit"}

REQUIRED_SECTIONS = {
    "Research Brief": ("Request", "Goal", "Possible readings", "Context",
                       "Scope", "Assumptions",
                       "How to use this knowledge base"),
    "Research Plan": ("Topic map", "Source strategy", "Coverage matrix",
                      "Risks and stop conditions"),
    "Research Audit": ("Coverage", "Source mix", "Gaps and weak spots",
                       "Unresolved objections", "Review independence"),
    "Finding": ("Answer",),
    "Recommendation": ("Recommendation", "Rationale", "How to check"),
    "Source": ("Evidence",),
    "Review": ("Objections",),
}
CLAIM_SECTIONS = {"Finding": "Answer", "Recommendation": "Rationale"}
CLAIM_MARKERS = re.compile(r"^\W*(inference|gap)\s*:", re.IGNORECASE)

SECTION_DESCRIPTIONS = {
    "findings": "Answers to the research questions, one directory per topic.",
    "recommendations": "Checkable guidance derived from the findings, for "
                       "implementers and reviewers.",
    "references": "One record per source, with publisher, dates, evidence "
                  "tier, commercial interest, and verbatim evidence.",
    "reviews": "Records of every review gate round, with objections and "
               "responses.",
}
SECTION_TITLES = {"findings": "Findings", "recommendations": "Recommendations",
                  "references": "Sources", "reviews": "Reviews"}
ROOT_ORDER = ("brief.md", "plan.md", "audit.md")

TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:\d{2})\Z")
PARTIAL_DATE_RE = re.compile(r"^(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?\Z")
ACTOR_RE = re.compile(r"^(human:\S+|process:\S+|[^\s/:]+/\S+)\Z")
# ISO 3166-1 alpha-2 officially assigned codes, plus EU and INT.
ISO_COUNTRIES = frozenset("""
AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BJ
BL BM BN BO BQ BR BS BT BV BW BY BZ CA CC CD CF CG CH CI CK CL CM CN CO CR
CU CV CW CX CY CZ DE DJ DK DM DO DZ EC EE EG EH ER ES ET FI FJ FK FM FO FR
GA GB GD GE GF GG GH GI GL GM GN GP GQ GR GS GT GU GW GY HK HM HN HR HT HU
ID IE IL IM IN IO IQ IR IS IT JE JM JO JP KE KG KH KI KM KN KP KR KW KY KZ
LA LB LC LI LK LR LS LT LU LV LY MA MC MD ME MF MG MH MK ML MM MN MO MP MQ
MR MS MT MU MV MW MX MY MZ NA NC NE NF NG NI NL NO NP NR NU NZ OM PA PE PF
PG PH PK PL PM PN PR PS PT PW PY QA RE RO RS RU RW SA SB SC SD SE SG SH SI
SJ SK SL SM SN SO SR SS ST SV SX SY SZ TC TD TF TG TH TJ TK TL TM TN TO TR
TT TV TW TZ UA UG UM US UY UZ VA VC VE VG VI VN VU WF WS YE YT ZA ZM ZW
EU INT""".split())
LANGUAGE_RE = re.compile(r"^([a-z]{2,3}(-[A-Za-z0-9]{2,8})*|unknown)\Z")
TOPIC_ID_RE = re.compile(r"^T\d+\Z")
DECISION_ID_RE = re.compile(r"^D\d+\Z")
URL_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
GATE_RE = re.compile(r"^(plan|synthesis|final|research:T\d+)\Z")
REQUIRED_PERSPECTIVES = {"plan": ("domain", "intent")}
NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*(\.md)?\Z")
MAX_FILE_BYTES = 5 * 1024 * 1024


# --------------------------------------------------------------------------
# YAML subset parser
# --------------------------------------------------------------------------

class YamlError(Exception):
    def __init__(self, message: str, line: int):
        super().__init__(message)
        self.message = message
        self.line = line


class _Unterminated(YamlError):
    """A quoted scalar or flow collection that continues on a later line."""


_INT_RE = re.compile(r"^[-+]?(0|[1-9]\d*)$")
_FLOAT_RE = re.compile(r"^[-+]?(\d+\.\d*|\.\d+)([eE][-+]?\d+)?$")
# Plain scalars that YAML 1.1 parsers (such as PyYAML) read as booleans.
YAML11_BOOLS = frozenset("y Y yes Yes YES n N no No NO on On ON off Off OFF"
                         .split())
MAX_FLOW_DEPTH = 32


def _resolve_plain(text: str, notes=None, line=0):
    if text in ("", "~", "null", "Null", "NULL"):
        return None
    if text in ("true", "True", "TRUE"):
        return True
    if text in ("false", "False", "FALSE"):
        return False
    if _INT_RE.match(text):
        return int(text)
    if _FLOAT_RE.match(text) and not PARTIAL_DATE_RE.match(text):
        return float(text)
    if notes is not None and text in YAML11_BOOLS:
        notes.append((line, "unquoted %r is read as a boolean by YAML 1.1 "
                            "parsers such as PyYAML; write \"%s\"" % (text, text)))
    return text


def _strip_comment(text: str):
    """Remove a trailing comment from a plain value.

    Returns (text, had_comment)."""
    m = re.search(r"(^|[ \t])#", text)
    if m:
        return text[:m.start()].rstrip(), True
    return text.rstrip(), False


_ESCAPES = {"n": "\n", "t": "\t", "\t": "\t", '"': '"', "\\": "\\",
            "/": "/", "0": "\0", "r": "\r", " ": " ", "a": "\a",
            "b": "\b", "e": "\x1b", "f": "\f", "v": "\v", "N": "\x85",
            "_": "\xa0", "L": " ", "P": " "}
_HEX_ESCAPES = {"x": 2, "u": 4, "U": 8}


def _unescape_double(body: str, line: int) -> str:
    out, i = [], 0
    while i < len(body):
        ch = body[i]
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        if i + 1 >= len(body):
            raise YamlError("unterminated escape in double-quoted string",
                            line)
        nxt = body[i + 1]
        if nxt in _HEX_ESCAPES:
            n = _HEX_ESCAPES[nxt]
            digits = body[i + 2:i + 2 + n]
            if not re.match(r"^[0-9A-Fa-f]{%d}$" % n, digits):
                raise YamlError("invalid escape \\%s%s" % (nxt, digits), line)
            out.append(chr(int(digits, 16)))
            i += 2 + n
            continue
        if nxt not in _ESCAPES:
            raise YamlError("unsupported escape \\%s" % nxt, line)
        out.append(_ESCAPES[nxt])
        i += 2
    return "".join(out)


def _read_quoted(text: str, start: int, line: int):
    """Read a quoted scalar starting at text[start]. Returns (value, end).

    Raises _Unterminated when the closing quote is missing."""
    quote = text[start]
    i = start + 1
    if quote == "'":
        buf = []
        while i < len(text):
            if text[i] == "'":
                if i + 1 < len(text) and text[i + 1] == "'":
                    buf.append("'")
                    i += 2
                    continue
                return "".join(buf), i + 1
            buf.append(text[i])
            i += 1
        raise _Unterminated("unterminated single-quoted string", line)
    while i < len(text):
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == '"':
            return _unescape_double(text[start + 1:i], line), i + 1
        i += 1
    raise _Unterminated("unterminated double-quoted string", line)


def _fold_lines(parts):
    """Join the lines of a multi-line flow or quoted scalar: a line break
    becomes a space, and each blank line becomes a newline."""
    out, blanks = "", 0
    for n, part in enumerate(parts):
        if part == "":
            blanks += 1
            continue
        if n and out:
            out += "\n" * blanks if blanks else " "
        out += part
        blanks = 0
    return out


class _FlowScanner:
    """Track brackets, quotes, and comments of a flow collection across
    lines, so that a line's comment can be removed and the end of the
    collection found."""

    def __init__(self, line):
        self.depth = 0
        self.quote = None
        self.prev = "["          # last significant character
        self.line = line

    def scan(self, text):
        """Return text without its comment; update the state."""
        i = 0
        while i < len(text):
            ch = text[i]
            if self.quote == '"':
                if ch == "\\":
                    i += 2
                    continue
                if ch == '"':
                    self.quote = None
                    self.prev = '"'
            elif self.quote == "'":
                if ch == "'":
                    if i + 1 < len(text) and text[i + 1] == "'":
                        i += 2
                        continue
                    self.quote = None
                    self.prev = "'"
            elif ch == "#" and (i == 0 or text[i - 1] in " \t"):
                return text[:i].rstrip()
            elif ch in "\"'" and self.prev in "[{,:":
                self.quote = ch
            elif ch in "[{":
                self.depth += 1
                if self.depth > MAX_FLOW_DEPTH:
                    raise YamlError("flow collections nested too deeply",
                                    self.line)
                self.prev = ch
            elif ch in "]}":
                self.depth -= 1
                self.prev = ch
            elif ch not in " \t":
                self.prev = ch
            i += 1
        return text.rstrip()

    @property
    def open(self):
        return self.depth > 0 or self.quote is not None


class _Flow:
    """Parser for flow collections: [a, b] and { k: v }."""

    def __init__(self, text: str, line: int, notes=None, item_lines=None):
        self.s = text
        self.i = 0
        self.line = line
        self.notes = notes
        self.item_lines = item_lines if item_lines is not None else {}
        self.depth = 0

    def ws(self):
        while self.i < len(self.s) and self.s[self.i] in " \t\n":
            self.i += 1

    def parse(self):
        value = self.value()
        self.ws()
        rest = self.s[self.i:]
        if rest:
            raise YamlError("unexpected text after flow collection: %r" % rest,
                            self.line)
        return value

    def value(self):
        self.ws()
        if self.i >= len(self.s):
            raise YamlError("unexpected end of flow collection", self.line)
        ch = self.s[self.i]
        if ch in "[{":
            self.depth += 1
            if self.depth > MAX_FLOW_DEPTH:
                raise YamlError("flow collections nested too deeply",
                                self.line)
            value = self.seq() if ch == "[" else self.mapping()
            self.depth -= 1
            return value
        if ch in "\"'":
            val, self.i = _read_quoted(self.s, self.i, self.line)
            return val
        if ch in "&*!|>%@`":
            raise YamlError("unsupported YAML syntax %r" % ch, self.line)
        return _resolve_plain(self.plain(), self.notes, self.line)

    def plain(self) -> str:
        start = self.i
        while self.i < len(self.s):
            ch = self.s[self.i]
            if ch in ",[]{}":
                break
            if ch == ":" and (self.i + 1 >= len(self.s)
                              or self.s[self.i + 1] in " ,[]{}"):
                break
            self.i += 1
        return self.s[start:self.i].strip()

    def seq(self):
        self.i += 1
        out = []
        while True:
            self.ws()
            if self.i >= len(self.s):
                raise YamlError("unterminated flow list", self.line)
            if self.s[self.i] == "]":
                self.i += 1
                return out
            out.append(self.value())
            self.ws()
            if self.i < len(self.s) and self.s[self.i] == ",":
                self.i += 1
            elif self.i < len(self.s) and self.s[self.i] == "]":
                continue
            else:
                raise YamlError("expected ',' or ']' in flow list", self.line)

    def mapping(self):
        self.i += 1
        out = OrderedDict()
        self.item_lines[id(out)] = self.line
        while True:
            self.ws()
            if self.i >= len(self.s):
                raise YamlError("unterminated flow mapping", self.line)
            if self.s[self.i] == "}":
                self.i += 1
                return out
            if self.s[self.i] in "\"'":
                key, self.i = _read_quoted(self.s, self.i, self.line)
            else:
                key = self.plain()
            if not key:
                raise YamlError("empty key in flow mapping", self.line)
            self.ws()
            if self.i >= len(self.s) or self.s[self.i] != ":":
                raise YamlError("expected ':' after key %r" % key, self.line)
            self.i += 1
            self.ws()
            if self.i < len(self.s) and self.s[self.i] in ",}":
                val = None
            else:
                val = self.value()
            if key in out:
                raise YamlError("duplicate key %r" % key, self.line)
            out[key] = val
            self.ws()
            if self.i < len(self.s) and self.s[self.i] == ",":
                self.i += 1
            elif self.i < len(self.s) and self.s[self.i] == "}":
                continue
            else:
                raise YamlError("expected ',' or '}' in flow mapping",
                                self.line)


_KEY_RE = re.compile(r"""^(?:"((?:[^"\\]|\\.)*)"|'((?:[^']|'')*)'|([^\s#'"\[\]{},&*!|>%@`?-][^:#]*?|-[^\s:#][^:#]*?))\s*:(?:\s+|$)(.*)$""")


def _is_seq_item(content: str) -> bool:
    return content == "-" or content.startswith("- ")


class _Yaml:
    def __init__(self, lines, first_line: int):
        self.raw = list(lines)
        self.first = first_line
        self.i = 0
        self.top_lines = {}
        self.item_lines = {}     # id(mapping) -> line where it starts
        self.notes = []          # (line, warning)
        self.depth = 0

    def lineno(self, idx: int) -> int:
        return self.first + idx

    def meaningful(self, idx: int) -> bool:
        stripped = self.raw[idx].strip()
        return bool(stripped) and not stripped.startswith("#")

    def peek(self):
        """Index of the next meaningful line, or None."""
        j = self.i
        while j < len(self.raw):
            if self.meaningful(j):
                return j
            j += 1
        return None

    def indent_of(self, idx: int) -> int:
        line = self.raw[idx]
        lead = line[:len(line) - len(line.lstrip(" \t"))]
        if "\t" in lead:
            raise YamlError("tab characters are not allowed in indentation",
                            self.lineno(idx))
        return len(lead)

    def parse(self):
        j = self.peek()
        if j is None:
            return OrderedDict()
        content = self.raw[j].strip()
        if _is_seq_item(content) or content.startswith(("[", "{")):
            raise YamlError("frontmatter must be a mapping", self.lineno(j))
        if self.indent_of(j) != 0:
            raise YamlError("unexpected indentation", self.lineno(j))
        value = self.block_map(0, top=True)
        j = self.peek()
        if j is not None:
            raise YamlError("unexpected indentation or content",
                            self.lineno(j))
        return value

    def _enter(self):
        self.depth += 1
        if self.depth > MAX_FLOW_DEPTH:
            j = self.peek()
            raise YamlError("block collections nested too deeply",
                            self.lineno(j if j is not None else 0))

    def block_map(self, indent: int, top: bool = False):
        self._enter()
        try:
            return self._block_map(indent, top)
        finally:
            self.depth -= 1

    def block_seq(self, indent: int):
        self._enter()
        try:
            return self._block_seq(indent)
        finally:
            self.depth -= 1

    def _block_map(self, indent: int, top: bool = False):
        out = OrderedDict()
        first = True
        while True:
            j = self.peek()
            if j is None:
                return out
            ind = self.indent_of(j)
            if ind < indent:
                return out
            if ind > indent:
                raise YamlError("unexpected indentation", self.lineno(j))
            content = self.raw[j].strip()
            if _is_seq_item(content):
                return out
            if content.startswith(("---", "...")):
                raise YamlError("multiple YAML documents are not supported",
                                self.lineno(j))
            m = _KEY_RE.match(content)
            if not m:
                raise YamlError("expected 'key: value', got %r" % content,
                                self.lineno(j))
            if m.group(1) is not None:
                key = _unescape_double(m.group(1), self.lineno(j))
            elif m.group(2) is not None:
                key = m.group(2).replace("''", "'")
            else:
                key = m.group(3).strip()
            if key in out:
                raise YamlError("duplicate key %r" % key, self.lineno(j))
            if first:
                self.item_lines[id(out)] = self.lineno(j)
                first = False
            if top:
                self.top_lines[key] = self.lineno(j)
            self.i = j + 1
            out[key] = self.value_after(m.group(4), indent, j)

    def _block_seq(self, indent: int):
        out = []
        while True:
            j = self.peek()
            if j is None:
                return out
            ind = self.indent_of(j)
            if ind < indent:
                return out
            if ind > indent:
                raise YamlError("unexpected indentation", self.lineno(j))
            content = self.raw[j].strip()
            if not _is_seq_item(content):
                return out
            rest = content[1:].lstrip()
            if _is_seq_item(rest):
                raise YamlError("nested lists on one line ('- - x') are not "
                                "supported; use a flow list [x]",
                                self.lineno(j))
            if rest and not rest.startswith(("[", "{", '"', "'", "#")) \
                    and _KEY_RE.match(rest):
                # A mapping that starts on the dash line: re-indent it.
                col = ind + (len(content) - len(rest))
                self.raw[j] = " " * col + rest
                self.i = j
                out.append(self.block_map(col))
                continue
            self.i = j + 1
            out.append(self.value_after(rest, indent, j, in_seq=True))

    def value_after(self, rest: str, indent: int, j: int, in_seq=False):
        rest = rest.strip()
        line = self.lineno(j)
        if rest == "" or rest.startswith("#"):
            k = self.peek()
            if k is None:
                return None
            ind = self.indent_of(k)
            content = self.raw[k].strip()
            if ind > indent:
                if _is_seq_item(content):
                    return self.block_seq(ind)
                if content.startswith(("[", "{", '"', "'", "|", ">")) or \
                        not _KEY_RE.match(content):
                    # A scalar or flow collection on the next line.
                    self.i = k + 1
                    return self.value_after(content, indent, k, in_seq)
                return self.block_map(ind)
            if ind == indent and not in_seq and _is_seq_item(content):
                return self.block_seq(indent)
            return None
        if rest[0] in "|>":
            return self.block_scalar(rest, indent, line)
        if rest[0] in "[{":
            return self.flow(rest, line)
        if rest[0] in "\"'":
            return self.quoted(rest, line)
        if rest[0] in "&*!%@`":
            raise YamlError("unsupported YAML syntax %r (anchors, aliases, "
                            "and tags are not supported)" % rest[0], line)
        if _is_seq_item(rest) and not in_seq:
            raise YamlError("a list cannot start after 'key:' on the same "
                            "line; put each '- item' on its own line", line)
        text, had_comment = _strip_comment(rest)
        if re.search(r":(\s|$)", text):
            raise YamlError("plain value contains ': '; put the value in "
                            "quotes", line)
        parts = [text]
        # Plain multi-line continuation (not after a comment).
        while not had_comment:
            k = self.i
            blanks = 0
            while k < len(self.raw) and self.raw[k].strip() == "":
                blanks += 1
                k += 1
            if k >= len(self.raw) or self.raw[k].strip().startswith("#"):
                break
            if self.indent_of(k) <= indent:
                break
            cont = self.raw[k].strip()
            if in_seq and _is_seq_item(cont):
                break
            cont, had_comment = _strip_comment(cont)
            if re.search(r":(\s|$)", cont):
                raise YamlError("plain value contains ': '; put the value in "
                                "quotes", self.lineno(k))
            parts += [""] * blanks + [cont]
            self.i = k + 1
        return _resolve_plain(_fold_lines(parts), self.notes, line)

    def flow(self, rest, line):
        scanner = _FlowScanner(line)
        parts = [scanner.scan(rest)]
        while scanner.open:
            if self.i >= len(self.raw):
                raise YamlError("unterminated flow collection", line)
            parts.append(scanner.scan(self.raw[self.i].strip()))
            self.i += 1
        return _Flow(_fold_lines(parts), line, self.notes,
                     self.item_lines).parse()

    def quoted(self, rest, line):
        text = rest
        while True:
            try:
                val, end = _read_quoted(text, 0, line)
                break
            except _Unterminated:
                if self.i >= len(self.raw):
                    raise
                nxt = self.raw[self.i].strip()
                self.i += 1
                trailing = len(text) - len(text.rstrip("\\"))
                if text.startswith('"') and trailing % 2 == 1:
                    text = text[:-1] + nxt      # escaped line break: join
                elif nxt == "":
                    text += "\n"           # a blank line is a newline
                elif text.endswith("\n"):
                    text += nxt
                else:
                    text = text.rstrip(" ") + " " + nxt   # line break: space
        tail = text[end:].strip()
        if tail and not tail.startswith("#"):
            raise YamlError("unexpected text after quoted string: %r"
                            % tail, line)
        return val

    def block_scalar(self, header: str, indent: int, line: int):
        m = re.match(r"^([|>])([-+]?)(\d?)([-+]?)\s*(#.*)?$", header)
        if not m:
            raise YamlError("invalid block scalar header %r" % header, line)
        style = m.group(1)
        chomp = m.group(2) or m.group(4)
        explicit = int(m.group(3)) if m.group(3) else None
        lines = []
        while self.i < len(self.raw):
            raw = self.raw[self.i]
            if raw.strip() and len(raw) - len(raw.lstrip(" ")) <= indent:
                break
            lines.append(raw if raw.strip() else "")
            self.i += 1
        trailing = 0
        while lines and lines[-1] == "":
            lines.pop()
            trailing += 1
        content = [ln for ln in lines if ln]
        if not content:
            return ""
        block_indent = (indent + explicit) if explicit else \
            len(content[0]) - len(content[0].lstrip(" "))
        for ln in content:
            if len(ln) - len(ln.lstrip(" ")) < block_indent:
                raise YamlError("block scalar line is indented less than its "
                                "first line", line)
        body = [ln[block_indent:] if ln else "" for ln in lines]
        if style == "|":
            text = "\n".join(body)
        else:
            text, prev, blanks = "", None, 0
            for ln in body:
                if ln == "":
                    blanks += 1
                    continue
                if prev is None:
                    text = "\n" * blanks + ln
                elif ln.startswith((" ", "\t")) or prev.startswith((" ",
                                                                   "\t")):
                    text += "\n" + "\n" * blanks + ln
                elif blanks:
                    text += "\n" * blanks + ln
                else:
                    text += " " + ln
                prev, blanks = ln, 0
        if chomp == "-":
            return text
        if chomp == "+":
            return text + "\n" * (trailing + 1)
        return text + "\n"


def parse_yaml_full(text: str, first_line: int = 1):
    """Parse a YAML-subset mapping.

    Returns (mapping, {top_key: line}, {id(mapping): line}, [(line, note)]).
    """
    parser = _Yaml(text.split("\n"), first_line)
    data = parser.parse()
    return data, parser.top_lines, parser.item_lines, parser.notes


def parse_yaml(text: str, first_line: int = 1):
    """Parse a YAML-subset mapping. Returns (mapping, {top_key: line})."""
    data, top_lines, _items, _notes = parse_yaml_full(text, first_line)
    return data, top_lines


def split_frontmatter(text: str):
    """Split a markdown file. Returns (yaml_text or None, body, body_line).

    body_line is the 1-based line number on which the body starts.
    Raises YamlError when an opening '---' has no closing '---'.
    """
    if text.startswith("﻿"):
        text = text[1:]
    lines = text.split("\n")
    if not lines or lines[0].rstrip() != "---":
        return None, text, 1
    for idx in range(1, len(lines)):
        if lines[idx].rstrip() == "---":
            return ("\n".join(lines[1:idx]), "\n".join(lines[idx + 1:]),
                    idx + 2)
    raise YamlError("frontmatter opened with '---' is never closed", 1)


# --------------------------------------------------------------------------
# Markdown helpers
# --------------------------------------------------------------------------

FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(\S.*)$")


def match_heading(line):
    """(level, title) of an ATX heading line, or None.

    Implemented without a backtracking regex for the closing hashes, so
    long lines cannot slow the checker down."""
    m = _HEADING_RE.match(line)
    if not m:
        return None
    title = m.group(2).rstrip()
    stripped = title.rstrip("#")
    if stripped != title and (stripped == "" or stripped[-1] in " \t"):
        title = stripped.rstrip()
    return (len(m.group(1)), title) if title else None
FOOTREF_RE = re.compile(r"\[\^([^\]\s]+)\](?!:)")
FOOTDEF_RE = re.compile(r"^\[\^([^\]\s]+)\]:")
LINK_RE = re.compile(r"(?<!!)\[(?:[^\[\]\\]|\\.)*\]\([ \t]*<?([^()\s<>]+)>?(?:[ \t]+\"[^\"]*\")?[ \t]*\)")


def code_mask(lines):
    """Return a list of booleans: True for lines inside fenced code."""
    mask, fence = [], None
    for line in lines:
        m = FENCE_RE.match(line)
        if fence is None and m:
            fence = m.group(1)[0] * len(m.group(1))
            mask.append(True)
        elif fence is not None:
            mask.append(True)
            if line.strip().startswith(fence) and \
                    line.strip().strip(fence[0]) == "":
                fence = None
        else:
            mask.append(False)
    return mask


def strip_inline_code(line: str) -> str:
    """Remove `code spans` from a line, in linear time."""
    if "`" not in line:
        return line
    out, i, n = [], 0, len(line)
    while i < n:
        if line[i] != "`":
            j = line.find("`", i)
            j = n if j < 0 else j
            out.append(line[i:j])
            i = j
            continue
        j = i
        while j < n and line[j] == "`":
            j += 1
        run = line[i:j]
        k = j
        while True:
            k = line.find(run, k)
            if k < 0 or (k + len(run) >= n or line[k + len(run)] != "`"):
                break
            while k < n and line[k] == "`":     # longer run: not a match
                k += 1
        if k < 0:
            out.append(run)                    # unmatched backticks stay
            i = j
        else:
            i = k + len(run)                   # drop the code span
    return "".join(out)


def sections(lines, mask):
    """Map level-1 heading text (lower case) -> (start_idx, end_idx)."""
    heads = []
    for idx, line in enumerate(lines):
        if mask[idx]:
            continue
        h = match_heading(line)
        if h and h[0] == 1:
            heads.append((idx, h[1].lower()))
    out = {}
    for n, (idx, title) in enumerate(heads):
        end = heads[n + 1][0] if n + 1 < len(heads) else len(lines)
        out.setdefault(title, (idx + 1, end))
    return out


def claim_blocks(lines, start, end, mask):
    """Yield (line_idx, text) for each paragraph, list item, or table row."""
    block, block_start, in_table_header = [], None, False

    def flush():
        if block:
            return [(block_start, " ".join(block))]
        return []

    out = []
    prev_table = False
    for idx in range(start, end):
        line = lines[idx]
        if mask[idx]:
            out += flush()
            block, block_start = [], None
            continue
        stripped = line.strip()
        if not stripped or match_heading(line) or FOOTDEF_RE.match(
                stripped):
            out += flush()
            block, block_start, prev_table = [], None, False
            continue
        if stripped.startswith("|"):
            out += flush()
            block, block_start = [], None
            if re.match(r"^\|?[\s:|-]+\|?$", stripped) and "-" in stripped:
                prev_table = True
                in_table_header = False
                continue
            if not prev_table:
                # Header row of a new table: not a claim.
                prev_table = True
                in_table_header = True
                continue
            if in_table_header:
                continue
            out.append((idx, stripped))
            continue
        prev_table = False
        if re.match(r"^([-*+]|\d+[.)])\s+", stripped):
            out += flush()
            block, block_start = [stripped], idx
            continue
        if block_start is None:
            block_start = idx
        block.append(stripped)
    out += flush()
    return out


# --------------------------------------------------------------------------
# Bundle model
# --------------------------------------------------------------------------

class Problem:
    def __init__(self, level, path, line, message):
        self.level, self.path, self.line, self.message = (
            level, path, line, message)

    def as_dict(self):
        return {"level": self.level, "path": self.path, "line": self.line,
                "message": self.message}

    def __str__(self):
        return "%s:%d: %s: %s" % (self.path, self.line, self.level,
                                  self.message)


class Doc:
    def __init__(self, rel, text):
        self.rel = rel                     # posix path relative to bundle
        self.text = text
        self.meta = None                   # dict or None
        self.key_lines = {}
        self.body = ""
        self.body_line = 1
        self.has_frontmatter = False
        self.error = None                  # YamlError
        self.item_lines = {}               # id(mapping) -> line
        self.notes = []                    # (line, YAML warning)

    @property
    def name(self):
        return PurePosixPath(self.rel).name

    @property
    def type(self):
        if isinstance(self.meta, dict):
            return self.meta.get("type")
        return None

    def line_of(self, key):
        return self.key_lines.get(key, 1)

    def item_line(self, item, key):
        """Line of a mapping inside a list, else the line of `key`."""
        return self.item_lines.get(id(item)) or self.line_of(key)

    def body_lines(self):
        return self.body.split("\n")


class Bundle:
    def __init__(self, root: Path):
        self.root = root
        self.docs = OrderedDict()          # rel -> Doc
        self.dirs = set()                  # rel dirs ("" = root)
        self.problems = []

    def add(self, level, path, line, message):
        self.problems.append(Problem(level, path, line, message))

    def error(self, path, line, message):
        self.add("error", path, line, message)

    def warn(self, path, line, message):
        self.add("warning", path, line, message)

    # -- loading --------------------------------------------------------

    def load(self):
        for dirpath, dirnames, filenames in os.walk(self.root,
                                                    followlinks=False):
            dirnames[:] = sorted(d for d in dirnames
                                 if not d.startswith(".")
                                 and not os.path.islink(
                                     os.path.join(dirpath, d)))
            rel_dir = os.path.relpath(dirpath, self.root)
            rel_dir = "" if rel_dir == "." else PurePosixPath(
                Path(rel_dir)).as_posix()
            self.dirs.add(rel_dir)
            for fn in sorted(filenames):
                if not fn.endswith(".md") or fn.startswith("."):
                    continue
                full = os.path.join(dirpath, fn)
                rel = (rel_dir + "/" if rel_dir else "") + fn
                try:
                    st = os.lstat(full)
                except OSError as exc:
                    self.error(rel, 1, "cannot read file: %s" % exc)
                    continue
                if stat.S_ISLNK(st.st_mode):
                    self.warn(rel, 1, "symbolic link skipped")
                    continue
                if not stat.S_ISREG(st.st_mode):
                    self.warn(rel, 1, "not a regular file; skipped")
                    continue
                if st.st_size > MAX_FILE_BYTES:
                    self.error(rel, 1, "file is larger than %d MB; skipped"
                               % (MAX_FILE_BYTES // (1024 * 1024)))
                    continue
                try:
                    with open(full, encoding="utf-8") as fh:
                        text = fh.read()
                except UnicodeDecodeError:
                    self.error(rel, 1, "file is not valid UTF-8")
                    continue
                except OSError as exc:
                    self.error(rel, 1, "cannot read file: %s" % exc)
                    continue
                self.docs[rel] = self.parse_doc(rel, text)

    def parse_doc(self, rel, text):
        doc = Doc(rel, text.replace("\r\n", "\n"))
        try:
            yaml_text, body, body_line = split_frontmatter(doc.text)
        except YamlError as exc:
            doc.error = exc
            doc.body = doc.text
            return doc
        doc.body, doc.body_line = body, body_line
        if yaml_text is None:
            return doc
        doc.has_frontmatter = True
        try:
            doc.meta, doc.key_lines, doc.item_lines, doc.notes = \
                parse_yaml_full(yaml_text, first_line=2)
        except YamlError as exc:
            doc.error = exc
        return doc

    # -- queries ----------------------------------------------------------

    def concepts(self, type_=None):
        for doc in self.docs.values():
            if doc.name in RESERVED or not isinstance(doc.meta, dict) or \
                    not is_str(doc.meta.get("type")):
                continue
            if type_ is None or doc.type == type_:
                yield doc

    def get(self, rel):
        return self.docs.get(rel)

    def resolve(self, from_rel, target):
        """Resolve a bundle path. Returns (rel_path or None, outside)."""
        target = target.split("#", 1)[0].split("?", 1)[0]
        if not target:
            return None, False
        if target.startswith("/"):
            parts = PurePosixPath(target.lstrip("/")).parts
        else:
            base = PurePosixPath(from_rel).parent.parts
            parts = base + PurePosixPath(target).parts
        stack = []
        for part in parts:
            if part in ("", "."):
                continue
            if part == "..":
                if not stack:
                    return None, True
                stack.pop()
            else:
                stack.append(part)
        return "/".join(stack), False

    def plan(self):
        doc = self.get("plan.md")
        if doc and isinstance(doc.meta, dict) and \
                doc.type == "Research Plan":
            return doc
        return None

    def plan_topics(self):
        plan = self.plan()
        topics = OrderedDict()
        if not plan:
            return topics
        for t in as_list(plan.meta.get("topics")):
            if isinstance(t, dict) and isinstance(t.get("id"), str):
                topics[t["id"]] = t
        return topics

    def plan_questions(self):
        out = OrderedDict()
        for tid, topic in self.plan_topics().items():
            for q in as_list(topic.get("questions")):
                if isinstance(q, dict) and isinstance(q.get("id"), str):
                    out[q["id"]] = (tid, q)
        return out

    def plan_decisions(self):
        plan = self.plan()
        out = OrderedDict()
        if not plan:
            return out
        for d in as_list(plan.meta.get("decisions")):
            if isinstance(d, dict) and isinstance(d.get("id"), str):
                out[d["id"]] = d
        return out

    def source_doc(self, from_doc, entry):
        """The Source Doc an entry of `sources` points to, or None."""
        if not isinstance(entry, dict):
            return None
        res = entry.get("resource")
        if not isinstance(res, str) or URL_SCHEME_RE.match(res):
            return None
        rel, _ = self.resolve(from_doc.rel, res)
        doc = self.get(rel) if rel else None
        return doc if doc is not None and doc.type == "Source" else None


# --------------------------------------------------------------------------
# Value checks
# --------------------------------------------------------------------------

def parse_timestamp(value):
    """Parse an ISO 8601 datetime with UTC offset, the same way on every
    Python version (3.9's fromisoformat is stricter about fractions)."""
    if not isinstance(value, str) or not TIMESTAMP_RE.match(value):
        return None
    m = re.match(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2})(?::(\d{2})(?:\.(\d+))?)?"
                 r"(Z|[+-]\d{2}:\d{2})$", value)
    seconds = m.group(2) or "00"
    fraction = (m.group(3) or "0")[:6].ljust(6, "0")
    offset = "+00:00" if m.group(4) == "Z" else m.group(4)
    try:
        return dt.datetime.fromisoformat("%s:%s.%s%s" % (
            m.group(1), seconds, fraction, offset))
    except ValueError:
        return None


def partial_date_end(value):
    """Last day covered by YYYY, YYYY-MM, or YYYY-MM-DD; None if invalid."""
    if not isinstance(value, str):
        return None
    m = PARTIAL_DATE_RE.match(value)
    if not m:
        return None
    year = int(m.group(1))
    try:
        if m.group(3):
            return dt.date(year, int(m.group(2)), int(m.group(3)))
        if m.group(2):
            month = int(m.group(2))
            if not 1 <= month <= 12:
                return None
            nxt = dt.date(year + (month == 12), month % 12 + 1, 1)
            return nxt - dt.timedelta(days=1)
        return dt.date(year, 12, 31)
    except ValueError:
        return None


def partial_date_start(value):
    m = PARTIAL_DATE_RE.match(value) if isinstance(value, str) else None
    if not m:
        return None
    try:
        return dt.date(int(m.group(1)), int(m.group(2) or 1),
                       int(m.group(3) or 1))
    except ValueError:
        return None


def is_str(value):
    return isinstance(value, str) and value.strip() != ""


def as_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def str_items(value):
    """The string items of a scalar-or-list value."""
    return [v for v in as_list(value) if isinstance(v, str)]


def is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


# --------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------

class Checker:
    def __init__(self, bundle: Bundle, today: dt.date, final=False):
        self.b = bundle
        self.today = today
        self.now = dt.datetime.combine(today, dt.time(23, 59, 59),
                                       tzinfo=dt.timezone.utc)
        self.final = final

    def run(self):
        b = self.b
        if not b.docs:
            b.error(".", 1, "no markdown files found; is this the bundle "
                    "directory?")
            return
        for doc in b.docs.values():
            self.check_okf(doc)
        if "index.md" not in b.docs:
            b.warn("index.md", 1, "bundle root has no index.md (run "
                   "`kb.py index`)")
        for rel, typ in ROOT_TYPES.items():
            doc = b.get(rel)
            if doc is None:
                if rel != "audit.md" or self.final:
                    b.error(rel, 1, "missing %s (type: %s)" % (rel, typ))
            elif isinstance(doc.meta, dict) and doc.type != typ:
                b.error(rel, doc.line_of("type"),
                        "%s must have type: %s" % (rel, typ))
        self.cited_sources = set()
        for doc in b.concepts():
            try:
                self.check_common(doc)
            except Exception as exc:  # keep checking the other files
                b.error(doc.rel, 1, "could not check this document (%s: %s);"
                        " check the types of its frontmatter values"
                        % (type(exc).__name__, exc))
            handler = {
                "Research Brief": self.check_brief,
                "Research Plan": self.check_plan,
                "Research Audit": self.check_audit,
                "Finding": self.check_finding,
                "Recommendation": self.check_recommendation,
                "Source": self.check_source,
                "Review": self.check_review,
            }.get(doc.type)
            if handler:
                try:
                    handler(doc)
                except Exception as exc:  # keep checking the other files
                    b.error(doc.rel, 1, "could not check this document "
                            "(%s: %s); check the types of its frontmatter "
                            "values" % (type(exc).__name__, exc))
        self.check_duplicate_resources()
        for doc in b.concepts("Source"):
            if doc.rel not in self.cited_sources:
                b.warn(doc.rel, 1, "source is not cited by any Finding or "
                       "Recommendation")
        self.check_indexes()
        self.gate_rounds = self.gates()
        if self.final:
            self.check_final()

    # -- OKF ----------------------------------------------------------------

    def check_okf(self, doc):
        b = self.b
        if doc.error is not None:
            b.error(doc.rel, doc.error.line, "frontmatter: %s"
                    % doc.error.message)
            return
        for line, note in doc.notes:
            b.warn(doc.rel, line, "frontmatter: %s" % note)
        if doc.name == "index.md":
            if doc.has_frontmatter:
                keys = set(doc.meta or {})
                if doc.rel != "index.md":
                    b.error(doc.rel, 1, "index.md below the bundle root must "
                            "not have frontmatter (OKF §8)")
                elif keys - {"okf_version"}:
                    b.error(doc.rel, 1, "root index.md frontmatter may only "
                            "contain okf_version (OKF §8)")
            if doc.rel == "index.md":
                ver = (doc.meta or {}).get("okf_version")
                if ver is None:
                    b.warn(doc.rel, 1, "root index.md should declare "
                           "okf_version: \"%s\"" % OKF_VERSION)
                elif str(ver) != OKF_VERSION:
                    b.warn(doc.rel, 1, "okf_version is %r; this checker "
                           "knows %s" % (ver, OKF_VERSION))
            self.check_links(doc)
            return
        if doc.name == "log.md":
            self.check_log(doc)
            return
        if not doc.has_frontmatter:
            b.error(doc.rel, 1, "concept document has no YAML frontmatter "
                    "(OKF §11)")
            return
        if not isinstance(doc.meta, dict) or not is_str(doc.meta.get("type")):
            b.error(doc.rel, doc.line_of("type"),
                    "frontmatter has no non-empty 'type' (OKF §4.1)")
            return
        self.check_links(doc)

    def check_log(self, doc):
        lines = doc.body_lines()
        mask = code_mask(lines)
        dates = []
        for idx, line in enumerate(lines):
            if mask[idx]:
                continue
            h = match_heading(line)
            if h and h[0] == 2:
                text = h[1]
                lineno = doc.body_line + idx
                if not re.match(r"^\d{4}-\d{2}-\d{2}$", text) or \
                        partial_date_end(text) is None:
                    self.b.error(doc.rel, lineno, "log date heading %r is "
                                 "not an ISO 8601 YYYY-MM-DD date (OKF §9)"
                                 % text)
                    continue
                dates.append((text, lineno))
        for (prev, _), (cur, lineno) in zip(dates, dates[1:]):
            if cur > prev:
                self.b.error(doc.rel, lineno, "log entries must be newest "
                             "first (%s after %s)" % (cur, prev))
        for text, lineno in dates:
            if partial_date_end(text) > self.today + dt.timedelta(days=1):
                self.b.error(doc.rel, lineno, "log date %s is in the future"
                             % text)

    def check_links(self, doc):
        lines = doc.body_lines()
        mask = code_mask(lines)
        for idx, line in enumerate(lines):
            if mask[idx]:
                continue
            for m in LINK_RE.finditer(strip_inline_code(line)):
                target = m.group(1)
                if URL_SCHEME_RE.match(target) or target.startswith("#"):
                    continue
                rel, outside = self.b.resolve(doc.rel, target)
                lineno = doc.body_line + idx
                if outside:
                    self.b.warn(doc.rel, lineno, "link %r points outside the "
                                "bundle" % target)
                elif rel is not None and rel not in self.b.docs and \
                        rel not in self.b.dirs and \
                        not (self.b.root / rel).exists():
                    self.b.warn(doc.rel, lineno, "broken link %r" % target)

    # -- common -------------------------------------------------------------

    def check_common(self, doc):
        b, m = self.b, doc.meta
        typ = doc.type
        if typ not in PROFILE_TYPES:
            b.warn(doc.rel, doc.line_of("type"), "type %r is not part of the "
                   "evidence-kb profile" % typ)
            return
        bad = [p for p in PurePosixPath(doc.rel).parts
               if not NAME_RE.match(p)]
        if bad:
            b.error(doc.rel, 1, "file and directory names must be lowercase "
                    "kebab-case ASCII (a-z, 0-9, -): %s" % ", ".join(bad))
        folder = PROFILE_TYPES[typ]
        parent = str(PurePosixPath(doc.rel).parent)
        if folder == "":
            if parent != "." or ROOT_TYPES.get(doc.name) != typ:
                b.error(doc.rel, doc.line_of("type"), "a %s belongs at the "
                        "bundle root as %s" % (typ, {v: k for k, v in
                                                     ROOT_TYPES.items()}[typ]))
        elif typ == "Finding":
            parts = PurePosixPath(doc.rel).parts
            if len(parts) != 3 or parts[0] != "findings":
                b.error(doc.rel, 1, "a Finding belongs in "
                        "findings/<topic-dir>/")
        elif PurePosixPath(doc.rel).parts[0] != folder or \
                len(PurePosixPath(doc.rel).parts) != 2:
            b.error(doc.rel, 1, "a %s belongs in %s/" % (typ, folder))
        for key in ("title", "description"):
            if not is_str(m.get(key)):
                b.error(doc.rel, doc.line_of(key) if key in m else 1,
                        "missing or empty '%s'" % key)
        status = m.get("status")
        if status is not None and status not in STATUS:
            b.error(doc.rel, doc.line_of("status"), "status must be one of "
                    "%s" % ", ".join(STATUS))
        if typ in ("Finding", "Recommendation", "Source") and status is None:
            b.error(doc.rel, 1, "missing 'status' (draft until its review "
                    "gate passes)")
        gen = m.get("generated")
        if gen is None:
            b.error(doc.rel, 1, "missing 'generated: { by, at }'")
        else:
            self.check_event(doc, "generated", gen)
        if "verified" in m:
            for ev in as_list(m.get("verified")):
                self.check_event(doc, "verified", ev)
        if typ in ("Finding", "Recommendation", "Source") and \
                status == "stable":
            self.check_stable(doc)
        if "stale_after" in m:
            ts = parse_timestamp(m.get("stale_after"))
            if ts is None:
                b.error(doc.rel, doc.line_of("stale_after"), "stale_after "
                        "must be an ISO 8601 datetime with UTC offset")
            elif ts <= self.now:
                b.warn(doc.rel, doc.line_of("stale_after"), "concept is stale"
                       " (stale_after %s)" % m["stale_after"])
        if "tags" in m and not (isinstance(m["tags"], list) and
                                all(is_str(t) for t in m["tags"])):
            b.error(doc.rel, doc.line_of("tags"), "tags must be a list of "
                    "strings")
        self.check_sections(doc)
        if typ in CLAIM_SECTIONS or "sources" in m:
            self.check_sources_field(doc)

    def check_stable(self, doc):
        b, m = self.b, doc.meta
        events = [e for e in as_list(m.get("verified")) if isinstance(e, dict)]
        trusted = [e for e in events if isinstance(e.get("by"), str) and
                   e["by"].startswith(("evidence-kb-reviewer/", "human:"))]
        line = doc.line_of("status")
        if not events:
            b.error(doc.rel, line, "status: stable needs a 'verified' entry "
                    "from its review gate")
            return
        if not trusted:
            b.error(doc.rel, line, "status: stable needs a 'verified' entry "
                    "by evidence-kb-reviewer/... or human:...; authors and "
                    "self-checks keep a document draft")
            return
        gen = m.get("generated")
        gen_at = parse_timestamp(gen.get("at")) if isinstance(gen, dict) \
            else None
        times = [parse_timestamp(e.get("at")) for e in trusted]
        times = [t for t in times if t is not None]
        if gen_at and times and gen_at > max(times):
            b.error(doc.rel, line, "changed after its last verification "
                    "(generated.at is later than verified.at); set status: "
                    "draft until a gate round reviews it again")

    def check_event(self, doc, key, ev):
        b = self.b
        if not isinstance(ev, dict):
            b.error(doc.rel, doc.line_of(key), "%s entries must be "
                    "{ by, at } mappings" % key)
            return
        by = ev.get("by")
        if not isinstance(by, str) or not ACTOR_RE.match(by):
            b.error(doc.rel, doc.line_of(key), "%s.by %r is not an actor "
                    "(<producer>/<version>, human:<id>, process:<id>)"
                    % (key, by))
        at = parse_timestamp(ev.get("at"))
        if at is None:
            b.error(doc.rel, doc.line_of(key), "%s.at must be an ISO 8601 "
                    "datetime with UTC offset, e.g. 2026-01-31T12:00:00Z"
                    % key)
        elif at > self.now + dt.timedelta(days=1):
            b.error(doc.rel, doc.line_of(key), "%s.at %s is in the future"
                    % (key, ev.get("at")))

    def check_sections(self, doc):
        required = REQUIRED_SECTIONS.get(doc.type, ())
        lines = doc.body_lines()
        mask = code_mask(lines)
        secs = sections(lines, mask)
        for title in required:
            if title.lower() not in secs:
                self.b.error(doc.rel, doc.body_line, "missing section "
                             "'# %s'" % title)
                continue
            start, end = secs[title.lower()]
            if not any(lines[i].strip() for i in range(start, end)
                       if not match_heading(lines[i])):
                self.b.error(doc.rel, doc.body_line + start - 1,
                             "section '# %s' is empty" % title)
        claim_sec = CLAIM_SECTIONS.get(doc.type)
        if claim_sec and claim_sec.lower() in secs:
            start, end = secs[claim_sec.lower()]
            for idx, text in claim_blocks(lines, start, end, mask):
                clean = strip_inline_code(text)
                clean_md = re.sub(r"^([-*+]|\d+[.)])\s+", "", clean)
                clean_md = re.sub(r"^\|\s*", "", clean_md)
                if FOOTREF_RE.search(clean) or \
                        CLAIM_MARKERS.match(clean_md.replace("*", "")
                                            .replace("_", "")):
                    continue
                self.b.error(doc.rel, doc.body_line + idx, "claim in "
                             "'# %s' has no source footnote and is not "
                             "marked 'Inference:' or 'Gap:'" % claim_sec)

    def check_sources_field(self, doc):
        b, m = self.b, doc.meta
        entries = m.get("sources")
        line = doc.line_of("sources")
        if doc.type in CLAIM_SECTIONS and not entries:
            b.error(doc.rel, line if "sources" in m else 1,
                    "%s needs at least one entry in 'sources'" % doc.type)
        if entries is not None and not isinstance(entries, list):
            b.error(doc.rel, line, "sources must be a list")
            return
        ids = []
        for entry in entries or []:
            line = doc.item_line(entry, "sources")
            if not isinstance(entry, dict):
                b.error(doc.rel, line, "sources entries must be mappings")
                continue
            res, sid = entry.get("resource"), entry.get("id")
            if not is_str(res):
                b.error(doc.rel, line, "sources entry without 'resource' "
                        "(OKF §5.1)")
                continue
            if not is_str(sid):
                b.error(doc.rel, line, "sources entry %r needs an 'id'" % res)
            else:
                ids.append(sid)
            if doc.type in CLAIM_SECTIONS:
                if URL_SCHEME_RE.match(res):
                    b.error(doc.rel, line, "source %r: cite external material"
                            " through a Source record in references/, not a "
                            "URL" % res)
                    continue
                src = b.source_doc(doc, entry)
                if src is None:
                    b.error(doc.rel, line, "source %r does not resolve to a "
                            "Source document" % res)
                    continue
                self.cited_sources.add(src.rel)
                stem = PurePosixPath(src.rel).stem
                if is_str(sid) and sid != stem:
                    b.error(doc.rel, line, "source id %r must equal the "
                            "Source file name %r" % (sid, stem))
        line = doc.line_of("sources")
        for sid, count in Counter(ids).items():
            if count > 1:
                b.error(doc.rel, line, "duplicate source id %r" % sid)
        # Footnotes.
        lines = doc.body_lines()
        mask = code_mask(lines)
        refs, defs = {}, {}
        for idx, raw in enumerate(lines):
            if mask[idx]:
                continue
            text = strip_inline_code(raw)
            dm = FOOTDEF_RE.match(text.strip())
            if dm:
                defs.setdefault(dm.group(1), doc.body_line + idx)
                text = text.strip()[dm.end():]
            for rm in FOOTREF_RE.finditer(text):
                refs.setdefault(rm.group(1), doc.body_line + idx)
        for label, lineno in refs.items():
            if label not in ids:
                b.error(doc.rel, lineno, "footnote [^%s] does not match any "
                        "sources id" % label)
            if label not in defs:
                b.error(doc.rel, lineno, "footnote [^%s] has no definition"
                        % label)
        for label, lineno in defs.items():
            if label not in refs:
                b.warn(doc.rel, lineno, "footnote [^%s] is defined but never"
                       " used" % label)
        for sid in ids:
            if sid not in refs and doc.type in CLAIM_SECTIONS:
                b.warn(doc.rel, line, "source %r is listed but never cited "
                       "in the body" % sid)

    # -- per type -----------------------------------------------------------

    def check_brief(self, doc):
        if doc.meta.get("kb_profile") != PROFILE:
            self.b.error(doc.rel, doc.line_of("kb_profile"), "kb_profile "
                         "must be %r" % PROFILE)

    def check_plan(self, doc):
        b, m = self.b, doc.meta
        decisions = m.get("decisions")
        dec_ids = set()
        if not isinstance(decisions, list) or not decisions:
            b.error(doc.rel, doc.line_of("decisions"), "plan needs a "
                    "non-empty 'decisions' list")
            decisions = []
        for d in decisions:
            dline = doc.item_line(d, "decisions")
            if not isinstance(d, dict) or not isinstance(d.get("id"), str) \
                    or not DECISION_ID_RE.match(d["id"]) or \
                    not is_str(d.get("title")):
                b.error(doc.rel, dline, "each decision needs id (D<n>) and "
                        "title: %r" % (d,))
                continue
            if d["id"] in dec_ids:
                b.error(doc.rel, dline, "duplicate decision id %s" % d["id"])
            dec_ids.add(d["id"])
        topics = m.get("topics")
        if not isinstance(topics, list) or not topics:
            b.error(doc.rel, doc.line_of("topics"), "plan needs a non-empty "
                    "'topics' list")
            return
        seen_t, seen_q, used_decisions = set(), set(), set()
        body = doc.body
        for t in topics:
            line = doc.item_line(t, "topics")
            if not isinstance(t, dict):
                b.error(doc.rel, line, "topics entries must be mappings")
                continue
            tid = t.get("id")
            if not isinstance(tid, str) or not TOPIC_ID_RE.match(tid):
                b.error(doc.rel, line, "topic id %r must look like T1"
                        % (tid,))
                continue
            if tid in seen_t:
                b.error(doc.rel, line, "duplicate topic id %s" % tid)
            seen_t.add(tid)
            if not is_str(t.get("title")):
                b.error(doc.rel, line, "topic %s needs a title" % tid)
            if t.get("priority") not in PRIORITIES:
                b.error(doc.rel, line, "topic %s priority must be must or "
                        "nice" % tid)
            tdecs = str_items(t.get("decisions"))
            if len(tdecs) != len(as_list(t.get("decisions"))):
                b.error(doc.rel, line, "topic %s decisions must be decision "
                        "ids" % tid)
            for did in tdecs:
                used_decisions.add(did)
                if did not in dec_ids:
                    b.error(doc.rel, line, "topic %s refers to unknown "
                            "decision %r" % (tid, did))
            mam = t.get("max_age_months")
            if mam is not None and (not is_int(mam) or mam <= 0):
                b.error(doc.rel, line, "topic %s max_age_months must be a "
                        "positive integer" % tid)
            qs = t.get("questions")
            if not isinstance(qs, list) or not qs:
                b.error(doc.rel, line, "topic %s needs a non-empty "
                        "'questions' list" % tid)
                qs = []
            for q in qs:
                qline = doc.item_line(q, "topics")
                qid = q.get("id") if isinstance(q, dict) else None
                if not isinstance(qid, str) or \
                        not re.match(r"^%s\.\d+$" % re.escape(tid), qid):
                    b.error(doc.rel, qline, "question id %r in topic %s must "
                            "look like %s.1" % (qid, tid, tid))
                    continue
                if not is_str(q.get("text")):
                    b.error(doc.rel, qline, "question %s needs text" % qid)
                if qid in seen_q:
                    b.error(doc.rel, qline, "duplicate question id %s" % qid)
                seen_q.add(qid)
                if not re.search(r"\b%s\b" % re.escape(qid), body):
                    b.warn(doc.rel, qline, "question %s is not mentioned in "
                           "the plan body" % qid)
            if not re.search(r"\b%s\b" % re.escape(tid), body):
                b.warn(doc.rel, line, "topic %s is not mentioned in the plan "
                       "body" % tid)
        for did in sorted(dec_ids - used_decisions):
            b.warn(doc.rel, doc.line_of("decisions"), "decision %s is not "
                   "supported by any topic" % did)

    def check_audit(self, doc):
        b, m = self.b, doc.meta
        known = set(b.plan_topics()) | set(b.plan_questions())
        for key in ("gaps", "unresolved"):
            if key not in m:
                b.error(doc.rel, 1, "audit needs '%s' (use [] for none)" % key)
            elif not isinstance(m[key], list):
                b.error(doc.rel, doc.line_of(key), "%s must be a list" % key)
        for g in as_list(m.get("gaps")) if isinstance(m.get("gaps"),
                                                     list) else []:
            gline = doc.item_line(g, "gaps")
            if not isinstance(g, dict) or not is_str(g.get("id")) or \
                    not is_str(g.get("reason")) or \
                    not is_str(g.get("to_close")):
                b.error(doc.rel, gline, "each gap needs id, reason, and "
                        "to_close: %r" % (g,))
            elif known and g["id"] not in known:
                b.error(doc.rel, gline, "gap %r is not a topic or question id "
                        "of the plan" % g["id"])
        for u in as_list(m.get("unresolved")) if isinstance(
                m.get("unresolved"), list) else []:
            if not isinstance(u, dict) or not isinstance(u.get("gate"), str) \
                    or not GATE_RE.match(u["gate"]) or \
                    not is_str(u.get("summary")):
                b.error(doc.rel, doc.item_line(u, "unresolved"), "each "
                        "unresolved entry needs gate and summary: %r" % (u,))

    def check_finding(self, doc):
        b, m = self.b, doc.meta
        topics, questions = b.plan_topics(), b.plan_questions()
        tid = m.get("topic")
        if not is_str(tid):
            b.error(doc.rel, 1, "Finding needs 'topic' (a plan topic id)")
        elif topics and tid not in topics:
            b.error(doc.rel, doc.line_of("topic"), "topic %r is not in the "
                    "plan" % tid)
        elif is_str(tid):
            parts = PurePosixPath(doc.rel).parts
            if len(parts) == 3 and not re.match(
                    r"^%s(-|$)" % re.escape(tid.lower()), parts[1]):
                b.error(doc.rel, doc.line_of("topic"), "a Finding for %s "
                        "belongs in findings/%s-<slug>/" % (tid, tid.lower()))
        qids = str_items(m.get("question"))
        if len(qids) != len(as_list(m.get("question"))):
            b.error(doc.rel, doc.line_of("question"), "question must be a "
                    "question id or a list of ids")
        if not qids:
            b.error(doc.rel, 1, "Finding needs 'question' (plan question id "
                    "or list of ids)")
        for qid in qids:
            if questions and qid not in questions:
                b.error(doc.rel, doc.line_of("question"), "question %r is not"
                        " in the plan" % (qid,))
            elif questions and questions[qid][0] != tid:
                b.error(doc.rel, doc.line_of("question"), "question %s "
                        "belongs to topic %s, not %s"
                        % (qid, questions[qid][0], tid))
        topic = topics.get(tid) if is_str(tid) else None
        if topic and topic.get("max_age_months") and "stale_after" not in m:
            b.warn(doc.rel, 1, "topic %s has max_age_months; set stale_after "
                   "to generated.at plus %s months" % (tid,
                                                       topic["max_age_months"]))
        if m.get("confidence") not in CONFIDENCE:
            b.error(doc.rel, doc.line_of("confidence"), "confidence must be "
                    "one of %s" % ", ".join(CONFIDENCE))

    def check_recommendation(self, doc):
        b, m = self.b, doc.meta
        if m.get("strength") not in STRENGTH:
            b.error(doc.rel, doc.line_of("strength"), "strength must be one "
                    "of %s" % ", ".join(STRENGTH))
        decisions = b.plan_decisions()
        decs = str_items(m.get("decisions"))
        if len(decs) != len(as_list(m.get("decisions"))):
            b.error(doc.rel, doc.line_of("decisions"), "decisions must be "
                    "decision ids")
        if not decs:
            b.error(doc.rel, 1, "Recommendation needs 'decisions' (plan "
                    "decision ids)")
        for did in decs:
            if decisions and did not in decisions:
                b.error(doc.rel, doc.line_of("decisions"), "decision %r is "
                        "not in the plan" % (did,))
        based = as_list(m.get("based_on"))
        if not based:
            b.error(doc.rel, 1, "Recommendation needs 'based_on' (paths of "
                    "the Findings it follows from)")
        for path in based:
            rel = b.resolve(doc.rel, path)[0] if isinstance(path, str) \
                else None
            target = b.get(rel) if rel else None
            if target is None or target.type != "Finding":
                b.error(doc.rel, doc.line_of("based_on"), "based_on %r does "
                        "not resolve to a Finding" % (path,))
        allowed = set()
        for path in based:
            rel = b.resolve(doc.rel, path)[0] if isinstance(path, str) \
                else None
            target = b.get(rel) if rel else None
            if target is not None and target.type == "Finding":
                for e in as_list(target.meta.get("sources")):
                    src = b.source_doc(target, e)
                    if src is not None:
                        allowed.add(src.rel)
        srcs = [b.source_doc(doc, e) for e in as_list(m.get("sources"))]
        srcs = [s for s in srcs if s is not None and
                isinstance(s.meta, dict)]
        for src in srcs:
            if src.rel not in allowed:
                b.error(doc.rel, doc.line_of("sources"), "source %s is not a "
                        "source of any based_on Finding; add it to a Finding "
                        "and fact-check it there first"
                        % PurePosixPath(src.rel).stem)
        if srcs and not any(s.meta.get("commercial_interest") is False
                            for s in srcs):
            b.error(doc.rel, doc.line_of("sources"), "a Recommendation needs"
                    " at least one source with commercial_interest: false")
        if m.get("strength") == "must" and srcs and not any(
                s.meta.get("evidence_tier") == "primary" for s in srcs):
            b.error(doc.rel, doc.line_of("strength"), "strength 'must' needs "
                    "at least one primary-tier source")

    def check_source(self, doc):
        b, m = self.b, doc.meta
        req = ("resource", "publisher", "publisher_type", "source_type",
               "evidence_tier", "commercial_interest", "country", "language",
               "published", "updated", "retrieved", "access")
        for key in req:
            if key not in m or m[key] is None or m[key] == "":
                b.error(doc.rel, doc.line_of(key) if key in m else 1,
                        "Source needs '%s' (write \"unknown\" if it cannot "
                        "be determined)" % key if key in (
                            "published", "updated", "country", "language")
                        else "Source needs '%s'" % key)

        def enum(key, allowed):
            if key in m and m[key] not in allowed:
                b.error(doc.rel, doc.line_of(key), "%s must be one of %s"
                        % (key, ", ".join(str(a).lower() for a in allowed)))

        enum("publisher_type", PUBLISHER_TYPES)
        enum("source_type", SOURCE_TYPES)
        enum("evidence_tier", TIERS)
        enum("access", ACCESS)
        ci = m.get("commercial_interest")
        if "commercial_interest" in m and not (
                ci is True or ci is False or ci == "unknown"):
            b.error(doc.rel, doc.line_of("commercial_interest"),
                    "commercial_interest must be true, false, or unknown")
        if m.get("commercial_interest") is True and \
                not is_str(m.get("commercial_interest_note")):
            b.error(doc.rel, doc.line_of("commercial_interest"),
                    "commercial_interest: true needs "
                    "commercial_interest_note")
        res = m.get("resource")
        if is_str(res) and not URL_SCHEME_RE.match(res):
            b.error(doc.rel, doc.line_of("resource"), "resource must be an "
                    "absolute URI (https://..., doi:..., urn:isbn:...)")
        country = m.get("country")
        if country is not None and country != "unknown" and not (
                isinstance(country, str) and country in ISO_COUNTRIES):
            b.error(doc.rel, doc.line_of("country"), "country %r is not an "
                    "ISO 3166-1 alpha-2 code (GB, not UK), EU, INT, or "
                    "unknown" % (country,))
        lang = m.get("language")
        if lang is not None and (not isinstance(lang, str) or
                                 not LANGUAGE_RE.match(lang)):
            b.error(doc.rel, doc.line_of("language"), "language %r is not "
                    "valid (BCP 47 like en, de, pt-BR, or unknown)" % (lang,))
        jur = m.get("jurisdiction")
        if "jurisdiction" in m and not (isinstance(jur, str) and
                                        jur in ISO_COUNTRIES):
            b.error(doc.rel, doc.line_of("jurisdiction"), "jurisdiction must "
                    "be an ISO 3166-1 alpha-2 code, EU, or INT")
        dates = {}
        for key in ("published", "updated"):
            val = m.get(key)
            if val is None or val == "unknown":
                continue
            if isinstance(val, int) and not isinstance(val, bool):
                val = str(val)
            end = partial_date_end(val)
            if end is None:
                b.error(doc.rel, doc.line_of(key), "%s %r must be YYYY, "
                        "YYYY-MM, YYYY-MM-DD, or unknown" % (key, m.get(key)))
                continue
            if partial_date_start(val) > self.today:
                b.error(doc.rel, doc.line_of(key), "%s %s is in the future"
                        % (key, val))
            dates[key] = val
        if "published" in dates and "updated" in dates and \
                partial_date_end(dates["updated"]) < \
                partial_date_start(dates["published"]):
            b.error(doc.rel, doc.line_of("updated"), "updated is before "
                    "published")
        if "retrieved" in m:
            ts = parse_timestamp(m.get("retrieved"))
            if ts is None:
                b.error(doc.rel, doc.line_of("retrieved"), "retrieved must be "
                        "an ISO 8601 datetime with UTC offset")
            elif ts > self.now + dt.timedelta(days=1):
                b.error(doc.rel, doc.line_of("retrieved"), "retrieved is in "
                        "the future")
        tier = m.get("evidence_tier")
        if tier in ("secondary", "tertiary") and "original" not in m:
            b.warn(doc.rel, 1, "a %s source should name its original in "
                   "'original' (Source path, URL, or unknown)" % tier)
        lines = doc.body_lines()
        mask = code_mask(lines)
        secs = sections(lines, mask)
        if "evidence" in secs:
            start, end = secs["evidence"]
            if not any(lines[i].lstrip().startswith(">") and not mask[i]
                       for i in range(start, end)):
                b.error(doc.rel, doc.body_line + start - 1, "'# Evidence' "
                        "needs at least one verbatim quote as a '>' "
                        "blockquote")

    def check_review(self, doc):
        b, m = self.b, doc.meta
        gate = m.get("gate")
        if not isinstance(gate, str) or not GATE_RE.match(gate):
            b.error(doc.rel, doc.line_of("gate"), "gate must be plan, "
                    "research:T<n>, synthesis, or final")
        elif gate.startswith("research:") and b.plan_topics() and \
                gate.split(":", 1)[1] not in b.plan_topics():
            b.error(doc.rel, doc.line_of("gate"), "gate %s names a topic that"
                    " is not in the plan" % gate)
        persp = m.get("perspective")
        allowed = REQUIRED_PERSPECTIVES.get(gate) if isinstance(gate, str) \
            else None
        if allowed and persp not in allowed:
            b.error(doc.rel, doc.line_of("perspective") if "perspective" in m
                    else 1, "a review of gate %s needs perspective: %s"
                    % (gate, " or ".join(allowed)))
        elif not allowed and persp is not None:
            b.error(doc.rel, doc.line_of("perspective"), "perspective is only"
                    " used by the plan gate; remove it")
        rnd = m.get("round")
        if not isinstance(rnd, int) or isinstance(rnd, bool) or rnd < 1:
            b.error(doc.rel, doc.line_of("round"), "round must be an integer "
                    ">= 1")
        cyc = m.get("cycle", 1)
        if not isinstance(cyc, int) or isinstance(cyc, bool) or cyc < 1:
            b.error(doc.rel, doc.line_of("cycle"), "cycle must be an integer "
                    ">= 1")
        self.check_review_body(doc)
        if m.get("verdict") not in VERDICTS:
            b.error(doc.rel, doc.line_of("verdict"), "verdict must be pass or"
                    " revise")
        reviewed = as_list(m.get("reviewed"))
        if not reviewed:
            b.error(doc.rel, 1, "Review needs 'reviewed' (paths of the "
                    "reviewed documents)")
        for path in reviewed:
            rel, outside = b.resolve(doc.rel, path) if isinstance(path, str) \
                else (None, False)
            if not isinstance(path, str) or outside or rel is None or not (
                    rel == "" or rel in b.docs or rel.rstrip("/") in b.dirs):
                b.error(doc.rel, doc.line_of("reviewed"), "reviewed path %r "
                        "does not exist in the bundle" % (path,))
        if isinstance(gate, str) and GATE_RE.match(gate) and is_int(rnd):
            stem = gate.replace(":", "-").lower()
            if isinstance(persp, str) and allowed and persp in allowed:
                stem += "-" + persp
            cyc = m.get("cycle", 1)
            if is_int(cyc) and cyc > 1:
                stem += "-c%d" % cyc
            expected = "%s-r%d.md" % (stem, rnd)
            if doc.name != expected:
                b.warn(doc.rel, 1, "Review file name should be %s (from gate,"
                       " perspective, cycle, and round)" % expected)

    def check_review_body(self, doc):
        b, verdict = self.b, doc.meta.get("verdict")
        lines = doc.body_lines()
        mask = code_mask(lines)
        secs = sections(lines, mask)
        if "verdict" not in secs:
            b.error(doc.rel, doc.body_line, "missing section '# Verdict' (the"
                    " reviewer's report, verbatim)")
        else:
            start, end = secs["verdict"]
            first = next((lines[i].strip() for i in range(start, end)
                          if lines[i].strip() and not mask[i]), "")
            word = re.match(r"^\W*(pass|revise)\b", first, re.IGNORECASE)
            if not word:
                b.error(doc.rel, doc.body_line + start, "'# Verdict' must "
                        "start with pass or revise")
            elif verdict in VERDICTS and word.group(1).lower() != verdict:
                b.error(doc.rel, doc.line_of("verdict"), "frontmatter verdict "
                        "%r does not match the report's verdict %r"
                        % (verdict, word.group(1).lower()))
        if verdict == "pass" and "objections" in secs:
            start, end = secs["objections"]
            col, open_rows = None, []
            for i in range(start, end):
                row = lines[i].strip()
                if mask[i] or not row.startswith("|"):
                    continue
                cells = [c.strip().strip("*_ ").lower()
                         for c in row.strip("|").split("|")]
                if col is None:
                    if "severity" in cells:
                        col = cells.index("severity")
                    continue
                if col < len(cells) and cells[col].strip("* ") in (
                        "blocking", "major"):
                    open_rows.append(doc.body_line + i)
            for lineno in open_rows:
                b.error(doc.rel, lineno, "verdict is pass, but this objection"
                        " is blocking or major")

    # -- bundle-wide --------------------------------------------------------

    def check_duplicate_resources(self):
        seen = {}
        for doc in self.b.concepts("Source"):
            res = doc.meta.get("resource")
            if not is_str(res):
                continue
            key = res.strip().rstrip("/").lower()
            if key in seen:
                self.b.error(doc.rel, doc.line_of("resource"), "same resource"
                             " as %s; reuse that Source instead" % seen[key])
            else:
                seen[key] = doc.rel

    def check_indexes(self):
        for rel, content in build_indexes(self.b).items():
            doc = self.b.get(rel)
            if doc is None:
                if rel != "index.md":
                    self.b.warn(rel, 1, "missing index.md (run `kb.py "
                                "index`)")
            elif doc.text != content:
                self.b.warn(rel, 1, "index.md is out of date (run "
                            "`kb.py index`)")

    def gates(self):
        """Report duplicate rounds; return gate_rounds() of the bundle."""
        seen = Counter()
        first = {}
        for gate, persp, cyc, rnd, _v, rel in review_records(self.b):
            seen[(gate, persp, cyc, rnd)] += 1
            first.setdefault((gate, persp, cyc, rnd), rel)
        for (gate, persp, cyc, rnd), count in sorted(seen.items()):
            if count > 1:
                self.b.error(first[(gate, persp, cyc, rnd)], 1, "gate %s%s "
                             "has %d reviews for %sround %d" % (
                                 gate, " (%s)" % persp if persp else "",
                                 count, "cycle %d, " % cyc if cyc > 1 else "",
                                 rnd))
        return gate_rounds(self.b)

    def check_final(self):
        b = self.b
        audit = b.get("audit.md")
        audit_meta = audit.meta if audit and isinstance(audit.meta, dict) \
            else {}
        gaps = {g.get("id") for g in as_list(audit_meta.get("gaps"))
                if isinstance(g, dict) and isinstance(g.get("id"), str)}
        unresolved = {u.get("gate") for u in
                      as_list(audit_meta.get("unresolved"))
                      if isinstance(u, dict) and
                      isinstance(u.get("gate"), str)}
        answered = set()
        for doc in b.concepts("Finding"):
            if doc.meta.get("status") != "deprecated":
                answered.update(str_items(doc.meta.get("question")))
        for qid, (tid, _q) in b.plan_questions().items():
            topic = b.plan_topics()[tid]
            if topic.get("priority") == "must" and qid not in answered and \
                    qid not in gaps and tid not in gaps:
                b.error("plan.md", 1, "must-have question %s has no Finding "
                        "and is not declared as a gap in audit.md" % qid)
        gates = self.gate_rounds
        expected = {"plan", "final"}
        expected |= {"research:%s" % d.meta.get("topic")
                     for d in b.concepts("Finding")
                     if is_str(d.meta.get("topic"))}
        if any(True for _ in b.concepts("Recommendation")):
            expected.add("synthesis")
        for gate in sorted(expected | set(gates), key=gate_sort):
            perspectives = gates.get(gate, {})
            needed = REQUIRED_PERSPECTIVES.get(gate, ("",))
            for persp in sorted(set(needed) | set(perspectives)):
                label = "%s (%s)" % (gate, persp) if persp else gate
                rounds = perspectives.get(persp)
                if not rounds:
                    if gate not in unresolved:
                        b.error("reviews", 1, "gate %s has no Review record"
                                % label)
                    continue
                last_round, verdict, rel = rounds[-1]
                if verdict != "pass" and gate not in unresolved:
                    b.error(rel, 1, "last round (%d) of gate %s did not pass"
                            " and audit.md does not list it as unresolved"
                            % (last_round, label))
        covered = self.covered_by_passing_reviews()
        for typ in ("Finding", "Recommendation", "Source"):
            for doc in b.concepts(typ):
                if doc.meta.get("status") == "stable" and \
                        doc.rel not in covered:
                    b.error(doc.rel, doc.line_of("status"), "status: stable, "
                            "but no passing round of its own gate has "
                            "reviewed this version (check 'reviewed', the "
                            "gate, and the timestamps)")
        if not any(True for _ in b.concepts("Recommendation")):
            b.warn("recommendations", 1, "the knowledge base has no "
                   "Recommendations")

    def covered_by_passing_reviews(self):
        """Documents that a passing Review of *their own* gate checked in
        their current version.

        A Review covers a document only if (1) the document is in the
        Review's `reviewed` list, directly or through a listed directory,
        (2) the document is in the gate's scope (research:T<n>: the topic's
        Findings and the Sources they cite; synthesis: Recommendations;
        plan and final: nothing that can be stable), and (3) the Review is
        not older than the document's generated.at.
        """
        b = self.b
        covered = set()

        def gen_at(doc):
            g = doc.meta.get("generated")
            return parse_timestamp(g.get("at")) if isinstance(g, dict) \
                else None

        for review in b.concepts("Review"):
            gate = review.meta.get("gate")
            if review.meta.get("verdict") != "pass" or \
                    not isinstance(gate, str):
                continue
            listed = set()
            for path in as_list(review.meta.get("reviewed")):
                if not isinstance(path, str):
                    continue
                rel, outside = b.resolve(review.rel, path)
                if outside or rel is None:
                    continue
                if rel == "":
                    listed.update(b.docs)
                elif rel in b.docs:
                    listed.add(rel)
                else:
                    prefix = rel.rstrip("/") + "/"
                    listed.update(r for r in b.docs if r.startswith(prefix))
            in_scope = set()
            if gate.startswith("research:"):
                tid = gate.split(":", 1)[1]
                for rel in listed:
                    doc = b.get(rel)
                    if doc is not None and doc.type == "Finding" and \
                            doc.meta.get("topic") == tid:
                        in_scope.add(rel)
                        for e in as_list(doc.meta.get("sources")):
                            src = b.source_doc(doc, e)
                            if src is not None:
                                in_scope.add(src.rel)
            elif gate == "synthesis":
                in_scope = {r for r in listed if b.get(r) is not None and
                            b.get(r).type == "Recommendation"}
            review_at = gen_at(review)
            for rel in in_scope:
                doc_at = gen_at(b.get(rel))
                if review_at is not None and doc_at is not None and \
                        review_at >= doc_at:
                    covered.add(rel)
        return covered


# --------------------------------------------------------------------------
# Stats
# --------------------------------------------------------------------------

def review_records(bundle):
    """(gate, perspective, cycle, round, verdict, rel) for each Review."""
    out = []
    for doc in bundle.concepts("Review"):
        gate, rnd = doc.meta.get("gate"), doc.meta.get("round")
        cyc = doc.meta.get("cycle", 1)
        if not isinstance(gate, str) or not isinstance(rnd, int) or \
                isinstance(rnd, bool):
            continue
        if not isinstance(cyc, int) or isinstance(cyc, bool):
            cyc = 1
        persp = doc.meta.get("perspective") \
            if gate in REQUIRED_PERSPECTIVES else None
        out.append((gate, str(persp or ""), cyc, rnd,
                    doc.meta.get("verdict"), doc.rel))
    return out


def gate_rounds(bundle):
    """{gate: {perspective: [(round, verdict, rel), ...]}} for the latest
    cycle of each gate and perspective, sorted by round.

    perspective is "" for gates with a single reviewer.
    """
    latest = {}
    records = review_records(bundle)
    for gate, persp, cyc, _r, _v, _rel in records:
        latest[(gate, persp)] = max(cyc, latest.get((gate, persp), 0))
    out = OrderedDict()
    for gate, persp, cyc, rnd, verdict, rel in sorted(
            records, key=lambda r: (r[0], r[1], r[2], r[3], r[5])):
        if cyc == latest[(gate, persp)]:
            out.setdefault(gate, OrderedDict()).setdefault(persp, []).append(
                (rnd, verdict, rel))
    return out


AGE_BUCKETS = ("<1y", "1-3y", "3-5y", ">5y", "unknown")


def source_age_bucket(meta, today):
    """Age of a source from its latest known date (period end)."""
    ends = []
    for key in ("updated", "published"):
        val = meta.get(key)
        if isinstance(val, int) and not isinstance(val, bool):
            val = str(val)
        end = partial_date_end(val) if val not in (None, "unknown") else None
        if end:
            ends.append(end)
    if not ends:
        return "unknown", None
    months = (today.year - max(ends).year) * 12 + \
        (today.month - max(ends).month)
    months = max(months, 0)
    if months < 12:
        return "<1y", months
    if months < 36:
        return "1-3y", months
    if months < 60:
        return "3-5y", months
    return ">5y", months


def summarize_sources(srcs, today, max_age=None):
    def label(value):
        if isinstance(value, bool):
            return "true" if value else "false"
        return "missing" if value is None else str(value)

    def count(key):
        return dict(Counter(label(s.meta.get(key))
                            for s in srcs).most_common())

    ages = Counter()
    too_old = []
    for s in srcs:
        bucket, months = source_age_bucket(s.meta, today)
        ages[bucket] += 1
        if is_int(max_age) and months is not None and months > max_age:
            too_old.append(s.rel)
    dated = sum(1 for s in srcs if s.meta.get("published") not in
                (None, "unknown") or s.meta.get("updated") not in
                (None, "unknown"))
    return OrderedDict([
        ("sources", len(srcs)),
        ("source_type", count("source_type")),
        ("evidence_tier", count("evidence_tier")),
        ("publisher_type", count("publisher_type")),
        ("country", count("country")),
        ("language", count("language")),
        ("commercial_interest", count("commercial_interest")),
        ("dated", dated),
        ("age", OrderedDict((b, ages.get(b, 0)) for b in AGE_BUCKETS)),
        ("older_than_max_age", sorted(too_old)),
    ])


def compute_stats(bundle, today):
    sources = [d for d in bundle.concepts("Source")]
    findings = [d for d in bundle.concepts("Finding")
                if d.meta.get("status") != "deprecated"]
    stats = OrderedDict()
    stats["counts"] = dict(Counter(d.type for d in bundle.concepts()))
    stats["overall"] = summarize_sources(sources, today)
    topics = OrderedDict()
    for tid, topic in bundle.plan_topics().items():
        tf = [f for f in findings if f.meta.get("topic") == tid]
        srcs = OrderedDict()
        for f in tf:
            for e in as_list(f.meta.get("sources")):
                s = bundle.source_doc(f, e)
                if s is not None:
                    srcs[s.rel] = s
        questions = [q.get("id") for q in as_list(topic.get("questions"))
                     if isinstance(q, dict) and isinstance(q.get("id"), str)]
        answered = {q for f in tf for q in str_items(f.meta.get("question"))}
        entry = OrderedDict([
            ("title", topic.get("title")),
            ("priority", topic.get("priority")),
            ("max_age_months", topic.get("max_age_months")),
            ("findings", len(tf)),
            ("questions", len(questions)),
            ("answered", sorted(q for q in questions if q in answered)),
            ("unanswered", [q for q in questions if q not in answered]),
            ("draft_findings", sum(1 for f in tf
                                   if f.meta.get("status") == "draft")),
        ])
        entry.update(summarize_sources(list(srcs.values()), today,
                                       topic.get("max_age_months")))
        topics[tid] = entry
    stats["topics"] = topics
    gates = OrderedDict()
    for gate, perspectives in sorted(gate_rounds(bundle).items(),
                                     key=lambda kv: gate_sort(kv[0])):
        for persp, rounds in sorted(perspectives.items()):
            label = "%s (%s)" % (gate, persp) if persp else gate
            gates[label] = {"rounds": max(r for r, _, _ in rounds),
                            "last_verdict": rounds[-1][1]}
    stats["gates"] = gates
    return stats


def fmt_counts(counts):
    return ", ".join("%s %d" % (k, v) for k, v in counts.items()) or "-"


def render_stats(stats):
    out = ["## Source mix (all sources)", ""]
    ov = stats["overall"]
    out += ["| Measure | Value |", "|---|---|"]
    out.append("| sources | %d |" % ov["sources"])
    out.append("| undated | %d |" % (ov["sources"] - ov["dated"]))
    for key in ("source_type", "evidence_tier", "publisher_type", "country",
                "language", "commercial_interest", "age"):
        out.append("| %s | %s |" % (key, fmt_counts(ov[key])))
    out += ["", "## Coverage per topic", "",
            "| Topic | Priority | Questions answered | Findings (draft) | "
            "Sources | Undated | Primary | Commercial | Older than max age |",
            "|---|---|---|---|---|---|---|---|---|"]
    for tid, t in stats["topics"].items():
        out.append("| %s %s | %s | %d of %d%s | %d (%d) | %d | %d | %d | %d |"
                   " %s |" % (
                       tid, t["title"], t["priority"], len(t["answered"]),
                       t["questions"],
                       (" (open: %s)" % ", ".join(t["unanswered"]))
                       if t["unanswered"] else "",
                       t["findings"], t["draft_findings"], t["sources"],
                       t["sources"] - t["dated"],
                       t["evidence_tier"].get("primary", 0),
                       t["commercial_interest"].get("true", 0),
                       len(t["older_than_max_age"])
                       if is_int(t["max_age_months"]) else "no limit"))
    out += ["", "## Mix per topic", "",
            "| Topic | Source types | Countries | Languages | Age |",
            "|---|---|---|---|---|"]
    for tid, t in stats["topics"].items():
        out.append("| %s | %s | %s | %s | %s |" % (
            tid, fmt_counts(t["source_type"]), fmt_counts(t["country"]),
            fmt_counts(t["language"]), fmt_counts(t["age"])))
    out += ["", "## Review gates", "", "| Gate | Rounds | Last verdict |",
            "|---|---|---|"]
    for g, v in stats["gates"].items():
        out.append("| %s | %d | %s |" % (g, v["rounds"], v["last_verdict"]))
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------
# Index generation
# --------------------------------------------------------------------------

def natural_key(text):
    return [int(p) if p.isdigit() else p.lower()
            for p in re.split(r"(\d+)", text)]


def gate_sort(gate):
    if not isinstance(gate, str):
        return (5, [])
    if gate.startswith("research:"):
        return (1, natural_key(gate))
    return ({"plan": 0, "synthesis": 2, "final": 3}.get(gate, 4), [])


def entry_line(title, link, description, status=None):
    title = str(title if title not in (None, "") else link)
    title = title.replace("[", "(").replace("]", ")")
    desc = " ".join(str(description or "").split())
    suffix = " (%s)" % status if status in ("draft", "deprecated") else ""
    return "* [%s](%s) - %s%s" % (title, link, desc, suffix)


def build_indexes(bundle):
    """Return {rel path of index.md: content} for every directory."""
    by_dir = OrderedDict()
    for doc in bundle.concepts():
        parent = str(PurePosixPath(doc.rel).parent)
        by_dir.setdefault("" if parent == "." else parent, []).append(doc)
    child_dirs = {}
    for d in bundle.dirs:
        if d:
            parent = str(PurePosixPath(d).parent)
            child_dirs.setdefault("" if parent == "." else parent,
                                  []).append(d)

    def has_content(d):
        return bool(by_dir.get(d)) or any(has_content(c)
                                          for c in child_dirs.get(d, []))

    topics = bundle.plan_topics()
    result = OrderedDict()
    for d in sorted(bundle.dirs, key=natural_key):
        if not has_content(d):
            continue
        docs = by_dir.get(d, [])
        subdirs = sorted((c for c in child_dirs.get(d, []) if has_content(c)),
                         key=natural_key)
        lines = []
        if d == "":
            lines += ["---", 'okf_version: "%s"' % OKF_VERSION, "---", ""]
            brief = bundle.get("brief.md")
            heading = "Start here"
            lines += ["# %s" % heading, ""]
            ordered = sorted(docs, key=lambda x: (
                ROOT_ORDER.index(x.name) if x.name in ROOT_ORDER else 9,
                natural_key(x.name)))
            for doc in ordered:
                lines.append(entry_line(doc.meta.get("title"), doc.name,
                                        doc.meta.get("description"),
                                        doc.meta.get("status")))
            if not ordered and brief is None:
                lines.pop()
                lines.pop()
            sub_heading = "Sections"
        else:
            name = PurePosixPath(d).name
            title = SECTION_TITLES.get(d)
            if title is None and d.startswith("findings/"):
                tid = name.split("-", 1)[0].upper()
                topic = topics.get(tid)
                title = "%s %s" % (tid, topic.get("title")) if topic \
                    else name
            lines += ["# %s" % (title or name), ""]
            if d == "reviews":
                docs = sorted(docs, key=lambda x: (
                    gate_sort(x.meta.get("gate")),
                    x.meta.get("round") if isinstance(
                        x.meta.get("round"), int) else 0))
            elif d.startswith("findings/"):
                docs = sorted(docs, key=lambda x: (
                    natural_key(str(as_list(x.meta.get("question"))[:1])),
                    natural_key(x.name)))
            else:
                docs = sorted(docs, key=lambda x: natural_key(x.name))
            for doc in docs:
                lines.append(entry_line(doc.meta.get("title"), doc.name,
                                        doc.meta.get("description"),
                                        doc.meta.get("status")))
            if not docs:
                lines.append("No documents directly in this directory.")
            sub_heading = "Subdirectories"
        if subdirs or d == "":
            if lines[-1] != "":
                lines.append("")
            lines += ["# %s" % sub_heading, ""]
            for c in subdirs:
                cname = PurePosixPath(c).name
                if c in SECTION_DESCRIPTIONS:
                    lines.append(entry_line(SECTION_TITLES[c],
                                            cname + "/index.md",
                                            SECTION_DESCRIPTIONS[c]))
                elif c.startswith("findings/"):
                    tid = cname.split("-", 1)[0].upper()
                    topic = topics.get(tid) or {}
                    desc = "Findings for %s (%s)." % (
                        tid, "must-have" if topic.get("priority") == "must"
                        else "nice-to-have" if topic.get("priority") ==
                        "nice" else "topic not in plan")
                    lines.append(entry_line(
                        "%s %s" % (tid, topic.get("title", cname)),
                        cname + "/index.md", desc))
                else:
                    lines.append(entry_line(cname, cname + "/index.md",
                                            "Subdirectory."))
            if d == "" and "log.md" in bundle.docs:
                lines.append(entry_line("Log", "log.md", "Chronological "
                                        "history of this knowledge base."))
        rel = (d + "/" if d else "") + "index.md"
        result[rel] = "\n".join(lines).rstrip("\n") + "\n"
    return result


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def file_hashes(root: Path):
    """[(relative path, SHA-256 of content)] for all regular files below
    root, sorted, skipping hidden entries and symbolic links."""
    out = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for fn in sorted(filenames):
            full = os.path.join(dirpath, fn)
            if fn.startswith("."):
                continue
            st = os.lstat(full)
            if not stat.S_ISREG(st.st_mode):
                continue
            digest = hashlib.sha256()
            with open(full, "rb") as fh:
                for chunk in iter(lambda: fh.read(65536), b""):
                    digest.update(chunk)
            out.append((PurePosixPath(Path(os.path.relpath(full, root)))
                        .as_posix(), digest.hexdigest()))
    return sorted(out)


def fingerprint(root: Path) -> str:
    """One SHA-256 over all paths and content hashes below root."""
    digest = hashlib.sha256()
    for rel, h in file_hashes(root):
        digest.update(("%s\0%s\n" % (rel, h)).encode("utf-8"))
    return digest.hexdigest()


def load_bundle(path):
    root = Path(path)
    if not root.is_dir():
        raise OSError("not a directory: %s" % path)
    bundle = Bundle(root)
    bundle.load()
    return bundle


def parse_today(value):
    if value is None:
        return dt.datetime.now(dt.timezone.utc).date()
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError("--today must be YYYY-MM-DD")


# --------------------------------------------------------------------------
# Workspace commands. The skill does everything it does to files through
# these, so that one pre-approved command covers a whole run. They are the
# security boundary for writes made by agents that process untrusted web
# content, so every path is checked strictly.
# --------------------------------------------------------------------------

MARKER = ".evidence-kb"
STATE_DIR = ".evidence-kb-state"          # snapshots and the lock
GUIDES = {"format": "bundle-format.md", "prompts": "prompts.md"}
LOG_KINDS_RE = re.compile(r"^[A-Z][A-Za-z-]{1,30}\Z")
DIR_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*\Z")
# Names that agents or tools load as instructions or components.
INSTRUCTION_FILES = frozenset(["claude.md", "agents.md", "gemini.md",
                               "skill.md", "agent.md", "copilot.md"])
COMPONENT_DIRS = frozenset(["commands", "agents", "skills", "hooks",
                            "output-styles", "rules", "plugins", "prompts",
                            "instructions"])
ROOT_FILES = frozenset(["brief.md", "plan.md", "audit.md"])
# C0/C1 controls except tab and newline, plus Unicode line separators that
# YAML 1.1 readers treat as line breaks.
FORBIDDEN_FM_CHARS = re.compile(
    "[\x00-\x08\x0b-\x1f\x7f-\x9f\u2028\u2029\ufeff]")
SECTION_DIRS = frozenset(["recommendations", "references", "reviews"])
WINDOWS_RESERVED = frozenset(
    ["con", "prn", "aux", "nul"] + ["com%d" % i for i in range(1, 10)] +
    ["lpt%d" % i for i in range(1, 10)])
LOCK_TIMEOUT = 30.0
LOCK_STALE = 120.0


class UsageError(Exception):
    pass


def utc_now():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def workspace_root(start=None):
    """The git work tree that contains `start` (default: the working
    directory). Refuses to work outside one, and in a repository that is
    the home directory or the filesystem root."""
    here = Path(start or os.getcwd()).resolve()
    for cand in [here] + list(here.parents):
        if (cand / ".git").exists():
            if cand == Path.home().resolve() or cand == Path(cand.anchor):
                raise UsageError("the git repository is %s; run the skill "
                                 "inside a project repository" % cand)
            return cand
    raise UsageError("%s is not inside a git repository; run the skill "
                     "inside a project (git init creates one)" % here)


def _no_symlinks_below(root, path):
    """Raise UsageError if any existing component of path below root is a
    symbolic link."""
    rel = Path(os.path.relpath(path, root))
    cur = Path(root)
    for part in rel.parts:
        cur = cur / part
        if os.path.islink(cur):
            raise UsageError("refusing to use %s: it is a symbolic link" % cur)
        if not cur.exists():
            break


def _reserved(name):
    return name.split(".", 1)[0].lower() in WINDOWS_RESERVED


def _below_root(path, root):
    """Split `path` into the part that leads to `root` (possibly through
    symbolic links above the root, such as /tmp -> /private/tmp) and the
    relative part below it. Returns the relative path, or None if `path`
    is not inside `root`. Symbolic links below the root are refused."""
    for anchor in [path] + list(path.parents):
        if anchor.exists() and Path(os.path.realpath(anchor)) == root:
            rel = path.relative_to(anchor)
            _no_symlinks_below(anchor, path)
            return rel
    return None


def _normalized(arg):
    raw = Path(arg)
    return Path(os.path.normpath(raw if raw.is_absolute()
                                 else Path(os.getcwd()) / raw))


def resolve_bundle(arg):
    """(absolute bundle path, project root). The bundle must be a
    subdirectory of the project whose path components are plain names:
    no hidden directories (so never .git or .claude), no component
    directories, no symlinks."""
    root = workspace_root()
    path = _normalized(arg)
    rel = _below_root(path, root)
    if rel is None:
        raise UsageError("bundle %s is outside the project (%s)"
                         % (path, root))
    path = root / rel
    if not rel.parts:
        raise UsageError("the bundle must be a subdirectory of the project, "
                         "not the project root")
    bad = [p for p in rel.parts if not DIR_RE.match(p) or _reserved(p) or
           p.lower() in COMPONENT_DIRS or p.lower() == "node_modules"]
    if bad:
        raise UsageError("bundle path components must be plain names (no "
                         "hidden directories, and none of %s, node_modules)"
                         ": %s; for a topic slug, add a suffix such as "
                         "'%s-kb'" % (", ".join(sorted(COMPONENT_DIRS)),
                                      ", ".join(repr(b) for b in bad),
                                      bad[-1].lower()))
    return path, root


def resolve_readable(arg):
    """Bundle path for read-only commands: inside the current project (or
    inside the git work tree that contains the path itself), without
    symbolic links below the project root. Any directory names are fine."""
    path = _normalized(arg)
    roots = []
    for start in (os.getcwd(), path if path.exists() else path.parent):
        try:
            roots.append(workspace_root(start))
        except UsageError:
            pass
    for root in roots:
        rel = _below_root(path, root)
        if rel is not None:
            return root / rel
    raise UsageError("%s is not inside a git repository (run the command "
                     "inside the project that contains the bundle)" % path)


def bundle_state(path):
    """missing | empty | kb | foreign"""
    if not path.exists():
        return "missing"
    if not path.is_dir():
        return "foreign"
    if (path / MARKER).is_file():
        return "kb"
    brief = path / "brief.md"
    if brief.is_file() and not brief.is_symlink():
        try:
            fm, _body, _line = split_frontmatter(
                brief.read_text(encoding="utf-8"))
            meta = parse_yaml(fm)[0] if fm is not None else {}
            if meta.get("kb_profile") == PROFILE:
                return "kb"
        except (OSError, ValueError, YamlError):
            pass
    entries = [e for e in os.listdir(path) if e != ".DS_Store"]
    return "empty" if not entries else "foreign"


def free_name(path):
    n = 2
    while True:
        cand = path.with_name("%s-%d" % (path.name, n))
        if bundle_state(cand) in ("missing", "empty"):
            return cand
        n += 1


def require_kb(path):
    state = bundle_state(path)
    if state != "kb":
        raise UsageError("%s is not an evidence-kb bundle (state: %s); run "
                         "`kb.py init` first" % (path, state))


def bundle_file(bundle, rel):
    """Validate a bundle-relative .md path; return (absolute, normalized)."""
    rel = rel.strip()
    parts = PurePosixPath(rel.lstrip("/")).parts
    if not parts or any(p in ("", ".", "..") for p in parts):
        raise UsageError("invalid path %r" % rel)
    if not parts[-1].endswith(".md"):
        raise UsageError("only .md files can be written: %r" % rel)
    bad = [p for p in parts if not NAME_RE.match(p) or _reserved(p)]
    if bad:
        raise UsageError("file and directory names must be lowercase "
                         "kebab-case ASCII: %s"
                         % ", ".join(repr(b) for b in bad))
    if parts[-1] == "index.md":
        raise UsageError("index.md files are generated; run `kb.py index`")
    if parts[-1] == "log.md":
        raise UsageError("log.md is changed only with `kb.py log`")
    if any(p.lower() in INSTRUCTION_FILES for p in parts):
        raise UsageError("%r is a name that agents load as instructions"
                         % rel)
    layout_ok = (
        (len(parts) == 1 and parts[0] in ROOT_FILES) or
        (len(parts) == 2 and parts[0] in SECTION_DIRS) or
        (len(parts) == 3 and parts[0] == "findings" and
         re.match(r"^t\d+(-|\Z)", parts[1])))
    if not layout_ok:
        raise UsageError("%r is not a place in the bundle layout: brief.md, "
                         "plan.md, audit.md, findings/t<n>-<slug>/<name>.md, "
                         "recommendations/, references/, or reviews/<name>.md"
                         % rel)
    target = bundle.joinpath(*parts)
    _no_symlinks_below(bundle, target)
    return target, "/".join(parts)


def read_stdin_text():
    stream = getattr(sys.stdin, "buffer", None)
    if stream is None:
        raise UsageError("no content on standard input; pass it as a "
                         "quoted heredoc")
    data = stream.read(MAX_FILE_BYTES + 2)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise UsageError("content is not valid UTF-8")
    # Normalize every line ending, so that the checks below see the same
    # lines as every later reader (which treat a lone CR as a line break).
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if not text.endswith("\n"):
        text += "\n"
    if len(text.encode("utf-8")) > MAX_FILE_BYTES:
        raise UsageError("content is larger than %d MB"
                         % (MAX_FILE_BYTES // (1024 * 1024)))
    return text


def _parse_meta(text):
    """Frontmatter mapping of text, or raise YamlError / UsageError."""
    fm, _body, _line = split_frontmatter(text)
    if fm is None:
        raise UsageError("no frontmatter")
    return parse_yaml_full(fm, first_line=2)[0]


def _human_events(meta):
    out = set()
    if not isinstance(meta, dict):
        return out
    events = [meta.get("generated")] + as_list(meta.get("verified"))
    for ev in events:
        if isinstance(ev, dict) and isinstance(ev.get("by"), str) and \
                ev["by"].startswith("human:"):
            out.add((ev["by"], str(ev.get("at"))))
    return out


def validate_document(rel, text, old_text=None):
    """Problems that make the content unacceptable. Returns messages."""
    try:
        fm, _body, _line = split_frontmatter(text)
        if fm is None:
            return ["%s:1: concept documents need YAML frontmatter" % rel]
        bad = FORBIDDEN_FM_CHARS.search(fm)
        if bad:
            return ["%s:%d: frontmatter contains the control or line-"
                    "separator character U+%04X, which YAML readers treat "
                    "differently; remove it" % (rel, fm[:bad.start()].count(
                        "\n") + 2, ord(bad.group(0)))]
        meta = parse_yaml_full(fm, first_line=2)[0]
    except YamlError as exc:
        return ["%s:%d: frontmatter: %s" % (rel, exc.line, exc.message)]
    if not is_str(meta.get("type")):
        return ["%s:1: frontmatter has no non-empty 'type'" % rel]
    old_humans = set()
    if old_text is not None:
        try:
            old_humans = _human_events(_parse_meta(old_text))
        except (UsageError, YamlError):
            pass
    added = _human_events(meta) - old_humans
    if added:
        return ["%s:1: human: events can only be added by the person "
                "(by hand): %s" % (rel, ", ".join(b for b, _a in
                                                  sorted(added)))]
    if old_text is not None:
        try:
            old_events = [json.dumps(e, sort_keys=True) for e in
                          as_list(_parse_meta(old_text).get("verified"))]
        except (UsageError, YamlError):
            old_events = []
        new_events = [json.dumps(e, sort_keys=True)
                      for e in as_list(meta.get("verified"))]
        lost = [e for e in old_events if e not in new_events]
        if lost:
            return ["%s:1: the new version drops %d verified event(s); keep "
                    "every verified event" % (rel, len(lost))]
    return []


try:
    import fcntl
except ImportError:            # Windows
    fcntl = None


class BundleLock:
    """Serializes all writes to one bundle.

    On POSIX it holds an fcntl.flock lock on a lock file; the operating
    system releases it when the process ends, so a crashed writer never
    leaves a stale lock. Elsewhere it falls back to an exclusive-create
    lock file that is considered stale after LOCK_STALE seconds."""

    def __init__(self, bundle):
        self.dir = bundle / STATE_DIR
        self.path = self.dir / "lock"
        self.fd = None

    def __enter__(self):
        if os.path.islink(self.dir):
            raise UsageError("%s is a symbolic link" % self.dir)
        self.dir.mkdir(exist_ok=True)
        if os.path.islink(self.path):
            raise UsageError("%s is a symbolic link" % self.path)
        deadline = time.time() + LOCK_TIMEOUT
        if fcntl is not None:
            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
            self.fd = os.open(str(self.path), flags, 0o644)
            while True:
                try:
                    fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return self
                except OSError:
                    if time.time() > deadline:
                        os.close(self.fd)
                        self.fd = None
                        raise UsageError("the bundle is busy (lock %s); try "
                                         "again" % self.path)
                    time.sleep(0.05)
        while True:
            try:
                fd = os.open(str(self.path),
                             os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
                os.close(fd)
                return self
            except FileExistsError:
                try:
                    if time.time() - os.lstat(self.path).st_mtime > \
                            LOCK_STALE:
                        os.unlink(self.path)
                        continue
                except FileNotFoundError:
                    continue
                if time.time() > deadline:
                    raise UsageError("the bundle is busy (lock %s); try "
                                     "again" % self.path)
                time.sleep(0.05)

    def __exit__(self, *exc):
        if self.fd is not None:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            finally:
                os.close(self.fd)
                self.fd = None
        else:
            try:
                os.unlink(self.path)
            except FileNotFoundError:
                pass
        return False


def _fsync_dir(directory):
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _write_tmp(parent, name, content):
    tmp = parent / (".%s.%d.%d.tmp" % (name, os.getpid(),
                                       int(time.time() * 1e6)))
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(content)
        fh.flush()
        os.fsync(fh.fileno())
    return tmp


def safe_write(root, rel, content):
    """Replace root/rel atomically and durably; never through a symlink;
    keeps the file's permission bits."""
    target = Path(root) / rel
    try:
        st = os.lstat(target)
    except FileNotFoundError:
        st = None
    if st is not None and not stat.S_ISREG(st.st_mode):
        raise OSError("refusing to write: not a regular file (symbolic "
                      "link?)")
    parent = target.parent
    if os.path.islink(parent):
        raise OSError("refusing to write: directory is a symbolic link")
    tmp = _write_tmp(parent, target.name, content)
    try:
        if st is not None:
            os.chmod(tmp, stat.S_IMODE(st.st_mode))
        os.replace(tmp, target)
        _fsync_dir(parent)
    finally:
        if tmp.exists():
            tmp.unlink()


def write_new(target, content):
    """Create target with its full content at once; raise FileExistsError
    if it exists (also if it is a symlink)."""
    tmp = _write_tmp(target.parent, target.name, content)
    try:
        try:
            os.link(str(tmp), str(target))     # atomic, never overwrites
        except FileExistsError:
            raise
        except (OSError, AttributeError, NotImplementedError):
            fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                         0o644)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(content)
                fh.flush()
                os.fsync(fh.fileno())
        _fsync_dir(target.parent)
    finally:
        if tmp.exists():
            tmp.unlink()


def _mkdirs(bundle, directory):
    _no_symlinks_below(bundle, directory)
    directory.mkdir(parents=True, exist_ok=True)
    _no_symlinks_below(bundle, directory)


def cmd_init(bundle_arg):
    path, root = resolve_bundle(bundle_arg)
    state = bundle_state(path)
    if state == "foreign":
        raise UsageError("%s exists and is not an evidence-kb bundle; it was "
                         "left untouched. Free name: %s"
                         % (path, free_name(path)))
    path.mkdir(parents=True, exist_ok=True)
    _no_symlinks_below(root, path)
    marker = path / MARKER
    if not marker.exists():
        write_new(marker, PROFILE + "\n")
    ignore = path / ".gitignore"
    line = STATE_DIR + "/"
    if not ignore.exists():
        write_new(ignore, line + "\n")
    elif not ignore.is_symlink() and line not in \
            ignore.read_text(encoding="utf-8").split():
        safe_write(path, ".gitignore", ignore.read_text(
            encoding="utf-8").rstrip("\n") + "\n" + line + "\n")
    print("%s %s" % ("existing" if state == "kb" else "created", path))
    return 0


def cmd_write(bundle_arg, rel, new):
    bundle, _root = resolve_bundle(bundle_arg)
    require_kb(bundle)
    target, rel = bundle_file(bundle, rel)
    content = read_stdin_text()
    with BundleLock(bundle):
        old = None
        if target.is_file() and not target.is_symlink():
            old = target.read_text(encoding="utf-8")
        problems = validate_document(rel, content, old)
        if problems:
            for p in problems:
                print("error: %s" % p, file=sys.stderr)
            print("not written: fix the content and write it again",
                  file=sys.stderr)
            return 1
        _mkdirs(bundle, target.parent)
        if new:
            try:
                write_new(target, content)
            except FileExistsError:
                print("error: %s already exists; read it, then extend it "
                      "with `append` or replace it with `write` (without "
                      "--new), or choose another name" % rel,
                      file=sys.stderr)
                return 1
        else:
            safe_write(bundle, rel, content)
    print("wrote %s" % rel)
    return 0


def _section_end(lines, start, title):
    """Index after the last line of level-1 section `title` (body lines
    from `start`), or None if there is no such section."""
    mask = code_mask(lines)
    found, end = None, None
    for i in range(start, len(lines)):
        if mask[i]:
            continue
        h = match_heading(lines[i])
        if h and h[0] == 1:
            if found is not None:
                end = i
                break
            if h[1].lower() == title.lower():
                found = i
    if found is None:
        return None
    end = len(lines) if end is None else end
    while end > found + 1 and lines[end - 1].strip() == "":
        end -= 1
    return end


def cmd_append(bundle_arg, rel, section):
    bundle, _root = resolve_bundle(bundle_arg)
    require_kb(bundle)
    target, rel = bundle_file(bundle, rel)
    chunk = read_stdin_text().strip("\n")
    if not chunk.strip():
        raise UsageError("nothing to append")
    if not section.strip() or "\n" in section:
        raise UsageError("invalid --section")
    with BundleLock(bundle):
        if not target.is_file() or target.is_symlink():
            raise UsageError("%s does not exist; create it with `write "
                             "--new`" % rel)
        text = target.read_text(encoding="utf-8")
        lines = text.rstrip("\n").split("\n")
        _fm, _body, body_line = split_frontmatter(text)
        end = _section_end(lines, body_line - 1, section)
        if end is None:
            lines += ["", "# " + section.strip(), "", chunk]
        else:
            lines[end:end] = ["", chunk]
        new = "\n".join(lines) + "\n"
        problems = validate_document(rel, new, text)
        if problems:
            for p in problems:
                print("error: %s" % p, file=sys.stderr)
            return 1
        safe_write(bundle, rel, new)
    print("appended to %s (# %s)" % (rel, section.strip()))
    return 0


def _fm_bounds(lines):
    """(start, end) indexes of the frontmatter lines (exclusive markers)."""
    if not lines or lines[0].rstrip() != "---":
        raise UsageError("no frontmatter")
    for i in range(1, len(lines)):
        if lines[i].rstrip() == "---":
            return 1, i
    raise UsageError("frontmatter is never closed")


def _key_range(lines, start, end, key):
    """(index of 'key:' line, index after its block) or None."""
    for i in range(start, end):
        if re.match(r"^%s\s*:" % re.escape(key), lines[i]):
            j = i + 1
            while j < end and (lines[j].strip() == "" or
                               lines[j][:1] in (" ", "\t") or
                               lines[j].startswith("- ") or
                               lines[j].rstrip() == "-"):
                j += 1
            while j > i + 1 and (lines[j - 1].strip() == "" or
                                 lines[j - 1].lstrip().startswith("#")):
                j -= 1
            return i, j
    return None


def _comment_of(line):
    m = re.search(r"\s+#(\s.*)?$", line)
    return m.group(0) if m else ""


def mark_text(text, status=None, verified_by=None, touch=False, now=None):
    """Return text with status / generated.at / a new verified event.

    Refuses invalid frontmatter, and checks afterwards that nothing else
    in the frontmatter changed."""
    now = now or utc_now()
    bom = text.startswith("﻿")
    if bom:
        text = text[1:]
    before = _parse_meta(text)
    lines = text.split("\n")
    start, end = _fm_bounds(lines)
    if touch:
        rng = _key_range(lines, start, end, "generated")
        if rng is None:
            raise UsageError("no 'generated' to touch")
        i, j = rng
        if re.search(r"\bat\s*:", lines[i]):
            lines[i] = re.sub(r"(\bat\s*:\s*)[^,}\s]+", r"\g<1>" + now,
                              lines[i], count=1)
        else:
            for k in range(i + 1, j):
                if re.match(r"^\s+at\s*:", lines[k]):
                    lines[k] = re.sub(r"(:\s*)[^\s#]+", r"\g<1>" + now,
                                      lines[k], count=1)
                    break
            else:
                raise UsageError("could not find generated.at")
    if verified_by is not None:
        event = "{ by: %s, at: %s }" % (verified_by, now)
        rng = _key_range(lines, start, end, "verified")
        if rng is None:
            lines[end:end] = ["verified:", "  - " + event]
            end += 2
        else:
            i, j = rng
            rest = lines[i].split(":", 1)[1]
            comment = _comment_of(rest)
            rest = rest[:len(rest) - len(comment)].strip()
            if rest in ("", "~", "null") and j == i + 1:
                lines[i:j] = ["verified:" + comment, "  - " + event]
                end += 1
            elif rest.startswith("{") and j == i + 1:
                lines[i:j] = ["verified:" + comment, "  - " + rest,
                              "  - " + event]
                end += 2
            elif rest.startswith("[") and rest.endswith("]") and j == i + 1:
                inner = rest[1:-1].strip()
                lines[i] = "verified: [%s]%s" % (
                    (inner + ", " + event) if inner else event, comment)
            elif rest == "" and j > i + 1:
                indent = re.match(r"^(\s*)", lines[i + 1]).group(1)
                lines[j:j] = [indent + "- " + event]
                end += 1
            else:
                raise UsageError("unsupported form of 'verified'; write the "
                                 "file with the event added")
    if status is not None:
        rng = _key_range(lines, start, end, "status")
        if rng is None:
            lines[end:end] = ["status: " + status]
        else:
            i, j = rng
            comment = _comment_of(lines[i].split(":", 1)[1])
            lines[i:j] = ["status: " + status + comment]
    out = "\n".join(lines)
    after = _parse_meta(out)
    changed = {"status"} if status is not None else set()
    if verified_by is not None:
        changed.add("verified")
    for key in set(before) | set(after):
        if key in changed:
            continue
        if key == "generated" and touch:
            b, a = before.get(key), after.get(key)
            if not (isinstance(a, dict) and isinstance(b, dict) and
                    {k: v for k, v in a.items() if k != "at"} ==
                    {k: v for k, v in b.items() if k != "at"}):
                raise UsageError("editing generated.at changed other values")
            continue
        if before.get(key) != after.get(key):
            raise UsageError("editing would change %r; nothing written" % key)
    if verified_by is not None:
        old_events = as_list(before.get("verified"))
        if as_list(after.get("verified"))[:-1] != old_events:
            raise UsageError("editing would change earlier verified events")
    return ("﻿" if bom else "") + out


def cmd_mark(bundle_arg, rels, status, verified_by, touch):
    bundle, _root = resolve_bundle(bundle_arg)
    require_kb(bundle)
    if verified_by is not None:
        if not ACTOR_RE.match(verified_by):
            raise UsageError("--verified-by %r is not an actor" % verified_by)
        if verified_by.startswith("human:"):
            raise UsageError("human: verifications are added by the person, "
                             "by hand")
    if status is None and verified_by is None and not touch:
        raise UsageError("nothing to do: give --status, --verified-by, or "
                         "--touch")
    targets = [bundle_file(bundle, rel) for rel in rels]
    for target, rel in targets:
        if not target.is_file() or target.is_symlink():
            raise UsageError("%s does not exist" % rel)
    now = utc_now()
    failed = 0
    with BundleLock(bundle):
        for target, rel in targets:
            try:
                text = target.read_text(encoding="utf-8")
                new = mark_text(text, status, verified_by, touch, now)
                meta = _parse_meta(new.lstrip("﻿"))
                if status is not None and meta.get("status") != status:
                    raise UsageError("status could not be set")
                if verified_by is not None:
                    last = as_list(meta.get("verified"))[-1]
                    if not isinstance(last, dict) or \
                            last.get("by") != verified_by:
                        raise UsageError("verified event could not be added")
                gen = meta.get("generated")
                if touch and not (isinstance(gen, dict) and
                                  gen.get("at") == now):
                    raise UsageError("generated.at could not be updated")
                safe_write(bundle, rel, new)
                print("marked %s" % rel)
            except (UsageError, YamlError, ValueError, IndexError) as exc:
                msg = exc.message if isinstance(exc, YamlError) else str(exc)
                print("error: %s: %s" % (rel, msg), file=sys.stderr)
                failed += 1
    return 1 if failed else 0


def cmd_log(bundle_arg, kind, message):
    bundle, _root = resolve_bundle(bundle_arg)
    require_kb(bundle)
    if not LOG_KINDS_RE.match(kind):
        raise UsageError("--kind must be one capitalized word, e.g. Review")
    if message in (None, "-"):
        message = read_stdin_text()
    message = " ".join(message.split())
    if not message:
        raise UsageError("empty log message")
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    entry = "* **%s**: %s" % (kind, message)
    target = bundle / "log.md"
    _no_symlinks_below(bundle, target)
    with BundleLock(bundle):
        try:
            text = target.read_text(encoding="utf-8") if target.exists() \
                else "# Log\n"
        except UnicodeDecodeError:
            raise UsageError("log.md is not valid UTF-8")
        lines = text.rstrip("\n").split("\n")
        mask = code_mask(lines)
        heads = [(i, lines[i].split()[1]) for i in range(len(lines))
                 if not mask[i] and
                 re.match(r"^##\s+\d{4}-\d{2}-\d{2}\s*\Z", lines[i])]
        same = [i for i, d in heads if d == today]
        if same:
            k = same[0] + 1
            while k < len(lines) and lines[k].strip() == "":
                k += 1
            lines[k:k] = [entry]
        else:
            older = [i for i, d in heads if d < today]
            if older:
                at = older[0]
                lines[at:at] = ["## " + today, "", entry, ""]
            else:
                lines += ["", "## " + today, "", entry]
        safe_write(bundle, "log.md", "\n".join(lines) + "\n")
    print("logged %s" % entry)
    return 0


def _snapshot_dir(bundle):
    return bundle / STATE_DIR / "snapshots"


def project_bundles(root, exclude=None):
    """{path relative to root: fingerprint} of every evidence-kb bundle in
    the project except `exclude`, skipping hidden directories, node_modules
    (where kb.py never creates bundles), and symbolic links."""
    out = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and
                       d != "node_modules" and
                       not os.path.islink(os.path.join(dirpath, d))]
        if MARKER in filenames and (exclude is None or
                                    Path(dirpath) != exclude):
            out.append(PurePosixPath(Path(os.path.relpath(
                dirpath, root))).as_posix())
    return dict((rel, fingerprint(Path(root) / rel)) for rel in sorted(out))


def cmd_snapshot(bundle_arg):
    bundle, root = resolve_bundle(bundle_arg)
    require_kb(bundle)
    snaps = _snapshot_dir(bundle)
    with BundleLock(bundle):
        files = file_hashes(bundle)
        manifest = json.dumps({"files": dict(files),
                               "bundles": project_bundles(root, bundle)},
                              sort_keys=True, indent=1).encode("utf-8")
        sid = hashlib.sha256(manifest).hexdigest()
        target = snaps / sid
        if not target.exists():
            tmp = snaps / (".tmp-%s-%d" % (sid, os.getpid()))
            if tmp.exists():
                shutil.rmtree(tmp)
            (tmp / "files").mkdir(parents=True)
            for rel, _h in files:
                dest = tmp / "files" / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(bundle / rel, dest, follow_symlinks=False)
            (tmp / "manifest.json").write_bytes(manifest)
            os.replace(tmp, target)
    print(sid)
    return 0


def _load_snapshot(bundle, sid):
    """{rel: sha} of snapshot sid, after verifying that neither the
    manifest nor the stored copies were changed."""
    if not re.match(r"^[0-9a-f]{64}\Z", sid):
        raise UsageError("snapshot id must be the 64-character id that "
                         "`snapshot` printed")
    base = _snapshot_dir(bundle) / sid
    manifest = base / "manifest.json"
    if not manifest.is_file() or os.path.islink(base):
        raise UsageError("snapshot %s does not exist (it was removed: treat "
                         "this as tampering)" % sid[:12])
    data = manifest.read_bytes()
    if hashlib.sha256(data).hexdigest() != sid:
        raise UsageError("snapshot %s was altered (treat this as tampering)"
                         % sid[:12])
    meta = json.loads(data.decode("utf-8"))
    files = meta["files"]
    stored = dict(file_hashes(base / "files"))
    if stored != files:
        raise UsageError("the copies in snapshot %s were altered (treat this "
                         "as tampering)" % sid[:12])
    bundles = meta.get("bundles", {})
    return files, base / "files", bundles if isinstance(bundles, dict) else {}


def _diff(old, new):
    return sorted((("added" if r not in old else "deleted" if r not in new
                    else "changed"), r)
                  for r in set(old) | set(new) if old.get(r) != new.get(r))


def cmd_compare(bundle_arg, sid):
    bundle, root = resolve_bundle(bundle_arg)
    require_kb(bundle)
    files, _copies, bundles = _load_snapshot(bundle, sid)
    changes = _diff(files, dict(file_hashes(bundle)))
    for state, rel in changes:
        print("%s %s" % (state, rel))
    others = _diff(bundles, project_bundles(root, bundle))
    for state, rel in others:
        print("%s bundle elsewhere in the project: %s" % (
            {"added": "new", "changed": "changed",
             "deleted": "removed"}[state], rel))
    if not changes and not others:
        print("unchanged")
    return 1 if changes or others else 0


def cmd_restore(bundle_arg, sid):
    bundle, _root = resolve_bundle(bundle_arg)
    require_kb(bundle)
    files, copies, _bundles = _load_snapshot(bundle, sid)
    with BundleLock(bundle):
        changes = _diff(files, dict(file_hashes(bundle)))
        for state, rel in changes:
            target = bundle / rel
            _no_symlinks_below(bundle, target)
            if state == "added":
                os.unlink(target)
            else:
                _mkdirs(bundle, target.parent)
                tmp = target.parent / (".%s.%d.restore" % (target.name,
                                                           os.getpid()))
                try:
                    shutil.copyfile(copies / rel, tmp, follow_symlinks=False)
                    os.replace(tmp, target)
                finally:
                    if tmp.exists():
                        tmp.unlink()
            print("restored %s (%s)" % (rel, state))
        remaining = _diff(files, dict(file_hashes(bundle)))
    if remaining:
        for state, rel in remaining:
            print("error: could not restore %s (%s)" % (rel, state),
                  file=sys.stderr)
        return 1
    if not changes:
        print("unchanged")
    return 0


def cmd_guide(topic):
    name = GUIDES.get(topic)
    if name is None:
        raise UsageError("guide topics: %s" % ", ".join(sorted(GUIDES)))
    path = Path(__file__).resolve().parent.parent / "references" / name
    sys.stdout.write(path.read_text(encoding="utf-8"))
    return 0


def cmd_preflight(bundle_arg):
    ok = True
    print("python: %s" % sys.version.split()[0])
    if sys.version_info < (3, 9):
        print("error: Python 3.9 or later is required", file=sys.stderr)
        ok = False
    print("now: %s" % utc_now())
    print("skill: %s" % Path(__file__).resolve().parent.parent)
    try:
        print("project: %s" % workspace_root())
        path, _root = resolve_bundle(bundle_arg)
        state = bundle_state(path)
        print("bundle: %s" % path)
        print("state: %s" % state)
        if state == "foreign":
            print("note: the directory exists and is not an evidence-kb "
                  "bundle; use this free name instead: %s" % free_name(path))
    except UsageError as exc:
        print("error: %s" % exc, file=sys.stderr)
        ok = False
    return 0 if ok else 1


def is_kb_bundle(bundle):
    """True if BUNDLE carries the marker file or a brief.md with this
    profile's kb_profile."""
    if (bundle.root / MARKER).is_file():
        return True
    brief = bundle.get("brief.md")
    return brief is not None and isinstance(brief.meta, dict) and \
        brief.meta.get("kb_profile") == PROFILE


def _main(argv=None):
    parser = argparse.ArgumentParser(
        prog="kb.py",
        description="The evidence-kb tool: validate, summarize, index, and "
                    "write a knowledge base (OKF v0.2 bundle).")
    sub = parser.add_subparsers(dest="command")
    p = sub.add_parser("check", help="validate the bundle")
    p.add_argument("bundle")
    p.add_argument("--final", action="store_true",
                   help="also apply the completion rules")
    p.add_argument("--strict", action="store_true",
                   help="treat warnings as errors")
    p.add_argument("--today", type=parse_today, default=None)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("stats", help="source mix and coverage")
    p.add_argument("bundle")
    p.add_argument("--today", type=parse_today, default=None)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("index", help="regenerate index.md files")
    p.add_argument("bundle")
    p.add_argument("--check", action="store_true",
                   help="write nothing; exit 1 if out of date")
    p = sub.add_parser("fingerprint", help="print a hash of all files")
    p.add_argument("bundle")
    p.add_argument("--files", action="store_true",
                   help="print one '<sha256>  <path>' line per file")
    p = sub.add_parser("snapshot", help="save the bundle state; print its id")
    p.add_argument("bundle")
    p = sub.add_parser("compare", help="list changes since a snapshot")
    p.add_argument("bundle")
    p.add_argument("id")
    p = sub.add_parser("restore", help="undo all changes since a snapshot")
    p.add_argument("bundle")
    p.add_argument("id")
    p = sub.add_parser("init", help="create (or adopt) a bundle directory")
    p.add_argument("bundle")
    p = sub.add_parser("write", help="write a bundle file from stdin")
    p.add_argument("bundle")
    p.add_argument("path", help="bundle-relative .md path")
    p.add_argument("--new", action="store_true",
                   help="fail if the file already exists")
    p = sub.add_parser("append", help="append stdin to a section of a file")
    p.add_argument("bundle")
    p.add_argument("path")
    p.add_argument("--section", required=True,
                   help="level-1 heading, e.g. Evidence or Responses")
    p = sub.add_parser("mark", help="set status, verified, generated.at")
    p.add_argument("bundle")
    p.add_argument("paths", nargs="+")
    p.add_argument("--status", choices=STATUS)
    p.add_argument("--verified-by", dest="verified_by")
    p.add_argument("--touch", action="store_true",
                   help="set generated.at to now")
    p = sub.add_parser("log", help="add an entry to log.md")
    p.add_argument("bundle")
    p.add_argument("--kind", required=True)
    p.add_argument("--message", default=None,
                   help="the entry; omit it to read the entry from stdin")
    p = sub.add_parser("guide", help="print a reference document")
    p.add_argument("topic", choices=sorted(GUIDES))
    sub.add_parser("now", help="print the current UTC timestamp")
    sub.add_parser("root", help="print the project root")
    p = sub.add_parser("preflight", help="check the environment")
    p.add_argument("bundle")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code else 0
    if not args.command:
        parser.print_usage(sys.stderr)
        return 2
    try:
        return _dispatch(args)
    except UsageError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2


def _dispatch(args):
    cmd = args.command
    simple = {
        "now": lambda: print(utc_now()) or 0,
        "root": lambda: print(workspace_root()) or 0,
        "guide": lambda: cmd_guide(args.topic),
    }
    if cmd in simple:
        return simple[cmd]()
    if cmd == "preflight":
        return cmd_preflight(args.bundle)
    if cmd == "init":
        return cmd_init(args.bundle)
    if cmd == "write":
        return cmd_write(args.bundle, args.path, args.new)
    if cmd == "append":
        return cmd_append(args.bundle, args.path, args.section)
    if cmd == "mark":
        return cmd_mark(args.bundle, args.paths, args.status,
                        args.verified_by, args.touch)
    if cmd == "log":
        return cmd_log(args.bundle, args.kind, args.message)
    if cmd == "snapshot":
        return cmd_snapshot(args.bundle)
    if cmd == "compare":
        return cmd_compare(args.bundle, args.id)
    if cmd == "restore":
        return cmd_restore(args.bundle, args.id)

    read_only = cmd in ("check", "stats", "fingerprint") or (
        cmd == "index" and args.check)
    path = resolve_readable(args.bundle) if read_only \
        else resolve_bundle(args.bundle)[0]
    if cmd == "fingerprint":
        if not path.is_dir():
            raise UsageError("not a directory: %s" % path)
        if args.files:
            for rel, h in file_hashes(path):
                print("%s  %s" % (h, rel))
        else:
            print(fingerprint(path))
        return 0
    bundle = load_bundle(path)
    today = getattr(args, "today", None) or parse_today(None)

    if cmd == "check":
        Checker(bundle, today, final=args.final).run()
        problems = sorted(bundle.problems,
                          key=lambda p: (p.path, p.line, p.level))
        errors = [p for p in problems if p.level == "error" or args.strict]
        if args.json:
            print(json.dumps({"ok": not errors,
                              "errors": sum(1 for p in problems
                                            if p.level == "error"),
                              "warnings": sum(1 for p in problems
                                              if p.level == "warning"),
                              "problems": [p.as_dict() for p in problems]},
                             indent=2))
        else:
            for p in problems:
                print(p, file=sys.stderr)
            print("%d error(s), %d warning(s) in %d file(s)" % (
                sum(1 for p in problems if p.level == "error"),
                sum(1 for p in problems if p.level == "warning"),
                len(bundle.docs)), file=sys.stderr)
        return 1 if errors else 0

    if cmd == "stats":
        parse_errors = [d for d in bundle.docs.values()
                        if d.error is not None]
        for d in parse_errors:
            print("%s:%d: error: frontmatter: %s" % (
                d.rel, d.error.line, d.error.message), file=sys.stderr)
        stats = compute_stats(bundle, today)
        if args.json:
            print(json.dumps(stats, indent=2))
        else:
            sys.stdout.write(render_stats(stats))
        return 1 if parse_errors else 0

    # index
    if not is_kb_bundle(bundle):
        raise UsageError("%s is not an evidence-kb bundle (no %s marker and "
                         "no brief.md with kb_profile: %s); refusing to "
                         "write index files" % (path, MARKER, PROFILE))
    blocking = [p for p in bundle.problems if p.level == "error"]
    blocking += [Problem("error", d.rel, d.error.line, "frontmatter: %s"
                         % d.error.message)
                 for d in bundle.docs.values() if d.error is not None]
    if blocking:
        for p in blocking:
            print(p, file=sys.stderr)
        print("index files not written: fix the errors above first",
              file=sys.stderr)
        return 1
    stale, failed = [], False
    with BundleLock(path) if not args.check else _NoLock():
        for rel, content in build_indexes(bundle).items():
            existing = bundle.get(rel)
            if existing is not None and existing.text == content:
                continue
            stale.append(rel)
            if not args.check:
                try:
                    _no_symlinks_below(path, path / rel)
                    safe_write(path, rel, content)
                except (OSError, UsageError) as exc:
                    print("%s:1: error: %s" % (rel, exc), file=sys.stderr)
                    failed = True
                    continue
            print("%s %s" % ("out of date:" if args.check else "wrote", rel),
                  file=sys.stderr)
    return 1 if failed or (args.check and stale) else 0


class _NoLock:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def main(argv=None):
    try:
        return _main(argv)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # never end with a traceback
        print("error: internal error (%s: %s); please report it"
              % (type(exc).__name__, exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
