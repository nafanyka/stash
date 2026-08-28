"""The only module that talks SQL.

Everything above this layer passes and receives plain dicts, so the schema can change
without the rest of the plugin noticing. Two rules hold throughout:

* every value reaches SQLite as a bound parameter - scraper output is untrusted input
  and never becomes part of a statement;
* transactions are short and never span a scraper call. A run writes its source row,
  drops the transaction, makes the network call, then commits the result. That keeps a
  slow scraper from blocking the database, and means a killed process (Stash's
  `stopJob` kills outright) loses at most the one source that was in flight.

Timestamps are ISO-8601 UTC with a `Z` suffix, which sort lexicographically, so the
stale-run sweep is a plain string comparison in SQL.
"""

from __future__ import annotations

import datetime
import json
import os
import sqlite3

from . import migrations

# Run statuses. READY_FOR_REVIEW and READY_WITH_ERRORS are both reviewable; the second
# exists so the UI can say "and two sources failed" without hiding the results
# (requirement 29, 54).
RUNNING = "RUNNING"
READY_FOR_REVIEW = "READY_FOR_REVIEW"
READY_WITH_ERRORS = "READY_WITH_ERRORS"
NO_RESULTS = "NO_RESULTS"
APPLIED = "APPLIED"
REJECTED = "REJECTED"
FAILED = "FAILED"
FAILED_APPLY = "FAILED_APPLY"
CANCELLED = "CANCELLED"

# Statuses whose payload is still there and still worth a decision.
REVIEWABLE = (READY_FOR_REVIEW, READY_WITH_ERRORS, FAILED_APPLY)
# Statuses that mean the run is over, whatever the outcome.
FINISHED = (READY_FOR_REVIEW, READY_WITH_ERRORS, NO_RESULTS, APPLIED, REJECTED,
            FAILED, FAILED_APPLY, CANCELLED)

# Source statuses.
S_RUNNING = "RUNNING"
S_OK = "OK"
S_NO_RESULT = "NO_RESULT"
S_ERROR = "ERROR"
S_TIMEOUT = "TIMEOUT"
S_SKIPPED = "SKIPPED"
S_UNREACHABLE = "UNREACHABLE"

# URL states.
U_PENDING = "PENDING"
U_SCRAPED = "SCRAPED"
U_NO_HANDLER = "NO_HANDLER"
U_SKIPPED_DEPTH = "SKIPPED_DEPTH"
U_SKIPPED_LIMIT = "SKIPPED_LIMIT"
U_RELATED = "RELATED"


