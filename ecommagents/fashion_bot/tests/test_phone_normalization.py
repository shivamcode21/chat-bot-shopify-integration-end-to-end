"""
Regression tests for phone-number normalization across the order-creation path.

Motivated by a production incident (client "Sereko", agent order-creation):
a customer's first message contained an 11-digit typo "80875053289" (real
number: "8075053289"). The old `extract_webchat_phone_candidate` blindly kept
the last 10 digits (`digits[-10:]`), turning the typo into a plausible-but-wrong
"0875053289" which got linked to the session and then shadowed every corrected
format the customer typed afterwards (+91 8075053289, +918075053289). Downstream
validators also rejected a number containing a space.

These tests pin the fixed behaviour:
  * an over-long typo is rejected (re-prompt), never truncated into a wrong number
  * well-defined +91/91/0 prefixes are stripped to a clean 10-digit number
  * validators tolerate spaces / dashes / dots
"""

import pytest

from fashion_bot.utils.utils import extract_webchat_phone_candidate
from fashion_bot.shopify.modules.customer_apis import _normalize_phone_variants
from fashion_bot.shopify.modules.order_creation_api import CustomerInfo


# The exact strings from the incident.
TYPO_11_DIGIT = "80875053289"        # extra digit — must be rejected
CORRECT_BARE = "8075053289"          # the real number
CORRECT_E164 = "+918075053289"
CORRECT_SPACED = "+91 8075053289"    # space tripped the old strict regex
ALT_NUMBER = "+919447632077"


class TestExtractWebchatPhoneCandidate:
    def test_rejects_over_long_typo_instead_of_truncating(self):
        # The root cause: the old code returned "0875053289" here.
        assert extract_webchat_phone_candidate(TYPO_11_DIGIT) is None

    @pytest.mark.parametrize(
        "raw,expected",
        [
            (CORRECT_BARE, "8075053289"),
            (CORRECT_E164, "8075053289"),
            (CORRECT_SPACED, "8075053289"),
            (ALT_NUMBER, "9447632077"),
            ("918075053289", "8075053289"),   # 91 prefix, no plus
            ("08075053289", "8075053289"),    # leading 0 trunk prefix
            ("my number is 9447632077 thanks", "9447632077"),
        ],
    )
    def test_accepts_valid_formats(self, raw, expected):
        assert extract_webchat_phone_candidate(raw) == expected

    def test_corrected_message_is_not_shadowed_by_stale_state(self):
        # The gate now extracts from the user's message only. Simulate that the
        # corrected number is what we read, not a stale/corrupted state value.
        assert extract_webchat_phone_candidate(CORRECT_SPACED) == "8075053289"


class TestNormalizePhoneVariants:
    def test_tolerates_space(self):
        variants = _normalize_phone_variants(CORRECT_SPACED)
        assert variants == ["8075053289", "+918075053289", "918075053289"]

    def test_rejects_over_long_typo(self):
        assert _normalize_phone_variants(TYPO_11_DIGIT) is None

    @pytest.mark.parametrize(
        "raw",
        [CORRECT_BARE, CORRECT_E164, CORRECT_SPACED, "08075053289", "918075053289"],
    )
    def test_accepts_valid_formats(self, raw):
        assert _normalize_phone_variants(raw) is not None


class TestIsValidPhone:
    @pytest.mark.parametrize(
        "raw",
        [CORRECT_BARE, CORRECT_E164, CORRECT_SPACED, "918075053289", "08075053289"],
    )
    def test_accepts_valid_formats_including_whitespace(self, raw):
        assert CustomerInfo._is_valid_phone(raw) is True

    @pytest.mark.parametrize("raw", [TYPO_11_DIGIT, "", "12345", "abcdefghij"])
    def test_rejects_invalid(self, raw):
        assert CustomerInfo._is_valid_phone(raw) is False
