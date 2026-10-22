"""Secret patterns: memories outside private topics that look like a key, a password, a card or an ID number.

The Sensitive information area of the Memory safety score (product-design.md §18.2) builds on this.
It is rules only, no model, and it reports which memory and which kind of pattern, never the matched text:
nothing here returns, logs or stores what matched. Memories in private topics (or marked private) are skipped,
since that is where such things belong. Phone numbers are left out because they match too much.

Kinds:

* ``key``: an API key, access token or private key: well-known prefixes (``sk-``, ``ghp_``, ``AKIA``, ``xoxb-``,
  ``AIza``, a JWT, ``-----BEGIN … PRIVATE KEY-----``) or a value of 16 or more characters, with a digit, after
  "api key", "token" or "secret".
* ``password``: "password", "passcode", "密码" or "口令" followed by ":", "=", "is" or "是" and a value with a
  digit or a symbol (so "the password is long" doesn't count).
* ``card``: 13 to 19 digits starting with 2 to 6 (the payment networks, Mastercard's 2-series included), spaces or
  dashes allowed, that pass the Luhn check.
* ``id_number``: a US Social Security number (``123-45-6789``, not 000, 666 or 9xx) or an 18-digit Chinese
  resident ID whose check character is right.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .vault import Vault

KINDS = ("key", "password", "card", "id_number")

_KEY = re.compile(
    r"(?<![A-Za-z0-9_-])(?:"
    r"sk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}"           # OpenAI, Anthropic
    r"|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}"
    r"|(?:AKIA|ASIA)[0-9A-Z]{16}"                      # AWS access key id
    r"|xox[abprs]-[A-Za-z0-9-]{10,}"                   # Slack
    r"|AIza[0-9A-Za-z_-]{35}"                          # Google
    r"|eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"  # JWT
    r")"
    r"|-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----"
    r"|\b(?:api[ _-]?key|access[ _-]?key|secret(?:[ _-]?key)?|(?:auth|access|bearer)?[ _-]?token)"
    r"\s*(?:[:=：]|\bis\b)\s*[\"']?(?=[A-Za-z_\-./+=]*\d)[A-Za-z0-9_\-./+=]{16,}",  # the value has a digit
    re.IGNORECASE,
)
_PASSWORD = re.compile(
    r"(?:\b(?:password|passwd|passcode|pwd)\b|密码|口令)\s*(?:[:=：]|\bis\b|是|为)\s*[\"'“]?"
    r"(?=[^\s\"'”，。,]*[\d!@#$%^&*_+=?-])[^\s\"'”，。,]{3,}",  # the value has a digit or a symbol
    re.IGNORECASE,
)
_CARD = re.compile(r"(?<![\d-])\d(?:[ -]?\d){12,18}(?![\d-])")
_SSN = re.compile(r"(?<![\d-])(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}(?![\d-])")
_CN_ID = re.compile(r"(?<![\dA-Za-z])\d{17}[\dXx](?![\dA-Za-z])")
_CN_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
_CN_CHECK = "10X98765432"


def _luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def _card(text: str) -> bool:
    for m in _CARD.finditer(text):
        digits = re.sub(r"[ -]", "", m.group())
        if 13 <= len(digits) <= 19 and digits[0] in "23456" and len(set(digits)) > 1 and _luhn(digits):
            return True
    return False


def _cn_id(text: str) -> bool:
    for m in _CN_ID.finditer(text):
        s = m.group().upper()
        if _CN_CHECK[sum(int(c) * w for c, w in zip(s[:17], _CN_WEIGHTS, strict=True)) % 11] == s[17]:
            return True
    return False


def kinds_in(text: str) -> list[str]:
    """The kinds of secret pattern in ``text``, in :data:`KINDS` order; empty when there are none."""
    found = {
        "key": bool(_KEY.search(text)),
        "password": bool(_PASSWORD.search(text)),
        "card": _card(text),
        "id_number": bool(_SSN.search(text)) or _cn_id(text),
    }
    return [k for k in KINDS if found[k]]


@dataclass(frozen=True)
class Finding:
    memory_id: str
    topic: str
    kind: str


def scan(vault: Vault) -> list[Finding]:
    """Every secret pattern in active memories outside private topics, one finding per memory and kind."""
    private = vault.private_topics()
    return [Finding(m.id, m.topic, kind)
            for m in vault.memories()
            if not (m.private or m.topic in private)
            for kind in kinds_in(m.content)]