def now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ago(seconds: float) -> str:
    moment = (datetime.datetime.now(datetime.timezone.utc)
              - datetime.timedelta(seconds=seconds))
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

    Not the plugin directory: installing a newer package version deletes every file the
    previous one installed (`pkg/pkg/manager.go`), and uninstalling removes the
    directory. The Stash config directory survives both.
    """
    base = stash_config_dir or os.getcwd()
    return os.path.join(base, "fast-discovery", "fastdiscovery.sqlite")


class Repo:
    """FastDiscovery's database. One connection, owned by one thread."""

    def __init__(self, connection, path):
        self.connection = connection
        self.path = path

    # -- lifecycle ---------------------------------------------------------

    @classmethod
    def open(cls, path):
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        connection = sqlite3.connect(path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=15000")
        migrations.migrate(connection)
        return cls(connection, path)

    def close(self):
        try:
            self.connection.close()
        except sqlite3.Error:
            pass

    def schema_version(self):
        return self.connection.execute("PRAGMA user_version").fetchone()[0]

    def vacuum(self):
        self.connection.execute("VACUUM")

    def counts(self):
        out = {}
        for table in ("runs", "sources", "results", "urls", "images", "applications"):
            out[table] = self.connection.execute(
                "SELECT COUNT(*) FROM %s" % table).fetchone()[0]
        try:
            out["bytes"] = os.path.getsize(self.path)
        except OSError:
            out["bytes"] = 0
        return out

    # -- meta --------------------------------------------------------------

    def meta_get(self, key, fallback=None):
        row = self.connection.execute("SELECT value FROM meta WHERE key = ?",
                                      (str(key),)).fetchone()
        return row["value"] if row else fallback

    def meta_set(self, key, value):
        with self.connection:
            self.connection.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(key), None if value is None else str(value)))

    # -- runs --------------------------------------------------------------

    def start_run(self, scene_id, trigger, config, snapshot, job_id=None):
        with self.connection:
            cursor = self.connection.execute(
                "INSERT INTO runs(scene_id, status, trigger, job_id, started_at,"
                " heartbeat_at, config_json, scene_snapshot_json)"
                " VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                (int(scene_id), RUNNING, str(trigger), str(job_id) if job_id else None,
                 now(), now(), _dumps(config or {}), _dumps(snapshot or {})))
        return cursor.lastrowid

    def heartbeat(self, run_id, progress=None):
        with self.connection:
            self.connection.execute(
                "UPDATE runs SET heartbeat_at = ?, progress_json = COALESCE(?,"
                " progress_json) WHERE id = ?",
                (now(), _dumps(progress) if progress is not None else None,
                 int(run_id)))

    def update_run_counts(self, run_id, **counts):
        allowed = ("source_count", "ok_source_count", "error_count", "url_count",
                   "result_count", "max_depth_reached")
        pairs = [(name, int(value)) for name, value in counts.items()
                 if name in allowed and value is not None]
        if not pairs:
            return
        assignments = ", ".join("%s = ?" % name for name, _ in pairs)
        with self.connection:
            self.connection.execute(
                "UPDATE runs SET %s WHERE id = ?" % assignments,
                tuple(value for _, value in pairs) + (int(run_id),))

    def finish_run(self, run_id, status, stop_reason=None, error=None):
        with self.connection:
            self.connection.execute(
                "UPDATE runs SET status = ?, finished_at = ?, heartbeat_at = ?,"
                " stop_reason = ?, error = ? WHERE id = ?",
                (str(status), now(), now(), stop_reason, error, int(run_id)))

    def set_run_status(self, run_id, status, error=None):
        with self.connection:
            self.connection.execute(
                "UPDATE runs SET status = ?, error = ?, decided_at = ? WHERE id = ?",
                (str(status), error, now(), int(run_id)))

    def set_rejected_sources(self, run_id, source_ids):
        """Which of this run's sources the reviewer struck out."""
        with self.connection:
            self.connection.execute(
                "UPDATE runs SET rejected_sources_json = ? WHERE id = ?",
                (_dumps(sorted({int(one) for one in (source_ids or [])})),
                 int(run_id)))

    def set_selection(self, run_id, selection):
        with self.connection:
            self.connection.execute("UPDATE runs SET selection_json = ? WHERE id = ?",
                                    (_dumps(selection or {}), int(run_id)))

    def set_job_id(self, run_id, job_id):
        with self.connection:
            self.connection.execute("UPDATE runs SET job_id = ? WHERE id = ?",
                                    (str(job_id) if job_id else None, int(run_id)))

    def run(self, run_id):
        row = self.connection.execute("SELECT * FROM runs WHERE id = ?",
                                      (int(run_id),)).fetchone()
        return self._run_row(row)

    def latest_run(self, scene_id, statuses=None):
        query = "SELECT * FROM runs WHERE scene_id = ?"
        params = [int(scene_id)]
        if statuses:
            query += " AND status IN (%s)" % ",".join("?" * len(statuses))
            params.extend(statuses)
        query += " ORDER BY started_at DESC, id DESC LIMIT 1"
        return self._run_row(self.connection.execute(query, params).fetchone())

    def active_run(self, scene_id):
        return self.latest_run(scene_id, [RUNNING])

    def reviewable_run(self, scene_id):
        return self.latest_run(scene_id, list(REVIEWABLE))

    def list_runs(self, statuses=None, page=1, per_page=25, scene_ids=None):
        where, params = ["1 = 1"], []
        if statuses:
            where.append("status IN (%s)" % ",".join("?" * len(statuses)))
            params.extend(statuses)
        if scene_ids:
            where.append("scene_id IN (%s)" % ",".join("?" * len(scene_ids)))
            params.extend(int(one) for one in scene_ids)
        clause = " AND ".join(where)
        total = self.connection.execute(
            "SELECT COUNT(*) FROM runs WHERE " + clause, params).fetchone()[0]
        page = max(1, int(page))
        per_page = max(1, min(200, int(per_page)))
        rows = self.connection.execute(
            "SELECT * FROM runs WHERE " + clause
            + " ORDER BY started_at DESC, id DESC LIMIT ? OFFSET ?",
            params + [per_page, (page - 1) * per_page]).fetchall()
        return total, [self._run_row(row) for row in rows]

    def status_counts(self):
        rows = self.connection.execute(
            "SELECT status, COUNT(*) AS total FROM runs GROUP BY status").fetchall()
        return {row["status"]: row["total"] for row in rows}

    def sweep_stale_runs(self, hours):
        """Mark runs whose process died as FAILED.

        `stopJob` kills the plugin process outright and a crash is no gentler, so a run
        left RUNNING is not evidence that anything is running. The heartbeat is what
        tells the two apart.
        """
        cutoff = ago(float(hours) * 3600.0)
        with self.connection:
            cursor = self.connection.execute(
                "UPDATE runs SET status = ?, finished_at = ?,"
                " error = COALESCE(error, 'the run stopped without finishing')"
                " WHERE status = ? AND COALESCE(heartbeat_at, started_at) < ?",
                (FAILED, now(), RUNNING, cutoff))
        return cursor.rowcount or 0

    def _run_row(self, row):
        if row is None:
            return None
        run = dict(row)
        run["config"] = _loads(run.pop("config_json", None), {})
        run["scene_snapshot"] = _loads(run.pop("scene_snapshot_json", None), {})
        run["selection"] = _loads(run.pop("selection_json", None), None)
        run["rejected_sources"] = _loads(run.pop("rejected_sources_json", None), []) or []
        run["progress"] = _loads(run.pop("progress_json", None), {})
        run["purged"] = bool(run.get("purged"))
        run["reviewable"] = run["status"] in REVIEWABLE and not run["purged"]
        return run

    # -- purge -------------------------------------------------------------

    def purge_run(self, run_id):
        """Drop a decided run's payload, keeping the audit row (requirement 24).

        Everything the user could still be shown goes: raw responses, discovered
        metadata, image candidates, the URL graph, the draft selection. What stays is
        the run row's counters and timestamps, which is what "this scene was discovered
        and applied on Tuesday" needs and no more.
        """
        run_id = int(run_id)
        with self.connection:
            self.connection.execute("DELETE FROM results WHERE run_id = ?", (run_id,))
            self.connection.execute("DELETE FROM urls WHERE run_id = ?", (run_id,))
            self.connection.execute("DELETE FROM sources WHERE run_id = ?", (run_id,))
            self.connection.execute(
                "UPDATE runs SET purged = 1, scene_snapshot_json = NULL,"
                " selection_json = NULL, progress_json = NULL, config_json = NULL,"
                " rejected_sources_json = NULL WHERE id = ?", (run_id,))
        return self.purge_orphan_images()

    def delete_run(self, run_id):
        """Remove a run completely. Used when a rescan replaces an undecided run."""
        with self.connection:
            self.connection.execute("DELETE FROM runs WHERE id = ?", (int(run_id),))
        return self.purge_orphan_images()

    def purge_orphan_images(self):
        with self.connection:
            cursor = self.connection.execute(
                "DELETE FROM images WHERE sha256 NOT IN"
                " (SELECT image_sha256 FROM results WHERE image_sha256 IS NOT NULL)")
        return cursor.rowcount or 0

    # -- sources -----------------------------------------------------------

    def begin_source(self, run_id, scene_id, source):
        """Open a source row, or return None if this run already has that exact source.

        The unique index on (run_id, source_key) is the (scraper, url) guard from
        requirement 4: the database refuses the duplicate rather than the caller
        remembering to.
        """
        try:
            with self.connection:
                cursor = self.connection.execute(
                    "INSERT INTO sources(run_id, scene_id, type, method, name, endpoint,"
                    " scraper_id, url, url_key, host, target, depth, parent_source_id,"
                    " source_key, attribution, handlers_json, status, started_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (int(run_id), int(scene_id), source["type"],
                     source.get("method") or "", source.get("name") or "",
                     source.get("endpoint"), source.get("scraper_id"),
                     source.get("url"), source.get("url_key"), source.get("host"),
                     source.get("target"), int(source.get("depth") or 0),
                     source.get("parent_source_id"), source.get("source_key") or "",
                     source.get("attribution") or "CERTAIN",
                     _dumps(source.get("handlers") or []), S_RUNNING, now()))
            return cursor.lastrowid
        except sqlite3.IntegrityError:
            return None

    def finish_source(self, source_id, status, duration_ms=0, result_count=0,
                      error=None):
        with self.connection:
            self.connection.execute(
                "UPDATE sources SET status = ?, finished_at = ?, duration_ms = ?,"
                " result_count = ?, error = ? WHERE id = ?",
                (str(status), now(), int(duration_ms or 0), int(result_count or 0),
                 error, int(source_id)))

    def add_terminal_source(self, run_id, scene_id, source, status, error=None):
        """Record a source that was never run - unreachable, or skipped by a limit."""
        source_id = self.begin_source(run_id, scene_id, source)
        if source_id is None:
            return None
        self.finish_source(source_id, status, 0, 0, error)
        return source_id

    def sources_of(self, run_id):
        rows = self.connection.execute(
            "SELECT * FROM sources WHERE run_id = ? ORDER BY depth, id",
            (int(run_id),)).fetchall()
        out = []
        for row in rows:
            source = dict(row)
            source["handlers"] = _loads(source.pop("handlers_json", None), [])
            out.append(source)
        return out

    def source_counts(self, run_id):
        """{total, ok, errors} counted from the rows themselves.

        Counted rather than accumulated so the numbers the review shows cover the
        sources that were never dispatched too - one that no API can reach, one a limit
        dropped - which is exactly where an in-memory counter would quietly disagree
        with the source list underneath it.
        """
        rows = self.connection.execute(
            "SELECT status, COUNT(*) AS total FROM sources WHERE run_id = ?"
            " GROUP BY status", (int(run_id),)).fetchall()
        counts = {row["status"]: row["total"] for row in rows}
        return {
            "total": sum(counts.values()),
            "ok": counts.get(S_OK, 0),
            "errors": counts.get(S_ERROR, 0) + counts.get(S_TIMEOUT, 0)
            + counts.get(S_UNREACHABLE, 0),
        }

    def source(self, source_id):
        row = self.connection.execute("SELECT * FROM sources WHERE id = ?",
                                      (int(source_id),)).fetchone()
        if row is None:
            return None
        source = dict(row)
        source["handlers"] = _loads(source.pop("handlers_json", None), [])
        return source

    # -- results -----------------------------------------------------------

    def add_result(self, run_id, source_id, ordinal, raw, image_url=None,
                   image_sha256=None):
        with self.connection:
            cursor = self.connection.execute(
                "INSERT INTO results(run_id, source_id, ordinal, raw_json, image_url,"
                " image_sha256, created_at) VALUES(?,?,?,?,?,?,?)",
                (int(run_id), int(source_id), int(ordinal), _dumps(raw),
                 image_url, image_sha256, now()))
        return cursor.lastrowid

    def results_of(self, run_id):
        """Every result of a run, each carrying the source that produced it."""
        rows = self.connection.execute(
            "SELECT r.*, s.type AS source_type, s.name AS source_name,"
            " s.endpoint AS source_endpoint, s.url AS source_url,"
            " s.scraper_id AS source_scraper_id, s.method AS source_method,"
            " s.attribution AS source_attribution, s.depth AS source_depth,"
            " s.parent_source_id AS source_parent_id"
            " FROM results r JOIN sources s ON s.id = r.source_id"
            " WHERE r.run_id = ? ORDER BY s.depth, s.id, r.ordinal",
            (int(run_id),)).fetchall()
        out = []
        for row in rows:
            result = dict(row)
            result["raw"] = _loads(result.pop("raw_json", None), {})
            out.append(result)
        return out

    def result(self, result_id):
        row = self.connection.execute("SELECT * FROM results WHERE id = ?",
                                      (int(result_id),)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["raw"] = _loads(result.pop("raw_json", None), {})
        return result

    # -- urls --------------------------------------------------------------

    def add_url(self, run_id, record, depth, role="scene", origin="",
                discovered_by_result_id=None, handler_ids=None, state=U_PENDING,
                note=None):
        """Record a discovered URL, or return None if this run already has it.

        The unique index does the de-duplication, so "have we seen this URL?" is one
        insert rather than a read followed by a race.
        """
        try:
            with self.connection:
                cursor = self.connection.execute(
                    "INSERT INTO urls(run_id, url, normalized, norm_key, host, depth,"
                    " role, origin, discovered_by_result_id, handler_ids_json, state,"
                    " note, found_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (int(run_id), record["url"], record["normalized"], record["key"],
                     record.get("host") or "", int(depth), str(role), str(origin),
                     discovered_by_result_id, _dumps(list(handler_ids or [])),
                     str(state), note, now()))
            return cursor.lastrowid
        except sqlite3.IntegrityError:
            return None

    def set_url_state(self, url_id, state, note=None):
        with self.connection:
            self.connection.execute(
                "UPDATE urls SET state = ?, note = COALESCE(?, note) WHERE id = ?",
                (str(state), note, int(url_id)))

    def pending_urls(self, run_id, depth):
        rows = self.connection.execute(
            "SELECT * FROM urls WHERE run_id = ? AND state = ? AND depth = ?"
            " AND role = 'scene' ORDER BY id", (int(run_id), U_PENDING, int(depth))
        ).fetchall()
        return [dict(row) for row in rows]

    def urls_of(self, run_id):
        rows = self.connection.execute(
            "SELECT * FROM urls WHERE run_id = ? ORDER BY depth, id",
            (int(run_id),)).fetchall()
        out = []
        for row in rows:
            record = dict(row)
            record["handler_ids"] = _loads(record.pop("handler_ids_json", None), [])
            out.append(record)
        return out

    def url_count(self, run_id):
        return self.connection.execute(
            "SELECT COUNT(*) FROM urls WHERE run_id = ?", (int(run_id),)).fetchone()[0]

    # -- images ------------------------------------------------------------

    def put_image(self, sha256, mime, data):
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO images(sha256, mime, bytes, data, created_at)"
                " VALUES(?,?,?,?,?)",
                (str(sha256), str(mime or "image/jpeg"), len(data),
                 sqlite3.Binary(data), now()))

    def image(self, sha256):
        row = self.connection.execute(
            "SELECT sha256, mime, bytes, data FROM images WHERE sha256 = ?",
            (str(sha256),)).fetchone()
        if row is None:
            return None
        return {"sha256": row["sha256"], "mime": row["mime"], "bytes": row["bytes"],
                "data": bytes(row["data"])}

    # -- applications ------------------------------------------------------

    def add_application(self, run_id, scene_id, status, fields=None, created=None,
                        error=None):
        with self.connection:
            cursor = self.connection.execute(
                "INSERT INTO applications(run_id, scene_id, applied_at, status,"
                " fields_json, created_json, error) VALUES(?,?,?,?,?,?,?)",
                (int(run_id), int(scene_id), now(), str(status),
                 _dumps(list(fields or [])), _dumps(created or {}), error))
        return cursor.lastrowid

    def applications_for(self, scene_id, limit=10):
        rows = self.connection.execute(
            "SELECT * FROM applications WHERE scene_id = ?"
            " ORDER BY applied_at DESC, id DESC LIMIT ?",
            (int(scene_id), int(limit))).fetchall()
        out = []
        for row in rows:
            record = dict(row)
            record["fields"] = _loads(record.pop("fields_json", None), [])
            record["created"] = _loads(record.pop("created_json", None), {})
            out.append(record)
        return out
