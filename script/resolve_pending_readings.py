#!/usr/bin/env python3
"""Resolve pending readings from exact, independently verifiable sources.

The resolver is deliberately conservative.  It never derives a compound reading
from individual kanji and it never asks a morphological analyser or a language
model to guess.  A row is eligible only when a complete spelling has one
applicable reading in formal data, JMdict/JMnedict, or when the same Aozora ruby
occurs in at least two different text files.

The default mode is read-only.  ``--apply`` requires a JSON report and a UUID
migration ledger; the CSV report is written next to the JSON report unless an
explicit path is supplied.
"""

from __future__ import annotations

import argparse
import bz2
import copy
import csv
import gzip
import hashlib
import io
import json
import lzma
import re
import sys
import unicodedata
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Iterator

from check_data_quality import DEFAULT_DATA, DEFAULT_PENDING, _remove_empty_parents
from create_entries import _apply_kyuji
from dictionary_rules import (
    compute_uuid_v5,
    expected_data_path,
    forbidden_identifier_chars,
    invalid_reading_chars,
    is_valid_reading,
)


STATUS_RESOLVED = "resolved"
STATUS_UNMATCHED = "unmatched"
STATUS_AMBIGUOUS = "ambiguous"
STATUS_CONFLICT = "source_conflict"
STATUS_SINGLE_RUBY = "single_ruby"
STATUS_FRAGMENT = "suspected_fragment"
STATUS_INVALID = "invalid_unicode"

