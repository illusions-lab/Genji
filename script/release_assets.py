#!/usr/bin/env python3
"""Create and validate the metadata shipped with compressed Genji releases."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO


MANIFEST_VERSION = 1
REQUIRED_METADATA = (
    "version",
    "commit",
    "commit_short",
    "branch",
    "repository",
    "build_date",
    "entry_count",
    "schema_version",
)
COUNT_TABLES = {
    "entries": "entries",
    "definitions": "definitions",
    "variants": "variant_lookup",
    "ftsEntries": "fts_entries",
    "ftsDefinitions": "fts_definitions",
}


def _stream_digest(source: BinaryIO) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    for chunk in iter(lambda: source.read(1024 * 1024), b""):
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


def sha256(path: Path) -> str:
    with path.open("rb") as source:
        return _stream_digest(source)[0]


@dataclass(frozen=True)
class ZstdFrameInfo:
    window_size: int
    window_log: int
    checksum: bool
    content_size: int | None


def zstd_frame_info(path: Path) -> ZstdFrameInfo:
    """Parse the bounded header of one standard Zstandard frame."""
    with path.open("rb") as source:
        header = source.read(18)
    if len(header) < 5 or header[:4] != b"\x28\xb5\x2f\xfd":
        raise ValueError(f"{path} is not a standard Zstandard frame")
    descriptor = header[4]
    if descriptor & 0x08:
        raise ValueError("Zstandard frame has its reserved descriptor bit set")
    if descriptor & 0x10:
        raise ValueError("Zstandard frame has its unused descriptor bit set")
    single_segment = bool(descriptor & 0x20)
    checksum = bool(descriptor & 0x04)
    content_size_flag = descriptor >> 6
    dictionary_id_size = (0, 1, 2, 4)[descriptor & 0x03]
    content_size_size = (
        1 if single_segment and content_size_flag == 0 else 1 << content_size_flag
    ) if single_segment or content_size_flag else 0

    offset = 5
    if single_segment:
        window_size = None
    else:
        if len(header) <= offset:
            raise ValueError("truncated Zstandard frame header")
        window_descriptor = header[offset]
        offset += 1
        window_log = 10 + (window_descriptor >> 3)
        window_base = 1 << window_log
        window_size = window_base + (window_base >> 3) * (window_descriptor & 0x07)

    offset += dictionary_id_size
    required_size = offset + content_size_size
    if len(header) < required_size:
        raise ValueError("truncated Zstandard frame header")
    content_size = None
    if content_size_size:
        content_size = int.from_bytes(header[offset:required_size], "little")
        if content_size_size == 2:
            content_size += 256
    if single_segment:
        assert content_size is not None
        window_size = content_size
    assert window_size is not None
    effective_window_log = max(10, (max(window_size, 1) - 1).bit_length())
    return ZstdFrameInfo(window_size, effective_window_log, checksum, content_size)


def verify_zstd_frame(path: Path, maximum_window_log: int) -> ZstdFrameInfo:
    info = zstd_frame_info(path)
    if info.window_size > 1 << maximum_window_log:
        raise ValueError(
            f"Zstandard frame window size {info.window_size} exceeds "
            f"2^{maximum_window_log} bytes"
        )
    if not info.checksum:
        raise ValueError("Zstandard frame has no checksum")
    if info.content_size is None:
        raise ValueError("Zstandard frame has no content size")
    print(
        "Zstandard frame verified: "
        f"windowSize={info.window_size}, windowLog={info.window_log}, "
        "checksum, content size"
    )
    return info


def _zstd_uncompressed_digest(path: Path) -> tuple[str, int]:
    try:
        process = subprocess.Popen(
            ["zstd", "-q", "-d", "-c", str(path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as error:
        raise ValueError("zstd executable is required to validate the asset") from error
    assert process.stdout is not None
    digest = _stream_digest(process.stdout)
    stderr = process.communicate()[1]
    if process.returncode:
        raise ValueError(
            f"failed to decompress {path}: {stderr.decode(errors='replace').strip()}"
        )
    return digest


def _asset_uncompressed_digest(kind: str, path: Path) -> tuple[str, int]:
    if kind == "gzip":
        with gzip.open(path, "rb") as source:
            return _stream_digest(source)
    return _zstd_uncompressed_digest(path)


def _metadata(connection: sqlite3.Connection) -> dict[str, str]:
    try:
        rows = connection.execute("SELECT key, value FROM _metadata").fetchall()
    except sqlite3.Error as error:
        raise ValueError("database is missing a readable _metadata table") from error
    metadata = {str(key): str(value) for key, value in rows}
    missing = [key for key in REQUIRED_METADATA if not metadata.get(key)]
    if missing:
        raise ValueError(f"missing required _metadata values: {', '.join(missing)}")
    return metadata


def _schema_records(connection: sqlite3.Connection) -> list[dict[str, str | None]]:
    rows = connection.execute(
        """SELECT type, name, tbl_name, sql FROM sqlite_schema
           WHERE name NOT LIKE 'sqlite_%'
           ORDER BY type, name, tbl_name, COALESCE(sql, '')"""
    ).fetchall()
    return [
        {"type": row[0], "name": row[1], "table": row[2], "sql": row[3]}
        for row in rows
    ]


def schema_sha256(connection: sqlite3.Connection) -> str:
    encoded = json.dumps(
        _schema_records(connection),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _table_summary(connection: sqlite3.Connection, name: str, sql: str) -> dict:
    columns = []
    query = (
        "SELECT cid, name, type, [notnull], dflt_value, pk, hidden "
        "FROM pragma_table_xinfo(?) ORDER BY cid"
    )
    for row in connection.execute(query, (name,)):
        _, column_name, column_type, not_null, default, primary_key, hidden = row
        columns.append(
            {
                "name": column_name,
                "type": column_type,
                "nullable": not bool(not_null),
                "default": default,
                "primaryKey": primary_key,
                "hidden": hidden,
            }
        )
    foreign_keys = []
    query = (
        "SELECT id, seq, [table], [from], [to], on_update, on_delete, match "
        "FROM pragma_foreign_key_list(?) ORDER BY id, seq"
    )
    for row in connection.execute(query, (name,)):
        foreign_keys.append(
            {
                "id": row[0],
                "sequence": row[1],
                "table": row[2],
                "from": row[3],
                "to": row[4],
                "onUpdate": row[5],
                "onDelete": row[6],
                "match": row[7],
            }
        )
    return {
        "name": name,
        "type": (
            "virtual"
            if sql.lstrip().upper().startswith("CREATE VIRTUAL TABLE")
            else "table"
        ),
        "columns": columns,
        "foreignKeys": foreign_keys,
    }


def _tables(connection: sqlite3.Connection) -> list[dict]:
    rows = connection.execute(
        """SELECT name, COALESCE(sql, '') FROM sqlite_schema
           WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
           ORDER BY name"""
    ).fetchall()
    return [_table_summary(connection, name, sql) for name, sql in rows]


def _indexes(connection: sqlite3.Connection) -> list[dict]:
    indexes = []
    table_names = [
        row[0]
        for row in connection.execute(
            """SELECT name FROM sqlite_schema
               WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"""
        )
    ]
    for table_name in table_names:
        rows = connection.execute(
            "SELECT seq, name, [unique], origin, partial "
            "FROM pragma_index_list(?) ORDER BY name", (table_name,)
        ).fetchall()
        for _, index_name, unique, origin, partial in rows:
            columns = []
            query = (
                "SELECT seqno, cid, name, desc, coll, key "
                "FROM pragma_index_xinfo(?) ORDER BY seqno"
            )
            for row in connection.execute(query, (index_name,)):
                sequence, column_id, column_name, descending, collation, key = row
                columns.append(
                    {
                        "sequence": sequence,
                        "columnId": column_id,
                        "name": column_name,
                        "descending": bool(descending),
                        "collation": collation,
                        "key": bool(key),
                    }
                )
            indexes.append(
                {
                    "name": index_name,
                    "table": table_name,
                    "unique": bool(unique),
                    "origin": origin,
                    "partial": bool(partial),
                    "columns": columns,
                }
            )
    return sorted(indexes, key=lambda item: (item["table"], item["name"]))


def _count(connection: sqlite3.Connection, table: str, where: str = "") -> int:
    try:
        return connection.execute(f'SELECT COUNT(*) FROM "{table}" {where}').fetchone()[0]
    except sqlite3.Error as error:
        raise ValueError(f"database is missing required table {table}") from error


def database_metadata(database: Path, version: str) -> dict:
    database_uri = database.resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(database_uri, uri=True) as connection:
        metadata = _metadata(connection)
        if metadata["version"] != version:
            raise ValueError(
                f"release version {version!r} does not match _metadata version {metadata['version']!r}"
            )
        try:
            schema_version = int(metadata["schema_version"])
            expected_entries = int(metadata["entry_count"])
        except ValueError as error:
            raise ValueError("schema_version and entry_count must be integers") from error
        user_version = connection.execute("PRAGMA user_version").fetchone()[0]
        if user_version != schema_version:
            raise ValueError(
                f"PRAGMA user_version {user_version} does not match schema_version {schema_version}"
            )
        counts = {key: _count(connection, table) for key, table in COUNT_TABLES.items()}
        if counts["entries"] != expected_entries:
            raise ValueError(
                f"entry_count {expected_entries} does not match entries count {counts['entries']}"
            )
        if counts["ftsEntries"] != counts["entries"]:
            raise ValueError("fts_entries count does not match entries count")
        source_definitions = _count(connection, "definitions", "WHERE gloss IS NOT NULL")
        if counts["ftsDefinitions"] != source_definitions:
            raise ValueError("fts_definitions count does not match its definitions source")
        schema = {
            "version": schema_version,
            "sha256": schema_sha256(connection),
            "tables": _tables(connection),
            "indexes": _indexes(connection),
        }
    return {
        "sqliteUserVersion": user_version,
        "release": {
            "commit": metadata["commit"],
            "commitShort": metadata["commit_short"],
            "branch": metadata["branch"],
            "repository": metadata["repository"],
            "builtAt": metadata["build_date"],
        },
        "schema": schema,
        "counts": counts,
    }


def build_manifest(args: argparse.Namespace) -> dict:
    db = args.database.resolve()
    gzip_asset = args.gzip.resolve()
    zstd_asset = args.zstd.resolve()
    expected_names = (
        (db, "genji.db"),
        (gzip_asset, "genji.db.gz"),
        (zstd_asset, "genji.db.zst"),
    )
    for path, expected_name in expected_names:
        if path.name != expected_name:
            raise ValueError(f"release asset must be named {expected_name}, got {path.name}")
    db_info = database_metadata(db, args.version)
    db_sha256 = sha256(db)
    db_size = db.stat().st_size
    zstd_info = verify_zstd_frame(zstd_asset, 27)
    if zstd_info.content_size != db_size:
        raise ValueError("Zstandard frame content size does not match database size")

    assets = {}
    for kind, path in (("gzip", gzip_asset), ("zstd", zstd_asset)):
        uncompressed_sha256, uncompressed_size = _asset_uncompressed_digest(kind, path)
        if (uncompressed_sha256, uncompressed_size) != (db_sha256, db_size):
            raise ValueError(f"{path.name} does not decompress to the release database")
        assets[kind] = {
            "name": path.name,
            "format": kind,
            "size": path.stat().st_size,
            "sha256": sha256(path),
            "uncompressedSize": uncompressed_size,
            "uncompressedSha256": uncompressed_sha256,
        }
    assets["zstd"].update(
        {
            "windowSize": zstd_info.window_size,
            "windowLog": zstd_info.window_log,
            "checksum": zstd_info.checksum,
            "contentSize": zstd_info.content_size,
        }
    )
    return {
        "manifestVersion": MANIFEST_VERSION,
        "version": args.version,
        "sqliteUserVersion": db_info["sqliteUserVersion"],
        "release": db_info["release"],
        "database": {
            "name": db.name,
            "format": "sqlite3",
            "size": db_size,
            "sha256": db_sha256,
            "schema": db_info["schema"],
            "counts": db_info["counts"],
        },
        "assets": assets,
    }


def write_manifest(args: argparse.Namespace) -> None:
    manifest = build_manifest(args)
    args.output.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _checksum_entries(path: Path) -> dict[str, str]:
    entries: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split("  ", 1)
        if len(parts) != 2 or len(parts[0]) != 64:
            raise ValueError(f"invalid checksum line: {line!r}")
        digest, name = parts
        try:
            int(digest, 16)
        except ValueError as error:
            raise ValueError(f"invalid SHA-256 digest for {name}") from error
        if name in entries:
            raise ValueError(f"duplicate checksum entry: {name}")
        entries[name] = digest
    return entries


def verify_release(args: argparse.Namespace) -> None:
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    expected = build_manifest(
        argparse.Namespace(
            version=manifest.get("version"),
            database=args.database,
            gzip=args.gzip,
            zstd=args.zstd,
        )
    )
    if manifest != expected:
        raise ValueError("manifest does not match release database and assets")
    files = (args.gzip, args.zstd, args.manifest)
    checksum_entries = _checksum_entries(args.checksums)
    expected_names = {path.name for path in files}
    if set(checksum_entries) != expected_names:
        raise ValueError(
            "SHA256SUMS does not contain exactly the release assets and manifest"
        )
    for path in files:
        if checksum_entries[path.name].lower() != sha256(path):
            raise ValueError(f"SHA256SUMS digest mismatch for {path.name}")
    print("Release manifest, assets, database, and SHA256SUMS verified")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    frame = commands.add_parser("verify-zstd-frame")
    frame.add_argument("asset", type=Path)
    frame.add_argument("--maximum-window-log", type=int, default=27)
    manifest = commands.add_parser("manifest")
    manifest.add_argument("--version", required=True)
    manifest.add_argument("--database", type=Path, required=True)
    manifest.add_argument("--gzip", type=Path, required=True)
    manifest.add_argument("--zstd", type=Path, required=True)
    manifest.add_argument("--output", type=Path, required=True)
    verify = commands.add_parser("verify")
    verify.add_argument("--database", type=Path, required=True)
    verify.add_argument("--gzip", type=Path, required=True)
    verify.add_argument("--zstd", type=Path, required=True)
    verify.add_argument("--manifest", type=Path, required=True)
    verify.add_argument("--checksums", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.command == "verify-zstd-frame":
        verify_zstd_frame(args.asset, args.maximum_window_log)
    elif args.command == "manifest":
        write_manifest(args)
    else:
        verify_release(args)


if __name__ == "__main__":
    main()
