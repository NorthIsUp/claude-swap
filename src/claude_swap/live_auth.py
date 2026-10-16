"""Publish auth through Claude's watched user settings, not its managed-key cache.

Verified with Claude Code 2.1.285: changing env.ANTHROPIC_API_KEY changes the
request header in the SAME process. Removing the setting leaves process.env
unchanged, so OAuth requires an explicit empty string. Keys stay out of logs
and argv; settings containing a key are written atomically with mode 0600.
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
from pathlib import Path

from claude_swap.claude_locks import proper_lockfile
from claude_swap.credentials import looks_like_api_key
from claude_swap.exceptions import SwitchError
from claude_swap.fsutil import replace_with_retry
from claude_swap.paths import get_claude_config_home

LIVE_AUTH_MARKER = "CLAUDE_SWAP_LIVE_AUTH"
AUTH_ENV = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR",
    "CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR",
)


def read_settings(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise SwitchError(f"Cannot read {path}; settings were left in place.") from exc
    if not isinstance(data, dict) or not isinstance(data.get("env", {}), dict):
        raise SwitchError(f"Invalid settings in {path}; settings were left in place.")
    return data


def _atomic_json(path: Path, data: dict) -> None:
    """Replace this directory entry, including a session's old sharing symlink.

    Callers resolve the DEFAULT settings path before calling us to preserve
    dotfiles symlinks; isolated snapshots deliberately replace their link.
    Rename is the final fallible operation so a failed write cannot publish
    half a credential or report failure after committing the new auth.
    """
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
            handle.write("\n")
        replace_with_retry(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def snapshot_session_settings(data: dict, destination: Path) -> None:
    """Copy shared preferences without overriding an isolated session's auth."""
    isolated = copy.deepcopy(data)
    env = isolated.get("env", {})
    for name in (*AUTH_ENV, LIVE_AUTH_MARKER):
        env.pop(name, None)
    isolated.pop("apiKeyHelper", None)
    _atomic_json(destination, isolated)


def publish_live_auth(credentials: str, backup_dir: Path) -> None:
    settings = get_claude_config_home() / "settings.json"
    target = settings.resolve()
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with proper_lockfile(target.with_name(target.name + ".cswap.lock")):
        data = read_settings(target)
        # Explicitly selected session profiles must not inherit a global
        # account change through their shared settings symlink. Replace only
        # links owned by cswap; their preferences are re-synced on next launch.
        sessions = backup_dir / "sessions"
        if sessions.is_dir():
            for profile in sessions.iterdir():
                shared = profile / "settings.json"
                if not shared.is_symlink() or shared.resolve() != target:
                    continue
                try:
                    manifest = json.loads((profile / ".cswap-shared.json").read_text())
                except (OSError, ValueError):
                    continue
                items = manifest.get("items") if isinstance(manifest, dict) else None
                if isinstance(items, list) and "settings.json" in items:
                    snapshot_session_settings(data, shared)
        updated = copy.deepcopy(data)
        env = updated.setdefault("env", {})
        env.update({name: "" for name in AUTH_ENV})
        env[LIVE_AUTH_MARKER] = "1"
        if looks_like_api_key(credentials):
            env["ANTHROPIC_API_KEY"] = credentials.strip()
        if updated != data:
            _atomic_json(target, updated)
