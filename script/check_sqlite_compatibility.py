#!/usr/bin/env python3
"""Validate and compare Genji schema-v3 SQLite dictionary databases.

JSON text is compared by type-aware decoded value, while every other public
column is compared by both SQLite storage class and value.  Rows are consumed
in bounded batches so the full release database can be checked without loading
it into memory.
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence


SCHEMA_VERSION = 3
PUBLIC_TABLES = (
    "_metadata",
    "entries",
    "definitions",
    "variant_lookup",
    "fts_entries",
    "fts_definitions",
)
JSON_COLUMNS = {
    "entries": {
        "reading_alternatives",
        "pos",
        "inflections",
        "relations",
        "meta",
        "raw_json",
    },
    "definitions": {"scenarios", "sensory_tags", "collocations", "examples"},
}
EXPECTED_COLUMNS = {
    "_metadata": ("key", "value"),
    "entries": (
        "uuid",
        "entry",
        "reading_primary",
        "reading_alternatives",
        "is_heteronym",
        "pos",
        "ctype",
        "ctype_source",
        "ctype_confidence",
        "inflections",
        "relations",
        "meta",
        "raw_json",
        "freq_rank",
        "needs_gloss",
        "lookup_register",
    ),
    "definitions": (
        "id",
        "entry_uuid",
        "def_index",
        "gloss",
        "register",
        "nuance",
        "scenarios",
        "sensory_tags",
        "collocations",
        "examples",
    ),
    "variant_lookup": ("variant", "entry", "entry_uuid"),
    "fts_entries": ("uuid", "entry", "reading_primary"),
    "fts_definitions": ("entry_uuid", "gloss"),
}
EXPECTED_INDEXES = {
    "idx_entries_entry",
    "idx_entries_entry_nocase",
    "idx_entries_reading",
    "idx_definitions_uuid",
    "idx_variant_lookup_variant",
}


class CompatibilityError(AssertionError):
    """A schema-v3 compatibility contract was violated."""


@dataclass(frozen=True)
class DatabaseStats:
    path: str
    bytes: int
    page_size: int
    page_count: int
    freelist_count: int
    journal_mode: str
    dbstat_bytes: dict[str, int]


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _connect(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise CompatibilityError(f"database does not exist: {path}")
    uri = f"file:{path.resolve().as_posix()}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    conn.execute("PRAGMA query_only=ON")
    return conn


def _schema_rows(conn: sqlite3.Connection) -> list[tuple[Any, ...]]:
    # rootpage is physical layout rather than schema and legitimately differs
    # across page-size/VACUUM candidates. All logical sqlite_schema fields remain.
    return conn.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_schema "
        "ORDER BY type, name, tbl_name, sql"
    ).fetchall()


def _pragma_rows(conn: sqlite3.Connection, pragma: str, name: str) -> list[tuple]:
    return conn.execute(
        f"PRAGMA {pragma}({_quote_identifier(name)})"
    ).fetchall()


def _public_table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_schema "
            "WHERE type='table' AND (name NOT LIKE 'sqlite_%' OR name='_metadata')"
        )
        if not any(
            row[0].startswith(prefix)
            for prefix in ("fts_entries_", "fts_definitions_")
        )
    }


def validate_database(conn: sqlite3.Connection, *, path: str = "database") -> None:
    """Validate the fixed schema-v3 release contract for one database."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version != SCHEMA_VERSION:
        raise CompatibilityError(f"{path}: user_version={version}, want 3")

    integrity = conn.execute("PRAGMA integrity_check").fetchall()
    if integrity != [("ok",)]:
        raise CompatibilityError(f"{path}: integrity_check failed: {integrity[:5]}")
    fk_errors = conn.execute("PRAGMA foreign_key_check").fetchmany(5)
    if fk_errors:
        raise CompatibilityError(f"{path}: foreign_key_check failed: {fk_errors}")

    tables = _public_table_names(conn)
    missing = set(PUBLIC_TABLES) - tables
    unexpected = tables - set(PUBLIC_TABLES)
    if missing or unexpected:
        raise CompatibilityError(
            f"{path}: public tables differ; missing={sorted(missing)}, "
            f"unexpected={sorted(unexpected)}"
        )

    for table, expected in EXPECTED_COLUMNS.items():
        actual = tuple(row[1] for row in _pragma_rows(conn, "table_xinfo", table) if row[6] == 0)
        if actual != expected:
            raise CompatibilityError(
                f"{path}: {table} columns={actual!r}, want {expected!r}"
            )

    indexes = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_schema "
            "WHERE type='index' AND name NOT LIKE 'sqlite_autoindex_%'"
        )
    }
    if indexes != EXPECTED_INDEXES:
        raise CompatibilityError(
            f"{path}: named indexes={sorted(indexes)}, "
            f"want {sorted(EXPECTED_INDEXES)}"
        )

    metadata_schema = conn.execute(
        "SELECT value FROM _metadata WHERE key='schema_version'"
    ).fetchone()
    if metadata_schema != (str(SCHEMA_VERSION),):
        raise CompatibilityError(
            f"{path}: _metadata schema_version={metadata_schema!r}, want ('3',)"
        )

    journal = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    if journal != "delete":
        raise CompatibilityError(f"{path}: journal_mode={journal}, want delete")

    plan = " ".join(
        row[3]
        for row in conn.execute(
            "EXPLAIN QUERY PLAN "
            "SELECT entry FROM entries WHERE entry >= ? AND entry < ?",
            ("語", "語\U0010ffff"),
        )
    )
    if "idx_entries_entry" not in plan or "SCAN entries" in plan:
        raise CompatibilityError(f"{path}: prefix lookup plan is incompatible: {plan}")


