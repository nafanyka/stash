"""Schema and forward-only migrations for scrape-discovery.sqlite.

The version lives in `PRAGMA user_version`, which SQLite stores in the file header, so
no bookkeeping table is needed and a fresh file reports 0. Each entry in MIGRATIONS is
a list of statements that moves the schema from `index` to `index + 1`; they run inside
one transaction per step, so a failed migration leaves the previous version intact.

Rules for adding one:

* append, never edit a released step - somebody's database already ran it;
* additive changes only (new table, new column with a default, new index). Anything
  destructive needs a new table plus a copy, so the old data survives a downgrade;
* if a step changes what normalisation, correlation or scoring mean, bump the matching
  version in `meta` instead of touching stored rows: the reprocessing tasks rebuild
  derived data from the raw results, and that is the whole point of keeping them.
"""

from __future__ import annotations

# Versions of the derived pipeline stages. Rows carry the version they were produced
# with, so a reprocessing task can find what is stale instead of rebuilding everything.
NORM_VERSION = 1
CORRELATION_VERSION = 1
SCORING_VERSION = 1

_INITIAL = [
    # -- configuration and pipeline bookkeeping --------------------------------
    """
    CREATE TABLE meta (
        key   TEXT PRIMARY KEY,
        value TEXT
    )
    """,
    # -- the scraper registry, as observed over time ---------------------------
    # `fingerprint` stands in for a version: scraper configs carry no version field,
    # but a change to a scraper's name, kinds or URL patterns changes this hash, which
    # is what lets "scan with newly installed scrapers" tell new from already-tried.
    """
    CREATE TABLE scrapers (
        id               TEXT PRIMARY KEY,
        name             TEXT NOT NULL DEFAULT '',
        kinds_json       TEXT NOT NULL DEFAULT '[]',
        url_patterns_json TEXT NOT NULL DEFAULT '[]',
        fingerprint      TEXT NOT NULL DEFAULT '',
        first_seen       TEXT NOT NULL,
        last_seen        TEXT NOT NULL
    )
    """,
    # -- one discovery execution for one scene ---------------------------------
    """
    CREATE TABLE scans (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        scene_id            INTEGER NOT NULL,
        trigger             TEXT NOT NULL DEFAULT 'manual',
        mode                TEXT NOT NULL DEFAULT 'normal',
        status              TEXT NOT NULL DEFAULT 'RUNNING',
        started_at          TEXT NOT NULL,
        finished_at         TEXT,
        heartbeat_at        TEXT,
        config_json         TEXT,
        scene_snapshot_json TEXT,
        scraper_count       INTEGER NOT NULL DEFAULT 0,
        attempt_count       INTEGER NOT NULL DEFAULT 0,
        match_count         INTEGER NOT NULL DEFAULT 0,
        error_count         INTEGER NOT NULL DEFAULT 0,
        url_count           INTEGER NOT NULL DEFAULT 0,
        candidate_count     INTEGER NOT NULL DEFAULT 0,
        best_confidence     REAL,
        stop_reason         TEXT,
        error               TEXT,
        progress_json       TEXT
    )
    """,
    "CREATE INDEX idx_scans_scene ON scans(scene_id, started_at DESC)",
    "CREATE INDEX idx_scans_status ON scans(status)",
    # -- one scraper invocation ------------------------------------------------
    # `parent_id` is the discovery graph edge: the attempt whose result produced the
    # URL this attempt scraped. `target_key` is the dedup and cache key.
    # `scraper_id` is null when Stash auto-routed a URL among several possible
    # handlers, because the API does not report which one ran (architecture.md L1).
    """
    CREATE TABLE attempts (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        scan_id             INTEGER NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
        scene_id            INTEGER NOT NULL,
        parent_id           INTEGER REFERENCES attempts(id) ON DELETE SET NULL,
        depth               INTEGER NOT NULL DEFAULT 0,
        scraper_id          TEXT,
        scraper_name        TEXT,
        scraper_fingerprint TEXT,
        attribution         TEXT NOT NULL DEFAULT 'CERTAIN',
        method              TEXT NOT NULL,
        target              TEXT NOT NULL DEFAULT '',
        target_key          TEXT NOT NULL,
        input_json          TEXT,
        status              TEXT NOT NULL DEFAULT 'RUNNING',
        error               TEXT,
        -- 'permanent' or 'transient'. A fragment scrape against a scraper that does
        -- not own the scene fails the same way every time (a 404 on a query URL built
        -- from nothing, a script exiting non-zero); a network blip does not. Both stay
        -- ERROR for diagnostics, but only one is worth retrying tomorrow.
        error_kind          TEXT,
        started_at          TEXT NOT NULL,
        finished_at         TEXT,
        duration_ms         INTEGER,
        result_count        INTEGER NOT NULL DEFAULT 0,
        from_cache          INTEGER NOT NULL DEFAULT 0,
        cached_from         INTEGER REFERENCES attempts(id) ON DELETE SET NULL
    )
    """,
    "CREATE INDEX idx_attempts_scan ON attempts(scan_id)",
    "CREATE INDEX idx_attempts_cache ON attempts(scene_id, target_key, finished_at DESC)",
    "CREATE INDEX idx_attempts_scraper ON attempts(scraper_id, status)",
    "CREATE INDEX idx_attempts_parent ON attempts(parent_id)",
    # -- images, out of line ---------------------------------------------------
    # Scrapers return covers as base64 data URIs of a couple of hundred kilobytes.
    # Keying on the hash means the same cover from ten scrapers is stored once, and
    # keeping them out of results.raw_json keeps list queries cheap.
    """
    CREATE TABLE blobs (
        sha256     TEXT PRIMARY KEY,
        mime       TEXT NOT NULL DEFAULT '',
        bytes      INTEGER NOT NULL DEFAULT 0,
        data       BLOB NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    # -- what a scraper returned ----------------------------------------------
    # One row per returned item: a name search answers with a list, and every entry of
    # it is a separate potential candidate.
    """
    CREATE TABLE results (
        id                     INTEGER PRIMARY KEY AUTOINCREMENT,
        attempt_id             INTEGER NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
        ordinal                INTEGER NOT NULL DEFAULT 0,
        raw_json               TEXT NOT NULL,
        raw_fingerprint        TEXT NOT NULL,
        normalized_json        TEXT,
        normalized_fingerprint TEXT,
        norm_version           INTEGER NOT NULL DEFAULT 0,
        image_sha256           TEXT REFERENCES blobs(sha256) ON DELETE SET NULL
    )
    """,
    "CREATE INDEX idx_results_attempt ON results(attempt_id)",
    "CREATE INDEX idx_results_raw_fp ON results(raw_fingerprint)",
    "CREATE INDEX idx_results_norm_fp ON results(normalized_fingerprint)",
    "CREATE INDEX idx_results_norm_version ON results(norm_version)",
    # -- URL expansion ---------------------------------------------------------
    """
    CREATE TABLE discovered_urls (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        scan_id          INTEGER NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
        scene_id         INTEGER NOT NULL,
        url              TEXT NOT NULL,
        normalized       TEXT NOT NULL,
        host             TEXT NOT NULL DEFAULT '',
        norm_key         TEXT NOT NULL,
        depth            INTEGER NOT NULL DEFAULT 0,
        source_result_id INTEGER REFERENCES results(id) ON DELETE SET NULL,
        handler_ids_json TEXT NOT NULL DEFAULT '[]',
        handler_count    INTEGER NOT NULL DEFAULT 0,
        state            TEXT NOT NULL DEFAULT 'PENDING',
        attempt_id       INTEGER REFERENCES attempts(id) ON DELETE SET NULL,
        found_at         TEXT NOT NULL,
        UNIQUE (scan_id, norm_key)
    )
    """,
    "CREATE INDEX idx_urls_scene ON discovered_urls(scene_id, norm_key)",
    "CREATE INDEX idx_urls_state ON discovered_urls(scan_id, state)",
    # -- candidates ------------------------------------------------------------
    """
    CREATE TABLE candidates (
        id                       INTEGER PRIMARY KEY AUTOINCREMENT,
        scene_id                 INTEGER NOT NULL,
        scan_id                  INTEGER REFERENCES scans(id) ON DELETE SET NULL,
        identity_key             TEXT NOT NULL,
        confidence               REAL,
        level                    TEXT,
        score_json               TEXT,
        merged_json              TEXT,
        source_count             INTEGER NOT NULL DEFAULT 0,
        independent_source_count INTEGER NOT NULL DEFAULT 0,
        state                    TEXT NOT NULL DEFAULT 'NEW',
        correlation_version      INTEGER NOT NULL DEFAULT 0,
        scoring_version          INTEGER NOT NULL DEFAULT 0,
        created_at               TEXT NOT NULL,
        updated_at               TEXT NOT NULL
    )
    """,
    "CREATE INDEX idx_candidates_scene ON candidates(scene_id, confidence DESC)",
    "CREATE INDEX idx_candidates_identity ON candidates(scene_id, identity_key)",
    "CREATE INDEX idx_candidates_state ON candidates(state)",
    # -- provenance ------------------------------------------------------------
    # Which results back a candidate, and which results back each individual field
    # value. The second table is what makes "these two sources agree independently"
    # and the per-field source dropdown answerable.
    """
    CREATE TABLE candidate_sources (
        candidate_id INTEGER NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
        result_id    INTEGER NOT NULL REFERENCES results(id) ON DELETE CASCADE,
        attempt_id   INTEGER REFERENCES attempts(id) ON DELETE SET NULL,
        scraper_id   TEXT,
        scraper_name TEXT,
        host         TEXT NOT NULL DEFAULT '',
        role         TEXT NOT NULL DEFAULT 'support',
        PRIMARY KEY (candidate_id, result_id)
    )
    """,
    """
    CREATE TABLE candidate_fields (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        candidate_id INTEGER NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
        field        TEXT NOT NULL,
        value_key    TEXT NOT NULL,
        value_json   TEXT NOT NULL,
        source_count INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE UNIQUE INDEX idx_cfields_value ON candidate_fields(candidate_id, field, value_key)",
    """
    CREATE TABLE candidate_field_sources (
        candidate_field_id INTEGER NOT NULL
            REFERENCES candidate_fields(id) ON DELETE CASCADE,
        result_id          INTEGER NOT NULL REFERENCES results(id) ON DELETE CASCADE,
        scraper_id         TEXT,
        scraper_name       TEXT,
        PRIMARY KEY (candidate_field_id, result_id)
    )
    """,
    # -- audit -----------------------------------------------------------------
    """
    CREATE TABLE applications (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        scene_id       INTEGER NOT NULL,
        candidate_id   INTEGER REFERENCES candidates(id) ON DELETE SET NULL,
        applied_at     TEXT NOT NULL,
        mode           TEXT NOT NULL DEFAULT 'manual',
        selection_json TEXT,
        before_json    TEXT,
        after_json     TEXT,
        changes_json   TEXT,
        status         TEXT NOT NULL DEFAULT 'APPLIED',
        error          TEXT
    )
    """,
    "CREATE INDEX idx_applications_scene ON applications(scene_id, applied_at DESC)",
    # -- the inbox index -------------------------------------------------------
    # Denormalised on purpose: the inbox has to filter, sort and page over thousands
    # of scenes without opening a single raw result.
    """
    CREATE TABLE scene_state (
        scene_id        INTEGER PRIMARY KEY,
        status          TEXT NOT NULL DEFAULT 'UNSCANNED',
        last_scan_id    INTEGER REFERENCES scans(id) ON DELETE SET NULL,
        last_scanned_at TEXT,
        candidate_count INTEGER NOT NULL DEFAULT 0,
        best_confidence REAL,
        attempt_count   INTEGER NOT NULL DEFAULT 0,
        error_count     INTEGER NOT NULL DEFAULT 0,
        url_count       INTEGER NOT NULL DEFAULT 0,
        title           TEXT NOT NULL DEFAULT '',
        path            TEXT NOT NULL DEFAULT '',
        studio_name     TEXT NOT NULL DEFAULT '',
        updated_at      TEXT NOT NULL
    )
    """,
    "CREATE INDEX idx_scene_state_status ON scene_state(status, best_confidence DESC)",
    "CREATE INDEX idx_scene_state_scanned ON scene_state(last_scanned_at DESC)",
    "CREATE INDEX idx_scene_state_conf ON scene_state(best_confidence DESC)",
    # -- statistics, derived ---------------------------------------------------
    # A view rather than counters: history is the single source of truth, so the
    # numbers cannot drift, and nothing has to be maintained on the hot path.
    """
    CREATE VIEW scraper_stats AS
    SELECT
        a.scraper_id                                         AS scraper_id,
        MAX(a.scraper_name)                                  AS scraper_name,
        COUNT(*)                                             AS attempts,
        SUM(CASE WHEN a.status = 'MATCH'    THEN 1 ELSE 0 END) AS matches,
        SUM(CASE WHEN a.status = 'NO_MATCH' THEN 1 ELSE 0 END) AS no_matches,
        SUM(CASE WHEN a.status = 'ERROR'    THEN 1 ELSE 0 END) AS errors,
        SUM(CASE WHEN a.status = 'TIMEOUT'  THEN 1 ELSE 0 END) AS timeouts,
        AVG(a.duration_ms)                                   AS avg_ms,
        MAX(a.finished_at)                                   AS last_attempt_at
    FROM attempts a
    WHERE a.scraper_id IS NOT NULL AND a.from_cache = 0
    GROUP BY a.scraper_id
    """,
]

MIGRATIONS = [_INITIAL]

SCHEMA_VERSION = len(MIGRATIONS)


def migrate(connection) -> tuple:
    """Bring `connection` up to SCHEMA_VERSION. Returns (from, to)."""
    current = connection.execute("PRAGMA user_version").fetchone()[0]
    if current > SCHEMA_VERSION:
        raise RuntimeError(
            "database schema version %s is newer than this plugin understands (%s); "
            "upgrade ScrapeDiscovery or point databasePath at a different file"
            % (current, SCHEMA_VERSION)
        )

    start = current
    while current < SCHEMA_VERSION:
        statements = MIGRATIONS[current]
        # One transaction per step. PRAGMA user_version cannot be parameterised.
        with connection:
            for statement in statements:
                connection.execute(statement)
            connection.execute("PRAGMA user_version = %d" % (current + 1))
        current += 1

    return start, current
