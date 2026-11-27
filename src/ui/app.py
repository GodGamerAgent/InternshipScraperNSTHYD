"""NiceGUI dashboard (layout + events only). Launch with ``uv run scraper ui``.

Everything shown comes from ``src.ui.view_models`` (pure, tested) over the local SQLite file.
Opening the page never touches the network; only the explicit **Run scrape** / **Run enrich**
buttons do, through ``src.ui.jobs`` → ``src.pipeline`` (robots.txt, rate limits and retries apply).
"""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from nicegui import ui

from src.collectors.registry import ALL_COLLECTOR_NAMES
from src.core.config import Settings
from src.core.search import search_backend_name
from src.enrichment.geo import GeoResolver
from src.enrichment.scorer import ScoringConfig
from src.storage import Repository
from src.ui import jobs
from src.ui.view_models import (
    METHOD_LABELS,
    TABLE_COLUMNS,
    WORK_MODE_LABELS,
    UiFilters,
    city_labels,
    contact_rows,
    download_payload,
    hidden_opportunity_rows,
    internship_rows,
    kpi_cards,
)

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_CONFIG: dict[str, Settings] = {}

# Quasar slot templates: Vue's {{ }} escapes text; every href is a validated http(s) URL.
_LINK = (
    '<q-td :props="props"><a v-if="props.value" :href="props.value" target="_blank" '
    'rel="noopener noreferrer" class="text-primary">{label}</a><span v-else>—</span></q-td>'
)
_PPO_BADGE = (
    '<q-td :props="props"><q-badge v-if="props.value === \'PPO\'" color="positive" label="PPO" />'
    "<span v-else>{{ props.value }}</span></q-td>"
)
_SCORE_BADGE = (
    '<q-td :props="props"><q-badge :label="props.value" :color="props.row.label === \'high\' ? '
    "'positive' : (props.row.label === 'medium' ? 'warning' : 'grey')\" /></q-td>"
)
_COLD_PITCH = (
    '<q-td :props="props"><q-badge v-if="props.value === \'Yes\'" color="primary" '
    'label="Invites pitches" /><span v-else>{{ props.value }}</span></q-td>'
)
_STARTUP_LINK = (
    '<q-td :props="props"><a :href="props.row.website" target="_blank" rel="noopener noreferrer"'
    ' class="text-primary">{{ props.value }}</a></q-td>'
)
_GITHUB_LINK = (
    '<q-td :props="props"><a v-if="props.value" :href="props.value" target="_blank" '
    'rel="noopener noreferrer" class="text-primary">'
    "{{ props.value.replace('https://github.com/', '') }}</a><span v-else>—</span></q-td>"
)
_GUESS_NOTE = (
    '<q-td :props="props"><q-badge v-if="props.row.is_guess" color="orange" '
    'label="Unconfirmed guess" class="q-mr-sm" />{{ props.value }}</q-td>'
)


# Explicit page backgrounds: without them the light theme is transparent and a dark browser
# canvas shows through (black tab labels on black).
_PAGE_CSS = """
body.body--light { background: #f5f6f8; }
body.body--dark { background: #121212; }
"""
_THEME: dict[str, bool | None] = {"auto": None, "light": False, "dark": True}


def configure(settings: Settings) -> None:
    """Remember which DB/config the page should read (set by ``scraper ui``)."""
    _CONFIG["settings"] = settings


def _settings() -> Settings:
    return _CONFIG.get("settings") or Settings.from_env()


def _table(name: str) -> ui.table:
    table = ui.table(columns=TABLE_COLUMNS[name], rows=[], row_key="id", pagination=15)
    table.classes("w-full").props("flat bordered wrap-cells")
    return table


