"""Sanity checks so a bad YAML edit fails in CI, not in production scrapes."""

import re
from pathlib import Path

import pytest

from src.core.config import ConfigError, Settings, load_yaml
from tests.conftest import CONFIG_DIR


def test_sources_regexes_compile_and_match_examples() -> None:
    sources = load_yaml("sources", CONFIG_DIR)
    include = [re.compile(p, re.IGNORECASE) for p in sources["internship"]["include_patterns"]]
    exclude = [
        re.compile(p, re.IGNORECASE) for p in sources["internship"]["exclude_title_patterns"]
    ]

    def is_internship(title: str) -> bool:
        return any(p.search(title) for p in include) and not any(p.search(title) for p in exclude)

    assert is_internship("Software Engineering Intern (6 months, PPO)")
    assert is_internship("Summer Internship 2027 - Data")
    assert not is_internship("International Sales Manager")
    assert not is_internship("Senior Engineer - mentor interns")
    assert not is_internship("Internal Tools Engineer")


def test_ats_endpoints_have_slug_placeholder() -> None:
    ats = load_yaml("sources", CONFIG_DIR)["ats"]
    assert set(ats) == {
        "greenhouse", "lever", "ashby", "workable", "rippling", "breezyhr", "recruitee",
        "zohorecruit",
    }  # fmt: skip
    for provider in ats.values():
        assert "{slug}" in provider["endpoint"]
        assert isinstance(provider["companies"], list)


def test_scoring_points_sum_to_max_score() -> None:
    scoring = load_yaml("scoring", CONFIG_DIR)
    assert sum(s["points"] for s in scoring["signals"].values()) == scoring["max_score"] == 100
    assert scoring["contact_confidence"] == {
        "direct_public": 0.95,
        "github_public": 0.85,
        "pattern_mx_verified": 0.50,
    }


def test_load_yaml_errors(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_yaml("missing", tmp_path)
    (tmp_path / "list.yaml").write_text("- just\n- a list\n")
    with pytest.raises(ConfigError, match="mapping"):
        load_yaml("list", tmp_path)
    (tmp_path / "broken.yaml").write_text("key: [unclosed\n")
    with pytest.raises(ConfigError, match="Invalid YAML"):
        load_yaml("broken", tmp_path)


def test_settings_from_env_resolves_relative_paths() -> None:
    settings = Settings.from_env({"SCRAPER_DB_PATH": "tmp/x.db", "GITHUB_TOKEN": "  "})
    assert settings.db_path.is_absolute()
    assert settings.db_path.name == "x.db"
    assert settings.github_token is None  # blank counts as unset
    assert settings.config_dir.name == "config"
    assert settings.log_level == "INFO"
