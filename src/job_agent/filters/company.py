"""Company eligibility: allow / block / ask.

Order is deliberate. Staffing is checked first because the biggest H-1B
sponsors are consultancies — checking sponsorship first would rank exactly
the wrong companies highest.

The ASK verdict is not indecision, it is the safe default. A wrong BLOCK is
invisible and costs a real opportunity; a wrong ALLOW costs ten seconds of
review. When signals conflict or data is missing, ask.
"""

from __future__ import annotations

from datetime import datetime

from ..config import Profile
from ..models import ATS, Company, Verdict, normalize_company
from . import sponsorship, staffing


def evaluate(
    company_name: str,
    profile: Profile,
    description: str | None = None,
    ats: ATS = ATS.UNKNOWN,
    ats_slug: str | None = None,
    overrides: dict[str, Verdict] | None = None,
) -> Company:
    normalized = normalize_company(company_name)

    company = Company(
        name=company_name,
        normalized_name=normalized,
        ats=ats,
        ats_slug=ats_slug,
        checked_at=datetime.now(),
    )

    # 0. Manual overrides always win — this is the user's escape hatch.
    if overrides and normalized in overrides:
        company.verdict = overrides[normalized]
        company.verdict_reason = "manual override"
        return company

    # 1. Staffing / consulting
    company.staffing = staffing.detect_staffing(company_name, description)
    if company.staffing.matched:
        company.verdict = Verdict.BLOCK
        company.verdict_reason = "; ".join(company.staffing.reasons[:2])
        return company

    # 2. Sponsorship — only relevant if the user actually needs it
    company.sponsorship = sponsorship.assess(company_name, description)
    if profile.work_authorization.needs_sponsorship:
        if company.sponsorship.jd_blocks:
            company.verdict = Verdict.BLOCK
            company.verdict_reason = "; ".join(company.sponsorship.jd_blocks[:2])
            return company

        # No filing history and no explicit offer: unproven, not disproven.
        if company.sponsorship.score < 0.5:
            company.verdict = Verdict.ASK
            company.verdict_reason = (
                "no H-1B filing history on record"
                if sponsorship.dataset_available()
                else "H-1B dataset not downloaded (run: job-agent data h1b)"
            )
            company.legit_score = 0.5
            return company

    # 3. Legitimacy. Being on a paid ATS is a meaningful positive signal —
    #    Greenhouse/Lever/Ashby cost money and scammers do not buy them.
    company.legit_score = 0.85 if ats != ATS.UNKNOWN else 0.5

    # Weak staffing suspicion that did not clear the bar still warrants a look.
    if company.staffing.confidence >= 0.35:
        company.verdict = Verdict.ASK
        company.verdict_reason = (
            f"possible staffing firm ({company.staffing.confidence:.0%}): "
            + "; ".join(company.staffing.reasons[:2])
        )
        return company

    company.verdict = Verdict.ALLOW
    company.verdict_reason = _allow_reason(company, profile)
    return company


def _allow_reason(company: Company, profile: Profile) -> str:
    if not profile.work_authorization.needs_sponsorship:
        return "no staffing signals"
    if company.sponsorship.jd_confirms:
        return company.sponsorship.jd_confirms[0]
    if company.sponsorship.total_filings:
        return f"{company.sponsorship.total_filings} H-1B approvals on record"
    return "no staffing signals"
