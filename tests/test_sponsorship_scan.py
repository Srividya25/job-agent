"""JD scanning for sponsorship, citizenship, and clearance requirements."""

from __future__ import annotations

import pytest

from job_agent.filters.sponsorship import scan_description


@pytest.mark.parametrize("text", [
    "We are unable to sponsor visas for this role.",
    "This position does not offer visa sponsorship.",
    "No sponsorship available.",
    "Applicants must be authorized to work without sponsorship.",
    "U.S. citizenship is required for this position.",
    "Must be a US citizen.",
    "US citizens only.",
    "Requires an active TS/SCI clearance.",
    "Candidates must hold a security clearance.",
    "This role is subject to ITAR requirements.",
])
def test_hard_requirements_block(text: str) -> None:
    blocks, _ = scan_description(text)
    assert blocks, text


@pytest.mark.parametrize("text", [
    "No security clearance required for this role.",
    "This position does not require a security clearance.",
    "You will work without a clearance on unclassified systems.",
])
def test_negated_clearance_does_not_block(text: str) -> None:
    """"No clearance required" is the opposite of a requirement.

    The old pattern blocked these, silently costing real opportunities —
    a wrong block is invisible.
    """
    blocks, _ = scan_description(text)
    assert blocks == [], text


def test_real_clearance_still_blocks_despite_other_negations() -> None:
    """A negation elsewhere must not launder an actual requirement."""
    text = (
        "An active Top Secret clearance is required. "
        "Relocation is not required."
    )
    blocks, _ = scan_description(text)
    assert blocks


@pytest.mark.parametrize("text", [
    "Visa sponsorship is available for this role.",
    "We sponsor H-1B visas.",
    "Willing to sponsor the right candidate.",
])
def test_sponsorship_offers_confirm(text: str) -> None:
    _, confirms = scan_description(text)
    assert confirms, text
