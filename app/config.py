"""Configuration for T2R OS.

Two kinds of settings live here, kept deliberately apart:

* **Runtime settings** (``Settings``) — database URL, Telegram token, admin ids.
  Read from the environment with the ``T2R__`` prefix.
* **Process configuration** (``PROCESS``) — the academy's *rules*: the Friday
  report questions, the "missed = N reports" intervention threshold, the
  reporting week. These are Trade2Retire's policy, not the engine's mechanics,
  so they sit in one obvious place and carry ``TODO(founder)`` where a real
  academy answer should replace the sensible default. Keeping them here is the
  seam that lets the same engine serve another academy later without touching
  the core.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-driven runtime settings (prefix ``T2R__``)."""

    model_config = SettingsConfigDict(env_prefix="T2R__", extra="ignore")

    database_url: str = Field(
        default="postgresql+asyncpg://postgres:postgres@localhost:5432/t2ros",
        description="Async SQLAlchemy DSN. Railway Postgres injects this.",
    )
    telegram_token: str = Field(default="", description="Telegram bot token.")
    admin_ids: str = Field(
        default="",
        description="Comma-separated Telegram ids granted the ADMIN role.",
    )
    academy_name: str = Field(default="Trade2Retire Academy")
    timezone: str = Field(default="Africa/Lagos")

    @field_validator("database_url", mode="before")
    @classmethod
    def _async_dsn(cls, value: object) -> object:
        """Accept Railway's plain ``postgresql://`` and use the async driver.

        Railway hands out ``postgres://…`` / ``postgresql://…``; SQLAlchemy's
        async engine needs ``postgresql+asyncpg://…``. Normalising here means
        ``T2R__DATABASE_URL`` can be set to Railway's own reference verbatim.
        """
        if isinstance(value, str):
            if value.startswith("postgres://"):
                return "postgresql+asyncpg://" + value[len("postgres://"):]
            if value.startswith("postgresql://"):
                return "postgresql+asyncpg://" + value[len("postgresql://"):]
        return value

    @property
    def admin_id_set(self) -> set[int]:
        out: set[int] = set()
        for token in self.admin_ids.replace(" ", "").split(","):
            if token.isdigit():
                out.add(int(token))
        return out


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


# ── Process configuration (academy policy — tune here) ───────────────────────


@dataclass(frozen=True)
class FridayQuestion:
    """One structured field in the weekly report."""

    key: str
    prompt: str
    required: bool = True


@dataclass(frozen=True)
class ProcessConfig:
    """Trade2Retire's operating rules. TODO(founder) marks values to confirm."""

    # TODO(founder Q2): confirm the real Friday report template. These are a
    # sensible structured starting set derived from the academy's own checklist
    # culture (setups seen vs executed, planned vs actual risk, rule adherence).
    friday_questions: tuple[FridayQuestion, ...] = (
        FridayQuestion("trades_taken", "How many trades did you take this week?"),
        FridayQuestion("setups_seen", "How many valid setups did you observe?"),
        FridayQuestion("setups_executed", "How many of those did you execute?"),
        FridayQuestion("planned_risk", "What risk % did you plan per trade?"),
        FridayQuestion("actual_risk", "What risk % did you actually take?"),
        FridayQuestion(
            "rule_adherence",
            "Did you follow the 6 Pillars / pre-entry checklist every time? "
            "(yes / mostly / no — and what broke)",
        ),
        FridayQuestion("journal_done", "Did you journal every trade? (yes/no)"),
        FridayQuestion("biggest_mistake", "Your biggest mistake this week?", required=False),
        FridayQuestion("lesson", "The single lesson you're taking into next week?"),
        FridayQuestion("help_needed", "Where do you need your mentor's help?", required=False),
    )

    # TODO(founder Q4): confirm the real "needs attention" rule. Default: two
    # consecutive missed Friday reports raises a mentor intervention.
    missed_reports_threshold: int = 2

    # Days a mentor has to act on an intervention before it is overdue.
    intervention_sla_days: int = 3

    # Reporting week ends on Friday (weekday 4). Reports are due for the week
    # they cover; "missed" is judged after the week closes.
    report_week_ends_weekday: int = 4

    academy_values: tuple[str, ...] = field(
        default=(
            "Process over profit — a losing trade can be well executed.",
            "Every flag explains itself.",
            "No fake scoring. No fabricated insight.",
        )
    )


PROCESS = ProcessConfig()
