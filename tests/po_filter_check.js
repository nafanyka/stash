/*
 * Exercises the real ui/filter.js against fixtures and prints one JSON verdict.
 *
 * Run by tests/test_po_manifest.py through node. The encoding it checks is Stash's,
 * mirrored from `ListFilterModel.getEncodedParams` / `decodeParams`, and the whole
 * quick filter is worthless if it drifts: a criterion Stash cannot parse is a filter
 * that silently returns everything.
 */
"use strict";

const path = require("path");

// filter.js is a browser script: it reads a namespace off `window` and writes back to
// it. Standing in for the browser is cheaper than restructuring the file for tests.
global.window = { PerformerOrganized: { FIELD: "organized", CRITERION: "custom_fields" } };
const F = require(path.join(__dirname, "..", "plugins", "PerformerOrganized", "ui",
                            "filter.js"));

const checks = [];

function check(name, actual, expected) {
  checks.push({
    name: name,
    ok: JSON.stringify(actual) === JSON.stringify(expected),
    actual: actual,
    expected: expected,
  });
}

/* --------------------------------------------------------------- the encoding */

const criterion = {
  type: "custom_fields",
  value: [{ field: "organized", modifier: "NOT_NULL", value: [] }],
};
const encoded = F.encodeCriterion(criterion);

check("braces leave as parentheses",
      /^\("type":/.test(decodeURIComponent(encoded)), true);
check("nothing that would end the parameter survives",
      /[?#&;=+]/.test(encoded), false);
check("round trip", F.decodeCriterion(decodeURIComponent(encoded)), criterion);

// A brace inside a string is data, not structure, and must come back untouched.
const awkward = { type: "custom_fields", value: [{ field: "a{b}c", modifier: "EQUALS",
                                                   value: ["x&y=z+1 ?"] }] };
check("braces inside strings survive",
      F.decodeCriterion(decodeURIComponent(F.encodeCriterion(awkward))), awkward);

/* ----------------------------------------------------------------- the states */

check("no filter reads as any", F.readState(""), null);
check("yes reads back", F.readState(F.searchWith("", true)), true);
check("no reads back", F.readState(F.searchWith("", false)), false);
check("any clears it", F.readState(F.searchWith(F.searchWith("", true), null)), null);
check("setting it twice does not stack",
      F.searchWith(F.searchWith("", true), true), F.searchWith("", true));
check("switching replaces rather than adds",
      F.readState(F.searchWith(F.searchWith("", true), false)), false);

/* --------------------------------------------------- what must not be disturbed */

const withSort = "?sortby=name&sortdir=asc&perPage=40&q=ada";
const filtered = F.searchWith(withSort, true);
["sortby=name", "sortdir=asc", "perPage=40", "q=ada"].forEach(function (part) {
  check("kept " + part, filtered.indexOf(part) >= 0, true);
});
check("page is reset", F.searchWith("?p=7", true).indexOf("p=7"), -1);

// Another criterion on another custom field, and an unrelated criterion: both have to
// come out the other side.
const otherCustom = "?c=" + F.encodeCriterion({
  type: "custom_fields",
  value: [{ field: "note", modifier: "EQUALS", value: ["x"] }],
});
const both = F.searchWith(otherCustom, true);
const parsed = new (require("url").URLSearchParams)(both.replace(/^\?/, ""))
  .getAll("c").map(F.decodeCriterion);
check("one custom_fields criterion, both entries", parsed.length, 1);
check("the other custom field is still there",
      parsed[0].value.map(function (e) { return e.field; }).sort(),
      ["note", "organized"]);

const rating = "?c=" + F.encodeCriterion({ type: "rating100", value: { value: 60 },
                                           modifier: "GREATER_THAN" });
check("an unrelated criterion survives",
      F.readState(F.searchWith(rating, true)), true);
check("and is still in the query",
      F.searchWith(rating, true).indexOf("rating100") >= 0, true);

// A criterion this plugin cannot parse - written by a newer Stash - must be carried
// across byte for byte rather than dropped.
const opaque = "?c=" + F.escapeParam("!! not json !!");
const carried = F.searchWith(opaque, true);
check("an unparseable criterion is carried across",
      carried.indexOf(F.escapeParam("!! not json !!")) >= 0, true);
check("and is still escaped when it goes back out",
      / /.test(carried), false);

console.log(JSON.stringify(checks));
process.exit(checks.every(function (one) { return one.ok; }) ? 0 : 1);
