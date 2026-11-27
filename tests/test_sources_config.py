"""Validation of ``config/sources.yaml`` seeds and schema (offline)."""

import copy

import pytest
import yaml
from pydantic import ValidationError

from src.collectors.config import ATS_PROVIDERS, CompanySeed, SourcesConfig
from src.enrichment.geo import GeoResolver
from tests.conftest import CONFIG_DIR


@pytest.fixture
def raw_sources() -> dict:
    return yaml.safe_load((CONFIG_DIR / "sources.yaml").read_text(encoding="utf-8"))


def test_real_seeds_are_valid_and_verified(geo: GeoResolver) -> None:
    sources = SourcesConfig.load(CONFIG_DIR)
    seeds = [seed for provider in ATS_PROVIDERS for seed in sources.ats[provider].companies]
    assert len(seeds) >= 10  # seed list grows over time; every seed must still be verified
    assert all(seed.verified_on is not None for seed in seeds)
    for seed in seeds:
        if seed.hq_city is not None:
            geo.city(seed.hq_city)  # raises KeyError on a typo'd city slug
    domains = [seed.domain for seed in seeds]
    assert len(domains) == len(set(domains)), "a startup is seeded twice"


def test_seed_domain_is_canonicalized() -> None:
    seed = CompanySeed(name="Acme", slug="acme", domain="https://www.Acme.in/careers")
    assert seed.domain == "acme.in"


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        (("ats", "lever", "endpoint"), "https://api.lever.co/v0/postings", "slug"),
        (("internship", "include_patterns"), ["(unclosed"], "invalid regex"),
        (("ats", "lever", "companies"), [{"name": "X", "slug": "a b", "domain": "x.in"}], "slug"),
        (
            ("ats", "lever", "companies"),
            [{"name": "X", "slug": "x", "domain": "x.in", "hq": 1}],
            "Extra inputs are not permitted",
        ),
    ],
)
def test_schema_rejects_bad_config(raw_sources: dict, path: tuple, value, message: str) -> None:
    bad = copy.deepcopy(raw_sources)
    node = bad
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    with pytest.raises(ValidationError, match=message):
        SourcesConfig.model_validate(bad)


def test_duplicate_slugs_rejected(raw_sources: dict) -> None:
    bad = copy.deepcopy(raw_sources)
    seed = {"name": "Dup", "slug": "dup", "domain": "dup.in"}
    bad["ats"]["ashby"]["companies"] = [seed, {**seed, "slug": "DUP"}]
    with pytest.raises(ValidationError, match="duplicate"):
        SourcesConfig.model_validate(bad)


def test_missing_provider_rejected(raw_sources: dict) -> None:
    bad = copy.deepcopy(raw_sources)
    del bad["ats"]["workable"]
    with pytest.raises(ValidationError, match="missing providers"):
        SourcesConfig.model_validate(bad)