def collect_stats(conn: sqlite3.Connection, path: Path) -> DatabaseStats:
    dbstat_bytes: dict[str, int] = {}
    try:
        dbstat_bytes = {
            str(name): int(size)
            for name, size in conn.execute(
                "SELECT name, sum(pgsize) FROM dbstat GROUP BY name ORDER BY name"
            )
        }
    except sqlite3.OperationalError:
        # Some Python SQLite builds omit the optional dbstat virtual table.
        pass
    return DatabaseStats(
        path=str(path),
        bytes=path.stat().st_size,
        page_size=int(conn.execute("PRAGMA page_size").fetchone()[0]),
        page_count=int(conn.execute("PRAGMA page_count").fetchone()[0]),
        freelist_count=int(conn.execute("PRAGMA freelist_count").fetchone()[0]),
        journal_mode=str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower(),
        dbstat_bytes=dbstat_bytes,
    )


def _normalize_json(value: Any) -> tuple[Any, ...]:
    """Produce a value whose equality distinguishes all JSON scalar types."""
    if value is None:
        return ("null",)
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, int):
        return ("int", value)
    if isinstance(value, float):
        if not math.isfinite(value):
            return ("float", repr(value))
        return ("float", value.hex())
    if isinstance(value, str):
        return ("string", value)
    if isinstance(value, list):
        return ("array", tuple(_normalize_json(item) for item in value))
    if isinstance(value, dict):
        return (
            "object",
            tuple(sorted((key, _normalize_json(item)) for key, item in value.items())),
        )
    raise TypeError(f"unsupported decoded JSON value: {type(value).__name__}")


def _decode_json(value: str, *, context: str) -> tuple[Any, ...]:
    try:
        return _normalize_json(json.loads(value))
    except (json.JSONDecodeError, TypeError) as exc:
        raise CompatibilityError(f"{context}: invalid JSON: {exc}") from exc


def _row_query(conn: sqlite3.Connection, table: str) -> tuple[str, tuple[str, ...]]:
    columns = tuple(
        row[1] for row in _pragma_rows(conn, "table_xinfo", table) if row[6] == 0
    )
    selected = ["rowid"]
    for column in columns:
        quoted = _quote_identifier(column)
        selected.extend((quoted, f"typeof({quoted})"))
    sql = (
        f"SELECT {', '.join(selected)} FROM {_quote_identifier(table)} "
        "ORDER BY rowid"
    )
    return sql, columns


