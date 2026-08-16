"""Every control type, driven through a real browser.

The rest of the suite deliberately tests the pure decision logic. That leaves
the part that actually touches a form — `write_field` — unproven, and a form
is not only text boxes: it is native selects, radio groups, checkboxes,
textareas, and the styled ARIA comboboxes that Greenhouse and Workday use for
almost every dropdown.

These run against Playwright's own bundled Chromium, headless, on a data: URL.
They never touch the user's Chrome profile or any real application.
"""

from __future__ import annotations

import pytest

from job_agent.fill.verify import read_value
from job_agent.fill.writer import write_field
from job_agent.resolve.engine import Answer, Tier

playwright_api = pytest.importorskip("playwright.async_api")

# The project ships anyio, not pytest-asyncio.
pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


FORM = """
<!doctype html><html><body><form>
  <label for="first">First Name</label>
  <input id="first" name="first" type="text">

  <label for="email">Email</label>
  <input id="email" name="email" type="email">

  <label for="phone">Phone</label>
  <input id="phone" name="phone" type="tel">

  <label for="site">Website</label>
  <input id="site" name="site" type="url">

  <label for="years">Years of experience</label>
  <input id="years" name="years" type="number">

  <label for="why">Why do you want this job?</label>
  <textarea id="why" name="why"></textarea>

  <label for="state">State</label>
  <select id="state" name="state">
    <option value="">Select…</option>
    <option>US-California</option>
    <option>US-Texas</option>
  </select>

  <fieldset>
    <legend>Are you legally authorized to work in the United States?</legend>
    <label><input type="radio" name="auth" value="y"> Yes, I am authorized</label>
    <label><input type="radio" name="auth" value="n"> No, I am not</label>
  </fieldset>

  <label><input type="checkbox" id="ack" name="ack"> I acknowledge</label>

  <label for="school">University</label>
  <input id="school" role="combobox" autocomplete="off" type="text">
  <ul role="listbox" id="opts" hidden>
    <li role="option">Bayview State University</li>
    <li role="option">Santa Clara University</li>
  </ul>
  <script>
    const box = document.getElementById('school');
    const list = document.getElementById('opts');
    box.addEventListener('input', () => { list.hidden = false; });
    list.querySelectorAll('[role=option]').forEach(o =>
      o.addEventListener('click', () => { box.value = o.textContent; list.hidden = true; }));
  </script>
</form></body></html>
"""


def answer(ref: str, label: str, value: str, field_type: str) -> Answer:
    return Answer(
        ref=ref, label=label, value=value, tier=Tier.RULES, field_type=field_type
    )


@pytest.fixture
async def page():
    async with playwright_api.async_playwright() as pw:
        browser = await pw.chromium.launch()
        try:
            page = await browser.new_page()
            await page.set_content(FORM)
            yield page
        finally:
            await browser.close()


# --------------------------------------------------------------------------
# text-shaped controls
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ref", "label", "value", "field_type", "expected"),
    [
        ("#first", "First Name", "Jane", "text", "Jane"),
        ("#email", "Email", "a@b.com", "email", "a@b.com"),
        # tel is normalized to digits by normalize_for_type.
        ("#phone", "Phone", "555-010-0199", "tel", "5550100199"),
        ("#site", "Website", "janedoe.github.io", "url",
         "https://janedoe.github.io"),
        ("#years", "Years of experience", "2", "number", "2"),
        ("#why", "Why do you want this job?", "Because I build things.",
         "textarea", "Because I build things."),
    ],
)
async def test_text_shaped_controls(
    page, ref: str, label: str, value: str, field_type: str, expected: str
) -> None:
    result = await write_field(page, answer(ref, label, value, field_type), [])
    assert result.ok, result.detail
    assert await read_value(page, ref, field_type) == expected


# --------------------------------------------------------------------------
# dropdowns
# --------------------------------------------------------------------------


async def test_native_select(page) -> None:
    """A real <select>, chosen by option label rather than index."""
    result = await write_field(
        page,
        answer("#state", "State", "California", "select"),
        ["US-California", "US-Texas"],
    )
    assert result.ok, result.detail
    assert result.detail == "US-California"
    assert await page.locator("#state").input_value() == "US-California"


async def test_native_select_refuses_when_nothing_matches(page) -> None:
    """Better an empty field than the wrong option on a screening question."""
    result = await write_field(
        page,
        answer("#state", "State", "Bavaria", "select"),
        ["US-California", "US-Texas"],
    )
    assert not result.ok
    assert "no option matches" in result.detail
    assert await page.locator("#state").input_value() == ""


async def test_aria_combobox(page) -> None:
    """The styled dropdown Greenhouse and Workday use everywhere.

    It is an <input role=combobox>, so the extractor sees plain text and the
    writer has to notice the role at write time and pick from the listbox.
    """
    result = await write_field(
        page,
        answer("#school", "University", "Bayview State University", "text"),
        [],
    )
    assert result.ok, result.detail
    assert "combobox" in result.detail
    assert await page.locator("#school").input_value() == "Bayview State University"


