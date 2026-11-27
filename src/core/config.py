"""Runtime settings from environment variables and loading of the YAML files in ``config/``.

Relative paths (``SCRAPER_DB_PATH``, ``SCRAPER_CONFIG_DIR``) resolve against the project root so
the CLI behaves the same no matter which directory it is launched from.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]
DEFAULT_USER_AGENT = "india-internship-scraper/0.1 (+https://github.com/your-org/your-repo)"


class ConfigError(RuntimeError):
    """Raised when a config file is missing, unreadable, or structurally invalid."""


def _resolve(path_value: str) -> Path:
    path = Path(path_value).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


@dataclass(frozen=True, slots=True)
class Settings:
    """Process-wide settings. Build with :meth:`from_env`; never read ``os.environ`` elsewhere."""

    db_path: Path
    config_dir: Path
    http_cache_dir: Path
    user_agent: str
    log_level: str
    log_format: str
    github_token: str | None
    firecrawl_api_key: str | None
    brave_search_api_key: str | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        """Read settings from ``env`` (defaults to ``os.environ``). Empty strings count as unset."""
        source = os.environ if env is None else env

        def get(key: str) -> str | None:
            value = source.get(key, "").strip()
            return value or None

        return cls(
            db_path=_resolve(get("SCRAPER_DB_PATH") or "data/scraper.db"),
            config_dir=_resolve(get("SCRAPER_CONFIG_DIR") or "config"),
            http_cache_dir=_resolve(get("SCRAPER_HTTP_CACHE_DIR") or "data/http_cache"),
            user_agent=get("SCRAPER_USER_AGENT") or DEFAULT_USER_AGENT,
            log_level=(get("SCRAPER_LOG_LEVEL") or "INFO").upper(),
            log_format=(get("SCRAPER_LOG_FORMAT") or "text").lower(),
            github_token=get("GITHUB_TOKEN"),
            firecrawl_api_key=get("FIRECRAWL_API_KEY"),
            brave_search_api_key=get("BRAVE_SEARCH_API_KEY"),
        )


def load_yaml(name: str, config_dir: Path | None = None) -> dict[str, Any]:
    """Load ``<config_dir>/<name>.yaml`` as a mapping.

    Args:
        name: File stem, e.g. ``"india_geo"``.
        config_dir: Directory to read from; defaults to ``Settings.from_env().config_dir``.

    Raises:
        ConfigError: If the file is missing, is not valid YAML, or is not a mapping at top level.
    """
    directory = config_dir or Settings.from_env().config_dir
    path = directory / f"{name}.yaml"
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"Config file not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a mapping at the top level")
    return data
