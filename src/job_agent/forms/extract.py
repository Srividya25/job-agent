"""Turn any application form into a normalized list of fields.

This is the file that replaces every `fill_workday()` / `fill_eightfold()`
function in the old implementation. It knows nothing about any specific ATS:
it walks the form controls and works out what each one is *called*, using the
same cascade a sighted human uses.

It deliberately operates on HTML rather than a live browser object, so the
identical code path runs against a saved fixture in tests and against
`page.content()` from a live tab in Phase 3. One implementation, one set of
tests, no drift.
"""

from __future__ import annotations

import re
from typing import Literal

from bs4 import BeautifulSoup, Tag
from pydantic import BaseModel, Field

FieldType = Literal[
    "text", "email", "tel", "url", "number", "date",
    "textarea", "select", "radio", "checkbox", "file", "hidden",
]

# Controls that are never worth filling.
_SKIP_TYPES = {"submit", "button", "reset", "image"}

# Inputs that belong to a widget rather than to the application. They are real
# <input> elements, so the extractor picked them up, took whatever text sat
# nearest as a label, and then reported them as required questions nobody
# could answer — a Stripe application parked on "Submit application"
# (reCAPTCHA's response textarea) and "iti search input" (the phone widget's
# country search) instead of being filled.
_WIDGET_INTERNALS = re.compile(
    r"g-recaptcha-response|h-captcha-response|cf-turnstile"
    r"|^iti-|__search-input|^select2-|^react-select-",
    re.I,
)


def _is_widget_internal(element: Tag) -> bool:
    identifier = f"{element.get('id') or ''} {element.get('name') or ''}"
    if _WIDGET_INTERNALS.search(identifier):
        return True
    # A nameless, id-less input inside a phone widget is the widget's own
    # country-code picker, not a question. Stripe's Greenhouse form grew a
    # second "Phone" field this way, and clicking it hung a whole fill.
    if not element.get("id") and not element.get("name"):
        for parent in element.parents:
            if not isinstance(parent, Tag):
                continue
            if parent.name == "form":
                break
            classes = " ".join(parent.get("class") or [])
            if "phone-input" in classes or classes.startswith("iti"):
                return True
    return False
_WS = re.compile(r"\s+")
_REQUIRED_MARK = re.compile(r"\s*[*✱]\s*$|\s*\(required\)\s*$", re.I)


class FormField(BaseModel):
    """One fillable control, described in portal-agnostic terms."""

    ref: str                       # CSS selector that finds this control again
    label: str                     # effective, human-readable label
    type: FieldType
    name: str | None = None
    required: bool = False
    options: list[str] = Field(default_factory=list)
    value: str = ""
    group: str | None = None       # radio/checkbox group name
    question: str = ""             # untruncated label, for asking a human

    @property
    def is_choice(self) -> bool:
        return self.type in {"select", "radio", "checkbox"}


# --------------------------------------------------------------------------
# label resolution
# --------------------------------------------------------------------------


def _clean(text: str | None) -> str:
    if not text:
        return ""
    return _WS.sub(" ", text).strip()


# Text that sits inside a label element but is not part of the label: inline
# validation messages, upload hints, help text. Real forms are full of it, and
# without this a label reads "Current location ✱ No location found. Try again".
_NOISE = re.compile(
    r"(couldn'?t|could not|no .{0,20}found|try (again|entering)|please .{0,30}|"
    r"attach|upload|drag and drop|or\s+enter\s+manually|must be|maximum|"
    r"accepted formats?|\.(pdf|docx?)\b|optional\b|we accept)",
    re.I,
)

