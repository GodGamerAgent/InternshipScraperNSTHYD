"""Offline tests for the optional UI: view models, job runner, `scraper ui`, layering rules.

Runs without NiceGUI installed (CI); the single app smoke test is skipped when the extra is absent.
"""

import csv
import importlib.util
import io
import json
import logging
import re
import sys
import types
from datetime import UTC, date, datetime

import pytest
from typer.testing import CliRunner

import src.cli as cli
from src.core.config import PROJECT_ROOT, Settings
from src.enrichment.geo import GeoResolver
from src.enrichment.scorer import ScoringConfig
from src.models import Contact, Opportunity, Startup, StartupLead, WorkMode
from src.storage import Repository
from src.ui import jobs
from src.ui.view_models import (
    TABLE_COLUMNS,
    UNCONFIRMED_NOTE,
    UiFilters,
    city_labels,
    contact_rows,
    download_payload,
    duration_label,
    format_inr,
    hidden_opportunity_rows,
    internship_rows,
    kpi_cards,
    ppo_label,
    score_band,
    stipend_label,
)
from tests.conftest import CONFIG_DIR, FakeTime, make_client
from tests.test_cli import FakeMx, Web, web

__all__ = ["web"]  # pytest fixture re-used from test_cli
TODAY = date(2026, 11, 26)


@pytest.fixture(autouse=True)
def _restore_logging():
    root = logging.getLogger()
    saved = (root.handlers[:], root.level)
    yield
    root.handlers[:] = saved[0]
    root.setLevel(saved[1])


@pytest.fixture(scope="module")
def labels(geo: GeoResolver) -> dict[str, str]:
    return city_labels(geo)


@pytest.fixture(scope="module")
def thresholds():
    return ScoringConfig.load(CONFIG_DIR).thresholds


@pytest.fixture
def ui_repo(repo: Repository) -> dict[str, int]:
    hyd = repo.upsert_startup(
        Startup(name="HydStack", canonical_domain="hydstack.example", hq_city_canonical="hyderabad",
                hidden_score=85, funding_stage="seed", last_funded_at=date(2026, 11, 1),
                open_to_cold_pitch=True, github_org="hydstack-oss", tech_stack=["python"])
    )  # fmt: skip
    blr = repo.upsert_startup(
        Startup(name="BlrPay", canonical_domain="blrpay.example", hq_city_canonical="bengaluru",
                hidden_score=40)
    )  # fmt: skip
    repo.upsert_startup(Startup(name="Unscored", canonical_domain="unscored.example"))

    def opp(sid: int, n: int, **kw) -> Opportunity:
        base = {"startup_id": sid, "source": "greenhouse", "title": f"Role {n}",
                "url": f"https://boards.example/{n}"}  # fmt: skip
        return Opportunity.model_validate(base | kw)

    repo.upsert_opportunities(
        [
            opp(hyd, 1, title="Backend Intern", city_canonical="hyderabad", work_mode="onsite",
                stipend_inr_min=20000, stipend_inr_max=30000, has_ppo=True, duration_months=6,
                description="Python and FastAPI"),
            opp(hyd, 2, title="Design Intern", city_canonical="hyderabad", work_mode="hybrid",
                is_paid=False),
            opp(blr, 3, title="Data Intern", city_canonical="remote_india",
                work_mode="remote_india",
                stipend_inr_max=150000),
            opp(blr, 4, title="=SUM(A1:A9) Intern", city_canonical="bengaluru", work_mode="onsite"),
        ]
    )  # fmt: skip
    repo.upsert_contacts(
        [
            Contact(startup_id=hyd, email="founder@hydstack.example",
                    source_method="pattern_mx_verified", confidence_score=0.5, verified_mx=True,
                    full_name="Asha Rao", role="Founder"),
            Contact(startup_id=hyd, email="careers@hydstack.example", source_method="direct_public",
                    confidence_score=0.95, role="Careers"),
            Contact(startup_id=blr, profile_url="https://github.com/blr-dev",
                    source_method="github_public", confidence_score=0.85, full_name="Blr Dev"),
        ]
    )  # fmt: skip
    repo.upsert_lead(StartupLead(name="Lead Co", source="entrackr"))
    return {"hyd": hyd, "blr": blr}


