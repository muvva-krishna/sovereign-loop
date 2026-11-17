"""Regex & checksum PII scrubber adapted from agent-safety-kit for Indian enterprise data."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from contracts.models import PiiRedactionSummary


def _normalize_digits(raw: str) -> str:
    return re.sub(r"[^\d]", "", raw)


def _luhn_ok(digits: str) -> bool:
    """Return True if digits passes the Luhn checksum."""
    if not digits.isdigit() or not (13 <= len(digits) <= 19):
        return False
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def _cc_validator(raw: str) -> bool:
    return _luhn_ok(_normalize_digits(raw))


def _ssn_validator(raw: str) -> bool:
    digits = _normalize_digits(raw)
    if len(digits) != 9:
        return False
    area, group, serial = digits[:3], digits[3:5], digits[5:]
    if area in {"000", "666"} or area.startswith("9"):
        return False
    if group == "00" or serial == "0000":
        return False
    return True


def _us_phone_validator(raw: str) -> bool:
    digits = _normalize_digits(raw)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10 or len(set(digits)) == 1:
        return False
    return True


def _indian_phone_validator(raw: str) -> bool:
    digits = _normalize_digits(raw)
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    if len(digits) != 10 or len(set(digits)) == 1:
        return False
    return digits[0] in {"6", "7", "8", "9"}


@dataclass(frozen=True)
class _DetectorSpec:
    pii_type: str
    replacement: str
    pattern: re.Pattern[str]
    validator: Callable[[str], bool] | None = None


# Ordered by specificity so composite patterns (GSTIN, API tokens, CC) match before sub-patterns (PAN, phone).
_DETECTORS: Sequence[_DetectorSpec] = (
    _DetectorSpec(
        pii_type="api_token",
        replacement="[REDACTED:TOKEN]",
        pattern=re.compile(
            r"(?:\bBearer\s+[A-Za-z0-9\-._~+/]+=*|\bsk-[A-Za-z0-9_\-]{12,}\b)",
            re.IGNORECASE,
        ),
    ),
    _DetectorSpec(
        pii_type="email",
        replacement="[REDACTED:EMAIL]",
        pattern=re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"),
    ),
    _DetectorSpec(
        pii_type="indian_gstin",
        replacement="[REDACTED:GSTIN]",
        pattern=re.compile(r"\b\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]\b"),
    ),
    _DetectorSpec(
        pii_type="indian_pan",
        replacement="[REDACTED:PAN]",
        pattern=re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b"),
    ),
    _DetectorSpec(
        pii_type="credit_card",
        replacement="[REDACTED:CREDIT_CARD]",
        pattern=re.compile(r"(?<!\d)(?:\d[ -]*?){13,19}(?!\d)"),
        validator=_cc_validator,
    ),
    _DetectorSpec(
        pii_type="ssn",
        replacement="[REDACTED:SSN]",
        pattern=re.compile(r"\b(?!000|666|9\d{2})\d{3}[- ]?(?!00)\d{2}[- ]?(?!0000)\d{4}\b"),
        validator=_ssn_validator,
    ),
    _DetectorSpec(
        pii_type="phone",
        replacement="[REDACTED:PHONE]",
        pattern=re.compile(r"(?<!\d)(?:\+?91[-.\s]?)?[6-9]\d{4}[-.\s]?\d{5}(?!\d)"),
        validator=_indian_phone_validator,
    ),
    _DetectorSpec(
        pii_type="phone",
        replacement="[REDACTED:PHONE]",
        pattern=re.compile(r"(?<!\d)(?:\+?1[-.\s]?)?(?:\(?\d{3}\)?[-.\s]?)\d{3}[-.\s]?\d{4}(?!\d)"),
        validator=_us_phone_validator,
    ),
)


@dataclass(frozen=True)
class _SpanMatch:
    start: int
    end: int
    pii_type: str
    replacement: str
    priority: int


def scrub_text(text: str) -> tuple[str, PiiRedactionSummary]:
    """Redact PII/secrets in text and return (scrubbed_text, PiiRedactionSummary)."""
    if not text:
        return text, PiiRedactionSummary()

    candidates: list[_SpanMatch] = []
    for priority, spec in enumerate(_DETECTORS):
        for match in spec.pattern.finditer(text):
            raw = match.group(0)
            if spec.validator and not spec.validator(raw):
                continue
            candidates.append(
                _SpanMatch(
                    start=match.start(),
                    end=match.end(),
                    pii_type=spec.pii_type,
                    replacement=spec.replacement,
                    priority=priority,
                )
            )

    if not candidates:
        return text, PiiRedactionSummary()

    # Select non-overlapping spans: higher specificity (lower priority), earlier start, longer match.
    candidates.sort(key=lambda m: (m.priority, m.start, -(m.end - m.start)))
    accepted: list[_SpanMatch] = []
    for cand in candidates:
        overlaps = any(not (cand.end <= ex.start or cand.start >= ex.end) for ex in accepted)
        if not overlaps:
            accepted.append(cand)

    accepted.sort(key=lambda m: m.start)
    parts: list[str] = []
    cursor = 0
    types_found: set[str] = set()
    for span in accepted:
        parts.append(text[cursor : span.start])
        parts.append(span.replacement)
        cursor = span.end
        types_found.add(span.pii_type)
    parts.append(text[cursor:])

    return "".join(parts), PiiRedactionSummary(
        redacted_count=len(accepted),
        types_found=sorted(types_found),
    )


def scrub_payload(data: Any) -> tuple[Any, PiiRedactionSummary]:
    """Recursively scrub PII from strings inside nested dicts/lists/tuples."""
    total_count = 0
    types_found: set[str] = set()

    def _walk(node: Any) -> Any:
        nonlocal total_count
        if isinstance(node, str):
            cleaned, summary = scrub_text(node)
            total_count += summary.redacted_count
            types_found.update(summary.types_found)
            return cleaned
        if isinstance(node, dict):
            return {k: _walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [_walk(item) for item in node]
        if isinstance(node, tuple):
            return tuple(_walk(item) for item in node)
        return node

    scrubbed = _walk(data)
    return scrubbed, PiiRedactionSummary(
        redacted_count=total_count,
        types_found=sorted(types_found),
    )
