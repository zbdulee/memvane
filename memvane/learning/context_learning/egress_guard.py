"""Reject credential-shaped input before project teacher transmission.

The caller excludes the whole case: rewriting text would change the immutable
revision/selection task. Findings contain only codes, field locations and hashes.
This is a conservative pattern gate, not a guarantee that arbitrary prose has
no secret. Only explicitly consented captured fields may reach this boundary.
"""
import re

from .contracts import digest

_PATTERNS = (
    ('private_key', re.compile(r'-----BEGIN (?:[A-Z0-9 ]* )?PRIVATE KEY-----')),
    ('provider_token', re.compile(r'\b(?:sk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|xox[baprs]-[A-Za-z0-9-]{12,}|AIza[A-Za-z0-9_-]{30,}|(?:AKIA|ASIA)[A-Z0-9]{16})\b')),
    ('jwt', re.compile(r'\beyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{6,}\b')),
    ('authorization', re.compile(r'(?i)\b(?:bearer|basic)\s+[A-Za-z0-9+/=_~.-]{8,}')),
    ('credential_assignment', re.compile(r'''(?ix)\b(?:[a-z0-9_-]+[_-])?(?:api[_-]?key|access[_-]?token|refresh[_-]?token|auth[_-]?token|client[_-]?secret|password|passwd|secret[_-]?key|aws[_-]?secret[_-]?access[_-]?key|private[_-]?key|token|secret|cookie|authorization)\b["']?\s*[:=]\s*["']?[^\s,"';}]{3,}''')),
    ('cookie', re.compile(r'(?im)^\s*(?:set-cookie|cookie|authorization|proxy-authorization)\s*:\s*\S+')),
    ('url_credentials', re.compile(r'(?i)\b[a-z][a-z0-9+.-]*://[^\s/@]+@')),
)


def inspect_payload(value):
    findings=[]
    def walk(item, field):
        if isinstance(item,str):
            for code,pattern in _PATTERNS:
                if pattern.search(item):
                    findings.append({'code':code,'field':field,'value_sha256':digest(item)})
        elif isinstance(item,list):
            for index,child in enumerate(item):walk(child,f'{field}[{index}]')
        elif isinstance(item,dict):
            # Payload keys are trusted fixed protocol keys, never source text.
            for key,child in item.items():walk(child,f'{field}.{key}')
    walk(value,'payload')
    return {'allowed':not findings,'findings':findings}
