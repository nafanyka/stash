"""ScrapeDiscovery - a discovery layer over the scrapers Stash already has installed.

The plugin owns the whole engine; see docs/architecture.md. Modules are imported
lazily by the entry point rather than here, so a task that only reads the database
does not pay for the modules it will not use.
"""

__version__ = "0.1.0"
