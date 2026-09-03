# src/sqlpush/hook.py
"""Project hook: ``sqlpush.py`` in the CWD.

The alembic ``env.py`` / pytest ``conftest.py`` pattern: when a
``sqlpush.py`` exists next to where the user runs the CLI, it becomes
the source of defaults for metadata, DSN and chain dir. Explicit flags
always win (flag > hook > env/default); without the hook every verb
behaves exactly as before.

Loading is BY PATH (``spec_from_file_location``), never by module
name: a file named ``sqlpush.py`` in the CWD would shadow the
installed package if the CWD came first on ``sys.path`` — so the CWD
is APPENDED (never ``insert(0)``), guaranteeing the package wins for
normal imports while the hook itself is only ever loaded explicitly.

Only typed :class:`SqlpushError`-family errors escape this module,
always naming the file and the member involved.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType

from sqlpush.types import SqlpushError

HOOK_FILENAME = "sqlpush.py"


class HookError(SqlpushError):
    """The project hook is broken or incomplete — names file + member."""


def load_project_hook() -> ModuleType | None:
    """Load ``./sqlpush.py`` by path if present; ``None`` when absent.

    On success the CWD is appended to ``sys.path`` (never inserted at
    the front — see the module docstring for the shadowing rationale).
    Any import-time failure is re-typed as :class:`HookError`.
    """
    path = Path.cwd() / HOOK_FILENAME
    if not path.is_file():
        return None
    sys.path.append(os.getcwd())
    spec = importlib.util.spec_from_file_location("sqlpush_project_hook", path)
    if spec is None or spec.loader is None:
        raise HookError(f"{HOOK_FILENAME}: cannot load (invalid module spec)")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise HookError(f"{HOOK_FILENAME}: import raised: {exc}") from exc
    return module


def hook_dsn(hook: ModuleType) -> str:
    """Call ``get_dsn()`` LAZILY — only when a verb actually needs it."""
    getter = getattr(hook, "get_dsn", None)
    if getter is None:
        raise HookError(f"{HOOK_FILENAME}: missing get_dsn()")
    try:
        return getter()
    except Exception as exc:
        raise HookError(f"{HOOK_FILENAME}: get_dsn() raised: {exc}") from exc


def hook_metadata(hook: ModuleType):
    """Call ``get_metadata()`` LAZILY — returns the populated MetaData object."""
    getter = getattr(hook, "get_metadata", None)
    if getter is None:
        raise HookError(f"{HOOK_FILENAME}: missing get_metadata()")
    try:
        return getter()
    except Exception as exc:
        raise HookError(f"{HOOK_FILENAME}: get_metadata() raised: {exc}") from exc


def hook_chain_dir(hook: ModuleType | None, default: str) -> str:
    """Read ``CHAIN_DIR`` as a module attribute; ``default`` when unset."""
    value = getattr(hook, "CHAIN_DIR", None) if hook is not None else None
    return default if value is None else value
