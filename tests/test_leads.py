"""Offline tests for ``StartupLead`` and lead storage (ADR 004)."""

from datetime import date

import pytest
from pydantic import ValidationError

from src.enrichment.geo import GeoResolver
from src.models import FundingStage, StartupLead, lead_name_key
from src.storage import Repository


@pytest.mark.parametrize(
    ("name", "key"),
    [
        ("ExampleLabs Technologies Pvt. Ltd.", "examplelabs technologies"),
        ("ExampleLabs Technologies Private Limited", "examplelabs technologies"),
        ("Acme LLP", "acme"),
        ("Zeta Labs", "zeta labs"),  # brand words are kept
        ("  GIVA  ", "giva"),
        ("Ltd", "ltd"),  # never reduce a name to nothing
    ],
)
def test_lead_name_key(name: str, key: str) -> None:
    assert lead_name_key(name) == key


def test_lead_cleans_domain_and_founders() -> None:
    lead = StartupLead(
        name="Acme",
        source="thub_lab32",
        domain="https://www.Acme.example/about",
        founders=["Asha  Rao", "Asha Rao", " ", "Vikram Iyer"],
    )
    assert lead.domain == "acme.example"
    assert lead.founders == ["Asha Rao", "Vikram Iyer"]
    assert StartupLead(name="Acme", source="inc42", domain="  ").domain is None


def test_lead_with_hq_and_to_startup(geo: GeoResolver) -> None:
    lead = StartupLead(
        name="Acme",
        source="entrackr",
        domain="acme.example",
        funding_stage="seed",
        last_funded_at=date(2026, 11, 20),
    ).with_hq(geo.resolve("Gurugram, Haryana"))
    assert (lead.hq_city_canonical, lead.hq_state) == ("delhi_ncr", "Haryana")
    startup = lead.to_startup()
    assert startup is not None
    assert startup.canonical_domain == "acme.example"
    assert startup.funding_stage is FundingStage.SEED
    assert startup.hq_city_canonical == "delhi_ncr"
    assert startup.discovered_via == "entrackr"
    assert StartupLead(name="NoSite", source="inc42").to_startup() is None


def test_lead_rejects_half_coordinates() -> None:
    with pytest.raises(ValidationError):
        StartupLead(name="A", source="inc42", hq_lat=1.0)


def test_lead_upsert_merges_name_variants(repo: Repository) -> None:
    first = repo.upsert_lead(
        StartupLead(name="ExampleLabs Pvt Ltd", source="thub_lab32", founders=["Asha Rao"])
    )
    second = repo.upsert_lead(
        StartupLead(
            name="ExampleLabs",
            source="entrackr",
            funding_stage="pre_seed",
            funding_amount="Rs 4 Cr",
            last_funded_at=date(2026, 11, 2),
        )
    )
    assert first == second
    (lead,) = repo.list_leads()
    assert lead.source == "entrackr"  # latest sighting
    assert lead.founders == ["Asha Rao"]  # not erased by a sighting without founders
    assert lead.funding_stage is FundingStage.PRE_SEED
    assert lead.first_seen_at is not None
    assert repo.get_lead_by_name("examplelabs private limited") is not None
    assert repo.counts()["leads"] == 1
