"""Write resolved answers into a live form.

The decision logic — which option text to pick, how to normalize a value for a
given control — is pure and unit-tested. Only a thin layer actually touches
Playwright, because that layer is the part that cannot be tested without a
real browser.

Three things reliably break naive form filling, all handled here:

  1. React-controlled inputs ignore a raw value assignment. Playwright's
     fill() dispatches proper events; some components still need an explicit
     input/change dispatch afterwards.
  2. Workday, Greenhouse and Ashby render <div role="combobox"> rather than
     <select>. select_option() does not work on those at all.
  3. A write that silently no-ops is the most common failure, and is
     invisible without reading the value back.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from playwright.async_api import Locator, Page
from rapidfuzz import fuzz

from ..config import ROOT
from ..notify.telegram import MULTI_SEP
from ..resolve.engine import Answer

# Affirmative/negative synonyms, so a "Yes" answer still matches an option
# labelled "Yes, I am authorized" or "I agree".
_YES = re.compile(r"^\s*(yes|y|true|i (agree|acknowledge|consent|do)|agree)\b", re.I)
_NO = re.compile(r"^\s*(no|n|false|i do not|decline)\b", re.I)


@dataclass
class WriteResult:
    ref: str
    label: str
    intended: str
    ok: bool
    detail: str = ""


@dataclass
class FillReport:
    written: list[WriteResult] = field(default_factory=list)
    failed: list[WriteResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed

    def summary(self) -> str:
        return f"{len(self.written)} written, {len(self.failed)} failed"


# --------------------------------------------------------------------------
# pure decision logic
# --------------------------------------------------------------------------


def best_option(value: str, options: list[str]) -> str | None:
    """Pick the option that best expresses `value`.

    Exact match, then yes/no semantics, then fuzzy. Returns None rather than
    guessing when nothing is close — a wrong answer on a screening question
    is worse than an unanswered one.
    """
    if not options:
        return None

    wanted = value.strip().lower()
    for option in options:
        if option.strip().lower() == wanted:
            return option

    # Country names come in half a dozen equivalent spellings; a form
    # offering "USA" must accept a profile that says "United States".
    _US = {"us", "usa", "u.s.", "u.s.a.", "united states",
           "united states of america"}
    if wanted in _US:
        for option in options:
            if option.strip().lower().rstrip(".") in _US:
                return option

    if _YES.match(value):
        for option in options:
            if _YES.match(option):
                return option
    if _NO.match(value):
        for option in options:
            if _NO.match(option):
                return option

    # Substring containment beats fuzzy noise ("California" in "US-California").
    for option in options:
        if wanted and wanted in option.strip().lower():
            return option

    scored = [(fuzz.token_set_ratio(wanted, o.lower()), o) for o in options]
    score, best = max(scored)
    return best if score >= 80 else None


def selection_matches(intended: str, selected: str) -> bool:
    """Whether the option a combobox ended up on is the one we meant.

    "Anna University" was once selected while a completely different school
    was intended, verification read the text box instead of the selection,
    and a wrong school reached a real application — caught only by the
    user's eyes before submitting. A selection that shares no identifying
    words with the intent is a failure, whatever the widget claims.
    """
    if not selected.strip():
        return False
    a, b = intended.strip().lower(), selected.strip().lower()
    if a == b or a in b or b in a:
        return True
    if best_option(intended, [selected]) is not None:
        return True
    # Distinctive-token overlap: generic words prove nothing.
    generic = {"university", "college", "institute", "school", "of", "the",
               "state", "degree", "and", "&"}
    a_tokens = {t for t in re.split(r"\W+", a) if t and t not in generic}
    b_tokens = {t for t in re.split(r"\W+", b) if t and t not in generic}
    return bool(a_tokens & b_tokens)


def normalize_for_type(value: str, field_type: str) -> str:
    """Shape a value for the control receiving it."""
    if field_type == "tel":
        digits = re.sub(r"\D", "", value)
        return digits or value
    if field_type == "number":
        return re.sub(r"[^\d.\-]", "", value) or value
    if field_type == "url" and value and not value.startswith(("http://", "https://")):
        return f"https://{value}"
    return value.strip()


def resolve_upload_path(value: str) -> Path | None:
    """Turn a profile-relative resume path into something on disk."""
    if not value:
        return None
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = ROOT / candidate
    return candidate if candidate.exists() else None


# --------------------------------------------------------------------------
# playwright layer
# --------------------------------------------------------------------------


async def _is_combobox(locator: Locator) -> bool:
    """A styled dropdown that only looks like a select."""
    try:
        role = await locator.get_attribute("role")
        tag = await locator.evaluate("el => el.tagName.toLowerCase()")
    except Exception:  # noqa: BLE001 - detection must never abort a fill
        return False
    return role == "combobox" and tag != "select"


async def _dispatch_input_events(locator: Locator) -> None:
    """Nudge frameworks that missed Playwright's synthetic events."""
    await locator.evaluate(
        """el => {
             el.dispatchEvent(new Event('input',  {bubbles: true}));
             el.dispatchEvent(new Event('change', {bubbles: true}));
           }"""
    )


