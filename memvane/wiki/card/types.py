"""Value types and bounds for wiki cards."""
import re

MAX_CARDS = 2_048

SAFE_KEY_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*")
SOURCE_SPANS_KEY = "sourceSpans"
MAX_SOURCE_SPANS = 16
FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.S)

SUMMARY_TRUNCATION_MARK = "… (이하 생략)"
SOURCE_KIND_FILE = "file"

SOURCE_REFRESH_PENDING = "source-refresh-pending.jsonl"
