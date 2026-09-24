/*
 * MyMoover UI.
 *
 * Plain JS, classic <script> tag, same as every other plugin here - see
 * FastDiscovery/ui/fastdiscovery.js for the reasoning. Two backends are used:
 *
 *  - `runPluginOperation` for anything that reads Stash configuration/scenes through
 *    MyMoover's own Python (config, folder.create, analyze, move, scan) - one Python
 *    process per call, deliberate and synchronous (no job queue, no progress bar).
 *  - Stash's own `directory` GraphQL query, called directly with a plain fetch, for
 *    browsing the filesystem tree while picking a destination - it is read-only, it
 *    is the exact query Stash's own Settings -> Library folder picker uses, and
 *    round-tripping every keystroke of navigation through a spawned Python process
 *    would make browsing feel broken for no safety benefit (nothing destructive ever
 *    happens from a browse call).
 *
 * The button is attached to `SceneList` (ui/v2.5/src/components/Scenes/SceneList.tsx)
 * via `patch.after`, the same technique PerformerOrganized already uses in this repo
 * for `PerformerList`. This is not literally inside Stash's native selected-items
 * dropdown - that dropdown's action list is a local variable inside
 * `FilteredSceneList`, not something a plugin can patch - so instead a small bar
 * renders directly above the scene grid/list/wall, in every display mode, wired to
 * the exact same `selectedIds`/`onSelectChange` the native toolbar uses.
 */
