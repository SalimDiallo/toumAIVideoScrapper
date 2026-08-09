"""Minimal `.env` reader/writer for the in-app settings page.

`Settings` (pydantic-settings) loads `.env` once at startup, so edits here take
effect on the next restart. We update keys in place, preserving comments, blank
lines and unrelated keys; a value of ``None`` removes the key.
"""

from __future__ import annotations

from pathlib import Path


def load_env(path: Path) -> dict[str, str]:
    """Parse ``KEY=VALUE`` lines into a dict (comments / blanks ignored)."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        key, _, value = s.partition("=")
        out[key.strip()] = value.strip()
    return out


def update_env(path: Path, updates: dict[str, str | None]) -> None:
    """Apply ``updates`` to the ``.env`` file, keeping everything else intact.

    Existing keys are rewritten in place; new keys are appended; keys mapped to
    ``None`` are dropped.
    """
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            out.append(line)
            continue
        key = s.partition("=")[0].strip()
        if key in updates:
            seen.add(key)
            val = updates[key]
            if val is None:
                continue  # remove the key
            out.append(f"{key}={val}")
        else:
            out.append(line)
    for key, val in updates.items():
        if key in seen or val is None:
            continue
        out.append(f"{key}={val}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
