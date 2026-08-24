"""Persistence for ScrapeDiscovery: schema, migrations, and the only SQL in the plugin."""

from . import migrations, repo  # noqa: F401

__all__ = ["migrations", "repo"]
