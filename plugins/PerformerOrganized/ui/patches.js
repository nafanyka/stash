/*
 * PerformerOrganized - where the controls attach to Stash.
 *
 * Three patch points, all of them named registration points Stash publishes for
 * plugins (`ui/v2.5/src/docs/en/Manual/UIPluginApi.md`). Nothing here reaches into
 * Stash's internals, queries the DOM, or replaces a component: each patch takes what
 * the component rendered and returns it with one more thing beside it.
 *
 * `after` is the appending form. Its contract is `(props..., result) -> result`: the
 * patch receives the component's own arguments with the rendered output appended, and
 * returns what should be rendered instead. Wrapping the original in a fragment adds to
 * it; another plugin patching the same component still sees the whole thing.
 */
(function () {
  "use strict";

  var api = window.PluginApi;
  var PO = window.PerformerOrganized;
  if (!api || !PO || !PO.components) return;

  var React = api.React;
  var h = React.createElement;
  var parts = PO.components;

  /* A performer can arrive under more than one prop name depending on the component,
   * and a patch that assumed one would silently render nothing on the others.
   *
   * When none of them holds one, the patch bows out and leaves the component exactly
   * as it was - but says so once, because "the icon is missing" with a silent log is
   * a bug report nobody can act on. Once, not per render: this runs on every card.
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

  api.patch.after("PerformerDetailsPanel", function (props, result) {
    var performer = performerOf(props, "PerformerDetailsPanel");
    if (!performer) return result;
    return h(
      React.Fragment,
      null,
      result,
      h(parts.OrganizedRow, { key: "po-row", performer: performer })
    );
  });

  /* The narrow variant of the same panel, shown when the page is scrolled and the
   * details collapse into a strip. Patching only the full one would make the switch
   * disappear halfway down the page. */
  api.patch.after("CompressedPerformerDetailsPanel", function (props, result) {
    var performer = performerOf(props, "CompressedPerformerDetailsPanel");
    if (!performer) return result;
    return h(
      React.Fragment,
      null,
      result,
      h(parts.CardBadge, { key: "po-compressed", performer: performer, inline: true })
    );
  });

  /* ------------------------------------------------------- the performer card */

  // The overlay layer of a card: the corner where Stash puts its own badges, so the
  // icon lands where a user already looks for card state instead of on top of the
  // image (requirements 9, 23).
  api.patch.after("PerformerCard.Overlays", function (props, result) {
    var performer = performerOf(props, "PerformerCard.Overlays");
    if (!performer) return result;
    return h(
      React.Fragment,
      null,
      result,
      h(parts.CardBadge, { key: "po-badge", performer: performer })
    );
  });

  /* --------------------------------------------------------------- the list */

  // `PerformerList` is handed `{ performers, filter, selectedIds, onSelectChange }`,
  // which is exactly the quick filter's input and the bulk action's input. The strip
  // goes *before* the rendered grid: a bar under forty cards is a bar nobody finds.
  api.patch.after("PerformerList", function (props, result) {
    return h(
      React.Fragment,
      null,
      h(parts.ListToolbar, {
        key: "po-toolbar",
        filter: props.filter,
        selectedIds: props.selectedIds,
      }),
      result
    );
  });

  PO.log("UI attached: performer page, card, and list toolbar");
})();
