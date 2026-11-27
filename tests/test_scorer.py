"""Offline tests for the hidden-opportunity scorer and ``scoring.yaml`` schema."""

import copy
from datetime import UTC, date, datetime

import pytest
import yaml
from pydantic import ValidationError

from src.enrichment.github_signals import GithubSignals
from src.enrichment.scorer import HiddenOpportunityScorer, ScoreInputs, ScoringConfig
from src.models import Contact, ContactSourceMethod, FundingStage, Opportunity, Startup
from tests.conftest import CONFIG_DIR

TODAY = date(2026, 11, 26)


@pytest.fixture(scope="module")
def config() -> ScoringConfig:
    return ScoringConfig.load(CONFIG_DIR)


def active_github(repos: int = 4, recent: bool = True) -> GithubSignals:
    return GithubSignals(
        org="examplestack-oss",
        public_repos=12,
        active_repos=repos,
        recently_pushed=recent,
        last_pushed_at=datetime(2026, 11, 25, tzinfo=UTC),
        languages=("Go", "Python"),
        org_email=None,
        blog=None,
        location=None,
    )


def test_perfect_score(config: ScoringConfig) -> None:
    scorer = HiddenOpportunityScorer(config, target_cities={"hyderabad"}, today=TODAY)
    result = scorer.score(
        ScoreInputs(
            funding_stage=FundingStage.SEED,
            last_funded_at=date(2026, 11, 1),
            hq_city="hyderabad",
            github=active_github(),
            open_to_cold_pitch=True,
            team_size=12,
        )
    )
    assert result.total == 100
    assert result.label == "high"
    assert result.components == {
        "recent_funding": 30,
        "target_location": 20,
        "github_activity": 20,
        "cold_pitch": 15,
        "team_or_stack": 15,
    }
    assert len(result.reasons) == 5


def test_zero_score(config: ScoringConfig) -> None:
    result = HiddenOpportunityScorer(config, today=TODAY).score(ScoreInputs())
    assert (result.total, result.label, result.components) == (0, "low", {})


@pytest.mark.parametrize(
    ("stage", "funded", "points"),
    [
        (FundingStage.PRE_SEED, date(2026, 10, 1), 30),
        (FundingStage.SEED, date(2026, 9, 27), 30),  # exactly 60 days
        (FundingStage.SEED, date(2026, 9, 26), 0),  # 61 days
        (FundingStage.SERIES_A, date(2026, 11, 20), 0),  # not an early stage
        (FundingStage.SEED, date(2026, 12, 5), 0),  # future-dated news
        (FundingStage.SEED, None, 0),
    ],
)
def test_recent_funding_window(config: ScoringConfig, stage, funded, points) -> None:
    result = HiddenOpportunityScorer(config, today=TODAY).score(
        ScoreInputs(funding_stage=stage, last_funded_at=funded)
    )
    assert result.components.get("recent_funding", 0) == points


def test_target_location_rules(config: ScoringConfig) -> None:
    targeted = HiddenOpportunityScorer(config, target_cities={"chennai"}, today=TODAY)
    anywhere = HiddenOpportunityScorer(config, today=TODAY)
    assert targeted.score(ScoreInputs(hq_city="pune")).total == 0
    assert anywhere.score(ScoreInputs(hq_city="pune")).total == 20  # no targets: any Indian city
    assert anywhere.score(ScoreInputs(hq_city="unknown")).total == 0
    assert targeted.score(ScoreInputs(hq_city="pune", offers_remote_india=True)).total == 20


def test_github_and_team_or_stack(config: ScoringConfig) -> None:
    scorer = HiddenOpportunityScorer(config, today=TODAY)
    assert scorer.score(ScoreInputs(github=active_github(repos=1, recent=False))).total == 0
    assert scorer.score(ScoreInputs(github=active_github(repos=1, recent=True))).total == 20
    assert scorer.score(ScoreInputs(github=active_github(repos=3, recent=False))).total == 20
    assert scorer.score(ScoreInputs(team_size=200)).total == 0
    assert scorer.score(ScoreInputs(team_size=200, tech_stack=("Python",))).total == 15
    assert scorer.score(ScoreInputs(team_size=35)).total == 15


def test_inputs_from_startup_records(config: ScoringConfig) -> None:
    startup = Startup(
        name="ExampleStack",
        canonical_domain="examplestack.example",
        hq_city_canonical="bengaluru",
        funding_stage="pre_seed",
        last_funded_at=date(2026, 11, 10),
    )
    contacts = [
        Contact(
            startup_id=1,
            email="careers@examplestack.example",
            source_method="direct_public",
            confidence_score=0.95,
        )
    ]
    remote = Opportunity(
        source="lever",
        title="Intern",
        url="https://jobs.lever.co/x/1",
        city_canonical="remote_india",
        work_mode="remote_india",
    )
    inputs = ScoreInputs.from_startup(
        startup, github=active_github(), contacts=contacts, opportunities=[remote]
    )
    assert inputs.has_direct_contact
    assert inputs.offers_remote_india
    assert inputs.tech_stack == ("go", "python")
    result = HiddenOpportunityScorer(config, target_cities={"hyderabad"}, today=TODAY).score(inputs)
    assert result.total == 100  # remote-India role satisfies location; python matches skills


def test_contact_confidence_lookup(config: ScoringConfig) -> None:
    cc = config.contact_confidence
    assert cc.for_method(ContactSourceMethod.DIRECT_PUBLIC) == 0.95
    assert cc.for_method(ContactSourceMethod.PATTERN_MX_VERIFIED) == 0.5


@pytest.fixture
def raw() -> dict:
    return yaml.safe_load((CONFIG_DIR / "scoring.yaml").read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda d: d["signals"]["cold_pitch"].update(points=20), "sum to"),
        (lambda d: d["thresholds"].update(medium=90), "medium"),
        (lambda d: d["signals"]["team_or_stack"].update(team_size_min=50), "team_size_min"),
        (lambda d: d["signals"]["recent_funding"].update(stages=["mega_round"]), "stages"),
        (lambda d: d.update(bonus=5), "Extra inputs"),
    ],
)
def test_scoring_config_validation(raw: dict, mutate, message: str) -> None:
    bad = copy.deepcopy(raw)
    mutate(bad)
    with pytest.raises(ValidationError, match=message):
        ScoringConfig.model_validate(bad)