def _iter_rows(cursor: sqlite3.Cursor, batch_size: int) -> Iterator[tuple[Any, ...]]:
    while True:
        rows = cursor.fetchmany(batch_size)
        if not rows:
            return
        yield from rows


def _compare_table_rows(
    baseline: sqlite3.Connection,
    candidate: sqlite3.Connection,
    table: str,
    *,
    batch_size: int,
) -> int:
    baseline_sql, baseline_columns = _row_query(baseline, table)
    candidate_sql, candidate_columns = _row_query(candidate, table)
    if baseline_columns != candidate_columns:
        raise CompatibilityError(
            f"{table}: columns differ: {baseline_columns!r} != {candidate_columns!r}"
        )

    left = _iter_rows(baseline.execute(baseline_sql), batch_size)
    right = _iter_rows(candidate.execute(candidate_sql), batch_size)
    json_columns = JSON_COLUMNS.get(table, set())
    count = 0
    sentinel = object()
    while True:
        before = next(left, sentinel)
        after = next(right, sentinel)
        if before is sentinel or after is sentinel:
            if before is not after:
                raise CompatibilityError(f"{table}: row counts differ after {count} rows")
            return count
        count += 1
        if before[0] != after[0]:
            raise CompatibilityError(
                f"{table} row {count}: rowid differs: {before[0]!r} != {after[0]!r}"
            )
        for index, column in enumerate(baseline_columns):
            value_index = 1 + index * 2
            before_value, before_type = before[value_index : value_index + 2]
            after_value, after_type = after[value_index : value_index + 2]
            context = f"{table} rowid={before[0]} column={column}"
            if before_type != after_type:
                raise CompatibilityError(
                    f"{context}: SQLite type differs: {before_type} != {after_type}"
                )
            if column in json_columns and before_type == "text":
                before_value = _decode_json(before_value, context=f"baseline {context}")
                after_value = _decode_json(after_value, context=f"candidate {context}")
            if before_value != after_value:
                raise CompatibilityError(f"{context}: value differs")


def _compare_named_pragmas(
    baseline: sqlite3.Connection, candidate: sqlite3.Connection
) -> None:
    names = sorted(
        {
            row[1]
            for row in baseline.execute(
                "SELECT type, name FROM sqlite_schema "
                "WHERE type IN ('table', 'index')"
            )
        }
        | {
            row[1]
            for row in candidate.execute(
                "SELECT type, name FROM sqlite_schema "
                "WHERE type IN ('table', 'index')"
            )
        }
    )
    table_names = {
        row[0]
        for row in baseline.execute("SELECT name FROM sqlite_schema WHERE type='table'")
    }
    table_names |= {
        row[0]
        for row in candidate.execute("SELECT name FROM sqlite_schema WHERE type='table'")
    }
    index_names = {
        row[0]
        for row in baseline.execute("SELECT name FROM sqlite_schema WHERE type='index'")
    }
    index_names |= {
        row[0]
        for row in candidate.execute("SELECT name FROM sqlite_schema WHERE type='index'")
    }
    for name in names:
        pragmas: Iterable[str]
        if name in table_names:
            pragmas = ("table_info", "table_xinfo", "index_list", "foreign_key_list")
        elif name in index_names:
            pragmas = ("index_info", "index_xinfo")
        else:
            continue
        for pragma in pragmas:
            before = _pragma_rows(baseline, pragma, name)
            after = _pragma_rows(candidate, pragma, name)
            if before != after:
                raise CompatibilityError(f"PRAGMA {pragma}({name}) differs")


def _query_equal(
    baseline: sqlite3.Connection,
    candidate: sqlite3.Connection,
    sql: str,
    params: Sequence[Any] = (),
    *,
    label: str,
) -> None:
    before = baseline.execute(sql, params).fetchall()
    after = candidate.execute(sql, params).fetchall()
    if before != after:
        raise CompatibilityError(f"{label} differs: {before[:3]!r} != {after[:3]!r}")


