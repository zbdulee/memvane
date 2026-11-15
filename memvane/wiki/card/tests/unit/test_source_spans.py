#!/usr/bin/env python3
"""Synthetic checks for verifier-cited source line spans; no model, no project data.

Expected line numbers and slice bytes are written out literally from the
synthetic source below, not recomputed with the module under test.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile
import unittest

from memvane.wiki.card import source_spans
from memvane.wiki.card import yaml_scalars

SOURCE = (
    "# Title\n"                    # 1
    "\n"                           # 2
    "포트는 8080이다.\n"            # 3
    "Second line of fact\n"        # 4
    "continues here.\n"            # 5
    "repeat me\n"                  # 6
    "repeat me\n"                  # 7
    "aaa\n"                        # 8
    "tail fact at end\n"           # 9
)
DATA = SOURCE.encode("utf-8")
SHA = hashlib.sha256(DATA).hexdigest()
PATH = "docs/source.md"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def evidence(data=DATA, shown=None, path=PATH):
    text = data.decode("utf-8")
    item = source_spans.evidence_item(path, sha(data), data,
                                      len(text) if shown is None else shown)
    assert item is not None
    return {path: item}


def claim(quote, path=PATH, supported=True):
    return {"claim": "c", "supported": supported, "quote": quote, "sourcePath": path}


class DeriveChecks(unittest.TestCase):
    def test_single_line_multibyte_quote(self):
        spans = source_spans.derive_spans([claim("포트는 8080")], evidence())
        self.assertEqual(spans, [{
            "path": PATH, "sourceSha256": SHA, "startLine": 3, "endLine": 3,
            "spanSha256": sha("포트는 8080이다.\n".encode("utf-8"))}])

    def test_multiline_quote_hashes_whole_line_slice(self):
        spans = source_spans.derive_spans(
            [claim("of fact\ncontinues")], evidence())
        self.assertEqual([(s["startLine"], s["endLine"]) for s in spans], [(4, 5)])
        self.assertEqual(spans[0]["spanSha256"],
                         sha(b"Second line of fact\ncontinues here.\n"))

    def test_duplicate_overlapping_and_paraphrased_quotes_are_not_located(self):
        for quote in ("repeat me", "aa", "Port is 8080", "포트는  8080"):
            with self.subTest(quote=quote):
                self.assertEqual(source_spans.derive_spans([claim(quote)], evidence()), [])

    def test_quote_must_lie_inside_shown_prefix(self):
        tail_start = SOURCE.index("tail fact")
        self.assertEqual(source_spans.derive_spans(
            [claim("tail fact")], evidence(shown=tail_start)), [])
        straddle = SOURCE.index("continues here.") + 5
        self.assertEqual(source_spans.derive_spans(
            [claim("continues here.")], evidence(shown=straddle)), [])
        located = source_spans.derive_spans([claim("tail fact")], evidence())
        self.assertEqual([(s["startLine"], s["endLine"]) for s in located], [(9, 9)])

    def test_decoy_or_respelled_source_path_is_ignored(self):
        for path in ("docs/other.md", "./docs/source.md", "docs//source.md", ""):
            with self.subTest(path=path):
                self.assertEqual(source_spans.derive_spans(
                    [claim("포트는 8080", path=path)], evidence()), [])

    def test_only_supported_true_claims_are_located(self):
        for supported in (False, None, "true", 1):
            with self.subTest(supported=supported):
                self.assertEqual(source_spans.derive_spans(
                    [claim("포트는 8080", supported=supported)], evidence()), [])
        self.assertEqual(source_spans.derive_spans([claim("   \n ")], evidence()), [])

    def test_mixed_claims_keep_only_locatable_spans_sorted_and_deduplicated(self):
        spans = source_spans.derive_spans([
            claim("tail fact"), claim("Port is 8080"), claim("포트는"),
            claim("8080이다"), claim("repeat me")], evidence())
        self.assertEqual([(s["startLine"], s["endLine"]) for s in spans], [(3, 3), (9, 9)])

    def test_crlf_lines_split_on_newline_only(self):
        data = b"a\r\nb fact\r\n"
        spans = source_spans.derive_spans([claim("b fact")], evidence(data))
        self.assertEqual([(s["startLine"], s["spanSha256"]) for s in spans],
                         [(2, sha(b"b fact\r\n"))])

    def test_evidence_refuses_bytes_that_do_not_match_revision(self):
        self.assertIsNone(source_spans.evidence_item(PATH, "0" * 64, DATA, 10))
        self.assertIsNone(source_spans.evidence_item(PATH, sha(b"\xff"), b"\xff", 1))


class SliceAndVerifyChecks(unittest.TestCase):
    def test_line_slice_bounds(self):
        self.assertEqual(source_spans.line_slice(b"x\ny", 2, 2), b"y")
        self.assertIsNone(source_spans.line_slice(b"x\n", 2, 2))  # no phantom line
        self.assertIsNone(source_spans.line_slice(b"x\ny\n", 1, 3))
        self.assertIsNone(source_spans.line_slice(b"", 1, 1))
        self.assertIsNone(source_spans.line_slice(b"x\n", 0, 1))

    def test_verify_span_uses_current_full_bytes(self):
        [span] = source_spans.derive_spans([claim("포트는 8080")], evidence())
        self.assertTrue(source_spans.verify_span(span, DATA))
        self.assertFalse(source_spans.verify_span(span, DATA.replace(b"8080", b"9090")))
        # Bytes elsewhere changed: the revision binding fails even if the line matches.
        self.assertFalse(source_spans.verify_span(span, DATA.replace(b"# Title", b"# Other")))
        moved = b"inserted\n" + DATA
        self.assertFalse(source_spans.verify_span({**span, "sourceSha256": sha(moved)}, moved))


def span_block(*entries: str) -> str:
    return "title: x\nsourceSpans:\n" + "".join(f"  - {e}\n" for e in entries) + "status: verified"


VALID = ('{path: "docs/source.md", sourceSha256: "%s", startLine: 3, endLine: 4, '
         'spanSha256: "%s"}' % ("a" * 64, "b" * 64))


class FrontmatterChecks(unittest.TestCase):
    def test_round_trip_and_absent(self):
        spans = [{"path": PATH, "sourceSha256": "a" * 64, "startLine": 3, "endLine": 4,
                  "spanSha256": "b" * 64}]
        emitted = yaml_scalars.dump_source_spans(spans)
        self.assertEqual(emitted, [VALID])
        self.assertEqual(source_spans.read_card_spans(span_block(*emitted)),
                         (source_spans.PRESENT, spans))
        self.assertEqual(source_spans.read_card_spans("title: x\nstatus: verified"),
                         (source_spans.ABSENT, []))

    def test_malformed_present_lists_are_invalid_without_partial_result(self):
        other = VALID.replace("startLine: 3, endLine: 4", "startLine: 9, endLine: 9")
        cases = {
            "quoted_int": VALID.replace("startLine: 3", 'startLine: "3"'),
            "zero_line": VALID.replace("startLine: 3", "startLine: 0"),
            "reversed": VALID.replace("endLine: 4", "endLine: 2"),
            "extra_key": VALID[:-1] + ', quote: "fact"}',
            "missing_key": VALID.replace(', spanSha256: "' + "b" * 64 + '"', ""),
            "duplicate_key": VALID[:-1] + ", startLine: 3}",
            "short_sha": VALID.replace("a" * 64, "a" * 63),
            "absolute_path": VALID.replace('"docs/source.md"', '"/etc/passwd"'),
            "dotdot_path": VALID.replace('"docs/source.md"', '"docs/../x.md"'),
            "hash_path": VALID.replace('"docs/source.md"', '"docs/a#b.md"'),
            "too_many_lines": VALID.replace("endLine: 4", "endLine: 203"),
            "huge_line": VALID.replace("startLine: 3", "startLine: 123456789"),
        }
        for name, entry in cases.items():
            with self.subTest(name=name):
                self.assertEqual(source_spans.read_card_spans(span_block(other, entry)),
                                 (source_spans.INVALID, []))
        layouts = {
            "duplicate_entry": span_block(VALID, VALID),
            "duplicate_header": span_block(VALID) + "\nsourceSpans:\n  - " + other,
            "quoted_duplicate_header": span_block(VALID) + '\n"sourceSpans":\n  - ' + other,
            "inline_value": "sourceSpans: []",
            "empty_list": "sourceSpans:\nstatus: verified",
            "bad_indent": "sourceSpans:\n    - " + VALID,
            "tab_indent": "sourceSpans:\n\t- " + VALID,
            "nested_mapping": "sourceSpans:\n  - path: docs/source.md",
            "over_budget": span_block(*[VALID.replace("startLine: 3, endLine: 4",
                                                      f"startLine: {n}, endLine: {n}")
                                        for n in range(1, 18)]),
        }
        for name, block in layouts.items():
            with self.subTest(name=name):
                self.assertEqual(source_spans.read_card_spans(block), (source_spans.INVALID, []))

    def test_dump_refuses_any_bad_sibling(self):
        good = {"path": PATH, "sourceSha256": "a" * 64, "startLine": 1, "endLine": 1,
                "spanSha256": "b" * 64}
        self.assertEqual(yaml_scalars.dump_source_spans([good, {**good, "startLine": True}]), [])
        self.assertEqual(yaml_scalars.dump_source_spans([good, dict(good)]), [])
        self.assertEqual(yaml_scalars.dump_source_spans([]), [])
