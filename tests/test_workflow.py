"""Static checks for the daily GitHub Actions workflow (it cannot run offline)."""

import re
import shlex

import pytest
import yaml

from src.collectors.registry import ALL_COLLECTOR_NAMES
from src.core.config import PROJECT_ROOT

WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "daily_scrape.yml"


@pytest.fixture(scope="module")
def workflow() -> dict:
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    data["on"] = data.pop(True, data.get("on"))  # PyYAML parses the bare key `on` as True
    return data


def steps(workflow: dict) -> list[dict]:
    return workflow["jobs"]["scrape"]["steps"]


def run_text(workflow: dict) -> str:
    return "\n".join(s.get("run", "") for s in steps(workflow))


def test_schedule_is_daily_ist_morning(workflow: dict) -> None:
    (entry,) = workflow["on"]["schedule"]
    minute, hour, *rest = entry["cron"].split()
    assert rest == ["*", "*", "*"]
    ist_minutes = (int(hour) * 60 + int(minute) + 330) % (24 * 60)  # UTC+5:30
    assert 6 * 60 <= ist_minutes <= 8 * 60
    assert "workflow_dispatch" in workflow["on"]


def test_pipeline_order_and_quality_gate(workflow: dict) -> None:
    text = run_text(workflow)
    order = [
        "uv sync --frozen",
        "ruff check",
        "ruff format --check",
        "pytest",
        "scraper scrape --dry-run",
        "scraper scrape ",
        "scraper enrich",
        "scraper export",
    ]
    positions = [text.index(marker) for marker in order]
    assert positions == sorted(positions)


def test_cache_and_artifacts(workflow: dict) -> None:
    uses = [s.get("uses", "") for s in steps(workflow)]
    assert any(u.startswith("actions/cache/restore@") for u in uses)
    assert any(u.startswith("actions/cache/save@") for u in uses)
    assert any(u.startswith("actions/upload-artifact@") for u in uses)
    env = workflow["jobs"]["scrape"]["env"]
    assert env["SCRAPER_HTTP_CACHE_DIR"].startswith("data/")
    assert env["SCRAPER_DB_PATH"].startswith("data/")
    save = next(s for s in steps(workflow) if s.get("uses", "").startswith("actions/cache/save@"))
    assert save["with"]["path"] == "data"


def test_secrets_only_via_env(workflow: dict) -> None:
    env = workflow["jobs"]["scrape"]["env"]
    assert "secrets.FIRECRAWL_API_KEY" in env["FIRECRAWL_API_KEY"]
    assert "secrets.GITHUB_TOKEN" in env["GITHUB_TOKEN"]
    # No ${{ ... }} expression is interpolated directly into a shell command (injection-safe).
    for step in steps(workflow):
        assert "${{" not in step.get("run", ""), step.get("name")
    assert workflow["permissions"] == {"contents": "read"}


def test_contacts_export_is_opt_in(workflow: dict) -> None:
    text = run_text(workflow)
    contacts_line = next(line for line in text.splitlines() if "--what contacts" in line)
    assert contacts_line.startswith(" ")  # inside the EXPORT_CONTACTS guard
    assert 'if [ "$EXPORT_CONTACTS" = "true" ]' in text


def test_cli_invocations_use_known_flags(workflow: dict) -> None:
    """Every `scraper` call in the workflow parses with the real CLI flag set."""
    known_flags = {
        "--what", "--cities", "--include-remote", "--format", "--out", "--status",
        "--min-hidden-score", "--work-mode", "--limit", "--dry-run", "--sources",
    }  # fmt: skip
    text = run_text(workflow).replace("\\\n", " ")
    for line in text.splitlines():
        if "uv run scraper" not in line:
            continue
        tokens = shlex.split(re.sub(r"\$\{SOURCES:\+[^}]*\}", "", line))
        flags = {t for t in tokens if t.startswith("--")}
        assert flags <= known_flags, line
    assert "hn_hiring" in ALL_COLLECTOR_NAMES  # dispatch input documents real collector names
