# src/sqlpush/cli.py
"""sqlpush: diff/push/check PostgreSQL schema drift from SQLAlchemy models.

Exit codes: diff always 0; check 0 clean / 2 drift / 3 destructive drift;
push 0 applied / 1 destructive blocked / 2 error (incl. partial failure);
revision 0 written / 1 error (empty drift refuses); migrate 0 clean /
1 blocked or partial failure.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path
from typing import Annotated

import typer
from sqlalchemy import create_engine
from sqlalchemy.exc import ArgumentError
from sqlalchemy.pool import NullPool

from sqlpush import api
from sqlpush.core.render import render
from sqlpush.types import (
    CheckResult,
    MetadataImportError,
    RiskClass,
    SqlpushError,
)

app = typer.Typer(add_completion=False, help=__doc__ or "")

# Annotated + None default: keeps ruff B008/B006 quiet (no call in the
# default, no mutable default) while staying the idiomatic typer style.
SchemaOpt = Annotated[list[str] | None, typer.Option("--schema")]
ExcludeOpt = Annotated[list[str] | None, typer.Option("--exclude")]
# Path-typed options need the Annotated form: B008 (call in default) only
# exempts typer.Option for non-Path annotations
DirOpt = Annotated[Path, typer.Option("--dir")]


def _load_metadata(spec: str):
    module, _, attr = spec.partition(":")
    try:
        obj = importlib.import_module(module)
        for part in attr.split("."):
            obj = getattr(obj, part)
        return obj
    except (ImportError, AttributeError) as exc:
        raise MetadataImportError(f"cannot import {spec!r}: {exc}") from exc


def _engine(dsn: str | None):
    dsn = dsn or os.environ.get("DATABASE_URL")
    if not dsn:
        typer.secho("no --dsn and no DATABASE_URL set", fg="red", err=True)
        raise typer.Exit(code=1)
    try:
        return create_engine(dsn, poolclass=NullPool)
    except ArgumentError as exc:
        # malformed DSN is a configuration error, not a verb error: exit 1
        typer.secho(f"invalid DSN: {exc}", fg="red", err=True)
        raise typer.Exit(code=1) from exc


def _emit_json(plan) -> None:
    typer.echo(json.dumps(plan.to_json_dict(), indent=2))


def _risk_summary(p) -> None:
    # --verbose adds an op-count line per PRESENT risk class (absent
    # classes stay silent, mirroring render's section omission)
    for cls in RiskClass:
        n = sum(1 for op in p.operations if op.risk is cls)
        if n:
            typer.echo(f"{cls.value}: {n} operation(s)")


@app.command()
def diff(
    metadata_spec: str = typer.Argument(..., help="module:metadata"),
    dsn: str | None = typer.Option(None),
    json_output: bool = typer.Option(False, "--json"),
    verbose: bool = typer.Option(False, "--verbose"),
    quiet: bool = typer.Option(False, "--quiet"),
    schema: SchemaOpt = None,
    exclude: ExcludeOpt = None,
):
    md = _load_metadata(metadata_spec)
    engine = _engine(dsn)
    try:
        p = api.plan(md, engine, schemas=schema, exclude=exclude or ())
        if json_output:
            _emit_json(p)
            raise typer.Exit(code=0)
        if verbose:
            _risk_summary(p)
        typer.echo(render(p) or "-- schema in sync --")
    finally:
        engine.dispose()
    raise typer.Exit(code=0)


@app.command()
def check(
    metadata_spec: str = typer.Argument(...),
    dsn: str | None = typer.Option(None),
    json_output: bool = typer.Option(False, "--json"),
    verbose: bool = typer.Option(False, "--verbose"),
    quiet: bool = typer.Option(False, "--quiet"),
    schema: SchemaOpt = None,
    exclude: ExcludeOpt = None,
):
    md = _load_metadata(metadata_spec)
    engine = _engine(dsn)
    try:
        # plan ONCE and derive everything from that single object: a
        # second plan could observe a different DB state than the one
        # the exit code was derived from (TOCTOU)
        p = api.plan(md, engine, schemas=schema, exclude=exclude or ())
        result = CheckResult(
            clean=not p.drift,
            drift=p.drift,
            has_destructive=p.has_destructive,
        )
        if json_output:
            _emit_json(p)
        elif verbose:
            ndest = sum(1 for op in p.operations if op.risk is RiskClass.DESTRUCTIVE)
            typer.echo(f"drift: {len(p.operations)} operation(s), {ndest} destructive")
    finally:
        engine.dispose()
    if result.clean:
        raise typer.Exit(code=0)
    raise typer.Exit(code=3 if result.has_destructive else 2)


@app.command()
def push(
    metadata_spec: str = typer.Argument(...),
    dsn: str | None = typer.Option(None),
    json_output: bool = typer.Option(False, "--json"),
    allow_destructive: bool = typer.Option(False, "--allow-destructive"),
    safe_only: bool = typer.Option(False, "--safe-only"),
    no_lock: bool = typer.Option(False, "--no-lock"),
    lock_timeout: float = typer.Option(5.0, "--lock-timeout"),
    advisory_wait: float = typer.Option(30.0, "--advisory-wait"),
    verbose: bool = typer.Option(False, "--verbose"),
    quiet: bool = typer.Option(False, "--quiet"),
    schema: SchemaOpt = None,
    exclude: ExcludeOpt = None,
):
    md = _load_metadata(metadata_spec)
    engine = _engine(dsn)
    try:
        try:
            report = api.push(
                md,
                engine,
                safe_only=safe_only,
                allow_destructive=allow_destructive,
                lock=not no_lock,
                lock_timeout=lock_timeout,
                advisory_wait=advisory_wait,
                schemas=schema,
                exclude=exclude or (),
            )
        except SqlpushError as exc:
            # binding exit-code table: 1 is exclusively "destructive
            # blocked"; every other push error (lock timeout, connect
            # failure, ...) is exit 2
            typer.secho(str(exc), fg="red", err=True)
            raise typer.Exit(code=2) from exc
        if json_output:
            typer.echo(
                json.dumps(
                    {
                        "version": 1,
                        "applied": [a.__dict__ for a in report.applied],
                        "blocked": [b.__dict__ for b in report.blocked],
                        "skipped": [s.__dict__ for s in report.skipped],
                        "partial_failure": report.partial_failure,
                    },
                    indent=2,
                    default=str,
                )
            )
        elif verbose:
            for a in report.applied:
                typer.echo(f"{a.status}: {a.type}")
            if report.skipped:
                typer.echo(f"skipped: {len(report.skipped)} operation(s)")
            typer.echo(f"duration: {report.duration:.3f}s")
    finally:
        engine.dispose()
    if report.blocked:
        if not quiet:
            typer.secho(
                f"{len(report.blocked)} destructive operation(s) blocked; "
                "re-run with --allow-destructive",
                fg="yellow",
                err=True,
            )
        raise typer.Exit(code=1)
    if report.skipped and not json_output and not quiet:
        # policy-skipped (--safe-only) is informational, never an error
        typer.secho(
            f"{len(report.skipped)} operation(s) skipped by --safe-only",
            fg="blue",
            err=True,
        )
    if report.partial_failure:
        raise typer.Exit(code=2)
    raise typer.Exit(code=0)


@app.command()
def revision(
    metadata_spec: str = typer.Argument(..., help="module:metadata"),
    ref_dsn: str = typer.Option(..., "--ref-dsn"),
    message: str | None = typer.Option(None, "--message", "-m"),
    out_dir: DirOpt = Path("migrations/versions"),
    schema: SchemaOpt = None,
    exclude: ExcludeOpt = None,
):
    """Generate the next migration file from models vs the reference DB."""
    md = _load_metadata(metadata_spec)
    # required --ref-dsn (no DATABASE_URL fallback): the reference DB is a
    # different database from the push target — conflating them silently
    # would chain against the wrong head
    engine = _engine(ref_dsn)
    try:
        path = api.revision(
            md,
            engine,
            out_dir=out_dir,
            message=message,
            schemas=schema,
            exclude=exclude or (),
        )
    finally:
        engine.dispose()
    typer.echo(str(path))
    raise typer.Exit(code=0)


@app.command()
def migrate(
    dsn: str | None = typer.Option(None),
    allow_destructive: bool = typer.Option(False, "--allow-destructive"),
    out_dir: DirOpt = Path("migrations/versions"),
):
    """Replay pending migration files (gates + checksum bookkeeping)."""
    engine = _engine(dsn)
    try:
        report = api.migrate(engine, chain_dir=out_dir, allow_destructive=allow_destructive)
    finally:
        engine.dispose()
    typer.echo(
        f"applied: {len(report.applied)}, skipped: {len(report.skipped)}, "
        f"blocked: {len(report.blocked)}, partial_failure: {report.partial_failure}"
    )
    for note in report.notes:
        typer.secho(note, fg="yellow", err=True)
    if report.blocked or report.partial_failure:
        raise typer.Exit(code=1)
    raise typer.Exit(code=0)


@app.command()
def stamp(
    dsn: str | None = typer.Option(None),
    out_dir: DirOpt = Path("migrations/versions"),
):
    """Adopt an existing DB: register chain files without executing SQL."""
    engine = _engine(dsn)
    try:
        report = api.stamp(engine, chain_dir=out_dir)
    finally:
        engine.dispose()
    typer.echo(
        f"applied: {len(report.applied)}, skipped (registered): {len(report.skipped)}, "
        f"blocked: {len(report.blocked)}, partial_failure: {report.partial_failure}"
    )
    for note in report.notes:
        typer.secho(note, fg="yellow", err=True)
    if report.blocked or report.partial_failure:
        raise typer.Exit(code=1)
    raise typer.Exit(code=0)


def main() -> None:  # [project.scripts] entry point
    try:
        app()
    except (MetadataImportError, SqlpushError) as exc:
        typer.secho(str(exc), fg="red", err=True)
        sys.exit(1)
