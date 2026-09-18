"""Schema and forward-only migrations for fastdiscovery.sqlite.

The version lives in `PRAGMA user_version`, which SQLite keeps in the file header, so
no bookkeeping table is needed and a fresh file reports 0. Each entry in MIGRATIONS is
a list of statements moving the schema from `index` to `index + 1`, run in one
transaction per step, so a failed migration leaves the previous version intact.

Rules for adding one: append, never edit a released step; additive changes only.

The shape of this schema follows from requirement 36: FastDiscovery is a review queue,
not an archive. A run holds a scene's whole discovery payload while it waits for a
decision, and the moment that decision is made everything but a one-row audit trail is
deleted. So the payload tables all cascade from `runs`, and `runs` itself carries the
handful of columns the audit keeps.
"""

from __future__ import annotations

_INITIAL = [
    """
    CREATE TABLE meta (
        key   TEXT PRIMARY KEY,
        value TEXT
    )
    """,
    # -- one discovery run for one scene ---------------------------------------
    # The columns above `config_json` are the audit record: they survive the purge that
    # Apply and Reject perform, and they are all that survives (requirement 24).
    """
    CREATE TABLE runs (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        scene_id            INTEGER NOT NULL,
        status              TEXT NOT NULL DEFAULT 'RUNNING',
        trigger             TEXT NOT NULL DEFAULT 'manual',
        job_id              TEXT,
        started_at          TEXT NOT NULL,
        finished_at         TEXT,
        decided_at          TEXT,
        source_count        INTEGER NOT NULL DEFAULT 0,
        ok_source_count     INTEGER NOT NULL DEFAULT 0,
        error_count         INTEGER NOT NULL DEFAULT 0,
        url_count           INTEGER NOT NULL DEFAULT 0,
        result_count        INTEGER NOT NULL DEFAULT 0,
        max_depth_reached   INTEGER NOT NULL DEFAULT 0,
        purged              INTEGER NOT NULL DEFAULT 0,
        heartbeat_at        TEXT,
        config_json         TEXT,
        scene_snapshot_json TEXT,
        selection_json      TEXT,
        progress_json       TEXT,
        stop_reason         TEXT,
        error               TEXT
    )
    """,
    "CREATE INDEX idx_runs_scene ON runs(scene_id, started_at DESC)",
    "CREATE INDEX idx_runs_status ON runs(status, started_at DESC)",
    # -- one invocation of one source ------------------------------------------
    # `parent_source_id` is the discovery graph edge: the source whose result produced
    # the URL this source was given (requirement 39). `scraper_id` is NULL when Stash
    # auto-routed a URL among several possible handlers, because the API does not
    # report which one ran; `attribution` records that honestly.
    """
    CREATE TABLE sources (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id            INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
        scene_id          INTEGER NOT NULL,
        type              TEXT NOT NULL,
        method            TEXT NOT NULL DEFAULT '',
        name              TEXT NOT NULL DEFAULT '',
        endpoint          TEXT,
        scraper_id        TEXT,
        url               TEXT,
        url_key           TEXT,
        host              TEXT,
        target            TEXT,
        depth             INTEGER NOT NULL DEFAULT 0,
        parent_source_id  INTEGER REFERENCES sources(id) ON DELETE SET NULL,
        source_key        TEXT NOT NULL DEFAULT '',
        attribution       TEXT NOT NULL DEFAULT 'CERTAIN',
        handlers_json     TEXT,
        status            TEXT NOT NULL DEFAULT 'RUNNING',
        error             TEXT,
        started_at        TEXT NOT NULL,
        finished_at       TEXT,
        duration_ms       INTEGER,
        result_count      INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX idx_sources_run ON sources(run_id, depth, id)",
    "CREATE UNIQUE INDEX idx_sources_key ON sources(run_id, source_key)",
    # -- what a source returned, verbatim --------------------------------------
    # `raw_json` is the payload exactly as Stash handed it over, minus any base64 image,
    # which is externalised into `images` and referenced by hash. Keeping it raw is what
    # lets the review be rebuilt without asking any website again, and what makes a bug
    # in the merge a bug that can be fixed after the fact rather than a lost scrape.
    """
    CREATE TABLE results (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id       INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
        source_id    INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
        ordinal      INTEGER NOT NULL DEFAULT 0,
        raw_json     TEXT NOT NULL,
        image_url    TEXT,
        image_sha256 TEXT,
        created_at   TEXT NOT NULL
    )
    """,
    "CREATE INDEX idx_results_run ON results(run_id)",
    "CREATE INDEX idx_results_source ON results(source_id, ordinal)",
    # -- the URL frontier ------------------------------------------------------
    # One row per distinct URL per run, which is the loop guard for discovery
    # (requirement 6); the per-scraper guard is the unique index on sources.source_key.
    """
    CREATE TABLE urls (
        id                     INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id                 INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
        url                    TEXT NOT NULL,
        normalized             TEXT NOT NULL,
        norm_key               TEXT NOT NULL,
        host                   TEXT NOT NULL DEFAULT '',
        depth                  INTEGER NOT NULL DEFAULT 0,
        role                   TEXT NOT NULL DEFAULT 'scene',
        origin                 TEXT NOT NULL DEFAULT '',
        discovered_by_result_id INTEGER REFERENCES results(id) ON DELETE SET NULL,
        handler_ids_json       TEXT,
        state                  TEXT NOT NULL DEFAULT 'PENDING',
        note                   TEXT,
        found_at               TEXT NOT NULL
    )
    """,
    "CREATE UNIQUE INDEX idx_urls_key ON urls(run_id, norm_key)",
    "CREATE INDEX idx_urls_state ON urls(run_id, state, depth)",
    # -- images, out of line ---------------------------------------------------
    # Scrapers return covers as base64 data URIs of a couple of hundred kilobytes, and
    # a data URI has no address to store instead. Keying on the hash means the same
    # cover from ten sources is stored once (requirement 53), and these rows are
    # deleted with the run, so nothing accumulates. An image the scraper gave as an
    # http(s) URL is never downloaded: only the URL is kept (requirement 42).
    """
    CREATE TABLE images (
        sha256     TEXT PRIMARY KEY,
        mime       TEXT NOT NULL DEFAULT 'image/jpeg',
        bytes      INTEGER NOT NULL DEFAULT 0,
        data       BLOB NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    # -- what an apply actually did --------------------------------------------
    # Small on purpose: field names and entity ids, never the values themselves
    # (requirement 55), so the audit does not become the metadata archive the whole
    # design exists to avoid.
    """
    CREATE TABLE applications (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id       INTEGER NOT NULL,
        scene_id     INTEGER NOT NULL,
        applied_at   TEXT NOT NULL,
        status       TEXT NOT NULL,
        fields_json  TEXT,
        created_json TEXT,
        error        TEXT
    )
    """,
    "CREATE INDEX idx_applications_scene ON applications(scene_id, applied_at DESC)",
]

# 2 - a reviewer can strike out a whole source.
#
# A source that got the wrong scene is wrong about every field at once, and unticking
# its values one row at a time is both tedious and easy to do incompletely. Rejecting it
# is stored on the run rather than kept in the page, so it survives a reload and so that
# Apply resolves the selection against exactly the matrix the reviewer was looking at.
_REJECTED_SOURCES = [
    "ALTER TABLE runs ADD COLUMN rejected_sources_json TEXT",
]

# 3 - what gets struck out is a result, not a source.
#
# One source can answer with several results - a stash-box name search returns a list -
# and they are separate columns because they are separate answers: the first can be the
# scene and the second a different film entirely. Rejection therefore keys on the
# column, `s<source id>_<ordinal>`, not on the source.
#
# `rejected_sources_json` from step 2 is left in place because migrations here are
# forward-only and additive; nothing reads it any more.
_REJECTED_COLUMNS = [
    "ALTER TABLE runs ADD COLUMN rejected_columns_json TEXT",
]

# 4 - performers, sharing this engine and this file rather than a second database.
#
# `entity_type` distinguishes a scene run from a performer run. The column that has
# always held a scene id (`scene_id`, on `runs` and `applications`) is kept exactly as
# it is - renaming a NOT NULL column every existing row and every existing query
# depends on is a needless risk - and is simply read as "the id of whatever
# `entity_type` says" from here on; `Repo._run_row` exposes it under both names so
# scene code that already reads `run["scene_id"]` needs no change at all, and new
# code reads `run["entity_id"]`/`run["entity_type"]`. Every existing row defaults to
# `entity_type = 'scene'`, so nothing already stored changes meaning.
#
# Without this, a performer id and a scene id that happen to share a number would
# collide in `latest_run`/`applications_for` and hand back the wrong run - a
# correctness bug, not a cosmetic one - so the type is part of every lookup key from
# here on, not an afterthought filtered in Python.
#
# `mode` records whether a performer run has only gone through the scrapers the user
# picked for Fast, or has since been topped up with Full (requirement 26). Harmless
# and unused on a scene run.
#
# `result_images` is the one real structural gap: a `ScrapedPerformer` can carry
# several photos (`images: [String!]`), where a `ScrapedScene` carries exactly one
# (`image: String`). Rather than reshape `results.image_url`/`image_sha256` - which
# every scene code path reads today - performer images go in a new child table, one
# row per photo, and scene results are untouched. `images` (the content-addressed
# blob table) already has no notion of "scene" or "performer" in it at all, so it is
# shared unchanged by both.
_PERFORMERS = [
    "ALTER TABLE runs ADD COLUMN entity_type TEXT NOT NULL DEFAULT 'scene'",
    "ALTER TABLE runs ADD COLUMN mode TEXT NOT NULL DEFAULT 'FAST'",
    "ALTER TABLE applications ADD COLUMN entity_type TEXT NOT NULL DEFAULT 'scene'",
    "CREATE INDEX idx_runs_entity ON runs(entity_type, scene_id, started_at DESC)",
    "CREATE INDEX idx_applications_entity"
    " ON applications(entity_type, scene_id, applied_at DESC)",
    """
    CREATE TABLE result_images (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        result_id    INTEGER NOT NULL REFERENCES results(id) ON DELETE CASCADE,
        ordinal      INTEGER NOT NULL DEFAULT 0,
        image_url    TEXT,
        image_sha256 TEXT
    )
    """,
    "CREATE INDEX idx_result_images_result ON result_images(result_id, ordinal)",
]

MIGRATIONS = [_INITIAL, _REJECTED_SOURCES, _REJECTED_COLUMNS, _PERFORMERS]
SCHEMA_VERSION = len(MIGRATIONS)


def migrate(connection):
    """Bring `connection` up to SCHEMA_VERSION. Returns (from, to)."""
    current = connection.execute("PRAGMA user_version").fetchone()[0]
    started = current
    while current < SCHEMA_VERSION:
        statements = MIGRATIONS[current]
        with connection:
            for statement in statements:
                connection.execute(statement)
            # PRAGMA cannot be parameterised, and `current` is an int from SQLite.
            connection.execute("PRAGMA user_version = %d" % (current + 1))
        current += 1
    return started, current