# --------------------------------------------------------------------------
# radios and checkboxes
# --------------------------------------------------------------------------


async def test_radio_group_picks_by_visible_text(page) -> None:
    result = await write_field(
        page,
        answer('input[name="auth"]',
               "Are you legally authorized to work in the United States?",
               "Yes", "radio"),
        ["Yes, I am authorized", "No, I am not"],
    )
    assert result.ok, result.detail
    assert await page.locator("input[name=auth][value=y]").is_checked()
    assert not await page.locator("input[name=auth][value=n]").is_checked()


async def test_radio_picks_the_no_option_when_answer_is_no(page) -> None:
    """The sponsorship question is asked both ways round; No must mean No."""
    result = await write_field(
        page,
        answer('input[name="auth"]', "Authorized?", "No", "radio"),
        ["Yes, I am authorized", "No, I am not"],
    )
    assert result.ok, result.detail
    assert await page.locator("input[name=auth][value=n]").is_checked()


async def test_checkbox(page) -> None:
    result = await write_field(
        page, answer('input[name="ack"]', "I acknowledge", "Yes", "checkbox"), ["I acknowledge"]
    )
    assert result.ok, result.detail
    assert await page.locator("#ack").is_checked()


# --------------------------------------------------------------------------
# failure is reported, never silent
# --------------------------------------------------------------------------


async def test_missing_element_is_reported_not_raised(page) -> None:
    result = await write_field(
        page, answer("#nope", "Nothing", "x", "text"), []
    )
    assert not result.ok
    assert result.detail == "element not found"


async def test_missing_file_is_reported(page) -> None:
    result = await write_field(
        page, answer("#first", "Resume", "/no/such/resume.pdf", "file"), []
    )
    assert not result.ok
    assert "file missing" in result.detail


# --------------------------------------------------------------------------
# reading a form back, to learn from what she filled by hand
# --------------------------------------------------------------------------


CAPTURE_FORM = """
<!doctype html><html><body><form>
  <label for="co">Current Company</label>
  <input id="co" type="text" value="Progrite Systems">

  <label for="month">Start Month</label>
  <select id="month">
    <option value="">Month...</option>
    <option value="5" selected>May</option>
    <option value="8">August</option>
  </select>

  <label for="langs">Languages</label>
  <select id="langs" multiple>
    <option value="py" selected>Python</option>
    <option value="js">JavaScript</option>
    <option value="sql" selected>SQL</option>
  </select>

  <fieldset>
    <legend>Preferred Work Location</legend>
    <label><input type="checkbox" name="loc" value="sf" checked> San Francisco HQ</label>
    <label><input type="checkbox" name="loc" value="ny"> New York City Office</label>
    <label><input type="checkbox" name="loc" value="sea" checked> Seattle Office</label>
  </fieldset>

  <!-- No value attribute: the browser reports "on", which is not an answer. -->
  <fieldset>
    <legend>Interests</legend>
    <label><input type="checkbox" name="Plaid's Mission" checked> Plaid's Mission</label>
  </fieldset>
</form></body></html>
"""


@pytest.fixture
async def capture_page():
    async with playwright_api.async_playwright() as pw:
        browser = await pw.chromium.launch()
        try:
            page = await browser.new_page()
            await page.set_content(CAPTURE_FORM)
            yield page
        finally:
            await browser.close()


async def test_select_reads_back_its_visible_text_not_its_value(capture_page) -> None:
    """input_value() would give "5"; the answer is "May"."""
    from job_agent.fill.verify import read_selected_options

    assert await read_selected_options(capture_page, "#month") == ["May"]


async def test_multi_select_reads_every_chosen_option(capture_page) -> None:
    from job_agent.fill.verify import read_selected_options

    assert await read_selected_options(capture_page, "#langs") == ["Python", "SQL"]


async def test_select_placeholder_is_not_an_answer(capture_page) -> None:
    from job_agent.fill.verify import read_selected_options

    await capture_page.select_option("#month", value="")
    assert await read_selected_options(capture_page, "#month") == []


async def test_checkbox_group_reads_the_ticked_options(capture_page) -> None:
    from job_agent.fill.verify import read_checked_options

    chosen = await read_checked_options(capture_page, 'input[name="loc"]')
    assert chosen == ["San Francisco HQ", "Seattle Office"]


async def test_a_checkbox_without_a_value_falls_back_to_its_name(
    capture_page,
) -> None:
    """The browser reports "on" here; Ashby puts the option text in `name`."""
    from job_agent.fill.verify import read_checked_options

    chosen = await read_checked_options(
        capture_page, "input[name=\"Plaid's Mission\"]"
    )
    assert chosen == ["Plaid's Mission"]
    assert "on" not in [c.lower() for c in chosen]


async def test_text_input_reads_what_she_typed(capture_page) -> None:
    from job_agent.fill.verify import read_value

    assert await read_value(capture_page, "#co", "text") == "Progrite Systems"
