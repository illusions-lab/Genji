#!/usr/bin/env python3
"""Create and validate the metadata shipped with compressed Genji releases."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def zstd_frame_info(path: Path) -> tuple[int, bool, bool]:
    """Return ``(window_log, checksum, content_size)`` for one standard frame.

    This deliberately reads only the frame header so CI does not depend on the
    presentation format of ``zstd --list``.
    """
    header = path.read_bytes()[:18]
    if len(header) < 6 or header[:4] != b"\x28\xb5\x2f\xfd":
        raise ValueError(f"{path} is not a standard Zstandard frame")
    descriptor = header[4]
    if descriptor & 0x08:
        raise ValueError("Zstandard frame has its reserved descriptor bit set")
    single_segment = bool(descriptor & 0x20)
    checksum = bool(descriptor & 0x04)
    content_size_flag = descriptor >> 6
    content_size = single_segment or content_size_flag != 0
    if not content_size:
        raise ValueError("Zstandard frame has no content size")
    if single_segment:
        # A single-segment frame has no Window_Descriptor.  Its FCS is also
        # necessarily its (no larger) window, so its effective log is bounded.
        fcs_size = (1, 2, 4, 8)[content_size_flag]
        if len(header) < 5 + 1 + fcs_size:
            raise ValueError("truncated Zstandard frame header")
        # Dictionary ID precedes FCS; account for its encoded size.
        dict_id_size = (0, 1, 2, 4)[descriptor & 0x03]
        offset = 5 + dict_id_size
        value = int.from_bytes(header[offset : offset + fcs_size], "little")
        return max(10, (max(value, 1) - 1).bit_length()), checksum, content_size
    window_descriptor = header[5]
    return 10 + (window_descriptor >> 3), checksum, content_size


def verify_zstd_frame(path: Path, maximum_window_log: int) -> None:
    window_log, checksum, content_size = zstd_frame_info(path)
    if window_log > maximum_window_log:
        raise ValueError(
            f"Zstandard frame windowLog {window_log} exceeds {maximum_window_log}"
        )
    if not checksum:
        raise ValueError("Zstandard frame has no checksum")
    if not content_size:
        raise ValueError("Zstandard frame has no content size")
    print(f"Zstandard frame verified: windowLog={window_log}, checksum, content size")


def write_manifest(args: argparse.Namespace) -> None:
    db = args.database.resolve()
    gzip_asset = args.gzip.resolve()
    zstd_asset = args.zstd.resolve()
    with sqlite3.connect(db) as connection:
        user_version = connection.execute("PRAGMA user_version").fetchone()[0]
    manifest = {
        "version": args.version,
        "sqliteUserVersion": user_version,
        "database": {"size": db.stat().st_size, "sha256": sha256(db)},
        "assets": {
            "gzip": {
                "name": "genji.db.gz",
                "format": "gzip",
                "size": gzip_asset.stat().st_size,
                "sha256": sha256(gzip_asset),
            },
            "zstd": {
                "name": "genji.db.zst",
                "format": "zstd",
                "size": zstd_asset.stat().st_size,
                "sha256": sha256(zstd_asset),
                "windowLog": 27,
            },
        },
    }
    args.output.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


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
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.command == "verify-zstd-frame":
        verify_zstd_frame(args.asset, args.maximum_window_log)
    else:
        write_manifest(args)


if __name__ == "__main__":
    main()