(function () {
  "use strict";

  var api = window.PluginApi;
  if (!api) {
    console.error("[MyMoover] PluginApi is not available; UI not loaded");
    return;
  }
  if (window.MyMoover) {
    console.warn("[MyMoover] already loaded; ignoring the second copy");
    return;
  }

  var React = api.React;
  var h = React.createElement;
  var Bootstrap = api.libraries.Bootstrap;
  var Modal = Bootstrap.Modal;
  var Button = Bootstrap.Button;
  var Form = Bootstrap.Form;

  var PLUGIN_ID = "MyMoover";

  function log(message) { console.log("[MyMoover] " + message); }
  function warn(message) { console.warn("[MyMoover] " + message); }

  /* ------------------------------------------------------------------ backend */

  function graphql(query, variables) {
    return fetch("/graphql", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query: query, variables: variables || {} }),
    })
      .then(function (response) {
        if (!response.ok) throw new Error("Stash returned HTTP " + response.status);
        return response.json();
      })
      .then(function (payload) {
        if (payload.errors && payload.errors.length) {
          throw new Error(payload.errors[0].message);
        }
        return payload.data;
      });
  }

  function callOp(op, args) {
    var query =
      "mutation MyMooverOp($id: ID!, $args: Map) {" +
      " runPluginOperation(plugin_id: $id, args: $args) }";
    return graphql(query, { id: PLUGIN_ID, args: Object.assign({ op: op }, args || {}) })
      .then(function (data) {
        var result = data && data.runPluginOperation;
        if (!result) throw new Error("MyMoover returned nothing - is the plugin enabled?");
        if (result.ok === false) {
          var error = new Error(result.error || "the operation failed");
          throw error;
        }
        return result;
      });
  }

  // Exactly Stash's own folder-browse query (`Query.directory`), used by its Settings
  // -> Library path picker - see ui/v2.5/src/components/Shared/FolderSelect.
  function browseDirectory(path) {
    return graphql(
      "query MyMooverDirectory($path: String) { directory(path: $path)" +
      " { path parent directories } }",
      { path: path || "" }
    ).then(function (data) { return data.directory; });
  }

  function useToaster() {
    var toast = api.hooks && api.hooks.useToast ? api.hooks.useToast() : null;
    return {
      success: function (message) {
        if (toast && toast.success) toast.success(message);
        else log(message);
      },
      warning: function (message) {
        if (toast && toast.warning) toast.warning(message);
        else if (toast && toast.success) toast.success(message);
        else warn(message);
      },
      failure: function (message) {
        if (toast && toast.error) toast.error(message);
        else console.error("[MyMoover] " + message);
      },
    };
  }

  /* -------------------------------------------------------------- item helpers */

  function idsOf(selected) {
    if (!selected) return [];
    var list =
      typeof selected.forEach === "function" && typeof selected.size === "number"
        ? Array.from(selected)
        : Array.isArray(selected)
        ? selected
        : [];
    return list
      .map(function (entry) { return entry && typeof entry === "object" ? entry.id : entry; })
      .filter(Boolean)
      .map(String);
  }

  function setItemOverwrite(items, fileId, value) {
    return items.map(function (item) {
      return item.file_id === fileId ? Object.assign({}, item, { overwrite: value }) : item;
    });
  }

  function setSidecarOverwrite(items, fileId, basename, value) {
    return items.map(function (item) {
      if (item.file_id !== fileId) return item;
      return Object.assign({}, item, {
        sidecars: item.sidecars.map(function (sidecar) {
          return sidecar.basename === basename
            ? Object.assign({}, sidecar, { overwrite: value })
            : sidecar;
        }),
      });
    });
  }

  function setAllConflicts(items, value) {
    return items.map(function (item) {
      var next = item.status === "CONFLICT" ? Object.assign({}, item, { overwrite: value }) : item;
      if (!item.sidecars || !item.sidecars.length) return next;
      return Object.assign({}, next, {
        sidecars: item.sidecars.map(function (sidecar) {
          return sidecar.status === "CONFLICT"
            ? Object.assign({}, sidecar, { overwrite: value })
            : sidecar;
        }),
      });
    });
  }

  function countConflicts(items) {
    var count = 0;
    items.forEach(function (item) {
      if (item.status === "CONFLICT") count++;
      (item.sidecars || []).forEach(function (sidecar) {
        if (sidecar.status === "CONFLICT") count++;
      });
    });
    return count;
  }

  function totalSidecars(items) {
    return items.reduce(function (sum, item) { return sum + (item.sidecars || []).length; }, 0);
  }

  /* ------------------------------------------------------------- folder picker */

  function FolderBrowser(props) {
    var state = React.useState({ loading: true, error: null, dir: null });
    var value = state[0];
    var setValue = state[1];
    var path = props.path;

    React.useEffect(function () {
      var live = true;
      setValue(function (previous) { return Object.assign({}, previous, { loading: true }); });
      browseDirectory(path).then(
        function (dir) { if (live) setValue({ loading: false, error: null, dir: dir }); },
        function (error) { if (live) setValue({ loading: false, error: error.message, dir: null }); }
      );
      return function () { live = false; };
    }, [path]);

    var rows = [];
    if (value.dir && value.dir.parent !== null && value.dir.parent !== undefined) {
      rows.push(
        h(
          "button",
          {
            key: "up",
            type: "button",
            className: "list-group-item list-group-item-action mymoover-dir-row",
            onClick: function () { props.onNavigate(value.dir.parent); },
          },
          "↑ .."
        )
      );
    }
    (value.dir ? value.dir.directories : []).forEach(function (dirPath) {
      var name = dirPath.split(/[\\/]/).filter(Boolean).pop() || dirPath;
      rows.push(
        h(
          "button",
          {
            key: dirPath,
            type: "button",
            className: "list-group-item list-group-item-action mymoover-dir-row",
            onClick: function () { props.onNavigate(dirPath); },
          },
          "📁 " + name
        )
      );
    });

    return h(
      "div",
      { className: "mymoover-browser" },
      h("div", { className: "mymoover-current-path" }, path || "(default)"),
      value.loading
        ? h("div", { className: "mymoover-loading" }, "Loading…")
        : value.error
        ? h("div", { className: "mymoover-error" }, value.error)
        : h("div", { className: "list-group mymoover-dir-list" }, rows.length ? rows : h(
            "div", { className: "mymoover-empty" }, "No subfolders")),
      h(
        Button,
        {
          variant: "secondary",
          size: "sm",
          className: "mymoover-select-btn",
          disabled: !value.dir,
          onClick: function () { props.onSelect(value.dir.path); },
        },
        "Select this folder"
      )
    );
  }

  function NewFolderForm(props) {
    var state = React.useState("");
    var name = state[0];
    var setName = state[1];
    var busy = React.useState(false);
    var error = React.useState(null);

    function create() {
      if (!name.trim()) return;
      busy[1](true);
      error[1](null);
      callOp("folder.create", { parent_path: props.parentPath, name: name.trim() }).then(
        function (result) {
          busy[1](false);
          setName("");
          props.onCreated(result.path);
        },
        function (failure) {
          busy[1](false);
          error[1](failure.message);
        }
      );
    }

    return h(
      "div",
      { className: "mymoover-new-folder" },
      h(Form.Control, {
        type: "text",
        placeholder: "Folder name",
        value: name,
        disabled: busy[0],
        onChange: function (event) { setName(event.target.value); },
        onKeyDown: function (event) { if (event.key === "Enter") create(); },
      }),
      h(Button, { variant: "primary", size: "sm", disabled: busy[0] || !name.trim(), onClick: create },
        "+ New Folder"),
      error[0] ? h("div", { className: "mymoover-error" }, error[0]) : null
    );
  }

  /* ------------------------------------------------------------- conflict list */

  function StatusBadge(props) {
    var classes = {
      MOVE: "mymoover-badge mymoover-badge-move",
      ALREADY_THERE: "mymoover-badge mymoover-badge-already",
      CONFLICT: "mymoover-badge mymoover-badge-conflict",
      ERROR: "mymoover-badge mymoover-badge-error",
      MOVED: "mymoover-badge mymoover-badge-move",
      OVERWRITTEN: "mymoover-badge mymoover-badge-conflict",
      SKIPPED: "mymoover-badge mymoover-badge-already",
      STATE_CHANGED: "mymoover-badge mymoover-badge-error",
    };
    var labels = {
      MOVE: "Move", ALREADY_THERE: "Already there", CONFLICT: "Conflict", ERROR: "Error",
      MOVED: "Moved", OVERWRITTEN: "Overwritten", SKIPPED: "Skipped",
      STATE_CHANGED: "Changed since Analyze",
    };
    return h("span", { className: classes[props.status] || "mymoover-badge" },
      labels[props.status] || props.status);
  }

  function ConflictRow(props) {
    var conflict = props.item.conflict;
    return h(
      "div",
      { className: "mymoover-conflict-row" },
      h("div", { className: "mymoover-conflict-name" },
        props.item.basename, " ", h(StatusBadge, { status: props.item.status })),
      h("div", { className: "mymoover-conflict-paths" },
        h("div", null, "Source: ", h("code", null, props.item.source_path)),
        h("div", null, "Target: ", h("code", null, props.item.target_path)),
        conflict && !conflict.on_disk_only
          ? h("div", { className: "mymoover-conflict-owner" },
              "Target belongs to: Scene #" + conflict.owner_scene_id + " — " +
              conflict.owner_scene_title)
          : conflict
          ? h("div", { className: "mymoover-conflict-owner" }, "Target exists on filesystem, not registered in Stash")
          : null),
      h(Form.Check, {
        type: "checkbox",
        label: "Overwrite",
        checked: !!props.item.overwrite,
        onChange: function (event) { props.onToggle(event.target.checked); },
      })
    );
  }

  function ConflictGroup(props) {
    var item = props.item;
    var sceneTitle = (item.scene_titles && item.scene_titles[0]) || "Scene";
    var conflictSidecars = (item.sidecars || []).filter(function (s) { return s.status === "CONFLICT"; });
    if (item.status !== "CONFLICT" && !conflictSidecars.length) return null;

    return h(
      "div",
      { className: "mymoover-conflict-group" },
      h("div", { className: "mymoover-conflict-scene" }, sceneTitle),
      item.status === "CONFLICT"
        ? h(ConflictRow, {
            item: item,
            onToggle: function (value) { props.onToggleMedia(item.file_id, value); },
          })
        : null,
      conflictSidecars.map(function (sidecar) {
        return h(ConflictRow, {
          key: sidecar.basename,
          item: sidecar,
          onToggle: function (value) { props.onToggleSidecar(item.file_id, sidecar.basename, value); },
        });
      })
    );
  }

  /* ---------------------------------------------------------------- summary */

  function summaryCounts(items, key) {
    var media = {};
    var sidecars = {};
    items.forEach(function (item) {
      var value = item[key] || item.status;
      media[value] = (media[value] || 0) + 1;
      (item.sidecars || []).forEach(function (sidecar) {
        var sv = sidecar[key] || sidecar.status;
        sidecars[sv] = (sidecars[sv] || 0) + 1;
      });
    });
    return { media: media, sidecars: sidecars };
  }

  function SummaryLine(props) {
    var counts = props.counts;
    function line(counts_, order, noun) {
      return order
        .filter(function (key) { return counts_[key]; })
        .map(function (key) { return counts_[key] + " " + noun + " " + key.toLowerCase().replace(/_/g, " "); })
        .join(", ");
    }
    var order = props.resultView
      ? ["MOVED", "ALREADY_THERE", "OVERWRITTEN", "SKIPPED", "ERROR", "STATE_CHANGED"]
      : ["MOVE", "ALREADY_THERE", "CONFLICT", "ERROR"];
    return h(
      "div",
      { className: "mymoover-summary" },
      h("div", null, line(counts.media, order, "media")),
      h("div", null, line(counts.sidecars, order, "sidecar"))
    );
  }

  /* ------------------------------------------------------------------- modal */

  function MoveModal(props) {
    var toaster = useToaster();
    var step = React.useState("destination"); // destination | review | result
    var browsePath = React.useState(null);
    var destination = React.useState(null);
    var busy = React.useState(false);
    var error = React.useState(null);
    var analyzed = React.useState(null); // { scenes, items, counts, destination_folder }
    var moveResult = React.useState(null);
    var roots = React.useState([]);

    React.useEffect(function () {
      callOp("config", {}).then(
        function (result) { roots[1](result.roots || []); },
        function (failure) { warn("could not load library roots: " + failure.message); }
      );
    }, []);

    function analyze() {
      if (!destination[0]) return;
      busy[1](true);
      error[1](null);
      callOp("analyze", { scene_ids: props.sceneIds, destination_folder: destination[0] }).then(
        function (result) {
          busy[1](false);
          analyzed[1](result);
          step[1]("review");
        },
        function (failure) {
          busy[1](false);
          error[1](failure.message);
        }
      );
    }

    function move() {
      if (!analyzed[0]) return;
      busy[1](true);
      error[1](null);
      callOp("move", {
        scene_ids: props.sceneIds,
        destination_folder: analyzed[0].destination_folder,
        items: analyzed[0].items,
      }).then(
        function (result) {
          busy[1](false);
          moveResult[1](result);
          step[1]("result");
          var errorCount = result.errors ? result.errors.length : 0;
          var movedMedia = (result.counts.media.MOVED || 0) + (result.counts.media.OVERWRITTEN || 0);
          var movedSidecars = (result.counts.sidecars.MOVED || 0) + (result.counts.sidecars.OVERWRITTEN || 0);
          if (errorCount) {
            toaster.warning("Move completed with " + errorCount + " error" + (errorCount === 1 ? "" : "s"));
          } else {
            toaster.success("Moved " + (movedMedia + movedSidecars) + " files");
          }
          if (props.onMoved) props.onMoved();
        },
        function (failure) {
          busy[1](false);
          error[1](failure.message);
        }
      );
    }

    function scanAffected() {
      var dirs = (moveResult[0] && moveResult[0].affected_dirs) || [];
      if (!dirs.length) return;
      busy[1](true);
      callOp("scan", { paths: dirs }).then(
        function () { busy[1](false); toaster.success("Scan started for " + dirs.length + " folder(s)"); },
        function (failure) { busy[1](false); toaster.failure(failure.message); }
      );
    }

    var body;
    if (step[0] === "destination") {
      body = h(
        React.Fragment,
        null,
        destination[0]
          ? h("div", { className: "mymoover-selected" }, "Selected:", h("br"), h("code", null, destination[0]))
          : null,
        roots[0].length
          ? h(
              "div",
              { className: "mymoover-roots" },
              roots[0].map(function (root) {
                return h(
                  Button,
                  {
                    key: root, size: "sm", variant: "outline-secondary",
                    className: "mymoover-root-btn",
                    onClick: function () { browsePath[1](root); },
                  },
                  root
                );
              })
            )
          : null,
        h(FolderBrowser, {
          path: browsePath[0],
          onNavigate: function (path) { browsePath[1](path); },
          onSelect: function (path) { destination[1](path); },
        }),
        h(NewFolderForm, {
          parentPath: browsePath[0] || destination[0] || "",
          onCreated: function (path) {
            destination[1](path);
            browsePath[1](path);
          },
        })
      );
    } else if (step[0] === "review" && analyzed[0]) {
      var items = analyzed[0].items;
      var counts = summaryCounts(items, "status");
      body = h(
        React.Fragment,
        null,
        h("div", { className: "mymoover-counts-line" },
          props.sceneIds.length + " scenes, " + items.length + " media files, " +
          totalSidecars(items) + " sidecars"),
        h(SummaryLine, { counts: counts, resultView: false }),
        countConflicts(items) > 0
          ? h(
              "div",
              { className: "mymoover-conflicts" },
              h("h6", null, "Conflicts"),
              h(
                "div",
                { className: "mymoover-conflict-actions" },
                h(Button, {
                  size: "sm", variant: "outline-secondary",
                  onClick: function () { analyzed[1](Object.assign({}, analyzed[0], { items: setAllConflicts(items, true) })); },
                }, "Select all"),
                h(Button, {
                  size: "sm", variant: "outline-secondary",
                  onClick: function () { analyzed[1](Object.assign({}, analyzed[0], { items: setAllConflicts(items, false) })); },
                }, "Select none")
              ),
              items.map(function (item) {
                return h(ConflictGroup, {
                  key: item.file_id,
                  item: item,
                  onToggleMedia: function (fileId, value) {
                    analyzed[1](Object.assign({}, analyzed[0], { items: setItemOverwrite(items, fileId, value) }));
                  },
                  onToggleSidecar: function (fileId, basename, value) {
                    analyzed[1](Object.assign({}, analyzed[0], { items: setSidecarOverwrite(items, fileId, basename, value) }));
                  },
                });
              })
            )
          : null
      );
    } else if (step[0] === "result" && moveResult[0]) {
      var counts2 = summaryCounts(moveResult[0].items, "result");
      body = h(
        React.Fragment,
        null,
        h("h6", null, "Move completed"),
        h("div", null, "Scenes: " + moveResult[0].scenes),
        h(SummaryLine, { counts: counts2, resultView: true }),
        moveResult[0].errors && moveResult[0].errors.length
          ? h(
              "div",
              { className: "mymoover-errors" },
              h("h6", null, "Errors"),
              moveResult[0].errors.map(function (err, index) {
                return h("div", { key: index, className: "mymoover-error" }, err.path + ": " + err.message);
              })
            )
          : null,
        moveResult[0].affected_dirs && moveResult[0].affected_dirs.length
          ? h(
              "div",
              { className: "mymoover-scan-hint" },
              h("p", null, "Stash paths were updated automatically for every moved media file. ",
                "If any moved sidecar needs to be picked up, run a targeted scan of the affected folders."),
              h(Button, { size: "sm", variant: "outline-secondary", onClick: scanAffected, disabled: busy[0] },
                "Scan affected folders (" + moveResult[0].affected_dirs.length + ")")
            )
          : null
      );
    }

    return h(
      Modal,
      { show: true, onHide: props.onClose, className: "mymoover-modal", size: "lg" },
      h(Modal.Header, { closeButton: true },
        h(Modal.Title, null, "Move " + props.sceneIds.length + " scene" + (props.sceneIds.length === 1 ? "" : "s"))),
      h(Modal.Body, null,
        error[0] ? h("div", { className: "mymoover-error" }, error[0]) : null,
        body),
      h(
        Modal.Footer,
        null,
        step[0] === "result"
          ? h(Button, { variant: "primary", onClick: props.onClose }, "Close")
          : h(
              React.Fragment,
              null,
              h(Button, { variant: "secondary", onClick: props.onClose, disabled: busy[0] }, "Cancel"),
              step[0] === "destination"
                ? h(Button, { variant: "primary", disabled: !destination[0] || busy[0], onClick: analyze },
                    busy[0] ? "Analyzing…" : "Analyze")
                : h(Button, { variant: "primary", disabled: busy[0], onClick: move },
                    busy[0] ? "Moving…" : "Move")
            )
      )
    );
  }

  /* ---------------------------------------------------------------- toolbar */

  function MoveToolbar(props) {
    var ids = idsOf(props.selectedIds);
    var open = React.useState(false);

    if (!ids.length) return null;

    return h(
      React.Fragment,
      null,
      h(
        "div",
        { className: "mymoover-toolbar" },
        h(Button, { size: "sm", variant: "secondary", onClick: function () { open[1](true); } },
          "Move (" + ids.length + ")")
      ),
      open[0]
        ? h(MoveModal, {
            sceneIds: ids,
            onClose: function () { open[1](false); },
            onMoved: function () {
              if (props.onSelectChange) {
                ids.forEach(function (id) { props.onSelectChange(id, false, false); });
              }
            },
          })
        : null
    );
  }

  /* ------------------------------------------------------------------ patch */

  api.patch.after("SceneList", function () {
    var props = arguments[0];
    var result = arguments[arguments.length - 1];
    if (!props) return result;
    return h(
      React.Fragment,
      null,
      h(MoveToolbar, {
        key: "mymoover-toolbar",
        selectedIds: props.selectedIds,
        onSelectChange: props.onSelectChange,
      }),
      result
    );
  });

  window.MyMoover = { log: log, warn: warn };
  log("UI attached: Move toolbar above the Scenes list");
})();
