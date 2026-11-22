"""Sensitive-data detection and redaction.

Extraction must never persist credentials, tokens, or government-issued
identifiers. This module scans text with a fixed rule set, returns the hits,
and can redact them. It is deliberately dependency-free and deterministic so
the rules are auditable.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable, List, Optional

# Order matters: the first matching rule wins for a given span.
_RULES: tuple = (
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    # JarvisMind's own agent/owner token: ``jmt_<8 hex public id>_<urlsafe secret>``.
    # Only the FULL form is a credential. The public prefix alone (``jmt_c0f26d38``)
    # is shown in listings by design, so it must keep passing: this rule requires
    # the ``_`` separator and a secret of at least 20 characters after it.
    ("jmt_token", re.compile(r"\bjmt_[0-9a-f]{8}_[A-Za-z0-9_-]{20,}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("bearer_token", re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{20,}\b", re.IGNORECASE)),
    ("credential_assignment", re.compile(
        r"\b(?:password|passwd|pwd|api[_-]?key|api[_-]?secret|access[_-]?token|"
        r"secret[_-]?key|private[_-]?key|auth[_-]?token)\b\s*[:=]\s*"
        r"['\"]?([^\s'\";,}]{6,})",
        re.IGNORECASE,
    )),
    # 003-B: additional authentication-secret shapes. They MUST precede the
    # PII rules below: ``scan()`` lets the first matching rule claim a span
    # (a DB connection URI contains an email-like tail, for example).
    ("pgp_private_key", re.compile(
        r"-----BEGIN PGP " r"PRIVATE KEY BLOCK-----[\s\S]*?-----END PGP " r"PRIVATE KEY BLOCK-----"
    )),
    ("private_key_header", re.compile(
        r"(?:-----BEGIN (?:[A-Z0-9 ]*PRIVATE KEY|SSH2 ENCRYPTED PRIVATE KEY)-----"
        r"|\bBEGIN [A-Z0-9 ]*PRIVATE KEY\b)"
    )),
    ("db_connection", re.compile(
        r"\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp|mssql|jdbc:[a-z]+)://"
        r"[^\s:/@]+:[^\s@/]+@[^\s/]+",
        re.IGNORECASE,
    )),
    ("verification_code", re.compile(
        r"(?:验证码|动态口令|校验码|短信码|一次性密码|OTP|one[- ]?time[- ]?(?:code|password))"
        r"[^\d]{0,12}(\d{4,8})\b",
        re.IGNORECASE,
    )),
    ("password_cn", re.compile(
        r"(?:密码|口令|通行码)\s*(?:是|为|:|：|=)\s*([^\s，,。;；]{4,})"
    )),
    ("generic_token", re.compile(
        r"\b(?:token|api[_-]?token|bearer[_-]?token|session[_-]?token)\b\s*[:=]\s*"
        r"['\"]?([^\s'\";,}]{6,})",
        re.IGNORECASE,
    )),
    ("id_card_cn", re.compile(r"\b[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx]\b")),
    ("bank_card", re.compile(r"\b(?:4[0-9]{12}(?:[0-9]{3})?|5[1-5][0-9]{14}|62[0-9]{14,17})\b")),
    ("phone_cn", re.compile(r"\b1[3-9]\d{9}\b")),
    ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
)

# ---------------------------------------------------------------------------
# Tolerant pass (JME-MEMORY-OVERVIEW-01-R2 / A1).
#
# The rules above are exact: a phone number written "138 0013 8000" or a
# password written in natural Chinese slips past them. These patterns run
# against (a) the NFKC-normalised text and (b) a copy with separators removed
# between digits, so spacing / full-width / punctuation cannot be used to hide
# a value.
#
# A hit here means PROTECTED, exactly like an exact hit. The tolerant pass
# never returns spans, so it is used only for gating, never for redaction.
_TOLERANT_RULES: tuple = (
    ("phone_sep", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    ("password_cn_tolerant", re.compile(
        r"(?:密码|口令|通行码|passcode)\s*(?:是|为|:|：|=|叫|改成|设置为|设为)?\s*"
        r"([A-Za-z0-9!@#$%^&*()_\-+=.]{4,})",
        re.IGNORECASE,
    )),
    ("email_obfuscated", re.compile(
        r"\b[A-Za-z0-9._%+-]+\s*(?:@|\(at\)|\[at\]|\bat\b|＠)\s*[A-Za-z0-9-]+"
        r"(?:\s*(?:\.|\(dot\)|\[dot\]|\bdot\b|点|。)\s*[A-Za-z0-9-]+)*"
        r"\s*(?:\.|\(dot\)|\[dot\]|\bdot\b|点|。)\s*[A-Za-z]{2,}\b",
        re.IGNORECASE,
    )),
    ("id_card_sep", re.compile(r"\b\d{6}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx]\b")),
    ("bank_card_sep", re.compile(r"\b(?:4\d{12}(?:\d{3})?|5[1-5]\d{14}|62\d{14,17})\b")),
    ("verification_code_tolerant", re.compile(
        r"(?:验证码|动态口令|校验码|短信码|一次性密码|OTP|one[- ]?time[- ]?(?:code|password))"
        r"[^\d]{0,16}(\d{4,8})\b",
        re.IGNORECASE,
    )),
)

#: Tolerant categories that are credentials, not merely PII.
_TOLERANT_CREDENTIALS = frozenset({
    "password_cn_tolerant",
    "verification_code_tolerant",
})

#: Redaction patterns for tolerant hits. Unlike ``_TOLERANT_RULES`` these match
#: the *whole* span in the original text (separators included), so ``redact``
#: can remove the value instead of leaving it behind.
_TOLERANT_REDACT: tuple = (
    ("phone_sep", re.compile(r"1[3-9]\d(?:[\s\-_.·•—–]?\d){8}")),
    ("id_card_sep", re.compile(r"\d{6}[\s\-]?(?:19|20)\d{2}[\s\-]?(?:0[1-9]|1[0-2])[\s\-]?(?:0[1-9]|[12]\d|3[01])[\s\-]?\d{3}[\dXx]")),
    ("bank_card_sep", re.compile(r"(?:4\d{3}[\s\-]?\d{4}[\s\-]?\d{4}[\s\-]?\d{4}|5[1-5]\d{2}[\s\-]?\d{4}[\s\-]?\d{4}[\s\-]?\d{4}|62\d{2}[\s\-]?\d{4}[\s\-]?\d{4}[\s\-]?\d{4,7})")),
    ("email_obfuscated", re.compile(
        r"[A-Za-z0-9._%+-]+\s*(?:\(at\)|\[at\]|\bat\b|＠|@)\s*[A-Za-z0-9-]+"
        r"(?:\s*(?:\(dot\)|\[dot\]|\bdot\b|点|。)\s*[A-Za-z0-9-]+)*"
        r"\s*(?:\(dot\)|\[dot\]|\bdot\b|点|。)\s*[A-Za-z]{2,}",
        re.IGNORECASE,
    )),
    ("password_cn_tolerant", re.compile(
        r"(?:密码|口令|通行码|passcode)\s*(?:是|为|:|：|=|叫|改成|设置为|设为)?\s*"
        r"[A-Za-z0-9!@#$%^&*()_\-+=.]{4,}",
        re.IGNORECASE,
    )),
)

_DIGIT_SEPARATORS = re.compile(r"(?<=\d)[ \t\-_.·•—–](?=\d)")


def normalize_for_scan(text: str) -> str:
    """NFKC-normalise so full-width digits/letters cannot hide a value."""
    return unicodedata.normalize("NFKC", text)


def collapse_digit_separators(text: str) -> str:
    """``138 0013 8000`` / ``138-0013-8000`` -> ``13800138000``."""
    return _DIGIT_SEPARATORS.sub("", text)


def tolerant_categories(text: Optional[str]) -> List[str]:
    """Categories found after normalisation. Value-free; never returns spans."""
    if not text:
        return []
    normalized = normalize_for_scan(text)
    collapsed = collapse_digit_separators(normalized)
    found = set()
    for category, pattern in _TOLERANT_RULES:
        haystack = collapsed if category.endswith("_sep") else normalized
        if pattern.search(haystack):
            found.add(category)
    return sorted(found)


REDACTED = "[REDACTED]"

# 003-B: credentials are permanently BLOCKed and never enter an approval flow.
# PII is a separate class: it may require an explicit dual authorization, but
# is never silently downgraded into a credential or a normal memory.
# ``api_token`` has no regex in _RULES: it is produced ONLY by
# security_gate.scan_credentials(exact_tokens=...) matching a live configured
# secret. ``unsafe_metadata`` is a conservative refusal marker (a metadata
# shape that cannot be exhaustively scanned), not a regex hit. Both are BLOCK
# categories so no caller can confuse them with ordinary PII.
CREDENTIAL_CATEGORIES = frozenset({
    "private_key", "pgp_private_key", "private_key_header", "jwt",
    "openai_key", "github_token", "aws_access_key", "slack_token",
    "bearer_token", "credential_assignment", "generic_token", "jmt_token",
    "db_connection", "verification_code", "password_cn", "api_token",
    "unsafe_metadata",
})

PII_CATEGORIES = frozenset({
    "id_card_cn", "bank_card", "phone_cn", "email",
})


def is_credential_category(category: str) -> bool:
    return category in CREDENTIAL_CATEGORIES


def credential_categories(text: Optional[str]) -> List[str]:
    """Return only credential categories (never the matched values)."""
    exact = {
        hit.category for hit in scan(text) if is_credential_category(hit.category)
    }
    # Tolerant pass: "密码 hunter2 用于登录" has no "是/为" separator.
    exact.update(
        category
        for category in tolerant_categories(text)
        if category in _TOLERANT_CREDENTIALS
    )
    return sorted(exact)


def pii_categories(text: Optional[str]) -> List[str]:
    """Return only personal-information categories (never the matched values)."""
    exact = {
        hit.category for hit in scan(text) if hit.category in PII_CATEGORIES
    }
    # Tolerant pass: spacing / full-width / obfuscation must not hide PII.
    # Credential-shaped tolerant hits belong to ``credential_categories``.
    exact.update(
        category
        for category in tolerant_categories(text)
        if category not in _TOLERANT_CREDENTIALS
    )
    return sorted(exact)


@dataclass(frozen=True)
class SensitiveHit:
    category: str
    start: int
    end: int
    value: str

    def to_dict(self) -> dict:
        # The matched value is never returned; only its category and length.
        return {"category": self.category, "start": self.start, "end": self.end}


def scan(text: Optional[str]) -> List[SensitiveHit]:
    """Return every sensitive hit found in ``text``."""
    if not text:
        return []
    hits: List[SensitiveHit] = []
    claimed: List[tuple] = []
    for category, pattern in _RULES:
        for match in pattern.finditer(text):
            span = match.span()
            if any(span[0] < end and start < span[1] for start, end in claimed):
                continue
            claimed.append(span)
            hits.append(
                SensitiveHit(
                    category=category,
                    start=span[0],
                    end=span[1],
                    value=text[span[0]:span[1]],
                )
            )
    hits.sort(key=lambda hit: hit.start)
    return hits


def contains_sensitive(text: Optional[str]) -> bool:
    """True when either the exact or the tolerant pass flags the text."""
    return bool(scan(text)) or bool(tolerant_categories(text))


def categories(text: Optional[str]) -> List[str]:
    """Exact-scan categories (compatibility surface used by the extractor)."""
    return sorted({hit.category for hit in scan(text)})


def redact(text: Optional[str], extra_tokens: Iterable[str] = ()) -> str:
    """Replace sensitive spans with a placeholder.

    ``extra_tokens`` lets the caller also redact a live API token that should
    never appear in stored text.
    """
    if not text:
        return ""
    hits = scan(text)
    # Tolerant redaction: remove spaced / obfuscated values that the exact
    # scanner cannot see (``138 0013 8000``, ``foo [at] bar [dot] com``).
    normalized = normalize_for_scan(text)
    for category, pattern in _TOLERANT_REDACT:
        for match in pattern.finditer(normalized):
            span = match.span()
            if any(span[0] < hit.end and hit.start < span[1] for hit in hits):
                continue
            hits.append(
                SensitiveHit(
                    category=category,
                    start=span[0],
                    end=span[1],
                    value=normalized[span[0] : span[1]],
                )
            )
    for token in extra_tokens:
        if token and token in text:
            offset = 0
            while True:
                index = text.find(token, offset)
                if index == -1:
                    break
                hits.append(
                    SensitiveHit(
                        category="api_token",
                        start=index,
                        end=index + len(token),
                        value=token,
                    )
                )
                offset = index + len(token)
    if not hits:
        return text
    hits.sort(key=lambda hit: (hit.start, -hit.end))
    result: List[str] = []
    cursor = 0
    for hit in hits:
        if hit.start < cursor:
            continue
        result.append(text[cursor:hit.start])
        result.append(REDACTED)
        cursor = hit.end
    result.append(text[cursor:])
    return "".join(result)


__all__ = [
    "CREDENTIAL_CATEGORIES",
    "PII_CATEGORIES",
    "REDACTED",
    "SensitiveHit",
    "categories",
    "collapse_digit_separators",
    "contains_sensitive",
    "credential_categories",
    "is_credential_category",
    "normalize_for_scan",
    "pii_categories",
    "redact",
    "scan",
    "tolerant_categories",
]