# ---------------------------------------------------------------- filters & formatting
def test_filters_map_to_export_filter() -> None:
    flt = UiFilters(cities=("hyderabad",), work_modes=("hybrid",), include_remote=True,
                    min_hidden_score=60).to_export_filter()  # fmt: skip
    assert flt.cities == frozenset({"hyderabad"})
    assert flt.work_modes == frozenset({WorkMode.HYBRID})
    assert flt.include_remote is True
    assert flt.min_hidden_score == 60
    empty = UiFilters().to_export_filter()
    assert (empty.cities, empty.work_modes, empty.min_hidden_score) == (None, None, None)


def test_keyword_matches_every_word_case_insensitive() -> None:
    f = UiFilters(keyword="python  HYDERABAD")
    assert f.matches_keyword("Backend Intern", "Hyderabad", "Python and FastAPI")
    assert not f.matches_keyword("Backend Intern", "Hyderabad")
    assert UiFilters().matches_keyword()


@pytest.mark.parametrize(
    ("amount", "text"),
    [(999, "₹999"), (15000, "₹15,000"), (150000, "₹1,50,000"), (12345678, "₹1,23,45,678")],
)
def test_format_inr_indian_grouping(amount: int, text: str) -> None:
    assert format_inr(amount) == text


@pytest.mark.parametrize(
    ("args", "text"),
    [
        ((15000, 25000, True), "₹15,000–₹25,000/mo"),
        ((20000, 20000, True), "₹20,000/mo"),
        ((None, 40000, True), "up to ₹40,000/mo"),
        ((None, None, False), "Unpaid"),
        ((None, None, True), "Paid (amount not listed)"),
        ((None, None, None), "—"),
    ],
)
def test_stipend_label(args, text: str) -> None:
    assert stipend_label(*args) == text


def test_small_labels(thresholds) -> None:
    assert (duration_label(6), duration_label(1), duration_label(2.8), duration_label(None)) == (
        "6 months", "1 month", "2.8 months", "—",
    )  # fmt: skip
    assert (ppo_label(True), ppo_label(False), ppo_label(None)) == ("PPO", "No PPO", "—")
    assert [score_band(s, thresholds) for s in (90, 40, 10, None)] == [
        "high", "medium", "low", "unscored",
    ]  # fmt: skip


def test_city_labels(labels: dict[str, str]) -> None:
    assert labels["delhi_ncr"] == "Delhi NCR"
    assert labels["remote_india"] == "Remote (India)"
    assert labels["unknown"] == "—"


# ---------------------------------------------------------------- rows
def test_internship_rows_and_top_contact(repo: Repository, ui_repo, labels) -> None:
    rows = internship_rows(repo, UiFilters(), labels=labels)
    by_role = {r["role"]: r for r in rows}
    backend = by_role["Backend Intern"]
    assert backend["startup"] == "HydStack"
    assert backend["city"] == "Hyderabad"
    assert backend["work_mode"] == "On-site"
    assert backend["stipend"] == "₹20,000–₹30,000/mo"
    assert backend["duration"] == "6 months"
    assert backend["ppo"] == "PPO"
    assert backend["apply_url"] == "https://boards.example/1"
    assert backend["contact"] == "careers@hydstack.example"  # published email beats a pattern guess
    assert by_role["Data Intern"]["stipend"] == "up to ₹1,50,000/mo"
    assert by_role["Data Intern"]["contact"] == "https://github.com/blr-dev"
    assert set(rows[0]) >= {c["field"] for c in TABLE_COLUMNS["internships"]}


@pytest.mark.parametrize(
    ("filters", "roles"),
    [
        (UiFilters(paid_only=True), {"Backend Intern", "Data Intern"}),
        (UiFilters(ppo_only=True), {"Backend Intern"}),
        (UiFilters(keyword="fastapi"), {"Backend Intern"}),
        (UiFilters(cities=("hyderabad",)), {"Backend Intern", "Design Intern"}),
        (UiFilters(cities=("bengaluru",), include_remote=True),
         {"Data Intern", "=SUM(A1:A9) Intern"}),
        (UiFilters(work_modes=("hybrid",)), {"Design Intern"}),
        (UiFilters(min_hidden_score=60), {"Backend Intern", "Design Intern"}),
    ],
)  # fmt: skip
def test_internship_filters(repo: Repository, ui_repo, labels, filters, roles) -> None:
    assert {r["role"] for r in internship_rows(repo, filters, labels=labels)} == roles


