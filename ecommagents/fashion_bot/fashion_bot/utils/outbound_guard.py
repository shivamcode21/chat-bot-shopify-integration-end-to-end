"""Last-mile guard for customer-facing reply text.

Blocks two classes of prompt-leak defect from reaching a customer:

1. **Unresolved template token** — ``{{support_team_contact_details}}`` or the
   bare ``{support_team_contact_details}`` form, left in the reply because a
   substitution step was skipped, ran in the wrong order, or had no config to
   fill it with.
2. **Placeholder phone number** — a phone-shaped run whose digits are mostly one
   repeated digit (``+91 99999 99999``, ``9999999999``), which is what a model
   emits when it is handed an unfilled contact slot.

Both were seen in production between 2026-08-01 and 2026-08-04: the
early/fast-delivery paths instruct the agent to answer with an *exact* sentence
and forbid tool calls, so an unfilled ``{{support_team_contact_details}}`` slot
got filled by the model with a dummy number instead of being left blank.
``utils.utils.resolve_support_contact_placeholder`` fixes the substitution at
prompt-build time; this module is the backstop at the point of delivery, and
covers every future prompt that grows a placeholder nobody wired up.

The offending span is removed together with its leading connector ("at", "or",
":") so the surrounding sentence stays grammatical — the same treatment
``utils.utils._strip_support_contact_clause`` applies upstream.

Pure and stateless: callers own logging and error reporting.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List

__all__ = [
    "OutboundViolation",
    "OutboundGuardResult",
    "PLACEHOLDER_TOKEN",
    "PLACEHOLDER_PHONE",
    "TOOL_CALL_DIRECTIVE",
    "sanitize_outbound_text",
    "enrich_after_tool_call_directive",
]

PLACEHOLDER_TOKEN = "unresolved_placeholder"
PLACEHOLDER_PHONE = "placeholder_phone"
# The model described a tool call in prose (``[Call get_contact_information]``)
# instead of invoking the tool. Delivered on 2026-08-26/27 for Concept Groove.
TOOL_CALL_DIRECTIVE = "tool_call_directive"

# Shown only when sanitizing empties the whole reply (i.e. the reply *was*
# nothing but a leaked placeholder). Never appended to an otherwise valid reply.
_EMPTY_REPLY_FALLBACK = (
    "Our team will get back to you shortly to help with this. 🙏"
)

# A connector the offending span may hang off ("contact us at X", "email or X",
# "reach us: X"). Optional, so a bare span is still matched.
_CONNECTOR = r"(?:[ \t]*(?:\b(?:at|on|via|through|to|or|and)\b|[:\-–—]))?[ \t]*"

# Identifier-shaped placeholder body only — so a reply containing real JSON
# (``{"id": 1}``) or an empty ``{}`` is never mistaken for a template token.
_PLACEHOLDER_BODY = r"[A-Za-z_][A-Za-z0-9_]*(?:[ _][A-Za-z0-9_]+){0,4}"

_PLACEHOLDER_CLAUSE_RE = re.compile(
    _CONNECTOR
    + r"(\{\{\s*"
    + _PLACEHOLDER_BODY
    + r"\s*\}\}|\{\s*"
    + _PLACEHOLDER_BODY
    + r"\s*\})"
)

# Phone-shaped candidate: 7-20 chars of digits and the usual separators.
# Validity is decided on the digits alone in ``_is_placeholder_phone``.
#
# The leading lookbehind is load-bearing: without it the digit run inside a
# product URL is phone-shaped too, and stripping it silently breaks the link.
# Real catalogue slugs look like
#   https://gant.in/products/gant-gmw25-000000022105-black-polo-tshirt
# so a candidate may not begin straight after a letter, digit, or slug/query
# separator. A phone in prose is always preceded by a space or start-of-line.
_PHONE_CLAUSE_RE = re.compile(
    _CONNECTOR + r"(?<![A-Za-z0-9/=_-])(\+?\d[\d\s\-().]{5,18}\d)(?![A-Za-z/=_])"
)

# Seven identical digits in a row, not six: a real Indian mobile can carry six
# (8056666668 is a genuine customer number we ship in order details), while
# every dummy observed in production carries ten (+91 99999 99999 ->
# 919999999999). Real support lines — +919999727891, +91 730 6660 660,
# 1800 120 000 500 — top out at four.
_REPEATED_DIGIT_RUN_RE = re.compile(r"(\d)\1{6,}")

# A dialable number is at least 9 digits: Indian mobiles are 10, and 12 with a
# +91 prefix. Anything shorter is an order total, a PIN code, a quantity, or an
# id — not something a customer would call — so it is out of scope by length
# alone, before the repeated-digit test even runs.
_MIN_PHONE_DIGITS = 9
_MAX_PHONE_DIGITS = 15

# A "Phone: +91 9999999999 (Mon-Sat)" line loses its number and leaves a bare
# label behind. Safe, but it reads broken, so drop the whole line — only ever
# applied to a reply the guard has already had to touch.
_ORPHAN_CONTACT_LABEL_RE = re.compile(
    r"^[^\w\n]{0,6}"
    r"(?:phone|mobile|contact|call|tel|telephone|whatsapp|helpline|email|e-mail)"
    r"[^\w\n]{0,3}(?:\([^)\n]*\))?[^\w\n]{0,3}$",
    re.IGNORECASE | re.MULTILINE,
)

# A bracketed prose directive that names a tool the model was supposed to call,
# e.g. ``[Call get_contact_information]`` or ``[Email provided by
# get_contact_information]``. The model described the tool instead of invoking
# it, so an unfilled instruction reached the customer. Concept Groove delivered
# both shapes on 2026-08-26/27.
#
# Anchored on a snake_case identifier of at least two underscore-joined tokens
# (``get_contact_information``, ``escalate_to_agent``, ``fetch_customer_data``)
# — every fashion_bot tool name has this shape (see utils/tool_action_names.py)
# and it does not occur in natural prose. A trailing ``(`` on the closing bracket
# is ruled out so a markdown link ``[text](url)`` never matches — every real
# leak reached the customer as bare ``[...]`` with nothing following.
_TOOL_CALL_DIRECTIVE_RE = re.compile(
    _CONNECTOR
    + r"(\[[^\[\]\n]*[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+){1,}[^\[\]\n]*\])"
    + r"(?!\()"
)

# A number wrapped for emphasis ("call us at **+91-9999999999**") leaves the
# markers behind once it is gone. Collapse the empty pair rather than ship
# "call us at **** for immediate assistance".
#
# Only a pair with nothing but spaces between it is collapsed, so "**bold**"
# is untouched — matching a single "*" here would eat one half of every real
# bold delimiter in the reply.
_EMPTY_EMPHASIS_RE = re.compile(r"(\*\*|__)[ \t]*\1")


@dataclass(frozen=True)
class OutboundViolation:
    """One blocked span: what kind of leak it was, and the text removed."""

    kind: str
    matched: str


@dataclass(frozen=True)
class OutboundGuardResult:
    """Sanitized reply plus the violations that were stripped out of it."""

    text: str
    violations: List[OutboundViolation] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        """True when the guard had to remove something."""
        return bool(self.violations)

    @property
    def kinds(self) -> List[str]:
        """Distinct violation kinds, in first-seen order — for log lines."""
        seen: List[str] = []
        for violation in self.violations:
            if violation.kind not in seen:
                seen.append(violation.kind)
        return seen


def _is_placeholder_phone(candidate: str) -> bool:
    """True when *candidate* is phone-shaped and mostly one repeated digit."""
    digits = re.sub(r"\D", "", candidate)
    if not (_MIN_PHONE_DIGITS <= len(digits) <= _MAX_PHONE_DIGITS):
        return False
    return bool(_REPEATED_DIGIT_RUN_RE.search(digits))


def _tidy(text: str) -> str:
    """Close the gap a removed span leaves behind, without reflowing the reply."""
    text = re.sub(r"\(\s*\)", "", text)          # "(  )" left by a stripped span
    text = _EMPTY_EMPHASIS_RE.sub("", text)
    text = _ORPHAN_CONTACT_LABEL_RE.sub("", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"[ \t]+([,.;:!?])", r"\1", text)
    text = re.sub(r"([,;:])\s*([,.;:!?])", r"\2", text)  # ", ." -> "."
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)       # blank line left by a dropped label
    return text.strip()


def sanitize_outbound_text(text: str) -> OutboundGuardResult:
    """Strip leaked placeholders and dummy phone numbers from a reply.

    Idempotent: sanitizing already-clean text returns it unchanged with no
    violations, so applying the guard at more than one outbound hop is safe.

    Args:
        text: The customer-facing reply about to be delivered or persisted.

    Returns:
        An :class:`OutboundGuardResult` carrying the safe text and every span
        that was removed. Never raises — a reply must always go out.
    """
    if not text or not isinstance(text, str):
        return OutboundGuardResult(text=text or "")

    violations: List[OutboundViolation] = []

    def _drop_placeholder(match: re.Match) -> str:
        violations.append(
            OutboundViolation(kind=PLACEHOLDER_TOKEN, matched=match.group(1))
        )
        return ""

    def _drop_placeholder_phone(match: re.Match) -> str:
        candidate = match.group(1)
        if not _is_placeholder_phone(candidate):
            return match.group(0)
        violations.append(
            OutboundViolation(kind=PLACEHOLDER_PHONE, matched=candidate.strip())
        )
        return ""

    def _drop_tool_call_directive(match: re.Match) -> str:
        violations.append(
            OutboundViolation(kind=TOOL_CALL_DIRECTIVE, matched=match.group(1))
        )
        return ""

    cleaned = _PLACEHOLDER_CLAUSE_RE.sub(_drop_placeholder, text)
    cleaned = _PHONE_CLAUSE_RE.sub(_drop_placeholder_phone, cleaned)
    cleaned = _TOOL_CALL_DIRECTIVE_RE.sub(_drop_tool_call_directive, cleaned)

    if not violations:
        return OutboundGuardResult(text=text)

    cleaned = _tidy(cleaned)
    if not cleaned:
        cleaned = _EMPTY_REPLY_FALLBACK

    return OutboundGuardResult(text=cleaned, violations=violations)


# ---------------------------------------------------------------------------
# Post-guard enrichment: inject real contact details when a tool-call
# directive was stripped, so the customer still gets actionable info.
# ---------------------------------------------------------------------------

_CONTACT_SUFFIX_RE = re.compile(
    r"(?:please\s+)?(?:contact|reach out to|reach)\s+(?:us|our\s+\w+\s*(?:team)?)"
    r"[^.!?\n]*[.!?]?\s*$",
    re.IGNORECASE,
)


async def enrich_after_tool_call_directive(
    text: str, client_id: str | None, violations: List[OutboundViolation] | None = None
) -> str:
    """Append real vendor contact details when the guard stripped a directive.

    Called by outbound-guard callers (streaming_service, gupshup_webhook) AFTER
    ``sanitize_outbound_text`` detects a ``TOOL_CALL_DIRECTIVE`` violation. The
    guard itself stays pure/sync; this async companion fetches vendor config
    (tiered cache) and appends real contact info so the customer gets actionable
    details instead of a hollow "contact us" sentence.

    Only enriches when the stripped directive references a contact-related tool
    (``get_contact_information``, ``escalate_to_agent``). Other tool leaks
    (e.g. ``search_products``) are stripped but not enriched with contact info.

    Returns *text* unchanged when no contact data is configured.
    """
    if not client_id or not text:
        return text

    if violations:
        contact_related = any(
            "contact" in v.matched.lower() or "escalat" in v.matched.lower()
            for v in violations
            if v.kind == TOOL_CALL_DIRECTIVE
        )
        if not contact_related:
            return text

    try:
        from fashion_bot.config_manager import aget_config
        import json as _json

        contact_data = await aget_config(
            "vendor_contact_details", client_id=client_id
        )
        if not contact_data:
            return text

        if isinstance(contact_data, str):
            contact_data = _json.loads(contact_data)

        email = (contact_data.get("support email id") or "").strip()
        phones = (contact_data.get("support phone numbers") or "").strip()

        parts = [p for p in (phones, email) if p]
        if not parts:
            return text

        contact_str = " or ".join(parts)

        if _CONTACT_SUFFIX_RE.search(text):
            text = _CONTACT_SUFFIX_RE.sub(
                lambda m: m.group(0).rstrip(".!? ")
                + f" at {contact_str}."
                + (" " if m.group(0)[-1:] in " \n" else ""),
                text,
                count=1,
            ).rstrip()
        else:
            text = text.rstrip()
            sep = " " if text.endswith((".", "!", "?", "🙏")) else ". "
            text += f"{sep}You can reach us at {contact_str}."

        return text

    except Exception:
        return text
