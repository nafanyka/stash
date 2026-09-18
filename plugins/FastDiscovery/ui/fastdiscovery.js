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
  var Button = Bootstrap.Button;

  // Stash's own nav items are FontAwesome icons over a label, so the menu entry is
  // built out of the same pieces rather than approximated. Both libraries are part of
  // PluginApi; the icon is optional, because a missing one should cost the label, not
  // the whole menu item.
  var FontAwesome = api.libraries.ReactFontAwesome;
  var Solid = api.libraries.FontAwesomeSolid || {};
  // FA6 renamed the magnifier; the old name is still exported as an alias by some
  // builds, and a plain compass is a reasonable last resort for "go and find things".
  var NAV_ICON = Solid.faMagnifyingGlass || Solid.faSearch || Solid.faCompass || null;

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

    var replace = React.useCallback(function (data) {
      setValue({ loading: false, data: data, error: null });
    }, []);

    return { loading: value.loading, data: value.data, error: value.error,
             reload: reload, replace: replace };
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
      if (!valueId) {
        return h("td", { className: "fd-cell fd-cell-image fd-cell-empty" }, "");
      }
      var image = props.byId[valueId];
      var isSelected = chosen === valueId;
      return h(
        "td",
        {
          className: cx("fd-cell", "fd-cell-image", "fd-cell-selectable",
                        isSelected && "fd-cell-selected"),
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
      h(
        "span",
        {
          className: "fd-entity-name",
          // The scraped name and the name on the record are not always the same one.
          // Saying which alias led here explains a row that otherwise looks like it
          // arrived from nowhere.
          title: entity.alias_of
            ? "Scraped as \"" + entity.alias_of + "\", which this record holds as an alias"
            : undefined
        },
        entity.name
      ),
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

  /* --------------------------------------------------------------- rejecting */

  // One result of one source, struck out. Lives in that column's header.
  function RejectToggle(props) {
    var column = props.column;
    return h(
      "label",
      {
        className: cx("fd-reject", column.rejected && "fd-reject-on"),
        title: column.rejected
          ? "Rejected: nothing in this column is counted. Untick to bring it back."
          : "Reject this result - drops everything it said from every row"
      },
      h("input", {
        type: "checkbox",
        checked: !!column.rejected,
        disabled: !!props.busy,
        onChange: function () { props.onReject(column, !column.rejected); }
      }),
      h("span", null, column.rejected ? "rejected" : "reject")
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

    var sized = cx("fd-thumb",
                   props.size === "small" && "fd-thumb-small",
                   props.size === "gallery" && "fd-thumb-gallery",
                   props.size === "header" && "fd-thumb-header");
    // `width` is the one override the performer image preview setting needs: a fixed
    // pixel width, height following automatically so nothing is distorted
    // (requirement 11). Scene call sites never pass it, so their sizing - the
    // percentage/max-height rules in the CSS - is completely unaffected. `maxHeight`
    // must be cleared here too: `.fd-thumb-gallery`'s own max-height/object-fit pair
    // is a fallback for when no width is given, and left in place it caps a tall
    // photo's height and then, because object-fit still has to fill that box, its
    // rendered width shrinks below the fixed width to keep the aspect ratio - which
    // is exactly "same width" turning into "same height instead" for tall pictures.
    var style = props.width
      ? { width: props.width + "px", maxWidth: props.width + "px", maxHeight: "none",
          height: "auto" }
      : undefined;
    // The placeholders carry the size classes too, so a cell does not change width
    // when its picture arrives.
    if (failed[0]) return h("div", { className: cx(sized, "fd-thumb-missing"), style: style }, "no preview");
    if (!loaded[0]) return h("div", { className: cx(sized, "fd-thumb-loading"), style: style }, "");
    return h("img", {
      className: sized,
      style: style,
      src: loaded[0],
      alt: "",
      loading: "lazy",
      // A cover can be an address this browser cannot reach - a scraper host that is
      // down, one that refuses hotlinking, or a Stash URL built for a different host.
      // Saying so beats a broken-image glyph the reviewer has to interpret.
      onError: function () { failed[1](true); }
    });
  }

  function ImagePicker(props) {
    var row = props.row;
    var chosen = props.chosen;
    var open = React.useState(false);
    // Two steps: first narrow a long gallery down to a handful of favourites
    // (checkboxes, nothing committed yet), then pick exactly one of *those*
    // (radio, committed only on Done). Both reset every time the gallery is
    // opened, seeded from whatever is chosen right now.
    var phase = React.useState("shortlist"); // "shortlist" | "pick"
    var shortlist = React.useState(function () { return new Set(); });
    var picked = React.useState(null);
    var index = Math.max(0, row.values.findIndex(function (one) { return one.id === chosen; }));
    var current = row.values[index] || row.values[0];
    var columns = {};
    (props.columns || []).forEach(function (column) { columns[column.id] = column; });

    function openGallery() {
      phase[1]("shortlist");
      shortlist[1](new Set(chosen ? [chosen] : []));
      picked[1](chosen || null);
      open[1](true);
    }

    function closeGallery() {
      open[1](false);
    }

    function toggleShortlist(id) {
      var next = new Set(shortlist[0]);
      if (next.has(id)) next.delete(id); else next.add(id);
      shortlist[1](next);
    }

    function goToPick() {
      if (!shortlist[0].size) return;
      picked[1](shortlist[0].has(chosen) ? chosen : Array.from(shortlist[0])[0]);
      phase[1]("pick");
    }

    function commitPick() {
      if (picked[0]) props.onPick(picked[0]);
      open[1](false);
    }

    // The gallery groups by source/result, never a flat grid: Source A and every
    // photo it offered, then Source B and its own. A photo two sources both
    // returned (deduplicated by content or URL) appears once in each of their
    // groups - it really was offered by both, and that is provenance, not a bug.
    // In the "pick" step the same grouping applies, just narrowed to whatever
    // made the shortlist.
    var groups = (props.columns || [])
      .filter(function (column) { return !column.rejected; })
      .map(function (column) {
        var items = row.values.filter(function (candidate) {
          if (phase[0] === "pick" && !shortlist[0].has(candidate.id)) return false;
          return candidate.sources.indexOf(column.id) >= 0;
        });
        return { column: column, items: items };
      })
      .filter(function (group) { return group.items.length; });

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
        current ? h(Thumbnail, { candidate: current, width: props.thumbWidth }) : null,
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
          { className: "btn btn-sm btn-secondary", onClick: openGallery },
          "Open gallery"
        )
      ),
      open[0]
        ? h(
            Modal,
            {
              show: true,
              size: "lg",
              onHide: closeGallery,
              className: "fd-modal",
              // Choosing a cover means looking at it, so the gallery gets the width.
              dialogClassName: "fd-modal-wide"
            },
            h(Modal.Header, null, h(Modal.Title, null, props.title || "Select image")),
            h(
              Modal.Body,
              null,
              h(
                "p",
                { className: "fd-muted fd-gallery-step" },
                phase[0] === "shortlist"
                  ? "Step 1 of 2: tick every photo worth a closer look."
                  : "Step 2 of 2: pick the one to use."
              ),
              h(
                "div",
                { className: "fd-gallery-groups" },
                groups.map(function (group) {
                  return h(
                    "div",
                    { key: group.column.id, className: "fd-gallery-group" },
                    h("div", { className: "fd-gallery-group-label" }, group.column.name),
                    h(
                      "div",
                      { className: "fd-gallery" },
                      group.items.map(function (candidate) {
                        // The group heading already says the source; a photo shared
                        // with another source is simply in both groups, so no
                        // per-image caption is needed here.
                        var inShortlist = phase[0] === "shortlist";
                        return h(
                          "label",
                          {
                            key: group.column.id + ":" + candidate.id,
                            className: cx("fd-gallery-item",
                                         (inShortlist
                                           ? shortlist[0].has(candidate.id)
                                           : picked[0] === candidate.id)
                                           && "fd-gallery-selected")
                          },
                          h("input", inShortlist
                            ? {
                                type: "checkbox",
                                checked: shortlist[0].has(candidate.id),
                                onChange: function () { toggleShortlist(candidate.id); }
                              }
                            : {
                                type: "radio",
                                name: "fd-image",
                                checked: picked[0] === candidate.id,
                                onChange: function () { picked[1](candidate.id); }
                              }),
                          h(Thumbnail, { candidate: candidate, size: "gallery",
                                        width: props.thumbWidth })
                        );
                      })
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
                { className: "btn btn-secondary", onClick: closeGallery },
                "Close"
              ),
              phase[0] === "shortlist"
                ? h(
                    "button",
                    { className: "btn btn-primary", onClick: goToPick,
                      disabled: !shortlist[0].size },
                    "Select"
                  )
                : h(
                    "button",
                    { className: "btn btn-primary", onClick: commitPick,
                      disabled: !picked[0] },
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
    var onReject = props.onReject;
    // Optional: a performer review passes its image row here so each column header
    // carries the result's first photo, for telling the right person from the wrong
    // one at a glance (requirement 10). Scene reviews never pass this, so a scene's
    // header is rendered exactly as before.
    var previewRow = props.headerPreview;
    var previewById = {};
    if (previewRow) {
      previewRow.values.forEach(function (value) { previewById[value.id] = value; });
    }

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
                    column.type === "stashbox" && "fd-th-box",
                    column.rejected && "fd-column-rejected"
                  ),
                  title: column.url || column.endpoint || column.name
                },
                h("div", { className: "fd-th-name" }, column.name),
                column.url
                  ? h("div", { className: "fd-th-sub" }, shortUrl(column.url))
                  : column.endpoint
                  ? h("div", { className: "fd-th-sub" }, shortUrl(column.endpoint))
                  : null,
                previewRow && column.id !== "current" && previewById[previewRow.cells[column.id]]
                  ? h(Thumbnail, {
                      candidate: previewById[previewRow.cells[column.id]],
                      size: "header",
                      width: props.thumbWidth
                    })
                  : null,
                // One result of one source. A source that answered with a list has a
                // column per answer, and the second can be a different scene entirely
                // while the first is right - so this is per column, not per source.
                onReject && column.id !== "current"
                  ? h(RejectToggle, {
                      column: column,
                      busy: props.busy,
                      onReject: onReject
                    })
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
              if (column.rejected) {
                // Shown, so the choice is visible and reversible; empty, because
                // nothing this source said counts any more.
                return h("td", { key: column.id, className: "fd-cell fd-column-rejected" });
              }
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

            // A writable row with nothing chosen writes nothing. That is a legitimate
            // choice - and also exactly what an unnoticed one looks like - so the field
            // name says so rather than leaving it to be spotted by scanning the row.
            var unset = row.writable &&
              (isList ? !(chosen && chosen.length) : !chosen);

            var rows = [
              h(
                "tr",
                { key: row.field, className: cx("fd-row", !row.writable && "fd-row-readonly") },
                h(
                  "th",
                  {
                    className: cx("fd-th-field", unset && "fd-field-unset"),
                    scope: "row",
                    title: unset
                      ? (isList
                          ? "Nothing selected - this field will be left empty"
                          : "Nothing selected - this field will be left as it is")
                      : undefined
                  },
                  h(
                    "div",
                    { className: "fd-field-label" },
                    row.label,
                    unset ? h("span", { className: "fd-unset-mark" }, "○") : null
                  ),
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
                          title: "Select " + row.label.toLowerCase(),
                          thumbWidth: props.thumbWidth,
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
        showBack: !props.sceneId,
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
      if (at >= 0) {
        list.splice(at, 1);
      } else {
        var row = data.rows.filter(function (one) { return one.field === field; })[0];
        // Some list rows can hold only one value per key - a scene has exactly one
        // stash id per box, and Stash enforces that with a unique index. Ticking one
        // therefore unticks its rival instead of queueing a write that fails in the
        // database.
        if (row && row.exclusive_by) {
          var byId = {};
          row.values.forEach(function (value) { byId[value.id] = value; });
          var key = (byId[id] || {})[row.exclusive_by];
          list = list.filter(function (other) {
            return (byId[other] || {})[row.exclusive_by] !== key;
          });
        }
        list.push(id);
      }
      next[field] = list;
      selection[1](next);
    }

    function rejectColumn(column, rejected) {
      busy[1]("reject");
      problem[1](null);
      callOp("review.reject_column", {
        run_id: data.run.id,
        column_id: column.id,
        rejected: rejected
      }).then(
        function (fresh) {
          busy[1](null);
          // The whole matrix is rebuilt without that source, so the page takes the new
          // one rather than trying to patch the old one.
          review.replace(fresh);
          selection[1](JSON.parse(JSON.stringify(fresh.selection || {})));
          toaster.success(
            (rejected ? "Rejected " : "Restored ") + column.name +
              (rejected ? " - nothing it said is counted." : " - its values are back.")
          );
        },
        function (failure) {
          busy[1](null);
          problem[1](failure.message);
          toaster.failure(failure.message);
        }
      );
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
                " field(s) to this scene." +
                // Apply also tags the scene, so it can be told apart later from one
                // FastDiscovery has never written to. Said out loud, because it is the
                // one change nobody ticked.
                (result.marker ? " Tagged " + result.marker.name + "." : "")
              : "FastDiscovery results rejected. The scene was not touched.";
          toaster.success(message);
          announceChange();
          if (history && !props.sceneId) {
            // The review had a page to itself, which now has nothing to show. The toast
            // already said what happened, so go back to the list rather than parking on
            // a dead end that needs one more click.
            history.push(BASE);
            return;
          }
          decided[1]({
            action: op === "apply.commit" ? "applied" : "rejected",
            message: message,
            changes: result.changes || [],
            created: result.created || {},
            linked: result.linked || {},
            marker: result.marker || null
          });
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
          { className: "fd-scene-head" },
          props.sceneId ? null : h("div", { className: "fd-kicker" }, "FastDiscovery"),
          // The scene, by name and by address. A new tab, because the review is the
          // thing being read: opening the scene in place would lose it.
          h(
            "h2",
            null,
            h(
              "a",
              {
                href: "/scenes/" + data.scene.id,
                target: "_blank",
                rel: "noopener noreferrer",
                title: "Open this scene in a new tab"
              },
              data.scene.title || data.scene.filename
            )
          ),
          data.scene.path
            ? h("div", { className: "fd-scene-path", title: data.scene.path },
                data.scene.path)
            : null,
          h(
            "div",
            { className: "fd-muted" },
            h(StatusPill, { status: data.run.status }),
            " · ",
            data.summary.columns + " column(s) from " + data.summary.sources + " source(s)",
            data.summary.failed_sources
              ? " · " + data.summary.failed_sources + " failed"
              : "",
            " · " + data.summary.urls + " URL(s)"
          )
        ),
        props.sceneId
          ? null
          : h(
              "div",
              { className: "fd-actions" },
              h(Router.NavLink, { className: "btn btn-link", to: BASE }, "All runs")
            )
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
            onToggle: toggle,
            busy: busy[0] === "reject",
            onReject: data.run.reviewable ? rejectColumn : null
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
    function tally(entities) {
      return Object.keys(entities || {})
        .map(function (kind) { return entities[kind].length + " " + kind + "(s)"; })
        .join(", ");
    }
    var createdLine = tally(outcome.created);
    // Ticked as new, but the library turned out to have it already - by the time Apply
    // ran, another scene's review had created it. Worth saying, so "created 3" here and
    // "created 2" on a similar scene is not a mystery.
    var linkedLine = tally(outcome.linked);
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
                (createdLine ? ". Created: " + createdLine : "") +
                (linkedLine ? ". Already existed, linked: " + linkedLine : "") + "." +
                (outcome.marker ? " Tagged " + outcome.marker.name + "." : "")
            )
          : null,
        h(
          "div",
          { className: "fd-actions" },
          props.showBack
            ? h(Router.NavLink, { className: "btn btn-primary", to: BASE },
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
    // The run currently being decided from this list, so its two buttons cannot be
    // pressed twice while the call is out.
    var deciding = React.useState(null);
    var toaster = useToaster();

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

    // Rejecting without opening the review. A run whose scene is plainly wrong -
    // nothing found, or one look at the source list is enough - does not need a table
    // read end to end before it can be thrown away.
    //
    // No confirmation: the whole point is that it is cheaper than opening the review,
    // and nothing is written to the scene either way. What it does destroy is the
    // stored results, which is what Reject means everywhere else in FastDiscovery.
    function rejectRun(run) {
      deciding[1](run.id);
      callOp("run.reject", { run_id: run.id }).then(
        function () {
          deciding[1](null);
          toaster.success("Results rejected. The scene was not touched.");
          // The list reloads through its own CHANGED_EVENT listener, which also
          // refreshes any scene tab open on the same run.
          announceChange();
        },
        function (failure) {
          deciding[1](null);
          toaster.failure(failure.message);
        }
      );
    }

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
      h(EntityKindSwitch, { active: "scene" }),
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
                          Router.NavLink,
                          {
                            className: "btn btn-sm btn-primary",
                            to: BASE + "/scene/" + run.scene_id
                          },
                          "Review"
                        )
                      : null,
                    run.reviewable
                      ? h(
                          "button",
                          {
                            className: "btn btn-sm btn-secondary",
                            disabled: deciding[0] === run.id,
                            title: "Throw these results away without opening them. " +
                              "The scene is not touched.",
                            onClick: function () { rejectRun(run); }
                          },
                          deciding[0] === run.id ? "Rejecting..." : "Reject"
                        )
                      : null,
                    !run.reviewable && !run.purged
                      ? h(
                          "button",
                          {
                            className: "btn btn-sm btn-secondary",
                            disabled: deciding[0] === run.id,
                            onClick: function () {
                              deciding[1](run.id);
                              callOp("run.delete", { run_id: run.id }).then(
                                function () { deciding[1](null); announceChange(); },
                                function (failure) {
                                  deciding[1](null);
                                  toaster.failure(failure.message);
                                }
                              );
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

  // Every stash-box is asked unconditionally, always first - never a setting to
  // pick (requirement: stash-boxes take priority, exactly the way scene discovery
  // always asks every one of them). This picklist is performer-*name-scraper*
  // choices only.
  function parsePerformerScraperList(value) {
    var text = String(value || "").trim();
    if (!text) return [];
    if (text.charAt(0) === "[") {
      try {
        var parsed = JSON.parse(text);
        if (Array.isArray(parsed)) {
          return parsed.map(function (one) { return String(one).trim(); })
            .filter(Boolean);
        }
      } catch (error) { /* fall through to the plain-text form below */ }
    }
    // Stash's own generic Settings -> Plugins panel edits any STRING setting as a
    // plain text box, and someone typing "StashDB, ThePornDB" there is not wrong -
    // nothing tells them it has to be JSON - so that has to parse too.
    return text.split(/[,;\n]+/).map(function (one) { return one.trim(); })
      .filter(Boolean);
  }

  function PerformerScraperMultiSelect(props) {
    // Never a hardcoded list (requirement 23): built from the performer-name
    // scrapers Stash actually has installed right now, fetched fresh every time
    // this page loads. A previously picked scraper that has since disappeared still
    // shows in the saved value until unticked, but is otherwise silently ignored at
    // run time (`performer_discovery.PerformerRunner.fast_scraper_ids`), so a stale
    // pick here cannot fail a run.
    var available = useOp("performer.scrapers", {});
    var scrapers = (available.data && available.data.scrapers) || [];
    var typed = parsePerformerScraperList(props.value);
    // A typed entry may be an id or a display name, in whatever case someone used -
    // resolved against what is actually installed the same tolerant way
    // `fast_scraper_ids` does, so a checkbox here agrees with what a run would use.
    var byKey = {};
    scrapers.forEach(function (entry) {
      byKey[entry.id] = entry.id;
      byKey[entry.id.toLowerCase()] = entry.id;
      byKey[entry.name.toLowerCase()] = entry.id;
    });
    var picked = [];
    typed.forEach(function (choice) {
      var real = byKey[choice] || byKey[choice.toLowerCase()];
      if (real && picked.indexOf(real) < 0) picked.push(real);
    });

    function toggle(id) {
      var next = picked.indexOf(id) >= 0
        ? picked.filter(function (one) { return one !== id; })
        : picked.concat([id]);
      props.onChange(JSON.stringify(next));
    }

    if (available.loading && !available.data) return h(Loading, { label: "Loading installed performer scrapers..." });
    if (available.error) return h(Problem, { error: available.error, onRetry: available.reload });
    if (!scrapers.length) {
      return h("div", { className: "fd-muted" },
                "No performer-name scrapers are installed. Install one under " +
                  "Settings -> Metadata Providers, then come back here.");
    }
    return h(
      "div",
      { className: "fd-scraper-picklist" },
      scrapers.map(function (entry) {
        var on = picked.indexOf(entry.id) >= 0;
        return h(
          "label",
          { key: entry.id, className: cx("fd-pick", on && "fd-pick-on") },
          h("input", { type: "checkbox", checked: on, onChange: function () { toggle(entry.id); } }),
          h("span", null, entry.name)
        );
      })
    );
  }

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
          var isScraperPicklist = entry.name === "performerFastScrapers";
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
              h("span", { className: "fd-setting-name" },
                isScraperPicklist ? "Fast performer scrapers" : entry.name)
            ),
            isScraperPicklist
              ? h(PerformerScraperMultiSelect, { value: value, onChange: change })
              : entry.type !== "BOOLEAN"
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
                      result.dead_end_runs_purged + " dead-end run(s), " +
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

  /* ============================================================== performers ===
   *
   * Everything below is the performer counterpart of the scene pages above. It
   * reuses every generic piece as-is - MergeTable, ValueCell, Thumbnail, ImagePicker,
   * ListEditor, RejectToggle, SourceList, StatusPill, Loading, Problem, useOp,
   * callOp, useToaster, ConfirmRescan - and adds only what a performer review
   * genuinely needs on top: Organize, Full discovery, and a header photo per column.
   * New pages rather than branches inside the scene ones, so a bug here cannot reach
   * a scene review and the other way around.
   */

  function EntityKindSwitch(props) {
    return h(
      "div",
      { className: "fd-kind-switch" },
      h(
        Router.NavLink,
        { className: cx("btn btn-sm", props.active === "scene" ? "btn-primary" : "btn-secondary"),
          to: BASE, exact: true },
        "Scenes"
      ),
      h(
        Router.NavLink,
        { className: cx("btn btn-sm", props.active === "performer" ? "btn-primary" : "btn-secondary"),
          to: BASE + "/performers" },
        "Performers"
      )
    );
  }

  var PERFORMER_PER_PAGE_KEY = "fastdiscovery.performerPerPage";

  function loadPerformerPerPage() {
    try {
      var stored = Number(window.localStorage.getItem(PERFORMER_PER_PAGE_KEY));
      return PER_PAGE_CHOICES.indexOf(stored) >= 0 ? stored : PER_PAGE_DEFAULT;
    } catch (error) {
      return PER_PAGE_DEFAULT;
    }
  }

  function savePerformerPerPage(value) {
    try {
      window.localStorage.setItem(PERFORMER_PER_PAGE_KEY, String(value));
    } catch (error) { /* not remembered; harmless */ }
  }

  // Starting a Fast run for one performer. Deliberately its own hook rather than a
  // shape squeezed into `useRunStarter`: that one always sends a list of scene ids,
  // and a performer run is always exactly one performer (requirement 2 - the button
  // starts discovery for *this* performer, nothing else).
  function usePerformerRunStarter(onStarted) {
    var busy = React.useState(false);
    var error = React.useState(null);
    var confirm = React.useState(null);

    function start(performerId, replace) {
      busy[1](true);
      error[1](null);
      return callOp("performer.discover", {
        performer_id: performerId,
        replace: !!replace,
        trigger: "ui"
      }).then(
        function (data) {
          busy[1](false);
          if (data.needs_confirmation) {
            confirm[1]({ performerId: performerId, blocked: data.blocked || [],
                        message: data.error });
            return null;
          }
          confirm[1](null);
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
      busy: busy[0], error: error[0], confirming: confirm[0], start: start,
      cancelConfirm: function () { confirm[1](null); },
      confirmReplace: function () {
        var pending = confirm[0];
        confirm[1](null);
        if (pending) return start(pending.performerId, true);
        return Promise.resolve(null);
      }
    };
  }

  /* ------------------------------------------------------------ performer runs */

  function PerformerRunsPage() {
    var tab = React.useState("ready");
    var page = React.useState(1);
    var perPage = React.useState(loadPerformerPerPage);
    var listing = useOp("performer.run_list",
                        { tab: tab[0], page: page[0], per_page: perPage[0] });
    var deciding = React.useState(null);
    var toaster = useToaster();

    React.useEffect(
      function () {
        window.addEventListener(CHANGED_EVENT, listing.reload);
        return function () { window.removeEventListener(CHANGED_EVENT, listing.reload); };
      },
      [listing.reload]
    );

    function rejectRun(run) {
      deciding[1](run.id);
      callOp("performer.reject", { run_id: run.id }).then(
        function () {
          deciding[1](null);
          toaster.success("Results rejected. The performer was not touched.");
          announceChange();
        },
        function (failure) { deciding[1](null); toaster.failure(failure.message); }
      );
    }

    function runFull(run) {
      deciding[1](run.id);
      callOp("performer.full", { run_id: run.id }).then(
        function () {
          deciding[1](null);
          toaster.success("Full performer discovery queued.");
          announceChange();
          listing.reload();
        },
        function (failure) { deciding[1](null); toaster.failure(failure.message); }
      );
    }

    function choosePerPage(value) {
      savePerformerPerPage(value);
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
          h("button", { className: "btn btn-secondary", onClick: listing.reload }, "Refresh"),
          h(Router.NavLink, { className: "btn btn-secondary", to: BASE + "/settings" }, "Settings")
        )
      ),
      h(EntityKindSwitch, { active: "performer" }),
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
                ["Performer", "Mode", "Status", "Sources", "URLs", "Results", "Started",
                 "Finished", ""].map(function (label, index) {
                  return h("th", { key: index }, label);
                })
              )
            ),
            h(
              "tbody",
              null,
              listing.data.runs.map(function (run) {
                var performer = run.performer || {};
                return h(
                  "tr",
                  { key: run.id },
                  h(
                    "td",
                    null,
                    h(
                      Router.NavLink,
                      { to: "/performers/" + run.scene_id },
                      performer.name || "performer " + run.scene_id
                    ),
                    performer.disambiguation
                      ? h("div", { className: "fd-muted" }, performer.disambiguation)
                      : null
                  ),
                  h("td", null, run.mode || "FAST"),
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
                          Router.NavLink,
                          { className: "btn btn-sm btn-primary",
                            to: BASE + "/performer/" + run.scene_id },
                          "Review"
                        )
                      : null,
                    run.reviewable && run.mode !== "FULL"
                      ? h(
                          "button",
                          {
                            className: "btn btn-sm btn-secondary",
                            disabled: deciding[0] === run.id,
                            title: "Try every installed performer scraper Fast did not use.",
                            onClick: function () { runFull(run); }
                          },
                          "Full"
                        )
                      : null,
                    run.reviewable
                      ? h(
                          "button",
                          {
                            className: "btn btn-sm btn-secondary",
                            disabled: deciding[0] === run.id,
                            title: "Discard this discovery result. The performer is not touched.",
                            onClick: function () { rejectRun(run); }
                          },
                          deciding[0] === run.id ? "..." : "Cancel"
                        )
                      : null,
                    !run.reviewable && !run.purged
                      ? h(
                          "button",
                          {
                            className: "btn btn-sm btn-secondary",
                            disabled: deciding[0] === run.id,
                            onClick: function () {
                              deciding[1](run.id);
                              callOp("performer.run_delete", { run_id: run.id }).then(
                                function () { deciding[1](null); announceChange(); },
                                function (failure) {
                                  deciding[1](null);
                                  toaster.failure(failure.message);
                                }
                              );
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
              { className: "btn btn-sm btn-secondary", disabled: page[0] <= 1,
                onClick: function () { page[1](page[0] - 1); } },
              "Previous"
            ),
            h(
              "span",
              null,
              " page " + page[0] + " of " + Math.ceil(listing.data.total / perPage[0]) + " "
            ),
            h(
              "button",
              { className: "btn btn-sm btn-secondary",
                disabled: page[0] * perPage[0] >= listing.data.total,
                onClick: function () { page[1](page[0] + 1); } },
              "Next"
            )
          )
        : null
    );
  }

  /* --------------------------------------------------------- performer review */

  function PerformerReviewPage(props) {
    var params = Router.useParams();
    var history = Router.useHistory ? Router.useHistory() : null;
    var performerId = props.performerId || (params && params.id);
    var review = useOp("performer.review_get", { performer_id: Number(performerId) });
    var selection = React.useState(null);
    var organize = React.useState(false);
    var busy = React.useState(null);
    var problem = React.useState(null);
    var decided = React.useState(null);
    var toaster = useToaster();
    var starter = usePerformerRunStarter(function () {
      toaster.success("Fast performer scraping queued. This page updates when it finishes.");
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

    React.useEffect(
      function () {
        if (!review.data || !selection[0]) return undefined;
        var runId = review.data.run.id;
        var payload = selection[0];
        var timer = setTimeout(function () {
          callOp("performer.review_save", { run_id: runId, selection: payload }).catch(function () {});
        }, 1500);
        return function () { clearTimeout(timer); };
      },
      [selection[0]]
    );

    // Poll while Full discovery is going, exactly like the scene tab's own panel -
    // the review stays open, showing whatever Fast already found, and refreshes once
    // Full has added to it.
    React.useEffect(
      function () {
        var run = review.data && review.data.run;
        if (!run || run.status !== "RUNNING") return undefined;
        var timer = setInterval(review.reload, 4000);
        return function () { clearInterval(timer); };
      },
      [review.data]
    );

    if (decided[0]) {
      return h(Decided, {
        outcome: decided[0],
        onRescan: function () { starter.start(Number(performerId), true); },
        showBack: !props.performerId,
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
          { className: "btn btn-primary", disabled: starter.busy,
            onClick: function () { starter.start(Number(performerId), false); } },
          "Run Fast Performer Discovery"
        ),
        h(ConfirmRescan, { confirming: starter.confirming, onCancel: starter.cancelConfirm,
                          onConfirm: starter.confirmReplace })
      );
    }
    if (!review.data || !selection[0]) return h(Loading, null);

    var data = review.data;
    var imageRow = data.rows.filter(function (one) { return one.kind === "image"; })[0];
    var thumbWidth = Number(data.image_preview_width) || 180;

    function pick(field, id) {
      var next = Object.assign({}, selection[0]);
      next[field] = next[field] === id ? null : id;
      selection[1](next);
    }

    function toggle(field, id) {
      var next = Object.assign({}, selection[0]);
      var list = (next[field] || []).slice();
      var at = list.indexOf(id);
      if (at >= 0) {
        list.splice(at, 1);
      } else {
        list.push(id);
      }
      next[field] = list;
      selection[1](next);
    }

    function rejectColumn(column, rejected) {
      busy[1]("reject");
      problem[1](null);
      callOp("performer.reject_column", {
        run_id: data.run.id, column_id: column.id, rejected: rejected
      }).then(
        function (fresh) {
          busy[1](null);
          review.replace(fresh);
          selection[1](JSON.parse(JSON.stringify(fresh.selection || {})));
          toaster.success(
            (rejected ? "Rejected " : "Restored ") + column.name +
              (rejected ? " - nothing it said is counted." : " - its values are back.")
          );
        },
        function (failure) { busy[1](null); problem[1](failure.message); toaster.failure(failure.message); }
      );
    }

    function runFull() {
      busy[1]("full");
      problem[1](null);
      callOp("performer.full", { run_id: data.run.id }).then(
        function () {
          busy[1](null);
          toaster.success("Full performer discovery queued. This page updates when it finishes.");
          review.reload();
        },
        function (failure) { busy[1](null); problem[1](failure.message); toaster.failure(failure.message); }
      );
    }

    function decide(op, extra) {
      busy[1](op);
      problem[1](null);
      callOp(op, Object.assign({ run_id: data.run.id }, extra || {})).then(
        function (result) {
          busy[1](null);
          if (op === "performer.apply_commit" && !result.applied) {
            toaster.success(result.reason || "Nothing needed writing.");
            return;
          }
          var message =
            op === "performer.apply_commit"
              ? "FastDiscovery applied " + (result.changes || []).length +
                " field(s) to this performer." +
                (result.organized ? " Marked Organized." : "")
              : "FastDiscovery results rejected. The performer was not touched.";
          toaster.success(message);
          announceChange();
          if (history && !props.performerId) {
            history.push(BASE + "/performers");
            return;
          }
          decided[1]({
            action: op === "performer.apply_commit" ? "applied" : "rejected",
            message: message,
            changes: result.changes || [],
            created: result.created || {},
            linked: result.linked || {},
            marker: result.organized ? { name: "Organized" } : null
          });
        },
        function (failure) { busy[1](null); problem[1](failure.message); toaster.failure(failure.message); }
      );
    }

    return h(
      "div",
      { className: "fd-page fd-review fd-performer-review" },
      h(
        "div",
        { className: "fd-review-head" },
        h(
          "div",
          { className: "fd-scene-head" },
          props.performerId ? null : h("div", { className: "fd-kicker" }, "FastDiscovery"),
          h(
            "h2",
            null,
            h(
              "a",
              { href: "/performers/" + data.performer.id, target: "_blank",
                rel: "noopener noreferrer", title: "Open this performer in a new tab" },
              data.performer.name
            )
          ),
          h(
            "div",
            { className: "fd-muted" },
            h(StatusPill, { status: data.run.status }),
            " · " + (data.run.mode || "FAST"),
            " · ",
            data.summary.columns + " column(s) from " + data.summary.sources + " source(s)",
            data.summary.failed_sources ? " · " + data.summary.failed_sources + " failed" : "",
            " · " + data.summary.urls + " URL(s)"
          )
        ),
        props.performerId
          ? null
          : h(
              "div",
              { className: "fd-actions" },
              h(Router.NavLink, { className: "btn btn-link", to: BASE + "/performers" }, "All performers")
            )
      ),
      h(Problem, { error: problem[0] || starter.error }),
      data.run.stop_reason
        ? h("div", { className: "fd-note" }, "Stopped early: " + data.run.stop_reason)
        : null,
      data.run.reviewable && data.run.mode !== "FULL"
        ? h(
            "div", { className: "fd-note" },
            "Fast discovery only used the scrapers picked in settings. ",
            h(
              "button",
              { className: "btn btn-sm btn-secondary", disabled: !!busy[0], onClick: runFull },
              busy[0] === "full" ? "Running Full discovery..." : "Run Full Discovery"
            )
          )
        : null,
      h(SourceList, { sources: data.sources }),
      data.rows.length
        ? h(MergeTable, {
            review: data,
            selection: selection[0],
            onPick: pick,
            onToggle: toggle,
            busy: busy[0] === "reject",
            onReject: data.run.reviewable ? rejectColumn : null,
            headerPreview: imageRow,
            thumbWidth: thumbWidth
          })
        : h("div", { className: "fd-empty" }, "Nothing was found for this performer."),
      h(UrlGraph, { graph: data.urls_graph }),
      h(
        "div",
        { className: "fd-decide" },
        h("div", { className: "fd-muted" }, "Nothing has been written to this performer yet."),
        h(
          "label",
          { className: "fd-organize" },
          h("input", {
            type: "checkbox",
            checked: !!organize[0],
            onChange: function (event) { organize[1](event.target.checked); }
          }),
          " Organize"
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
                decide("performer.apply_commit", {
                  selection: selection[0],
                  organize: organize[0],
                  expected_updated_at: data.performer.updated_at
                });
              }
            },
            busy[0] === "performer.apply_commit" ? "Applying..." : "Apply"
          ),
          h(
            "button",
            {
              className: "btn btn-secondary",
              disabled: !!busy[0] || !data.run.reviewable,
              onClick: function () { decide("performer.reject"); }
            },
            "Cancel"
          )
        )
      ),
      h(ConfirmRescan, { confirming: starter.confirming, onCancel: starter.cancelConfirm,
                        onConfirm: starter.confirmReplace })
    );
  }

  /* ----------------------------------------------------- performer page button */

  // The Fast Discovery trigger: a plain button reading the real performer id
  // straight from React props, exactly the way PerformerOrganized's own controls
  // do. Not a registered scraper - Stash gives a scraper_id-based performer scrape
  // no reliable id, and its own "Scrape with..." menu always opens a search dialog
  // first regardless of what the scraper declares, so a scraper entry point cannot
  // do this: queue the job and toast, no dialog at all.
  // The whole performer-page footprint, by design (requirement: nothing on the
  // performer's own page except this button and its status - history and the full
  // review live on the separate FastDiscovery Performers page/review route).
  // `showStatus` adds a small StatusPill next to the label, for the details-panel
  // placement where there is room for it; the card/compressed placements omit it
  // and rely on the label alone ("⚡", "⚡ ...", "⚡ 4").
  function FastDiscoveryPerformerButton(props) {
    var performer = props.performer;
    var toaster = useToaster();
    var history = Router.useHistory ? Router.useHistory() : null;
    var status = useOp("performer.status", { performer_id: Number(performer && performer.id) },
                       { skip: !performer || !performer.id });
    var starter = usePerformerRunStarter(function () {
      toaster.success("Fast performer scraping queued");
      status.reload();
    });

    // Poll only while something is actually running, so the button/status catches
    // up without the reviewer having to reopen the page.
    React.useEffect(
      function () {
        var run = status.data && status.data.run;
        if (!run || run.status !== "RUNNING") return undefined;
        var timer = setInterval(status.reload, 4000);
        return function () { clearInterval(timer); };
      },
      [status.data]
    );

    if (!performer || !performer.id) return null;

    var run = status.data && status.data.run;
    var label = "⚡ Fast Discovery";
    var onClick = function (event) {
      if (event && event.stopPropagation) event.stopPropagation();
      starter.start(Number(performer.id), false).then(function (result) {
        if (result === null && !starter.confirming) toaster.failure(starter.error);
      });
    };
    if (run && run.status === "RUNNING") {
      label = "⚡ ...";
      onClick = function (event) { if (event && event.stopPropagation) event.stopPropagation(); };
    } else if (run && run.reviewable) {
      label = "⚡ Review (" + run.result_count + ")";
      onClick = function (event) {
        if (event && event.stopPropagation) event.stopPropagation();
        if (history) history.push(BASE + "/performer/" + performer.id);
      };
    }

    return h(
      React.Fragment,
      null,
      h(
        "button",
        {
          className: cx("btn btn-sm fd-performer-btn", props.inline && "fd-performer-btn-inline"),
          disabled: starter.busy || (run && run.status === "RUNNING"),
          title: run && run.reviewable
            ? "FastDiscovery has results waiting - click to review"
            : "Queue Fast Performer Discovery for " + (performer.name || "this performer"),
          onClick: onClick
        },
        label
      ),
      props.showStatus && run ? h(StatusPill, { status: run.status }) : null,
      h(Problem, { error: starter.error }),
      h(ConfirmRescan, { confirming: starter.confirming, onCancel: starter.cancelConfirm,
                        onConfirm: starter.confirmReplace })
    );
  }

  /* -------------------------------------------------------------- registration */

  // One registered route, not five. `PluginApi.register.route` appends a plain
  // `<Route path={path} component={...} />` - no `exact`, and the component that
  // hosts every plugin's routes (`PluginRoutes`) renders its children as a bare
  // Fragment, not a `Switch`. So several registered paths that are all prefixes of
  // the same URL - `/fast-discovery` is a prefix of `/fast-discovery/performers` -
  // render *at once*, stacked, rather than the more specific one winning. Routing
  // ourselves with a real `Switch` inside one registered path is what actually
  // gives each page exclusivity.
  function FastDiscoveryRoot() {
    return h(
      Router.Switch,
      null,
      h(Router.Route, { exact: true, path: BASE, component: RunsPage }),
      h(Router.Route, { exact: true, path: BASE + "/settings", component: SettingsPage }),
      h(Router.Route, { exact: true, path: BASE + "/scene/:id", component: ReviewPage }),
      h(Router.Route, { exact: true, path: BASE + "/performers", component: PerformerRunsPage }),
      h(Router.Route, { exact: true, path: BASE + "/performer/:id",
                        component: PerformerReviewPage }),
      h(Router.Redirect, { to: BASE })
    );
  }

  api.register.route(BASE, FastDiscoveryRoot);

  // Built to match what Stash renders for Scenes, Performers and the rest, class for
  // class: a Nav.Link wrapper carrying the responsive column widths, and inside it a
  // minimal primary Button that is really a router link. Anything less and the entry
  // sits in the menu looking like it was bolted on afterwards.
  //
  // Not `exact`: the item stays lit while you are inside a review, which is a
  // FastDiscovery page like any other.
  api.patch.before("MainNavBar.MenuItems", function (props) {
    return [
      Object.assign({}, props, {
        children: h(
          React.Fragment,
          null,
          props.children,
          h(
            Nav.Link,
            {
              as: "div",
              eventKey: BASE,
              className: "col-4 col-sm-3 col-md-2 col-lg-auto"
            },
            h(
              Button,
              {
                as: Router.NavLink,
                to: BASE,
                variant: "primary",
                className: "minimal p-4 p-xl-2 d-flex d-xl-inline-block flex-column " +
                  "justify-content-between align-items-center",
                title: "FastDiscovery"
              },
              NAV_ICON && FontAwesome
                ? h(FontAwesome.FontAwesomeIcon, {
                    icon: NAV_ICON,
                    className: "fa-icon nav-menu-icon d-block d-xl-inline mb-2 mb-xl-0"
                  })
                : null,
              h("span", null, "FastDiscovery")
            )
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

  /* --------------------------------------------------- performer page patches */
  //
  // Performers do not have scene's `ScenePage.Tabs`/`TabContent`/`SceneListOperations`
  // patch points in this Stash version - `PerformerPage` and `PerformerList` are each
  // one `PatchComponent`-wrapped whole, with no sub-points to append a tab or a bulk
  // menu item to (checked against the v0.31.1 source: `Performer.tsx`/
  // `PerformerList.tsx`). So the performer entry point is the same kind of control
  // PerformerOrganized already uses successfully on these exact three points -
  // `after` patches that append to what the component rendered, reading the real
  // performer object straight from its props, never from a name.
  function attachAfter(name, where, build) {
    api.patch.after(name, function () {
      var props = arguments[0];
      var result = arguments[arguments.length - 1];
      var extra;
      try {
        extra = build(props);
      } catch (error) {
        console.warn("[FastDiscovery] could not build the performer control for " +
                     name + ": " + error);
        return result;
      }
      if (!extra) return result;
      return where === "before"
        ? h(React.Fragment, null, extra, result)
        : h(React.Fragment, null, result, extra);
    });
  }

  // A performer can arrive under more than one prop name depending on the
  // component - the same reason PerformerOrganized's own `patches.js` checks all
  // three - so this checks all three too rather than assuming `performer` alone.
  function performerOf(props) {
    return (props && (props.performer || props.item || props.object)) || null;
  }

  attachAfter("PerformerDetailsPanel", "after", function (props) {
    // Deliberately just the button and its status - not the review, not history:
    // those live on the FastDiscovery Performers page and its review route, one
    // click away once there is something to review.
    var performer = performerOf(props);
    return performer ? h("div", { key: "fd-performer-panel", className: "fd-panel" },
      h(FastDiscoveryPerformerButton, { performer: performer, showStatus: true })) : null;
  });

  attachAfter("CompressedPerformerDetailsPanel", "after", function (props) {
    var performer = performerOf(props);
    return performer ? h(FastDiscoveryPerformerButton,
                        { key: "fd-performer-compressed", performer: performer,
                          inline: true }) : null;
  });

  attachAfter("PerformerCard.Overlays", "after", function (props) {
    var performer = performerOf(props);
    return performer ? h(FastDiscoveryPerformerButton,
                        { key: "fd-performer-card", performer: performer }) : null;
  });

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