def test_hidden_opportunity_rows(repo: Repository, ui_repo, labels, thresholds) -> None:
    rows = hidden_opportunity_rows(repo, UiFilters(), labels=labels, thresholds=thresholds)
    assert [(r["rank"], r["startup"], r["score"]) for r in rows] == [
        (1, "HydStack", 85),
        (2, "BlrPay", 40),
    ]  # unscored startups are not "hidden opportunities"
    top = rows[0]
    assert top["label"] == "high"
    assert top["funding"] == "Seed · 2026-11-01"
    assert top["cold_pitch"] == "Yes"
    assert top["github_url"] == "https://github.com/hydstack-oss"
    assert top["open_internships"] == 2
    assert "careers@hydstack.example" in top["contacts"]
    assert "founder@hydstack.example (unconfirmed guess)" in top["contacts"]
    assert top["website"] == "https://hydstack.example"
    high_only = hidden_opportunity_rows(
        repo, UiFilters(min_hidden_score=60), labels=labels, thresholds=thresholds
    )
    assert [r["startup"] for r in high_only] == ["HydStack"]


def test_contact_rows_flag_pattern_guesses(repo: Repository, ui_repo) -> None:
    rows = contact_rows(repo, UiFilters())
    guess = next(r for r in rows if r["is_guess"])
    assert guess["contact"] == "founder@hydstack.example (unconfirmed guess)"
    assert guess["confidence"] == "0.50"
    assert guess["note"] == UNCONFIRMED_NOTE
    assert guess["method"] == "Pattern guess"
    only_direct = contact_rows(repo, UiFilters(), methods=("direct_public",))
    assert [r["contact"] for r in only_direct] == ["careers@hydstack.example"]
    verified = contact_rows(repo, UiFilters(), verified=True)
    assert [r["is_guess"] for r in verified] == [True]
    not_verified = contact_rows(repo, UiFilters(), verified=False)
    assert len(not_verified) == 2
    assert [r["startup"] for r in contact_rows(repo, UiFilters(min_hidden_score=60))] == [
        "HydStack", "HydStack",
    ]  # fmt: skip


def test_kpi_cards(repo: Repository, ui_repo) -> None:
    cards = {c.label: c.value for c in kpi_cards(repo.stats())}
    assert cards == {
        "Startups": 3,
        "Open opportunities": 4,
        "Hidden leads": 1,
        "Public contacts": 3,
    }
    assert [c.label for c in kpi_cards({})] == list(cards)  # empty DB never crashes


# ---------------------------------------------------------------- downloads
def test_csv_download_payload_is_formula_safe(repo: Repository, ui_repo, labels) -> None:
    rows = internship_rows(repo, UiFilters(), labels=labels)
    payload = download_payload(rows, table="internships", fmt="csv", today=TODAY)
    assert payload.filename == "internships_20261126.csv"
    assert payload.media_type == "text/csv"
    assert payload.rows == 4
    records = list(csv.DictReader(io.StringIO(payload.content.decode("utf-8"))))
    assert list(records[0]) == [c["field"] for c in TABLE_COLUMNS["internships"]]
    roles = {r["role"] for r in records}
    assert "'=SUM(A1:A9) Intern" in roles  # never executable in a spreadsheet
    assert "₹20,000–₹30,000/mo" in {r["stipend"] for r in records}


def test_json_download_payload(repo: Repository, ui_repo, labels, thresholds) -> None:
    rows = hidden_opportunity_rows(repo, UiFilters(), labels=labels, thresholds=thresholds)
    payload = download_payload(rows, table="hidden", fmt="json", today=TODAY)
    assert (payload.filename, payload.media_type) == ("hidden_20261126.json", "application/json")
    data = json.loads(payload.content)
    assert list(data[0]) == [c["field"] for c in TABLE_COLUMNS["hidden"]]
    assert data[0]["score"] == 85  # numbers stay numbers in JSON
    empty = download_payload([], table="contacts", fmt="csv", today=TODAY)
    assert empty.content.decode().strip() == ",".join(c["field"] for c in TABLE_COLUMNS["contacts"])
    with pytest.raises(ValueError, match="csv or json"):
        download_payload([], table="contacts", fmt="xlsx", today=TODAY)  # type: ignore[arg-type]


# ---------------------------------------------------------------- jobs (mocked HTTP / DNS)
async def test_scrape_job_reports_and_streams_logs(web: Web) -> None:
    lines: list[str] = []
    result = await jobs.scrape_job(
        Settings.from_env(),
        names=["greenhouse", "lever"],
        http_factory=lambda s: make_client(web.handler, FakeTime()),
        on_line=lines.append,
    )
    assert result.ok
    assert result.error is None
    assert [(r["collector"], r["opportunities"]) for r in result.summary] == [
        ("greenhouse", 3),
        ("lever", 2),
    ]
    assert any("collector.finished" in line for line in lines)
    assert not jobs.is_running()
    assert not logging.getLogger("src").handlers  # capture handler removed afterwards


