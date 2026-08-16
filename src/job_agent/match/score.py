"""Job <-> resume scoring.

Deliberately a composite of four visible parts rather than one opaque number.
A bare "87%" is not reviewable and cannot be calibrated; the components are,
and they are what let the threshold be tuned against real judgement.

    skills   0.40   explicit coverage of the skills you want to be hired for
    title    0.25   is this the job you asked for, at your level
    semantic 0.25   overall JD/resume similarity
    recency  0.10   freshly posted roles get a nudge

Hard gates (location, excluded titles) run before scoring and short-circuit.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

from rapidfuzz import fuzz

from ..config import Profile
from ..filters.experience import passes_experience_bar
from ..models import Job, MatchBreakdown, normalize_title
from .resume import ParsedResume, tokenize

# --------------------------------------------------------------------------
# components
# --------------------------------------------------------------------------


def score_skills(
    job_text: str, profile: Profile, resume: ParsedResume | None = None
) -> tuple[float, list[str], list[str]]:
    """Coverage of must-have skills, with nice-to-haves as a bonus.

    Must-haves dominate: having 3 of 3 required skills beats having 8
    peripheral ones, because the former is what gets you past a screen.

    When a resume is supplied, a skill only counts if it appears in the job
    AND in that resume. Without this the component is identical for every
    resume, and since recency and title barely vary either, resume choice
    collapses onto raw token overlap — whichever file happens to be longest
    wins, which is noise rather than matching.
    """
    text = job_text.lower()
    resume_text = resume.text.lower() if resume else None
    must = profile.skills.must_have
    nice = profile.skills.nice_to_have

    def present(skill: str) -> bool:
        s = skill.lower()
        # Word-ish containment; handles "React" in "ReactJS" and "Node.js".
        if s not in text:
            return False
        return resume_text is None or s in resume_text

    matched_must = [s for s in must if present(s)]
    matched_nice = [s for s in nice if present(s)]
    missing = [s for s in must if s not in matched_must]

    if not must:
        base = 1.0 if not nice else len(matched_nice) / len(nice)
    else:
        base = len(matched_must) / len(must)
        if nice:
            base = min(1.0, base + 0.15 * (len(matched_nice) / len(nice)))

    return round(base, 4), matched_must + matched_nice, missing


def score_title(title: str, profile: Profile, resume: ParsedResume) -> float:
    """Fuzzy match against this resume's target roles, then wanted titles.

    The resume's own target_roles dominate. Taking a plain max over the union
    lets a shared preference title match equally well for every resume, which
    erases the distinction this component exists to make: an ML resume should
    win an ML posting even though "Software Engineer" is also on the wanted
    list.
    """
    normalized = normalize_title(title)

    def best_of(candidates: list[str]) -> float:
        if not candidates:
            return 0.0
        return max(
            fuzz.token_set_ratio(normalized, normalize_title(c)) for c in candidates
        ) / 100.0

    targeted = best_of(resume.target_roles)
    wanted = best_of(profile.preferences.titles)

    if not resume.target_roles:
        return round(wanted or 0.5, 4)

    # A resume aimed at this role scores on that; otherwise it can still ride
    # the general preference list, but at a discount so it loses to a resume
    # that actually targets the posting.
    return round(max(targeted, wanted * 0.85), 4)


def score_semantic(job_text: str, resume: ParsedResume) -> float:
    """Overall similarity between the posting and the resume.

    Token-set cosine. Cheap, deterministic, explainable, and needs no model
    download — which keeps Phase 1 runnable with zero setup. An embedding
    backend can replace this without touching any caller.
    """
    job_tokens = tokenize(job_text)
    resume_tokens = resume.tokens
    if not job_tokens or not resume_tokens:
        return 0.0

    overlap = len(job_tokens & resume_tokens)
    denominator = math.sqrt(len(job_tokens) * len(resume_tokens))
    # Empirically this lands around 0.2-0.45 for genuine matches, so it is
    # rescaled to spread that band across most of 0..1.
    raw = overlap / denominator if denominator else 0.0
    return round(min(1.0, raw / 0.45), 4)


def score_recency(posted_at: datetime | None) -> float:
    """Fresh postings score higher; anything past ~60 days is likely stale."""
    if not posted_at:
        return 0.5
    now = datetime.now(UTC)
    when = posted_at if posted_at.tzinfo else posted_at.replace(tzinfo=UTC)
    days = max(0.0, (now - when).total_seconds() / 86400)
    if days <= 7:
        return 1.0
    if days >= 60:
        return 0.0
    return round(1.0 - (days - 7) / 53, 4)


# --------------------------------------------------------------------------
# hard gates
# --------------------------------------------------------------------------


def passes_gates(job: Job, profile: Profile) -> tuple[bool, str]:
    prefs = profile.preferences
    title_lower = job.title.lower()

    for excluded in prefs.exclude_titles:
        if excluded.lower() in title_lower:
            return False, f"excluded title: {excluded}"

    # Relevance gate: the queue exists for the roles she asked for, not for
    # everything her resume shares a few tokens with. Without this a
    # "Customer Experience Representative" rode a 14% score onto her list —
    # she asked for engineering roles only. Fuzzy, so "Sr Software
    # Development Engineer" still clears against "Software Engineer".
    if prefs.titles and prefs.min_title_match:
        normalized = normalize_title(job.title)
        best = max(
            fuzz.token_set_ratio(normalized, normalize_title(want))
            for want in prefs.titles
        ) / 100.0
        if best < prefs.min_title_match:
            return False, f"title unrelated to wanted roles ({best:.0%})"

    location = (job.location or "").lower()

    # Excluded locations are checked BEFORE the remote bypass. Postings like
    # "Canada - Remote (ON, AB, BC, or NS Only)" contain the word "remote"
    # but are not open to a US-based candidate, and would otherwise sail
    # through on the remote_ok flag.
    for excluded in prefs.exclude_locations:
        if excluded.lower() in location:
            return False, f"location excluded: {excluded}"

    ok, why = passes_experience_bar(job.description, prefs.max_years_required)
    if not ok:
        return False, why

    if prefs.locations and location:
        remote_ok = prefs.remote_ok and (
            "remote" in location or "remote" in title_lower
        )
        if not remote_ok and not any(
            want.lower() in location for want in prefs.locations
        ):
            return False, f"location not wanted: {job.location}"

    return True, ""


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------


def score_job(
    job: Job, profile: Profile, resumes: list[ParsedResume]
) -> tuple[float, MatchBreakdown | None, str | None]:
    """Score a job against every resume; the best one wins.

    Picking the best of N is a *relative* comparison, which is far more
    reliable than any absolute threshold — that part still needs calibrating.
    """
    ok, _ = passes_gates(job, profile)
    if not ok or not resumes:
        return 0.0, None, None

    job_text = f"{job.title}\n{job.description or ''}"

    best_score = -1.0
    best_breakdown: MatchBreakdown | None = None
    best_label: str | None = None

    for resume in resumes:
        # Recomputed per resume: a skill only counts if this resume shows it.
        skills, matched, missing = score_skills(job_text, profile, resume)
        breakdown = MatchBreakdown(
            skills=skills,
            title=score_title(job.title, profile, resume),
            semantic=score_semantic(job_text, resume),
            recency=score_recency(job.posted_at),
            matched_skills=matched,
            missing_skills=missing,
        )
        if breakdown.total > best_score:
            best_score = breakdown.total
            best_breakdown = breakdown
            best_label = resume.label

    return round(best_score, 4), best_breakdown, best_label