def build_page() -> None:
    """The single dashboard page (NiceGUI root page function)."""
    settings = _settings()
    geo = GeoResolver.from_config(settings.config_dir)
    scoring = ScoringConfig.load(settings.config_dir)
    labels = city_labels(geo)
    dark = ui.dark_mode(value=None)  # None = follow the OS until the user picks Light/Dark
    ui.add_css(_PAGE_CSS)
    shown: dict[str, list[dict[str, Any]]] = {"internships": [], "hidden": [], "contacts": []}

    # ------------------------------------------------------------------ header
    with ui.header(elevated=True).classes("items-center gap-3 px-4"):
        ui.button(icon="menu", on_click=lambda: drawer.toggle()).props("flat round color=white")
        ui.label("India Startup Internships").classes("text-lg font-semibold")
        ui.label(str(settings.db_path.name)).classes("text-xs opacity-70")
        ui.space()
        ui.toggle(
            {"auto": "Auto", "light": "Light", "dark": "Dark"},
            value="auto",
            on_change=lambda e: dark.set_value(_THEME[str(e.value)]),
        ).props(
            "dense no-caps color=primary text-color=white "
            "toggle-color=white toggle-text-color=primary"
        ).tooltip("Theme")

    # ------------------------------------------------------------------ filter drawer
    with ui.left_drawer(value=True, bordered=True).classes("p-4 gap-3") as drawer:
        ui.label("Filters").classes("text-base font-semibold")
        cities = (
            ui.select(
                {slug: labels[slug] for slug in geo.city_slugs},
                multiple=True,
                label="Cities",
                value=[],
            )
            .props("use-chips clearable outlined dense")
            .classes("w-full")
        )
        include_remote = ui.switch("Include Remote India")
        modes = (
            ui.select(WORK_MODE_LABELS, multiple=True, label="Work mode", value=[])
            .props("use-chips clearable outlined dense")
            .classes("w-full")
        )
        ui.label("Min hidden-opportunity score").classes("text-sm mt-2")
        min_score = ui.slider(min=0, max=100, step=5, value=0).props("label")
        paid_only = ui.switch("Paid only")
        ppo_only = ui.switch("PPO only")
        keyword = (
            ui.input("Keyword search", placeholder="e.g. python, fintech")
            .props("clearable outlined dense debounce=300")
            .classes("w-full")
        )
        ui.button("Reset filters", icon="restart_alt", on_click=lambda: reset()).props(
            "flat"
        ).classes("mt-2")

    # ------------------------------------------------------------------ KPIs
    kpi_row = ui.row().classes("w-full gap-4 px-2 pt-2")

    # ------------------------------------------------------------------ tabs
    with ui.tabs().classes("w-full") as tabs:
        tab_int = ui.tab("Internships", icon="work")
        tab_hidden = ui.tab("Hidden Opportunities", icon="travel_explore")
        tab_contacts = ui.tab("Contacts", icon="contact_mail")
        tab_pipe = ui.tab("Pipeline & Exports", icon="sync")

    with ui.tab_panels(tabs, value=tab_int).classes("w-full"):
        with ui.tab_panel(tab_int):
            search_int = ui.input(placeholder="Search this table").props("dense clearable")
            table_int = _table("internships")
            table_int.add_slot("body-cell-apply_url", _LINK.replace("{label}", "Apply ↗"))
            table_int.add_slot("body-cell-ppo", _PPO_BADGE)
            search_int.bind_value(table_int, "filter")

        with ui.tab_panel(tab_hidden):
            search_hidden = ui.input(placeholder="Search this table").props("dense clearable")
            table_hidden = _table("hidden")
            table_hidden.add_slot("body-cell-startup", _STARTUP_LINK)
            table_hidden.add_slot("body-cell-score", _SCORE_BADGE)
            table_hidden.add_slot("body-cell-cold_pitch", _COLD_PITCH)
            table_hidden.add_slot("body-cell-github_url", _GITHUB_LINK)
            search_hidden.bind_value(table_hidden, "filter")

        with ui.tab_panel(tab_contacts):
            with ui.row().classes("items-center gap-4"):
                method_sel = (
                    ui.select(METHOD_LABELS, multiple=True, label="Source method", value=[])
                    .props("use-chips clearable outlined dense")
                    .classes("min-w-[16rem]")
                )
                verified_sel = (
                    ui.select(
                        {"any": "Any", "yes": "MX verified", "no": "Not MX verified"},
                        label="MX status",
                        value="any",
                    )
                    .props("outlined dense")
                    .classes("min-w-[12rem]")
                )
                search_contacts = ui.input(placeholder="Search this table").props("dense clearable")
            ui.label(
                "Pattern guesses (confidence 0.50) are unconfirmed: only the domain's mail server"
                " was checked. Contacts are for human-reviewed outreach only."
            ).classes("text-sm text-orange-600")
            table_contacts = _table("contacts")
            table_contacts.add_slot("body-cell-note", _GUESS_NOTE)
            search_contacts.bind_value(table_contacts, "filter")

        with ui.tab_panel(tab_pipe):
            _pipeline_panel(settings, shown, lambda: refresh())

    # ------------------------------------------------------------------ behaviour
    def current_filters() -> UiFilters:
        return UiFilters(
            cities=tuple(cities.value or ()),
            include_remote=bool(include_remote.value),
            work_modes=tuple(modes.value or ()),
            min_hidden_score=int(min_score.value or 0),
            paid_only=bool(paid_only.value),
            ppo_only=bool(ppo_only.value),
            keyword=str(keyword.value or ""),
        )

    def refresh() -> None:
        filters = current_filters()
        verified = {"yes": True, "no": False}.get(str(verified_sel.value))
        with Repository.open(settings.db_path) as repo:  # local file only
            shown["internships"] = internship_rows(repo, filters, labels=labels)
            shown["hidden"] = hidden_opportunity_rows(
                repo, filters, labels=labels, thresholds=scoring.thresholds
            )
            shown["contacts"] = contact_rows(
                repo, filters, methods=tuple(method_sel.value or ()), verified=verified
            )
            cards = kpi_cards(repo.stats())
        for table, key in ((table_int, "internships"), (table_hidden, "hidden"),
                           (table_contacts, "contacts")):  # fmt: skip
            table.rows = shown[key]
            table.update()
        kpi_row.clear()
        with kpi_row:
            for card in cards:
                with ui.card().classes("min-w-[11rem]"), ui.row().classes("items-center gap-3"):
                    ui.icon(card.icon, size="md").classes("text-primary")
                    with ui.column().classes("gap-0"):
                        ui.label(f"{card.value:,}").classes("text-2xl font-bold")
                        ui.label(card.label).classes("text-xs opacity-70")

    def reset() -> None:
        for element, value in ((cities, []), (modes, []), (include_remote, False),
                               (min_score, 0), (paid_only, False), (ppo_only, False),
                               (keyword, "")):  # fmt: skip
            element.set_value(value)
        refresh()

    for element in (cities, include_remote, modes, min_score, paid_only, ppo_only, keyword,
                    method_sel, verified_sel):  # fmt: skip
        element.on_value_change(lambda _: refresh())
    refresh()