async def test_enrich_job_scores(web: Web) -> None:
    factory = lambda s: make_client(web.handler, FakeTime())  # noqa: E731
    await jobs.scrape_job(Settings.from_env(), names=["greenhouse"], http_factory=factory)
    result = await jobs.enrich_job(
        Settings.from_env(), limit=5, http_factory=factory, resolver_factory=FakeMx
    )
    assert result.ok
    assert result.summary[0]["domain"] == "examplestack.example"
    assert result.summary[0]["score"] is not None


async def test_jobs_never_raise(web: Web) -> None:
    def broken(settings: Settings):
        raise RuntimeError("no network today")

    result = await jobs.scrape_job(Settings.from_env(), http_factory=broken)
    assert not result.ok
    assert "RuntimeError: no network today" in (result.error or "")
    web.fail_everything = True
    failed = await jobs.scrape_job(
        Settings.from_env(),
        names=["lever"],
        http_factory=lambda s: make_client(web.handler, FakeTime()),
    )
    assert not failed.ok  # every target failed


# ---------------------------------------------------------------- `scraper ui`
runner = CliRunner()


def test_ui_command_explains_missing_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    real = importlib.util.find_spec
    monkeypatch.setattr(
        cli.importlib.util, "find_spec", lambda name, *a: None if name == "nicegui" else real(name)
    )
    result = runner.invoke(cli.app, ["ui"])
    assert result.exit_code == 1
    assert "uv sync --extra ui" in result.output


@pytest.fixture
def fake_app(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Stand-in for ``src.ui.app`` so the launch path is tested without starting a server."""
    calls: list[dict] = []
    module = types.ModuleType("src.ui.app")
    module.run_ui = lambda settings, **kw: calls.append({"settings": settings, **kw})  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "src.ui.app", module)
    monkeypatch.setattr(cli.importlib.util, "find_spec", lambda name, *a: object())
    return calls


def test_ui_command_defaults_to_loopback(fake_app: list[dict], tmp_path) -> None:
    result = runner.invoke(cli.app, ["--db", str(tmp_path / "x.db"), "ui", "--no-open"])
    assert result.exit_code == 0, result.output
    (call,) = fake_app
    assert (call["host"], call["port"], call["show"]) == ("127.0.0.1", 8080, False)
    assert call["settings"].db_path == (tmp_path / "x.db").resolve()
    assert "http://127.0.0.1:8080" in result.output


@pytest.mark.parametrize("host", ["0.0.0.0", "::", ""])
def test_ui_command_refuses_wildcard_hosts(fake_app: list[dict], host: str) -> None:
    result = runner.invoke(cli.app, ["ui", "--host", host])
    assert result.exit_code == 2
    assert fake_app == []


def test_ui_command_warns_for_lan_hosts(fake_app: list[dict]) -> None:
    result = runner.invoke(cli.app, ["ui", "--host", "192.168.1.20", "--port", "9000", "--no-open"])
    assert result.exit_code == 0
    assert "reachable from your network" in result.output
    assert fake_app[0]["port"] == 9000


# ---------------------------------------------------------------- layering guardrails
def test_ui_layer_never_touches_sql_or_http_directly() -> None:
    forbidden = re.compile(
        r"^\s*(?:import|from)\s+(?:httpx|sqlalchemy|sqlmodel|sqlite3|src\.storage\.(?:tables|db|repository))\b",
        re.M,
    )
    for path in (PROJECT_ROOT / "src" / "ui").glob("*.py"):
        assert not forbidden.search(path.read_text()), path.name
    for path in (PROJECT_ROOT / "src").rglob("*.py"):
        if path.name != "app.py" or path.parent.name != "ui":
            assert not re.search(r"^\s*(?:import|from)\s+nicegui\b", path.read_text(), re.M), path


def test_app_module_binds_loopback_and_disables_reload(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("nicegui")
    import src.ui.app as app_module

    captured: dict = {}
    monkeypatch.setattr(app_module.ui, "run", lambda root, **kw: captured.update(root=root, **kw))
    app_module.run_ui(Settings.from_env({}), show=False)
    assert captured["root"] is app_module.build_page
    assert captured["host"] == "127.0.0.1"  # NiceGUI's own default would be 0.0.0.0
    assert captured["reload"] is False
    assert datetime.now(UTC)  # keep DTZ-safe imports used
