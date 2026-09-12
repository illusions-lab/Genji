from __future__ import annotations

import importlib.util
import gzip
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import mock_open, patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "release_assets", ROOT / "script" / "release_assets.py"
)
assert SPEC and SPEC.loader
release_assets = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = release_assets
SPEC.loader.exec_module(release_assets)

MAGIC = bytes.fromhex("28b52ffd")


def frame_header(
    descriptor: int,
    *,
    window_descriptor: int | None = None,
    dictionary_id: bytes = b"",
    content_size: bytes = b"",
) -> bytes:
    parts = [MAGIC, bytes([descriptor])]
    if window_descriptor is not None:
        parts.append(bytes([window_descriptor]))
    parts.extend((dictionary_id, content_size))
    return b"".join(parts)


class ZstdFrameTests(unittest.TestCase):
    def write_frame(self, root: str, contents: bytes) -> Path:
        path = Path(root) / "fixture.zst"
        path.write_bytes(contents)
        return path

    def test_reads_only_the_bounded_frame_header(self):
        header = frame_header(0x44, window_descriptor=17 << 3, content_size=b"\0\0")
        opened = mock_open(read_data=header)
        with patch.object(Path, "open", opened):
            info = release_assets.zstd_frame_info(Path("large.zst"))
        opened().read.assert_called_once_with(18)
        self.assertEqual(info.window_size, 1 << 27)

    def test_accepts_exactly_128_mib_and_rejects_window_mantissa_above_it(self):
        with tempfile.TemporaryDirectory() as root:
            exact = self.write_frame(
                root,
                frame_header(0x44, window_descriptor=17 << 3, content_size=b"\0\0"),
            )
            release_assets.verify_zstd_frame(exact, 27)
            oversized = self.write_frame(
                root,
                frame_header(
                    0x44,
                    window_descriptor=(17 << 3) | 1,
                    content_size=b"\0\0",
                ),
            )
            with self.assertRaisesRegex(ValueError, "window size"):
                release_assets.verify_zstd_frame(oversized, 27)

    def test_decodes_all_single_segment_content_size_widths(self):
        cases = (
            (0x24, 1, 42, 42),
            (0x64, 2, 0, 256),
            (0xA4, 4, 1234, 1234),
            (0xE4, 8, 1234, 1234),
        )
        with tempfile.TemporaryDirectory() as root:
            for descriptor, width, encoded, expected in cases:
                with self.subTest(width=width):
                    path = self.write_frame(
                        root,
                        frame_header(
                            descriptor,
                            content_size=encoded.to_bytes(width, "little"),
                        ),
                    )
                    info = release_assets.zstd_frame_info(path)
                    self.assertEqual(info.content_size, expected)
                    self.assertEqual(info.window_size, expected)

    def test_rejects_missing_integrity_fields(self):
        with tempfile.TemporaryDirectory() as root:
            no_checksum = self.write_frame(
                root,
                frame_header(0x40, window_descriptor=0, content_size=b"\0\0"),
            )
            with self.assertRaisesRegex(ValueError, "no checksum"):
                release_assets.verify_zstd_frame(no_checksum, 27)
            no_content_size = self.write_frame(
                root, frame_header(0x04, window_descriptor=0)
            )
            with self.assertRaisesRegex(ValueError, "no content size"):
                release_assets.verify_zstd_frame(no_content_size, 27)

    def test_rejects_invalid_or_truncated_headers(self):
        fixtures = {
            "bad magic": b"not-zstd",
            "reserved": frame_header(0x4C, window_descriptor=0, content_size=b"\0\0"),
            "unused": frame_header(0x54, window_descriptor=0, content_size=b"\0\0"),
            "truncated": MAGIC + bytes([0x44]),
        }
        with tempfile.TemporaryDirectory() as root:
            for name, contents in fixtures.items():
                with self.subTest(name=name):
                    path = self.write_frame(root, contents)
                    with self.assertRaises(ValueError):
                        release_assets.zstd_frame_info(path)


