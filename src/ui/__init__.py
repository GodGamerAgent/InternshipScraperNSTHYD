"""Optional local web UI (NiceGUI). Install with ``uv sync --extra ui``; launch with ``scraper ui``.

Only ``app.py`` imports NiceGUI. ``view_models.py`` and ``jobs.py`` are plain Python so they are
tested in CI without the optional extra.
"""
