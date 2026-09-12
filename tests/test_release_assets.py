from __future__ import annotations

import importlib.util
import json
import sqlite3
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
    def test_manifest_records_database_and_both_assets(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            database = directory / "genji.db"
            with sqlite3.connect(database) as connection:
                connection.execute("PRAGMA user_version=3")
                connection.execute("CREATE TABLE entries(entry TEXT)")
            gzip_asset = directory / "genji.db.gz"
            gzip_asset.write_bytes(b"gzip fixture")
            zstd_asset = directory / "genji.db.zst"
            zstd_asset.write_bytes(b"zstd fixture")
            output = directory / "genji-manifest.json"
            release_assets.write_manifest(
                SimpleNamespace(
                    version="test-version",
                    database=database,
                    gzip=gzip_asset,
                    zstd=zstd_asset,
                    output=output,
                )
            )
            manifest = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(manifest["version"], "test-version")
            self.assertEqual(manifest["sqliteUserVersion"], 3)
            self.assertEqual(manifest["database"]["size"], database.stat().st_size)
            self.assertEqual(
                manifest["database"]["sha256"], release_assets.sha256(database)
            )
            self.assertEqual(manifest["assets"]["gzip"]["name"], "genji.db.gz")
            self.assertEqual(
                manifest["assets"]["gzip"]["sha256"],
                release_assets.sha256(gzip_asset),
            )
            self.assertEqual(
                manifest["assets"]["zstd"]["sha256"],
                release_assets.sha256(zstd_asset),
            )
            self.assertEqual(manifest["assets"]["zstd"]["windowLog"], 27)


if __name__ == "__main__":
    unittest.main()
