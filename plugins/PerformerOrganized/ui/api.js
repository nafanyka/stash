/*
 * PerformerOrganized - the storage layer and the public API.
 *
 * Everything that talks to Stash lives here. The components below never write a query
 * of their own, so there is exactly one place that knows how the flag is stored and
 * exactly one place to change if Stash ever grows a real `organized` column for
 * performers.
 *
 * ---------------------------------------------------------------------------------
 * Where the flag lives, and why
 *
 * `Performer.custom_fields` is a real field of the Stash schema (`Map!`), writable
 * through `PerformerUpdateInput.custom_fields` and `BulkPerformerUpdateInput.
 * custom_fields`, and - the part that decides the whole design - *filterable*:
 * `PerformerFilterType.custom_fields: [CustomFieldCriterionInput!]`. So the filter is
 * a real SQL query against the performers table, not a list loaded into the browser
 * and sieved there, and it stays fast on a library of any size.
 *
 * That rules out the service-tag fallback. A tag would have worked, but it would have
 * put a fake tag in the user's tag list, in every performer's tag editor, and in every
 * tag-based count; the custom field is invisible everywhere except the one place it is
 * meant to be read.
 *
 * ---------------------------------------------------------------------------------
 * Writing without touching anything else
 *
 * `CustomFieldsInput` has three arms: `full` replaces the whole map, `partial` updates
 * only the keys given, `remove` deletes only the keys listed. This plugin uses
 * `partial` and `remove` and never `full`, so a performer's other custom fields - and
 * every ordinary field: name, aliases, tags, urls, images, measurements, stash ids,
 * rating, favourite - are not in the mutation at all and cannot be disturbed by it.
 */
(function () {
  "use strict";

  var api = window.PluginApi;
  if (!api) {
    console.error("[PerformerOrganized] PluginApi is not available; not loaded");
    return;
  }
  if (window.PerformerOrganized) {
    // Loaded twice - a second <script> tag, or a hot reload. The first one won.
    console.warn("[PerformerOrganized] already loaded; ignoring the second copy");
    return;
  }

  var PLUGIN = "PerformerOrganized";

  // The custom field key. Deliberately the same word Stash uses for the equivalent
  // flag on scenes, so a user who opens Custom Fields on a performer reads something
  // they already understand rather than a plugin's private codename.
  var FIELD = "organized";

  var CRITERION = "custom_fields";

  function log(message) {
    console.log("[" + PLUGIN + "] " + message);
  }

  function warn(message) {
    console.warn("[" + PLUGIN + "] " + message);
  }

  /* ------------------------------------------------------------------ reading */

  /* Is this performer organized?
   *
   * Deliberately generous about the value. The field is a plain custom field, so a
   * user can also set it by hand in the Custom Fields editor, where they will type
   * "true" or "yes" rather than a JSON boolean. Anything that plainly means yes counts;
   * anything else, including the key being absent, means no.
   */
  function isOrganizedValue(value) {
    if (value === true || value === 1) return true;
    if (typeof value !== "string") return false;
    var text = value.trim().toLowerCase();
    return text === "true" || text === "yes" || text === "1" || text === "on";
  }

  function readPerformer(performer) {
    var fields = performer && performer.custom_fields;
    return !!fields && isOrganizedValue(fields[FIELD]);
  }

  /* ------------------------------------------------------------------ writing */

  // The app's own Apollo client, captured the first time a component renders. Using it
  // rather than a bare fetch is what makes the UI update by itself: the mutation asks
  // for `custom_fields` back, Apollo normalises the answer into the cache under that
  // performer's id, and every card and panel showing that performer re-renders. There
  // is no manual reload and no event bus.
  var client = null;

  function attach(apolloClient) {
    if (apolloClient && client !== apolloClient) {
      client = apolloClient;
    }
    return client;
  }

  var Apollo = api.libraries && api.libraries.Apollo;
  var gql = Apollo && Apollo.gql;

  var UPDATE_ONE = gql && gql(
    "mutation PerformerOrganizedSet($id: ID!, $fields: CustomFieldsInput!) {" +
    "  performerUpdate(input: { id: $id, custom_fields: $fields }) {" +
    "    id custom_fields" +
    "  }" +
    "}");

  var UPDATE_MANY = gql && gql(
    "mutation PerformerOrganizedSetMany($ids: [ID!], $fields: CustomFieldsInput!) {" +
    "  bulkPerformerUpdate(input: { ids: $ids, custom_fields: $fields }) {" +
    "    id custom_fields" +
    "  }" +
    "}");

  /* What goes in `custom_fields` for each direction.
   *
   * Organized sets the key; not organized *removes* it rather than writing false. Two
   * reasons: a performer who was never touched and one that was turned off then read
   * the same way, so the filter cannot disagree with itself; and the field does not
   * linger in the Custom Fields editor of every performer that is simply not organized.
   */
  function payload(organized) {
    if (organized) {
      var partial = {};
      partial[FIELD] = true;
      return { partial: partial };
    }
    return { remove: [FIELD] };
  }

  function requireClient() {
    if (!client) {
      throw new Error("no Apollo client yet - open a performer page or list first");
    }
    if (!UPDATE_ONE || !UPDATE_MANY) {
      throw new Error("PluginApi.libraries.Apollo is missing gql");
    }
    return client;
  }

  function setPerformerOrganized(id, organized) {
    var apollo = requireClient();
    log((organized ? "organizing" : "unorganizing") + " performer " + id);
    return apollo
      .mutate({
        mutation: UPDATE_ONE,
        variables: { id: String(id), fields: payload(organized) },
      })
      .then(function (response) {
        return readPerformer((response.data || {}).performerUpdate);
      });
  }

  /* The same thing for a selection, in one mutation.
   *
   * `bulkPerformerUpdate` takes the same `CustomFieldsInput`, so a group change is one
   * round trip and one transaction rather than a loop that can half-succeed.
   *
   * This one also refetches the performer list. A single toggle does not need it - the
   * cache update is enough to redraw the icon - but a group change is usually made
   * *while filtering by* organized, and there the honest result is the rows that no
   * longer match leaving the list.
   */
  function setPerformersOrganized(ids, organized) {
    var apollo = requireClient();
    var list = (ids || []).map(String);
    if (!list.length) return Promise.resolve([]);
    log((organized ? "organizing" : "unorganizing") + " " + list.length + " performer(s)");
    return apollo
      .mutate({
        mutation: UPDATE_MANY,
        variables: { ids: list, fields: payload(organized) },
        refetchQueries: ["FindPerformers"],
      })
      .then(function (response) {
        return (response.data || {}).bulkPerformerUpdate || [];
      });
  }

  /* Whether one performer is organized, asked of the server.
   *
   * The components do not use this - they already hold the performer object, and
   * `custom_fields` is part of the PerformerData fragment Stash fetches for both cards
   * and pages, so asking again would be a round trip for something already in hand.
   * It is here for other plugins, which may only have an id.
   */
  function isPerformerOrganized(id) {
    var apollo = requireClient();
    return apollo
      .query({
        query: gql("query PerformerOrganizedRead($id: ID!) {" +
                   "  findPerformer(id: $id) { id custom_fields }" +
                   "}"),
        variables: { id: String(id) },
        fetchPolicy: "network-only",
      })
      .then(function (response) {
        return readPerformer((response.data || {}).findPerformer);
      });
  }

  /* --------------------------------------------------------------------- api */

  window.PerformerOrganized = {
    PLUGIN: PLUGIN,
    FIELD: FIELD,
    CRITERION: CRITERION,

    log: log,
    warn: warn,

    // Storage
    attach: attach,
    isOrganized: readPerformer,          // from a performer object already in hand
    isPerformerOrganized: isPerformerOrganized,
    setPerformerOrganized: setPerformerOrganized,
    setPerformersOrganized: setPerformersOrganized,

    // Filtering is added by filter.js, which loads next: PO.filter.{readState,
    // searchWith}. It works on the page's query string rather than on a
    // ListFilterModel, because the query string is the one interface both ends
    // already agree on - see the comment at the top of that file.
  };

  log("storage layer ready; flag lives in custom_fields." + FIELD);
})();
