"""Value types and bounds for wiki cards."""
from __future__ import annotations
import re

MAX_CARDS = 2_048

SAFE_KEY_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*")
SOURCE_SPANS_KEY = "sourceSpans"
MAX_SOURCE_SPANS = 16
FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.S)
AUTO_START_PATTERN = r'<!-- qmd:auto:start id="main" sourceHash="([a-f0-9]+)" -->'
AUTO_END = "<!-- qmd:auto:end -->"
AUTO_START_RE = re.compile(AUTO_START_PATTERN)
AUTO_BLOCK_RE = re.compile(AUTO_START_PATTERN + r"\n.*?\n" + re.escape(AUTO_END), re.S)

SUMMARY_TRUNCATION_MARK = "… (이하 생략)"
SOURCE_KIND_FILE = "file"

SOURCE_REFRESH_PENDING = "source-refresh-pending.jsonl"
