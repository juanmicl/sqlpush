# src/sqlpush/hook.py
"""Project hook: ``migrations/sqlpush.py`` first, then ``sqlpush.py``.

The alembic ``env.py`` / pytest ``conftest.py`` pattern: a hook file
next to where the user runs the CLI becomes the source of defaults for
metadata, DSN and chain dir. Explicit flags always win (flag > hook >
env/default); without the hook every verb behaves exactly as before.

Discovery checks ``migrations/sqlpush.py`` first (the preferred
location — it lives next to the chain, no root clutter) and falls
back to the repo-root ``sqlpush.py`` (backwards compat); first match
wins.

Loading is BY PATH (``spec_from_file_location``), never by module
name: a file named ``sqlpush.py`` in the CWD would shadow the
installed package if the CWD came first on ``sys.path`` — so the CWD
is APPENDED (never ``insert(0)``), and always the CWD, never the
loaded candidate's own directory (the append exists so the
consumer's package imports resolve). This guarantees the package
wins for normal imports while the hook itself is only ever loaded
explicitly.

Only typed :class:`SqlpushError`-family errors escape this module,
always naming the file that actually loaded and the member involved.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType

from sqlpush.types import SqlpushError

HOOK_FILENAME = "sqlpush.py"
# first match wins: migrations/ (next to the chain) preferred, the
# repo root kept as the backwards-compat fallback
CANDIDATES = (Path("migrations") / HOOK_FILENAME, Path(HOOK_FILENAME))


class HookError(SqlpushError):
    """The project hook is broken or incomplete — names file + member."""


def load_project_hook() -> ModuleType | None:
    """Load the first existing hook candidate by path; ``None`` when absent.

    ``migrations/sqlpush.py`` is checked before the repo-root
    ``sqlpush.py`` — first match wins. On success the CWD is appended
    to ``sys.path`` (never inserted at the front, and never the
    candidate's own directory — see the module docstring). Any
    import-time failure is re-typed as :class:`HookError` naming the
    candidate that failed, in the candidate spelling.
    """
    hook_file = next((p for p in CANDIDATES if p.is_file()), None)
    if hook_file is None:
        return None
    sys.path.append(os.getcwd())
    spec = importlib.util.spec_from_file_location("sqlpush_project_hook", hook_file)
    if spec is None or spec.loader is None:
        raise HookError(f"{hook_file.as_posix()}: cannot load (invalid module spec)")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise HookError(f"{hook_file.as_posix()}: import raised: {exc}") from exc
    return module


def _hook_label(hook: ModuleType) -> str:
    """The CANDIDATES spelling of the loaded hook file (posix, portable).

    Errors must name the file that actually loaded, so map the loaded
    module's ``__file__`` back to its candidate spelling; unknown
    provenance degrades to the bare filename.
    """
    loaded_file = getattr(hook, "__file__", None)
    if not loaded_file:
        return HOOK_FILENAME
    loaded = Path(loaded_file).resolve()
    for candidate in CANDIDATES:
        if (Path.cwd() / candidate).resolve() == loaded:
            return candidate.as_posix()
    return HOOK_FILENAME


def hook_dsn(hook: ModuleType) -> str:
    """Call ``get_dsn()`` LAZILY — only when a verb actually needs it."""
    label = _hook_label(hook)
    getter = getattr(hook, "get_dsn", None)
    if getter is None:
        raise HookError(f"{label}: missing get_dsn()")
    try:
        return getter()
    except Exception as exc:
        raise HookError(f"{label}: get_dsn() raised: {exc}") from exc


def hook_metadata(hook: ModuleType):
    """Call ``get_metadata()`` LAZILY — returns the populated MetaData object."""
    label = _hook_label(hook)
    getter = getattr(hook, "get_metadata", None)
    if getter is None:
        raise HookError(f"{label}: missing get_metadata()")
    try:
        return getter()
    except Exception as exc:
        raise HookError(f"{label}: get_metadata() raised: {exc}") from exc


def hook_chain_dir(hook: ModuleType | None, default: str) -> str:
    """Read ``CHAIN_DIR`` as a module attribute; ``default`` when unset."""
    value = getattr(hook, "CHAIN_DIR", None) if hook is not None else None
    return default if value is None else value