def _sample_values(
    conn: sqlite3.Connection, table: str, column: str, *, limit: int = 4
) -> list[str]:
    quoted_column = _quote_identifier(column)
    quoted_table = _quote_identifier(table)
    return [
        row[0]
        for row in conn.execute(
            f"SELECT DISTINCT {quoted_column} FROM {quoted_table} "
            f"WHERE typeof({quoted_column})='text' AND {quoted_column} <> '' "
            f"ORDER BY {quoted_column} LIMIT ?",
            (limit,),
        )
    ]


def _fts_expression(term: str, *, prefix: bool = False) -> str:
    expression = '"' + term.replace('"', '""') + '"'
    return expression + ("*" if prefix else "")


def _compare_behavior(
    baseline: sqlite3.Connection, candidate: sqlite3.Connection
) -> None:
    for sql, label in (
        (
            "SELECT rowid, uuid, entry, reading_primary FROM entries "
            "ORDER BY entry COLLATE BINARY, rowid LIMIT 1000",
            "BINARY ordering",
        ),
        (
            "SELECT rowid, uuid, entry, reading_primary FROM entries "
            "ORDER BY entry COLLATE NOCASE, rowid LIMIT 1000",
            "NOCASE ordering",
        ),
        (
            "SELECT rowid, uuid, entry FROM entries WHERE is_heteronym=1 "
            "ORDER BY rowid LIMIT 100",
            "heteronym selection",
        ),
        (
            "SELECT rowid, entry_uuid, def_index, typeof(gloss), gloss "
            "FROM definitions WHERE gloss IS NULL OR gloss='' ORDER BY rowid LIMIT 100",
            "NULL and empty gloss selection",
        ),
        (
            "SELECT rowid, variant, entry, entry_uuid FROM variant_lookup "
            "ORDER BY variant COLLATE BINARY, rowid LIMIT 1000",
            "variant lookup ordering",
        ),
    ):
        _query_equal(baseline, candidate, sql, label=label)

    for column in ("uuid", "entry", "reading_primary"):
        for value in _sample_values(baseline, "entries", column):
            _query_equal(
                baseline,
                candidate,
                "SELECT rowid, uuid, entry, reading_primary, is_heteronym, "
                "ctype, ctype_source, ctype_confidence, freq_rank, needs_gloss, "
                "lookup_register FROM entries "
                f"WHERE {_quote_identifier(column)}=? ORDER BY rowid",
                (value,),
                label=f"entries {column} lookup {value!r}",
            )

    for value in _sample_values(baseline, "entries", "entry"):
        prefix = value[:1]
        _query_equal(
            baseline,
            candidate,
            "SELECT rowid, uuid, entry FROM entries WHERE entry>=? AND entry<? "
            "ORDER BY entry, rowid LIMIT 1000",
            (prefix, prefix + "\U0010ffff"),
            label=f"entry prefix {prefix!r}",
        )

    fts_probes = (
        (
            "fts_entries",
            "entry",
            "SELECT rowid, uuid, entry, reading_primary, "
            "snippet(fts_entries,1,'<b>','</b>','...',32), "
            "highlight(fts_entries,1,'<b>','</b>') "
            "FROM fts_entries WHERE fts_entries MATCH ? LIMIT 100",
        ),
        (
            "fts_definitions",
            "gloss",
            "SELECT rowid, entry_uuid, gloss, "
            "snippet(fts_definitions,1,'<b>','</b>','...',64), "
            "highlight(fts_definitions,1,'<b>','</b>') "
            "FROM fts_definitions WHERE fts_definitions MATCH ? LIMIT 100",
        ),
    )
    for table, column, sql in fts_probes:
        for term in _sample_values(baseline, table, column):
            for prefix in (False, True):
                expression = _fts_expression(term, prefix=prefix)
                _query_equal(
                    baseline,
                    candidate,
                    sql,
                    (expression,),
                    label=f"{table} {'prefix ' if prefix else ''}MATCH {term!r}",
                )


