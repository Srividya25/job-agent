"""Read the form back and compare against what we meant to write.

A write that silently does nothing is the single most common failure mode in
form automation — React reverts the value, a combobox never committed, a field
was disabled. None of it raises. Without reading back, the agent reports
success on an empty form.

ADR-004 makes this a gate: any mismatch means the application is left filled
but unsubmitted, for review.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from playwright.async_api import Page

from ..resolve.engine import Answer

_WS = re.compile(r"\s+")


@dataclass
class Mismatch:
    ref: str
    label: str
    expected: str
    actual: str
    reason: str


@dataclass
class VerifyReport:
    checked: int = 0
    matched: int = 0
    mismatches: list[Mismatch] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.mismatches

    def summary(self) -> str:
        return (
            f"{self.matched}/{self.checked} verified"
            + (f", {len(self.mismatches)} mismatched" if self.mismatches else "")
        )


def values_equivalent(expected: str, actual: str, field_type: str) -> bool:
    """Compare tolerantly — the form is allowed to reformat what we typed.

    A phone typed as 5551234567 legitimately reads back as (555) 123-4567,
    and a combobox shows a display label rather than the raw value. Treating
    those as failures would bury the real ones.
    """
    exp = _WS.sub(" ", expected.strip().lower())
    act = _WS.sub(" ", actual.strip().lower())

    if exp == act:
        return True
    if not exp:
        return not act
    # An empty field is never a match for a non-empty intent. This has to be
    # checked before the containment test below, where "" is a substring of
    # everything — which would pass a completely blank form as verified, the
    # exact failure this module exists to detect.
    if not act:
        return False

    if field_type in {"tel", "number"}:
        return re.sub(r"\D", "", exp) == re.sub(r"\D", "", act)

    if field_type == "url":
        strip = lambda s: s.removeprefix("https://").removeprefix("http://").rstrip("/")
        return strip(exp) == strip(act)

    # Phone widgets render a country picker that displays the dialing code
    # rather than the country name, so selecting "United States" correctly
    # reads back as "+1". Without this, every form with an international
    # phone field reports a permanent mismatch and can never clear the gate.
    # Not gated on field_type: the extractor sees React-Select's underlying
    # <input> and types it "text", so a combobox never announces itself here.
    if re.fullmatch(r"\+\d{1,4}", act):
        return True

    # Comboboxes and selects display a label that contains what we asked for.
    return exp in act or act in exp


# React-Select and similar components clear the text input once a choice is
# made and render the selection in a sibling element. Reading input.value then
# returns "" for a field that is correctly filled, which would report every
# dropdown as a failure. This walks up to the component container and reads
# whatever it is displaying.
_READ_DISPLAYED = """
el => {
  if (el.value) return el.value;

  // Only a genuine select container counts. Climbing to an arbitrary
  // ancestor scrapes whatever widget happens to sit next door — a phone
  // country-code picker showing "+1" was read as the Country answer.
  const container =
    el.closest('[class*="select__control"]') ||
    el.closest('[class*="select-shell"]');
  if (!container) return null;

  const rendered = container.querySelector(
    '[class*="single-value"], [class*="multi-value__label"]'
  );
  if (rendered) return rendered.textContent.trim();

  // Container found but nothing rendered: genuinely empty.
  return container.querySelector('[class*="placeholder"]') ? '' : null;
}
"""


async def read_answer(root, field) -> str:
    """What a control currently says, in a form worth remembering.

    Groups report the ticked options' text, selects the chosen option's text,
    everything else its value — the shared reader for learning from a form a
    person finished by hand.
    """
    sep = " | "
    def join(values):
        return sep.join(dict.fromkeys(v.strip() for v in values if v.strip()))

    if field.type in {"radio", "checkbox"}:
        return join(await read_checked_options(root, field.ref))
    if field.type == "select":
        return join(await read_selected_options(root, field.ref))
    try:
        value = await read_value(root, field.ref, field.type)
    except Exception:  # noqa: BLE001 - a missing control is not fatal
        return ""
    return (value or "").strip()


async def read_checked_options(page: Page, ref: str) -> list[str]:
    """The visible text of every ticked option in a radio/checkbox group.

    read_value() answers "is this control checked" because that is what
    verifying a write needs. Learning needs the opposite: *which* option was
    chosen. Using read_value here cached "checked" as the answer to
    "Preferred Work Location", which is worse than not learning at all —
    best_option() would then fuzzy-match it onto some arbitrary choice.
    """
    try:
        return await page.eval_on_selector_all(
            ref,
            # "on" is the browser's default value for a checkbox with no value
            # attribute, so it must not be mistaken for the option's text —
            # four answers were learned as "on". Ashby puts the option text in
            # `name`, which is the useful fallback.
            """els => els.filter(e => e.checked).map(e => {
                 const label = e.labels && e.labels[0];
                 const text = label && label.innerText.trim();
                 if (text) return text;
                 const value = (e.value || '').trim();
                 if (value && value.toLowerCase() !== 'on') return value;
                 return (e.name || '').trim();
               }).filter(Boolean)""",
        )
    except Exception:  # noqa: BLE001 - unreadable is reported, not raised
        return []


async def read_selected_options(page: Page, ref: str) -> list[str]:
    """The visible text of every selected option in a <select>.

    input_value() returns the option's *value attribute*, which is what
    verifying a write needs but not what a person chose: a month dropdown
    written as <option value="5">May</option> reads back as "5", and caching
    that would put "5" into the next form's month field. Handles <select
    multiple> too, where several options can be chosen at once.
    """
    try:
        return await page.eval_on_selector_all(
            ref,
            """els => els.flatMap(el =>
                 Array.from(el.selectedOptions || []).map(o =>
                   (o.label || o.textContent || '').trim()
                 )
               ).filter(t => t && !/^(select|choose|month|year|day)\\b/i.test(t))""",
        )
    except Exception:  # noqa: BLE001 - unreadable is reported, not raised
        return []


async def read_value(page: Page, ref: str, field_type: str) -> str | None:
    """Current value of a control, or None if it cannot be read."""
    locator = page.locator(ref).first
    try:
        if await locator.count() == 0:
            return None
        if field_type in {"radio", "checkbox"}:
            return "checked" if await locator.is_checked() else ""
        if field_type == "file":
            # File inputs never expose their path to script for security
            # reasons; presence of a filename is the most that can be checked.
            return await locator.evaluate(
                "el => el.files && el.files.length ? el.files[0].name : ''"
            )

        value = await locator.input_value()
        if value:
            return value
        # Empty may mean "not filled" or "filled, displayed elsewhere".
        return await locator.evaluate(_READ_DISPLAYED)
    except Exception:  # noqa: BLE001 - unreadable is reported, not raised
        return None


async def verify(page: Page, answers: list[Answer]) -> VerifyReport:
    report = VerifyReport()

    for answer in answers:
        actual = await read_value(page, answer.ref, answer.field_type)

        if actual is None:
            report.skipped.append(answer.label)
            continue

        report.checked += 1

        if answer.field_type == "file":
            # Any filename present means the upload took.
            if actual:
                report.matched += 1
            else:
                report.mismatches.append(
                    Mismatch(answer.ref, answer.label, answer.value, "",
                             "file did not attach")
                )
            continue

        if answer.field_type in {"radio", "checkbox"}:
            if actual == "checked":
                report.matched += 1
            else:
                report.mismatches.append(
                    Mismatch(answer.ref, answer.label, answer.value, actual,
                             "not checked")
                )
            continue

        if values_equivalent(answer.value, actual, answer.field_type):
            report.matched += 1
        else:
            report.mismatches.append(
                Mismatch(answer.ref, answer.label, answer.value, actual,
                         "value did not stick")
            )

    return report
