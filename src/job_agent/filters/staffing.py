"""Detect staffing agencies, body shops and IT consultancies.

This runs BEFORE the sponsorship check on purpose. The largest H-1B sponsors
by volume are precisely the consultancies most candidates want to avoid, so
filtering on sponsorship first would surface mostly the wrong companies.

Signals are weighted and combined rather than treated as a single rule,
because any one of them alone produces false positives on real employers.
"""

from __future__ import annotations

import re

from ..models import StaffingSignal, normalize_company

# --------------------------------------------------------------------------
# Job-description phrases. These are the strongest signals available: a
# product company describing its own opening never refers to "our client".
# --------------------------------------------------------------------------

_JD_DEFINITIVE = [
    (r"\bour client\b", 0.95, "JD says 'our client'"),
    (r"\bclient is (seeking|looking)\b", 0.95, "JD says 'client is seeking'"),
    (r"\bon behalf of (our|a) client\b", 0.95, "JD applies on behalf of a client"),
    (r"\bc2c\b|\bcorp[\s-]?to[\s-]?corp\b", 0.90, "mentions corp-to-corp"),
    (r"\bw2 only\b|\bonly w2\b", 0.90, "mentions 'W2 only'"),
    (r"\b1099\b", 0.70, "mentions 1099 contracting"),
]

_JD_MODERATE = [
    (r"\bcontract[\s-]?to[\s-]?hire\b|\bc2h\b", 0.55, "contract-to-hire role"),
    (r"\brate\s*[:\-]?\s*\$\s*\d+\s*(/|per\s*)?(hr|hour)", 0.60, "quotes an hourly rate"),
    (r"\bsubmit (your )?resumes?\b", 0.40, "asks to 'submit resumes'"),
    (r"\bbench\b.{0,30}\b(candidates?|consultants?)\b", 0.80, "mentions bench candidates"),
    (r"\b(gc|green card|h1b|h-1b)\s*(holders?|only|transfer)", 0.55, "filters by visa class"),
    (r"@(gmail|yahoo|hotmail|outlook)\.com", 0.50, "recruiter uses a personal email"),
]

# --------------------------------------------------------------------------
# Company-name patterns.
#
# STRONG words are used almost exclusively by staffing firms.
# WEAK words ("Solutions", "Technologies", "Group") appear in plenty of real
# product companies, so they only count when two or more co-occur.
# --------------------------------------------------------------------------

_NAME_STRONG = re.compile(
    r"\b(staffing|recruit(ing|ment|ers)?|placements?|manpower|talent\s+"
    r"(solutions|acquisition|group)|consultancy|consultants|it\s+services|"
    r"outsourc(ing|e)|workforce|resourcing|headhunt)",
    re.I,
)

_NAME_WEAK = re.compile(
    r"\b(solutions|technologies|systems|infotech|softtech|softech|consulting|"
    r"resources|global|enterprises|informatics|infosystems)\b",
    re.I,
)

# --------------------------------------------------------------------------
# Known firms. Seeded from the large consultancies and the well-known
# staffing tier; extend freely — matching is on the normalized name.
# --------------------------------------------------------------------------

_KNOWN_FIRMS = {
    # global consultancies / outsourcers
    "infosys", "tata consultancy services", "tcs", "wipro", "cognizant",
    "hcl", "hcl america", "hcltech", "tech mahindra", "capgemini",
    "ltimindtree", "l t infotech", "mindtree", "mphasis", "virtusa",
    "syntel", "ust", "ust global", "persistent", "zensar", "hexaware",
    "birlasoft", "cybage", "iGate", "nagarro", "coforge", "niit",
    "accenture", "deloitte", "cognizant technology",
    # staffing / body shops
    "diverse lynx", "mindlance", "artech", "collabera", "eteam",
    "nityo infotech", "compunnel", "photon", "photon interactive",
    "v soft", "v soft consulting", "sunrise systems", "judge group",
    "kellton", "softpath", "sgs consulting", "aditi", "aditi consulting",
    "apex systems", "insight global", "teksystems", "robert half",
    "randstad", "kforce", "motion recruitment", "beacon hill",
    "signature consultants", "modis", "adecco", "manpowergroup",
    "experis", "cybercoders", "jobot", "dice", "akraya", "intelliswift",
    "amtex", "ampcus", "prokarma", "saxon global", "tekshapers",
    "vdart", "infojini", "kforce inc", "pyramid consulting",
    "us tech solutions", "insys", "genuent", "talentburst",
}


def _normalized_known() -> set[str]:
    return {normalize_company(n) for n in _KNOWN_FIRMS}


_KNOWN_NORMALIZED = _normalized_known()


def detect_staffing(
    company_name: str,
    description: str | None = None,
    threshold: float = 0.70,
) -> StaffingSignal:
    """Score how likely this employer is a staffing firm rather than the hirer."""
    reasons: list[str] = []
    scores: list[float] = []

    normalized = normalize_company(company_name)

    # 1. known firm — decisive
    if normalized in _KNOWN_NORMALIZED:
        return StaffingSignal(
            matched=True,
            confidence=1.0,
            reasons=[f"'{company_name}' is a known staffing/consulting firm"],
        )

    # 2. name patterns
    if m := _NAME_STRONG.search(company_name):
        scores.append(0.85)
        reasons.append(f"name contains '{m.group(0)}'")

    weak_hits = _NAME_WEAK.findall(company_name)
    if len(weak_hits) >= 2:
        scores.append(0.45)
        reasons.append(f"name stacks generic words: {', '.join(weak_hits[:3])}")

    # 3. job-description phrases
    jd = (description or "").lower()
    if jd:
        for pattern, weight, why in (*_JD_DEFINITIVE, *_JD_MODERATE):
            if re.search(pattern, jd, re.I):
                scores.append(weight)
                reasons.append(why)

    if not scores:
        return StaffingSignal(matched=False, confidence=0.0, reasons=[])

    # Combine as noisy-OR: independent weak signals reinforce each other
    # without any single one being able to exceed certainty.
    combined = 1.0
    for s in scores:
        combined *= 1.0 - s
    confidence = round(1.0 - combined, 3)

    return StaffingSignal(
        matched=confidence >= threshold,
        confidence=confidence,
        reasons=reasons[:5],
    )