def _compare_query_plans(
    baseline: sqlite3.Connection, candidate: sqlite3.Connection
) -> None:
    probes = (
        ("SELECT raw_json FROM entries WHERE uuid=?", ("probe",)),
        ("SELECT raw_json FROM entries WHERE entry=? ORDER BY uuid", ("probe",)),
        (
            "SELECT raw_json FROM entries WHERE reading_primary=? ORDER BY entry",
            ("probe",),
        ),
        (
            "SELECT entry FROM entries WHERE entry>=? AND entry<?",
            ("語", "語\U0010ffff"),
        ),
        ("SELECT * FROM variant_lookup WHERE variant=?", ("probe",)),
        ("SELECT * FROM definitions WHERE entry_uuid=?", ("probe",)),
    )
    for sql, params in probes:
        before = baseline.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall()
        after = candidate.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall()
        if before != after:
            raise CompatibilityError(f"query plan differs for {sql!r}: {before} != {after}")


def compare_databases(
    baseline_path: Path, candidate_path: Path, *, batch_size: int = 512
) -> dict[str, Any]:
    """Compare two complete databases and return a serializable report."""
    baseline = _connect(baseline_path)
    candidate = _connect(candidate_path)
    try:
        validate_database(baseline, path=str(baseline_path))
        validate_database(candidate, path=str(candidate_path))
        before_schema = _schema_rows(baseline)
        after_schema = _schema_rows(candidate)
        if before_schema != after_schema:
            raise CompatibilityError("logical sqlite_schema differs")
        _compare_named_pragmas(baseline, candidate)

        counts = {
            table: _compare_table_rows(
                baseline, candidate, table, batch_size=batch_size
            )
            for table in PUBLIC_TABLES
        }
        _compare_behavior(baseline, candidate)
        _compare_query_plans(baseline, candidate)
        return {
            "compatible": True,
            "schema_version": SCHEMA_VERSION,
            "rows": counts,
            "baseline": asdict(collect_stats(baseline, baseline_path)),
            "candidate": asdict(collect_stats(candidate, candidate_path)),
        }
    finally:
        baseline.close()
        candidate.close()


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate or compare Genji schema-v3 SQLite databases"
    )
    parser.add_argument("baseline", nargs="?", type=Path)
    parser.add_argument("candidate", nargs="?", type=Path)
    parser.add_argument("--validate-only", type=Path, metavar="DB")
    parser.add_argument("--batch-size", type=int, default=512)
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args(argv)
    if args.validate_only:
        if args.baseline or args.candidate:
            parser.error("--validate-only cannot be combined with database arguments")
    elif not args.baseline or not args.candidate:
        parser.error("provide BASELINE CANDIDATE, or use --validate-only DB")
    if args.batch_size < 1:
        parser.error("--batch-size must be positive")
    return args


def main(argv: Sequence[str] | None = None) -> None:
    args = _parse_args(argv)
    try:
        if args.validate_only:
            conn = _connect(args.validate_only)
            try:
                validate_database(conn, path=str(args.validate_only))
                result: dict[str, Any] = {
                    "compatible": True,
                    "schema_version": SCHEMA_VERSION,
                    "database": asdict(collect_stats(conn, args.validate_only)),
                }
            finally:
                conn.close()
        else:
            result = compare_databases(
                args.baseline, args.candidate, batch_size=args.batch_size
            )
    except (CompatibilityError, sqlite3.DatabaseError) as exc:
        print(f"incompatible: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    if args.json_output:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    elif args.validate_only:
        stats = result["database"]
        print(
            f"compatible schema v3: {stats['path']} "
            f"({stats['bytes']} bytes, {stats['page_count']} pages)"
        )
    else:
        before = result["baseline"]
        after = result["candidate"]
        print("compatible schema v3")
        print(f"baseline:  {before['bytes']} bytes ({before['page_count']} pages)")
        print(f"candidate: {after['bytes']} bytes ({after['page_count']} pages)")
        print(f"saved:     {before['bytes'] - after['bytes']} bytes")


if __name__ == "__main__":
    main()
