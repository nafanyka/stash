"""Backend-side path validation.

Requirement 27: the frontend's own checks (greying out a folder outside a library
root, rejecting an obviously bad new-folder name) are convenience only. Nothing here
trusts a path that came from the browser - every one is re-derived or re-checked
against `configuration.general.stashes` before it is used for anything destructive.
"""

from __future__ import annotations

import os
import re

_INVALID_NAME_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


class ValidationError(ValueError):
    """A path or name failed a security/sanity check."""


def _real(path: str) -> str:
    return os.path.realpath(path)


def normalize_roots(roots) -> list[str]:
    return [_real(root) for root in (roots or []) if root]


def is_within_roots(path: str, roots: list[str]) -> bool:
    real_path = _real(path)
    for root in roots:
        if real_path == root or real_path.startswith(root + os.sep):
            return True
    return False


def require_within_roots(path: str, roots: list[str], what: str) -> str:
    """Returns the realpath, or raises if it escapes every configured library root.

    Resolved with `realpath` specifically so a symlink cannot point the destination
    outside the roots it appears to be inside of (requirement 27's "symlink escape").
    """
    if not roots:
        raise ValidationError(
            "no Stash library paths are configured (Settings -> Library); "
            "MyMoover refuses to guess an allowed root")
    real_path = _real(path)
    if not is_within_roots(real_path, roots):
        raise ValidationError(
            "%s (%s) is outside every configured Stash library path" % (what, path))
    return real_path


def validate_new_folder_name(name: str) -> str:
    """A bare folder name for `+ New Folder` - never a path.

    Rejects empty names, path separators, `..`, and control characters, so a name
    cannot be used to escape the currently-selected parent directory (requirement 6).
    """
    text = str(name or "").strip()
    if not text:
        raise ValidationError("folder name must not be empty")
    if text in (".", ".."):
        raise ValidationError("folder name must not be '.' or '..'")
    if "/" in text or "\\" in text:
        raise ValidationError("folder name must not contain a path separator")
    if _INVALID_NAME_CHARS.search(text):
        raise ValidationError("folder name contains a character that is not allowed")
    if os.path.isabs(text):
        raise ValidationError("folder name must not be an absolute path")
    return text