async def write_field(page: Page, answer: Answer, options: list[str]) -> WriteResult:
    result = WriteResult(
        ref=answer.ref, label=answer.label, intended=answer.value, ok=False
    )
    locator = page.locator(answer.ref).first

    try:
        if await locator.count() == 0:
            result.detail = "element not found"
            return result

        if answer.field_type == "file":
            path = resolve_upload_path(answer.value)
            if not path:
                result.detail = f"file missing: {answer.value}"
                return result
            await locator.set_input_files(str(path))
            result.ok = True
            result.detail = path.name
            return result

        if answer.field_type == "select":
            choice = best_option(answer.value, options)
            if choice is None:
                result.detail = f"no option matches {answer.value!r}"
                return result
            await locator.select_option(label=choice)
            result.ok = True
            result.detail = choice
            return result

        if answer.field_type in {"radio", "checkbox"}:
            # "Select all that apply" answers arrive joined; tick each one.
            # Treating the whole string as a single choice matched nothing and
            # left a multi-select question empty.
            wanted = [
                part.strip()
                for part in answer.value.split(MULTI_SEP)
                if part.strip()
            ] or [answer.value]

            ticked: list[str] = []
            for value in wanted:
                choice = best_option(value, options) or value
                # Match by the option's visible text rather than by index.
                option = page.get_by_role(
                    "radio" if answer.field_type == "radio" else "checkbox",
                    name=re.compile(re.escape(choice), re.I),
                ).first
                if await option.count():
                    await option.check()
                elif not await locator.is_visible():
                    # A hidden state-carrier (Ashby's yes/no widget): checking
                    # it changes the DOM but not the app's state, and the
                    # submission fails with "missing entry" afterwards. The
                    # visible controls are sibling buttons — click the one
                    # bearing the chosen text.
                    button = locator.locator("xpath=..").get_by_role(
                        "button", name=re.compile(rf"^{re.escape(choice)}$", re.I)
                    ).first
                    if await button.count():
                        # Never click an already-selected option. The
                        # extractor once produced two fields for one card,
                        # and the second click TOGGLED the answer back off —
                        # the server then rejected every submission with
                        # "missing entry" while the fill looked perfect.
                        pressed = await button.get_attribute("aria-pressed")
                        cls = (await button.get_attribute("class")) or ""
                        already = pressed == "true" or "selected" in cls.lower()                             or "active" in cls.lower()
                        if not already:
                            await button.click()
                    else:
                        await locator.check()
                else:
                    await locator.check()
                ticked.append(choice)
            result.ok = True
            result.detail = ", ".join(ticked)
            return result

        if await _is_combobox(locator):
            await locator.click(timeout=5000)
            # Some pickers show their whole list on click and typing FILTERS
            # it — Stripe's country question renders 31 options immediately,
            # and typing "United States" filtered all of them away. Match
            # against what is already on screen before touching the keyboard.
            try:
                await page.get_by_role("option").first.wait_for(
                    state="visible", timeout=1500
                )
                shown = [
                    t.strip()
                    for t in await page.get_by_role("option").all_text_contents()
                    if t.strip()
                ]
                if (pick := best_option(answer.value, shown)) is not None:
                    await page.get_by_role(
                        "option", name=re.compile(rf"^{re.escape(pick)}$", re.I)
                    ).first.click()
                    landed = ""
                    try:
                        landed = await locator.input_value()
                    except Exception:  # noqa: BLE001
                        pass
                    if landed and not selection_matches(answer.value, landed):
                        result.detail = (
                            f"combobox landed on {landed!r}, wanted {answer.value!r}"
                        )
                        return result
                    result.ok = True
                    result.detail = f"combobox (listed): {pick}"
                    return result
            except Exception:  # noqa: BLE001 - no pre-shown list; type instead
                pass
            await locator.type(answer.value, delay=25)

            # Location and school pickers query a server on each keystroke, so
            # the listbox is empty for a moment. Waiting for the role to appear
            # is what makes these succeed rather than reporting "no match".
            option = page.get_by_role(
                "option", name=re.compile(re.escape(answer.value), re.I)
            ).first
            try:
                await option.wait_for(state="visible", timeout=4000)
            except Exception:  # noqa: BLE001 - fall through to the retries below
                pass

            if await option.count():
                await option.click()
                landed = ""
                try:
                    landed = await locator.input_value()
                except Exception:  # noqa: BLE001
                    pass
                if landed and not selection_matches(answer.value, landed):
                    result.detail = f"combobox landed on {landed!r}, wanted {answer.value!r}"
                    return result
                result.ok = True
                result.detail = f"combobox: {answer.value}"
                return result

            # Some pickers only offer a broader match ("San Jose, CA, USA" for
            # "San Jose, California"); accept the first suggestion rather than
            # leaving a required field empty.
            any_option = page.get_by_role("option").first
            if await any_option.count():
                text = (await any_option.text_content() or "").strip()
                if best_option(answer.value, [text]):
                    await any_option.click()
                    result.ok = True
                    result.detail = f"combobox: {text}"
                    return result

            # The list may simply not contain the value's exact wording:
            # Greenhouse's degree picker has "Master's Degree" and no
            # "Master of Science". Retype just the first word and take the
            # closest of what is actually on offer — preferring the shortest
            # candidate, which is the generic form rather than a sibling
            # specialization (MBA also starts with "Master").
            first_word = answer.value.split()[0] if answer.value.split() else ""
            if len(first_word) >= 3:
                await locator.click()
                await locator.press("Meta+a")
                await locator.press("Backspace")
                await locator.type(first_word, delay=25)
                await page.wait_for_timeout(2500)
                options_now = [
                    t.strip()
                    for t in await page.get_by_role("option").all_text_contents()
                    if t.strip()
                ][:12]
                pick = best_option(answer.value, options_now)
                if pick is None:
                    starting = sorted(
                        (o for o in options_now
                         if o.lower().startswith(first_word.lower())),
                        key=len,
                    )
                    pick = starting[0] if starting else None
                if pick is not None:
                    await page.get_by_role(
                        "option", name=re.compile(re.escape(pick), re.I)
                    ).first.click()
                    landed = ""
                    try:
                        landed = await locator.input_value()
                    except Exception:  # noqa: BLE001
                        pass
                    if landed and not selection_matches(answer.value, landed):
                        result.detail = (
                            f"combobox landed on {landed!r}, wanted {answer.value!r}"
                        )
                        return result
                    result.ok = True
                    result.detail = f"combobox (nearest): {pick}"
                    return result

            result.detail = "combobox: no matching option"
            return result

        value = normalize_for_type(answer.value, answer.field_type)
        if answer.field_type == "tel":
            # Two dialects disagree: Greenhouse's #phone accepts a plain fill
            # (and clicking it can hang behind the intl-tel widget), while
            # Ashby's widget ignores fills and needs real keystrokes. Fill
            # first, read back, and only type if the value did not land.
            await locator.fill(value)
            await _dispatch_input_events(locator)
            landed = ""
            try:
                landed = await locator.input_value()
            except Exception:  # noqa: BLE001 - unreadable means not landed
                pass
            if not landed.strip():
                await locator.click(timeout=3000)
                await locator.press("Meta+a")
                await locator.press("Backspace")
                await locator.type(value, delay=30)
        else:
            await locator.fill(value)
        await _dispatch_input_events(locator)
        result.ok = True
        result.detail = value
        return result

    except Exception as exc:  # noqa: BLE001 - one bad field must not stop the form
        result.detail = f"{type(exc).__name__}: {exc}"[:120]
        return result


async def fill_form(
    page: Page, answers: list[Answer], options_by_ref: dict[str, list[str]]
) -> FillReport:
    report = FillReport()
    for answer in answers:
        result = await write_field(page, answer, options_by_ref.get(answer.ref, []))
        (report.written if result.ok else report.failed).append(result)
    return report
