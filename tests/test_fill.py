"""Fill and verify decision logic.

All pure functions — the Playwright layer is deliberately thin so the parts
that decide *what* to write can be tested without a browser.
"""

from __future__ import annotations

import pytest

from job_agent.fill.verify import values_equivalent
from job_agent.fill.writer import best_option, normalize_for_type


# --------------------------------------------------------------------------
# option matching
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "options", "expected"),
    [
        ("Yes", ["Yes", "No"], "Yes"),
        ("yes", ["Yes", "No"], "Yes"),
        # Real forms rarely use bare Yes/No.
        ("Yes", ["Yes, I am authorized to work", "No"], "Yes, I am authorized to work"),
        ("No", ["Yes, I will require sponsorship", "No, I will not"], "No, I will not"),
        ("Yes", ["I agree", "I decline"], "I agree"),
        # Containment beats fuzzy noise.
        ("California", ["US-California", "US-Texas"], "US-California"),
        ("Female", ["Male", "Female", "Decline to self-identify"], "Female"),
        ("Bachelor's", ["Bachelor's Degree", "Master's Degree"], "Bachelor's Degree"),
    ],
)
def test_best_option(value: str, options: list[str], expected: str) -> None:
    assert best_option(value, options) == expected


def test_best_option_refuses_when_nothing_matches() -> None:
    """A wrong screening answer is worse than an unanswered one."""
    assert best_option("Purple", ["Yes", "No"]) is None
    assert best_option("Kubernetes", ["Male", "Female"]) is None


def test_best_option_empty_list() -> None:
    assert best_option("Yes", []) is None


def test_yes_does_not_match_a_no_option() -> None:
    """The failure that would answer a sponsorship question backwards."""
    assert best_option("No", ["Yes, I require sponsorship"]) is None


# --------------------------------------------------------------------------
# value shaping
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "field_type", "expected"),
    [
        ("(555) 123-4567", "tel", "5551234567"),
        ("+1 555-123-4567", "tel", "15551234567"),
        ("linkedin.com/in/jane", "url", "https://linkedin.com/in/jane"),
        ("https://github.com/jane", "url", "https://github.com/jane"),
        ("  Jane  ", "text", "Jane"),
        ("2 years", "number", "2"),
    ],
)
def test_normalize_for_type(value: str, field_type: str, expected: str) -> None:
    assert normalize_for_type(value, field_type) == expected


# --------------------------------------------------------------------------
# verification tolerance
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("expected", "actual", "field_type"),
    [
        # Forms reformat what we type; that is not a failure.
        ("5551234567", "(555) 123-4567", "tel"),
        ("https://github.com/jane", "github.com/jane", "url"),
        ("Jane", "  Jane ", "text"),
        # A select shows a longer display label.
        ("Yes", "Yes, I am authorized", "select"),
        ("California", "US-California", "select"),
    ],
)
def test_values_equivalent_tolerates_reformatting(
    expected: str, actual: str, field_type: str
) -> None:
    assert values_equivalent(expected, actual, field_type)


@pytest.mark.parametrize(
    ("expected", "actual", "field_type"),
    [
        # The failure this exists to catch: the write silently no-opped.
        ("Jane", "", "text"),
        ("5551234567", "", "tel"),
        ("Yes", "No", "select"),
        ("Jane", "Bob", "text"),
    ],
)
def test_values_equivalent_catches_real_mismatches(
    expected: str, actual: str, field_type: str
) -> None:
    assert not values_equivalent(expected, actual, field_type)


def test_empty_expected_requires_empty_actual() -> None:
    assert values_equivalent("", "", "text")
    assert not values_equivalent("", "leftover", "text")


# --------------------------------------------------------------------------
# learning from a form filled by hand
# --------------------------------------------------------------------------


def test_read_value_reports_checked_state_not_the_choice() -> None:
    """Documents why learning cannot use read_value for a group.

    read_value exists to verify a write landed, so a ticked box reads back as
    "checked". Cached as the answer to "Preferred Work Location" that is worse
    than learning nothing — best_option would fuzzy-match "checked" onto some
    arbitrary option on the next form.
    """
    assert best_option("checked", ["San Francisco HQ", "New York City Office"]) in (
        None, "San Francisco HQ", "New York City Office",
    )
    # The point: "checked" carries no information about which was chosen.
    assert best_option("San Francisco HQ",
                       ["San Francisco HQ", "New York City Office"]) == "San Francisco HQ"


@pytest.mark.parametrize("junk", ["checked", "on", "ON", "true", " false "])
def test_cache_refuses_browser_artefacts(junk: str) -> None:
    """"checked" is what read_value reports for a ticked box; "on" is a
    checkbox's default value attribute. Both were learned as real answers."""
    from job_agent.resolve import cache

    cache.remember("Preferred Work Location", junk, "artefact-test", "checkbox")
    assert cache.lookup("Preferred Work Location", "artefact-test") is None


def test_cache_keeps_a_real_option() -> None:
    from job_agent.resolve import cache

    cache.remember("Preferred Work Location", "San Francisco HQ",
                   "artefact-test-2", "checkbox")
    found = cache.lookup("Preferred Work Location", "artefact-test-2")
    assert found and found.answer == "San Francisco HQ"


@pytest.mark.parametrize("junk", [
    "5ba59b87-591b-4860-90aa-560d63ab12cd",       # Ashby option UUID
    "847C4F54-C9B3-42D8-B0B2-04A749FE0011",       # case-insensitive
    "d38ee6d90269a45be9d38ee6d90269a4",           # long opaque hex token
])
def test_cache_refuses_machine_ids(junk: str) -> None:
    """A Notion/Ashby form surfaced option UUIDs as option text, and taps
    cached raw ids as the answers to real questions — including work
    authorization. No human answer looks like a UUID."""
    from job_agent.resolve import cache

    cache.remember("Do you have experience with LLMs?", junk, "uuid-test", "radio")
    assert cache.lookup("Do you have experience with LLMs?", "uuid-test") is None


def test_cache_keeps_short_hexish_real_answers() -> None:
    """"cafe" or "2021" must not be swept up by the machine-id guard."""
    from job_agent.resolve import cache

    cache.remember("Favorite word?", "cafe", "uuid-test-2", "text")
    found = cache.lookup("Favorite word?", "uuid-test-2")
    assert found and found.answer == "cafe"


def test_cache_refuses_ambiguous_date_labels() -> None:
    """"Start Date" names both the month and the year select; a cache entry
    under it answers both halves with whichever was written last. This
    resurfaced twice — via the hand-fill capture and via learn() after an
    ordinary fill — so it is refused at the cache itself."""
    from job_agent.resolve import cache

    for label in ("Start Date", "End Date", "start date", "Graduation Date"):
        cache.remember(label, "2021", "date-test", "select")
        assert cache.lookup(label, "date-test") is None
