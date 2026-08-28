"""Thread-local working directory + worktree confinement.

The tool implementations resolve relative paths against a working directory.
In normal use that directory is ``os.getcwd()`` (process-wide), which is fine
for a single agent. But multi-agent teammates (add3.0) run in parallel threads,
each potentially inside its own ``.git/worktree``, so a *thread-local* cwd is
needed to stop one teammate's relative paths from leaking into another's.

``bash.py`` already tracks ``cd`` per thread via its own ``threading.local``;
this module is the shared counterpart that the file tools (read/write/edit/glob/
grep) consult. When a worktree teammate runs, it calls :func:`set_cwd` and
:func:`set_root` at the top of its thread, and :func:`resolve` then (a) resolves
relative paths against that cwd and (b) refuses any path that escapes the root.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

_local = threading.local()


def set_cwd(path: str | None) -> None:
    """Set this thread's working directory (None clears it)."""
    _local.cwd = path


def get_cwd() -> Path:
    """This thread's working directory, falling back to the process cwd."""
    return Path(getattr(_local, "cwd", None) or os.getcwd())


def set_root(path: str | None) -> None:
    """Set this thread's confinement root (a worktree). None clears it."""
    _local.root = path


def get_root() -> Path | None:
    """This thread's confinement root, or None when unrestricted."""
    raw = getattr(_local, "root", None)
    return Path(raw) if raw else None


def resolve(path: str) -> Path:
    """Resolve a tool path against the thread-local cwd, confined to the root.

    Raises ``ValueError`` when a root is set and the resolved path escapes it.
    Callers already wrap ``execute`` bodies in try/except and return the error
    as a string, so this surfaces as a clean message rather than a crash.
    """
    p = Path(path).expanduser()
    p = (p if p.is_absolute() else get_cwd() / p).resolve()
    root = get_root()
    if root is not None:
        try:
            p.relative_to(root)
        except ValueError:
            raise ValueError(f"path escapes worktree root: {path}") from None
    return p
