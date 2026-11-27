"""Name -> collector registry used by the pipeline and (Phase 5) the ``--sources`` CLI flag."""

from collections.abc import Collection, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from src.collectors.ashby import AshbyCollector
from src.collectors.ats_base import AtsCollector
from src.collectors.breezyhr import BreezyHrCollector
from src.collectors.config import (
    POLLED_ATS_PROVIDERS,
    ZOHO_PORTAL_HOST,
    AtsSourceConfig,
    CompanySeed,
    SourcesConfig,
)
from src.collectors.cutshort import CutshortCollector
from src.collectors.funding_news import FundingNewsCollector
from src.collectors.greenhouse import GreenhouseCollector
from src.collectors.himalayas import HimalayasCollector
from src.collectors.hn_hiring import HnHiringCollector
from src.collectors.iitm import IitmCollector
from src.collectors.instahyre import InstahyreCollector
from src.collectors.internshala import InternshalaCollector
from src.collectors.lever import LeverCollector
from src.collectors.nsrcel import NsrcelCollector
from src.collectors.recruitee import RecruiteeCollector
from src.collectors.remotive import RemotiveCollector
from src.collectors.rippling import RipplingCollector
from src.collectors.thub import ThubCollector
from src.collectors.workable import WorkableCollector
from src.collectors.yc_india import YcIndiaCollector
from src.collectors.zohorecruit import ZohoRecruitCollector
from src.core.base import BaseCollector
from src.core.firecrawl_client import HtmlRenderer
from src.core.http import HttpClient
from src.enrichment.geo import GeoResolver
from src.enrichment.internship import InternshipMatcher
from src.models import CITY_REMOTE_INDIA, CITY_UNKNOWN, AtsProvider, Startup

ATS_COLLECTORS: dict[str, type[AtsCollector]] = {
    cls.name: cls
    for cls in (
        GreenhouseCollector,
        LeverCollector,
        AshbyCollector,
        WorkableCollector,
        RipplingCollector,
        BreezyHrCollector,
        RecruiteeCollector,
        ZohoRecruitCollector,
    )
}


def discovered_seeds(startups: Iterable[Startup]) -> dict[AtsProvider, list[CompanySeed]]:
    """Seeds for startups whose pollable board was found by discovery (auto-enrollment).

    Covers Tier 1 providers and Zoho Recruit (ADR 008). A Zoho slug must be a full portal host
    (``acme.zohorecruit.in``); older rows that stored only the subdomain are skipped.
    """
    seeds: dict[AtsProvider, list[CompanySeed]] = {}
    for startup in startups:
        if startup.ats_provider in POLLED_ATS_PROVIDERS and startup.ats_slug:
            if startup.ats_provider is AtsProvider.ZOHORECRUIT and not ZOHO_PORTAL_HOST.fullmatch(
                startup.ats_slug.lower()
            ):
                continue
            seeds.setdefault(startup.ats_provider, []).append(
                CompanySeed(
                    name=startup.name,
                    slug=startup.ats_slug,
                    domain=startup.canonical_domain,
                    hq_city=startup.hq_city_canonical
                    if startup.hq_city_canonical not in (CITY_UNKNOWN, CITY_REMOTE_INDIA)
                    else None,
                    notes="auto-enrolled from careers-page discovery",
                )
            )
    return seeds


def build_ats_collectors(
    http: HttpClient,
    names: Sequence[str] | None = None,
    *,
    config_dir: Path | None = None,
    geo: GeoResolver | None = None,
    sources: SourcesConfig | None = None,
    extra_seeds: Mapping[AtsProvider, Sequence[CompanySeed]] | None = None,
    cities: Collection[str] | None = None,
) -> list[AtsCollector]:
    """Instantiate ATS collectors from ``config/sources.yaml``.

    Args:
        http: Shared polite client (one per run so rate limits are global).
        names: Subset such as ``["greenhouse", "ashby"]``; default every ATS collector whose
            provider has a section in ``sources.yaml: ats``.
        config_dir: Override for the YAML directory (tests).
        geo / sources: Pre-built dependencies (tests, or to share one resolver).
        extra_seeds: Additional boards (e.g. ``discovered_seeds(repo.list_startups())``); merged
            with the YAML seeds, YAML wins on duplicate slugs.
        cities: Only poll seeds whose ``hq_city`` is one of these slugs (targeted pass).

    Raises:
        ValueError: On an unknown collector name.
    """
    sources = sources or SourcesConfig.load(config_dir)
    selected = (
        list(names)
        if names is not None
        else [n for n, cls in ATS_COLLECTORS.items() if cls.provider in sources.ats]
    )
    unknown = sorted(set(selected) - set(ATS_COLLECTORS))
    if unknown:
        raise ValueError(f"unknown ATS collectors {unknown}; known: {sorted(ATS_COLLECTORS)}")
    unconfigured = sorted(n for n in selected if ATS_COLLECTORS[n].provider not in sources.ats)
    if unconfigured:
        raise ValueError(f"no sources.yaml 'ats' section for {unconfigured}")
    geo = geo or GeoResolver.from_config(config_dir)
    matcher = InternshipMatcher.from_config(sources.internship)
    collectors: list[AtsCollector] = []
    for name in selected:
        provider = ATS_COLLECTORS[name].provider
        source = sources.ats[provider]
        extra = list((extra_seeds or {}).get(provider, []))
        if extra:
            known = {c.slug.lower() for c in source.companies}
            merged = [*source.companies, *(s for s in extra if s.slug.lower() not in known)]
            unique = list({s.slug.lower(): s for s in reversed(merged)}.values())[::-1]
            source = AtsSourceConfig(endpoint=source.endpoint, companies=unique)
        if cities is not None:
            wanted = set(cities)
            source = AtsSourceConfig(
                endpoint=source.endpoint,
                companies=[c for c in source.companies if c.hq_city in wanted],
            )
        collectors.append(ATS_COLLECTORS[name](http, source=source, geo=geo, matcher=matcher))
    return collectors