def _pipeline_panel(
    settings: Settings, shown: dict[str, list[dict[str, Any]]], refresh: Callable[[], None]
) -> None:
    """Downloads of the filtered tables + manual, async scrape / enrich runs."""
    with ui.card().classes("w-full"):
        ui.label("Download filtered data").classes("text-base font-semibold")
        with ui.row().classes("items-center gap-3"):
            which = ui.select(
                {"internships": "Internships", "hidden": "Hidden opportunities",
                 "contacts": "Contacts"},
                value="internships",
                label="Table",
            ).props("outlined dense").classes("min-w-[14rem]")  # fmt: skip

            def download(fmt: str) -> None:
                table = str(which.value)
                payload = download_payload(
                    shown[table],
                    table=table,
                    fmt=fmt,
                    today=datetime.now(UTC).date(),  # type: ignore[arg-type]
                )
                ui.download.content(payload.content, payload.filename, payload.media_type)
                ui.notify(f"Downloaded {payload.rows} rows as {payload.filename}")

            ui.button("Download CSV", icon="download", on_click=lambda: download("csv"))
            ui.button("Download JSON", icon="data_object", on_click=lambda: download("json")).props(
                "outline"
            )

    with ui.card().classes("w-full"):
        ui.label("Run the pipeline locally").classes("text-base font-semibold")
        ui.label(
            "These buttons make polite network requests (robots.txt, 1.5–2.5 s per site). "
            "Nothing runs unless you click."
        ).classes("text-sm opacity-70")
        with ui.row().classes("items-center gap-3"):
            sources = (
                ui.select(
                    list(ALL_COLLECTOR_NAMES),
                    multiple=True,
                    label="Sources (blank = all)",
                    value=[],
                )
                .props("use-chips clearable outlined dense")
                .classes("min-w-[22rem]")
            )
            scrape_btn = ui.button("Run scrape", icon="cloud_download")
        with ui.row().classes("items-center gap-3"):
            limit = ui.number("Startups to enrich", value=10, min=1, max=200, precision=0).props(
                "outlined dense"
            )
            enrich_btn = ui.button("Run enrich", icon="auto_fix_high")
        with ui.row().classes("items-center gap-3"):
            discover_btn = ui.button("Discover boards", icon="travel_explore")
            if search_backend_name(settings) is None:
                ui.label(
                    "Needs BRAVE_SEARCH_API_KEY or FIRECRAWL_API_KEY in .env (search APIs only)."
                ).classes("text-sm opacity-70")
        with ui.row().classes("items-center gap-2"):
            spinner = ui.spinner(size="md")
            spinner.set_visibility(False)
            status = ui.label("Idle").classes("text-sm")
        summary = ui.table(columns=[], rows=[]).classes("w-full").props("flat dense")
        summary.set_visibility(False)
        log = ui.log(max_lines=400).classes("w-full h-64 font-mono text-xs")

    async def run(kind: str) -> None:
        if jobs.is_running():
            ui.notify("A job is already running", type="warning")
            return
        buttons = (scrape_btn, enrich_btn, discover_btn)
        for button in buttons:
            button.disable()
        spinner.set_visibility(True)
        status.set_text(f"Running {kind}…")
        log.clear()
        try:
            if kind == "scrape":
                result = await jobs.scrape_job(
                    settings, names=list(sources.value or ()) or None, on_line=log.push
                )
            elif kind == "discover":
                result = await jobs.discover_job(settings, on_line=log.push)
            else:
                result = await jobs.enrich_job(
                    settings, limit=int(limit.value or 10), on_line=log.push
                )
        finally:
            spinner.set_visibility(False)
            for button in buttons:
                button.enable()
        if result.summary:
            summary.columns = [{"name": k, "label": k, "field": k} for k in result.summary[0]]
            summary.rows = result.summary
            summary.set_visibility(True)
            summary.update()
        message = f"{kind} finished" if result.ok else f"{kind} failed: {result.error or 'see log'}"
        status.set_text(message)
        ui.notify(message, type="positive" if result.ok else "negative")
        refresh()

    async def on_scrape() -> None:
        await run("scrape")

    async def on_enrich() -> None:
        await run("enrich")

    async def on_discover() -> None:
        await run("discover")

    scrape_btn.on_click(on_scrape)
    enrich_btn.on_click(on_enrich)
    discover_btn.on_click(on_discover)


def run_ui(
    settings: Settings, *, host: str = "127.0.0.1", port: int = 8080, show: bool = True
) -> None:
    """Start the local server. ``host`` must be given explicitly: NiceGUI defaults to 0.0.0.0."""
    configure(settings)
    ui.run(
        build_page,
        host=host,
        port=port,
        title="India Internship Scraper",
        favicon="🎓",
        reload=False,
        show=show,
        dark=None,  # follow the OS until the user toggles
        show_welcome_message=True,
    )
