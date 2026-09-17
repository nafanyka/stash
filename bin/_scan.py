#!/usr/bin/env python3
"""What changed since the last build, and the record of what was built.

Used by `bin/build.sh`; not meant to be run by hand, though `report` is harmless.

It imports the packager rather than re-implementing any of it, so the version it reads
and the files it hashes are exactly the ones that end up in the zip. A build script that
disagreed with the builder about what a component *is* would be worse than no build
script at all.

    report   one line per component, tab separated:  status  kind  name  version  note
    commit   write the current state to the cache, after a successful build
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CACHE = Path(__file__).resolve().parent / "build-cache.json"
PACKAGER = ROOT / ".github" / "workflows" / "build_index.py"

# Statuses, in the order a human wants to read them.
NEW = "new"
VERSION = "version"        # version changed - the normal reason to rebuild
CONTENT = "content"        # files changed, version did not
UNCHANGED = "unchanged"
REMOVED = "removed"        # in the cache, no longer on disk


def packager():
    """The real `build_index.py`, imported from where the CI runs it."""
    spec = importlib.util.spec_from_file_location("build_index", PACKAGER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fingerprint(build_index, folder: Path) -> str:
    """A hash of everything that would go into this component's zip.

    The same file list the packager uses, plus the shared modules it bundles in - a
    change in `common/python/` changes the archive just as much as a change in the
    component's own directory, and a cache that missed that would report "unchanged"
    for a zip whose contents had moved.

    Deliberately not the zip's own sha256: that would mean building every component to
    find out whether it needed building.
    """
    digest = hashlib.sha256()
    for abs_path, rel in build_index.files_of(folder):
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(abs_path.read_bytes())
        digest.update(b"\0")
    for name in build_index.shared_used_by(folder):
        for abs_path, rel in build_index.shared_files(name):
            digest.update(("common/" + rel).encode("utf-8"))
            digest.update(b"\0")
            digest.update(abs_path.read_bytes())
            digest.update(b"\0")
    return digest.hexdigest()


def survey():
    """Every component on disk: (kind, name) -> {version, fingerprint}."""
    build_index = packager()
    found = {}
    for kind in build_index.KINDS:
        source = ROOT / kind
        if not source.is_dir():
            continue
        for folder in sorted(p for p in source.iterdir() if p.is_dir()):
            manifest = build_index.manifest_for(folder)
            if manifest is None:
                continue
            meta = build_index.meta_of(manifest)
            found[f"{kind}/{folder.name}"] = {
                "version": str(meta.get("version", "")),
                "fingerprint": fingerprint(build_index, folder),
            }
    return found


def cached():
    if not CACHE.exists():
        return {}
    try:
        return json.loads(CACHE.read_text(encoding="utf-8")).get("components", {})
    except (ValueError, OSError):
        # A cache that cannot be read is a cache that says nothing; everything is new,
        # which rebuilds everything, which is correct if slow.
        return {}


def report():
    now, before = survey(), cached()
    lines = []

    for key, state in now.items():
        kind, _, name = key.partition("/")
        was = before.get(key)
        if was is None:
            status, note = NEW, "not built from here before"
        elif was.get("version") != state["version"]:
            status = VERSION
            note = f"{was.get('version') or '?'} -> {state['version']}"
        elif was.get("fingerprint") != state["fingerprint"]:
            status = CONTENT
            note = "files changed, version did not"
        else:
            status, note = UNCHANGED, ""
        lines.append((status, kind, name, state["version"], note))

    for key in before:
        if key not in now:
            kind, _, name = key.partition("/")
            lines.append((REMOVED, kind, name, before[key].get("version", ""),
                          "gone from the working tree"))

    order = {NEW: 0, VERSION: 1, CONTENT: 2, REMOVED: 3, UNCHANGED: 4}
    for line in sorted(lines, key=lambda one: (order[one[0]], one[1], one[2])):
        print("\t".join(line))


def commit():
    CACHE.write_text(json.dumps({
        "_comment": "Written by bin/build.sh. What each component looked like when it "
                    "was last packaged, so the next build can say what moved. Safe to "
                    "delete - that just means the next build treats everything as new.",
        "components": survey(),
    }, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else "report"
    if command == "report":
        report()
    elif command == "commit":
        commit()
    else:
        print(f"unknown command {command!r}", file=sys.stderr)
        sys.exit(2)
