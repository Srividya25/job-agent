"""Tier 2: deterministic label -> profile value.

One table. The old implementation spread this logic across `ai_fill_field()`,
`EIGHTFOLD_VALUES`, `WORKDAY_VALUES` and four per-portal fill functions, so
changing an address meant four edits and the portals silently disagreed.

Patterns are ordered: the first match wins, so put the specific ones first.
"Last name" must be tested before "name", or every name field gets a surname.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from ..config import Profile
from ..forms.extract import FormField

Resolver = Callable[[Profile], str]

# (pattern, resolver) — first match wins.
RULES: list[tuple[re.Pattern[str], Resolver]] = [
    # --- name (specific before general) ---
    (re.compile(r"\b(last|family|sur)[\s_-]?name\b|\bsurname\b", re.I),
     lambda p: p.identity.last_name),
    (re.compile(r"\b(first|given|fore)[\s_-]?name\b", re.I),
     lambda p: p.identity.first_name),
    (re.compile(r"\bmiddle[\s_-]?(name|initial)\b", re.I), lambda p: ""),
    (re.compile(r"\b(full|legal|preferred)?[\s_-]?name\b", re.I),
     lambda p: p.identity.full_name),

    # --- contact ---
    (re.compile(r"\be-?mail\b", re.I), lambda p: p.identity.email),
    (re.compile(r"\b(phone|mobile|cell|telephone)\b", re.I),
     lambda p: p.identity.phone),

    # --- links (before generic 'website') ---
    (re.compile(r"\blinked-?in\b", re.I), lambda p: p.identity.linkedin),
    (re.compile(r"\bgit-?hub\b", re.I), lambda p: p.identity.github),
    (re.compile(r"\b(portfolio|personal (site|website)|website|blog)\b", re.I),
     lambda p: p.identity.portfolio),

    # --- address ---
    (re.compile(r"\b(address\s*(line\s*)?2|apt|suite|unit)\b", re.I),
     lambda p: p.address.line2),
    (re.compile(r"\b(street|address\s*(line\s*)?1?|mailing address)\b", re.I),
     lambda p: p.address.line1),
    (re.compile(r"\b(zip|postal)\s*code\b|\bpostcode\b", re.I),
     lambda p: p.address.postal_code),
    # "City" only counts when the field is asking where she lives. Plaid's
    # form offers "New York City Office" as a location choice, and the bare
    # \bcity\b match filled it with "San Jose" — a wrong answer that reads
    # perfectly plausibly in a log.
    (re.compile(r"^(?!.*\boffice\b)(?=.*\b(city|town)\b).*$", re.I),
     lambda p: p.address.city),
    (re.compile(r"\b(state|province|region)\b", re.I), lambda p: p.address.state),
    (re.compile(r"\bcountry\b", re.I), lambda p: p.address.country),
    # Same exclusion as the city rule: "Office location" is picking a site to
    # work at, not stating where she lives.
    (re.compile(r"^(?!.*\boffice\b)(?=.*\b(current )?location\b).*$", re.I),
     lambda p: f"{p.address.city}, {p.address.state}"),

    # --- education ---
    (re.compile(r"\b(university|college|school|institution)\b", re.I),
     lambda p: p.education[0].school if p.education else ""),
    (re.compile(r"\b(degree|qualification)\b", re.I),
     lambda p: p.education[0].degree if p.education else ""),
    (re.compile(r"\b(major|field of study|discipline)\b", re.I),
     lambda p: p.education[0].field if p.education else ""),
    (re.compile(r"\bgraduat\w*\s*(year|date)?\b", re.I),
     lambda p: str(p.education[0].graduation_year or "") if p.education else ""),

    # --- work ---
    (re.compile(r"\b(current )?(company|employer)\b", re.I),
     lambda p: ""),  # intentionally blank: varies, and guessing looks wrong
    (re.compile(r"\b(current )?(job )?title\b", re.I),
     lambda p: p.experience.current_title),
    (re.compile(r"\byears?\s+(of\s+)?experience\b", re.I),
     lambda p: str(int(p.experience.years))),

    # --- EEO (only answered when the profile supplies a value) ---
    (re.compile(r"\bgender\b", re.I), lambda p: p.eeo.gender),
    (re.compile(r"\b(race|ethnicity)\b", re.I), lambda p: p.eeo.race),
    (re.compile(r"\bveteran\b", re.I), lambda p: p.eeo.veteran_status),
    (re.compile(r"\bdisabilit", re.I), lambda p: p.eeo.disability_status),
]


# Work-authorization questions are answered by intent rather than text match,
# because the same fact is asked two opposite ways:
#   "Are you authorized to work in the US?"        -> yes
#   "Will you require visa sponsorship?"           -> yes (needs sponsorship)
_AUTHORIZED = re.compile(
    r"legally\s+(authoriz|entitled)|authoriz\w*\s+to\s+work|"
    r"eligible\s+to\s+work|right\s+to\s+work",
    re.I,
)
_SPONSORSHIP = re.compile(
    r"require\w*\s+(visa\s+)?sponsor|need\w*\s+sponsor|sponsorship\s+(now|in the future)|"
    r"will\s+you\s+(now\s+or\s+in\s+the\s+future\s+)?require",
    re.I,
)


def _work_authorization(label: str, profile: Profile) -> str | None:
    auth = profile.work_authorization
    # Order matters: "require sponsorship" often also contains "to work".
    if _SPONSORSHIP.search(label):
        return "Yes" if auth.needs_sponsorship else "No"
    if _AUTHORIZED.search(label):
        return "Yes" if auth.authorized_now else "No"
    return None


_MONTHS = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]
_MONTH_NAMES = {m.lower() for m in _MONTHS} | {m[:3].lower() for m in _MONTHS}
_YEAR = re.compile(r"^(19|20)\d{2}$")

_START_DATE = re.compile(r"\bstart\s*(date|month|year)?\b", re.I)
_END_DATE = re.compile(
    r"\b(end|completion|graduation)\s*(date|month|year)?\b|\bgraduat\w*", re.I
)


def _control_unit(field: FormField) -> str:
    """Whether a date control is asking for the month or the year.

    Forms split a date across two selects that share one label, so the label
    cannot tell them apart — "Start Date" appears twice. The options can: one
    holds month names, the other four-digit years. Without this both received
    the same answer and the year select was sent "May".
    """
    options = [o.strip() for o in field.options if o.strip()]
    if not options:
        return ""
    months = sum(1 for o in options if o.lower() in _MONTH_NAMES)
    years = sum(1 for o in options if _YEAR.match(o))
    if months >= 6:
        return "month"
    if years >= 6:
        return "year"
    return ""


def _education_date(field: FormField, profile: Profile) -> str | None:
    """Answer one half of an education start/end date."""
    if not profile.education:
        return None
    unit = _control_unit(field)
    if not unit:
        return None

    label = field.label
    entry = profile.education[0]
    if _START_DATE.search(label):
        value = entry.start_month if unit == "month" else entry.start_year
    elif _END_DATE.search(label):
        value = entry.end_month if unit == "month" else entry.graduation_year
    else:
        return None
    return str(value) if value else None


_RESUME = re.compile(r"\b(resume|cv|curriculum)\b", re.I)
_COVER_LETTER = re.compile(r"\bcover\s*letter\b", re.I)


def _file_upload(field: FormField, profile: Profile, resume_path: str | None) -> str | None:
    """File inputs take a path, not text.

    Cover letters are deliberately skipped: uploading a resume where a cover
    letter is asked for is worse than leaving it blank.
    """
    if field.type != "file":
        return None
    label = field.label
    if _COVER_LETTER.search(label):
        return ""
    if _RESUME.search(label) or not label:
        if resume_path:
            return resume_path
        return profile.resumes[0].path if profile.resumes else ""
    return None


# Bot traps. Greenhouse and Workday both ship a decoy input whose label tells
# a human to leave it alone; anything written there marks the application as
# automated. Salesforce's Workday form carries "Enter website. This input is
# for robots only, please leave blank" — and the `website` rule below matched
# it and filled in the portfolio URL, which would have flagged every such
# application while looking perfectly successful in the logs.
_HONEYPOT = re.compile(
    r"robots?\s+only|leave\s+(this\s+)?(field\s+)?(blank|empty)|"
    r"do\s+not\s+fill|don'?t\s+fill|if\s+you\s+are\s+human",
    re.I,
)


def resolve(
    field: FormField, profile: Profile, resume_path: str | None = None
) -> str | None:
    """Return a value for this field, or None if no rule applies."""
    label = field.label

    # Checked before everything, including the file-upload branch: a trap
    # named "resume" would otherwise be handed an actual resume.
    if label and _HONEYPOT.search(label):
        return ""

    if (upload := _file_upload(field, profile, resume_path)) is not None:
        return upload

    if not label:
        return None

    if (answer := _work_authorization(label, profile)) is not None:
        return answer

    # Before the table below: the generic `graduat\w*` rule there answers any
    # date field with the graduation year, which is the wrong half of a
    # month/year pair and the wrong end of a start date.
    if (answer := _education_date(field, profile)) is not None:
        return answer

    for pattern, resolver in RULES:
        if pattern.search(label):
            value = resolver(profile)
            # An empty result is still a decision — the rule matched and
            # determined the field should be left blank.
            return value

    return None
