"""Offline tests for ``src/core/robots.py`` (RFC 9309)."""

import asyncio

import pytest

from src.core.robots import (
    MAX_ROBOTS_BYTES,
    RobotsChecker,
    RobotsFetch,
    RobotsRules,
    product_token,
)
from tests.conftest import FIXTURES, TEST_UA, FakeTime

OTHER_UA = "SomeOtherBot/2.0"


def rules(text: str, ua: str = TEST_UA) -> RobotsRules:
    return RobotsRules.parse(text, ua)


# ---------------------------------------------------------------- product token
@pytest.mark.parametrize(
    ("ua", "token"),
    [
        (TEST_UA, "india-internship-scraper"),
        ("Googlebot/2.1 (+http://www.google.com/bot.html)", "googlebot"),
        ("my_bot", "my_bot"),
    ],
)
def test_product_token(ua: str, token: str) -> None:
    assert product_token(ua) == token


def test_product_token_rejects_garbage() -> None:
    with pytest.raises(ValueError, match="product token"):
        product_token("/0.1 no token")


# ---------------------------------------------------------------- group selection §2.2.1
def test_specific_group_beats_wildcard_group() -> None:
    r = rules("User-agent: *\nDisallow: /\n\nUser-agent: india-internship-scraper\nDisallow: /x\n")
    assert r.is_allowed("/jobs")
    assert not r.is_allowed("/x/1")


def test_matching_groups_are_merged_and_case_insensitive() -> None:
    text = (
        "User-agent: INDIA-INTERNSHIP-SCRAPER\nDisallow: /a\n\n"
        "User-agent: other\nDisallow: /b\n\n"
        "User-agent: india-internship-scraper/9.9\nDisallow: /c\n"
    )
    r = rules(text)
    assert not r.is_allowed("/a")
    assert r.is_allowed("/b")
    assert not r.is_allowed("/c")


def test_falls_back_to_wildcard_and_then_allows_all() -> None:
    assert not rules("User-agent: *\nDisallow: /private\n", OTHER_UA).is_allowed("/private")
    assert rules("User-agent: googlebot\nDisallow: /\n").is_allowed("/anything")
    assert rules("").is_allowed("/anything")


def test_consecutive_user_agents_share_one_group() -> None:
    r = rules("User-agent: foo\nUser-agent: india-internship-scraper\nDisallow: /shared\n")
    assert not r.is_allowed("/shared")


def test_rules_before_any_user_agent_are_ignored() -> None:
    assert rules("Disallow: /\nUser-agent: *\nAllow: /\n").is_allowed("/x")


# ---------------------------------------------------------------- rule matching §2.2.2
def test_longest_match_wins() -> None:
    r = rules("User-agent: *\nDisallow: /careers\nAllow: /careers/open\n")
    assert r.is_allowed("/careers/open/123")
    assert not r.is_allowed("/careers/closed")


def test_equal_length_tie_goes_to_allow() -> None:
    assert rules("User-agent: *\nDisallow: /page\nAllow: /page\n").is_allowed("/page")


def test_rule_order_does_not_matter() -> None:
    a = rules("User-agent: *\nAllow: /a/b\nDisallow: /a\n")
    b = rules("User-agent: *\nDisallow: /a\nAllow: /a/b\n")
    assert a.is_allowed("/a/b/c") == b.is_allowed("/a/b/c") is True


@pytest.mark.parametrize(
    ("pattern", "path", "allowed"),
    [
        ("/*.php$", "/index.php", False),
        ("/*.php$", "/index.php?x=1", True),  # $ anchors the end of path+query
        ("/*.php", "/index.php?x=1", False),
        ("/fish*", "/fish.html", False),
        ("/fish*", "/Fish.html", True),  # paths are case-sensitive
        ("/*?sessionid=", "/a/b?sessionid=42", False),
        ("/*/private/", "/team/private/x", False),
        ("/$", "/", False),
        ("/$", "/jobs", True),
    ],
)
def test_wildcards_and_end_anchor(pattern: str, path: str, allowed: bool) -> None:
    assert rules(f"User-agent: *\nDisallow: {pattern}\n").is_allowed(path) is allowed


def test_empty_disallow_means_no_restriction() -> None:
    assert rules("User-agent: *\nDisallow:\n").is_allowed("/anything")


def test_robots_txt_itself_is_always_allowed() -> None:
    assert rules("User-agent: *\nDisallow: /\n").is_allowed("/robots.txt")


def test_full_urls_and_query_strings_are_matched_on_path() -> None:
    r = rules("User-agent: *\nDisallow: /search?q=\n")
    assert not r.is_allowed("https://acme.in/search?q=intern")
    assert r.is_allowed("https://acme.in/search")
    assert r.is_allowed("https://acme.in")  # empty path == "/"


def test_percent_encoding_is_normalized() -> None:
    r = rules("User-agent: *\nDisallow: /jobs/€\nDisallow: /a%3cd\n")
    assert not r.is_allowed("/jobs/%E2%82%AC/1")
    assert not r.is_allowed("/a<d")


def test_pattern_without_leading_slash_is_tolerated() -> None:
    assert not rules("User-agent: *\nDisallow: private\n").is_allowed("/private/x")


