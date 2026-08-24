/*
 * ScrapeDiscovery UI.
 *
 * Plain JavaScript on purpose. Stash loads plugin scripts as classic <script> tags
 * (ui/v2.5/src/plugins.tsx -> useScript), so there is no module system to build for,
 * and this repository's CI is Python-only. Components are written with
 * PluginApi.React.createElement through the `h` alias below - the same thing JSX
 * compiles to.
 *
 * Two rules run through the whole file:
 *
 *  - everything displayed comes from a scraper and is therefore untrusted. It is
 *    passed as a React child, never as innerHTML, so React escapes it; a discovered
 *    URL only becomes an href after its scheme has been checked.
 *  - the plugin backend is reached with runPluginOperation, which spawns a Python
 *    process per call, so calls are deliberate: one per view, plus polling only while
 *    a scan is actually running.
 */
(function () {
  "use strict";

  var api = window.PluginApi;
  if (!api) {
    // Stash injects PluginApi before plugin scripts; if it is missing, the version is
    // too old for a plugin page and there is nothing useful to do quietly.
    console.error("[ScrapeDiscovery] PluginApi is not available; UI not loaded");
    return;
  }

  var React = api.React;
  var h = React.createElement;
  var Router = api.libraries.ReactRouterDOM;
  var Bootstrap = api.libraries.Bootstrap;
  var Nav = Bootstrap.Nav;
  var Tab = Bootstrap.Tab;

  var PLUGIN_ID = "ScrapeDiscovery";
  var BASE = "/scrape-discovery";

  var TABS = [
    { key: "unresolved", label: "Unresolved" },
    { key: "candidates", label: "Candidates" },
    { key: "results", label: "Results" },
    { key: "resolved", label: "Resolved" },
    { key: "failed", label: "Failed" },
    { key: "all", label: "All" }
  ];

  var SORTS = [
    { key: "last_scanned_at", label: "Last scanned" },
    { key: "confidence", label: "Best confidence" },
    { key: "candidates", label: "Candidates" },
    { key: "attempts", label: "Attempts" },
    { key: "errors", label: "Errors" },
    { key: "title", label: "Title" },
    { key: "studio", label: "Studio" },
    { key: "scene_id", label: "Scene id" }
  ];

  /* ------------------------------------------------------------------ backend */

  // One GraphQL round trip per call. `runPluginOperation` runs the plugin process and
  // returns whatever it printed, so `output` is our own JSON.
  function callOp(op, args) {
    var body = {
      query:
        "mutation SDOp($id: ID!, $args: Map) {" +
        " runPluginOperation(plugin_id: $id, args: $args) }",
      variables: { id: PLUGIN_ID, args: Object.assign({ op: op }, args || {}) }
    };
    return fetch("/graphql", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    })
      .then(function (response) {
        if (!response.ok) {
          throw new Error("Stash returned HTTP " + response.status);
        }
        return response.json();
      })
      .then(function (payload) {
        if (payload.errors && payload.errors.length) {
          throw new Error(payload.errors[0].message);
        }
        var result = payload.data && payload.data.runPluginOperation;
        if (!result) {
          throw new Error("the plugin returned nothing - is it enabled?");
        }
        if (result.ok === false) {
          throw new Error(result.error || "the operation failed");
        }
        return result;
      });
  }

  // A hook rather than a helper so a view can re-run it and show its own errors.
  function useOp(op, args, deps) {
    var state = React.useState({ loading: true, data: null, error: null });
    var value = state[0];
    var setValue = state[1];
    var nonce = React.useState(0);
    var reload = React.useCallback(function () {
      nonce[1](function (n) {
        return n + 1;
      });
    }, []);
    var serialized = JSON.stringify(args || {});

    React.useEffect(
      function () {
        var live = true;
        setValue(function (previous) {
          return { loading: true, data: previous.data, error: null };
        });
        callOp(op, JSON.parse(serialized)).then(
          function (data) {
            if (live) setValue({ loading: false, data: data, error: null });
          },
          function (error) {
            if (live) setValue({ loading: false, data: null, error: error.message });
          }
        );
        return function () {
          live = false;
        };
      },
      // eslint-disable-next-line
      [op, serialized, nonce[0]].concat(deps || [])
    );

    return { loading: value.loading, data: value.data, error: value.error, reload: reload };
  }

  /* ------------------------------------------------------------- presentation */

  function cx() {
    var out = [];
    for (var i = 0; i < arguments.length; i++) {
      if (arguments[i]) out.push(arguments[i]);
    }
    return out.join(" ");
  }

  // Scraper output decides none of our markup, but it can decide an href, so the
  // scheme is checked before anything becomes a link.
  function safeHref(url) {
    var text = String(url || "");
    return /^https?:\/\//i.test(text) ? text : null;
  }

  function ExternalLink(props) {
    var href = safeHref(props.url);
    var label = props.label || props.url || "";
    if (!href) return h("span", { className: "sd-plain" }, label);
    return h(
      "a",
      { href: href, target: "_blank", rel: "noreferrer noopener", title: props.url },
      label
    );
  }

  function levelOf(confidence) {
    if (confidence === null || confidence === undefined) return "unknown";
    if (confidence >= 95) return "almost_certain";
    if (confidence >= 80) return "strong";
    if (confidence >= 60) return "possible";
    return "weak";
  }

  function Confidence(props) {
    var value = props.value;
    if (value === null || value === undefined) {
      return h("span", { className: "sd-conf sd-conf-none" }, "—");
    }
    return h(
      "span",
      { className: "sd-conf sd-conf-" + levelOf(value) },
      Math.round(value) + "%"
    );
  }

  var STATUS_LABEL = {
    UNSCANNED: "Not scanned",
    SCANNING: "Scanning",
    CANDIDATES: "Candidates",
    RESULTS: "Results",
    NO_RESULTS: "Nothing found",
    FAILED: "Failed",
    APPLIED: "Applied",
    DISMISSED: "Dismissed"
  };

  function StatusPill(props) {
    var status = props.status || "UNSCANNED";
    return h(
      "span",
      { className: "sd-pill sd-pill-" + status.toLowerCase() },
      STATUS_LABEL[status] || status
    );
  }

  function AttemptStatus(props) {
    var status = props.status || "";
    return h("span", { className: "sd-att sd-att-" + status.toLowerCase() }, status);
  }

  function when(value) {
    if (!value) return "—";
    var parsed = new Date(value);
    if (isNaN(parsed.getTime())) return String(value);
    return parsed.toLocaleString();
  }

  function millis(value) {
    if (value === null || value === undefined) return "—";
    if (value < 1000) return value + " ms";
    return (value / 1000).toFixed(1) + " s";
  }

  function Loading(props) {
    return h("div", { className: "sd-loading" }, props.what || "Loading…");
  }

  function Problem(props) {
    return h(
      "div",
      { className: "sd-error" },
      h("strong", null, "ScrapeDiscovery: "),
      props.message,
      props.onRetry
        ? h(
            "button",
            { className: "btn btn-sm btn-secondary sd-retry", onClick: props.onRetry },
            "Try again"
          )
        : null
    );
  }

  function Empty(props) {
    return h("div", { className: "sd-empty" }, props.children);
  }

  /* -------------------------------------------------------------- scan controls */

  // Starting a scan queues a Stash job; the button then watches that job rather than
  // holding the request open, because a scan is minutes of work.
  function useScanStarter(onFinished) {
    var state = React.useState({ busy: false, job: null, error: null, note: null });
    var value = state[0];
    var setValue = state[1];

    var start = React.useCallback(function (sceneIds, mode, extra) {
      setValue({ busy: true, job: null, error: null, note: null });
      callOp("scan.start", Object.assign({ scene_ids: sceneIds, mode: mode }, extra || {}))
        .then(
          function (data) {
            setValue({
              busy: true,
              job: data.job_id,
              error: null,
              note:
                (mode === "deep" ? "Deep scan" : "Scan") +
                " queued for " +
                sceneIds.length +
                (sceneIds.length === 1 ? " scene" : " scenes")
            });
          },
          function (error) {
            setValue({ busy: false, job: null, error: error.message, note: null });
          }
        );
    }, []);

    React.useEffect(
      function () {
        if (!value.job) return undefined;
        var timer = setInterval(function () {
          callOp("scan.status", { job_id: value.job }).then(
            function (data) {
              var job = data.job;
              // No job row means Stash has already forgotten it: finished either way.
              var done = !job || job.status === "FINISHED" || job.status === "CANCELLED" ||
                job.status === "FAILED";
              if (done) {
                clearInterval(timer);
                setValue(function (previous) {
                  return {
                    busy: false,
                    job: null,
                    error: job && job.error ? job.error : null,
                    note: job && job.status === "CANCELLED" ? "Scan cancelled" : "Scan finished"
                  };
                });
                if (onFinished) onFinished();
              }
            },
            function () {
              /* a failed poll is not a failed scan; the next tick tries again */
            }
          );
        }, 3000);
        return function () {
          clearInterval(timer);
        };
      },
      [value.job, onFinished]
    );

    var cancel = React.useCallback(
      function () {
        if (!value.job) return;
        callOp("scan.cancel", { job_id: value.job });
      },
      [value.job]
    );

    return { start: start, cancel: cancel, busy: value.busy, job: value.job,
             error: value.error, note: value.note };
  }

  function ScanButtons(props) {
    var scanner = props.scanner;
    var ids = props.sceneIds;
    return h(
      "div",
      { className: "sd-scan-buttons" },
      h(
        "button",
        {
          className: "btn btn-primary",
          disabled: scanner.busy || !ids.length,
          onClick: function () {
            scanner.start(ids, "normal");
          },
          title: "Try the scrapers that are likely to know this scene"
        },
        "Discover"
      ),
      h(
        "button",
        {
          className: "btn btn-secondary",
          disabled: scanner.busy || !ids.length,
          onClick: function () {
            scanner.start(ids, "deep");
          },
          title: "Try every enabled scraper, including name searches. Slow."
        },
        "Deep scan"
      ),
      scanner.job
        ? h(
            "button",
            { className: "btn btn-danger", onClick: scanner.cancel },
            "Cancel"
          )
        : null,
      scanner.busy && !scanner.job
        ? h("span", { className: "sd-note" }, "Queueing…")
        : null,
      scanner.note ? h("span", { className: "sd-note" }, scanner.note) : null,
      scanner.error ? h("span", { className: "sd-error-inline" }, scanner.error) : null
    );
  }

  /* -------------------------------------------------------------------- inbox */

  function InboxRow(props) {
    var row = props.row;
    return h(
      "tr",
      null,
      h(
        "td",
        { className: "sd-col-scene" },
        h(
          Router.Link,
          { to: BASE + "/scene/" + row.scene_id },
          row.title || "scene " + row.scene_id
        ),
        h("div", { className: "sd-sub" }, row.path || "")
      ),
      h("td", null, h(StatusPill, { status: row.status })),
      h("td", { className: "sd-num" }, row.candidate_count || 0),
      h("td", { className: "sd-num" }, h(Confidence, { value: row.best_confidence })),
      h("td", { className: "sd-num" }, row.attempt_count || 0),
      h(
        "td",
        { className: cx("sd-num", row.error_count ? "sd-has-errors" : null) },
        row.error_count || 0
      ),
      h("td", { className: "sd-num" }, row.url_count || 0),
      h("td", null, row.studio_name || "—"),
      h("td", null, when(row.last_scanned_at)),
      h(
        "td",
        null,
        h(
          "a",
          { href: "/scenes/" + row.scene_id, className: "sd-sub" },
          "open scene"
        )
      )
    );
  }

  function Inbox() {
    var tabState = React.useState("candidates");
    var tab = tabState[0];
    var setTab = tabState[1];
    var queryState = React.useState("");
    var query = queryState[0];
    var setQuery = queryState[1];
    var searchState = React.useState("");
    var search = searchState[0];
    var setSearch = searchState[1];
    var sortState = React.useState("last_scanned_at");
    var sort = sortState[0];
    var setSort = sortState[1];
    var dirState = React.useState("desc");
    var direction = dirState[0];
    var setDirection = dirState[1];
    var pageState = React.useState(1);
    var page = pageState[0];
    var setPage = pageState[1];
    var minState = React.useState("");
    var minConfidence = minState[0];
    var setMinConfidence = minState[1];

    var request = {
      tab: tab,
      q: search,
      sort: sort,
      direction: direction,
      page: page,
      perPage: 50
    };
    if (minConfidence !== "") request.minConfidence = Number(minConfidence);

    var inbox = useOp("inbox.list", request);
    var scanner = useScanStarter(inbox.reload);

    // Only poll while something is actually running - each poll is a process spawn.
    var running = (inbox.data && inbox.data.running) || [];
    React.useEffect(
      function () {
        if (!running.length) return undefined;
        var timer = setInterval(inbox.reload, 4000);
        return function () {
          clearInterval(timer);
        };
      },
      [running.length, inbox.reload]
    );

    function submitSearch(event) {
      event.preventDefault();
      setSearch(query);
      setPage(1);
    }

    var data = inbox.data || {};
    var items = data.items || [];
    var total = data.total || 0;
    var pages = Math.max(1, Math.ceil(total / (data.per_page || 50)));

    return h(
      "div",
      { className: "sd-page" },
      h(
        "div",
        { className: "sd-header" },
        h("h1", null, "ScrapeDiscovery"),
        h(
          "div",
          { className: "sd-header-actions" },
          h(
            Router.Link,
            { className: "btn btn-secondary", to: BASE + "/settings" },
            "Settings"
          )
        )
      ),

      running.length
        ? h(
            "div",
            { className: "sd-running" },
            running.map(function (scan) {
              var progress = scan.progress || {};
              return h(
                "div",
                { key: scan.id, className: "sd-running-row" },
                h(
                  Router.Link,
                  { to: BASE + "/scene/" + scan.scene_id },
                  "scene " + scan.scene_id
                ),
                h(
                  "span",
                  { className: "sd-note" },
                  " " +
                    (scan.mode || "normal") +
                    " scan: " +
                    (progress.done || 0) +
                    (progress.cached ? "+" + progress.cached + " cached" : "") +
                    "/" +
                    (progress.planned || "?") +
                    " attempts, " +
                    (progress.matches || 0) +
                    " match, " +
                    (progress.errors || 0) +
                    " error, " +
                    (progress.urls || 0) +
                    " url"
                )
              );
            })
          )
        : null,

      h(
        "ul",
        { className: "nav nav-tabs sd-tabs" },
        TABS.map(function (entry) {
          var count = data.tabs ? data.tabs[entry.key] : null;
          return h(
            "li",
            { key: entry.key, className: "nav-item" },
            h(
              "button",
              {
                className: cx("nav-link", tab === entry.key ? "active" : null),
                onClick: function () {
                  setTab(entry.key);
                  setPage(1);
                }
              },
              entry.label,
              count !== null && count !== undefined
                ? h("span", { className: "sd-count" }, count)
                : null
            )
          );
        })
      ),

      h(
        "form",
        { className: "sd-filters", onSubmit: submitSearch },
        h("input", {
          className: "form-control sd-search",
          placeholder: "Search title, path or scene id",
          value: query,
          onChange: function (event) {
            setQuery(event.target.value);
          }
        }),
        h("input", {
          className: "form-control sd-minconf",
          type: "number",
          min: 0,
          max: 100,
          placeholder: "min %",
          value: minConfidence,
          onChange: function (event) {
            setMinConfidence(event.target.value);
            setPage(1);
          }
        }),
        h(
          "select",
          {
            className: "form-control sd-sort",
            value: sort,
            onChange: function (event) {
              setSort(event.target.value);
            }
          },
          SORTS.map(function (entry) {
            return h("option", { key: entry.key, value: entry.key }, entry.label);
          })
        ),
        h(
          "button",
          {
            type: "button",
            className: "btn btn-secondary",
            onClick: function () {
              setDirection(direction === "desc" ? "asc" : "desc");
            }
          },
          direction === "desc" ? "↓" : "↑"
        ),
        h("button", { type: "submit", className: "btn btn-secondary" }, "Filter")
      ),

      inbox.error ? h(Problem, { message: inbox.error, onRetry: inbox.reload }) : null,
      inbox.loading && !items.length ? h(Loading, null) : null,

      !inbox.loading && !items.length && !inbox.error
        ? h(
            Empty,
            null,
            tab === "candidates"
              ? "No candidates yet. Tag the scenes you cannot identify with your input tag and run the “Discover tagged scenes” task, or press Discover on a scene."
              : "Nothing here yet."
          )
        : null,

      items.length
        ? h(
            "div",
            { className: "sd-table-wrap" },
            h(
              "table",
              { className: "table sd-table" },
              h(
                "thead",
                null,
                h(
                  "tr",
                  null,
                  h("th", null, "Scene"),
                  h("th", null, "Status"),
                  h("th", { className: "sd-num" }, "Cand."),
                  h("th", { className: "sd-num" }, "Best"),
                  h("th", { className: "sd-num" }, "Att."),
                  h("th", { className: "sd-num" }, "Err."),
                  h("th", { className: "sd-num" }, "URLs"),
                  h("th", null, "Studio"),
                  h("th", null, "Last scanned"),
                  h("th", null, "")
                )
              ),
              h(
                "tbody",
                null,
                items.map(function (row) {
                  return h(InboxRow, { key: row.scene_id, row: row });
                })
              )
            )
          )
        : null,

      total
        ? h(
            "div",
            { className: "sd-pager" },
            h(
              "button",
              {
                className: "btn btn-secondary",
                disabled: page <= 1,
                onClick: function () {
                  setPage(page - 1);
                }
              },
              "Previous"
            ),
            h("span", { className: "sd-note" }, "page " + page + " of " + pages +
              " (" + total + " scenes)"),
            h(
              "button",
              {
                className: "btn btn-secondary",
                disabled: page >= pages,
                onClick: function () {
                  setPage(page + 1);
                }
              },
              "Next"
            )
          )
        : null,

      items.length
        ? h(
            "div",
            { className: "sd-bulk" },
            h("span", { className: "sd-note" }, "Rescan everything on this page:"),
            h(ScanButtons, {
              scanner: scanner,
              sceneIds: items.map(function (row) {
                return String(row.scene_id);
              })
            })
          )
        : null
    );
  }

  /* ------------------------------------------------------- scene discovery view */

  function ResultCard(props) {
    var result = props.result;
    var normalized = result.normalized || {};
    var imageState = React.useState(null);
    var image = imageState[0];
    var setImage = imageState[1];

    // Covers are fetched one at a time, on request: a scraper's cover is a couple of
    // hundred kilobytes of base64 and a scene can have thirty results.
    function loadImage() {
      if (!result.image_sha256 || image) return;
      callOp("image.get", { sha256: result.image_sha256 }).then(
        function (data) {
          setImage(data.data_uri);
        },
        function () {
          setImage(null);
        }
      );
    }

    var performers = normalized.performers || [];
    var tags = normalized.tags || [];
    var urls = normalized.urls || [];

    return h(
      "div",
      { className: "sd-result" },
      h(
        "div",
        { className: "sd-result-head" },
        h(
          "span",
          { className: "sd-source" },
          result.scraper_name || "auto-routed",
          result.attribution === "AMBIGUOUS"
            ? h(
                "span",
                {
                  className: "sd-warn",
                  title:
                    "Stash chose which scraper handled this URL and does not report " +
                    "which one, so the source cannot be attributed with certainty."
                },
                " (source uncertain)"
              )
            : null
        ),
        h("span", { className: "sd-method" }, result.method),
        result.depth ? h("span", { className: "sd-depth" }, "depth " + result.depth) : null,
        h("span", { className: "sd-note" }, when(result.started_at))
      ),
      h(
        "div",
        { className: "sd-result-body" },
        h(
          "div",
          { className: "sd-result-image", onClick: loadImage },
          result.image_sha256
            ? image
              ? h("img", { src: image, alt: "" })
              : h("button", { className: "btn btn-sm btn-secondary" }, "Show image")
            : h("span", { className: "sd-note" }, "no image")
        ),
        h(
          "div",
          { className: "sd-result-fields" },
          h("div", { className: "sd-title" }, normalized.title || "(no title)"),
          h(
            "dl",
            null,
            field("Date", normalized.date),
            field("Duration", normalized.duration ? normalized.duration + " s" : null),
            field("Studio", normalized.studio ? normalized.studio.name : null),
            field("Code", normalized.code),
            field("Director", normalized.director),
            performers.length
              ? field(
                  "Performers",
                  performers
                    .map(function (one) {
                      return one.name;
                    })
                    .join(", ")
                )
              : null,
            tags.length
              ? field(
                  "Tags",
                  tags
                    .slice(0, 12)
                    .map(function (one) {
                      return one.name;
                    })
                    .join(", ") + (tags.length > 12 ? " +" + (tags.length - 12) : "")
                )
              : null,
            urls.length
              ? h(
                  React.Fragment,
                  null,
                  h("dt", null, "URLs"),
                  h(
                    "dd",
                    null,
                    urls.map(function (one, index) {
                      return h(
                        "div",
                        { key: index },
                        h(ExternalLink, { url: one.url, label: one.url })
                      );
                    })
                  )
                )
              : null
          ),
          result.stale_normalization
            ? h(
                "div",
                { className: "sd-note" },
                "stored with an older normalisation; a rebuild will refresh it"
              )
            : null
        )
      )
    );
  }

  function field(label, value) {
    if (value === null || value === undefined || value === "") return null;
    return h(React.Fragment, null, h("dt", null, label), h("dd", null, String(value)));
  }

  function AttemptTable(props) {
    var showAllState = React.useState(false);
    var showAll = showAllState[0];
    var setShowAll = showAllState[1];
    var attempts = props.attempts || [];
    var interesting = attempts.filter(function (one) {
      return one.status !== "NO_MATCH" && one.status !== "SKIPPED";
    });
    var shown = showAll ? attempts : interesting;

    return h(
      "div",
      null,
      h(
        "div",
        { className: "sd-section-head" },
        h("h3", null, "Attempts"),
        h(
          "button",
          {
            className: "btn btn-sm btn-secondary",
            onClick: function () {
              setShowAll(!showAll);
            }
          },
          showAll
            ? "Hide no-match attempts"
            : "Show all " + attempts.length + " attempts"
        )
      ),
      h(
        "div",
        { className: "sd-note" },
        interesting.length +
          " of " +
          attempts.length +
          " attempts returned something or failed. No-match attempts are kept - that " +
          "is what stops the next scan from asking again."
      ),
      h(
        "div",
        { className: "sd-table-wrap" },
        h(
          "table",
          { className: "table sd-table sd-attempts" },
          h(
            "thead",
            null,
            h(
              "tr",
              null,
              h("th", null, "Source"),
              h("th", null, "Method"),
              h("th", null, "Status"),
              h("th", { className: "sd-num" }, "Took"),
              h("th", { className: "sd-num" }, "Results"),
              h("th", null, "Target / error")
            )
          ),
          h(
            "tbody",
            null,
            shown.map(function (attempt) {
              return h(
                "tr",
                { key: attempt.id },
                h(
                  "td",
                  null,
                  attempt.scraper_name || "auto-routed",
                  attempt.from_cache
                    ? h("span", { className: "sd-cached", title: "answered from cache" }, " cached")
                    : null
                ),
                h("td", null, attempt.method),
                h("td", null, h(AttemptStatus, { status: attempt.status })),
                h("td", { className: "sd-num" }, millis(attempt.duration_ms)),
                h("td", { className: "sd-num" }, attempt.result_count || 0),
                h(
                  "td",
                  { className: "sd-target" },
                  attempt.error
                    ? h("span", { className: "sd-error-inline" }, attempt.error)
                    : attempt.target
                    ? h(ExternalLink, { url: attempt.target, label: attempt.target })
                    : "—"
                )
              );
            })
          )
        )
      )
    );
  }

  function UrlList(props) {
    var urls = props.urls || [];
    if (!urls.length) return null;
    return h(
      "div",
      null,
      h("h3", null, "Discovered URLs"),
      h(
        "table",
        { className: "table sd-table" },
        h(
          "thead",
          null,
          h(
            "tr",
            null,
            h("th", { className: "sd-num" }, "Depth"),
            h("th", null, "URL"),
            h("th", null, "State"),
            h("th", null, "Handled by")
          )
        ),
        h(
          "tbody",
          null,
          urls.map(function (url) {
            return h(
              "tr",
              { key: url.id },
              h("td", { className: "sd-num" }, url.depth),
              h("td", null, h(ExternalLink, { url: url.url, label: url.url })),
              h("td", null, h("span", { className: "sd-att" }, url.state)),
              h(
                "td",
                null,
                (url.handlers || []).length
                  ? (url.handlers || []).join(", ")
                  : h("span", { className: "sd-note" }, "no installed scraper matches")
              )
            );
          })
        )
      )
    );
  }

  function SceneSummary(props) {
    var scene = props.scene;
    if (!scene) return null;
    return h(
      "div",
      { className: "sd-scene-summary" },
      h(
        "dl",
        null,
        field("Title", scene.title),
        field("From filename", scene.title ? null : scene.filename_title),
        field("Searched for", scene.search_term),
        field("Date", scene.date),
        field("Duration", scene.duration ? scene.duration + " s" : null),
        field("Studio", scene.studio ? scene.studio.name : null),
        field(
          "Performers",
          (scene.performers || [])
            .map(function (one) {
              return one.name;
            })
            .join(", ")
        ),
        field("File", scene.filename),
        field("Organized", scene.organized ? "yes" : "no")
      ),
      (scene.urls || []).length
        ? h(
            "div",
            null,
            h("strong", null, "URLs on the scene: "),
            (scene.urls || []).map(function (one, index) {
              return h(
                "span",
                { key: index, className: "sd-url-chip" },
                h(ExternalLink, { url: one.url, label: one.host || one.url })
              );
            })
          )
        : null
    );
  }

  function SceneDiscovery() {
    var params = Router.useParams();
    var sceneId = params.id;
    var detail = useOp("scene.detail", { scene_id: Number(sceneId) });
    var scanner = useScanStarter(detail.reload);

    var data = detail.data || {};
    var state = data.state || {};
    var scanning = state.status === "SCANNING";

    React.useEffect(
      function () {
        if (!scanning) return undefined;
        var timer = setInterval(detail.reload, 4000);
        return function () {
          clearInterval(timer);
        };
      },
      [scanning, detail.reload]
    );

    if (detail.loading && !detail.data) return h(Loading, { what: "Loading discovery…" });
    if (detail.error) return h(Problem, { message: detail.error, onRetry: detail.reload });

    var candidates = data.candidates || [];
    var results = data.results || [];
    var scans = data.scans || [];

    return h(
      "div",
      { className: "sd-page" },
      h(
        "div",
        { className: "sd-header" },
        h(
          "h1",
          null,
          h(Router.Link, { to: BASE }, "ScrapeDiscovery"),
          " / ",
          (data.scene && data.scene.display_title) || "scene " + sceneId
        ),
        h(
          "div",
          { className: "sd-header-actions" },
          h(StatusPill, { status: state.status || "UNSCANNED" }),
          h("a", { className: "btn btn-secondary", href: "/scenes/" + sceneId }, "Open scene")
        )
      ),

      h(ScanButtons, { scanner: scanner, sceneIds: [String(sceneId)] }),

      h(
        "div",
        { className: "sd-stats" },
        stat("Candidates", state.candidate_count || 0),
        stat("Best confidence", state.best_confidence === null ||
          state.best_confidence === undefined
          ? "—"
          : Math.round(state.best_confidence) + "%"),
        stat("Stored results", results.length),
        stat("Attempts", state.attempt_count || 0),
        stat("Errors", state.error_count || 0),
        stat("URLs", state.url_count || 0),
        stat("Last scanned", when(state.last_scanned_at))
      ),

      h("h3", null, "Scene as Stash has it"),
      h(SceneSummary, { scene: data.scene }),

      candidates.length
        ? h(
            "div",
            null,
            h("h3", null, "Candidates"),
            candidates.map(function (candidate) {
              return h(
                "div",
                { key: candidate.id, className: "sd-candidate" },
                h(Confidence, { value: candidate.confidence }),
                " ",
                (candidate.merged && candidate.merged.title) || candidate.identity_key,
                h(
                  "span",
                  { className: "sd-note" },
                  " " + candidate.source_count + " source(s), " +
                    candidate.independent_source_count + " independent"
                )
              );
            })
          )
        : h(
            Empty,
            null,
            results.length
              ? "Scrapers answered, but candidate correlation and scoring are not in this version yet - the raw answers are below, grouped by source."
              : "Nothing found yet. Press Discover, or Deep scan to try every enabled scraper including name searches."
          ),

      results.length
        ? h(
            "div",
            null,
            h("h3", null, "What the scrapers returned"),
            h(
              "div",
              { className: "sd-note" },
              results.length + " stored result(s). Nothing here has been written to the scene."
            ),
            results.map(function (result) {
              return h(ResultCard, { key: result.id, result: result });
            })
          )
        : null,

      h(AttemptTable, { attempts: data.attempts || [] }),
      h(UrlList, { urls: data.urls || [] }),

      scans.length
        ? h(
            "div",
            null,
            h("h3", null, "Scan history"),
            h(
              "table",
              { className: "table sd-table" },
              h(
                "thead",
                null,
                h(
                  "tr",
                  null,
                  h("th", null, "Started"),
                  h("th", null, "Trigger"),
                  h("th", null, "Mode"),
                  h("th", null, "Status"),
                  h("th", { className: "sd-num" }, "Attempts"),
                  h("th", { className: "sd-num" }, "Matches"),
                  h("th", { className: "sd-num" }, "Errors"),
                  h("th", null, "Stopped because")
                )
              ),
              h(
                "tbody",
                null,
                scans.map(function (scan) {
                  return h(
                    "tr",
                    { key: scan.id },
                    h("td", null, when(scan.started_at)),
                    h("td", null, scan.trigger),
                    h("td", null, scan.mode),
                    h("td", null, h(AttemptStatus, { status: scan.status })),
                    h("td", { className: "sd-num" }, scan.attempt_count),
                    h("td", { className: "sd-num" }, scan.match_count),
                    h("td", { className: "sd-num" }, scan.error_count),
                    h("td", null, scan.stop_reason || scan.error || "—")
                  );
                })
              )
            )
          )
        : null
    );
  }

  function stat(label, value) {
    return h(
      "div",
      { className: "sd-stat", key: label },
      h("div", { className: "sd-stat-value" }, value),
      h("div", { className: "sd-stat-label" }, label)
    );
  }

  /* ----------------------------------------------------------------- settings */

  function SettingsPage() {
    var loaded = useOp("settings.get", {});
    var draftState = React.useState({});
    var draft = draftState[0];
    var setDraft = draftState[1];
    var savingState = React.useState({ busy: false, message: null, error: null });
    var saving = savingState[0];
    var setSaving = savingState[1];

    if (loaded.loading && !loaded.data) return h(Loading, { what: "Loading settings…" });
    if (loaded.error) return h(Problem, { message: loaded.error, onRetry: loaded.reload });

    var spec = (loaded.data && loaded.data.settings) || [];

    function valueOf(entry) {
      if (Object.prototype.hasOwnProperty.call(draft, entry.key)) return draft[entry.key];
      if (entry.type === "JSON") {
        return JSON.stringify(entry.value === null ? {} : entry.value, null, 2);
      }
      return entry.value === null || entry.value === undefined ? "" : entry.value;
    }

    function change(entry, value) {
      setDraft(
        Object.assign({}, draft, (function () {
          var patch = {};
          patch[entry.key] = value;
          return patch;
        })())
      );
    }

    function save() {
      var values = {};
      var bad = null;
      Object.keys(draft).forEach(function (key) {
        var entry = spec.filter(function (one) {
          return one.key === key;
        })[0];
        if (!entry) return;
        if (entry.type === "JSON") {
          try {
            values[key] = JSON.parse(draft[key] || "{}");
          } catch (error) {
            bad = key + " is not valid JSON: " + error.message;
          }
        } else {
          values[key] = draft[key];
        }
      });
      if (bad) {
        setSaving({ busy: false, message: null, error: bad });
        return;
      }
      setSaving({ busy: true, message: null, error: null });
      callOp("settings.set", { values: values }).then(
        function (data) {
          setDraft({});
          setSaving({
            busy: false,
            error: (data.problems || []).join("; ") || null,
            message: data.saved && data.saved.length
              ? "Saved " + data.saved.length + " setting(s)"
              : "Nothing to save"
          });
          loaded.reload();
        },
        function (error) {
          setSaving({ busy: false, message: null, error: error.message });
        }
      );
    }

    return h(
      "div",
      { className: "sd-page" },
      h(
        "div",
        { className: "sd-header" },
        h(
          "h1",
          null,
          h(Router.Link, { to: BASE }, "ScrapeDiscovery"),
          " / Settings"
        ),
        h(
          "div",
          { className: "sd-header-actions" },
          h(
            "button",
            { className: "btn btn-primary", disabled: saving.busy || !Object.keys(draft).length,
              onClick: save },
            saving.busy ? "Saving…" : "Save"
          )
        )
      ),
      h(
        "div",
        { className: "sd-note" },
        "These are the same settings Stash shows under Settings → Plugins; both write " +
          "to the same store, so neither can go stale."
      ),
      saving.message ? h("div", { className: "sd-ok" }, saving.message) : null,
      saving.error ? h(Problem, { message: saving.error }) : null,
      (loaded.data.problems || []).length
        ? h(
            "div",
            { className: "sd-error" },
            (loaded.data.problems || []).map(function (problem, index) {
              return h("div", { key: index }, problem);
            })
          )
        : null,
      h(
        "div",
        { className: "sd-settings" },
        spec.map(function (entry) {
          var value = valueOf(entry);
          var input;
          if (entry.type === "BOOLEAN") {
            input = h("input", {
              type: "checkbox",
              checked: !!value,
              onChange: function (event) {
                change(entry, event.target.checked);
              }
            });
          } else if (entry.type === "NUMBER") {
            input = h("input", {
              className: "form-control",
              type: "number",
              value: value,
              onChange: function (event) {
                change(entry, event.target.value);
              }
            });
          } else if (entry.type === "JSON") {
            input = h("textarea", {
              className: "form-control sd-json",
              rows: 6,
              value: value,
              onChange: function (event) {
                change(entry, event.target.value);
              }
            });
          } else {
            input = h("input", {
              className: "form-control",
              type: "text",
              value: value,
              onChange: function (event) {
                change(entry, event.target.value);
              }
            });
          }
          return h(
            "div",
            { key: entry.key, className: "sd-setting" },
            h(
              "label",
              null,
              h("span", { className: "sd-setting-key" }, entry.key),
              input
            ),
            h("div", { className: "sd-note" }, entry.description)
          );
        })
      ),
      h(Diagnostics, null)
    );
  }

  function Diagnostics() {
    var info = useOp("diagnostics.info", { limit: 40 });
    if (info.loading && !info.data) return h(Loading, { what: "Loading diagnostics…" });
    if (info.error) return h(Problem, { message: info.error, onRetry: info.reload });
    var data = info.data || {};
    var counts = data.counts || {};
    var stats = data.scraper_stats || [];

    return h(
      "div",
      { className: "sd-diagnostics" },
      h("h3", null, "Diagnostics"),
      h(
        "div",
        { className: "sd-stats" },
        stat("Database", ((data.database_bytes || 0) / 1048576).toFixed(1) + " MB"),
        stat("Scans", counts.scans || 0),
        stat("Attempts", counts.attempts || 0),
        stat("Results", counts.results || 0),
        stat("Images", counts.blobs || 0),
        stat("Scrapers seen", counts.scrapers || 0),
        stat("Schema", "v" + (data.schema_version || "?"))
      ),
      h("div", { className: "sd-note" }, data.database || ""),
      stats.length
        ? h(
            "div",
            { className: "sd-table-wrap" },
            h(
              "table",
              { className: "table sd-table" },
              h(
                "thead",
                null,
                h(
                  "tr",
                  null,
                  h("th", null, "Scraper"),
                  h("th", { className: "sd-num" }, "Attempts"),
                  h("th", { className: "sd-num" }, "Matches"),
                  h("th", { className: "sd-num" }, "No match"),
                  h("th", { className: "sd-num" }, "Errors"),
                  h("th", { className: "sd-num" }, "Timeouts"),
                  h("th", { className: "sd-num" }, "Avg")
                )
              ),
              h(
                "tbody",
                null,
                stats.map(function (row) {
                  return h(
                    "tr",
                    { key: row.scraper_id },
                    h("td", null, row.scraper_name || row.scraper_id),
                    h("td", { className: "sd-num" }, row.attempts),
                    h("td", { className: "sd-num" }, row.matches),
                    h("td", { className: "sd-num" }, row.no_matches),
                    h("td", { className: "sd-num" }, row.errors),
                    h("td", { className: "sd-num" }, row.timeouts),
                    h("td", { className: "sd-num" }, millis(Math.round(row.avg_ms || 0)))
                  );
                })
              )
            )
          )
        : null
    );
  }

  /* ------------------------------------------------------ scene page integration */

  function SceneDiscoveryTabPanel(props) {
    var sceneId = props.sceneId;
    var summary = useOp("scene.summary", { scene_id: Number(sceneId) });
    var scanner = useScanStarter(summary.reload);
    var data = summary.data || {};

    React.useEffect(
      function () {
        if (!data.scanning) return undefined;
        var timer = setInterval(summary.reload, 4000);
        return function () {
          clearInterval(timer);
        };
      },
      [data.scanning, summary.reload]
    );

    return h(
      "div",
      { className: "sd-scene-tab" },
      h(ScanButtons, { scanner: scanner, sceneIds: [String(sceneId)] }),
      summary.error ? h(Problem, { message: summary.error, onRetry: summary.reload }) : null,
      h(
        "div",
        { className: "sd-stats" },
        stat("Status", STATUS_LABEL[data.status] || data.status || "—"),
        stat("Candidates", data.candidate_count || 0),
        stat("Best", data.best_confidence === null || data.best_confidence === undefined
          ? "—"
          : Math.round(data.best_confidence) + "%"),
        stat("Results", data.result_count || 0),
        stat("Attempts", data.attempt_count || 0),
        stat("Errors", data.error_count || 0)
      ),
      (data.top_candidates || []).length
        ? h(
            "ul",
            { className: "sd-top" },
            (data.top_candidates || []).map(function (candidate) {
              return h(
                "li",
                { key: candidate.id },
                h(Confidence, { value: candidate.confidence }),
                " ",
                candidate.title || "(untitled)"
              );
            })
          )
        : null,
      h(
        "p",
        null,
        h(
          Router.Link,
          { className: "btn btn-secondary", to: BASE + "/scene/" + sceneId },
          "Open the full discovery view"
        )
      ),
      h(
        "p",
        { className: "sd-note" },
        "Discovery never writes to this scene. Whatever the scrapers return is stored " +
          "in ScrapeDiscovery's own database until you apply it."
      )
    );
  }

  function DiscoveryBadge(props) {
    var summary = useOp("scene.summary", { scene_id: Number(props.sceneId) });
    var data = summary.data;
    if (!data || (!data.candidate_count && !data.result_count)) return null;
    if (data.candidate_count) {
      return h(
        "span",
        { className: "sd-tab-badge" },
        data.candidate_count +
          (data.best_confidence ? " / " + Math.round(data.best_confidence) + "%" : "")
      );
    }
    return h("span", { className: "sd-tab-badge sd-tab-badge-muted" }, data.result_count);
  }

  /* -------------------------------------------------------------- registration */

  api.register.route(BASE, Inbox);
  api.register.route(BASE + "/settings", SettingsPage);
  api.register.route(BASE + "/scene/:id", SceneDiscovery);

  // The main menu. `before` on a container component replaces its children, so the
  // existing ones are passed through and ours appended - the same shape pluginApi's
  // own registerRoute uses.
  api.patch.before("MainNavBar.MenuItems", function (props) {
    return [
      Object.assign({}, props, {
        children: h(
          React.Fragment,
          null,
          props.children,
          h(
            Router.NavLink,
            { className: "nav-utility minimal btn btn-primary", exact: true, to: BASE,
              title: "ScrapeDiscovery" },
            h("span", null, "Discovery")
          )
        )
      })
    ];
  });

  // A tab on the scene page rather than more buttons in its header: the scene page is
  // already busy, and discovery has more to show than a button's worth.
  api.patch.before("ScenePage.Tabs", function (props) {
    var sceneId = props.scene && props.scene.id;
    if (!sceneId) return [props];
    return [
      Object.assign({}, props, {
        children: h(
          React.Fragment,
          null,
          props.children,
          h(
            Nav.Item,
            null,
            h(
              Nav.Link,
              { eventKey: "scrapediscovery-panel" },
              "Discovery",
              h(DiscoveryBadge, { sceneId: sceneId })
            )
          )
        )
      })
    ];
  });

  api.patch.before("ScenePage.TabContent", function (props) {
    var sceneId = props.scene && props.scene.id;
    if (!sceneId) return [props];
    return [
      Object.assign({}, props, {
        children: h(
          React.Fragment,
          null,
          props.children,
          h(
            Tab.Pane,
            { eventKey: "scrapediscovery-panel" },
            h(SceneDiscoveryTabPanel, { sceneId: sceneId })
          )
        )
      })
    ];
  });

  console.log("[ScrapeDiscovery] UI loaded");
})();
