#!/usr/bin/env python3
"""Package every plugin/scraper folder into dist/ and write the Stash source indexes.

Layout in  : plugins/<Name>/<Name>.yml, scrapers/<Name>/<Name>.yml
Layout out : dist/plugins/<Name>.zip + dist/plugins/index.yml (same for scrapers)

Shared helpers are wired up by import, not by a manifest key: any top-level
package or module in common/python/ that a folder's Python actually imports is
copied into the root of that folder's zip, so it lands next to the script at
runtime. Manifests stay pure Stash config - Stash rejects unknown yml fields.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import zipfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
DIST = ROOT / "dist"
SHARED = ROOT / "common" / "python"
KINDS = ("plugins", "scrapers")
SKIP = {".git", "__pycache__", ".gitkeep", ".DS_Store"}


def git_meta(path: Path) -> tuple[str, str]:
    """Short sha and commit date of the last change touching `path`."""
    fmt = subprocess.run(
        ["git", "log", "-1", "--format=%h|%ad", "--date=format:%Y-%m-%d %H:%M:%S", "--", str(path)],
        cwd=ROOT, capture_output=True, text=True, check=False,
    ).stdout.strip()
    sha, _, date = fmt.partition("|")
    return sha or "0000000", date or "1970-01-01 00:00:00"


def manifest_for(folder: Path) -> Path | None:
    preferred = folder / f"{folder.name}.yml"
    if preferred.exists():
        return preferred
    ymls = sorted(p for p in folder.glob("*.yml"))
    return ymls[0] if ymls else None


COMMENT_META = re.compile(r"^#\s*(?P<key>version|description)\s*:\s*(?P<value>.+?)\s*$", re.MULTILINE)


def meta_of(manifest: Path) -> dict:
    """Index metadata for one manifest.

    Plugin configs carry `name`/`description`/`version` natively. Scraper configs
    do not - and Stash rejects unknown keys in them - so the same values are read
    from `# key: value` comments, which the yml parser drops. Real fields win.
    """
    text = manifest.read_text(encoding="utf-8")
    meta = {key: value for key, value in COMMENT_META.findall(text)}
    parsed = yaml.safe_load(text) or {}
    for key in ("name", "description", "version"):
        if parsed.get(key) not in (None, ""):
            meta[key] = parsed[key]
    meta["requires"] = parsed.get("requires", [])
    return meta


def files_of(folder: Path):
    for path in sorted(folder.rglob("*")):
        if path.is_file() and not any(part in SKIP for part in path.parts):
            yield path, path.relative_to(folder).as_posix()


def shared_names() -> list[str]:
    """Importable top-level names available in common/python/."""
    if not SHARED.is_dir():
        return []
    names = [p.name for p in SHARED.iterdir() if p.is_dir() and (p / "__init__.py").exists()]
    names += [p.stem for p in SHARED.glob("*.py")]
    return sorted(names)


def shared_used_by(folder: Path) -> list[str]:
    """Which shared names the folder's Python imports."""
    sources = [p.read_text(encoding="utf-8", errors="replace") for p in folder.rglob("*.py")]
    used = []
    for name in shared_names():
        pattern = re.compile(rf"^\s*(?:import|from)\s+{re.escape(name)}\b", re.MULTILINE)
        if any(pattern.search(text) for text in sources):
            used.append(name)
    return used


def shared_files(name: str):
    """(absolute, archive-relative) pairs for one shared package or module."""
    target = SHARED / name
    if target.is_dir():
        for path in sorted(target.rglob("*.py")):
            if "__pycache__" not in path.parts:
                yield path, path.relative_to(SHARED).as_posix()
    else:
        module = SHARED / f"{name}.py"
        if module.is_file():
            yield module, module.name


def build(kind: str) -> list[dict]:
    src, out = ROOT / kind, DIST / kind
    out.mkdir(parents=True, exist_ok=True)
    entries = []

    for folder in sorted(p for p in src.iterdir() if p.is_dir()):
        manifest = manifest_for(folder)
        if manifest is None:
            print(f"  !! {kind}/{folder.name}: no .yml manifest, skipped")
            continue

        meta = meta_of(manifest)
        sha, date = git_meta(folder)
        version = f"{meta.get('version', '0.0')}-{sha}"
        zip_path = out / f"{folder.name}.zip"

        bundled = shared_used_by(folder)
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            for abs_path, rel in files_of(folder):
                z.write(abs_path, rel)
            for name in bundled:
                for abs_path, rel in shared_files(name):
                    z.write(abs_path, rel)

        entries.append({
            "id": folder.name,
            "name": meta.get("name", folder.name),
            "metadata": {"description": meta.get("description", "")},
            "version": version,
            "date": date,
            "path": zip_path.name,
            "sha256": hashlib.sha256(zip_path.read_bytes()).hexdigest(),
            "requires": meta.get("requires", []),
        })
        extra = f" + common/python/{{{','.join(bundled)}}}" if bundled else ""
        print(f"  -> {kind}/{folder.name} {version}{extra}")

    header = (
        "# AUTOGENERATED — do not edit by hand.\n"
        f"# Built by .github/workflows/publish.yml from {kind}/*/*.yml\n"
    )
    body = yaml.safe_dump(entries, sort_keys=False, allow_unicode=True) if entries else "[]\n"
    (out / "index.yml").write_text(header + body, encoding="utf-8")
    return entries


if __name__ == "__main__":
    for kind in KINDS:
        print(f"[{kind}]")
        build(kind)
    print("indexes written to", DIST)
