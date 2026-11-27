"""Offline tests for ``src/enrichment/geo.py`` driven by ``tests/fixtures/geo/``."""

import copy
import json

import pytest
import yaml
from bs4 import BeautifulSoup
from pydantic import ValidationError

from src.enrichment.geo import GeoConfig, GeoResolver, haversine_km
from src.models import CITY_REMOTE_INDIA, CITY_UNKNOWN, Country, ResolutionMethod, WorkMode
from tests.conftest import CONFIG_DIR, FIXTURES

CASES = json.loads((FIXTURES / "geo" / "location_cases.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------- step 1 + 2: table-driven
@pytest.mark.parametrize("case", CASES, ids=[c["raw"] or "<empty>" for c in CASES])
def test_resolve_fixture_cases(geo: GeoResolver, case: dict) -> None:
    loc = geo.resolve(case["raw"])
    assert loc.country == (Country(case["country"]) if case["country"] else None)
    assert loc.city_canonical == case["city"]
    assert loc.state == case["state"]
    assert loc.work_mode == (WorkMode(case["work_mode"]) if case["work_mode"] else None)
    assert loc.resolution_method == ResolutionMethod(case["method"])


def test_resolved_city_has_centroid_and_raw(geo: GeoResolver) -> None:
    loc = geo.resolve("  Gachibowli, Hyderabad  ")
    assert loc.raw_location == "Gachibowli, Hyderabad"
    assert (loc.lat, loc.lng) == (17.3850, 78.4867)
    assert loc.matched_term == "Gachibowli"
    assert loc.is_india
    assert loc.is_resolved


def test_multi_city_listing_keeps_order_and_primary(geo: GeoResolver) -> None:
    loc = geo.resolve("Bangalore / Hyderabad / Chennai / Bengaluru")
    assert loc.city_canonical == "bengaluru"
    assert loc.all_cities == ["bengaluru", "hyderabad", "chennai"]


def test_matching_is_case_and_punctuation_insensitive(geo: GeoResolver) -> None:
    variants = ["HITEC CITY", "hitec-city", "Hi Tech City", "hi-tech   city", "Hitech City!!"]
    assert {geo.resolve(v).city_canonical for v in variants} == {"hyderabad"}


def test_whole_word_matching_avoids_substring_false_positives(geo: GeoResolver) -> None:
    # "maa" (Chennai airport code) must not match inside "Kumaar"; "del" not inside "Model".
    assert geo.resolve("Kumaar Model Systems").city_canonical == CITY_UNKNOWN


def test_timezone_name_is_not_a_kolkata_city_match(geo: GeoResolver) -> None:
    loc = geo.resolve("Remote, Asia/Kolkata")
    assert loc.city_canonical == CITY_REMOTE_INDIA
    assert "kolkata" not in loc.all_cities


def test_remote_india_city_requires_remote_mode(geo: GeoResolver) -> None:
    loc = geo.resolve("Remote within India")
    assert loc.city_canonical == CITY_REMOTE_INDIA
    assert loc.work_mode is WorkMode.REMOTE_INDIA
    assert loc.lat is None
    assert loc.lng is None


def test_hybrid_beats_remote(geo: GeoResolver) -> None:
    assert geo.resolve("Hybrid - Remote/Office, Pune").work_mode is WorkMode.HYBRID


def test_bare_remote_from_indian_company_is_remote_india(geo: GeoResolver) -> None:
    assert geo.resolve("Remote").work_mode is WorkMode.REMOTE_GLOBAL
    assert geo.resolve("Remote", hq_city="pune").work_mode is WorkMode.REMOTE_INDIA
    # ...but an explicit foreign marker still wins over the company's Indian HQ.
    assert geo.resolve("Remote - US", hq_city="pune").work_mode is WorkMode.REMOTE_GLOBAL


# ---------------------------------------------------------------- step 3: HQ fallback
def test_pin_in_hq_footer_html_fixture(geo: GeoResolver) -> None:
    html = (FIXTURES / "geo" / "contact_footer_bengaluru.html").read_text(encoding="utf-8")
    footer_text = BeautifulSoup(html, "lxml").get_text(" ")
    loc = geo.resolve("India", hq_text=footer_text)
    assert loc.city_canonical == "bengaluru"
    assert loc.resolution_method is ResolutionMethod.HQ_TEXT_PIN
    assert loc.matched_term == "560095"  # phone number "98765 43210" was not mistaken for a PIN


def test_fallback_order_pin_then_hq_city_then_source_default(geo: GeoResolver) -> None:
    pin = geo.resolve("India 600 113", hq_city="pune", source_default_city="hyderabad")
    assert (pin.city_canonical, pin.resolution_method) == ("chennai", ResolutionMethod.PIN_CODE)

    hq = geo.resolve("India", hq_city="pune", source_default_city="hyderabad")
    assert (hq.city_canonical, hq.resolution_method) == ("pune", ResolutionMethod.HQ_CITY)

    src = geo.resolve(None, source_default_city="hyderabad")
    assert (src.city_canonical, src.resolution_method) == (
        "hyderabad",
        ResolutionMethod.SOURCE_DEFAULT,
    )


def test_explicit_city_beats_fallbacks(geo: GeoResolver) -> None:
    loc = geo.resolve("Chennai", hq_city="pune", source_default_city="hyderabad")
    assert loc.city_canonical == "chennai"


def test_fallback_not_applied_to_foreign_or_remote(geo: GeoResolver) -> None:
    assert geo.resolve("London, UK", hq_city="pune").city_canonical == CITY_UNKNOWN
    assert geo.resolve("Remote - India", hq_city="pune").city_canonical == CITY_REMOTE_INDIA


def test_unknown_fallback_slug_raises(geo: GeoResolver) -> None:
    with pytest.raises(ValueError, match="not a configured city"):
        geo.resolve("India", hq_city="atlantis")


@pytest.mark.parametrize(
    ("pin", "city"),
    [
        ("500081", "hyderabad"),
        ("560034", "bengaluru"),
        ("562125", "bengaluru"),
        ("600096", "chennai"),
        ("400076", "mumbai"),
        ("410210", "mumbai"),  # Kharghar via 4-digit prefix 4102
        ("411057", "pune"),
        ("410501", "pune"),  # Chakan via 4-digit prefix 4105
        ("122002", "delhi_ncr"),
        ("201301", "delhi_ncr"),
        ("700091", "kolkata"),
        ("380015", "ahmedabad"),
        ("682001", None),  # Kochi: valid PIN, no configured city
        ("12345", None),
    ],
)
def test_city_for_pin(geo: GeoResolver, pin: str, city: str | None) -> None:
    assert geo.city_for_pin(pin) == city


@pytest.mark.parametrize("text", ["+91 98765 43210", "ID 9500812", "year 2026", "50008"])
def test_pin_regex_ignores_non_pin_numbers(geo: GeoResolver, text: str) -> None:
    assert geo.find_pin_city(text) is None


# ---------------------------------------------------------------- distances
def test_haversine_hyderabad_to_bengaluru() -> None:
    assert 480 < haversine_km(17.3850, 78.4867, 12.9716, 77.5946) < 520
    assert haversine_km(1.0, 2.0, 1.0, 2.0) == 0


def test_cities_within_radius(geo: GeoResolver) -> None:
    assert geo.cities_within(17.44, 78.38, 50) == ["hyderabad"]  # near Gachibowli
    assert geo.cities_within(19.0, 73.0, 200)[:2] == ["mumbai", "pune"]


# ---------------------------------------------------------------- config validation
@pytest.fixture
def raw_geo_config() -> dict:
    return yaml.safe_load((CONFIG_DIR / "india_geo.yaml").read_text(encoding="utf-8"))


def test_real_config_loads_all_target_cities(raw_geo_config: dict) -> None:
    config = GeoConfig.model_validate(raw_geo_config)
    expected = {"hyderabad", "bengaluru", "chennai", "mumbai", "delhi_ncr", "pune", "kolkata"}
    assert expected | {"ahmedabad"} <= set(config.cities)


def test_config_rejects_term_mapped_to_two_cities(raw_geo_config: dict) -> None:
    bad = copy.deepcopy(raw_geo_config)
    bad["cities"]["pune"]["neighborhoods"].append("Gachibowli")
    with pytest.raises(ValidationError, match="maps to both"):
        GeoConfig.model_validate(bad)


def test_config_rejects_duplicate_pin_prefix(raw_geo_config: dict) -> None:
    bad = copy.deepcopy(raw_geo_config)
    bad["cities"]["pune"]["pin_prefixes"].append("500")
    with pytest.raises(ValidationError, match="PIN prefix"):
        GeoConfig.model_validate(bad)


def test_config_rejects_reserved_slug(raw_geo_config: dict) -> None:
    bad = copy.deepcopy(raw_geo_config)
    bad["cities"]["remote_india"] = bad["cities"].pop("pune")
    with pytest.raises(ValidationError, match="reserved"):
        GeoConfig.model_validate(bad)


def test_config_rejects_unknown_keys(raw_geo_config: dict) -> None:
    bad = copy.deepcopy(raw_geo_config)
    bad["cities"]["pune"]["alias"] = ["typo"]
    with pytest.raises(ValidationError):
        GeoConfig.model_validate(bad)


def test_new_city_from_config_needs_no_code(raw_geo_config: dict) -> None:
    extended = copy.deepcopy(raw_geo_config)
    extended["cities"]["jaipur"] = {
        "display_name": "Jaipur",
        "state": "Rajasthan",
        "lat": 26.9124,
        "lng": 75.7873,
        "aliases": ["Jaipur", "Pink City"],
        "pin_prefixes": ["302"],
    }
    extended["states"]["Rajasthan"] = ["Rajasthan"]  # move Jaipur out of the state-only list
    resolver = GeoResolver(GeoConfig.model_validate(extended))
    assert resolver.resolve("Pink City").city_canonical == "jaipur"
    assert resolver.city_for_pin("302017") == "jaipur"