DISCOVERY_COLLECTORS = ("funding_news", "thub", "hn_hiring", "yc_india", "iitm", "nsrcel")
# Remote-job feeds filtered to internships open to India (ADR 010).
REMOTE_COLLECTORS = ("himalayas", "remotive")
# Indian student portals with unauthenticated public listings (ADR 011).
PORTAL_COLLECTORS = ("internshala", "cutshort", "instahyre")
# CLI-friendly aliases that select a subset of the funding feeds.
FEED_ALIASES = ("inc42", "entrackr")
ALL_COLLECTOR_NAMES = (
    *ATS_COLLECTORS, *DISCOVERY_COLLECTORS, *REMOTE_COLLECTORS, *PORTAL_COLLECTORS, *FEED_ALIASES
)  # fmt: skip
DEFAULT_COLLECTOR_NAMES = (
    *ATS_COLLECTORS, *DISCOVERY_COLLECTORS, *REMOTE_COLLECTORS, *PORTAL_COLLECTORS
)  # fmt: skip


def build_collectors(
    http: HttpClient,
    names: Sequence[str] | None = None,
    *,
    config_dir: Path | None = None,
    geo: GeoResolver | None = None,
    sources: SourcesConfig | None = None,
    renderer: HtmlRenderer | None = None,
    extra_seeds: Mapping[AtsProvider, Sequence[CompanySeed]] | None = None,
    cities: Collection[str] | None = None,
) -> list[BaseCollector[Any, Any, Any]]:
    """Every collector by name (ATS + discovery). ``inc42`` / ``entrackr`` select those feeds.

    ``renderer`` is the optional Firecrawl fallback (``FirecrawlClient.from_settings``) used for
    JavaScript-only directory pages. ``extra_seeds`` / ``cities`` are passed to
    :func:`build_ats_collectors` (auto-enrolled boards; targeted city pass for ATS seeds only).
    """
    selected = list(dict.fromkeys(names if names is not None else DEFAULT_COLLECTOR_NAMES))
    unknown = sorted(set(selected) - set(ALL_COLLECTOR_NAMES))
    if unknown:
        raise ValueError(f"unknown collectors {unknown}; known: {sorted(ALL_COLLECTOR_NAMES)}")
    sources = sources or SourcesConfig.load(config_dir)
    geo = geo or GeoResolver.from_config(config_dir)

    ats_names = [n for n in selected if n in ATS_COLLECTORS]
    collectors: list[BaseCollector[Any, Any, Any]] = list(
        build_ats_collectors(
            http, ats_names, geo=geo, sources=sources, extra_seeds=extra_seeds, cities=cities
        )
        if ats_names
        else []
    )
    feed_names = {n for n in selected if n in FEED_ALIASES}
    if "funding_news" in selected:
        feed_names = {f.name for f in sources.funding_feeds}
    if feed_names:
        feeds = [f for f in sources.funding_feeds if f.name in feed_names]
        collectors.append(FundingNewsCollector(http, feeds=feeds, geo=geo))
    if "thub" in selected:
        collectors.append(
            ThubCollector(
                http,
                incubators=sources.incubators,
                geo=geo,
                renderer=renderer,
                non_company_hosts=sources.non_company_hosts,
            )
        )
    if "iitm" in selected:
        collectors.append(
            IitmCollector(
                http,
                incubators=sources.incubators,
                geo=geo,
                non_company_hosts=sources.non_company_hosts,
            )
        )
    if "nsrcel" in selected:
        collectors.append(
            NsrcelCollector(
                http,
                incubators=sources.incubators,
                geo=geo,
                renderer=renderer,
                non_company_hosts=sources.non_company_hosts,
            )
        )
    if "yc_india" in selected:
        collectors.append(
            YcIndiaCollector(
                http, config=sources.yc_india, geo=geo, non_company_hosts=sources.non_company_hosts
            )
        )
    if "himalayas" in selected or "remotive" in selected:
        matcher = InternshipMatcher.from_config(sources.internship)
        for name, cls in (("himalayas", HimalayasCollector), ("remotive", RemotiveCollector)):
            if name in selected:
                collectors.append(cls(http, config=sources.remote_feeds, geo=geo, matcher=matcher))
    portals = sources.student_portals
    if any(name in selected for name in PORTAL_COLLECTORS):
        matcher = InternshipMatcher.from_config(sources.internship)
        if "internshala" in selected:
            collectors.append(
                InternshalaCollector(
                    http, config=portals.internshala, geo=geo, matcher=matcher, cities=cities
                )
            )
        if "cutshort" in selected:
            collectors.append(
                CutshortCollector(
                    http,
                    config=portals.cutshort,
                    geo=geo,
                    matcher=matcher,
                    non_company_hosts=sources.non_company_hosts,
                    cities=cities,
                )
            )
        if "instahyre" in selected:
            collectors.append(
                InstahyreCollector(http, config=portals.instahyre, geo=geo, matcher=matcher)
            )
    if "hn_hiring" in selected:
        collectors.append(
            HnHiringCollector(
                http,
                config=sources.hacker_news,
                geo=geo,
                non_company_hosts=sources.non_company_hosts,
            )
        )
    return collectors
