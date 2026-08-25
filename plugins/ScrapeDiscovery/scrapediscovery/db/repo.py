"""The only module that talks SQL.

Everything above this layer passes and receives plain dicts, so the schema can change
without the engine noticing. Two rules hold throughout:

* every value reaches SQLite as a bound parameter - scraper output is untrusted input
  and never becomes part of a statement;
* transactions are short and never span a scraper call. A scan writes its attempt row,
  drops the transaction, makes the network call, then commits the result. That keeps a
  slow scraper from blocking the database, and means a killed process (Stash's
  `stopJob` kills outright) loses at most the one attempt that was in flight.

Timestamps are ISO-8601 UTC strings with a `Z` suffix, which sort lexicographically, so
TTL and retention comparisons are plain string comparisons in SQL.
"""

from __future__ import annotations

import datetime
import json
import os
import sqlite3

from . import migrations

# Statuses an attempt can end in.
MATCH = "MATCH"
NO_MATCH = "NO_MATCH"
ERROR = "ERROR"
TIMEOUT = "TIMEOUT"
SKIPPED = "SKIPPED"
CANCELLED = "CANCELLED"
RUNNING = "RUNNING"

# Scan statuses.
SCAN_RUNNING = "RUNNING"
SCAN_COMPLETED = "COMPLETED"
SCAN_WARNINGS = "COMPLETED_WITH_ERRORS"
SCAN_CANCELLED = "CANCELLED"
SCAN_FAILED = "FAILED"

# Scene inbox statuses. RESULTS sits between CANDIDATES and NO_RESULTS: scrapers did
# answer, but nothing has correlated those answers into candidates yet. Without it a
# scene with five matches and no candidates would read as "nothing found", which is
# both wrong and the exact state a scan leaves behind before the candidate stage runs.
UNSCANNED = "UNSCANNED"
SCANNING = "SCANNING"
CANDIDATES = "CANDIDATES"
RESULTS = "RESULTS"
NO_RESULTS = "NO_RESULTS"
FAILED = "FAILED"
APPLIED = "APPLIED"
DISMISSED = "DISMISSED"

# A scan whose heartbeat is older than this was killed rather than finished.
STALE_SCAN_SECONDS = 300


def now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ago(seconds: float) -> str:
    moment = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=seconds)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _loads(text, fallback=None):
    if not text:
        return fallback
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return fallback


def default_path(stash_config_dir: str) -> str:
    """Where the database lives when the setting is empty.

    Not the plugin directory: installing a newer package version deletes every file
    the previous one installed (pkg/pkg/manager.go), and uninstalling removes the
    directory. The Stash config directory survives both.
    """
    base = stash_config_dir or os.getcwd()
    return os.path.join(base, "scrape-discovery", "scrape-discovery.sqlite")


def connect(path: str) -> sqlite3.Connection:
    """Open (creating if needed) and migrate the database."""
    folder = os.path.dirname(os.path.abspath(path))
    if folder:
        os.makedirs(folder, exist_ok=True)

    connection = sqlite3.connect(path, timeout=30.0, isolation_level="")
    connection.row_factory = sqlite3.Row
    # WAL so a long scan's writes never block a UI read, and vice versa. NORMAL
    # synchronous is the usual WAL companion: a power cut can cost the last commits,
    # which for a cache of scraper answers is not worth the fsync per write.
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = NORMAL")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    migrations.migrate(connection)
    return connection


