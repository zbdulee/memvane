"""Shared index errors, SQLite read mode and FTS text normalization."""
from bisect import bisect_right
from pathlib import Path
import re
import sys
import time


class IndexContractError(ValueError):
    pass


def _query_only_uri(path):
    """Open existing SQLite data for query_only, including macOS Python 3.9 WAL."""
    mode = 'rw' if sys.platform == 'darwin' and sys.version_info[:2] == (3, 9) else 'ro'
    return Path(path).as_uri() + '?mode=' + mode


def _qmd_title(body, path):
    match = re.search(r'^##?\s+(.+)$', body, re.MULTILINE)
    if match:
        title = match.group(1).strip()
        if title in ('📝 Notes', 'Notes'):
            next_title = re.search(r'^##\s+(.+)$', body, re.MULTILINE)
            if next_title:
                return next_title.group(1).strip()
        return title
    name = path.rsplit('/', 1)[-1]
    return name.rsplit('.', 1)[0] or name


# Expanded from QMD's four JavaScript ``\p{Script=...}`` properties under the
# selected Node 24.21.0 / Unicode 17.0 runtime. Unicode blocks include gaps
# and omit supplementary Script members, so broad block ranges are incorrect.
_QMD_CJK_SCRIPT_RANGES = (
    (0x2e80, 0x2e99), (0x2e9b, 0x2ef3), (0x2f00, 0x2fd5),
    (0x3005, 0x3005), (0x3007, 0x3007), (0x3021, 0x3029),
    (0x3038, 0x303b), (0x3400, 0x4dbf), (0x4e00, 0x9fff),
    (0xf900, 0xfa6d), (0xfa70, 0xfad9), (0x16fe2, 0x16fe3),
    (0x16ff0, 0x16ff6), (0x20000, 0x2a6df), (0x2a700, 0x2b81d),
    (0x2b820, 0x2cead), (0x2ceb0, 0x2ebe0), (0x2ebf0, 0x2ee5d),
    (0x2f800, 0x2fa1d), (0x30000, 0x3134a), (0x31350, 0x33479),
    (0x3041, 0x3096), (0x309d, 0x309f), (0x1b001, 0x1b11f),
    (0x1b132, 0x1b132), (0x1b150, 0x1b152), (0x1f200, 0x1f200),
    (0x30a1, 0x30fa), (0x30fd, 0x30ff), (0x31f0, 0x31ff),
    (0x32d0, 0x32fe), (0x3300, 0x3357), (0xff66, 0xff6f),
    (0xff71, 0xff9d), (0x1aff0, 0x1aff3), (0x1aff5, 0x1affb),
    (0x1affd, 0x1affe), (0x1b000, 0x1b000), (0x1b120, 0x1b122),
    (0x1b155, 0x1b155), (0x1b164, 0x1b167),
    (0x1100, 0x11ff), (0x302e, 0x302f), (0x3131, 0x318e),
    (0x3200, 0x321e), (0x3260, 0x327e), (0xa960, 0xa97c),
    (0xac00, 0xd7a3), (0xd7b0, 0xd7c6), (0xd7cb, 0xd7fb),
    (0xffa0, 0xffbe), (0xffc2, 0xffc7), (0xffca, 0xffcf),
    (0xffd2, 0xffd7), (0xffda, 0xffdc),
)
_QMD_CJK_SCRIPT_RANGES = tuple(sorted(_QMD_CJK_SCRIPT_RANGES))
_QMD_CJK_SCRIPT_STARTS = tuple(start for start, _end in _QMD_CJK_SCRIPT_RANGES)


def _cjk(ch):
    code = ord(ch)
    if code < 0x1100:
        return False
    offset = bisect_right(_QMD_CJK_SCRIPT_STARTS, code) - 1
    return offset >= 0 and code <= _QMD_CJK_SCRIPT_RANGES[offset][1]


# One character class over the same ranges as ``_cjk``; the search check
# runs this over every body of a generation, so it must not loop per char.
_QMD_CJK_RUN = re.compile('[' + ''.join(
    '\\U%08x-\\U%08x' % (start, end) for start, end in _QMD_CJK_SCRIPT_RANGES) + ']+')


def _space_cjk_run(match):
    # ' a b c ' for the run 'abc': a space before each code point and after.
    return ' ' + ' '.join(match.group()) + ' '


def _qmd_fts_text(text, *, deadline=None):
    """Space CJK runs by code point, as QMD's normalizeCjkForFTS does.

    Every code point of a maximal CJK run gets a leading space and the run a
    trailing one; everything else is copied. The deadline is checked before
    the work and, for text longer than one 4096-char block, after it.
    """
    if text and deadline is not None and time.monotonic() >= deadline:
        raise IndexContractError('fts_deadline_exceeded')
    out = text if isinstance(text, str) and text.isascii() else _QMD_CJK_RUN.sub(_space_cjk_run, text)
    if len(text) > 4096 and deadline is not None and time.monotonic() >= deadline:
        raise IndexContractError('fts_deadline_exceeded')
    return out