# Placeholder prompts that are not labels at all. "Search schools..." and
# "Start typing..." matter: Ashby's typeahead inputs carry no id, name or
# aria-label, so the placeholder was the only text found and became the
# label — which no rule could match and no human could answer. Rejecting it
# here lets resolve_label fall through to the question card, where the real
# label ("Education History", "Location") lives.
_PLACEHOLDER_ONLY = re.compile(
    r"^(select\.*|choose\.*|--+|write here\.*|type here\.*|search\.*|"
    r"search\s+\w+\.*|start typing\.*|type to search\.*|"
    # Lever's free-text cards are labelled "Type your response" — a prompt,
    # not a question, and it was reported as a required field.
    r"type your (response|answer)\.*|your (response|answer)\.*|"
    r"enter\s+\w+\.*|none|n/?a|"
    # File-upload button captions. Greenhouse renders "Attach" / "Dropbox" /
    # "Google Drive" as the only text near the input, which would name both
    # the resume and the cover-letter field "Attach".
    r"attach|upload|browse|add file|choose file|dropbox|google drive)$",
    re.I,
)


def _normalize_label(text: str, cap: bool = True) -> str:
    """Trim a raw label down to the question a person would read.

    Cuts at the required marker, drops trailing help/validation text, and
    caps length — a 200-character "label" is page copy, not a field name,
    and would poison the answer cache with un-matchable keys.

    `cap=False` keeps the full sentence. The cap is right for a cache key and
    wrong for asking a human: a question truncated to "how would you rate
    Plaid's position in AI compared to…" cannot be answered by anyone.
    """
    label = _clean(text)
    if not label:
        return ""

    # The required marker reliably terminates the label proper.
    label = re.split(r"[✱*]", label, maxsplit=1)[0].strip() or label

    # Drop the tail once noise begins.
    if match := _NOISE.search(label):
        head = label[: match.start()].strip(" -–—:,.")
        if len(head) >= 3:
            label = head

    label = _REQUIRED_MARK.sub("", label).strip(" -–—:,")
    if cap and len(label) > 90:
        label = label[:90].rsplit(" ", 1)[0] + "…"

    return "" if _PLACEHOLDER_ONLY.match(label) else label


def _label_from_for(soup: BeautifulSoup, element: Tag) -> str:
    element_id = element.get("id")
    if not element_id:
        return ""
    for label in soup.find_all("label"):
        if label.get("for") == element_id:
            return _clean(label.get_text(" "))
    return ""


def _label_from_wrapper(element: Tag) -> str:
    """A <label> ancestor, with any nested control text removed."""
    for parent in element.parents:
        if not isinstance(parent, Tag):
            continue
        if parent.name == "label":
            clone = BeautifulSoup(str(parent), "lxml").find("label")
            if clone:
                for control in clone.find_all(["input", "select", "textarea"]):
                    control.decompose()
                return _clean(clone.get_text(" "))
        if parent.name == "form":
            break
    return ""


def _label_from_aria(soup: BeautifulSoup, element: Tag) -> str:
    if aria := _clean(element.get("aria-label")):
        return aria
    if ref := element.get("aria-labelledby"):
        parts = [
            _clean(target.get_text(" "))
            for token in ref.split()
            if (target := soup.find(id=token))
        ]
        if joined := _clean(" ".join(parts)):
            return joined
    return ""


def _label_from_nearby(element: Tag) -> str:
    """Closest preceding text: a heading, legend, or labelled sibling div.

    Workday and other component frameworks routinely render a <div> caption
    next to an unlabelled input, which is invisible to every other strategy.
    """
    for previous in element.find_all_previous(
        ["label", "legend", "h1", "h2", "h3", "h4", "h5", "h6", "p", "span", "div"],
        limit=6,
    ):
        if previous.find(["input", "select", "textarea"]):
            continue
        if text := _clean(previous.get_text(" ")):
            if 1 < len(text) <= 120:
                return text
    return ""


def _humanize(value: str) -> str:
    """'job_application_answers_attributes_0' -> 'job application answers'."""
    s = re.sub(r"[\[\]_\-.]+", " ", value)
    s = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", s)
    s = re.sub(r"\b\d+\b", " ", s)
    return _clean(s.lower())