_XML_BUILTINS = rb"(?:amp|lt|gt|quot|apos)"
_UNKNOWN_ENTITY_RE = re.compile(rb"&(?!(?:" + _XML_BUILTINS + rb");)[A-Za-z_][\w.:-]*;")
_DOCTYPE_RE = re.compile(rb"<!DOCTYPE(?:[^>\[]|\[(?:[^\]]|\](?!>))*\])*>", re.DOTALL)
_EXPLICIT_RUBY_RE = re.compile(r"｜([^｜《》\r\n]+)《([^《》\r\n]+)》")
_IMPLICIT_RUBY_RE = re.compile(r"([々〆ヶヵ〻\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+)《([^《》\r\n]+)》")
_TEXT_SUFFIXES = frozenset({".txt", ".text"})
_PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _nfc(value: str) -> str:
    return unicodedata.normalize("NFC", value.strip())


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _ledger_path(path: str) -> str:
    value = Path(path)
    try:
        return str(value.resolve().relative_to(_PROJECT_ROOT))
    except ValueError:
        return str(value)


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        temporary.replace(path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _open_compressed(path: Path) -> BinaryIO:
    suffix = path.suffix.lower()
    if suffix == ".gz":
        return gzip.open(path, "rb")
    if suffix == ".bz2":
        return bz2.open(path, "rb")
    if suffix in {".xz", ".lzma"}:
        return lzma.open(path, "rb")
    return path.open("rb")


def _xml_entry_iterator(path: Path) -> Iterator[ET.Element]:
    """Yield entry elements, retrying with harmless entity removal if needed."""
    try:
        with _open_compressed(path) as stream:
            for _, element in ET.iterparse(stream, events=("end",)):
                if element.tag.rsplit("}", 1)[-1] == "entry":
                    yield element
                    element.clear()
        return
    except ET.ParseError:
        # JMdict distributions sometimes refer to an external DTD.  Entity text
        # occurs in sense metadata, which this resolver intentionally ignores.
        with _open_compressed(path) as stream:
            raw = stream.read()
        raw = _DOCTYPE_RE.sub(b"", raw)
        raw = _UNKNOWN_ENTITY_RE.sub(b"", raw)
        for _, element in ET.iterparse(io.BytesIO(raw), events=("end",)):
            if element.tag.rsplit("}", 1)[-1] == "entry":
                yield element
                element.clear()


def _children(element: ET.Element, name: str) -> Iterator[ET.Element]:
    for child in element:
        if child.tag.rsplit("}", 1)[-1] == name:
            yield child


def _child_text(element: ET.Element, name: str) -> str | None:
    child = next(_children(element, name), None)
    if child is None or child.text is None:
        return None
    value = _nfc(child.text)
    return value or None


@dataclass
class SourceIndex:
    """Exact spelling -> reading -> stable evidence identifiers."""

    source: str
    values: dict[str, dict[str, set[str]]] = field(
        default_factory=lambda: defaultdict(lambda: defaultdict(set))
    )
    invalid: dict[str, list[dict[str, str]]] = field(default_factory=lambda: defaultdict(list))

    def add(self, entry: str, reading: str, evidence: str) -> None:
        entry, reading = _nfc(entry), _nfc(reading)
        if not entry or not reading:
            return
        if is_valid_reading(reading):
            self.values[entry][reading].add(evidence)
        else:
            self.invalid[entry].append({
                "reading": reading,
                "evidence": evidence,
                "invalid_characters": [f"U+{ord(char):04X}" for char in invalid_reading_chars(reading)],
            })


def parse_edrdg(path: Path, source: str, wanted: set[str] | None = None) -> SourceIndex:
    """Parse full-spelling reading mappings shared by JMdict and JMnedict."""
    index = SourceIndex(source)
    for element in _xml_entry_iterator(path):
        sequence = _child_text(element, "ent_seq") or "unknown"
        spellings: list[str] = []
        for k_ele in _children(element, "k_ele"):
            keb = _child_text(k_ele, "keb")
            if keb:
                spellings.append(keb)
        if not spellings:
            continue
        for position, r_ele in enumerate(_children(element, "r_ele"), 1):
            reading = _child_text(r_ele, "reb")
            if not reading:
                continue
            restrictions = {
                text for child in _children(r_ele, "re_restr")
                if (text := _nfc(child.text or ""))
            }
            reading_has_no_kanji = next(_children(r_ele, "re_nokanji"), None) is not None
            applicable = [] if reading_has_no_kanji else [
                spelling for spelling in spellings if not restrictions or spelling in restrictions
            ]
            for spelling in applicable:
                if wanted is None or spelling in wanted:
                    index.add(spelling, reading, f"{source}:{sequence}:r{position}")
    return index


def build_formal_index(data_root: Path, wanted: set[str] | None = None) -> SourceIndex:
    index = SourceIndex("formal")
    if not data_root.exists():
        return index
    for path in sorted(data_root.rglob("*.json")):
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        rows = loaded if isinstance(loaded, list) else [loaded]
        for row_number, item in enumerate(rows, 1):
            if not isinstance(item, dict):
                continue
            entry = item.get("entry")
            reading_block = item.get("reading")
            reading = reading_block.get("primary") if isinstance(reading_block, dict) else None
            if not isinstance(entry, str) or not isinstance(reading, str):
                continue
            evidence = f"formal:{path.relative_to(data_root)}#{row_number}"
            if wanted is None or entry in wanted:
                index.add(entry, reading, evidence)
            meta = item.get("meta")
            variants = meta.get("variant_writings") if isinstance(meta, dict) else None
            if isinstance(variants, list):
                for variant in variants:
                    if isinstance(variant, str) and (wanted is None or variant in wanted):
                        index.add(variant, reading, evidence + ":variant")
    return index


def extract_aozora_ruby(text: str) -> Iterator[tuple[str, str]]:
    """Yield explicit and legal implicit Aozora ruby pairs from one text."""
    explicit_spans: list[tuple[int, int]] = []
    for match in _EXPLICIT_RUBY_RE.finditer(text):
        explicit_spans.append(match.span())
        yield _nfc(match.group(1)), _nfc(match.group(2))
    for match in _IMPLICIT_RUBY_RE.finditer(text):
        if match.start() > 0 and text[match.start() - 1] == "｜":
            continue
        if any(start <= match.start() < end for start, end in explicit_spans):
            continue
        yield _nfc(match.group(1)), _nfc(match.group(2))


def build_aozora_index(root: Path, wanted: set[str] | None = None) -> SourceIndex:
    index = SourceIndex("aozora_ruby")
    if not root.exists():
        return index
    files = [root] if root.is_file() else sorted(
        path for path in root.rglob("*") if path.is_file() and path.suffix.lower() in _TEXT_SUFFIXES
    )
    for path in files:
        try:
            text = path.read_text(encoding="utf-8-sig", errors="strict")
        except UnicodeError:
            try:
                text = path.read_text(encoding="shift_jis", errors="replace")
            except OSError:
                continue
        except OSError:
            continue
        reference = str(path.relative_to(root)) if root.is_dir() else path.name
        seen_in_file: set[tuple[str, str]] = set()
        for entry, reading in extract_aozora_ruby(text):
            if wanted is not None and entry not in wanted:
                continue
            pair = (entry, reading)
            if pair in seen_in_file:
                continue
            seen_in_file.add(pair)
            index.add(entry, reading, f"aozora:{reference}")
    return index


def _load_pending(pending_root: Path) -> list[tuple[Path, int, object]]:
    rows: list[tuple[Path, int, object]] = []
    if not pending_root.exists():
        return rows
    for path in sorted(pending_root.rglob("*.json")):
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            rows.append((path, 1, {"entry": "", "_load_error": str(exc)}))
            continue
        values = loaded if isinstance(loaded, list) else [loaded]
        rows.extend((path, position, item) for position, item in enumerate(values, 1))
    return rows


def _entry_unicode_invalid(entry: object) -> bool:
    if not isinstance(entry, str) or not entry or entry != entry.strip():
        return True
    if forbidden_identifier_chars(entry) or any(char in entry for char in ("゛", "゜")):
        return True
    previous_is_kana = False
    for char in entry:
        if unicodedata.category(char) in {"Mn", "Mc"} and not previous_is_kana:
            return True
        name = unicodedata.name(char, "")
        previous_is_kana = name.startswith("HIRAGANA LETTER ") or name.startswith("KATAKANA LETTER ")
    return False


def _is_japanese_lexical_char(char: str) -> bool:
    # Common one-character particles mark a word boundary in unsegmented prose.
    if char in "はがをにへとのもやぞねよか":
        return False
    name = unicodedata.name(char, "")
    return (
        "IDEOGRAPH" in name or
        name.startswith("HIRAGANA LETTER ") or
        name.startswith("KATAKANA LETTER ") or
        char in "々〆ヶヵ〻"
    )


def _example_texts(item: object) -> Iterator[str]:
    if not isinstance(item, dict):
        return
    definitions = item.get("definitions")
    if not isinstance(definitions, list):
        return
    for definition in definitions:
        examples = definition.get("examples") if isinstance(definition, dict) else None
        if not isinstance(examples, dict):
            continue
        for values in examples.values():
            if not isinstance(values, list):
                continue
            for example in values:
                text = example.get("text") if isinstance(example, dict) else None
                if isinstance(text, str):
                    yield text


def _suspected_fragment(entry: str, item: object = None) -> bool:
    entry = _apply_kyuji(entry)
    if len(entry) == 1:
        name = unicodedata.name(entry, "")
        if name.startswith("HIRAGANA LETTER ") or name.startswith("KATAKANA LETTER "):
            return True
    has_ideograph = any("IDEOGRAPH" in unicodedata.name(char, "") for char in entry)
    # Mixed-script okurigana endings are often tokenizer stems in this pending
    # corpus.  This is a reporting hint only and never enables promotion.
    if has_ideograph and bool(re.search(r"[ぁ-ゖ]{1,2}$", entry)):
        return True

    # If every observed occurrence is joined to Japanese letters on at least
    # one side, the extracted form is probably a stem or a piece of a compound
    # (e.g. 帰 in 帰って, 尖 in 尖端).  Lack of examples is not evidence.
    embedded = 0
    standalone = 0
    for text in _example_texts(item):
        text = _apply_kyuji(text)
        start = 0
        while (position := text.find(entry, start)) >= 0:
            left = text[position - 1] if position else ""
            end = position + len(entry)
            right = text[end] if end < len(text) else ""
            if ((entry.startswith("云") and left == "と") or
                    (left and _is_japanese_lexical_char(left)) or
                    (right and _is_japanese_lexical_char(right))):
                embedded += 1
            else:
                standalone += 1
            start = position + max(1, len(entry))
    return embedded > standalone


def _frequency(item: object) -> int | float:
    if not isinstance(item, dict):
        return 0
    meta = item.get("meta")
    frequencies = meta.get("frequencies") if isinstance(meta, dict) else None
    value = frequencies.get("aozora", 0) if isinstance(frequencies, dict) else 0
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def _candidate_rows(entry: str, indexes: list[SourceIndex]) -> list[dict]:
    grouped: dict[str, dict[str, object]] = {}
    for index in indexes:
        for reading, evidence in index.values.get(entry, {}).items():
            candidate = grouped.setdefault(reading, {
                "reading": reading, "sources": [], "evidence_count": 0, "evidence": []
            })
            candidate["sources"].append(index.source)
            candidate["evidence"].extend(sorted(evidence))
    for candidate in grouped.values():
        candidate["sources"] = sorted(set(candidate["sources"]))
        candidate["evidence"] = sorted(set(candidate["evidence"]))
        candidate["evidence_count"] = len(candidate["evidence"])
    return sorted(grouped.values(), key=lambda candidate: str(candidate["reading"]))


def classify_entry(
    entry: str, indexes: list[SourceIndex], item: object = None
) -> tuple[str, str | None, str]:
    """Return (status, accepted reading, human-readable reason)."""
    if _entry_unicode_invalid(entry):
        return STATUS_INVALID, None, "entry contains invalid or misplaced Unicode characters"
    if any(index.invalid.get(entry) for index in indexes):
        return STATUS_INVALID, None, "a source supplied a reading containing illegal characters"

    by_source = {index.source: index.values.get(entry, {}) for index in indexes}
    dictionary_sources = ("formal", "jmdict", "jmnedict")
    for source in dictionary_sources:
        if len(by_source.get(source, {})) > 1:
            return STATUS_AMBIGUOUS, None, f"{source} has multiple applicable full-entry readings"

    ruby = by_source.get("aozora_ruby", {})
    ruby_qualified = {reading for reading, files in ruby.items() if len(files) >= 2}
    qualified: dict[str, set[str]] = defaultdict(set)
    for source in dictionary_sources:
        readings = by_source.get(source, {})
        if len(readings) == 1:
            qualified[next(iter(readings))].add(source)
    for reading in ruby_qualified:
        qualified[reading].add("aozora_ruby")

    all_readings = {
        reading for readings in by_source.values() for reading in readings
    }
    if len(all_readings) > 1 and qualified:
        return STATUS_CONFLICT, None, "sources disagree on the complete-entry reading"
    if len(qualified) > 1:
        return STATUS_CONFLICT, None, "qualified sources disagree on the reading"
    if _suspected_fragment(entry, item):
        return STATUS_FRAGMENT, None, "observed uses are predominantly embedded, or shape resembles a tokenizer fragment"
    if len(qualified) == 1:
        reading = next(iter(qualified))
        return STATUS_RESOLVED, reading, "one complete-entry reading is supported without conflict"
    if ruby:
        if len(ruby) > 1:
            return STATUS_CONFLICT, None, "Aozora texts contain conflicting ruby readings"
        return STATUS_SINGLE_RUBY, None, "ruby occurs in only one distinct Aozora text file"
    return STATUS_UNMATCHED, None, "no accepted complete-entry evidence"


def build_report(
    data_root: Path,
    pending_root: Path,
    jmdict: Path | None = None,
    jmnedict: Path | None = None,
    aozora_root: Path | None = None,
) -> tuple[dict, list[tuple[Path, int, object]], list[SourceIndex]]:
    pending_rows = _load_pending(pending_root)
    wanted = {
        _nfc(item.get("entry")) for _, _, item in pending_rows
        if isinstance(item, dict) and isinstance(item.get("entry"), str)
    }
    indexes = [build_formal_index(data_root, wanted)]
    source_files: dict[str, dict[str, str] | None] = {"formal": {"path": str(data_root)}}
    for source, path in (("jmdict", jmdict), ("jmnedict", jmnedict)):
        if path is not None:
            if not path.is_file():
                raise ValueError(f"{source} file does not exist: {path}")
            indexes.append(parse_edrdg(path, source, wanted))
            source_files[source] = {"path": str(path), "sha256": _sha256(path)}
        else:
            indexes.append(SourceIndex(source))
            source_files[source] = None
    if aozora_root is not None:
        if not aozora_root.exists():
            raise ValueError(f"Aozora path does not exist: {aozora_root}")
        indexes.append(build_aozora_index(aozora_root, wanted))
        source_files["aozora_ruby"] = {"path": str(aozora_root)}
    else:
        indexes.append(SourceIndex("aozora_ruby"))
        source_files["aozora_ruby"] = None

    entries: list[dict] = []
    counts: Counter[str] = Counter()
    for path, row_number, item in pending_rows:
        entry = item.get("entry") if isinstance(item, dict) else ""
        entry = _nfc(entry) if isinstance(entry, str) else ""
        status, reading, reason = classify_entry(entry, indexes, item)
        counts[status] += 1
        old_uuid = item.get("uuid") if isinstance(item, dict) else None
        new_uuid = compute_uuid_v5(entry, reading) if status == STATUS_RESOLVED and reading else None
        target = expected_data_path(data_root, reading) if reading else None
        invalid_evidence = [
            {"source": index.source, **evidence}
            for index in indexes for evidence in index.invalid.get(entry, [])
        ]
        entries.append({
            "entry": entry,
            "aozora_frequency": _frequency(item),
            "candidates": _candidate_rows(entry, indexes),
            "invalid_evidence": invalid_evidence,
            "final_status": status,
            "reason": reason,
            "resolved_reading": reading,
            "old_uuid": old_uuid,
            "new_uuid": new_uuid,
            "source_path": str(path),
            "source_row": row_number,
            "target_path": str(target) if target else None,
        })
    report = {
        "generated_at": _now_iso(),
        "mode": "dry-run",
        "sources": source_files,
        "statistics": {"total": len(entries), "by_status": dict(sorted(counts.items()))},
        "entries": entries,
    }
    return report, pending_rows, indexes


def _write_csv(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    fields = [
        "entry", "aozora_frequency", "candidate_readings", "sources",
        "evidence_count", "final_status", "reason", "resolved_reading",
        "old_uuid", "new_uuid", "source_path", "source_row", "target_path",
    ]
    try:
        with temporary.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            for row in report["entries"]:
                candidates = row["candidates"]
                writer.writerow({
                    **{key: row.get(key) for key in fields},
                    "candidate_readings": "|".join(candidate["reading"] for candidate in candidates),
                    "sources": "|".join(sorted({
                        source for candidate in candidates for source in candidate["sources"]
                    })),
                    "evidence_count": sum(candidate["evidence_count"] for candidate in candidates),
                })
        temporary.replace(path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _merge_promoted_record(existing: dict, promoted: dict) -> None:
    """Merge only additive pending evidence into an existing formal record."""
    existing_meta = existing.get("meta")
    promoted_meta = promoted.get("meta")
    if isinstance(existing_meta, dict) and isinstance(promoted_meta, dict):
        if isinstance(promoted_meta.get("updated_at"), str):
            existing_meta["updated_at"] = promoted_meta["updated_at"]
        left = existing_meta.get("frequencies")
        right = promoted_meta.get("frequencies")
        if isinstance(right, dict):
            if not isinstance(left, dict):
                left = {}
                existing_meta["frequencies"] = left
            for source, value in right.items():
                if (isinstance(value, (int, float)) and not isinstance(value, bool) and
                        (not isinstance(left.get(source), (int, float)) or value > left[source])):
                    left[source] = value
        variants = promoted_meta.get("variant_writings")
        if isinstance(variants, list):
            current = existing_meta.setdefault("variant_writings", [])
            if isinstance(current, list):
                current[:] = sorted(set(value for value in [*current, *variants] if isinstance(value, str)))

    existing_defs = existing.get("definitions")
    promoted_defs = promoted.get("definitions")
    if not isinstance(existing_defs, list) or not isinstance(promoted_defs, list):
        return
    by_sense = {
        (definition.get("gloss"), definition.get("register")): definition
        for definition in existing_defs if isinstance(definition, dict)
    }
    first = next((definition for definition in existing_defs if isinstance(definition, dict)), None)
    for source_definition in promoted_defs:
        if not isinstance(source_definition, dict):
            continue
        target = by_sense.get((source_definition.get("gloss"), source_definition.get("register")))
        if target is None and source_definition.get("gloss") in (None, ""):
            target = first
        if target is None:
            continue
        source_examples = source_definition.get("examples")
        target_examples = target.get("examples")
        if not isinstance(source_examples, dict) or not isinstance(target_examples, dict):
            continue
        for kind, additions in source_examples.items():
            if not isinstance(additions, list):
                continue
            destination = target_examples.setdefault(kind, [])
            if not isinstance(destination, list):
                continue
            seen_text = {
                unicodedata.normalize("NFC", value["text"]).strip(): value
                for value in destination
                if isinstance(value, dict) and isinstance(value.get("text"), str)
            }
            seen_other = {
                json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                for value in destination
                if not (isinstance(value, dict) and isinstance(value.get("text"), str))
            }
            for value in additions:
                if isinstance(value, dict) and isinstance(value.get("text"), str):
                    text_key = unicodedata.normalize("NFC", value["text"]).strip()
                    if text_key in seen_text:
                        current = seen_text[text_key]
                        for key, field_value in value.items():
                            if key not in current or current[key] in (None, ""):
                                current[key] = field_value
                            elif isinstance(current[key], dict) and isinstance(field_value, dict):
                                for nested_key, nested_value in field_value.items():
                                    if (nested_key not in current[key] or
                                            current[key][nested_key] in (None, "")):
                                        current[key][nested_key] = nested_value
                        continue
                    destination.append(value)
                    seen_text[text_key] = value
                    continue
                key = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                if key not in seen_other:
                    destination.append(value)
                    seen_other.add(key)


def _apply_resolutions(
    report: dict,
    pending_rows: list[tuple[Path, int, object]],
    ledger_path: Path,
    pending_root: Path,
) -> int:
    planned = [row for row in report["entries"] if row["final_status"] == STATUS_RESOLVED]
    dictionary_hashes = {
        source: details["sha256"] for source, details in report["sources"].items()
        if source in {"jmdict", "jmnedict"} and isinstance(details, dict) and "sha256" in details
    }
    ledger_rows = [{
        "entry": row["entry"],
        "reading": row["resolved_reading"],
        "old_uuid": row["old_uuid"],
        "new_uuid": row["new_uuid"],
        "source_path": _ledger_path(row["source_path"]),
        "source_row": row["source_row"],
        "target_path": _ledger_path(row["target_path"]),
        "evidence": row["candidates"],
        "dictionary_hashes": dictionary_hashes,
    } for row in planned]
    ledger: dict = {"generated_at": _now_iso(), "migrations": []}
    if ledger_path.exists():
        loaded_ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        if not isinstance(loaded_ledger, dict) or not isinstance(loaded_ledger.get("migrations"), list):
            raise ValueError(f"invalid migration ledger: {ledger_path}")
        ledger = loaded_ledger
    existing_keys = {
        (row.get("old_uuid"), row.get("new_uuid"), row.get("source_path"), row.get("source_row"))
        for row in ledger["migrations"] if isinstance(row, dict)
    }
    additions = [row for row in ledger_rows if (
        row.get("old_uuid"), row.get("new_uuid"), row.get("source_path"), row.get("source_row")
    ) not in existing_keys]
    if additions or not ledger_path.exists():
        ledger["migrations"].extend(additions)
        # The ledger is durable before any dictionary row is moved.
        _atomic_json(ledger_path, ledger)
    if not planned:
        return 0

    item_lookup = {(str(path), row_number): item for path, row_number, item in pending_rows}
    destination_cache: dict[Path, list[object]] = {}
    resolved_keys: set[tuple[str, int]] = set()
    for row in planned:
        key = (row["source_path"], row["source_row"])
        original = item_lookup.get(key)
        if not isinstance(original, dict):
            continue
        target = Path(row["target_path"])
        values = destination_cache.get(target)
        if values is None:
            if target.exists():
                loaded = json.loads(target.read_text(encoding="utf-8"))
                values = loaded if isinstance(loaded, list) else [loaded]
            else:
                values = []
            destination_cache[target] = values
        promoted = copy.deepcopy(original)
        promoted.setdefault("reading", {})["primary"] = row["resolved_reading"]
        promoted["uuid"] = row["new_uuid"]
        meta = promoted.setdefault("meta", {})
        if isinstance(meta, dict):
            meta.pop("needs_reading", None)
            meta.pop("reading_failure_reason", None)
            meta["updated_at"] = report["generated_at"]
        collision = next((item for item in values if isinstance(item, dict) and (
            item.get("uuid") == promoted["uuid"] or
            (item.get("entry") == promoted.get("entry") and
             isinstance(item.get("reading"), dict) and
             item["reading"].get("primary") == row["resolved_reading"])
        )), None)
        if collision is None:
            values.append(promoted)
        else:
            _merge_promoted_record(collision, promoted)
        resolved_keys.add(key)

    for target, values in destination_cache.items():
        _atomic_json(target, values)

    by_source: dict[Path, set[int]] = defaultdict(set)
    for source_path, row_number in resolved_keys:
        by_source[Path(source_path)].add(row_number)
    for source, removed_rows in by_source.items():
        loaded = json.loads(source.read_text(encoding="utf-8"))
        rows = loaded if isinstance(loaded, list) else [loaded]
        kept = [item for number, item in enumerate(rows, 1) if number not in removed_rows]
        if kept:
            _atomic_json(source, kept)
        else:
            source.unlink(missing_ok=True)
    _remove_empty_parents(pending_root)
    return len(resolved_keys)


def resolve(
    data_root: Path,
    pending_root: Path,
    *,
    jmdict: Path | None = None,
    jmnedict: Path | None = None,
    aozora_root: Path | None = None,
    apply: bool = False,
    report_path: Path | None = None,
    csv_path: Path | None = None,
    ledger_path: Path | None = None,
) -> dict:
    if apply and (report_path is None or ledger_path is None):
        raise ValueError("--apply requires both --report and --ledger")
    report, pending_rows, _ = build_report(data_root, pending_root, jmdict, jmnedict, aozora_root)
    if apply:
        report["mode"] = "apply"
    report["statistics"]["planned_promotions"] = sum(
        row["final_status"] == STATUS_RESOLVED for row in report["entries"]
    )
    if report_path is not None:
        _atomic_json(report_path, report)
    if csv_path is None and report_path is not None:
        csv_path = report_path.with_suffix(".csv")
    if csv_path is not None:
        _write_csv(csv_path, report)
    promoted = 0
    if apply:
        promoted = _apply_resolutions(report, pending_rows, ledger_path, pending_root)
    report["statistics"]["promoted"] = promoted
    if report_path is not None:
        _atomic_json(report_path, report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--pending-dir", type=Path, default=DEFAULT_PENDING)
    parser.add_argument("--jmdict", type=Path)
    parser.add_argument("--jmnedict", type=Path)
    parser.add_argument("--aozora-dir", type=Path)
    parser.add_argument("--report", type=Path, help="JSON decision report")
    parser.add_argument("--csv", type=Path, help="CSV summary (defaults beside --report)")
    parser.add_argument("--ledger", type=Path, help="UUID migration ledger (required with --apply)")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = resolve(
            args.data_dir.resolve(), args.pending_dir.resolve(),
            jmdict=args.jmdict.resolve() if args.jmdict else None,
            jmnedict=args.jmnedict.resolve() if args.jmnedict else None,
            aozora_root=args.aozora_dir.resolve() if args.aozora_dir else None,
            apply=args.apply,
            report_path=args.report.resolve() if args.report else None,
            csv_path=args.csv.resolve() if args.csv else None,
            ledger_path=args.ledger.resolve() if args.ledger else None,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ET.ParseError, ValueError) as exc:
        parser.error(str(exc))
    for row in report["entries"]:
        candidate_text = ",".join(candidate["reading"] for candidate in row["candidates"]) or "-"
        print(f"{row['final_status']:18} {row['entry']} candidates={candidate_text} reason={row['reason']}")
    print(json.dumps(report["statistics"], ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
