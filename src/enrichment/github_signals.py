"""Engineering-velocity signals from the GitHub REST API (public data only).

Endpoints (https://docs.github.com/rest/orgs, https://docs.github.com/rest/repos):
``GET /orgs/{org}`` and ``GET /orgs/{org}/repos?sort=pushed``. These are documented APIs, so
``respect_robots=False`` (ADR 002); pacing, retries and ETag caching still apply — conditional
requests answered with 304 do not count against GitHub's rate limit. ``GITHUB_TOKEN`` (optional)
raises the limit from 60 to 5,000 requests/hour.
"""

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from src.core.http import FetchError, HttpClient

API = "https://api.github.com"


@dataclass(frozen=True, slots=True)
class GithubSignals:
    """Activity summary for one GitHub org."""

    org: str
    public_repos: int | None
    active_repos: int  # non-fork, non-archived repos pushed within ``active_repo_days``
    recently_pushed: bool  # any such repo pushed within ``recent_commit_days``
    last_pushed_at: datetime | None
    languages: tuple[str, ...]
    org_email: str | None
    blog: str | None
    location: str | None

    def is_active(self, min_active_repos: int = 3) -> bool:
        """Rubric: ``min_active_repos``+ active repos, or a push within the recent window."""
        return self.active_repos >= min_active_repos or self.recently_pushed


def github_headers(token: str | None = None) -> dict[str, str]:
    """Standard REST headers (+ bearer token when configured)."""
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _when(value: Any) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)


def compute_signals(
    org: Mapping[str, Any],
    repos: Sequence[Mapping[str, Any]],
    *,
    now: datetime,
    recent_commit_days: int = 14,
    active_repo_days: int = 30,
) -> GithubSignals:
    """Pure computation from ``/orgs/{org}`` and ``/orgs/{org}/repos`` JSON."""
    live = [r for r in repos if not r.get("fork") and not r.get("archived")]
    pushed = [(r, _when(r.get("pushed_at"))) for r in live]
    active = [(r, t) for r, t in pushed if t is not None and now - t <= timedelta(active_repo_days)]
    last = max((t for _, t in pushed if t is not None), default=None)
    languages = Counter(r.get("language") for r, _ in active if r.get("language"))
    return GithubSignals(
        org=str(org.get("login") or ""),
        public_repos=org.get("public_repos"),
        active_repos=len(active),
        recently_pushed=last is not None and now - last <= timedelta(days=recent_commit_days),
        last_pushed_at=last,
        languages=tuple(lang for lang, _ in languages.most_common(5)),
        org_email=(org.get("email") or None),
        blog=(org.get("blog") or None),
        location=(org.get("location") or None),
    )


async def fetch_github_signals(
    http: HttpClient,
    org: str,
    *,
    token: str | None = None,
    now: datetime | None = None,
    recent_commit_days: int = 14,
    active_repo_days: int = 30,
) -> GithubSignals | None:
    """Signals for ``org``, or ``None`` if GitHub has no such organization (404).

    Raises:
        FetchError: Other API failures (rate limit 403, outages after retries).
    """
    headers = github_headers(token)
    try:
        # Documented REST API -> robots opt-out (ADR 002).
        org_json = await http.get_json(f"{API}/orgs/{org}", headers=headers, respect_robots=False)
    except FetchError as exc:
        if exc.status == 404:
            return None
        raise
    repos = await http.get_json(
        f"{API}/orgs/{org}/repos",
        params={"sort": "pushed", "per_page": 100, "type": "public"},
        headers=headers,
        respect_robots=False,
    )
    return compute_signals(
        org_json,
        repos if isinstance(repos, list) else [],
        now=now or datetime.now(UTC),
        recent_commit_days=recent_commit_days,
        active_repo_days=active_repo_days,
    )
