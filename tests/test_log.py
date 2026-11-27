"""Offline tests for ``src/core/log.py`` structured formatters."""

import json
import logging

import pytest

from src.core.config import Settings
from src.core.log import JsonFormatter, KeyValueFormatter, configure_logging, record_fields


def _record(**extra: object) -> logging.LogRecord:
    record = logging.LogRecord(
        "src.core.http", logging.WARNING, __file__, 1, "http.retry", (), None
    )
    record.__dict__.update(extra)
    return record


def test_record_fields_only_returns_extras() -> None:
    assert record_fields(_record(url="https://a.in", attempt=2)) == {
        "url": "https://a.in",
        "attempt": 2,
    }


def test_json_formatter_includes_event_and_extras() -> None:
    payload = json.loads(JsonFormatter().format(_record(status=503, delay_s=2.1)))
    assert payload["event"] == "http.retry"
    assert payload["level"] == "WARNING"
    assert payload["logger"] == "src.core.http"
    assert payload["status"] == 503
    assert payload["ts"].endswith("+00:00")


def test_key_value_formatter_quotes_values_with_spaces() -> None:
    line = KeyValueFormatter().format(_record(status=503, error="read timed out"))
    assert "WARNING src.core.http http.retry" in line
    assert "status=503" in line
    assert "error='read timed out'" in line


def test_configure_logging_is_idempotent_and_validates() -> None:
    root = logging.getLogger()
    saved = (root.handlers[:], root.level)
    try:
        configure_logging("debug", "json")
        configure_logging("info", "json")
        assert len(root.handlers) == 1
        assert isinstance(root.handlers[0].formatter, JsonFormatter)
        assert root.level == logging.INFO
        with pytest.raises(ValueError, match="log format"):
            configure_logging("INFO", "xml")
    finally:
        root.handlers[:] = saved[0]
        root.setLevel(saved[1])


def test_settings_expose_phase2_fields() -> None:
    settings = Settings.from_env({"SCRAPER_LOG_FORMAT": "JSON"})
    assert settings.log_format == "json"
    assert settings.http_cache_dir.parts[-2:] == ("data", "http_cache")