def _fieldset_question(parent: Tag) -> str:
    """The question a fieldset asks, when it is not in a <legend>.

    Ashby writes the question as a <label> inside the fieldset and each
    option as another <label>. They are told apart by what they point at: an
    option's `for` resolves to an input inside the fieldset, the question's
    does not.
    """
    for label in parent.find_all("label"):
        target = label.get("for")
        if target and parent.find(id=target):
            continue  # labels one of the options
        raw = label.get_text(" ")
        # Returned raw: the caller normalizes, and only the caller knows
        # whether this is going into a cache key (capped) or to a human
        # (uncapped). Normalizing here truncated the question before the
        # uncapped path could ever see it.
        if _normalize_label(raw):
            return raw
    return ""


def _classes(tag: Tag) -> str:
    value = tag.get("class") or []
    return " ".join(value if isinstance(value, list) else [value])


# Containers that wrap one question and its options without using <fieldset>.
# Ashby wraps each question in a "_fieldEntry_" div whose question-title label
# points at the data-field-path, not at any input. Without matching it here the
# label fell through to "nearest preceding text", which named the immigration
# question after the previous card's last option — and the field vanished.
_QUESTION_CARD = re.compile(
    r"application-question|custom-question|form-question|field-?entry", re.I
)
_QUESTION_TEXT = re.compile(r"application-label|question-title|question-text", re.I)


def _card_question(parent: Tag) -> str:
    """The question a non-fieldset question card asks.

    Lever wraps each custom question in <li class="application-question"> with
    the text in <div class="application-label">, and never uses <fieldset>. So
    the group lookup found nothing, every strategy fell through, and the
    question was named by humanizing the input's `name` — producing
    "cards 18631c8a d2a4 41d9 ba8a 8fccf4193494 f" as a required question.

    A card's question names its control only when the card holds exactly one
    logical control. Ashby's education card holds six — school, degree,
    field, dates — under one "Education History" title, and using the card
    label there renamed all six to the same unanswerable thing. Yes/no
    widgets count as one: their two buttons wrap a single state checkbox.
    """
    controls = [
        c for c in parent.find_all(["input", "select", "textarea"])
        if c.get("type") not in ("hidden", "submit", "button")
    ]
    names = {c.get("name") or id(c) for c in controls}
    if len(names) > 1:
        return ""

    label = parent.find(class_=_QUESTION_TEXT)
    if not label:
        return ""
    # Drop any nested controls so option text does not join the question.
    clone = BeautifulSoup(str(label), "lxml")
    for control in clone.find_all(["input", "select", "textarea"]):
        control.decompose()
    return clone.get_text(" ")


def _group_label(soup: BeautifulSoup, element: Tag) -> str:
    """Label for a radio/checkbox *group*, not for one option.

    Without this, a pronouns group is named "He/him" — the text of whichever
    option happened to come first — which is useless as a cache key. On Ashby
    it produced questions like "Plaid's Mission" and "New York City Office",
    which are tick-boxes under "Why are you interested in working at Plaid?"
    and "Which office?" respectively.
    """
    for parent in element.parents:
        if not isinstance(parent, Tag):
            continue
        if parent.name == "fieldset":
            if legend := parent.find("legend"):
                raw = legend.get_text(" ")
                if _normalize_label(raw):
                    return raw
            if text := _fieldset_question(parent):
                return text
        if parent.get("role") in {"group", "radiogroup"}:
            raw = parent.get("aria-label") or ""
            if _normalize_label(raw):
                return raw
        if _QUESTION_CARD.search(_classes(parent)):
            raw = _card_question(parent)
            if _normalize_label(raw):
                return raw
        if parent.name == "form":
            break
    return ""