class Repo:
    def __init__(self, connection: sqlite3.Connection, path: str = ""):
        self.db = connection
        self.path = path

    @classmethod
    def open(cls, path: str) -> "Repo":
        return cls(connect(path), path)

    def close(self) -> None:
        try:
            self.db.close()
        except sqlite3.Error:
            pass

    # -- helpers -----------------------------------------------------------

    def _one(self, sql, params=()):
        row = self.db.execute(sql, params).fetchone()
        return dict(row) if row else None

    def _all(self, sql, params=()):
        return [dict(row) for row in self.db.execute(sql, params).fetchall()]

    def _write(self, sql, params=()):
        with self.db:
            return self.db.execute(sql, params)

    # -- meta --------------------------------------------------------------

    def meta_get(self, key, fallback=None):
        row = self._one("SELECT value FROM meta WHERE key = ?", (key,))
        return row["value"] if row else fallback

    def meta_set(self, key, value) -> None:
        self._write(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )

    def schema_version(self) -> int:
        return self.db.execute("PRAGMA user_version").fetchone()[0]

    # -- scraper registry --------------------------------------------------

    def sync_scrapers(self, scrapers) -> dict:
        """Record the scrapers Stash currently has. Returns counts of new/changed.

        A scraper config carries no version, so `fingerprint` (a hash of its name,
        kinds and URL patterns) stands in for one. A changed fingerprint is what makes
        an updated scraper count as worth retrying.
        """
        stamp = now()
        added, changed = [], []
        with self.db:
            for scraper in scrapers:
                existing = self.db.execute(
                    "SELECT fingerprint FROM scrapers WHERE id = ?", (scraper["id"],)
                ).fetchone()
                if existing is None:
                    added.append(scraper["id"])
                elif existing["fingerprint"] != scraper["fingerprint"]:
                    changed.append(scraper["id"])
                self.db.execute(
                    "INSERT INTO scrapers(id, name, kinds_json, url_patterns_json,"
                    " fingerprint, first_seen, last_seen) VALUES(?,?,?,?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET name = excluded.name,"
                    " kinds_json = excluded.kinds_json,"
                    " url_patterns_json = excluded.url_patterns_json,"
                    " fingerprint = excluded.fingerprint,"
                    " last_seen = excluded.last_seen",
                    (
                        scraper["id"], scraper.get("name") or "",
                        _dumps(sorted(scraper.get("kinds") or [])),
                        _dumps(scraper.get("url_patterns") or []),
                        scraper["fingerprint"], stamp, stamp,
                    ),
                )
        self.meta_set("scrapers_synced_at", stamp)
        return {"total": len(scrapers), "added": added, "changed": changed}

    def known_scrapers(self) -> dict:
        return {
            row["id"]: {
                "id": row["id"],
                "name": row["name"],
                "kinds": _loads(row["kinds_json"], []),
                "url_patterns": _loads(row["url_patterns_json"], []),
                "fingerprint": row["fingerprint"],
                "first_seen": row["first_seen"],
                "last_seen": row["last_seen"],
            }
            for row in self.db.execute("SELECT * FROM scrapers")
        }

    def scrapers_tried(self, scene_id: int) -> dict:
        """scraper id -> fingerprint last used against this scene.

        The basis of "scan with newly installed scrapers": anything absent here, or
        recorded under a different fingerprint, has not been tried in its current form.
        """
        return {
            row["scraper_id"]: row["scraper_fingerprint"]
            for row in self.db.execute(
                "SELECT scraper_id, MAX(scraper_fingerprint) AS scraper_fingerprint "
                "FROM attempts WHERE scene_id = ? AND scraper_id IS NOT NULL "
                "GROUP BY scraper_id",
                (scene_id,),
            )
        }

    # -- scans -------------------------------------------------------------

    def start_scan(self, scene_id, trigger, mode, config, scene_snapshot) -> int:
        stamp = now()
        cursor = self._write(
            "INSERT INTO scans(scene_id, trigger, mode, status, started_at,"
            " heartbeat_at, config_json, scene_snapshot_json)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (int(scene_id), trigger, mode, SCAN_RUNNING, stamp, stamp,
             _dumps(config), _dumps(scene_snapshot)),
        )
        scan_id = cursor.lastrowid
        self.set_scene_status(scene_id, SCANNING, last_scan_id=scan_id)
        return scan_id

    def heartbeat(self, scan_id, progress=None) -> None:
        """Prove the scan's process is alive, and store the detail the UI polls.

        Stash's job progress is a single float (architecture.md L9), so the numbers a
        user actually wants to see live here instead.
        """
        self._write(
            "UPDATE scans SET heartbeat_at = ?, progress_json = COALESCE(?, progress_json)"
            " WHERE id = ?",
            (now(), _dumps(progress) if progress is not None else None, scan_id),
        )

    def update_scan_counts(self, scan_id, **counts) -> None:
        allowed = ("scraper_count", "attempt_count", "match_count", "error_count",
                   "url_count", "candidate_count", "best_confidence")
        fields = [key for key in counts if key in allowed]
        if not fields:
            return
        sql = "UPDATE scans SET " + ", ".join(f"{f} = ?" for f in fields) + " WHERE id = ?"
        self._write(sql, tuple(counts[f] for f in fields) + (scan_id,))

    def finish_scan(self, scan_id, status, stop_reason=None, error=None) -> None:
        self._write(
            "UPDATE scans SET status = ?, finished_at = ?, stop_reason = ?, error = ?"
            " WHERE id = ?",
            (status, now(), stop_reason, error, scan_id),
        )

    def scan(self, scan_id):
        row = self._one("SELECT * FROM scans WHERE id = ?", (scan_id,))
        return self._scan_row(row) if row else None

    def scans_for(self, scene_id, limit=20):
        return [
            self._scan_row(row)
            for row in self._all(
                "SELECT * FROM scans WHERE scene_id = ? ORDER BY started_at DESC LIMIT ?",
                (int(scene_id), int(limit)),
            )
        ]

    @staticmethod
    def _scan_row(row):
        row = dict(row)
        row["config"] = _loads(row.pop("config_json", None), {})
        row["scene_snapshot"] = _loads(row.pop("scene_snapshot_json", None), {})
        row["progress"] = _loads(row.pop("progress_json", None), {})
        return row

    def running_scans(self, limit=20):
        """Scans that believe they are still going, for the UI's live view."""
        return [
            self._scan_row(row) for row in self._all(
                "SELECT * FROM scans WHERE status = ? ORDER BY started_at DESC LIMIT ?",
                (SCAN_RUNNING, int(limit)),
            )
        ]

    def sweep_stale_scans(self) -> int:
        """Mark scans whose process died as cancelled.

        `stopJob` kills the plugin process outright, so a scan can never write its own
        final status. Any operation that opens the database sweeps first, which is why
        a killed scan shows up as CANCELLED rather than running forever.
        """
        cutoff = ago(STALE_SCAN_SECONDS)
        cursor = self._write(
            "UPDATE scans SET status = ?, finished_at = COALESCE(finished_at, ?),"
            " stop_reason = COALESCE(stop_reason, 'process ended before the scan finished')"
            " WHERE status = ? AND COALESCE(heartbeat_at, started_at) < ?",
            (SCAN_CANCELLED, now(), SCAN_RUNNING, cutoff),
        )
        swept = cursor.rowcount or 0
        if swept:
            # Those scenes are still showing SCANNING in the inbox; recompute them.
            for row in self._all(
                "SELECT DISTINCT scene_id FROM scans WHERE status = ? AND finished_at >= ?",
                (SCAN_CANCELLED, cutoff),
            ):
                self.refresh_scene_state(row["scene_id"])

        # A scene can also claim to be scanning with no scan behind it at all, if a
        # queued job never started - Stash may reject it, or the queue may be long. The
        # scans table is the authority on what is running, so anything claiming
        # otherwise is repaired here rather than left wedged, refusing new scans.
        for row in self._all(
            "SELECT s.scene_id FROM scene_state s WHERE s.status = ?"
            " AND NOT EXISTS (SELECT 1 FROM scans c WHERE c.scene_id = s.scene_id"
            "                 AND c.status = ?)",
            (SCANNING, SCAN_RUNNING),
        ):
            self.refresh_scene_state(row["scene_id"])
            swept += 1
        return swept

    # -- attempts ----------------------------------------------------------

    def find_cached_attempt(self, scene_id, target_key, fingerprint, ttl_days_for):
        """The most recent finished attempt for this target, if still fresh.

        `ttl_days_for(status)` returns the TTL in days, so cache policy stays in
        settings and this layer only compares timestamps. An attempt made with a
        different scraper fingerprint is ignored: the scraper has changed since, which
        is exactly the case "scan with newly installed scrapers" wants to catch.
        """
        row = self._one(
            "SELECT * FROM attempts WHERE scene_id = ? AND target_key = ?"
            " AND status NOT IN (?, ?) AND finished_at IS NOT NULL"
            " ORDER BY finished_at DESC LIMIT 1",
            (int(scene_id), target_key, RUNNING, SKIPPED),
        )
        # A cache hit on a row that was itself a cache hit points at the real attempt,
        # so the chain never grows: results always hang off the attempt that ran.
        if not row:
            return None
        if fingerprint and row["scraper_fingerprint"] and \
                row["scraper_fingerprint"] != fingerprint:
            return None
        days = ttl_days_for(row["status"], row.get("error_kind"))
        if not days:
            return None
        if row["finished_at"] < ago(float(days) * 86400.0):
            return None
        return row  # already a dict, via _one

    def begin_attempt(self, scan_id, scene_id, method, target, target_key,
                      scraper=None, parent_id=None, depth=0, input_payload=None,
                      attribution="CERTAIN") -> int:
        scraper = scraper or {}
        cursor = self._write(
            "INSERT INTO attempts(scan_id, scene_id, parent_id, depth, scraper_id,"
            " scraper_name, scraper_fingerprint, attribution, method, target,"
            " target_key, input_json, status, started_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (scan_id, int(scene_id), parent_id, int(depth), scraper.get("id"),
             scraper.get("name"), scraper.get("fingerprint"), attribution, method,
             target or "", target_key, _dumps(input_payload or {}), RUNNING, now()),
        )
        return cursor.lastrowid

    def finish_attempt(self, attempt_id, status, duration_ms, result_count=0,
                       error=None, error_kind=None) -> None:
        self._write(
            "UPDATE attempts SET status = ?, finished_at = ?, duration_ms = ?,"
            " result_count = ?, error = ?, error_kind = ? WHERE id = ?",
            (status, now(), int(duration_ms), int(result_count),
             (error or None), error_kind, attempt_id),
        )

    def record_cached_attempt(self, scan_id, scene_id, source_attempt, parent_id=None,
                              depth=0) -> int:
        """Copy a cached attempt into this scan, so history stays complete.

        The scan reports what it did, and a skipped-because-cached attempt is part of
        that. The payload is not copied: `cached_from` points at the attempt that
        actually ran, and its results stay the single stored copy.
        """
        origin = source_attempt.get("cached_from") or source_attempt["id"]
        stamp = now()
        cursor = self._write(
            "INSERT INTO attempts(scan_id, scene_id, parent_id, depth, scraper_id,"
            " scraper_name, scraper_fingerprint, attribution, method, target,"
            " target_key, input_json, status, started_at, finished_at, duration_ms,"
            " result_count, from_cache, cached_from, error, error_kind)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,1,?,?,?)",
            (scan_id, int(scene_id), parent_id, int(depth),
             source_attempt["scraper_id"], source_attempt["scraper_name"],
             source_attempt["scraper_fingerprint"], source_attempt["attribution"],
             source_attempt["method"], source_attempt["target"],
             source_attempt["target_key"], source_attempt["input_json"],
             source_attempt["status"], stamp, stamp,
             source_attempt["result_count"], origin,
             source_attempt.get("error"), source_attempt.get("error_kind")),
        )
        return cursor.lastrowid

    def results_via(self, attempt):
        """Results belonging to an attempt, following a cache reference if there is one."""
        return self.results_of_attempt(attempt.get("cached_from") or attempt["id"])

    def attempts_of_scan(self, scan_id):
        return self._all(
            "SELECT * FROM attempts WHERE scan_id = ? ORDER BY started_at, id", (scan_id,)
        )

    def attempts_of_scene(self, scene_id, limit=500, offset=0):
        return self._all(
            "SELECT * FROM attempts WHERE scene_id = ?"
            " ORDER BY started_at DESC, id DESC LIMIT ? OFFSET ?",
            (int(scene_id), int(limit), int(offset)),
        )

    def attempt_summary(self, scan_id):
        return self._all(
            "SELECT status, COUNT(*) AS n FROM attempts WHERE scan_id = ? GROUP BY status",
            (scan_id,),
        )

    # -- results and blobs -------------------------------------------------

    def put_blob(self, sha256, mime, data) -> str:
        """Store an image once, however many scrapers return it."""
        self._write(
            "INSERT INTO blobs(sha256, mime, bytes, data, created_at)"
            " VALUES(?,?,?,?,?) ON CONFLICT(sha256) DO NOTHING",
            (sha256, mime or "", len(data), sqlite3.Binary(data), now()),
        )
        return sha256

    def blob(self, sha256):
        row = self._one("SELECT sha256, mime, bytes, data FROM blobs WHERE sha256 = ?",
                        (sha256,))
        if row:
            row["data"] = bytes(row["data"])
        return row

    def add_result(self, attempt_id, ordinal, raw, raw_fingerprint, normalized=None,
                   normalized_fingerprint=None, norm_version=0, image_sha256=None) -> int:
        cursor = self._write(
            "INSERT INTO results(attempt_id, ordinal, raw_json, raw_fingerprint,"
            " normalized_json, normalized_fingerprint, norm_version, image_sha256)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (attempt_id, int(ordinal), _dumps(raw), raw_fingerprint,
             _dumps(normalized) if normalized is not None else None,
             normalized_fingerprint, int(norm_version), image_sha256),
        )
        return cursor.lastrowid

    def results_of_attempt(self, attempt_id):
        return [self._result_row(row) for row in self._all(
            "SELECT * FROM results WHERE attempt_id = ? ORDER BY ordinal", (attempt_id,)
        )]

    def results_of_scene(self, scene_id, include_raw=False):
        """Every stored result for a scene, newest attempt first.

        `include_raw` is off by default because raw payloads are the largest thing in
        the database and the inbox never needs them.
        """
        columns = ("r.id, r.attempt_id, r.ordinal, r.raw_fingerprint,"
                   " r.normalized_json, r.normalized_fingerprint, r.norm_version,"
                   " r.image_sha256, a.scraper_id, a.scraper_name, a.method,"
                   " a.target, a.depth, a.attribution, a.started_at")
        if include_raw:
            columns += ", r.raw_json"
        rows = self._all(
            "SELECT " + columns + " FROM results r JOIN attempts a ON a.id = r.attempt_id"
            " WHERE a.scene_id = ? ORDER BY a.started_at DESC, r.ordinal",
            (int(scene_id),),
        )
        return [self._result_row(row) for row in rows]

    @staticmethod
    def _result_row(row):
        row = dict(row)
        if "raw_json" in row:
            row["raw"] = _loads(row.pop("raw_json"), {})
        row["normalized"] = _loads(row.pop("normalized_json", None), None)
        return row

    def result_with_context(self, result_id):
        """One result including its raw payload and the attempt that produced it."""
        row = self._one(
            "SELECT r.*, a.scraper_id, a.scraper_name, a.method, a.target, a.depth,"
            " a.attribution, a.started_at, a.scan_id"
            " FROM results r JOIN attempts a ON a.id = r.attempt_id WHERE r.id = ?",
            (int(result_id),))
        return self._result_row(row) if row else None

    def attempt_id_of_result(self, result_id):
        """Which attempt produced a result - the parent edge of the discovery graph."""
        if not result_id:
            return None
        row = self._one("SELECT attempt_id FROM results WHERE id = ?", (result_id,))
        return (row or {}).get("attempt_id")

    def result_count_for_scene(self, scene_id) -> int:
        row = self._one(
            "SELECT COUNT(*) AS n FROM results r JOIN attempts a ON a.id = r.attempt_id"
            " WHERE a.scene_id = ?",
            (int(scene_id),),
        )
        return row["n"] if row else 0

    # -- discovered urls ---------------------------------------------------

    def add_url(self, scan_id, scene_id, url, normalized, host, norm_key, depth,
                handler_ids, source_result_id=None, state="PENDING"):
        """Record a URL for this scan. Returns its id, or None if already known.

        The unique index on (scan_id, norm_key) is the loop guard: the same URL cannot
        enter one scan's work list twice, however many results mention it.
        """
        try:
            cursor = self._write(
                "INSERT INTO discovered_urls(scan_id, scene_id, url, normalized, host,"
                " norm_key, depth, source_result_id, handler_ids_json, handler_count,"
                " state, found_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (scan_id, int(scene_id), url, normalized, host, norm_key, int(depth),
                 source_result_id, _dumps(list(handler_ids)), len(handler_ids), state,
                 now()),
            )
            return cursor.lastrowid
        except sqlite3.IntegrityError:
            return None

    def set_url_state(self, url_id, state, attempt_id=None) -> None:
        self._write(
            "UPDATE discovered_urls SET state = ?,"
            " attempt_id = COALESCE(?, attempt_id) WHERE id = ?",
            (state, attempt_id, url_id),
        )

    def pending_urls(self, scan_id, limit=50):
        return self._all(
            "SELECT * FROM discovered_urls WHERE scan_id = ? AND state = 'PENDING'"
            " ORDER BY depth, id LIMIT ?",
            (scan_id, int(limit)),
        )

    def urls_of_scan(self, scan_id):
        return [self._url_row(row) for row in self._all(
            "SELECT * FROM discovered_urls WHERE scan_id = ? ORDER BY depth, id",
            (scan_id,),
        )]

    @staticmethod
    def _url_row(row):
        row = dict(row)
        row["handlers"] = _loads(row.pop("handler_ids_json", None), [])
        return row

    def url_count(self, scan_id) -> int:
        row = self._one("SELECT COUNT(*) AS n FROM discovered_urls WHERE scan_id = ?",
                        (scan_id,))
        return row["n"] if row else 0

    # -- candidates --------------------------------------------------------

    def clear_candidates(self, scene_id) -> None:
        """Candidates are derived data; rebuilding replaces them wholesale."""
        self._write("DELETE FROM candidates WHERE scene_id = ?", (int(scene_id),))

    def add_candidate(self, scene_id, scan_id, identity_key, confidence, level,
                      score, merged, source_count, independent_source_count,
                      correlation_version, scoring_version) -> int:
        stamp = now()
        cursor = self._write(
            "INSERT INTO candidates(scene_id, scan_id, identity_key, confidence, level,"
            " score_json, merged_json, source_count, independent_source_count, state,"
            " correlation_version, scoring_version, created_at, updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,'NEW',?,?,?,?)",
            (int(scene_id), scan_id, identity_key, confidence, level, _dumps(score),
             _dumps(merged), int(source_count), int(independent_source_count),
             int(correlation_version), int(scoring_version), stamp, stamp),
        )
        return cursor.lastrowid

    def add_candidate_source(self, candidate_id, result_id, attempt_id, scraper_id,
                             scraper_name, host, role="support") -> None:
        self._write(
            "INSERT INTO candidate_sources(candidate_id, result_id, attempt_id,"
            " scraper_id, scraper_name, host, role) VALUES(?,?,?,?,?,?,?)"
            " ON CONFLICT(candidate_id, result_id) DO NOTHING",
            (candidate_id, result_id, attempt_id, scraper_id, scraper_name, host or "",
             role),
        )

    def add_candidate_field(self, candidate_id, field, value_key, value, sources) -> int:
        cursor = self._write(
            "INSERT INTO candidate_fields(candidate_id, field, value_key, value_json,"
            " source_count) VALUES(?,?,?,?,?)"
            " ON CONFLICT(candidate_id, field, value_key) DO UPDATE SET"
            " source_count = excluded.source_count",
            (candidate_id, field, value_key, _dumps(value), len(sources)),
        )
        field_id = cursor.lastrowid
        for source in sources:
            self._write(
                "INSERT INTO candidate_field_sources(candidate_field_id, result_id,"
                " scraper_id, scraper_name) VALUES(?,?,?,?)"
                " ON CONFLICT(candidate_field_id, result_id) DO NOTHING",
                (field_id, source.get("result_id"), source.get("scraper_id"),
                 source.get("scraper_name")),
            )
        return field_id

    def candidates_of_scene(self, scene_id):
        return [self._candidate_row(row) for row in self._all(
            "SELECT * FROM candidates WHERE scene_id = ?"
            " ORDER BY confidence DESC, id ASC",
            (int(scene_id),),
        )]

    def candidate(self, candidate_id):
        row = self._one("SELECT * FROM candidates WHERE id = ?", (candidate_id,))
        return self._candidate_row(row) if row else None

    @staticmethod
    def _candidate_row(row):
        row = dict(row)
        row["score"] = _loads(row.pop("score_json", None), {})
        row["merged"] = _loads(row.pop("merged_json", None), {})
        return row

    def candidate_sources(self, candidate_id):
        return self._all(
            "SELECT * FROM candidate_sources WHERE candidate_id = ?", (candidate_id,)
        )

    def candidate_fields(self, candidate_id):
        fields = self._all(
            "SELECT * FROM candidate_fields WHERE candidate_id = ? ORDER BY field,"
            " source_count DESC",
            (candidate_id,),
        )
        for field in fields:
            field["value"] = _loads(field.pop("value_json"), None)
            field["sources"] = self._all(
                "SELECT result_id, scraper_id, scraper_name FROM candidate_field_sources"
                " WHERE candidate_field_id = ?",
                (field["id"],),
            )
        return fields

    def set_candidate_state(self, candidate_id, state) -> None:
        self._write(
            "UPDATE candidates SET state = ?, updated_at = ? WHERE id = ?",
            (state, now(), candidate_id),
        )

    # -- audit -------------------------------------------------------------

    def record_application(self, scene_id, candidate_id, mode, selection, before,
                           after, changes, status="APPLIED", error=None) -> int:
        cursor = self._write(
            "INSERT INTO applications(scene_id, candidate_id, applied_at, mode,"
            " selection_json, before_json, after_json, changes_json, status, error)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (int(scene_id), candidate_id, now(), mode, _dumps(selection),
             _dumps(before), _dumps(after), _dumps(changes), status, error),
        )
        return cursor.lastrowid

    def applications_of_scene(self, scene_id):
        rows = self._all(
            "SELECT * FROM applications WHERE scene_id = ? ORDER BY applied_at DESC",
            (int(scene_id),),
        )
        for row in rows:
            for key in ("selection", "before", "after", "changes"):
                row[key] = _loads(row.pop(key + "_json", None), None)
        return rows

    # -- scene state / inbox ------------------------------------------------

    def set_scene_status(self, scene_id, status, last_scan_id=None, **fields) -> None:
        columns = {"status": status, "updated_at": now()}
        if last_scan_id is not None:
            columns["last_scan_id"] = last_scan_id
        for key in ("last_scanned_at", "candidate_count", "best_confidence",
                    "attempt_count", "error_count", "url_count", "title", "path",
                    "studio_name"):
            if key in fields:
                columns[key] = fields[key]
        names = ["scene_id"] + list(columns)
        placeholders = ",".join("?" for _ in names)
        updates = ", ".join(f"{key} = excluded.{key}" for key in columns)
        self._write(
            "INSERT INTO scene_state(%s) VALUES(%s) ON CONFLICT(scene_id) DO UPDATE SET %s"
            % (", ".join(names), placeholders, updates),
            tuple([int(scene_id)] + [columns[key] for key in columns]),
        )

    def refresh_scene_state(self, scene_id, scene=None) -> dict:
        """Recompute the inbox row for one scene from what is stored.

        Derived, so it can always be rebuilt; kept in a table, so the inbox can page
        and sort in SQL without opening a raw result.
        """
        scene_id = int(scene_id)
        last = self._one(
            "SELECT * FROM scans WHERE scene_id = ? ORDER BY started_at DESC, id DESC"
            " LIMIT 1",
            (scene_id,),
        )
        counts = self._one(
            "SELECT COUNT(*) AS attempts,"
            " SUM(CASE WHEN status IN ('ERROR','TIMEOUT') THEN 1 ELSE 0 END) AS errors"
            " FROM attempts WHERE scene_id = ?",
            (scene_id,),
        ) or {}
        best = self._one(
            "SELECT COUNT(*) AS n, MAX(confidence) AS best FROM candidates"
            " WHERE scene_id = ? AND state != 'DISMISSED'",
            (scene_id,),
        ) or {}
        applied = self._one(
            "SELECT COUNT(*) AS n FROM applications WHERE scene_id = ? AND status = 'APPLIED'",
            (scene_id,),
        ) or {}
        urls = self._one(
            "SELECT COUNT(DISTINCT norm_key) AS n FROM discovered_urls WHERE scene_id = ?",
            (scene_id,),
        ) or {}
        matched = self._one(
            "SELECT COUNT(*) AS n FROM attempts WHERE scene_id = ? AND status = 'MATCH'",
            (scene_id,),
        ) or {}

        candidate_count = best.get("n") or 0
        if applied.get("n"):
            status = APPLIED
        elif last is None:
            status = UNSCANNED
        elif last["status"] == SCAN_RUNNING:
            status = SCANNING
        elif last["status"] == SCAN_FAILED:
            status = FAILED
        elif candidate_count:
            status = CANDIDATES
        elif matched.get("n"):
            # Scrapers answered; nothing has correlated those answers yet.
            status = RESULTS
        elif (counts.get("attempts") or 0) and (counts.get("errors") or 0) == (
                counts.get("attempts") or 0):
            # Every attempt failed, so "nothing found" would be misleading.
            status = FAILED
        else:
            status = NO_RESULTS

        fields = {
            "last_scanned_at": (last or {}).get("finished_at") or (last or {}).get("started_at"),
            "candidate_count": candidate_count,
            "best_confidence": best.get("best"),
            "attempt_count": counts.get("attempts") or 0,
            "error_count": counts.get("errors") or 0,
            "url_count": urls.get("n") or 0,
        }
        if scene:
            fields["title"] = scene.get("display_title") or ""
            fields["path"] = scene.get("path") or ""
            fields["studio_name"] = ((scene.get("studio") or {}) or {}).get("name") or ""

        self.set_scene_status(scene_id, status,
                              last_scan_id=(last or {}).get("id"), **fields)
        return self._one("SELECT * FROM scene_state WHERE scene_id = ?", (scene_id,))

    def inbox(self, status=None, query="", min_confidence=None, studio="", scraper="",
              has_errors=None, sort="last_scanned_at", direction="desc",
              page=1, per_page=50):
        """Page over the inbox entirely in SQL.

        The sort column comes from a whitelist rather than the caller's string, since
        it cannot be parameterised.
        """
        sortable = {
            "scene_id": "s.scene_id",
            "status": "s.status",
            "confidence": "s.best_confidence",
            "best_confidence": "s.best_confidence",
            "candidates": "s.candidate_count",
            "candidate_count": "s.candidate_count",
            "last_scanned_at": "s.last_scanned_at",
            "attempts": "s.attempt_count",
            "errors": "s.error_count",
            "title": "s.title",
            "studio": "s.studio_name",
        }
        column = sortable.get(sort, "s.last_scanned_at")
        order = "DESC" if str(direction).lower() != "asc" else "ASC"

        where, params = [], []
        if status and status != "all":
            wanted = [part.strip() for part in str(status).split(",") if part.strip()]
            where.append("s.status IN (%s)" % ",".join("?" for _ in wanted))
            params.extend(wanted)
        if query:
            where.append("(s.title LIKE ? OR s.path LIKE ? OR CAST(s.scene_id AS TEXT) = ?)")
            like = "%" + str(query) + "%"
            params.extend([like, like, str(query)])
        if min_confidence not in (None, ""):
            where.append("s.best_confidence >= ?")
            params.append(float(min_confidence))
        if studio:
            where.append("s.studio_name LIKE ?")
            params.append("%" + str(studio) + "%")
        if has_errors is True:
            where.append("s.error_count > 0")
        elif has_errors is False:
            where.append("s.error_count = 0")
        if scraper:
            where.append(
                "EXISTS (SELECT 1 FROM attempts a WHERE a.scene_id = s.scene_id"
                " AND (a.scraper_id = ? OR a.scraper_name = ?))"
            )
            params.extend([scraper, scraper])

        clause = (" WHERE " + " AND ".join(where)) if where else ""
        total = self._one("SELECT COUNT(*) AS n FROM scene_state s" + clause, tuple(params))
        per_page = max(1, min(int(per_page), 200))
        page = max(1, int(page))
        rows = self._all(
            "SELECT s.* FROM scene_state s" + clause +
            " ORDER BY %s %s, s.scene_id DESC LIMIT ? OFFSET ?" % (column, order),
            tuple(params) + (per_page, (page - 1) * per_page),
        )
        return {
            "total": (total or {}).get("n") or 0,
            "page": page,
            "per_page": per_page,
            "items": rows,
        }

    def scene_state(self, scene_id):
        return self._one("SELECT * FROM scene_state WHERE scene_id = ?", (int(scene_id),))

    def status_counts(self) -> dict:
        return {
            row["status"]: row["n"]
            for row in self._all(
                "SELECT status, COUNT(*) AS n FROM scene_state GROUP BY status"
            )
        }

    def scenes_by_status(self, statuses, limit=1000):
        placeholders = ",".join("?" for _ in statuses)
        return [
            row["scene_id"]
            for row in self._all(
                "SELECT scene_id FROM scene_state WHERE status IN (%s)"
                " ORDER BY COALESCE(last_scanned_at, '') ASC LIMIT ?" % placeholders,
                tuple(statuses) + (int(limit),),
            )
        ]

    # -- statistics and maintenance ----------------------------------------

    def scraper_stats(self, limit=1000):
        return self._all(
            "SELECT * FROM scraper_stats ORDER BY attempts DESC LIMIT ?", (int(limit),)
        )

    def scraper_stats_full(self, include_untried=True):
        """Every scraper we know of, with its record, whether it has one or not.

        A LEFT JOIN rather than reading the view alone: the view only has rows for
        scrapers that have been attempted, and "installed, never tried" is exactly the
        state a user wants to see next to "tried 300 times, matched twice".
        """
        rows = self._all(
            "SELECT s.id, s.name, s.kinds_json, s.url_patterns_json,"
            " s.first_seen, s.last_seen,"
            " COALESCE(st.attempts, 0)   AS attempts,"
            " COALESCE(st.matches, 0)    AS matches,"
            " COALESCE(st.no_matches, 0) AS no_matches,"
            " COALESCE(st.errors, 0)     AS errors,"
            " COALESCE(st.timeouts, 0)   AS timeouts,"
            " st.avg_ms                  AS avg_ms,"
            " st.last_attempt_at         AS last_attempt_at"
            " FROM scrapers s LEFT JOIN scraper_stats st ON st.scraper_id = s.id"
            " ORDER BY COALESCE(st.attempts, 0) DESC, s.id"
        )
        out = []
        for row in rows:
            if not include_untried and not row["attempts"]:
                continue
            row["kinds"] = _loads(row.pop("kinds_json"), [])
            row["url_patterns"] = _loads(row.pop("url_patterns_json"), [])
            row["results"] = 0
            out.append(row)
        return out

    def scraper_errors(self, signature=None, max_groups=20000):
        """Per scraper: how it typically fails, and what it said most recently.

        Aggregated in SQL down to distinct messages first, then folded together by
        `signature` in Python - the messages embed the URL that was fetched, so grouping
        on the raw text would report one recurring fault as a hundred separate ones.
        There are far fewer distinct messages than attempts, so this stays cheap.
        """
        signature = signature or (lambda text: str(text or "")[:180])
        rows = self._all(
            "SELECT scraper_id, error, error_kind, COUNT(*) AS n,"
            " MAX(finished_at) AS last_at"
            " FROM attempts"
            " WHERE scraper_id IS NOT NULL AND error IS NOT NULL AND error != ''"
            "   AND from_cache = 0"
            " GROUP BY scraper_id, error, error_kind"
            " ORDER BY n DESC LIMIT ?",
            (int(max_groups),),
        )

        out = {}
        for row in rows:
            entry = out.setdefault(row["scraper_id"], {
                "groups": {}, "total": 0, "last": None, "permanent": 0, "transient": 0,
            })
            key = signature(row["error"])
            group = entry["groups"].setdefault(key, {
                "signature": key, "count": 0, "kind": row["error_kind"],
                "example": row["error"], "last_at": row["last_at"],
            })
            group["count"] += row["n"]
            if (row["last_at"] or "") > (group["last_at"] or ""):
                group["last_at"] = row["last_at"]
                group["example"] = row["error"]
            entry["total"] += row["n"]
            if row["error_kind"] == "permanent":
                entry["permanent"] += row["n"]
            else:
                entry["transient"] += row["n"]
            if entry["last"] is None or (row["last_at"] or "") > (entry["last"]["at"] or ""):
                entry["last"] = {"at": row["last_at"], "message": row["error"],
                                 "kind": row["error_kind"]}

        for entry in out.values():
            groups = sorted(entry.pop("groups").values(),
                            key=lambda one: (-one["count"], one["signature"]))
            entry["top"] = groups[0] if groups else None
            entry["distinct"] = len(groups)
            entry["groups"] = groups[:5]
        return out

    def results_per_scraper(self):
        """scraper id -> how many stored results it produced.

        Not the same as its match count: one name search can answer with a dozen
        results, and a match is one attempt.
        """
        return {
            row["scraper_id"]: row["n"]
            for row in self._all(
                "SELECT a.scraper_id, COUNT(r.id) AS n FROM attempts a"
                " JOIN results r ON r.attempt_id = a.id"
                " WHERE a.scraper_id IS NOT NULL GROUP BY a.scraper_id"
            )
        }

    def forget_scraper(self, scraper_id):
        """Drop a scraper from the registry, keeping its history.

        Called after an uninstall. The attempts stay: they are the record of what was
        tried and why it was not worth keeping, and deleting them would also delete the
        results other scrapers' candidates may be correlated against.
        """
        cursor = self._write("DELETE FROM scrapers WHERE id = ?", (str(scraper_id),))
        return cursor.rowcount or 0

    def counts(self) -> dict:
        tables = ("scans", "attempts", "results", "discovered_urls", "candidates",
                  "applications", "scene_state", "scrapers", "blobs")
        out = {}
        for table in tables:
            # Table names are from this literal tuple, never from a caller.
            out[table] = self.db.execute("SELECT COUNT(*) AS n FROM " + table).fetchone()["n"]
        pages = self.db.execute("PRAGMA page_count").fetchone()[0]
        size = self.db.execute("PRAGMA page_size").fetchone()[0]
        out["bytes"] = pages * size
        return out

    def prune_history(self, days) -> dict:
        """Delete scans older than `days`. Cascades to attempts and results."""
        if not days:
            return {"scans": 0}
        cutoff = ago(float(days) * 86400.0)
        cursor = self._write(
            "DELETE FROM scans WHERE finished_at IS NOT NULL AND finished_at < ?",
            (cutoff,),
        )
        return {"scans": cursor.rowcount or 0}

    def prune_orphan_blobs(self) -> int:
        cursor = self._write(
            "DELETE FROM blobs WHERE sha256 NOT IN"
            " (SELECT image_sha256 FROM results WHERE image_sha256 IS NOT NULL)"
        )
        return cursor.rowcount or 0

    def clear_expired_cache(self, ttl_days_for) -> int:
        """Drop expired attempts that carry nothing worth keeping.

        Deliberately narrow: only NO_MATCH, ERROR and TIMEOUT rows go, because those
        exist purely to answer "do not ask again yet". A MATCH row owns results, and
        those are the raw material every later stage can be rebuilt from, so expiring
        one would throw away discovery information - the retention setting is the only
        thing allowed to remove those, and it removes whole scans with their context.
        """
        removed = 0
        # (status, error_kind) rather than status alone: a permanent error is cached as
        # long as a no-match, so expiring it on the short error TTL would put a hundred
        # doomed attempts per scene back on the work list.
        buckets = ((NO_MATCH, None), (TIMEOUT, None),
                   (ERROR, "transient"), (ERROR, "permanent"))
        for status, kind in buckets:
            days = ttl_days_for(status, kind)
            if not days:
                continue
            cutoff = ago(float(days) * 86400.0)
            if kind is None:
                cursor = self._write(
                    "DELETE FROM attempts WHERE status = ? AND finished_at IS NOT NULL"
                    " AND finished_at < ? AND result_count = 0",
                    (status, cutoff),
                )
            else:
                cursor = self._write(
                    "DELETE FROM attempts WHERE status = ? AND finished_at IS NOT NULL"
                    " AND finished_at < ? AND result_count = 0"
                    " AND COALESCE(error_kind, 'transient') = ?",
                    (status, cutoff, kind),
                )
            removed += cursor.rowcount or 0
        return removed

    def vacuum(self) -> None:
        # VACUUM cannot run inside a transaction, so drop to autocommit for it.
        previous = self.db.isolation_level
        try:
            self.db.isolation_level = None
            self.db.execute("VACUUM")
            self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            self.db.isolation_level = previous
