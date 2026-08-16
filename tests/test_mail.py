"""The confirmation-email watcher's matching logic.

Pure functions only — no IMAP, no network. The mailbox wrapper is a thin
stdlib shim; what needs proving is the part that decides which email belongs
to which application and what it means.
"""

from __future__ import annotations

from job_agent.models import ATS, Job, JobStatus
from job_agent.notify import mail
from job_agent.notify.mail import Mail, classify, company_tokens, match_jobs, mentions


def job(company: str, title: str = "Software Engineer") -> Job:
    return Job(
        dedupe_key=f"k-{company.lower()}",
        company=company,
        title=title,
        url=f"https://example.com/{company.lower()}",
        ats=ATS.GREENHOUSE,
        status=JobStatus.APPLIED,
    )


# --------------------------------------------------------------------------
# classification
# --------------------------------------------------------------------------


def test_thank_you_for_applying_is_a_confirmation() -> None:
    assert classify("Thank you for applying to Plaid!") == "confirmed"


def test_application_received_is_a_confirmation() -> None:
    assert classify("Your application has been received") == "confirmed"


def test_rejection_outranks_the_polite_thank_you() -> None:
    """"Thanks for applying — unfortunately" is one very common email."""
    assert classify(
        "Thank you for applying",
        "Unfortunately we have decided to move forward with other candidates.",
    ) == "rejected"


def test_interview_mail_is_an_update_not_a_confirmation() -> None:
    assert classify("Next steps: schedule your interview") == "update"


def test_unrelated_mail_is_nothing() -> None:
    assert classify("Your monthly newsletter is here") == ""


# --------------------------------------------------------------------------
# company matching
# --------------------------------------------------------------------------


def test_sender_domain_matches_the_company() -> None:
    assert mentions("Plaid", "Application update", "no-reply@plaid.com")


def test_subject_mention_matches_as_a_whole_word() -> None:
    assert mentions("Stripe", "Your Stripe application", "jobs@greenhouse.io")


def test_short_prefix_does_not_match_inside_another_word() -> None:
    """"Meta" must not match "Metadata Weekly"."""
    assert not mentions("Meta", "Metadata Weekly digest", "news@example.com")


def test_generic_words_alone_never_match() -> None:
    """A company whose name is all generic words cannot claim every email."""
    assert not mentions(
        "Software Solutions Inc", "Software engineering digest",
        "digest@example.com",
    )
    assert company_tokens("Software Solutions Inc") == set()


def test_match_jobs_pairs_each_mail_with_its_company() -> None:
    mails = [
        Mail(uid="1", subject="Thanks for applying to MongoDB",
             sender="no-reply@mongodb.com"),
        Mail(uid="2", subject="Weekly jobs digest", sender="digest@boards.io"),
    ]
    hits = match_jobs(mails, [job("MongoDB"), job("Plaid")])
    assert set(hits) == {"1"}
    assert [j.company for j in hits["1"]] == ["MongoDB"]


def test_findings_message_carries_the_receipt() -> None:
    text = mail.format_findings([
        mail.Finding("confirmed", "k", "Plaid", "Software Engineer",
                     "Thank you for applying to Plaid"),
    ])
    assert "confirmed" in text
    assert "Plaid" in text
    assert "Thank you for applying" in text
