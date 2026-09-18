#!/usr/bin/env bash
#
# Rebuild dist/ after a change. Run this instead of calling the packager by hand.
#
#   bin/build.sh              build if anything changed
#   bin/build.sh --check      say what changed, build nothing, exit 1 if there is work
#   bin/build.sh --force      build even if nothing changed
#   bin/build.sh --no-tests   skip pytest (for a quick look; not for publishing)
#   bin/build.sh --help
#
# See bin/README.md for what it does and the one gotcha about version numbers.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
cd "$ROOT"

FORCE=0
RUN_TESTS=1
CHECK_ONLY=0

for arg in "$@"; do
  case "$arg" in
    --force)    FORCE=1 ;;
    --no-tests) RUN_TESTS=0 ;;
    --check)    CHECK_ONLY=1 ;;
    -h|--help)  sed -n '2,11p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $arg  (try --help)" >&2; exit 2 ;;
  esac
done

# Python is the packager's language and is required; everything else is optional and
# skipped with a word rather than a failure.
PYTHON="${PYTHON:-python}"
command -v "$PYTHON" >/dev/null 2>&1 || PYTHON=python3
command -v "$PYTHON" >/dev/null 2>&1 || { echo "python not found" >&2; exit 1; }

bold() { printf '\033[1m%s\033[0m\n' "$1"; }
dim()  { printf '\033[2m%s\033[0m\n' "$1"; }

# ---------------------------------------------------------------- what changed

bold "Scanning plugins and scrapers..."
REPORT="$("$PYTHON" "$HERE/_scan.py" report)"

count_of() { printf '%s\n' "$REPORT" | awk -v s="$1" -F'\t' '$1==s' | wc -l | tr -d ' '; }
list_of()  { printf '%s\n' "$REPORT" | awk -v s="$1" -F'\t' '$1==s {printf "    %s/%s  %s  %s\n", $2, $3, $4, $5}'; }

NEW_COUNT=$(count_of new)
VERSION_COUNT=$(count_of version)
CONTENT_COUNT=$(count_of content)
REMOVED_COUNT=$(count_of removed)
UNCHANGED_COUNT=$(count_of unchanged)

# Written as `if` rather than `test && { ... }`: under `set -e` a false test makes the
# whole list fail, and whether that ends the script has varied between bash versions.
if [ "$NEW_COUNT" -gt 0 ];     then echo "  new:";             list_of new;     fi
if [ "$VERSION_COUNT" -gt 0 ]; then echo "  version changed:"; list_of version; fi
if [ "$REMOVED_COUNT" -gt 0 ]; then echo "  removed:";         list_of removed; fi

# Files changed but the version did not. Not an error: the index version carries a git
# sha suffix, so the published version still moves once the change is committed. But the
# human-readable part will not, and someone comparing "1.3.0" with "1.3.0" in Stash's
# plugin list has no way to see that anything happened.
if [ "$CONTENT_COUNT" -gt 0 ]; then
  echo "  changed without a version bump:"
  list_of content
  dim "    (the -<sha> suffix will still move once committed, but the version you read"
  dim "     in Stash will not. Consider bumping it in the manifest.)"
fi

if [ "$UNCHANGED_COUNT" -gt 0 ]; then dim "  unchanged: $UNCHANGED_COUNT"; fi

WORK=$((NEW_COUNT + VERSION_COUNT + CONTENT_COUNT + REMOVED_COUNT))

if [ "$CHECK_ONLY" -eq 1 ]; then
  if [ "$WORK" -gt 0 ]; then
    echo
    bold "$WORK component(s) need building."
    exit 1
  fi
  echo
  bold "Everything is up to date."
  exit 0
fi

if [ "$WORK" -eq 0 ] && [ "$FORCE" -eq 0 ]; then
  echo; bold "Nothing changed. (bin/build.sh --force to rebuild anyway.)"
  exit 0
fi

# ------------------------------------------------------------------- the checks

if [ "$RUN_TESTS" -eq 1 ]; then
  echo
  bold "Running tests..."
  # Scoped to tests/ on purpose: a plugin folder can ship its own bundled test file
  # (a third-party plugin's own test_*.py sitting next to its source), and bare
  # `pytest -q` from the repo root discovers those too - with none of the
  # dependencies or fixtures they expect, so they fail the build for a reason that
  # has nothing to do with this repo's own suite.
  if ! "$PYTHON" -m pytest -q tests/; then
    echo
    echo "Tests failed - dist/ not touched." >&2
    exit 1
  fi
else
  dim "Skipping tests (--no-tests)."
fi

# Plugin UI JavaScript never reaches pytest, and a syntax error in it is invisible until
# the browser refuses to load the page. `node --check` parses without running.
if command -v node >/dev/null 2>&1; then
  echo
  bold "Checking plugin JavaScript..."
  JS_FILES=$(find plugins -name '*.js' -not -path '*/node_modules/*' 2>/dev/null || true)
  if [ -n "$JS_FILES" ]; then
    FAILED=0
    while IFS= read -r js; do
      if ! node --check "$js" 2>&1; then FAILED=1; fi
    done <<< "$JS_FILES"
    if [ "$FAILED" -eq 1 ]; then
      echo "JavaScript failed to parse - dist/ not touched." >&2
      exit 1
    fi
    dim "  $(printf '%s\n' "$JS_FILES" | wc -l | tr -d ' ') file(s) ok"
  fi
else
  dim "node not found - skipping the JavaScript syntax check."
fi

# ------------------------------------------------------------------- the build

echo
bold "Packaging..."
# The packager rebuilds every zip and both indexes, and that is on purpose: the index
# lists every component with the sha256 of its archive, so it cannot be written a piece
# at a time. Archives are reproducible - fixed timestamps - so a component that did not
# change comes out byte for byte identical and git sees no diff.
"$PYTHON" .github/workflows/build_index.py

"$PYTHON" "$HERE/_scan.py" commit

echo
bold "Done."
dim "  dist/ is rebuilt and bin/build-cache.json updated."
dim "  Commit the source and dist/ together."
