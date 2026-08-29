"""FastDiscovery - run every source Stash has for one scene, then review the lot.

The package is deliberately layered so each piece is testable without a Stash server:

    settings   configuration schema and defaults (Stash declares neither)
    logs       Stash's stderr log protocol
    stash      the only module containing GraphQL
    urls       URL normalisation and the loop guard's key
    fields     the ScrapedScene field model - what merges how
    registry   installed scrapers, stash-boxes, and which scraper owns a URL
    executor   bounded concurrency
    discovery  the run: every stash-box, then URLs, recursively
    merge      stored results -> the review matrix (values, provenance, entities)
    apply      the reviewed selection -> one Stash write, then the payload is dropped
    ops        the UI's request/response API
    tasks      the job-queue entry points
    db         SQLite, migrations, and the only SQL
"""

__version__ = "0.2.6"
