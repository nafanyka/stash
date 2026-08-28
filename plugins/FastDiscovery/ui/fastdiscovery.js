/*
 * FastDiscovery UI.
 *
 * Plain JavaScript on purpose. Stash loads plugin scripts as classic <script> tags
 * (ui/v2.5/src/plugins.tsx -> useScript), so there is no module system to build for,
 * and this repository's CI is Python-only. Components are written with
 * PluginApi.React.createElement through the `h` alias below - the same thing JSX
 * compiles to.
 *
 * Three rules run through the whole file:
 *
 *  - everything displayed came from a scraper and is therefore untrusted. It is passed
 *    as a React child, never as innerHTML, so React escapes it; a discovered URL only
 *    becomes an href after its scheme has been checked.
 *  - nothing here writes to a scene. Apply is the single call that does, and it is a
 *    button the user presses on a selection they can see in full.
 *  - the backend is reached with runPluginOperation, which spawns a Python process per
 *    call, so calls are deliberate: one per view, plus polling only while a run is
 *    actually going.
 */
(function () {
  "use strict";

  var api = window.PluginApi;
  if (!api) {
    console.error("[FastDiscovery] PluginApi is not available; UI not loaded");
    return;
  }

  var React = api.React;
  var h = React.createElement;
  var Router = api.libraries.ReactRouterDOM;
  var Bootstrap = api.libraries.Bootstrap;
  var Nav = Bootstrap.Nav;
  var Tab = Bootstrap.Tab;
  var Modal = Bootstrap.Modal;

  var PLUGIN_ID = "FastDiscovery";
  var BASE = "/fast-discovery";

  var TABS = [
    { key: "ready", label: "Ready for review" },
    { key: "running", label: "Running" },
    { key: "empty", label: "No results" },
    { key: "failed", label: "Failed" },
    { key: "done", label: "Decided" },
    { key: "all", label: "All" }
  ];

  var STATUS_LABEL = {
    RUNNING: "Running",
    READY_FOR_REVIEW: "Ready",
    READY_WITH_ERRORS: "Ready, with errors",
    NO_RESULTS: "No results",
    APPLIED: "Applied",
    REJECTED: "Rejected",
    FAILED: "Failed",
    FAILED_APPLY: "Apply failed",
    CANCELLED: "Cancelled"
  };

  // How many runs a page shows. Kept in localStorage rather than in the plugin's
  // settings: it is a property of this browser window, not of the install, and going
  // through the backend for it would cost a Python process per change.
  var PER_PAGE_KEY = "fastdiscovery.perPage";
  var PER_PAGE_CHOICES = [10, 20, 50, 100];
  var PER_PAGE_DEFAULT = 10;

  // A private window, or a browser set to block site data, makes localStorage throw on
  // access rather than return nothing, so every use of it is guarded.
  function loadPerPage() {
    try {
      var stored = Number(window.localStorage.getItem(PER_PAGE_KEY));
      return PER_PAGE_CHOICES.indexOf(stored) >= 0 ? stored : PER_PAGE_DEFAULT;
    } catch (error) {
      return PER_PAGE_DEFAULT;
    }
  }

  function savePerPage(value) {
    try {
      window.localStorage.setItem(PER_PAGE_KEY, String(value));
    } catch (error) {
      /* nothing to do: the choice simply will not be remembered */
    }
  }

  // Applying or rejecting changes what the runs page should be showing, and the two
  // can be on screen at once - the review lives on the scene page as well as on its
  // own route. A window event is the cheapest way for one to tell the other.
  var CHANGED_EVENT = "fastdiscovery:changed";

  function announceChange() {
    try {
      window.dispatchEvent(new CustomEvent(CHANGED_EVENT));
    } catch (error) {
      /* very old browser: the list will refresh on its next mount instead */
    }
  }

  function useToaster() {
    var toast = api.hooks && api.hooks.useToast ? api.hooks.useToast() : null;
    return {
      success: function (message) {
        if (toast && toast.success) toast.success(message);
        else console.log("[FastDiscovery] " + message);
      },
      failure: function (message) {
        if (toast && toast.error) toast.error(message);
        else console.error("[FastDiscovery] " + message);
      }
    };
  }

  var STATUS_CLASS = {
    RUNNING: "fd-pill-running",
    READY_FOR_REVIEW: "fd-pill-ready",
    READY_WITH_ERRORS: "fd-pill-warn",
    NO_RESULTS: "fd-pill-muted",
    APPLIED: "fd-pill-done",
    REJECTED: "fd-pill-muted",
    FAILED: "fd-pill-error",
    FAILED_APPLY: "fd-pill-error",
    CANCELLED: "fd-pill-muted"
  };

  /* ------------------------------------------------------------------ backend */

  function callOp(op, args) {
    var body = {
      query:
        "mutation FDOp($id: ID!, $args: Map) {" +
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
        if (!response.ok) throw new Error("Stash returned HTTP " + response.status);
        return response.json();
      })
      .then(function (payload) {
        if (payload.errors && payload.errors.length) {
          throw new Error(payload.errors[0].message);
        }
        var result = payload.data && payload.data.runPluginOperation;
        if (!result) {
          throw new Error("the FastDiscovery plugin returned nothing - is it enabled?");
        }
        if (result.ok === false && !result.needs_confirmation) {
          var error = new Error(result.error || "the operation failed");
          error.payload = result;
          throw error;
        }
        return result;
      });
  }

  // A hook rather than a helper so a view can re-run it and show its own errors.
  function useOp(op, args, options) {
    var state = React.useState({ loading: true, data: null, error: null });
    var value = state[0];
    var setValue = state[1];
    var nonce = React.useState(0);
    var skip = (options || {}).skip;
    var reload = React.useCallback(function () {
      nonce[1](function (n) { return n + 1; });
    }, []);
    var serialized = JSON.stringify(args || {});

    React.useEffect(
      function () {
        if (skip) {
          setValue({ loading: false, data: null, error: null });
          return undefined;
        }
        var live = true;
        setValue(function (previous) {
          return { loading: true, data: previous.data, error: null };
        });
        callOp(op, JSON.parse(serialized)).then(
          function (data) { if (live) setValue({ loading: false, data: data, error: null }); },
          function (error) {
            if (live) setValue({ loading: false, data: null, error: error.message });
          }
        );
        return function () { live = false; };
      },
      [op, serialized, nonce[0], skip]
    );

    return { loading: value.loading, data: value.data, error: value.error, reload: reload };
  }

  /* ------------------------------------------------------------------- helpers */

  function cx() {
    var out = [];
    for (var i = 0; i < arguments.length; i++) {
      if (arguments[i]) out.push(arguments[i]);
    }
    return out.join(" ");
  }

  function safeHref(url) {
    var text = String(url || "").trim();
    return /^https?:\/\//i.test(text) ? text : null;
  }

  function ExternalLink(props) {
    var href = safeHref(props.href);
    if (!href) return h("span", { className: "fd-muted" }, props.children || props.href);
    return h(
      "a",
      { href: href, target: "_blank", rel: "noopener noreferrer", title: props.href },
      props.children || props.href
    );
  }

  function shortUrl(url) {
    var text = String(url || "");
    return text.replace(/^https?:\/\//i, "").replace(/^www\./i, "");
  }

  function when(value) {
    if (!value) return "-";
    var parsed = new Date(value);
    return isNaN(parsed.getTime()) ? String(value) : parsed.toLocaleString();
  }

  function Loading(props) {
    return h("div", { className: "fd-loading" }, props.label || "Loading...");
  }

  function Problem(props) {
    if (!props.error) return null;
    return h(
      "div",
      { className: "fd-problem" },
      h("strong", null, "FastDiscovery: "),
      props.error,
      props.onRetry
        ? h("button", { className: "btn btn-sm btn-secondary fd-retry", onClick: props.onRetry }, "Retry")
        : null
    );
  }

  function StatusPill(props) {
    var status = props.status || "";
    return h(
      "span",
      { className: cx("fd-pill", STATUS_CLASS[status] || "fd-pill-muted") },
      STATUS_LABEL[status] || status
    );
  }

  /* --------------------------------------------------------------- run control */

  // Starting a run is one call that may come back asking for confirmation, because a
  // rescan throws away results that are still waiting for a decision (requirement 22).
  function useRunStarter(onStarted) {
    var busy = React.useState(false);
    var error = React.useState(null);
    var confirm = React.useState(null);

    function start(sceneIds, replace) {
      busy[1](true);
      error[1](null);
      return callOp("run.start", {
        scene_ids: sceneIds,
        replace: !!replace,
        trigger: "ui"
      }).then(
        function (data) {
          busy[1](false);
          if (data.needs_confirmation) {
            confirm[1]({ sceneIds: sceneIds, blocked: data.blocked || [], message: data.error });
            return null;
          }
          confirm[1](null);
          // A queued run is a row the list does not have yet.
          announceChange();
          if (onStarted) onStarted(data);
          return data;
        },
        function (failure) {
          busy[1](false);
          error[1](failure.message);
          return null;
        }
      );
    }

    return {
      busy: busy[0],
      error: error[0],
      confirming: confirm[0],
      start: start,
      cancelConfirm: function () { confirm[1](null); },
      confirmReplace: function () {
        var pending = confirm[0];
        confirm[1](null);
        if (pending) return start(pending.sceneIds, true);
        return Promise.resolve(null);
      }
    };
  }

  function ConfirmRescan(props) {
    if (!props.confirming) return null;
    return h(
      Modal,
      { show: true, onHide: props.onCancel, className: "fd-modal" },
      h(Modal.Header, null, h(Modal.Title, null, "Replace existing results?")),
      h(
        Modal.Body,
        null,
        h("p", null, props.confirming.message),
        h(
          "p",
          { className: "fd-muted" },
          "Existing FastDiscovery results will be replaced. Nothing on the scene itself changes."
        )
      ),
      h(
        Modal.Footer,
        null,
        h("button", { className: "btn btn-secondary", onClick: props.onCancel }, "Cancel"),
        h("button", { className: "btn btn-primary", onClick: props.onConfirm }, "Rescan")
      )
    );
  }

  /* ------------------------------------------------------------- sources panel */

  var SOURCE_ICON = {
    OK: "✓",
    NO_RESULT: "—",
    ERROR: "✗",
    TIMEOUT: "✗",
    SKIPPED: "—",
    UNREACHABLE: "!",
    RUNNING: "…"
  };

  function SourceList(props) {
    var sources = props.sources || [];
    var collapsed = React.useState(true);
    var failed = sources.filter(function (one) {
      return one.status === "ERROR" || one.status === "TIMEOUT" || one.status === "UNREACHABLE";
    });
    var shown = collapsed[0] ? sources.filter(function (one) {
      return one.status !== "NO_RESULT" && one.status !== "SKIPPED";
    }) : sources;

    return h(
      "div",
      { className: "fd-sources" },
      h(
        "div",
        { className: "fd-sources-head" },
        h("h4", null, "Sources"),
        h(
          "button",
          {
            className: "btn btn-sm btn-link",
            onClick: function () { collapsed[1](!collapsed[0]); }
          },
          collapsed[0] ? "Show all " + sources.length : "Hide the quiet ones"
        )
      ),
      failed.length
        ? h(
            "div",
            { className: "fd-sources-warn" },
            failed.length + " source(s) did not answer. Everything else is below."
          )
        : null,
      h(
        "ul",
        { className: "fd-source-list" },
        shown.map(function (source) {
          return h(
            "li",
            { key: source.id, className: cx("fd-source", "fd-source-" + source.status) },
            h("span", { className: "fd-source-icon" }, SOURCE_ICON[source.status] || "?"),
            h(
              "span",
              { className: "fd-source-name" },
              source.name,
              source.url
                ? h("span", { className: "fd-source-url" }, " ", shortUrl(source.url))
                : null
            ),
            source.type === "url_scraper" && source.depth
              ? h("span", { className: "fd-badge" }, "depth " + source.depth)
              : null,
            source.attribution === "AMBIGUOUS"
              ? h(
                  "span",
                  {
                    className: "fd-badge fd-badge-warn",
                    title:
                      "Stash picks the scraper for a URL itself and does not report which " +
                      "one ran, so this answer can only be attributed to one of: " +
                      (source.handlers || []).join(", ")
                  },
                  "unattributed"
                )
              : null,
            source.error ? h("span", { className: "fd-source-error" }, source.error) : null
          );
        })
      )
    );
  }

  /* ---------------------------------------------------------------- merge table */

  function ValueCell(props) {
    var row = props.row;
    var column = props.column;
    var chosen = props.chosen;
    var valueId = row.cells[column.id];

    if (row.kind === "scalar") {
      if (!valueId) return h("td", { className: "fd-cell fd-cell-empty" }, "");
      var value = props.byId[valueId];
      var selected = chosen === valueId;
      return h(
        "td",
        {
          className: cx("fd-cell", "fd-cell-selectable", selected && "fd-cell-selected"),
          onClick: function () { props.onPick(valueId); },
          title: value.display
        },
        h("span", { className: "fd-radio" }, selected ? "◉" : "○"),
        h("span", { className: "fd-cell-text" }, value.display)
      );
    }

    if (row.kind === "image") {
      if (!valueId) return h("td", { className: "fd-cell fd-cell-empty" }, "");
      var image = props.byId[valueId];
      var isSelected = chosen === valueId;
      return h(
        "td",
        {
          className: cx("fd-cell", "fd-cell-selectable", isSelected && "fd-cell-selected"),
          onClick: function () { props.onPick(valueId); }
        },
        h(Thumbnail, { candidate: image, size: "small" })
      );
    }

    if (row.kind === "entity") {
      if (!valueId) return h("td", { className: "fd-cell fd-cell-empty" }, "");
      var entity = props.byId[valueId];
      var picked = chosen === valueId;
      return h(
        "td",
        {
          className: cx("fd-cell", "fd-cell-selectable", picked && "fd-cell-selected"),
          onClick: function () { props.onPick(valueId); }
        },
        h("span", { className: "fd-radio" }, picked ? "◉" : "○"),
        h(EntityChip, { entity: entity, compact: true })
      );
    }

    // Every list kind: the cell shows what this column contributed, and each chip is
    // the same logical value the union editor below the row toggles.
    var ids = row.cells[column.id] || [];
    if (!ids.length) return h("td", { className: "fd-cell fd-cell-empty" }, "");
    return h(
      "td",
      { className: "fd-cell fd-cell-list" },
      ids.map(function (id) {
        var item = props.byId[id];
        if (!item) return null;
        var on = (chosen || []).indexOf(id) >= 0;
        return h(
          "span",
          {
            key: id,
            className: cx("fd-chip", on ? "fd-chip-on" : "fd-chip-off"),
            onClick: function () { props.onToggle(id); },
            title: item.name || item.display
          },
          h("span", { className: "fd-check" }, on ? "✓" : " "),
          row.kind === "entity_list"
            ? h(EntityChip, { entity: item, compact: true })
            : h("span", null, item.display)
        );
      })
    );
  }

  function EntityChip(props) {
    var entity = props.entity;
    return h(
      "span",
      { className: cx("fd-entity", entity.existing ? "fd-entity-known" : "fd-entity-new") },
      h("span", { className: "fd-entity-name" }, entity.name),
      entity.disambiguation
        ? h("span", { className: "fd-entity-disambiguation" }, " (" + entity.disambiguation + ")")
        : null,
      !props.compact && !entity.existing
        ? h("span", { className: "fd-badge fd-badge-new", title: "Does not exist yet; ticking it creates it on Apply" }, "new")
        : null
    );
  }

  // The union editor for a list row: what is on the scene, what the sources added, and
  // what would have to be created. Deliberately shaped like Stash's own merge dialog -
  // existing entities in the list, candidates underneath with a + (requirement 41).
  function ListEditor(props) {
    var row = props.row;
    var chosen = props.chosen || [];
    var existing = row.values.filter(function (one) {
      return row.kind === "entity_list" ? one.existing || one.on_scene : true;
    });
    var candidates = row.kind === "entity_list"
      ? row.values.filter(function (one) { return !one.existing && !one.on_scene; })
      : [];

    function line(item, isCandidate) {
      var on = chosen.indexOf(item.id) >= 0;
      return h(
        "label",
        { key: item.id, className: cx("fd-pick", on && "fd-pick-on") },
        h("input", {
          type: "checkbox",
          checked: on,
          onChange: function () { props.onToggle(item.id); }
        }),
        isCandidate ? h("span", { className: "fd-plus" }, "+") : null,
        row.kind === "entity_list"
          ? h(EntityChip, { entity: item })
          : h(
              "span",
              { className: "fd-pick-text" },
              row.field === "urls"
                ? h(ExternalLink, { href: item.raw }, shortUrl(item.raw))
                : item.display
            ),
        h(Provenance, { sources: item.sources, columns: props.columns }),
        item.possible_match
          ? h(
              "span",
              { className: "fd-hint", title: "Same name as an entity already in this list" },
              "may be " + item.possible_match.name
            )
          : null,
        item.ambiguous_matches
          ? h(
              "span",
              { className: "fd-hint" },
              item.ambiguous_matches.length + " local records share this name"
            )
          : null
      );
    }

    return h(
      "div",
      { className: "fd-list-editor" },
      existing.length
        ? h(
            "div",
            { className: "fd-list-group" },
            h("div", { className: "fd-list-label" },
              row.kind === "entity_list" ? "Existing / linked" : "Values"),
            existing.map(function (item) { return line(item, false); })
          )
        : null,
      candidates.length
        ? h(
            "div",
            { className: "fd-list-group fd-list-candidates" },
            h(
              "div",
              { className: "fd-list-label" },
              "Candidates",
              h(
                "span",
                { className: "fd-muted" },
                " - do not exist yet; ticking one creates it when you apply"
              )
            ),
            candidates.map(function (item) { return line(item, true); })
          )
        : null
    );
  }

  function Provenance(props) {
    var names = {};
    (props.columns || []).forEach(function (column) { names[column.id] = column; });
    var sources = props.sources || [];
    var label = sources
      .map(function (id) { return (names[id] || {}).name || id; })
      .join(" · ");
    return h(
      "span",
      { className: "fd-provenance", title: label },
      sources.length + (sources.length === 1 ? " source" : " sources")
    );
  }

  /* ------------------------------------------------------------------- images */

  function Thumbnail(props) {
    var candidate = props.candidate;
    var loaded = React.useState(candidate.kind === "blob" ? null : candidate.url);
    var failed = React.useState(false);

    React.useEffect(
      function () {
        // A base64 cover has no address of its own, so it is fetched from the plugin -
        // and only when it is actually about to be shown (requirement 42).
        if (candidate.kind !== "blob" || loaded[0]) return undefined;
        var live = true;
        callOp("review.image", { sha256: candidate.sha256 }).then(
          function (data) { if (live) loaded[1](data.data_uri); },
          function () { if (live) failed[1](true); }
        );
        return function () { live = false; };
      },
      [candidate.sha256, candidate.kind]
    );

    if (failed[0]) return h("div", { className: "fd-thumb fd-thumb-missing" }, "no preview");
    if (!loaded[0]) return h("div", { className: "fd-thumb fd-thumb-loading" }, "");
    return h("img", {
      className: cx("fd-thumb", props.size === "small" && "fd-thumb-small"),
      src: loaded[0],
      alt: "",
      loading: "lazy"
    });
  }

  function ImagePicker(props) {
    var row = props.row;
    var chosen = props.chosen;
    var open = React.useState(false);
    var index = Math.max(0, row.values.findIndex(function (one) { return one.id === chosen; }));
    var current = row.values[index] || row.values[0];
    var columns = {};
    (props.columns || []).forEach(function (column) { columns[column.id] = column; });

    function step(delta) {
      var next = (index + delta + row.values.length) % row.values.length;
      props.onPick(row.values[next].id);
    }

    return h(
      "div",
      { className: "fd-image-picker" },
      h(
        "div",
        { className: "fd-image-strip" },
        h("button", { className: "btn btn-sm btn-secondary", onClick: function () { step(-1); } }, "‹"),
        current ? h(Thumbnail, { candidate: current }) : null,
        h("button", { className: "btn btn-sm btn-secondary", onClick: function () { step(1); } }, "›")
      ),
      h(
        "div",
        { className: "fd-image-meta" },
        h(
          "div",
          null,
          "Source: ",
          (current ? current.sources : [])
            .map(function (id) { return (columns[id] || {}).name || id; })
            .join(" · ")
        ),
        h("div", { className: "fd-muted" }, index + 1 + " / " + row.values.length),
        h(
          "button",
          { className: "btn btn-sm btn-secondary", onClick: function () { open[1](true); } },
          "Open gallery"
        )
      ),
      open[0]
        ? h(
            Modal,
            { show: true, size: "lg", onHide: function () { open[1](false); }, className: "fd-modal" },
            h(Modal.Header, null, h(Modal.Title, null, "Select scene image")),
            h(
              Modal.Body,
              null,
              h(
                "div",
                { className: "fd-gallery" },
                row.values.map(function (candidate) {
                  return h(
                    "label",
                    {
                      key: candidate.id,
                      className: cx("fd-gallery-item", chosen === candidate.id && "fd-gallery-selected")
                    },
                    h("input", {
                      type: "radio",
                      name: "fd-image",
                      checked: chosen === candidate.id,
                      onChange: function () { props.onPick(candidate.id); }
                    }),
                    h(Thumbnail, { candidate: candidate }),
                    h(
                      "span",
                      { className: "fd-gallery-label" },
                      candidate.sources
                        .map(function (id) { return (columns[id] || {}).name || id; })
                        .join(" · ")
                    )
                  );
                })
              )
            ),
            h(
              Modal.Footer,
              null,
              h(
                "button",
                { className: "btn btn-primary", onClick: function () { open[1](false); } },
                "Done"
              )
            )
          )
        : null
    );
  }

  /* -------------------------------------------------------------- review page */

  function MergeTable(props) {
    var review = props.review;
    var selection = props.selection;
    var expanded = React.useState({});

    return h(
      "div",
      { className: "fd-table-wrap" },
      h(
        "table",
        { className: "fd-table" },
        h(
          "thead",
          null,
          h(
            "tr",
            null,
            h("th", { className: "fd-th-field" }, "Field"),
            review.columns.map(function (column) {
              return h(
                "th",
                {
                  key: column.id,
                  className: cx(
                    "fd-th",
                    column.id === "current" && "fd-th-current",
                    column.type === "stashbox" && "fd-th-box"
                  ),
                  title: column.url || column.endpoint || column.name
                },
                h("div", { className: "fd-th-name" }, column.name),
                column.url
                  ? h("div", { className: "fd-th-sub" }, shortUrl(column.url))
                  : column.endpoint
                  ? h("div", { className: "fd-th-sub" }, shortUrl(column.endpoint))
                  : null
              );
            })
          )
        ),
        h(
          "tbody",
          null,
          review.rows.map(function (row) {
            var byId = {};
            row.values.forEach(function (value) { byId[value.id] = value; });
            var chosen = selection[row.field];
            var isList = row.kind === "url_list" || row.kind === "entity_list" ||
              row.kind === "stash_id_list";
            var showEditor = isList || row.kind === "image";
            var isOpen = expanded[0][row.field] !== false;

            var cells = review.columns.map(function (column) {
              return h(ValueCell, {
                key: column.id,
                row: row,
                column: column,
                byId: byId,
                chosen: chosen,
                onPick: function (id) { props.onPick(row.field, id); },
                onToggle: function (id) { props.onToggle(row.field, id); }
              });
            });

            var rows = [
              h(
                "tr",
                { key: row.field, className: cx("fd-row", !row.writable && "fd-row-readonly") },
                h(
                  "th",
                  { className: "fd-th-field", scope: "row" },
                  h("div", { className: "fd-field-label" }, row.label),
                  !row.writable
                    ? h(
                        "div",
                        { className: "fd-muted fd-field-note", title: row.note },
                        "read only"
                      )
                    : null,
                  showEditor
                    ? h(
                        "button",
                        {
                          className: "btn btn-sm btn-link fd-toggle",
                          onClick: function () {
                            var next = Object.assign({}, expanded[0]);
                            next[row.field] = isOpen ? false : true;
                            expanded[1](next);
                          }
                        },
                        isOpen ? "hide chooser" : "choose"
                      )
                    : null
                ),
                cells
              )
            ];

            if (showEditor && isOpen && row.writable) {
              rows.push(
                h(
                  "tr",
                  { key: row.field + "-editor", className: "fd-row-editor" },
                  h(
                    "td",
                    { colSpan: review.columns.length + 1 },
                    row.kind === "image"
                      ? h(ImagePicker, {
                          row: row,
                          chosen: chosen,
                          columns: review.columns,
                          onPick: function (id) { props.onPick(row.field, id); }
                        })
                      : h(ListEditor, {
                          row: row,
                          chosen: chosen,
                          columns: review.columns,
                          onToggle: function (id) { props.onToggle(row.field, id); }
                        })
                  )
                )
              );
            }
            return rows;
          })
        )
      )
    );
  }

  function ReviewPage(props) {
    var params = Router.useParams();
    var history = Router.useHistory ? Router.useHistory() : null;
    var sceneId = props.sceneId || (params && params.id);
    var review = useOp("review.get", { scene_id: Number(sceneId) });
    var selection = React.useState(null);
    var busy = React.useState(null);
    var problem = React.useState(null);
    // What this page did, once it has done it. Set instead of reloading the review:
    // a decided run has no results left to show, and re-fetching one only to render
    // "those are gone" reads like a failure when it is the successful outcome.
    var decided = React.useState(null);
    var toaster = useToaster();
    var starter = useRunStarter(function () {
      toaster.success("FastDiscovery queued. This page updates when it finishes.");
      decided[1](null);
      review.reload();
    });

    React.useEffect(
      function () {
        if (review.data && review.data.selection) {
          selection[1](JSON.parse(JSON.stringify(review.data.selection)));
        }
      },
      [review.data]
    );

    // The selection is saved as it changes, so closing the tab and coming back later
    // does not lose the choices already made (requirement 27).
    React.useEffect(
      function () {
        if (!review.data || !selection[0]) return undefined;
        var runId = review.data.run.id;
        var payload = selection[0];
        var timer = setTimeout(function () {
          callOp("review.save", { run_id: runId, selection: payload }).catch(function () {});
        }, 1500);
        return function () { clearTimeout(timer); };
      },
      [selection[0]]
    );

    if (decided[0]) {
      return h(Decided, {
        outcome: decided[0],
        onRescan: function () { starter.start([Number(sceneId)], true); },
        onBack: history ? function () { history.push(BASE); } : null,
        busy: starter.busy,
        confirming: starter.confirming,
        onCancelConfirm: starter.cancelConfirm,
        onConfirmReplace: starter.confirmReplace
      });
    }
    if (review.loading && !review.data) return h(Loading, { label: "Building the review..." });
    if (review.error) {
      return h(
        "div",
        { className: "fd-page" },
        h(Problem, { error: review.error, onRetry: review.reload }),
        h(
          "button",
          {
            className: "btn btn-primary",
            disabled: starter.busy,
            onClick: function () { starter.start([Number(sceneId)], false); }
          },
          "Run FastDiscovery"
        ),
        h(ConfirmRescan, {
          confirming: starter.confirming,
          onCancel: starter.cancelConfirm,
          onConfirm: starter.confirmReplace
        })
      );
    }
    if (!review.data || !selection[0]) return h(Loading, null);

    var data = review.data;

    function pick(field, id) {
      var next = Object.assign({}, selection[0]);
      next[field] = next[field] === id ? null : id;
      selection[1](next);
    }

    function toggle(field, id) {
      var next = Object.assign({}, selection[0]);
      var list = (next[field] || []).slice();
      var at = list.indexOf(id);
      if (at >= 0) list.splice(at, 1);
      else list.push(id);
      next[field] = list;
      selection[1](next);
    }

    function decide(op, extra) {
      busy[1](op);
      problem[1](null);
      callOp(op, Object.assign({ run_id: data.run.id }, extra || {})).then(
        function (result) {
          busy[1](null);
          if (op === "apply.commit" && !result.applied) {
            // Nothing was selected that would change anything. Not a failure, and not
            // a decision either - the review stays open.
            toaster.success(result.reason || "Nothing needed writing.");
            return;
          }
          var message =
            op === "apply.commit"
              ? "FastDiscovery applied " + (result.changes || []).length +
                " field(s) to this scene."
              : "FastDiscovery results rejected. The scene was not touched.";
          toaster.success(message);
          decided[1]({
            action: op === "apply.commit" ? "applied" : "rejected",
            message: message,
            changes: result.changes || [],
            created: result.created || {}
          });
          announceChange();
        },
        function (failure) {
          busy[1](null);
          problem[1](failure.message);
          toaster.failure(failure.message);
        }
      );
    }

    return h(
      "div",
      { className: "fd-page fd-review" },
      h(
        "div",
        { className: "fd-review-head" },
        h(
          "div",
          null,
          h("h2", null, "FastDiscovery"),
          h(
            "div",
            { className: "fd-muted" },
            data.scene.title || data.scene.filename,
            " · ",
            h(StatusPill, { status: data.run.status }),
            " · ",
            data.summary.columns + " column(s) from " + data.summary.sources + " source(s)",
            data.summary.failed_sources
              ? " · " + data.summary.failed_sources + " failed"
              : "",
            " · " + data.summary.urls + " URL(s)"
          )
        ),
        history
          ? h(
              "div",
              { className: "fd-actions" },
              h(
                "button",
                { className: "btn btn-link", onClick: function () { history.push(BASE); } },
                "All runs"
              )
            )
          : null
      ),
      h(Problem, { error: problem[0] || starter.error }),
      data.run.stop_reason
        ? h("div", { className: "fd-note" }, "Stopped early: " + data.run.stop_reason)
        : null,
      h(SourceList, { sources: data.sources }),
      data.rows.length
        ? h(MergeTable, {
            review: data,
            selection: selection[0],
            onPick: pick,
            onToggle: toggle
          })
        : h("div", { className: "fd-empty" }, "Nothing was found for this scene."),
      h(UrlGraph, { graph: data.urls_graph }),
      // Under the table, not above it: the decision is what you reach after reading
      // everything, so it is where reading everything leaves you.
      h(
        "div",
        { className: "fd-decide" },
        h(
          "div",
          { className: "fd-muted" },
          "Nothing has been written to this scene yet."
        ),
        h(
          "div",
          { className: "fd-actions" },
          h(
            "button",
            {
              className: "btn btn-primary",
              disabled: !!busy[0] || !data.run.reviewable,
              onClick: function () {
                decide("apply.commit", {
                  selection: selection[0],
                  expected_updated_at: data.scene.updated_at
                });
              }
            },
            busy[0] === "apply.commit" ? "Applying..." : "Apply"
          ),
          h(
            "button",
            {
              className: "btn btn-secondary",
              disabled: !!busy[0] || !data.run.reviewable,
              onClick: function () { decide("run.reject"); }
            },
            "Reject"
          ),
          h(
            "button",
            {
              className: "btn btn-secondary",
              disabled: starter.busy,
              onClick: function () { starter.start([Number(sceneId)], false); }
            },
            "Rescan"
          )
        )
      ),
      h(ConfirmRescan, {
        confirming: starter.confirming,
        onCancel: starter.cancelConfirm,
        onConfirm: starter.confirmReplace
      })
    );
  }

  // What is left after a decision. The results are gone by design, so there is nothing
  // to re-fetch and nothing to apologise for - just what happened, and the two things
  // worth doing next.
  function Decided(props) {
    var outcome = props.outcome;
    var created = outcome.created || {};
    var createdLine = Object.keys(created)
      .map(function (kind) { return created[kind].length + " " + kind + "(s)"; })
      .join(", ");
    return h(
      "div",
      { className: "fd-page fd-review" },
      h(
        "div",
        { className: "fd-decided" },
        h("h3", null, outcome.action === "applied" ? "Applied" : "Rejected"),
        h("p", null, outcome.message),
        outcome.action === "applied" && outcome.changes.length
          ? h(
              "p",
              { className: "fd-muted" },
              "Fields written: " +
                outcome.changes.map(function (change) { return change.field; }).join(", ") +
                (createdLine ? ". Created: " + createdLine + "." : ".")
            )
          : null,
        h(
          "div",
          { className: "fd-actions" },
          props.onBack
            ? h("button", { className: "btn btn-primary", onClick: props.onBack },
                "Back to FastDiscovery")
            : null,
          h(
            "button",
            { className: "btn btn-secondary", disabled: props.busy, onClick: props.onRescan },
            "Run again"
          )
        )
      ),
      h(ConfirmRescan, {
        confirming: props.confirming,
        onCancel: props.onCancelConfirm,
        onConfirm: props.onConfirmReplace
      })
    );
  }

  function UrlGraph(props) {
    var open = React.useState(false);
    var graph = props.graph || [];
    if (!graph.length) return null;
    return h(
      "div",
      { className: "fd-graph" },
      h(
        "button",
        { className: "btn btn-sm btn-link", onClick: function () { open[1](!open[0]); } },
        (open[0] ? "Hide" : "Show") + " the " + graph.length + " URL(s) this run walked"
      ),
      open[0]
        ? h(
            "table",
            { className: "fd-graph-table" },
            h(
              "thead",
              null,
              h(
                "tr",
                null,
                ["URL", "Depth", "Found by", "Handlers", "State"].map(function (label) {
                  return h("th", { key: label }, label);
                })
              )
            ),
            h(
              "tbody",
              null,
              graph.map(function (entry, index) {
                return h(
                  "tr",
                  { key: index, className: "fd-graph-" + entry.state },
                  h("td", null, h(ExternalLink, { href: entry.url }, shortUrl(entry.url))),
                  h("td", null, entry.depth),
                  h("td", null, entry.found_by || "-"),
                  h("td", null, (entry.handlers || []).join(", ") || "-"),
                  h("td", { title: entry.note || "" }, entry.state)
                );
              })
            )
          )
        : null
    );
  }

  /* ----------------------------------------------------------------- runs page */

  function RunsPage() {
    var tab = React.useState("ready");
    var page = React.useState(1);
    var perPage = React.useState(loadPerPage);
    var listing = useOp("run.list",
                        { tab: tab[0], page: page[0], per_page: perPage[0] });
    var history = Router.useHistory ? Router.useHistory() : null;

    // A decision taken in a review that is open at the same time - on the scene page,
    // or in another tab of this browser - moves a run out of this list, so listen for
    // it rather than leaving a stale row behind.
    React.useEffect(
      function () {
        window.addEventListener(CHANGED_EVENT, listing.reload);
        return function () { window.removeEventListener(CHANGED_EVENT, listing.reload); };
      },
      [listing.reload]
    );

    function choosePerPage(value) {
      savePerPage(value);
      perPage[1](value);
      page[1](1);
    }

    return h(
      "div",
      { className: "fd-page" },
      h(
        "div",
        { className: "fd-review-head" },
        h("h2", null, "FastDiscovery"),
        h(
          "div",
          { className: "fd-actions" },
          h(
            "button",
            { className: "btn btn-secondary", onClick: listing.reload },
            "Refresh"
          ),
          h(
            Router.NavLink,
            { className: "btn btn-secondary", to: BASE + "/settings" },
            "Settings"
          )
        )
      ),
      h(
        "div",
        { className: "fd-tabs" },
        TABS.map(function (entry) {
          var counts = (listing.data && listing.data.counts) || {};
          var total = entry.key === "all"
            ? Object.keys(counts).reduce(function (sum, key) { return sum + counts[key]; }, 0)
            : null;
          return h(
            "button",
            {
              key: entry.key,
              className: cx("btn btn-sm", tab[0] === entry.key ? "btn-primary" : "btn-secondary"),
              onClick: function () { tab[1](entry.key); page[1](1); }
            },
            entry.label,
            total !== null ? h("span", { className: "fd-badge" }, total) : null
          );
        }),
        h(
          "label",
          { className: "fd-per-page" },
          "Show",
          h(
            "select",
            {
              className: "form-control input-sm",
              value: perPage[0],
              onChange: function (event) { choosePerPage(Number(event.target.value)); }
            },
            PER_PAGE_CHOICES.map(function (size) {
              return h("option", { key: size, value: size }, size);
            })
          )
        )
      ),
      h(Problem, { error: listing.error, onRetry: listing.reload }),
      listing.loading && !listing.data ? h(Loading, null) : null,
      listing.data
        ? h(
            "table",
            { className: "fd-runs" },
            h(
              "thead",
              null,
              h(
                "tr",
                null,
                ["Scene", "Status", "Sources", "URLs", "Results", "Started", "Finished", ""]
                  .map(function (label, index) { return h("th", { key: index }, label); })
              )
            ),
            h(
              "tbody",
              null,
              listing.data.runs.map(function (run) {
                var scene = run.scene || {};
                return h(
                  "tr",
                  { key: run.id },
                  h(
                    "td",
                    null,
                    h(
                      Router.NavLink,
                      { to: "/scenes/" + run.scene_id },
                      scene.title || scene.filename || "scene " + run.scene_id
                    ),
                    scene.studio ? h("div", { className: "fd-muted" }, scene.studio) : null
                  ),
                  h(
                    "td",
                    null,
                    h(StatusPill, { status: run.status }),
                    run.error_count
                      ? h("div", { className: "fd-muted" }, run.error_count + " error(s)")
                      : null
                  ),
                  h("td", null, run.ok_source_count + " / " + run.source_count),
                  h("td", null, run.url_count),
                  h("td", null, run.result_count),
                  h("td", null, when(run.started_at)),
                  h("td", null, when(run.finished_at)),
                  h(
                    "td",
                    { className: "fd-row-actions" },
                    run.reviewable
                      ? h(
                          "button",
                          {
                            className: "btn btn-sm btn-primary",
                            onClick: function () {
                              if (history) history.push(BASE + "/scene/" + run.scene_id);
                            }
                          },
                          "Review"
                        )
                      : null,
                    !run.reviewable && !run.purged
                      ? h(
                          "button",
                          {
                            className: "btn btn-sm btn-secondary",
                            onClick: function () {
                              callOp("run.delete", { run_id: run.id }).then(listing.reload);
                            }
                          },
                          "Dismiss"
                        )
                      : null
                  )
                );
              })
            )
          )
        : null,
      listing.data && !listing.data.runs.length
        ? h("div", { className: "fd-empty" }, "Nothing here.")
        : null,
      listing.data && listing.data.total > perPage[0]
        ? h(
            "div",
            { className: "fd-paging" },
            h(
              "button",
              {
                className: "btn btn-sm btn-secondary",
                disabled: page[0] <= 1,
                onClick: function () { page[1](page[0] - 1); }
              },
              "Previous"
            ),
            h(
              "span",
              null,
              " page " + page[0] + " of " +
                Math.ceil(listing.data.total / perPage[0]) + " "
            ),
            h(
              "button",
              {
                className: "btn btn-sm btn-secondary",
                disabled: page[0] * perPage[0] >= listing.data.total,
                onClick: function () { page[1](page[0] + 1); }
              },
              "Next"
            )
          )
        : null
    );
  }

  /* ------------------------------------------------------------- settings page */

  function SettingsPage() {
    var loaded = useOp("settings.get", {});
    var draft = React.useState(null);
    var saving = React.useState(false);
    var problem = React.useState(null);
    var saved = React.useState(null);

    React.useEffect(
      function () {
        if (loaded.data) draft[1](Object.assign({}, loaded.data.values));
      },
      [loaded.data]
    );

    if (loaded.loading && !loaded.data) return h(Loading, null);
    if (loaded.error) return h(Problem, { error: loaded.error, onRetry: loaded.reload });
    if (!draft[0]) return h(Loading, null);

    function save() {
      saving[1](true);
      problem[1](null);
      callOp("settings.set", { values: draft[0] }).then(
        function (result) {
          saving[1](false);
          saved[1]("Saved.");
          draft[1](Object.assign({}, result.values));
        },
        function (failure) {
          saving[1](false);
          problem[1](failure.message);
        }
      );
    }

    return h(
      "div",
      { className: "fd-page fd-settings" },
      h("h2", null, "FastDiscovery settings"),
      h(Problem, { error: problem[0] }),
      saved[0] ? h("div", { className: "fd-done" }, saved[0]) : null,
      h(
        "div",
        { className: "fd-detected" },
        h("h4", null, "Detected stash-boxes"),
        h(
          "ul",
          null,
          (loaded.data.stash_boxes || []).map(function (box) {
            return h(
              "li",
              { key: box.endpoint },
              h("span", { className: "fd-source-icon" }, "✓"),
              " ",
              box.name,
              h("span", { className: "fd-muted" }, " " + shortUrl(box.endpoint))
            );
          })
        ),
        h(
          "p",
          { className: "fd-muted" },
          "Read from Stash every time a run starts. Add or remove one in Settings -> " +
            "Metadata Providers and FastDiscovery follows; there is no second place to " +
            "configure them and no second copy of their API keys."
        )
      ),
      h(
        "div",
        { className: "fd-setting-list" },
        loaded.data.spec.map(function (entry) {
          var value = draft[0][entry.name];
          function change(next) {
            var updated = Object.assign({}, draft[0]);
            updated[entry.name] = next;
            draft[1](updated);
          }
          return h(
            "div",
            { key: entry.name, className: "fd-setting" },
            h(
              "label",
              null,
              entry.type === "BOOLEAN"
                ? h("input", {
                    type: "checkbox",
                    checked: !!value,
                    onChange: function (event) { change(event.target.checked); }
                  })
                : null,
              h("span", { className: "fd-setting-name" }, entry.name)
            ),
            entry.type !== "BOOLEAN"
              ? h("input", {
                  className: "form-control fd-setting-input",
                  type: entry.type === "NUMBER" ? "number" : "text",
                  value: value === null || value === undefined ? "" : value,
                  onChange: function (event) {
                    change(entry.type === "NUMBER"
                      ? Number(event.target.value)
                      : event.target.value);
                  }
                })
              : null,
            h("div", { className: "fd-setting-help" }, entry.description),
            entry.limits
              ? h(
                  "div",
                  { className: "fd-muted" },
                  "between " + entry.limits[0] + " and " + entry.limits[1] +
                    ", default " + entry.default
                )
              : null
          );
        })
      ),
      h(
        "div",
        { className: "fd-actions" },
        h(
          "button",
          { className: "btn btn-primary", disabled: saving[0], onClick: save },
          saving[0] ? "Saving..." : "Save"
        ),
        h(
          "button",
          {
            className: "btn btn-secondary",
            onClick: function () {
              callOp("maintenance.run", { vacuum: true }).then(
                function (result) {
                  saved[1](
                    "Cleaned up: " + result.stale_runs_failed + " stale run(s), " +
                      result.orphan_images_removed + " image(s)."
                  );
                },
                function (failure) { problem[1](failure.message); }
              );
            }
          },
          "Clean up stale runs"
        )
      )
    );
  }

  /* -------------------------------------------------------- scene page panel */

  function ScenePanel(props) {
    var sceneId = props.sceneId;
    var status = useOp("scene.status", { scene_id: Number(sceneId) });
    var starter = useRunStarter(function () { status.reload(); });
    var history = Router.useHistory ? Router.useHistory() : null;

    // Poll only while something is actually running.
    React.useEffect(
      function () {
        var run = status.data && status.data.run;
        if (!run || run.status !== "RUNNING") return undefined;
        var timer = setInterval(status.reload, 4000);
        return function () { clearInterval(timer); };
      },
      [status.data]
    );

    // The review below this panel can decide the run; when it does, the counts and
    // buttons up here are about a run that no longer exists in that state.
    React.useEffect(
      function () {
        window.addEventListener(CHANGED_EVENT, status.reload);
        return function () { window.removeEventListener(CHANGED_EVENT, status.reload); };
      },
      [status.reload]
    );

    if (status.loading && !status.data) return h(Loading, null);
    if (status.error) return h(Problem, { error: status.error, onRetry: status.reload });

    var run = status.data && status.data.run;
    var reviewing = run && run.reviewable;

    return h(
      "div",
      { className: "fd-panel" },
      h(
        "div",
        { className: "fd-panel-head" },
        h("h4", null, "FastDiscovery"),
        run ? h(StatusPill, { status: run.status }) : null
      ),
      h(Problem, { error: starter.error }),
      !run
        ? h(
            "p",
            { className: "fd-muted" },
            "Runs every stash-box you have configured, then follows every URL through " +
              "every scraper that can read it. Nothing is written until you apply."
          )
        : h(
            "div",
            { className: "fd-panel-counts" },
            h("span", null, run.ok_source_count + " / " + run.source_count + " source(s) answered"),
            h("span", null, run.result_count + " result(s)"),
            h("span", null, run.url_count + " URL(s)"),
            run.error_count ? h("span", { className: "fd-warn" }, run.error_count + " error(s)") : null,
            run.error ? h("span", { className: "fd-warn" }, run.error) : null
          ),
      h(
        "div",
        { className: "fd-actions" },
        reviewing
          ? h(
              "button",
              {
                className: "btn btn-primary",
                onClick: function () {
                  if (history) history.push(BASE + "/scene/" + sceneId);
                }
              },
              "Review results"
            )
          : null,
        h(
          "button",
          {
            className: reviewing ? "btn btn-secondary" : "btn btn-primary",
            disabled: starter.busy || (run && run.status === "RUNNING"),
            onClick: function () { starter.start([Number(sceneId)], false); }
          },
          run && run.status === "RUNNING"
            ? "Running..."
            : reviewing
            ? "Rescan"
            : "Run FastDiscovery"
        ),
        run && run.status === "RUNNING" && run.job_id
          ? h(
              "button",
              {
                className: "btn btn-secondary",
                onClick: function () {
                  callOp("run.cancel", { run_id: run.id }).then(status.reload);
                }
              },
              "Cancel"
            )
          : null
      ),
      reviewing
        ? h(ReviewPage, { sceneId: sceneId })
        : null,
      (status.data.history || []).length
        ? h(
            "div",
            { className: "fd-history" },
            h("h5", null, "History"),
            status.data.history.map(function (entry) {
              return h(
                "div",
                { key: entry.id, className: "fd-muted" },
                when(entry.applied_at) + " · " + entry.status +
                  ((entry.fields || []).length ? " · " + entry.fields.join(", ") : "")
              );
            })
          )
        : null,
      h(ConfirmRescan, {
        confirming: starter.confirming,
        onCancel: starter.cancelConfirm,
        onConfirm: starter.confirmReplace
      })
    );
  }

  function SceneTabBadge(props) {
    var status = useOp("scene.status", { scene_id: Number(props.sceneId) });
    var run = status.data && status.data.run;
    if (!run) return null;
    if (run.status === "RUNNING") return h("span", { className: "fd-tab-badge" }, "...");
    if (!run.reviewable) return null;
    return h("span", { className: "fd-tab-badge" }, run.result_count);
  }

  /* -------------------------------------------------------------- registration */

  api.register.route(BASE, RunsPage);
  api.register.route(BASE + "/settings", SettingsPage);
  api.register.route(BASE + "/scene/:id", ReviewPage);

  api.patch.before("MainNavBar.MenuItems", function (props) {
    return [
      Object.assign({}, props, {
        children: h(
          React.Fragment,
          null,
          props.children,
          h(
            Router.NavLink,
            {
              className: "nav-utility minimal btn btn-primary",
              exact: true,
              to: BASE,
              title: "FastDiscovery"
            },
            h("span", null, "FastDiscovery")
          )
        )
      })
    ];
  });

  // A tab rather than more buttons in the scene header: the scene page is already busy,
  // and the review is a table, not a button's worth of information.
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
              { eventKey: "fastdiscovery-panel" },
              "FastDiscovery",
              h(SceneTabBadge, { sceneId: sceneId })
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
            { eventKey: "fastdiscovery-panel" },
            h(ScenePanel, { sceneId: sceneId })
          )
        )
      })
    ];
  });

  // The scene list's operations menu for the current selection. Batch discovery never
  // opens a review: the runs land on the FastDiscovery page, one row per scene
  // (requirement 25).
  api.patch.before("SceneListOperations", function (props) {
    return [
      Object.assign({}, props, {
        children: h(
          React.Fragment,
          null,
          props.children,
          h(BulkDiscoverItem, { selected: props.selected || props.selectedIds })
        )
      })
    ];
  });

  function BulkDiscoverItem(props) {
    var history = Router.useHistory ? Router.useHistory() : null;
    var starter = useRunStarter(function () {
      if (history) history.push(BASE);
    });
    var ids = idsOf(props.selected);

    return h(
      React.Fragment,
      null,
      h(
        Bootstrap.Dropdown.Item,
        {
          disabled: !ids.length || starter.busy,
          onClick: function () { starter.start(ids, false); }
        },
        "FastDiscovery" + (ids.length ? " (" + ids.length + " scenes)" : "")
      ),
      h(ConfirmRescan, {
        confirming: starter.confirming,
        onCancel: starter.cancelConfirm,
        onConfirm: starter.confirmReplace
      })
    );
  }

  // Stash has passed the current selection as a Set of ids, an array of ids and an
  // array of scenes at different points in its history; all three are read here so the
  // menu item does not quietly stop working after an upgrade.
  function idsOf(selected) {
    if (!selected) return [];
    var list = typeof selected.forEach === "function" && typeof selected.size === "number"
      ? Array.from(selected)
      : Array.isArray(selected)
      ? selected
      : [];
    return list
      .map(function (entry) {
        return entry && typeof entry === "object" ? entry.id : entry;
      })
      .filter(Boolean)
      .map(Number)
      .filter(function (id) { return !isNaN(id); });
  }

  console.log("[FastDiscovery] UI loaded");
})();
