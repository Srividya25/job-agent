"""Orchestrate one application: snapshot -> resolve -> fill -> verify.

Never submits. ADR-004 puts submission behind a six-condition gate that lands
in Phase 5; until then every application is left filled and unsubmitted for
review. The gate's hardest condition — every field resolved at tier <= 2 — is
already computable here, and is reported so the threshold can be calibrated
before anything is ever sent automatically.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from playwright.async_api import Page

from .config import Profile
from .fill.verify import VerifyReport, verify
from .fill.writer import FillReport, fill_form
from .forms.extract import FormField, extract_fields
from .resolve.engine import Answer, Resolution, learn, resolve_fields


@dataclass
class ApplyOutcome:
    url: str
    fields: list[FormField] = field(default_factory=list)
    resolution: Resolution | None = None
    fill: FillReport | None = None
    verification: VerifyReport | None = None
    error: str = ""

    @property
    def would_auto_submit(self) -> bool:
        """Whether ADR-004's field-level conditions are met.

        Reported, never acted on, until Phase 5. The remaining conditions
        (daily cap, per-day opt-in, match threshold) live at the run level.
        """
        return bool(
            self.resolution
            and self.resolution.all_confident
            and not self.resolution.blocking
            and self.fill is not None and self.fill.ok
            and self.verification is not None and self.verification.ok
        )

    def summary(self) -> str:
        if self.error:
            return f"error: {self.error}"
        parts = []
        if self.resolution:
            counts = self.resolution.summary()
            parts.append(
                f"resolved {counts['cache']} cached + {counts['rules']} rules, "
                f"{counts['unresolved']} unresolved"
            )
        if self.fill:
            parts.append(self.fill.summary())
        if self.verification:
            parts.append(self.verification.summary())
        return " · ".join(parts)


async def snapshot(page: Page) -> list[FormField]:
    """Extract fields from the live DOM.

    `page.content()` serializes the *rendered* DOM, so client-side frameworks
    have already run. This is what lets the identical extractor serve both a
    saved fixture and a live tab.
    """
    return extract_fields(await page.content())


# Tried in order, most specific first, matched as a case-insensitive
# substring of the accessible name.
#
# Plain strings rather than one regex: Playwright's `get_by_role(name=...)`
# accepts a compiled pattern but translates it for the browser, and a pattern
# that matched "Apply now" in Python found nothing on the real page while the
# literal string found it immediately. Strings are what actually work here.
MIN_APPLICATION_FIELDS = 3

# Labels that are a rating-scale point, a percentage bucket, or a placeholder
# rather than something anyone can answer.
_NOT_A_QUESTION = re.compile(
    r"^\s*(\d+\s*[-–]\s*\w|[<>]\s*\d+\s*%|n/?a\b|start typing|search\b|select\b"
    # Section headers and widget prompts that leaked through as "questions"
    # in the 11-job run: "Application", "Additional", "Custom", "Pick date...".
    # Nobody can answer a heading.
    r"|pick\s|(application|additional|custom|details|other)\s*$)",
    re.I,
)

_LOGIN_WALL = re.compile(
    r"\bpassword\b|\bsign\s?in\b|\bcreate\s+(an\s+)?account\b", re.I
)

_APPLY_NAMES = [
    "Apply for this job",
    "Apply for this role",
    "Apply now",
    "Start application",
    "I'm interested",
    "Apply",
]


async def _find_apply(page: Page):
    """The visible Apply affordance, or None. Links before buttons."""
    for name in _APPLY_NAMES:
        for role in ("link", "button"):
            candidate = page.get_by_role(role, name=name).first
            try:
                if await candidate.count() and await candidate.is_visible():
                    return candidate
            except Exception:  # noqa: BLE001 - keep looking
                continue
    return None


async def form_root(page: Page):
    """The frame the form actually lives in — often not the main one.

    Company career sites embed the ATS in an iframe: Stripe's page is a
    Greenhouse `job_app` embed holding 35 inputs while the main frame holds
    none. `page.content()` serializes only the main frame, so the extractor
    saw an empty page and the job was written off as unfillable.

    Returns whichever frame yields the most fields; a Frame exposes the same
    locator API as a Page, so everything downstream is unchanged.
    """
    best, best_fields = page, await snapshot(page)
    for frame in page.frames:
        if frame is page.main_frame:
            continue
        # Consent banners, tag managers and reCAPTCHA are iframes too, and a
        # reCAPTCHA frame does contain an input. Compare on field count so the
        # real form wins rather than whichever frame is found first.
        try:
            fields = extract_fields(await frame.content())
        except Exception:  # noqa: BLE001 - a cross-origin frame is not the form
            continue
        if len(fields) > len(best_fields):
            best, best_fields = frame, fields
    return best, best_fields


async def reach_form(page: Page, hops: int = 2) -> list[FormField]:
    """Get from wherever we landed to the page that actually has the form.

    Two thirds of the queue are direct ATS links whose URL *is* the form. The
    rest — company career sites and Workday tenants — land on a posting with
    an Apply button and no inputs at all. Filling used to give up there and
    report "nothing resolved", so a perfectly good job was filed as skipped
    without anyone seeing it.

    Bounded to `hops` clicks: Apply pages occasionally chain (posting →ATS
    →form), but an unbounded walk on a page that keeps offering "Apply" would
    wander off the application entirely.
    """
    fields = await snapshot(page)
    if not fields:
        # Ashby and friends render the form in the client, so a page opened a
        # moment ago is genuinely empty. Without this the walk found no fields
        # and no Apply button and gave up instantly — Plaid's application has
        # 35 fields and was reported as having none.
        await page.wait_for_timeout(3000)
        fields = await snapshot(page)

    for _ in range(hops):
        if fields:
            return fields

        target = await _find_apply(page)
        if target is None:
            return fields

        try:
            # Some ATSs open the form in a new tab; follow it when they do.
            async with page.context.expect_page(timeout=3000) as popup:
                await target.click()
            page = await popup.value
        except Exception:  # noqa: BLE001 - same-tab navigation is the common case
            pass

        try:
            await page.wait_for_load_state("domcontentloaded", timeout=15000)
            await page.wait_for_timeout(1500)  # client-rendered forms
        except Exception:  # noqa: BLE001 - snapshot below decides, not this
            pass
        fields = await snapshot(page)
    return fields


async def apply_to_page(
    page: Page,
    profile: Profile,
    resume_path: str | None = None,
    dry_run: bool = False,
    job: object | None = None,
) -> ApplyOutcome:
    outcome = ApplyOutcome(url=page.url)

    await reach_form(page)
    root, outcome.fields = await form_root(page)
    if not outcome.fields:
        outcome.error = "no form fields found on this page"
        return outcome

    # Workday puts an account wall behind Apply: the page that loads asks for
    # Email, Password and Verify Password. It is a form, and the resolver
    # would happily fill the email — producing a screenshot that looks like a
    # filled application and is actually a signup. Refuse rather than present
    # something misleading for approval.
    if any(_LOGIN_WALL.search(f.label) for f in outcome.fields if f.label) or (
        # Workday's email-first sign-in shows a single "Email address" box and
        # no password until the next step, so the label check alone missed it
        # and a one-field "application" went out for approval. No real
        # application asks fewer than three things.
        len(outcome.fields) < MIN_APPLICATION_FIELDS
    ):
        outcome.error = (
            "sign-in or account creation required, not an application"
            if len(outcome.fields) >= MIN_APPLICATION_FIELDS
            else f"only {len(outcome.fields)} field(s) — not an application form"
        )
        outcome.fields = []
        return outcome

    outcome.resolution = resolve_fields(
        outcome.fields, profile, url=page.url, resume_path=resume_path,
        # Tier 3 only when actually filling: a dry run should stay free and
        # instant, and LLM answers still reach her review before any submit.
        use_llm=not dry_run,
        company=getattr(job, "company", "") or "",
        title=getattr(job, "title", "") or "",
        description=getattr(job, "description", "") or "",
    )
    if dry_run:
        return outcome

    options = {f.ref: f.options for f in outcome.fields}
    # `root` may be an iframe; writing and verifying must both happen there,
    # or every locator resolves against a page that has none of these fields.
    outcome.fill = await fill_form(root, outcome.resolution.answers, options)

    # Verify only what was actually written; a failed write is already known.
    written_refs = {r.ref for r in outcome.fill.written}
    outcome.verification = await verify(
        root, [a for a in outcome.resolution.answers if a.ref in written_refs]
    )

    # Promote rule-derived answers to the cache so the next form is Tier 1.
    learn(outcome.resolution, page.url)

    return outcome


def unanswered_questions(outcome: ApplyOutcome) -> list[FormField]:
    """Fields a human still has to deal with, required ones first.

    Deduplicated by label, and stripped of things that are not questions.
    Plaid's form produced "End Date" twice, the same employment question
    twice, and rating-scale *options* like "1 - Slightly below average" and
    "< 40%" — asking those wastes the one channel the user actually reads.
    """
    if not outcome.resolution:
        return []

    seen: set[str] = set()
    questions: list[FormField] = []
    for field_ in sorted(
        outcome.resolution.unresolved, key=lambda f: (not f.required, f.label)
    ):
        label = (field_.label or "").strip()
        key = label.lower()
        if not label or key in seen or _NOT_A_QUESTION.match(label):
            continue
        seen.add(key)
        questions.append(field_)
    return questions


def answers_needing_review(outcome: ApplyOutcome) -> list[Answer]:
    if not outcome.resolution:
        return []
    return [a for a in outcome.resolution.answers if not a.is_confident]