class ManifestTests(unittest.TestCase):
    def create_database(self, directory: Path, *, version: str = "test-version") -> Path:
        database = directory / "genji.db"
        with sqlite3.connect(database) as connection:
            connection.executescript("""
                PRAGMA user_version=3;
                CREATE TABLE entries(
                    uuid TEXT PRIMARY KEY,
                    entry TEXT NOT NULL,
                    reading_primary TEXT
                );
                CREATE TABLE definitions(
                    id INTEGER PRIMARY KEY,
                    entry_uuid TEXT NOT NULL REFERENCES entries(uuid),
                    gloss TEXT
                );
                CREATE TABLE variant_lookup(
                    variant TEXT NOT NULL,
                    entry TEXT NOT NULL,
                    entry_uuid TEXT NOT NULL
                );
                CREATE INDEX idx_entries_entry ON entries(entry);
                CREATE VIRTUAL TABLE fts_entries USING fts5(
                    uuid UNINDEXED, entry, reading_primary, tokenize='unicode61'
                );
                CREATE VIRTUAL TABLE fts_definitions USING fts5(
                    entry_uuid UNINDEXED, gloss, tokenize='unicode61'
                );
                CREATE TABLE _metadata(key TEXT PRIMARY KEY, value TEXT);
                INSERT INTO entries VALUES ('u1', '雪', 'ゆき');
                INSERT INTO definitions VALUES (1, 'u1', 'snow');
                INSERT INTO variant_lookup VALUES ('雪古', '雪', 'u1');
                INSERT INTO fts_entries VALUES ('u1', '雪', 'ゆき');
                INSERT INTO fts_definitions VALUES ('u1', 'snow');
            """)
            metadata = {
                "version": version,
                "commit": "0123456789abcdef",
                "commit_short": "01234567",
                "branch": "main",
                "repository": "https://example.test/Genji",
                "build_date": "2026-09-12T12:00:00Z",
                "entry_count": "1",
                "schema_version": "3",
            }
            connection.executemany(
                "INSERT INTO _metadata(key, value) VALUES (?, ?)", metadata.items()
            )
        return database

    def create_assets(self, directory: Path, database: Path) -> tuple[Path, Path]:
        gzip_asset = directory / "genji.db.gz"
        with database.open("rb") as source, gzip_asset.open("wb") as compressed:
            with gzip.GzipFile(filename="", mode="wb", fileobj=compressed, mtime=0) as target:
                shutil.copyfileobj(source, target)
        zstd_asset = directory / "genji.db.zst"
        subprocess.run(
            ["zstd", "-q", "-f", "--check", "--content-size", str(database), "-o", str(zstd_asset)],
            check=True,
        )
        return gzip_asset, zstd_asset

    def args(self, directory: Path, database: Path, gzip_asset: Path, zstd_asset: Path):
        return SimpleNamespace(
            version="test-version",
            database=database,
            gzip=gzip_asset,
            zstd=zstd_asset,
            output=directory / "genji-manifest.json",
        )

    @unittest.skipUnless(shutil.which("zstd"), "zstd executable is required")
    def test_manifest_records_database_schema_counts_and_assets(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            database = self.create_database(directory)
            gzip_asset, zstd_asset = self.create_assets(directory, database)
            args = self.args(directory, database, gzip_asset, zstd_asset)
            release_assets.write_manifest(args)
            manifest = json.loads(args.output.read_text(encoding="utf-8"))
            self.assertEqual(manifest["manifestVersion"], 1)
            self.assertEqual(manifest["version"], "test-version")
            self.assertEqual(manifest["sqliteUserVersion"], 3)
            self.assertEqual(manifest["release"]["commitShort"], "01234567")
            self.assertEqual(manifest["database"]["name"], "genji.db")
            self.assertEqual(manifest["database"]["format"], "sqlite3")
            self.assertEqual(manifest["database"]["size"], database.stat().st_size)
            self.assertEqual(
                manifest["database"]["sha256"], release_assets.sha256(database)
            )
            self.assertEqual(manifest["database"]["schema"]["version"], 3)
            self.assertEqual(manifest["database"]["counts"], {
                "entries": 1, "definitions": 1, "variants": 1,
                "ftsEntries": 1, "ftsDefinitions": 1,
            })
            table_types = {
                item["name"]: item["type"]
                for item in manifest["database"]["schema"]["tables"]
            }
            self.assertEqual(table_types["fts_entries"], "virtual")
            definitions = next(
                item for item in manifest["database"]["schema"]["tables"]
                if item["name"] == "definitions"
            )
            self.assertEqual(definitions["foreignKeys"][0]["table"], "entries")
            self.assertTrue(any(
                item["name"] == "idx_entries_entry"
                for item in manifest["database"]["schema"]["indexes"]
            ))
            self.assertEqual(manifest["assets"]["gzip"]["name"], "genji.db.gz")
            for kind in ("gzip", "zstd"):
                self.assertEqual(manifest["assets"][kind]["uncompressedSize"], database.stat().st_size)
                self.assertEqual(manifest["assets"][kind]["uncompressedSha256"], release_assets.sha256(database))
            self.assertTrue(manifest["assets"]["zstd"]["checksum"])
            self.assertEqual(manifest["assets"]["zstd"]["contentSize"], database.stat().st_size)

    def test_schema_fingerprint_is_deterministic_and_tracks_logical_changes(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            a_dir, b_dir = directory / "a", directory / "b"
            a_dir.mkdir()
            b_dir.mkdir()
            first = self.create_database(a_dir)
            second = self.create_database(b_dir)
            with sqlite3.connect(first) as connection:
                first_hash = release_assets.schema_sha256(connection)
            with sqlite3.connect(second) as connection:
                self.assertEqual(first_hash, release_assets.schema_sha256(connection))

            mutations = (
                "ALTER TABLE entries ADD COLUMN added TEXT",
                "CREATE INDEX idx_definitions_gloss ON definitions(gloss)",
                "CREATE TABLE citations(entry_uuid TEXT REFERENCES entries(uuid))",
                """DROP TABLE fts_entries;
                   CREATE VIRTUAL TABLE fts_entries USING fts5(
                       uuid UNINDEXED, entry, reading_primary, extra
                   );""",
            )
            for index, mutation in enumerate(mutations):
                with self.subTest(mutation=mutation):
                    changed_dir = directory / f"changed-{index}"
                    changed_dir.mkdir()
                    changed = self.create_database(changed_dir)
                    with sqlite3.connect(changed) as connection:
                        connection.executescript(mutation)
                        self.assertNotEqual(
                            first_hash, release_assets.schema_sha256(connection)
                        )

    def test_rejects_metadata_and_count_conflicts(self):
        cases = (
            ("DELETE FROM _metadata WHERE key='commit'", "missing required"),
            ("UPDATE _metadata SET value='other' WHERE key='version'", "release version"),
            ("UPDATE _metadata SET value='4' WHERE key='schema_version'", "user_version"),
            ("UPDATE _metadata SET value='2' WHERE key='entry_count'", "entry_count"),
            ("DELETE FROM fts_entries", "fts_entries"),
        )
        for mutation, message in cases:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as root:
                directory = Path(root)
                database = self.create_database(directory)
                with sqlite3.connect(database) as connection:
                    connection.execute(mutation)
                with self.assertRaisesRegex(ValueError, message):
                    release_assets.database_metadata(database, "test-version")

    @unittest.skipUnless(shutil.which("zstd"), "zstd executable is required")
    def test_checksums_are_standard_and_release_verification_detects_changes(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            database = self.create_database(directory)
            gzip_asset, zstd_asset = self.create_assets(directory, database)
            manifest_args = self.args(directory, database, gzip_asset, zstd_asset)
            release_assets.write_manifest(manifest_args)
            checksums = directory / "SHA256SUMS"
            files = (gzip_asset, zstd_asset, manifest_args.output)
            checksums.write_text("".join(
                f"{release_assets.sha256(path)}  {path.name}\n" for path in files
            ), encoding="utf-8")
            subprocess.run(["sha256sum", "--check", checksums.name], cwd=directory, check=True)
            verify_args = SimpleNamespace(
                database=database, gzip=gzip_asset, zstd=zstd_asset,
                manifest=manifest_args.output, checksums=checksums,
            )
            release_assets.verify_release(verify_args)
            checksums.write_text(checksums.read_text().replace("a", "b", 1))
            with self.assertRaises(ValueError):
                release_assets.verify_release(verify_args)


if __name__ == "__main__":
    unittest.main()
