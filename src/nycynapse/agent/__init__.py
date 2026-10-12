"""The guide agent: city data first, then live sources and the web, over MCP."""

from __future__ import annotations


def build(settings):
    from .. import lake as lake_db
    from ..catalog import store
    from ..pipeline.context import Context
    from ..tools.http import load_keys
    from .core import Agent
    from .host import MCPHost
    from .lake import Lake

    load_keys()
    ctx = Context(
        settings, lake_db.connect(settings, threads=2), store.connect(settings.app_pg_dsn)
    )
    return Agent(Lake(ctx), MCPHost())