def _group_key(element: Tag) -> str | None:
    """Stable identity for the group a radio/checkbox belongs to.

    Grouping on the `name` attribute is the usual convention, but Ashby puts
    the *option text* in `name` — every tick-box in "Select all that apply"
    carries a different one — so name-based grouping produced one field per
    option. The enclosing fieldset is what actually defines the group.
    """
    for parent in element.parents:
        if not isinstance(parent, Tag):
            continue
        if parent.name == "form":
            break
        if path := parent.get("data-field-path"):
            return f"path:{path}"
        if parent.name == "fieldset" or parent.get("role") in {"group", "radiogroup"}:
            return f"fs:{parent.get('id') or id(parent)}"
    return None


def resolve_label(
    soup: BeautifulSoup, element: Tag, is_group: bool = False, cap: bool = True
) -> str:
    """Work out what this control is called, best source first."""
    if is_group:
        # The group's container comes first. Every <label for=…> pointing at
        # one of these inputs describes a single option, so consulting those
        # first names the question after whichever choice came first —
        # "Plaid's Mission" instead of "Why are you interested in working at
        # Plaid?". The comment here always said this; the order did not.
        strategies = [
            lambda: _group_label(soup, element),
            lambda: _label_from_aria(soup, element),
            lambda: _label_from_for(soup, element),
            lambda: _label_from_nearby(element),
            lambda: _humanize(element.get("name") or ""),
        ]
    else:
        strategies = [
            lambda: _label_from_aria(soup, element),
            lambda: _label_from_for(soup, element),
            lambda: _label_from_wrapper(element),
            lambda: _clean(element.get("placeholder")),
            lambda: _clean(element.get("title")),
            # A free-text field inside a question card belongs to that card's
            # question. Consulted before _label_from_nearby, which for Lever's
            # follow-up box grabbed the preceding option and labelled a
            # required field "No". Only unlabelled fields ever reach here.
            lambda: _group_label(soup, element),
            lambda: _label_from_nearby(element),
            lambda: _humanize(element.get("data-automation-id") or ""),
            lambda: _humanize(element.get("name") or ""),
            # Greenhouse gives file inputs id="resume" / id="cover_letter"
            # and no name, so the id is the last thing that identifies them.
            lambda: _humanize(element.get("id") or ""),
        ]

    for strategy in strategies:
        if label := _normalize_label(strategy(), cap=cap):
            return label
    return ""


# --------------------------------------------------------------------------
# selectors
# --------------------------------------------------------------------------


def _css_escape(value: str) -> str:
    return re.sub(r'(["\\])', r"\\\1", value)


def build_ref(element: Tag, tag_position: int) -> str:
    """A selector that will find this control again in a live DOM.

    Prefers id, then name, then a positional fallback. The positional form is
    fragile by nature, which is exactly why it is last.

    The fallback is Playwright's `:nth-match(tag, n)` — the n-th matching
    element in the whole page. The previous `tag:nth-of-type(n)` was doubly
    wrong: CSS nth-of-type counts within one *parent*, and n was a global
    count across inputs, selects and textareas together. It matched nothing,
    so every write to an id-less field (Ashby's school, location and date
    controls) failed with "element not found" — while the review showed the
    intended values as if they had been written.
    """
    if element_id := element.get("id"):
        return f'#{element_id}' if re.fullmatch(r"[A-Za-z][\w\-]*", element_id) \
            else f'[id="{_css_escape(element_id)}"]'
    if name := element.get("name"):
        return f'{element.name}[name="{_css_escape(name)}"]'
    return f":nth-match({element.name}, {tag_position})"


# --------------------------------------------------------------------------
# extraction
# --------------------------------------------------------------------------


def _field_type(element: Tag) -> FieldType | None:
    if element.name == "textarea":
        return "textarea"
    if element.name == "select":
        return "select"

    raw = (element.get("type") or "text").lower()
    if raw in _SKIP_TYPES:
        return None
    if raw in {"radio", "checkbox", "file", "email", "tel", "url", "number",
               "date", "hidden"}:
        return raw  # type: ignore[return-value]
    return "text"


