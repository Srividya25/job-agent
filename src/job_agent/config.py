"""Profile + environment loading.

One source of truth for personal data: `profile/profile.yaml`. Credentials
come from the environment. Nothing personal is ever hardcoded in source —
that is what makes the repo safe to publish.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, model_validator

ROOT = Path(__file__).resolve().parents[2]
PROFILE_PATH = ROOT / "profile" / "profile.yaml"
EXAMPLE_PATH = ROOT / "profile" / "profile.example.yaml"
DATA_DIR = ROOT / "data"


class ProfileNotFound(RuntimeError):
    pass


# --------------------------------------------------------------------------
# profile schema — mirrors profile.example.yaml
# --------------------------------------------------------------------------


class Identity(BaseModel):
    first_name: str
    last_name: str
    email: str
    phone: str = ""
    linkedin: str = ""
    github: str = ""
    portfolio: str = ""

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}"


class Address(BaseModel):
    line1: str = ""
    line2: str = ""
    city: str = ""
    state: str = ""
    postal_code: str = ""
    country: str = "United States"


class WorkAuthorization(BaseModel):
    status: str = ""
    needs_sponsorship: bool = False
    authorized_now: bool = True
    requires_clearance_eligible: bool = False


class Education(BaseModel):
    school: str
    degree: str = ""
    field: str = ""
    graduation_year: int | None = None
    # Application forms ask for attendance dates as separate month and year
    # controls. graduation_year alone left the month blank and put the same
    # answer into both selects.
    start_month: str = ""
    start_year: int | None = None
    end_month: str = ""


class Experience(BaseModel):
    years: float = 0
    current_title: str = ""
    # Short, true, one-line achievements ("Built an LLM-powered X"). They
    # feed the Tier 3 fact sheet, so essay drafts can cite something real
    # instead of producing generic enthusiasm.
    highlights: list[str] = Field(default_factory=list)


class Skills(BaseModel):
    must_have: list[str] = Field(default_factory=list)
    nice_to_have: list[str] = Field(default_factory=list)

    @property
    def all(self) -> list[str]:
        return self.must_have + self.nice_to_have


DEFAULT_EXCLUDED_LOCATIONS = [
    "canada", "ontario", "toronto", "vancouver", "montreal", "british columbia",
    "india", "bengaluru", "bangalore", "hyderabad", "pune", "chennai", "gurgaon",
    "united kingdom", "london", "ireland", "dublin",
    "germany", "berlin", "munich", "france", "paris", "netherlands", "amsterdam",
    "spain", "barcelona", "madrid", "poland", "warsaw", "krakow",
    "australia", "sydney", "melbourne", "singapore", "japan", "tokyo",
    "brazil", "sao paulo", "mexico", "israel", "tel aviv", "china", "shanghai",
]


class Preferences(BaseModel):
    titles: list[str] = Field(default_factory=list)
    exclude_titles: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    # Checked before the remote bypass — "Canada - Remote" is still Canada.
    # Anything set in profile.yaml is ADDED to DEFAULT_EXCLUDED_LOCATIONS
    # rather than replacing it: pydantic would otherwise silently discard the
    # built-in list the moment a user added one country of their own.
    exclude_locations: list[str] = Field(
        default_factory=lambda: list(DEFAULT_EXCLUDED_LOCATIONS)
    )

    remote_ok: bool = True
    min_company_size: int = 0
    min_match_score: float = 0.80
    # Hard relevance gate: a job whose title matches none of `titles` at
    # least this well (fuzzy, 0..1) is dropped at discovery and never shown.
    # Calibrated on real data: recruiters/CX/designers sit below ~0.5, real
    # engineering titles at 0.6+. 0 disables the gate.
    min_title_match: float = 0.58
    # Floor on the overall match score for a job to be *proposed* on a batch.
    # Distinct from min_match_score, which gates auto-submission and is
    # stricter. 0 proposes everything the title gate lets through.
    min_propose_score: float = 0.0
    # Reject postings whose stated floor exceeds this. 0 disables the check.
    max_years_required: int = 2

    @model_validator(mode="after")
    def _merge_excluded_locations(self) -> Preferences:
        merged = dict.fromkeys(
            [*DEFAULT_EXCLUDED_LOCATIONS, *(s.lower() for s in self.exclude_locations)]
        )
        self.exclude_locations = list(merged)
        return self


class ResumeRef(BaseModel):
    label: str
    path: str
    target_roles: list[str] = Field(default_factory=list)
    # Filename of the matching row in the Jobtracker `resumes` table, so an
    # application records which resume was sent. Label matching cannot bridge
    # this on its own — a resume labelled "general" is filed there as
    # something like "JaneDoeResume.pdf", which shares no words with the label.
    tracker_file: str = ""

    def resolve(self) -> Path:
        return ROOT / self.path


class EEO(BaseModel):
    gender: str = ""
    race: str = ""
    veteran_status: str = ""
    disability_status: str = ""


class Automation(BaseModel):
    # never = fill only, ask = Telegram approval, auto = submit unattended
    submit_mode: str = "never"
    daily_cap: int = 15
    delay_between_apps: int = 45
    never_apply_portals: list[str] = Field(default_factory=lambda: ["linkedin"])


class Profile(BaseModel):
    identity: Identity
    address: Address = Field(default_factory=Address)
    work_authorization: WorkAuthorization = Field(default_factory=WorkAuthorization)
    education: list[Education] = Field(default_factory=list)
    experience: Experience = Field(default_factory=Experience)
    skills: Skills = Field(default_factory=Skills)
    preferences: Preferences = Field(default_factory=Preferences)
    resumes: list[ResumeRef] = Field(default_factory=list)
    eeo: EEO = Field(default_factory=EEO)
    automation: Automation = Field(default_factory=Automation)


# --------------------------------------------------------------------------
# credentials — environment only, never the profile file
# --------------------------------------------------------------------------


class Secrets(BaseModel):
    anthropic_api_key: str | None = None
    supabase_url: str | None = None
    supabase_anon_key: str | None = None
    supabase_email: str | None = None
    supabase_password: str | None = None
    gmail_address: str | None = None
    gmail_app_password: str | None = None
    adzuna_app_id: str | None = None
    adzuna_app_key: str | None = None
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    # Optional second chat: job lists land there, keeping the main chat for
    # form questions, reviews, and submissions. Unset = everything in one.
    telegram_jobs_chat_id: str | None = None

    @classmethod
    def from_env(cls) -> Secrets:
        # override=True: .env is the source of truth for this tool. Without
        # it, a variable already present in the environment (including one
        # read as empty earlier in the same process) silently wins over the
        # file, which is confusing and hard to spot.
        load_dotenv(ROOT / ".env", override=True)
        g = os.getenv
        return cls(
            anthropic_api_key=g("ANTHROPIC_API_KEY"),
            supabase_url=g("SUPABASE_URL"),
            supabase_anon_key=g("SUPABASE_ANON_KEY"),
            supabase_email=g("SUPABASE_EMAIL"),
            supabase_password=g("SUPABASE_PASSWORD"),
            gmail_address=g("GMAIL_ADDRESS"),
            gmail_app_password=g("GMAIL_APP_PASSWORD"),
            adzuna_app_id=g("ADZUNA_APP_ID"),
            adzuna_app_key=g("ADZUNA_APP_KEY"),
            telegram_bot_token=g("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=g("TELEGRAM_CHAT_ID"),
            telegram_jobs_chat_id=g("TELEGRAM_JOBS_CHAT_ID"),
        )


@lru_cache(maxsize=1)
def load_profile() -> Profile:
    if not PROFILE_PATH.exists():
        raise ProfileNotFound(
            f"No profile at {PROFILE_PATH}.\n"
            f"  cp {EXAMPLE_PATH.relative_to(ROOT)} "
            f"{PROFILE_PATH.relative_to(ROOT)}\n"
            "then fill it in."
        )
    data = yaml.safe_load(PROFILE_PATH.read_text()) or {}
    return Profile.model_validate(data)


@lru_cache(maxsize=1)
def load_secrets() -> Secrets:
    return Secrets.from_env()


def data_dir() -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR
