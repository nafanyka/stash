/*
 * PerformerOrganized - reading and writing the Performers list filter.
 *
 * The list's filter lives in the page's query string: one `c=` parameter per criterion,
 * and Stash rebuilds its ListFilterModel from them on every navigation. Editing that is
 * how the quick control filters the *standard* list rather than a list of its own -
 * what comes back is the ordinary Performers page, filtered by the server, with the
 * criterion visible in Filters and saveable as a preset like any other.
 *
 * ---------------------------------------------------------------------------------
 * Why the query string and not the ListFilterModel
 *
 * The model has methods for exactly this - `clone`, `makeCriterion`, `replaceCriteria`,
 * `makeQueryParameters` - and going through them would be tidier. It also requires the
 * patched component to hand its filter over, and requires those methods to exist under
 * those names in the Stash the user is running. Neither is something this plugin can
 * check, and when either is untrue the control renders and silently does nothing, which
 * is the worst of the available failures.
 *
 * The query string is the interface both ends already agree on: Stash writes it when
 * you use the filter builder and reads it back on navigation. It cannot be absent, and
 * a criterion put there is indistinguishable from one the user set by hand.
 *
 * The encoding below mirrors `ListFilterModel.getEncodedParams` and `decodeParams`
 * (`ui/v2.5/src/models/list-filter/filter.ts`): JSON with braces swapped for
 * parentheses outside strings, URI-encoded, with the characters that would end the
 * parameter escaped by hand.
 */
(function () {
  "use strict";

  var PO = window.PerformerOrganized;
  if (!PO) return;

  var FIELD = PO.FIELD;
  var CRITERION = PO.CRITERION;

  /* Braces are swapped for parentheses so a criterion reads as one query parameter
   * rather than something a proxy or a mail client feels entitled to mangle. Only
   * outside strings: a brace inside a scraped name is data.
   */
  function translateJSON(text, decoding) {
    var inString = false;
    var escaped = false;
    var out = "";
    for (var i = 0; i < text.length; i += 1) {
      var c = text.charAt(i);
      if (escaped) {
        escaped = false;
        out += c;
        continue;
      }
      if (c === "\\") {
        if (inString) escaped = true;
      } else if (c === '"') {
        inString = !inString;
      } else if (!inString) {
        if (decoding && c === "(") c = "{";
        else if (decoding && c === ")") c = "}";
        else if (!decoding && c === "{") c = "(";
        else if (!decoding && c === "}") c = ")";
      }
      out += c;
    }
    return out;
  }

  // Every character that would otherwise end the parameter or change its meaning.
  var RESERVED = [
    [/\?/g, "%3F"], [/#/g, "%23"], [/&/g, "%26"],
    [/;/g, "%3B"], [/=/g, "%3D"], [/\+/g, "%2B"],
  ];

  /* Escape a value so it survives being one `c=` parameter among several. */
  function escapeParam(text) {
    var out = encodeURI(text);
    RESERVED.forEach(function (pair) { out = out.replace(pair[0], pair[1]); });
    return out;
  }

  function encodeCriterion(criterion) {
    return escapeParam(translateJSON(JSON.stringify(criterion), false));
  }

  /* The reverse. `value` has already been percent-decoded by whoever read it out of
   * the query string, which is what Stash's own decode step assumes too.
   *
   * A criterion this cannot parse is returned as null and left alone by the caller:
   * a filter written by a newer Stash than this plugin knows about must survive being
   * carried across, not be dropped because it could not be understood.
   */
  function decodeCriterion(value) {
    try {
      var parsed = JSON.parse(translateJSON(value, true));
      return parsed && typeof parsed === "object" ? parsed : null;
    } catch (error) {
      return null;
    }
  }

  function criteriaIn(search) {
    var params = new URLSearchParams(search || "");
    var out = [];
    params.getAll("c").forEach(function (raw) {
      var criterion = decodeCriterion(raw);
      // Kept verbatim when it will not parse - a criterion written by a newer Stash
      // than this plugin understands has to be carried across, not dropped because it
      // could not be read.
      out.push(criterion || { raw: raw });
    });
    return out;
  }

  function organizedEntries(criteria) {
    var out = [];
    criteria.forEach(function (criterion) {
      if (criterion.type !== CRITERION) return;
      (criterion.value || []).forEach(function (entry) { out.push(entry); });
    });
    return out;
  }

  /* Which organized state the list is currently asking for: true, false, or null for
   * "not filtering on it". Read from the URL, so the control agrees with the filter
   * builder in both directions.
   */
  function readState(search) {
    var entries = organizedEntries(criteriaIn(search));
    for (var i = 0; i < entries.length; i += 1) {
      if (entries[i].field !== FIELD) continue;
      if (entries[i].modifier === "NOT_NULL") return true;
      if (entries[i].modifier === "IS_NULL") return false;
    }
    return null;
  }

  /* The same query string, asking for a different organized state.
   *
   * `NOT_NULL` / `IS_NULL` rather than comparing to true: `pkg/sqlite/custom_fields.go`
   * joins those with a LEFT JOIN, so "not organized" includes every performer with no
   * custom fields at all - nearly all of them on a fresh library. `EQUALS` uses an
   * inner join and would answer "none" to the question that matters most.
   */
  function searchWith(search, state) {
    var criteria = criteriaIn(search);

    // Everything that is not this plugin's entry survives untouched: other custom
    // field criteria, and every other criterion in the filter.
    var mine = [];
    if (state !== null) {
      mine.push({ field: FIELD, modifier: state ? "NOT_NULL" : "IS_NULL", value: [] });
    }
    var kept = [];
    criteria.forEach(function (criterion) {
      if (criterion.type !== CRITERION) {
        kept.push(criterion);
        return;
      }
      var others = (criterion.value || []).filter(function (entry) {
        return entry.field !== FIELD;
      });
      mine = others.concat(mine);
    });
    if (mine.length) {
      kept.push({ type: CRITERION, value: mine });
    }

    var params = new URLSearchParams(search || "");
    var out = [];
    params.forEach(function (value, key) {
      // `c` is rebuilt below; `p` is the page number, and a filter change that keeps it
      // lands the user on page 4 of a two-page result.
      if (key === "c" || key === "p") return;
      out.push(key + "=" + encodeURIComponent(value));
    });
    kept.forEach(function (criterion) {
      // `raw` was read out of the query string and is therefore already decoded;
      // putting it back needs the same escaping every other value gets, or a criterion
      // carrying a space or an ampersand would corrupt the URL it is carried through.
      out.push("c=" + (criterion.raw !== undefined
        ? escapeParam(criterion.raw)
        : encodeCriterion(criterion)));
    });
    return out.length ? "?" + out.join("&") : "";
  }

  PO.filter = {
    translateJSON: translateJSON,
    escapeParam: escapeParam,
    encodeCriterion: encodeCriterion,
    decodeCriterion: decodeCriterion,
    readState: readState,
    searchWith: searchWith,
  };

  // Node runs this file directly to test the encoding against fixtures; a browser has
  // no `module`.
  if (typeof module === "object" && module.exports) {
    module.exports = PO.filter;
  }
})();