# ---------------------------------------------------------------- parsing robustness
def test_fixture_with_bom_comments_typos_and_groups() -> None:
    raw = (FIXTURES / "robots" / "startup_site.txt").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8")

    ours = rules(text)
    assert ours.is_allowed("/careers/backend-intern")
    assert not ours.is_allowed("/careers/internal/roster")
    assert not ours.is_allowed("/team/private")
    assert ours.is_allowed("/wp-admin/")  # our own group replaces the * group entirely
    assert ours.crawl_delay is None
    assert ours.sitemaps == (
        "https://www.examplelabs.in/sitemap_index.xml",
        "https://www.examplelabs.in/careers-sitemap.xml",
    )

    other = rules(text, OTHER_UA)
    assert not other.is_allowed("/wp-admin/options.php")
    assert other.is_allowed("/wp-admin/admin-ajax.php")
    assert not other.is_allowed("/blog/post?replytocom=7")
    assert not other.is_allowed("/brochure.pdf")
    assert not other.is_allowed("/checkout")  # "dissallow" typo accepted
    assert other.crawl_delay == 5

    assert not rules(text, "CCBot/2.0").is_allowed("/")


def test_crlf_and_whitespace() -> None:
    r = rules("User-agent:   *  \r\n  Disallow :  /tmp  # comment\r\n")
    assert not r.is_allowed("/tmp/x")


def test_bad_crawl_delay_is_ignored() -> None:
    assert rules("User-agent: *\nCrawl-delay: soon\n").crawl_delay is None


# ---------------------------------------------------------------- checker §2.3, §2.4, §2.5
class Fetcher:
    """Scripted robots fetcher that counts calls per URL."""

    def __init__(self, responses: dict[str, RobotsFetch]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    async def __call__(self, url: str) -> RobotsFetch:
        self.calls.append(url)
        await asyncio.sleep(0)
        return self.responses.get(url, RobotsFetch(404))


def checker(fetcher: Fetcher, fake_time: FakeTime, **kw: float) -> RobotsChecker:
    return RobotsChecker(fetcher, TEST_UA, clock=fake_time.clock, **kw)


@pytest.mark.parametrize(
    ("fetched", "allowed"),
    [
        (RobotsFetch(200, b"User-agent: *\nDisallow: /x\n"), False),
        (RobotsFetch(404), True),  # unavailable -> no restrictions
        (RobotsFetch(403), True),
        (RobotsFetch(429), False),  # treated as unreachable (conservative)
        (RobotsFetch(500), False),  # unreachable -> complete disallow
        (RobotsFetch(503), False),
        (RobotsFetch(None), False),  # network error
    ],
)
async def test_status_handling(fake_time: FakeTime, fetched: RobotsFetch, allowed: bool) -> None:
    c = checker(Fetcher({"https://acme.in/robots.txt": fetched}), fake_time)
    assert await c.can_fetch("https://acme.in/x/1") is allowed


async def test_rules_cached_per_origin_and_fetched_once_concurrently(fake_time: FakeTime) -> None:
    fetcher = Fetcher({"https://acme.in/robots.txt": RobotsFetch(200, b"User-agent: *\n")})
    c = checker(fetcher, fake_time)
    await asyncio.gather(*(c.can_fetch(f"https://acme.in/p{i}") for i in range(10)))
    await c.can_fetch("https://acme.in:8443/x")
    await c.can_fetch("http://acme.in/x")
    assert fetcher.calls == [
        "https://acme.in/robots.txt",
        "https://acme.in:8443/robots.txt",
        "http://acme.in/robots.txt",
    ]


async def test_cache_expires_after_ttl(fake_time: FakeTime) -> None:
    fetcher = Fetcher({"https://acme.in/robots.txt": RobotsFetch(200, b"")})
    c = checker(fetcher, fake_time, ttl_s=100)
    await c.can_fetch("https://acme.in/")
    fake_time.now += 99
    await c.can_fetch("https://acme.in/")
    fake_time.now += 2
    await c.can_fetch("https://acme.in/")
    assert len(fetcher.calls) == 2


async def test_unreachable_is_retried_sooner(fake_time: FakeTime) -> None:
    fetcher = Fetcher({"https://acme.in/robots.txt": RobotsFetch(503)})
    c = checker(fetcher, fake_time, ttl_s=3600, unreachable_ttl_s=60)
    await c.can_fetch("https://acme.in/")
    fake_time.now += 61
    await c.can_fetch("https://acme.in/")
    assert len(fetcher.calls) == 2


async def test_only_first_500_kib_is_parsed(fake_time: FakeTime) -> None:
    padding = b"# " + b"x" * MAX_ROBOTS_BYTES + b"\n"
    body = b"User-agent: *\nDisallow: /early\n" + padding + b"Disallow: /late\n"
    c = checker(Fetcher({"https://acme.in/robots.txt": RobotsFetch(200, body)}), fake_time)
    assert not await c.can_fetch("https://acme.in/early")
    assert await c.can_fetch("https://acme.in/late")


def test_checker_validation() -> None:
    with pytest.raises(ValueError, match="24 h"):
        RobotsChecker(Fetcher({}), TEST_UA, ttl_s=25 * 3600)
    with pytest.raises(ValueError, match="product token"):
        RobotsChecker(Fetcher({}), "(no token)")
    with pytest.raises(ValueError, match="absolute"):
        RobotsChecker.origin_of("/relative/path")


def test_unknown_directives_like_content_signal_are_ignored() -> None:
    # Shape of inc42.com/robots.txt (2026-11): non-standard Content-Signal line + AI-bot group.
    text = (
        "User-agent: *\nContent-Signal: search=yes, ai-train=no\nAllow: /\nDisallow: /*?*\n"
        "Disallow: /*page/*\n\nUser-agent: GPTBot\nUser-agent: CCBot\nDisallow: /\n"
    )
    ours = rules(text)
    assert ours.is_allowed("/startups/feed/")
    assert ours.is_allowed("/buzz/feed/")
    assert not ours.is_allowed("/buzz/?s=funding")
    assert not ours.is_allowed("/startups/page/2/")
    assert not rules(text, "GPTBot/1.0").is_allowed("/buzz/feed/")
