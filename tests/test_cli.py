"""Offline end-to-end CLI tests: Typer ``CliRunner`` + temp config/DB + mocked HTTP/DNS."""

import csv
import io
import json
import logging
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from typer.testing import CliRunner

import src.cli as cli
from src.cli import app
from tests.conftest import CONFIG_DIR, FIXTURES, FakeTime, make_client

runner = CliRunner()
SITE_PAGES = {"/": "home.html", "/careers": "careers.html", "/about": "about.html"}


class FakeMx:
    async def has_mx(self, domain: str) -> bool:
        return True


class Web:
    """Mutable fake internet: ATS boards by slug, one startup site, GitHub API."""

    def __init__(self) -> None:
        self.boards: dict[str, Any] = {
            "examplelabs": json.loads((FIXTURES / "greenhouse/board_examplelabs.json").read_text()),
            "examplepay": json.loads((FIXTURES / "lever/postings_examplepay.json").read_text()),
        }
        self.requests: list[str] = []
        self.fail_everything = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        self.requests.append(f"{host}{path}")
        if self.fail_everything:
            return httpx.Response(503)
        if path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        for slug, board in self.boards.items():
            if slug in path:
                return httpx.Response(200, json=board)
        if host == "examplestack.example" and path in SITE_PAGES:
            return httpx.Response(200, text=(FIXTURES / "site" / SITE_PAGES[path]).read_text())
        if host == "api.github.com":
            gh = FIXTURES / "github"
            if path.endswith("/repos"):
                return httpx.Response(200, text=(gh / "repos.json").read_text())
            if path.endswith("/public_members"):
                return httpx.Response(200, text=(gh / "members.json").read_text())
            if path.startswith("/users/"):
                users = json.loads((gh / "users.json").read_text())
                return httpx.Response(200, json=users[path.rsplit("/", 1)[-1]])
            return httpx.Response(200, text=(gh / "org.json").read_text())
        return httpx.Response(404)


@pytest.fixture(autouse=True)
def _restore_logging():
    """The CLI reconfigures root logging onto CliRunner's (later closed) stream; undo it."""
    root = logging.getLogger()
    saved = (root.handlers[:], root.level)
    yield
    root.handlers[:] = saved[0]
    root.setLevel(saved[1])


@pytest.fixture
def web(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Web:
    """Temp config (fictional seeds), temp DB/cache, mocked HTTP + DNS."""
    config = tmp_path / "config"
    shutil.copytree(CONFIG_DIR, config)
    sources = yaml.safe_load((config / "sources.yaml").read_text())
    seeds = {
        "greenhouse": [{"name": "ExampleStack", "slug": "examplelabs",
                        "domain": "examplestack.example", "hq_city": "hyderabad"}],
        "lever": [{"name": "ExamplePay", "slug": "examplepay", "domain": "examplepay.example",
                   "hq_city": "bengaluru"}],
        "ashby": [],
        "workable": [],
    }  # fmt: skip
    for provider, companies in seeds.items():
        sources["ats"][provider]["companies"] = companies
    (config / "sources.yaml").write_text(yaml.safe_dump(sources, allow_unicode=True))

    monkeypatch.setenv("SCRAPER_DB_PATH", str(tmp_path / "scraper.db"))
    monkeypatch.setenv("SCRAPER_CONFIG_DIR", str(config))
    monkeypatch.setenv("SCRAPER_HTTP_CACHE_DIR", str(tmp_path / "http_cache"))
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    fake_web = Web()
    monkeypatch.setattr(
        cli, "make_http_client", lambda s: make_client(fake_web.handler, FakeTime())
    )
    monkeypatch.setattr(cli, "make_mx_resolver", FakeMx)

    return fake_web


def invoke(*args: str):
    result = runner.invoke(app, list(args))
    return result


def csv_rows(text: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(text)))


# ---------------------------------------------------------------- basics
def test_version() -> None:
    result = invoke("version")
    assert result.exit_code == 0
    assert result.output.strip() == "0.1.0"


def test_scrape_dry_run_makes_no_requests(web: Web, tmp_path: Path) -> None:
    result = invoke("scrape", "--dry-run", "--sources", "greenhouse,lever,hn_hiring")
    assert result.exit_code == 0, result.output
    assert "greenhouse (1 targets): examplelabs" in result.output
    assert "lever (1 targets): examplepay" in result.output
    assert "hn_hiring (1 targets): latest_thread" in result.output
    assert web.requests == []
    assert not (tmp_path / "scraper.db").exists()  # dry run never creates the database


def test_scrape_dry_run_accepts_v02_sources(web: Web) -> None:
    result = invoke("scrape", "--dry-run", "--sources", "zohorecruit,yc_india,iitm,nsrcel")
    assert result.exit_code == 0, result.output
    assert "yc_india (1 targets): india" in result.output
    assert "iitm (1 targets): iitm_incubation" in result.output
    assert "nsrcel (1 targets): nsrcel" in result.output
    assert "zohorecruit (" in result.output
    assert web.requests == []


def test_scrape_dry_run_accepts_remote_feeds(web: Web) -> None:
    result = invoke("scrape", "--dry-run", "--sources", "himalayas,remotive")
    assert result.exit_code == 0, result.output
    assert "himalayas (1 targets): search" in result.output
    assert "remotive (1 targets): feed" in result.output
    assert web.requests == []


