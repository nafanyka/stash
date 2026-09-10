/*
 * PerformerOrganized - where the controls attach to Stash.
 *
 * Four patch points, all of them named registration points Stash publishes for plugins
 * (`ui/v2.5/src/docs/en/Manual/UIPluginApi.md`). Nothing here reaches into Stash's
 * internals, queries the DOM, or replaces a component: each patch takes what the
 * component rendered and returns it with one more thing beside it.
 *
 * ---------------------------------------------------------------------------------
 * Reading an `after` patch's arguments
 *
 * Stash invokes them as `afterFn.apply(ctx, args.concat(result))` - the component's own
 * arguments, with its rendered output appended. `args` is what React passed the
 * component, and React does not call a function component with one argument: it calls
 * `Component(props, context)`, where a component with no legacy context gets `{}`. So a
 * handler written `function (props, result)` is really being handed the empty context
 * object, and returning it as a child is React error #31, "Objects are not valid as a
 * React child (found: object with keys {})".
 *
 * The result is whatever came last, and only that is guaranteed. Hence `attach` below,
 * which every patch here goes through so the mistake cannot be made twice.
 */
(function () {
  "use strict";

  var api = window.PluginApi;
  var PO = window.PerformerOrganized;
  if (!api || !PO || !PO.components) return;

  var React = api.React;
  var h = React.createElement;
  var parts = PO.components;

  /* Add one element beside what a component rendered.
   *
   * `where` is "after" to append and "before" to put the addition first. `build(props)`
   * returns the element, or null to leave the component exactly as it was.
   */
  function attach(name, where, build) {
    api.patch.after(name, function () {
      var props = arguments[0];
      var result = arguments[arguments.length - 1];
      var extra;
      try {
        extra = build(props);
      } catch (error) {
        // A control that throws must not take the page down with it: the worst this
        // plugin may cost is its own icon.
        PO.warn("could not build the control for " + name + ": " + error);
        return result;
      }
      if (!extra) return result;
      return where === "before"
        ? h(React.Fragment, null, extra, result)
        : h(React.Fragment, null, result, extra);
    });
  }

  /* A performer can arrive under more than one prop name depending on the component,
   * and a patch that assumed one would silently render nothing on the others.
   *
   * When none of them holds one, the patch bows out and leaves the component exactly
   * as it was - but says so once, because "the icon is missing" with a silent log is a
   * bug report nobody can act on. Once, not per render: this runs on every card.
   */
  var complained = {};

  function performerOf(props, where) {
    var performer = props && (props.performer || props.item || props.object);
    if (performer) return performer;
    if (!complained[where]) {
      complained[where] = true;
      PO.warn(where + " did not pass a performer; its control is not shown");
    }
    return null;
  }

  /* ------------------------------------------------------- the performer page */

  attach("PerformerDetailsPanel", "after", function (props) {
    var performer = performerOf(props, "PerformerDetailsPanel");
    return performer
      ? h(parts.OrganizedRow, { key: "po-row", performer: performer })
      : null;
  });

  /* The narrow variant of the same panel, shown when the page is scrolled and the
   * details collapse into a strip. Patching only the full one would make the control
   * disappear halfway down the page. */
  attach("CompressedPerformerDetailsPanel", "after", function (props) {
    var performer = performerOf(props, "CompressedPerformerDetailsPanel");
    return performer
      ? h(parts.CardBadge, { key: "po-compressed", performer: performer, inline: true })
      : null;
  });

  /* ------------------------------------------------------- the performer card */

  // The overlay layer of a card: the corner where Stash puts its own badges, so the
  // icon lands where a user already looks for card state instead of on top of the
  // image (requirements 9, 23).
  attach("PerformerCard.Overlays", "after", function (props) {
    var performer = performerOf(props, "PerformerCard.Overlays");
    return performer
      ? h(parts.CardBadge, { key: "po-badge", performer: performer })
      : null;
  });

  /* --------------------------------------------------------------- the list */

  // `PerformerList` is handed `{ performers, filter, selectedIds, onSelectChange }`,
  // which is exactly the quick filter's input and the bulk action's input. The strip
  // goes *before* the rendered grid: a bar under forty cards is a bar nobody finds.
  attach("PerformerList", "before", function (props) {
    return h(parts.ListToolbar, {
      key: "po-toolbar",
      filter: props && props.filter,
      selectedIds: props && props.selectedIds,
    });
  });

  PO.log("UI attached: performer page, card, and list toolbar");
})();
