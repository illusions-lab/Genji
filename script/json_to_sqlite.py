#!/usr/bin/env python3
"""
JSON データを SQLite データベースに変換するスクリプト

data/ 以下の全 JSON ファイルを読み込み、単一の SQLite DB にまとめる。
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from check_data_quality import audit

_SCRIPT_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPT_DIR.parent
_DATA_DIR = _REPO_ROOT / "data"
_PENDING_DIR = _REPO_ROOT / "pending" / "needs_reading"

class BuildOptions:
    """Options used by reproducible compatibility/size experiments.

    The public invocation needs no arguments and continues to write ``genji.db``.
    The command-line switches are intentionally build-only controls used by tests
    and release measurements; none of them changes the schema contract.
    """

    def __init__(
        self,
        *,
        output_path: Path = _REPO_ROOT / "genji.db",
        data_dir: Path = _DATA_DIR,
        pending_dir: Path = _PENDING_DIR,
        json_mode: str = "compact",
        page_size: int = 4096,
        build_date: str | None = None,
        fts_optimize: bool = True,
        build_journal: str = "WAL",
        exclusive_locking: bool = False,
        finalize_order: str = "analyze-vacuum",
        check_quality: bool = True,
    ) -> None:
        self.output_path = output_path
        self.data_dir = data_dir
        self.pending_dir = pending_dir
        self.json_mode = json_mode
        self.page_size = page_size
        self.build_date = build_date
        self.fts_optimize = fts_optimize
        self.build_journal = build_journal
        self.exclusive_locking = exclusive_locking
        self.finalize_order = finalize_order
        self.check_quality = check_quality


def serialize_json(value: Any, *, compact: bool = True) -> str:
    """Serialize a JSON value without changing its semantic content or order."""
    kwargs: dict[str, Any] = {"ensure_ascii": False}
    if compact:
        kwargs["separators"] = (",", ":")
    return json.dumps(value, **kwargs)


def _git(args: list[str]) -> str:
    """Run a git command and return stripped stdout, or empty string on failure."""
    try:
        return subprocess.check_output(
            ["git", *args], cwd=_REPO_ROOT, stderr=subprocess.DEVNULL, text=True
        ).strip()
    except Exception:
        return ""


def create_metadata(
    conn: sqlite3.Connection, entry_count: int, *, build_date: str | None = None
) -> None:
    """Create and populate the build metadata table."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS _metadata (
            key   TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    version = os.environ.get("GENJI_VERSION") or _git(["describe", "--tags", "--always"])
    commit = os.environ.get("GENJI_COMMIT") or _git(["rev-parse", "HEAD"])
    commit_short = commit[:8] if commit else ""
    branch = os.environ.get("GENJI_BRANCH") or _git(["rev-parse", "--abbrev-ref", "HEAD"])
    repo = os.environ.get("GENJI_REPO") or _git(["remote", "get-url", "origin"])
    build_date = (
        build_date
        or os.environ.get("GENJI_BUILD_DATE")
        or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    )

    rows = [
        ("version", version),
        ("commit", commit),
        ("commit_short", commit_short),
        ("branch", branch),
        ("repository", repo),
        ("build_date", build_date),
        ("entry_count", str(entry_count)),
        ("schema_version", "3"),
    ]
    conn.executemany(
        "INSERT OR REPLACE INTO _metadata (key, value) VALUES (?, ?)", rows
    )
    print(f"Metadata: version={version} commit={commit_short} branch={branch}")


def create_fts(conn: sqlite3.Connection, *, optimize: bool = False) -> None:
    """全文検索（FTS5）テーブルを作成する"""
    print("FTS5 テーブルを作成中...")
    conn.executescript("""
        CREATE VIRTUAL TABLE IF NOT EXISTS fts_entries USING fts5(
            uuid UNINDEXED,
            entry,
            reading_primary,
            tokenize='unicode61'
        );

        INSERT INTO fts_entries(uuid, entry, reading_primary)
        SELECT uuid, entry, reading_primary FROM entries;

        CREATE VIRTUAL TABLE IF NOT EXISTS fts_definitions USING fts5(
            entry_uuid UNINDEXED,
            gloss,
            tokenize='unicode61'
        );

        INSERT INTO fts_definitions(entry_uuid, gloss)
        SELECT entry_uuid, gloss FROM definitions WHERE gloss IS NOT NULL;
    """)
    if optimize:
        conn.execute("INSERT INTO fts_entries(fts_entries) VALUES('optimize')")
        conn.execute("INSERT INTO fts_definitions(fts_definitions) VALUES('optimize')")
    print("FTS5 テーブル作成完了")