def _button_options(element: Tag) -> list[str]:
    """Choices rendered as sibling <button>s of a hidden state checkbox.

    Ashby's required yes/no questions are two buttons plus an
    <input type="checkbox" tabindex="-1"> that only carries state. The
    checkbox was extracted with no options, filled by .check() — which
    Ashby's frontend never notices — and the submission failed with
    "Missing entry for required field" while everything looked filled.
    """
    parent = element.parent
    if not isinstance(parent, Tag):
        return []
    texts = [
        _clean(b.get_text(" "))
        for b in parent.find_all("button", recursive=False)
    ]
    return [t for t in texts if t and len(t) <= 40]


def _select_options(element: Tag) -> list[str]:
    options = []
    for option in element.find_all("option"):
        text = _clean(option.get_text(" "))
        # Skip the empty prompt row ("Select...", "--", "").
        if text and not re.fullmatch(r"[-–—]+|select\.*\.*|choose\.*", text, re.I):
            options.append(text)
    return options


def extract_fields(html: str, include_hidden: bool = False) -> list[FormField]:
    """Parse a form page into normalized fields.

    Radio and checkbox inputs sharing a `name` are collapsed into a single
    field whose `options` are the individual choices — that is how a person
    perceives them, and how the resolver needs to answer them.
    """
    soup = BeautifulSoup(html, "lxml")
    fields: list[FormField] = []
    groups: dict[str, FormField] = {}

    # Position of each element among elements of the SAME tag, counted over
    # everything in the document — skipped and hidden ones included — because
    # that is what Playwright's :nth-match counts against in the live DOM.
    tag_seen: dict[str, int] = {"input": 0, "select": 0, "textarea": 0}

    for element in soup.find_all(["input", "select", "textarea"]):
        tag_seen[element.name] += 1
        field_type = _field_type(element)
        if field_type is None:
            continue
        if field_type == "hidden" and not include_hidden:
            continue
        if _is_widget_internal(element):
            continue

        name = element.get("name")
        option_label = _clean(
            element.get("value")
            or _label_from_wrapper(element)
            # Ashby renders the option's text as a sibling <label for=id>,
            # which neither `value` nor the wrapper search reaches.
            or _label_from_for(soup, element)
        )

        # Collapse radios/checkboxes belonging to one question into a single
        # logical field. The enclosing fieldset wins over `name`, because a
        # form that names each option separately still asks one question.
        group_id = None
        if field_type in {"radio", "checkbox"}:
            group_id = _group_key(element) or name
            if group_id and (existing := groups.get(group_id)):
                if option_label and option_label not in existing.options:
                    existing.options.append(option_label)
                continue

        is_group = field_type in {"radio", "checkbox"}
        label = resolve_label(soup, element, is_group=is_group)
        question = resolve_label(soup, element, is_group=is_group, cap=False)

        # Drop validation shims. Component libraries (React-Select and
        # friends) render a hidden <input> beside the real control purely to
        # carry the `required` attribute. With no name, no id and no label it
        # cannot be submitted, cannot be referenced stably, and is not a
        # question anyone is being asked — it is a duplicate of the styled
        # control sitting next to it.
        if not label and not name and not element.get("id"):
            continue
        required = (
            element.has_attr("required")
            or element.get("aria-required") == "true"
            or bool(_REQUIRED_MARK.search(_label_from_for(soup, element)))
        )

        field = FormField(
            ref=build_ref(element, tag_seen[element.name]),
            label=label,
            type=field_type,
            name=name,
            required=required,
            options=_select_options(element) if field_type == "select" else (
                _button_options(element)
                or ([option_label] if option_label and
                    field_type in {"radio", "checkbox"} else [])
            ),
            value=_clean(element.get("value")) if field_type not in
            {"radio", "checkbox"} else "",
            question=question or label,
            group=group_id,
        )

        if group_id:
            groups[group_id] = field
        fields.append(field)

    return fields
