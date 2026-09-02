"""Annotated-SQL migration files: the chain format (spec 2026-09-02 §4).

Files are plain editable SQL with a structured header line that carries
the risk classification. Labels are legal SQL comments, so generated
files are directly psql-runnable. Parsing is FAIL-LOUD: a missing or
malformed header refuses the file — never assume-safe.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

from sqlpush.types import RiskClass, SqlpushError

RISK_RANK: dict[RiskClass, int] = {
    RiskClass.SAFE: 0,
    RiskClass.RISKY: 1,
    RiskClass.DESTRUCTIVE: 2,
}
"""Explicit ordering — RiskClass is a plain str-Enum with no < (spec P5)."""

# Header line: `-- sqlpush: revision=0007 risk=DESTRUCTIVE ops=3`. Keys other
# than revision/risk (e.g. ops=, parent=, generated=) are tolerated — the
# writer may add informative keys, the parser only requires the fail-loud two.
_HEADER_LINE_RE = re.compile(r"^--\s*sqlpush:\s*(?P<body>.*)$")
_OP_RE = re.compile(r"^--\s*op\s+\d+\s+\[(?P<label>[^\]]*)\]\s*(?P<desc>.*)$")
_REV_PREFIX_RE = re.compile(r"^(\d+)_")


class MigrationFileError(SqlpushError):
    """A chain file is missing/malformed — fail-loud, never assume-safe."""


@dataclass
class MigrationFile:
    name: str
    revision_id: str
    risk: RiskClass
    ops: list[tuple[str, str]] = field(default_factory=list)
    text: str = ""


def checksum(text: str) -> str:
    normalized = text.replace("\r\n", "\n").rstrip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _parse_header(text: str, name: str) -> tuple[str, RiskClass]:
    for line in text.splitlines():
        m = _HEADER_LINE_RE.match(line.strip())
        if m:
            break
    else:
        raise MigrationFileError(f"{name}: missing -- sqlpush: header")
    body = m.group("body").strip()
    if not body:
        raise MigrationFileError(f"{name}: malformed -- sqlpush: header (empty)")
    pairs: dict[str, str] = {}
    for token in body.split():
        key, sep, value = token.partition("=")
        if not sep or not key or not value:
            raise MigrationFileError(f"{name}: malformed -- sqlpush: header token {token!r}")
        pairs[key] = value
    revision = pairs.get("revision", "")
    if not revision.isdigit():
        raise MigrationFileError(f"{name}: malformed -- sqlpush: header: revision=NNNN required")
    risk_name = pairs.get("risk")
    if risk_name is None:
        raise MigrationFileError(
            f"{name}: malformed -- sqlpush: header: risk= missing (expected SAFE|RISKY|DESTRUCTIVE)"
        )
    try:
        risk = RiskClass[risk_name]
    except KeyError:
        raise MigrationFileError(
            f"{name}: malformed -- sqlpush: header: unknown risk={risk_name!r} "
            f"(expected SAFE|RISKY|DESTRUCTIVE)"
        ) from None
    return revision, risk


def render_migration_file(
    *,
    ops: list[tuple[str, str]],
    revision_id: str,
    risk: RiskClass,
    message: str | None = None,
    parent: str | None = None,
) -> str:
    lines = [f"-- sqlpush: revision={revision_id} risk={risk.name}"]
    # parent= is informative-only: the parser never requires it (hand-written
    # files omit it freely), but the writer always records the slot.
    lines.append(f"-- parent={parent or ''}")
    if message:
        lines.append(f"-- {message}")
    lines.append("")
    for i, (label, sql) in enumerate(ops, start=1):
        lines.append(f"-- op {i} {label}")
        lines.append(sql.rstrip(";") + ";")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def parse_migration_file(text: str, *, name: str) -> MigrationFile:
    revision, risk = _parse_header(text, name)

    ops: list[tuple[str, str]] = []
    sql_buf: list[str] = []
    current_label = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("-- sqlpush:"):
            continue
        op_m = _OP_RE.match(stripped)
        if op_m:
            if sql_buf:
                ops.append((current_label, "\n".join(sql_buf).strip()))
                sql_buf = []
            current_label = f"[{op_m.group('label')}] {op_m.group('desc')}".strip()
            continue
        if stripped.startswith("--"):
            continue  # comentarios libres (message, parent, generated...) — ignorable
        if stripped:
            sql_buf.append(line)
    if sql_buf:
        ops.append((current_label, "\n".join(sql_buf).strip()))
    if not ops:
        raise MigrationFileError(f"{name}: file has no SQL")

    return MigrationFile(name=name, revision_id=revision, risk=risk, ops=ops, text=text)


def next_revision_id(chain_dir: Path | str) -> str:
    max_n = 0
    for f in sorted(Path(chain_dir).glob("*.sql")):
        m = _REV_PREFIX_RE.match(f.name)
        if m:
            max_n = max(max_n, int(m.group(1)))
    return f"{max_n + 1:04d}"