def create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS entries (
            uuid            TEXT PRIMARY KEY,
            entry           TEXT NOT NULL,
            reading_primary TEXT,
            reading_alternatives TEXT,  -- JSON array
            is_heteronym    INTEGER DEFAULT 0,
            pos             TEXT,       -- JSON array
            ctype           TEXT,
            ctype_source    TEXT,
            ctype_confidence TEXT,
            inflections     TEXT,       -- JSON
            relations       TEXT,       -- JSON
            meta            TEXT,       -- JSON
            raw_json        TEXT NOT NULL,
            freq_rank       INTEGER,
            needs_gloss     INTEGER NOT NULL DEFAULT 0,
            lookup_register TEXT
        );

        CREATE TABLE IF NOT EXISTS definitions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_uuid      TEXT NOT NULL REFERENCES entries(uuid),
            def_index       INTEGER,
            gloss           TEXT,
            register        TEXT,
            nuance          TEXT,
            scenarios       TEXT,       -- JSON array
            sensory_tags    TEXT,       -- JSON
            collocations    TEXT,       -- JSON array
            examples        TEXT,       -- JSON
            UNIQUE(entry_uuid, def_index)
        );

        CREATE INDEX IF NOT EXISTS idx_entries_entry ON entries(entry);
        CREATE INDEX IF NOT EXISTS idx_entries_entry_nocase ON entries(entry COLLATE NOCASE);
        CREATE INDEX IF NOT EXISTS idx_entries_reading ON entries(reading_primary);
        CREATE INDEX IF NOT EXISTS idx_definitions_uuid ON definitions(entry_uuid);
        CREATE TABLE IF NOT EXISTS variant_lookup (
            variant TEXT NOT NULL,
            entry TEXT NOT NULL,
            entry_uuid TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_variant_lookup_variant ON variant_lookup(variant);

        PRAGMA user_version = 3;
    """)


def insert_entry(
    conn: sqlite3.Connection, item: dict[str, Any], *, compact_json: bool = True
) -> None:
    reading = item.get("reading", {})
    grammar = item.get("grammar", {})

    conn.execute(
        """INSERT OR REPLACE INTO entries
           (uuid, entry, reading_primary, reading_alternatives, is_heteronym,
            pos, ctype, ctype_source, ctype_confidence,
            inflections, relations, meta, raw_json,
            freq_rank, needs_gloss, lookup_register)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            item.get("uuid"),
            item.get("entry"),
            reading.get("primary"),
            serialize_json(reading.get("alternatives", []), compact=compact_json),
            1 if reading.get("is_heteronym") else 0,
            serialize_json(grammar.get("pos", []), compact=compact_json),
            grammar.get("ctype"),
            grammar.get("ctype_source"),
            grammar.get("ctype_confidence"),
            serialize_json(grammar.get("inflections"), compact=compact_json)
            if grammar.get("inflections")
            else None,
            serialize_json(item.get("relations", {}), compact=compact_json),
            serialize_json(item.get("meta", {}), compact=compact_json),
            serialize_json(item, compact=compact_json),
            item.get("meta", {}).get("freq_rank"),
            1 if item.get("meta", {}).get("needs_gloss") else 0,
            next((d.get("register") for d in item.get("definitions", []) if d.get("register")), None),
        ),
    )

    conn.execute("DELETE FROM variant_lookup WHERE entry_uuid = ?", (item.get("uuid"),))
    conn.executemany(
        "INSERT INTO variant_lookup(variant, entry, entry_uuid) VALUES (?, ?, ?)",
        [(variant, item.get("entry"), item.get("uuid")) for variant in item.get("meta", {}).get("variant_writings", [])],
    )

    for defn in item.get("definitions", []):
        conn.execute(
            """INSERT OR REPLACE INTO definitions
               (entry_uuid, def_index, gloss, register, nuance,
                scenarios, sensory_tags, collocations, examples)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                item.get("uuid"),
                defn.get("index"),
                defn.get("gloss"),
                defn.get("register"),
                defn.get("nuance"),
                serialize_json(defn.get("scenarios", []), compact=compact_json),
                serialize_json(defn.get("sensory_tags", {}), compact=compact_json),
                serialize_json(defn.get("collocations", []), compact=compact_json),
                serialize_json(defn.get("examples", {}), compact=compact_json),
            ),
        )


def _validate_database(conn: sqlite3.Connection) -> None:
    """Run the final release integrity and planner checks."""
    if conn.execute("PRAGMA user_version").fetchone()[0] != 3:
        raise RuntimeError("SQLite user_version is not 3")
    if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise RuntimeError("SQLite integrity_check failed")
    if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise RuntimeError("SQLite foreign_key_check failed")
    plan = " ".join(
        row[3]
        for row in conn.execute(
            "EXPLAIN QUERY PLAN "
            "SELECT entry FROM entries WHERE entry >= ? AND entry < ?",
            ("語", "語\U0010ffff"),
        )
    )
    if "SCAN entries" in plan:
        raise RuntimeError(f"prefix query lost its index: {plan}")


def _set_build_pragmas(conn: sqlite3.Connection, options: BuildOptions) -> None:
    if options.page_size not in (4096, 8192, 16384):
        raise ValueError(f"unsupported page size: {options.page_size}")
    if options.build_journal not in ("WAL", "MEMORY"):
        raise ValueError(f"unsupported build journal: {options.build_journal}")
    conn.execute(f"PRAGMA page_size={options.page_size}")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(f"PRAGMA journal_mode={options.build_journal}")
    conn.execute("PRAGMA synchronous=OFF")
    if options.exclusive_locking:
        conn.execute("PRAGMA locking_mode=EXCLUSIVE")


def build_database(options: BuildOptions) -> tuple[int, int]:
    """Build a schema-v3 database and return ``(entry_count, file_errors)``."""
    # 發布資料庫只收錄 data/；pending 的完整性由 CI/`make quality` 檢查。
    if options.check_quality:
        quality = audit(
            options.data_dir, options.pending_dir, fix=False, include_pending=False
        )
        if quality.error_count:
            print(
                "Dictionary quality check failed; SQLite was not rebuilt.",
                file=sys.stderr,
            )
            for code, count in quality.issue_counts.most_common():
                print(f"  {code}: {count}", file=sys.stderr)
            print("Run: python3 script/check_data_quality.py", file=sys.stderr)
            raise RuntimeError("dictionary quality check failed")

    output_path = options.output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        output_path.unlink()

    json_files = sorted(options.data_dir.rglob("*.json"))
    if not json_files:
        raise RuntimeError(f"No JSON files found in {options.data_dir}")

    print(f"Found {len(json_files)} JSON files")

    conn = sqlite3.connect(str(output_path))
    _set_build_pragmas(conn, options)
    create_schema(conn)

    count = 0
    errors = 0
    conn.execute("BEGIN")
    for i, f in enumerate(json_files, 1):
        try:
            items = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(items, dict):
                items = [items]
            for item in items:
                insert_entry(
                    conn, item, compact_json=(options.json_mode == "compact")
                )
                count += 1
        except Exception as e:
            errors += 1
            if errors <= 10:
                print(f"Warning: {f}: {e}", file=sys.stderr)

        if i % 10000 == 0:
            conn.execute("COMMIT")
            conn.execute("BEGIN")
            print(f"  Processed {i}/{len(json_files)} files ({count} entries)")

    conn.execute("COMMIT")
    create_metadata(conn, count, build_date=options.build_date)
    create_fts(conn, optimize=options.fts_optimize)
    conn.execute("PRAGMA user_version = 3")
    conn.commit()
    if options.finalize_order == "analyze-vacuum":
        conn.execute("ANALYZE")
        conn.commit()
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("PRAGMA locking_mode=NORMAL")
        conn.execute("VACUUM")
    elif options.finalize_order == "vacuum-analyze":
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("PRAGMA locking_mode=NORMAL")
        conn.execute("VACUUM")
        conn.execute("ANALYZE")
    elif options.finalize_order == "analyze-vacuum-analyze":
        conn.execute("ANALYZE")
        conn.commit()
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("PRAGMA locking_mode=NORMAL")
        conn.execute("VACUUM")
        conn.execute("ANALYZE")
    else:
        raise ValueError(f"unsupported finalize order: {options.finalize_order}")
    conn.commit()
    _validate_database(conn)
    conn.close()

    size_mb = output_path.stat().st_size / (1024 * 1024)
    print(f"Done: {count} entries written to {output_path.name} ({size_mb:.1f} MB)")
    if errors:
        print(f"  ({errors} files had errors)")
    return count, errors


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build the schema-v3 Genji SQLite dictionary"
    )
    parser.add_argument("--output", type=Path, default=_REPO_ROOT / "genji.db")
    parser.add_argument("--data-dir", type=Path, default=_DATA_DIR)
    parser.add_argument("--pending-dir", type=Path, default=_PENDING_DIR)
    parser.add_argument("--json-mode", choices=("compact", "legacy"), default="compact")
    parser.add_argument("--page-size", type=int, choices=(4096, 8192, 16384), default=4096)
    parser.add_argument("--build-date", help="fixed ISO-8601 metadata timestamp")
    parser.add_argument(
        "--fts-optimize", action=argparse.BooleanOptionalAction, default=True
    )
    parser.add_argument("--build-journal", choices=("WAL", "MEMORY"), default="WAL")
    parser.add_argument("--exclusive-locking", action="store_true")
    parser.add_argument(
        "--finalize-order",
        choices=("analyze-vacuum", "vacuum-analyze", "analyze-vacuum-analyze"),
        default="analyze-vacuum",
    )
    parser.add_argument("--skip-quality", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    options = BuildOptions(
        output_path=args.output,
        data_dir=args.data_dir,
        pending_dir=args.pending_dir,
        json_mode=args.json_mode,
        page_size=args.page_size,
        build_date=args.build_date,
        fts_optimize=args.fts_optimize,
        build_journal=args.build_journal,
        exclusive_locking=args.exclusive_locking,
        finalize_order=args.finalize_order,
        check_quality=not args.skip_quality,
    )
    try:
        build_database(options)
    except (RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