def test_scrape_dry_run_accepts_student_portals(web: Web) -> None:
    result = invoke("scrape", "--dry-run", "--sources", "internshala,cutshort,instahyre")
    assert result.exit_code == 0, result.output
    line = next(x for x in result.output.splitlines() if x.startswith("internshala (1 targets): "))
    assert set(line.split(": ", 1)[1].split(",")) == {"bengaluru", "hyderabad", "chennai", "remote"}
    assert "cutshort (1 targets):" in result.output
    assert "instahyre (1 targets): internships" in result.output
    assert web.requests == []


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["scrape", "--sources", "linkedin"], "unknown source"),
        (["export", "--cities", "atlantis"], "unknown city"),
        (["export", "--work-mode", "underwater"], "unknown work mode"),
        (["export", "--min-hidden-score", "101"], "--min-hidden-score"),
        (["export", "--format", "xlsx"], "xlsx"),
        (["enrich", "--limit", "0"], "--limit"),
    ],
)
def test_invalid_arguments_exit_2(web: Web, args: list[str], message: str) -> None:
    result = invoke(*args)
    assert result.exit_code == 2
    assert message in result.output
    assert web.requests == []


# ---------------------------------------------------------------- scrape -> stats -> export
def test_scrape_stats_export_and_close(web: Web, tmp_path: Path) -> None:
    result = invoke("scrape", "--sources", "greenhouse,lever")
    assert result.exit_code == 0, result.output
    assert "greenhouse" in result.output
    assert "lever" in result.output

    stats = json.loads(invoke("stats", "--json").output)
    assert stats["counts"]["opportunities"] == 5  # 3 Greenhouse + 2 Lever internships
    assert stats["open_by_source"] == {"greenhouse": 3, "lever": 2}

    out = tmp_path / "hyd.csv"
    result = invoke(
        "export", "--what", "opportunities", "--cities", "hyderabad", "--include-remote",
        "--format", "csv", "--out", str(out),
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    assert f"Wrote 3 opportunities to {out}" in result.output
    titles = {row["title"] for row in csv_rows(out.read_text())}
    assert titles == {"Product Design", "Growth Intern", "Backend Engineer"}

    hybrid = json.loads(
        invoke("export", "--work-mode", "hybrid", "--format", "json", "--out", "-").output
    )
    assert [(r["title"], r["company"]) for r in hybrid] == [
        ("Intern - Product Analytics", "ExamplePay")
    ]

    # "Growth Intern" disappears from the Greenhouse board -> closed on the next run.
    board = web.boards["examplelabs"]
    board["jobs"] = [j for j in board["jobs"] if j.get("title") != "Growth Intern"]
    result = invoke("scrape", "--sources", "greenhouse")
    assert result.exit_code == 0, result.output
    open_titles = {r["title"] for r in csv_rows(invoke("export", "--out", "-").output)}
    assert "Growth Intern" not in open_titles
    assert "Backend Engineer" in open_titles  # Lever board untouched by a Greenhouse run
    closed = csv_rows(invoke("export", "--status", "closed", "--out", "-").output)
    assert [(r["title"], r["status"]) for r in closed] == [("Growth Intern", "closed")]
    assert len(csv_rows(invoke("export", "--status", "all", "--out", "-").output)) == 5


def test_scrape_exit_code_when_every_target_fails(web: Web) -> None:
    web.fail_everything = True
    result = invoke("scrape", "--sources", "greenhouse,lever")
    assert result.exit_code == 1
    assert "fetch FetchError" in result.output


def test_scrape_city_filter_only_polls_matching_seeds(web: Web) -> None:
    result = invoke("scrape", "--sources", "greenhouse,lever", "--cities", "bengaluru")
    assert result.exit_code == 0, result.output
    assert not any("examplelabs" in r for r in web.requests)  # Hyderabad seed skipped
    assert any("examplepay" in r for r in web.requests)


# ---------------------------------------------------------------- enrich -> contacts export
def test_enrich_scores_and_exports_contacts(web: Web, tmp_path: Path) -> None:
    assert invoke("scrape", "--sources", "greenhouse").exit_code == 0
    result = invoke("enrich", "--limit", "5")
    assert result.exit_code == 0, result.output
    assert "examplestack.example" in result.output

    stats = json.loads(invoke("stats", "--json").output)
    assert stats["startups_by_score"]["unscored"] == 0
    assert stats["contacts_by_method"]["direct_public"] >= 3

    startups = csv_rows(invoke("export", "--what", "startups", "--out", "-").output)
    (stack,) = startups
    assert stack["ats_provider"] == "greenhouse"
    assert stack["github_org"] == "examplestack-oss"
    assert stack["open_to_cold_pitch"] == "yes"
    assert int(stack["hidden_score"]) >= 60

    contacts = csv_rows(
        invoke("export", "--what", "contacts", "--min-hidden-score", "60", "--out", "-").output
    )
    emails = {row["email"] for row in contacts if row["email"]}
    assert "careers@examplestack.example" in emails
    assert not any(e.endswith("@gmail.com") for e in emails)

    # Text stats also render.
    text = invoke("stats").output
    assert "Startups by hidden score" in text
    assert "Open roles by city" in text


def test_enrich_on_empty_db(web: Web) -> None:
    result = invoke("enrich")
    assert result.exit_code == 0
    assert "No startups to enrich yet" in result.output


def test_export_default_path(web: Web, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = invoke("export", "--what", "leads", "--format", "json")
    assert result.exit_code == 0, result.output
    expected = tmp_path / "exports" / f"leads_{datetime.now(UTC):%Y%m%d}.json"
    assert json.loads(expected.read_text()) == []


def test_global_db_option_overrides_env(web: Web, tmp_path: Path) -> None:
    other = tmp_path / "other.db"
    assert invoke("--db", str(other), "scrape", "--sources", "lever").exit_code == 0
    assert other.exists()
    stats = json.loads(invoke("--db", str(other), "stats", "--json").output)
    assert stats["counts"]["opportunities"] == 2
    assert json.loads(invoke("stats", "--json").output)["counts"]["opportunities"] == 0
