#!/usr/bin/env python3
"""Create and validate the metadata shipped with compressed Genji releases."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class ZstdFrameInfo:
    window_size: int
    window_log: int
    checksum: bool
    content_size: int | None


def zstd_frame_info(path: Path) -> ZstdFrameInfo:
    """Parse the bounded header of one standard Zstandard frame.

    This deliberately reads only the frame header so CI does not depend on the
    presentation format of ``zstd --list``.
    """
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


def verify_zstd_frame(path: Path, maximum_window_log: int) -> None:
    info = zstd_frame_info(path)
    maximum_window_size = 1 << maximum_window_log
    if info.window_size > maximum_window_size:
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
