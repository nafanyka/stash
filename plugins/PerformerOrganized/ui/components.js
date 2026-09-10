/*
 * PerformerOrganized - the controls.
 *
 * Three of them, and all three are the same idea in different sizes: read the flag off
 * the performer object the surrounding component already has, write it through the
 * service in api.js, and let Apollo's cache redraw whatever was showing it.
 *
 * None of these touch the DOM. They are React components handed to Stash's own patch
 * points, so a re-render or a route change cannot leave a second copy behind, and
 * there is nothing to clean up on navigation (requirements 17, 18, 19).
 */
(function () {
  "use strict";

  var api = window.PluginApi;
  var PO = window.PerformerOrganized;
  if (!api || !PO) return;

  var React = api.React;
  var h = React.createElement;
  var Bootstrap = api.libraries.Bootstrap;
  var Button = Bootstrap.Button;
  var Router = api.libraries.ReactRouterDOM;
  var FontAwesome = api.libraries.ReactFontAwesome;
  var Solid = api.libraries.FontAwesomeSolid || {};
  var Regular = api.libraries.FontAwesomeRegular || {};

  // Filled means organized, outline means not - the same glyph either way, so the two
  // states read as one control in two positions rather than as two different icons.
  var ICON_ON = Solid.faCircleCheck || Solid.faCheckCircle || Solid.faBox || null;
  var ICON_OFF = Regular.faCircleCheck || Regular.faCheckCircle || Solid.faBox || null;

  function icon(on) {
    var glyph = on ? ICON_ON : ICON_OFF;
    if (!glyph || !FontAwesome) return h("span", null, on ? "☑" : "☐");
    return h(FontAwesome.FontAwesomeIcon, { icon: glyph, className: "fa-icon" });
  }

  /* One toggle's worth of behaviour, shared by the page switch and the card icon.
   *
   * Optimistic, with a rollback: the control shows the new state immediately, and puts
   * it back if the server refuses. Waiting for the round trip instead would make every
   * click feel broken on a slow connection, and not rolling back would leave the page
   * claiming something the library does not agree with.
   */
  function useToggle(performer) {
    var client = api.libraries.Apollo.useApolloClient();
    var toast = api.hooks.useToast();
    var pending = React.useState(null);   // the state we are optimistically showing
    var busy = React.useState(false);

    PO.attach(client);

    var actual = PO.isOrganized(performer);
    var shown = pending[0] === null ? actual : pending[0];

    // The server's answer has caught up with what we were showing, so stop overriding
    // it - from here the performer object is the truth again.
    React.useEffect(function () {
      if (pending[0] !== null && pending[0] === actual) pending[1](null);
    }, [actual, pending[0]]);

    function toggle(event) {
      if (event) {
        // On a card the icon sits on top of a link to the performer. Toggling is not
        // navigating (requirement 9).
        event.preventDefault();
        event.stopPropagation();
      }
      if (busy[0] || !performer || !performer.id) return;
      var next = !shown;
      pending[1](next);
      busy[1](true);
      PO.setPerformerOrganized(performer.id, next).then(
        function () { busy[1](false); },
        function (error) {
          busy[1](false);
          pending[1](null);          // rolled back: the library never changed
          PO.warn("could not set organized on performer " + performer.id + ": " +
                  (error && error.message));
          if (toast && toast.error) toast.error(error);
        }
      );
    }

    return { organized: shown, busy: busy[0], toggle: toggle };
  }

  function label(on) {
    return on ? "Organized" : "Not organized";
  }

  /* ------------------------------------------------------- the performer page */

  /* A row under the performer's details, shaped like the rest of them: the name of the
   * thing on the left, its value on the right. The value happens to be a switch.
   */
  function OrganizedRow(props) {
    var state = useToggle(props.performer);
    return h(
      "div",
      { className: "po-row detail-item" },
      h("span", { className: "po-row-label detail-item-title" }, "Organized"),
      h(
        "span",
        { className: "po-row-value detail-item-value" },
        h(
          Button,
          {
            className: "po-switch minimal",
            variant: "secondary",
            disabled: state.busy,
            title: label(state.organized),
            "aria-pressed": state.organized,
            onClick: state.toggle,
          },
          icon(state.organized),
          h("span", { className: "po-switch-text" },
            state.organized ? "Yes" : "No")
        )
      )
    );
  }

  /* ------------------------------------------------------- the performer card */

  /* `inline` is the same control without the corner positioning, for the compressed
   * details strip - where an absolutely placed badge would land on top of whatever
   * happens to be behind it. */
  function CardBadge(props) {
    var state = useToggle(props.performer);
    return h(
      "button",
      {
        type: "button",
        className: "po-card-badge" +
          (props.inline ? " po-inline" : "") +
          (state.organized ? " po-on" : " po-off"),
        disabled: state.busy,
        title: label(state.organized),
        "aria-label": label(state.organized),
        "aria-pressed": state.organized,
        onClick: state.toggle,
      },
      icon(state.organized)
    );
  }

  /* --------------------------------------------------------- the list toolbar */

  /* Everything the performers list needs, in one strip above the grid: which organized
   * state the list is filtered by, and - only while something is selected - what to do
   * with the selection.
   *
   * The filter half does not run a query of its own. It rewrites the list's own filter
   * and navigates, so what comes back is the standard Performers list, filtered
   * server-side, with the criterion visible in Filters and saveable as a preset like
   * any other (requirements 5, 6, 15, 30).
   */
  function ListToolbar(props) {
    var client = api.libraries.Apollo.useApolloClient();
    var toast = api.hooks.useToast();
    var history = Router.useHistory ? Router.useHistory() : null;
    var busy = React.useState(false);
    PO.attach(client);

    var filter = props.filter;
    var selected = props.selectedIds;
    var count = selected && typeof selected.size === "number" ? selected.size : 0;
    var state = filter ? PO.filterState(filter) : null;

    function choose(next) {
      if (!filter || !history) return;
      history.push({ search: PO.searchFor(PO.withState(filter, next)) });
    }

    function apply(organized) {
      if (!count || busy[0]) return;
      busy[1](true);
      var ids = Array.from(selected);
      PO.setPerformersOrganized(ids, organized).then(
        function (updated) {
          busy[1](false);
          if (toast && toast.success) {
            toast.success((organized ? "Organized " : "Unorganized ") +
                          (updated.length || ids.length) + " performer(s)");
          }
        },
        function (error) {
          busy[1](false);
          PO.warn("bulk update failed: " + (error && error.message));
          if (toast && toast.error) toast.error(error);
        }
      );
    }

    function tab(value, text) {
      var active = state === value;
      return h(
        Button,
        {
          key: text,
          size: "sm",
          variant: active ? "primary" : "secondary",
          className: "po-tab",
          disabled: !filter || !history,
          onClick: function () { choose(value); },
        },
        text
      );
    }

    return h(
      "div",
      { className: "po-toolbar" },
      h(
        "div",
        { className: "po-quick" },
        h("span", { className: "po-quick-label" }, "Organized"),
        h(
          "div",
          { className: "btn-group", role: "group" },
          tab(null, "Any"),
          tab(true, "Yes"),
          tab(false, "No")
        )
      ),
      count
        ? h(
            "div",
            { className: "po-bulk" },
            h("span", { className: "po-bulk-count" }, count + " selected"),
            h(
              Button,
              {
                size: "sm",
                variant: "secondary",
                disabled: busy[0],
                onClick: function () { apply(true); },
              },
              icon(true),
              h("span", { className: "po-switch-text" }, "Set organized")
            ),
            h(
              Button,
              {
                size: "sm",
                variant: "secondary",
                disabled: busy[0],
                onClick: function () { apply(false); },
              },
              icon(false),
              h("span", { className: "po-switch-text" }, "Set unorganized")
            )
          )
        : null
    );
  }

  window.PerformerOrganized.components = {
    OrganizedRow: OrganizedRow,
    CardBadge: CardBadge,
    ListToolbar: ListToolbar,
  };
})();
