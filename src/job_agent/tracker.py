"""Write submitted applications into the Jobtracker Supabase instance.

Write-only, into the existing `applications` table. The Jobtracker repo is
open source and is not modified: no new tables, no schema changes, no columns
added. Agent metadata rides in the existing `notes` field.

Uses the REST API directly rather than the supabase-py client — one dependency
fewer, and the two calls involved are trivial.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import httpx

from .config import load_secrets

# Jobtracker's UI uses lowercase statuses (ApplicationForm.tsx STATUSES), but
# setup.sql defaults the column to 'Applied' with a capital A. Kanban.tsx
# filters on `a.status === col.key`, so a capitalised row matches no column and
# vanishes from the board. Always send lowercase; never rely on the default.
STATUS_APPLIED = "applied"


class TrackerError(RuntimeError):
    pass


@dataclass
class Tracker:
    base_url: str
    anon_key: str
    access_token: str
    user_id: str

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "apikey": self.anon_key,
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }

    # ----------------------------------------------------------------
    @classmethod
    def connect(cls) -> Tracker:
        """Sign in as the user so row-level security applies normally.

        The alternative — a service_role key — bypasses RLS entirely and is a
        full-database credential sitting on disk. Not worth it for a personal
        tool.
        """
        s = load_secrets()
        missing = [
            name
            for name, value in (
                ("SUPABASE_URL", s.supabase_url),
                ("SUPABASE_ANON_KEY", s.supabase_anon_key),
                ("SUPABASE_EMAIL", s.supabase_email),
                ("SUPABASE_PASSWORD", s.supabase_password),
            )
            if not value
        ]
        if missing:
            raise TrackerError(f"missing in .env: {', '.join(missing)}")

        base = str(s.supabase_url).rstrip("/")
        try:
            r = httpx.post(
                f"{base}/auth/v1/token?grant_type=password",
                headers={"apikey": s.supabase_anon_key, "Content-Type": "application/json"},
                json={"email": s.supabase_email, "password": s.supabase_password},
                timeout=20.0,
            )
        except httpx.HTTPError as exc:
            raise TrackerError(f"cannot reach Supabase: {exc}") from exc

        if r.status_code != 200:
            raise TrackerError(
                f"sign-in failed ({r.status_code}). If you use a magic link or "
                "Google sign-in rather than a password, set a password in "
                "Supabase or tell me and I'll switch to token auth."
            )

        payload = r.json()
        return cls(
            base_url=base,
            anon_key=str(s.supabase_anon_key),
            access_token=payload["access_token"],
            user_id=payload["user"]["id"],
        )

    # ----------------------------------------------------------------
    def already_applied(self, job_url: str) -> str | None:
        """Existing application id for this URL, if any.

        There is no unique constraint on job_url and adding one would mean
        changing the tracker's schema, so duplicates are prevented by checking
        first. Single user, so the race window is irrelevant.
        """
        r = httpx.get(
            f"{self.base_url}/rest/v1/applications",
            headers=self._headers,
            params={"select": "id", "user_id": f"eq.{self.user_id}",
                    "job_url": f"eq.{job_url}", "limit": "1"},
            timeout=20.0,
        )
        if r.status_code != 200:
            return None
        rows = r.json()
        return rows[0]["id"] if rows else None

    def resume_id(self, label: str | None, tracker_file: str = "") -> str | None:
        """Map a local resume onto a row in the tracker's resumes table.

        `tracker_file` from the profile is authoritative; the label is only a
        fallback. Best-effort either way: if nothing matches, the column is
        left null and the application still records fine.
        """
        needle = (tracker_file or label or "").strip()
        if not needle:
            return None
        r = httpx.get(
            f"{self.base_url}/rest/v1/resumes",
            headers=self._headers,
            params={"select": "id,file_name", "user_id": f"eq.{self.user_id}"},
            timeout=20.0,
        )
        if r.status_code != 200:
            return None
        for row in r.json():
            if needle.lower() in (row.get("file_name") or "").lower():
                return row["id"]
        return None

    def record(
        self,
        company: str,
        job_title: str,
        job_url: str,
        job_description: str = "",
        resume_label: str | None = None,
        tracker_file: str = "",
        note: str = "",
        status: str = STATUS_APPLIED,
    ) -> tuple[bool, str]:
        """Insert one application. Returns (created, id-or-message)."""
        if existing := self.already_applied(job_url):
            return False, existing

        body = {
            "user_id": self.user_id,
            "company": company,
            "job_title": job_title,
            "job_url": job_url,
            "job_description": (job_description or "")[:20000],
            "status": status,
            "applied_date": date.today().isoformat(),
            "resume_id": self.resume_id(resume_label, tracker_file),
            "notes": note,
        }
        r = httpx.post(
            f"{self.base_url}/rest/v1/applications",
            headers={**self._headers, "Prefer": "return=representation"},
            json=body,
            timeout=30.0,
        )
        if r.status_code not in (200, 201):
            raise TrackerError(f"insert failed ({r.status_code}): {r.text[:200]}")
        rows = r.json()
        return True, (rows[0]["id"] if rows else "")


def build_note(
    match_score: float,
    resume_label: str | None,
    cached: int,
    rules: int,
    asked: int,
) -> str:
    """Agent metadata, phrased to read naturally in the tracker UI."""
    parts = [
        "Auto-applied by job-agent",
        f"match {match_score:.0%}",
    ]
    if resume_label:
        parts.append(f"resume: {resume_label}")
    total = cached + rules + asked
    parts.append(f"{cached + rules}/{total} fields automatic")
    if asked:
        parts.append(f"{asked} answered by you")
    return " · ".join(parts)
