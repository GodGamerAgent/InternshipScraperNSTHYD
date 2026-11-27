"""Hidden-opportunity score (0–100) — rubric and weights from ``config/scoring.yaml``.

| Signal            | Default | Awarded when |
|-------------------|---------|--------------|
| recent_funding    | 30      | Pre-Seed/Seed (``stages``) within ``max_age_days`` |
| target_location   | 20      | HQ in a target city, or the startup offers Remote-India roles |
| github_activity   | 20      | ``min_active_repos``+ active repos, or a recent push |
| cold_pitch        | 15      | careers page invites pitches, or a direct recruiting email |
| team_or_stack     | 15      | team size in [min, max], or tech stack overlaps ``target_skills`` |

Each signal is all-or-nothing, so a score is always explainable via ``ScoreBreakdown.reasons``.
"""

from collections.abc import Collection, Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.core.config import load_yaml
from src.enrichment.github_signals import GithubSignals
from src.models import (
    CITY_REMOTE_INDIA,
    CITY_UNKNOWN,
    Contact,
    ContactSourceMethod,
    FundingStage,
    Opportunity,
    Startup,
    WorkMode,
)


# ---------------------------------------------------------------------------- config schema
class _Signal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    points: int = Field(ge=0, le=100)
    description: str = ""


class RecentFundingSignal(_Signal):
    max_age_days: int = Field(gt=0)
    stages: list[FundingStage] = Field(min_length=1)


class GithubActivitySignal(_Signal):
    min_active_repos: int = Field(ge=1)
    recent_commit_days: int = Field(gt=0)
    active_repo_days: int = Field(gt=0)


class TeamOrStackSignal(_Signal):
    team_size_min: int = Field(ge=1)
    team_size_max: int = Field(ge=1)
    target_skills: list[str] = Field(default_factory=list)


class Signals(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    recent_funding: RecentFundingSignal
    target_location: _Signal
    github_activity: GithubActivitySignal
    cold_pitch: _Signal
    team_or_stack: TeamOrStackSignal


class Thresholds(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    high: int = Field(ge=0, le=100)
    medium: int = Field(ge=0, le=100)


class ContactConfidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    direct_public: float = Field(ge=0, le=1)
    github_public: float = Field(ge=0, le=1)
    pattern_mx_verified: float = Field(ge=0, le=1)

    def for_method(self, method: ContactSourceMethod) -> float:
        return float(getattr(self, method.value))


class ScoringConfig(BaseModel):
    """Validated ``config/scoring.yaml``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_score: int = Field(gt=0)
    signals: Signals
    thresholds: Thresholds
    contact_confidence: ContactConfidence

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        total = sum(getattr(self.signals, name).points for name in Signals.model_fields)
        if total != self.max_score:
            raise ValueError(f"signal points sum to {total}, expected max_score={self.max_score}")
        if self.thresholds.medium > self.thresholds.high:
            raise ValueError("thresholds.medium must be <= thresholds.high")
        tos = self.signals.team_or_stack
        if tos.team_size_min > tos.team_size_max:
            raise ValueError("team_size_min must be <= team_size_max")
        return self

    @classmethod
    def load(cls, config_dir: Path | None = None) -> Self:
        return cls.model_validate(load_yaml("scoring", config_dir))


# ---------------------------------------------------------------------------- inputs / outputs
@dataclass(frozen=True, slots=True)
class ScoreInputs:
    """Everything the rubric looks at, decoupled from storage."""

    funding_stage: FundingStage | None = None
    last_funded_at: date | None = None
    hq_city: str | None = None
    offers_remote_india: bool = False
    github: GithubSignals | None = None
    open_to_cold_pitch: bool | None = None
    has_direct_contact: bool = False
    team_size: int | None = None
    tech_stack: tuple[str, ...] = ()

    @classmethod
    def from_startup(
        cls,
        startup: Startup,
        *,
        github: GithubSignals | None = None,
        contacts: Iterable[Contact] = (),
        opportunities: Iterable[Opportunity] = (),
    ) -> Self:
        """Build inputs from stored records (+ optional fresh GitHub signals)."""
        languages = tuple(lang.lower() for lang in github.languages) if github else ()
        return cls(
            funding_stage=startup.funding_stage,
            last_funded_at=startup.last_funded_at,
            hq_city=startup.hq_city_canonical,
            offers_remote_india=any(o.work_mode is WorkMode.REMOTE_INDIA for o in opportunities),
            github=github,
            open_to_cold_pitch=startup.open_to_cold_pitch,
            has_direct_contact=any(
                c.source_method is ContactSourceMethod.DIRECT_PUBLIC and c.email for c in contacts
            ),
            team_size=startup.team_size,
            tech_stack=tuple(dict.fromkeys((*startup.tech_stack, *languages))),
        )


@dataclass(slots=True)
class ScoreBreakdown:
    """Total plus per-signal points and human-readable reasons."""

    total: int
    label: str
    components: dict[str, int] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------- scorer
class HiddenOpportunityScorer:
    """Apply the rubric. ``target_cities=None`` means "any known Indian city counts"."""

    def __init__(
        self,
        config: ScoringConfig,
        *,
        target_cities: Collection[str] | None = None,
        today: date | None = None,
    ) -> None:
        self.config = config
        self.target_cities = set(target_cities) if target_cities is not None else None
        self.today = today or datetime.now(UTC).date()

    def score(self, inputs: ScoreInputs) -> ScoreBreakdown:
        s = self.config.signals
        components: dict[str, int] = {}
        reasons: list[str] = []

        def award(name: str, points: int, reason: str) -> None:
            components[name] = points
            reasons.append(f"+{points} {reason}")

        if (
            inputs.funding_stage in s.recent_funding.stages
            and inputs.last_funded_at is not None
            and 0 <= (self.today - inputs.last_funded_at).days <= s.recent_funding.max_age_days
        ):
            award(
                "recent_funding",
                s.recent_funding.points,
                f"{inputs.funding_stage} round on {inputs.last_funded_at}",
            )

        city_ok = inputs.hq_city not in (None, CITY_UNKNOWN, CITY_REMOTE_INDIA) and (
            self.target_cities is None or inputs.hq_city in self.target_cities
        )
        if city_ok or inputs.offers_remote_india:
            where = f"HQ in {inputs.hq_city}" if city_ok else "offers Remote-India roles"
            award("target_location", s.target_location.points, where)

        gh = inputs.github
        if gh is not None and gh.is_active(s.github_activity.min_active_repos):
            award(
                "github_activity",
                s.github_activity.points,
                f"GitHub {gh.org}: {gh.active_repos} active repos",
            )

        if inputs.open_to_cold_pitch or inputs.has_direct_contact:
            why = "careers page invites cold pitches" if inputs.open_to_cold_pitch else (
                "direct public recruiting email"
            )  # fmt: skip
            award("cold_pitch", s.cold_pitch.points, why)

        tos = s.team_or_stack
        small_team = inputs.team_size is not None and (
            tos.team_size_min <= inputs.team_size <= tos.team_size_max
        )
        skills = {k.lower() for k in tos.target_skills}
        stack_hits = sorted(skills & {t.lower() for t in inputs.tech_stack})
        if small_team or stack_hits:
            why = f"team of {inputs.team_size}" if small_team else f"stack: {', '.join(stack_hits)}"
            award("team_or_stack", tos.points, why)

        total = min(sum(components.values()), self.config.max_score)
        th = self.config.thresholds
        label = "high" if total >= th.high else "medium" if total >= th.medium else "low"
        return ScoreBreakdown(total, label, components, reasons)
