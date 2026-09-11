#!/usr/bin/env python3
"""Conservatively adjudicate pending readings with a local OpenAI-compatible LLM.

Machine evidence stays authoritative.  The LLM may only select a reading assembled
from KANJIDIC2 character readings; it cannot invent one.  Two deliberately different
reviews must agree before an entry is promoted or rejected.  Everything else remains
pending for inspection.  The default mode is read-only and every API result is
checkpointed so a long local run can resume safely.
"""

from __future__ import annotations

import argparse
import copy
import gzip
import hashlib
import html
import itertools
import json
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from check_data_quality import DEFAULT_DATA, DEFAULT_PENDING, _remove_empty_parents
from create_entries import _apply_kyuji
from dictionary_rules import compute_uuid_v5, expected_data_path, is_valid_reading
from resolve_pending_readings import (
    STATUS_RESOLVED,
    _apply_resolutions,
    _atomic_json,
    _load_pending,
    build_formal_index,
    build_report,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REJECTED = PROJECT_ROOT / "rejected" / "needs_reading"
PASS_PROMPTS = (
    "日本語辞書の厳格な校閲者として、青空文庫の形態素抽出候補を判定する。"
    "候補の大半は誤分割である。独立した辞書見出し語で、文脈上の読みが候補から一意に選べる時だけP。"
    "誤分割、活用語幹、固有名の断片、OCR誤り、偶然隣接した漢字はD。"
    "用例なしは絶対にPにせずR。固有名はD。"
    "『と云事』からの云事、『三千餘兩』からの余兩、『妻兒如何』からの児如、"
    "『李處耘』からの処耘のように、周囲を切っただけの二字は必ずD。"
    "少しでも不明ならR。漢字ごとの意味を足しただけでは語と認めない。読みは必ず提示候補から選ぶ。",
    "第一判定を知らない反証担当者として保守的に再判定する。"
    "実在し文中で一語として使われ、候補読みが確実な場合だけP。"
    "文字列が実在しても、この用例が別語・数量・人名・漢文訓読の断片ならD。"
    "用例なしはR。独立語である積極的証拠がなければPにしない。"
    "読みは必ず提示候補から選ぶ。",
)
_LINE_RE = re.compile(r"^(\d+):([PDR])(\d+)?$")
_KOTOBANK_READING_RE = re.compile(r"（読み）([^<]+)")
_WEBLIO_HEADING_RE = re.compile(
    r'<h2\s+class=["\']?midashigo["\']?\s+title="([^"]+)">.*?</h2>(.{0,2500})', re.DOTALL
)
_WEBLIO_READING_RE = re.compile(r"読み方[：:]\s*([ぁ-ゖァ-ヺー・、,/／]+)")
_VOICING = {
    "か": ("が",), "き": ("ぎ",), "く": ("ぐ",), "け": ("げ",), "こ": ("ご",),
    "さ": ("ざ",), "し": ("じ",), "す": ("ず",), "せ": ("ぜ",), "そ": ("ぞ",),
    "た": ("だ",), "ち": ("ぢ",), "つ": ("づ",), "て": ("で",), "と": ("ど",),
    "は": ("ば", "ぱ"), "ひ": ("び", "ぴ"), "ふ": ("ぶ", "ぷ"),
    "へ": ("べ", "ぺ"), "ほ": ("ぼ", "ぽ"),
}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def kata_to_hira(text: str) -> str:
    return "".join(chr(ord(char) - 0x60) if "ァ" <= char <= "ヶ" else char for char in text)


def clean_kanjidic_reading(value: str) -> set[str]:
    """Return useful lexical forms from KANJIDIC's dotted/hyphenated notation."""
    value = kata_to_hira(value.strip().lstrip("-").rstrip("-"))
    if not value:
        return set()
    forms = {value.replace(".", "")}
    if "." in value:
        forms.add(value.split(".", 1)[0])
    return {form for form in forms if form and is_valid_reading(form)}


def load_kanjidic(path: Path) -> dict[str, tuple[str, ...]]:
    readings: dict[str, tuple[str, ...]] = {}
    opener = gzip.open if path.suffix.lower() == ".gz" else open
    with opener(path, "rb") as stream:
        for _, element in ET.iterparse(stream, events=("end",)):
            if element.tag.rsplit("}", 1)[-1] != "character":
                continue
            literal = element.findtext("literal")
            values: list[str] = []
            for reading in element.findall("./reading_meaning/rmgroup/reading"):
                if reading.get("r_type") not in {"ja_on", "ja_kun"} or not reading.text:
                    continue
                # XML order is useful evidence: KANJIDIC conventionally puts the
                # principal on-readings before rarer alternatives.  Preserve it.
                forms = clean_kanjidic_reading(reading.text)
                raw = kata_to_hira(reading.text.strip().lstrip("-").rstrip("-")).replace(".", "")
                for value in sorted(forms, key=lambda form: (form != raw, -len(form), form)):
                    if value not in values:
                        values.append(value)
            if literal and values:
                readings[literal] = tuple(values)
            element.clear()
    return readings


def segment_variants(segment: str, *, noninitial: bool) -> set[str]:
    values = {segment}
    if noninitial and segment and segment[0] in _VOICING:
        values.update(replacement + segment[1:] for replacement in _VOICING[segment[0]])
    return values


def reading_candidates(entry: str, kanjidic: dict[str, tuple[str, ...]], cap: int = 256) -> list[str]:
    """Assemble auditable candidates; return none rather than truncate silently."""
    per_character: list[tuple[str, ...]] = []
    previous: tuple[str, ...] | None = None
    for character in entry:
        if character == "々" and previous:
            values = previous
        else:
            values = kanjidic.get(character)
        if not values:
            return []
        per_character.append(values)
        previous = values

    candidates: dict[str, int] = {}
    combinations = 1
    for values in per_character:
        combinations *= len(values) * 3
        if combinations > cap * 20:
            return []
    for parts in itertools.product(*per_character):
        rank = sum(per_character[index].index(part) for index, part in enumerate(parts))
        choices = [segment_variants(part, noninitial=index > 0) for index, part in enumerate(parts)]
        for variant_parts in itertools.product(*choices):
            joined = "".join(variant_parts)
            phonetic_penalty = sum(left != right for left, right in zip(parts, variant_parts))
            candidates[joined] = min(candidates.get(joined, 10**9), rank + phonetic_penalty * 4)
            if len(variant_parts) > 1 and variant_parts[0].endswith(("く", "き", "つ", "ち")):
                geminated = variant_parts[0][:-1] + "っ" + "".join(variant_parts[1:])
                candidates[geminated] = min(candidates.get(geminated, 10**9), rank + 4)
            if len(candidates) > cap:
                return []
    return sorted(candidates, key=lambda value: (candidates[value], len(value), value))


def example_texts(item: object, maximum: int = 2, width: int = 160) -> list[str]:
    values: list[str] = []
    if not isinstance(item, dict):
        return values
    for definition in item.get("definitions", []):
        if not isinstance(definition, dict):
            continue
        examples = definition.get("examples", {})
        if not isinstance(examples, dict):
            continue
        for group in examples.values():
            if not isinstance(group, list):
                continue
            for example in group:
                text = example.get("text") if isinstance(example, dict) else None
                if isinstance(text, str) and text.strip():
                    values.append(text.strip()[:width])
                    if len(values) >= maximum:
                        return values
    return values


def comparison_reading(value: str) -> str:
    """Fold dictionary display orthography without changing the stored reading."""
    return kata_to_hira(value).translate(str.maketrans("ぁぃぅぇぉゃゅょゎ", "あいうえおやゆよわ"))


def modernize_display_reading(value: str) -> str:
    """Convert dictionary typography such as えんきゆう to normal small-kana spelling."""
    value = kata_to_hira(value)
    return re.sub(
        r"([きぎしじちぢにひびぴみり])([やゆよ])",
        lambda match: match.group(1) + {"や": "ゃ", "ゆ": "ゅ", "よ": "ょ"}[match.group(2)],
        value,
    )


def parse_kotobank_readings(page: str) -> list[str]:
    match = _KOTOBANK_READING_RE.search(page)
    if not match:
        return []
    display = html.unescape(match.group(1)).strip()
    # The first form is the modern dictionary reading; parenthesized forms are
    # historical spellings.  Multiple modern readings may be separated by ・.
    modern = re.split(r"[（(]", display, maxsplit=1)[0]
    values: list[str] = []
    for raw in re.split(r"[・、,/／]", modern):
        reading = modernize_display_reading(re.sub(r"\s+", "", raw))
        if is_valid_reading(reading) and reading not in values:
            values.append(reading)
    return values


def load_kotobank_cache(path: Path) -> dict[str, dict]:
    values: dict[str, dict] = {}
    if not path.exists():
        return values
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            row = json.loads(line)
            if isinstance(row, dict) and isinstance(row.get("entry"), str):
                values[row["entry"]] = row
    return values


def fetch_kotobank(entry: str, timeout: int) -> dict:
    from urllib.parse import quote
    url = "https://kotobank.jp/word/" + quote(entry, safe="")
    request = urllib.request.Request(url, headers={"User-Agent": "Genji-reading-audit/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            page = response.read().decode("utf-8", "replace")
        readings = parse_kotobank_readings(page)
        return {"entry": entry, "status": "matched" if readings else "no_reading",
                "readings": readings, "url": url}
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {"entry": entry, "status": "not_found", "readings": [], "url": url}
        return {"entry": entry, "status": f"http_{exc.code}", "readings": [], "url": url}
    except OSError as exc:
        return {"entry": entry, "status": "error", "readings": [], "url": url,
                "error": str(exc)}


def populate_kotobank_cache(path: Path, entries: list[str], workers: int, timeout: int) -> dict[str, dict]:
    cache = load_kotobank_cache(path)
    missing = [entry for entry in entries if entry not in cache or cache[entry].get("status") == "error"]
    if not missing:
        return cache
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream, ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fetch_kotobank, entry, timeout): entry for entry in missing}
        completed = 0
        for future in as_completed(futures):
            row = future.result()
            cache[row["entry"]] = row
            stream.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
            stream.flush()
            completed += 1
            if completed % 100 == 0 or completed == len(missing):
                print(f"kotobank: {completed}/{len(missing)}", file=sys.stderr)
    return cache


def exact_dictionary_reading(row: dict | None, candidates: list[str]) -> str | None:
    if not row or row.get("status") != "matched":
        return None
    displayed = [
        modernize_display_reading(value) for value in row.get("readings", [])
        if isinstance(value, str) and is_valid_reading(modernize_display_reading(value))
    ]
    # An exact full-headword dictionary reading outranks character composition.
    # KANJIDIC candidates remain useful for disambiguating multiple displayed forms.
    if len(displayed) == 1:
        return displayed[0]
    by_folded: dict[str, list[str]] = defaultdict(list)
    for candidate in candidates:
        by_folded[comparison_reading(candidate)].append(candidate)
    matched: set[str] = set()
    for value in displayed:
        matched.update(by_folded.get(comparison_reading(value), []))
    return next(iter(matched)) if len(matched) == 1 else None


def formal_variant_match(entry: str, formal_index, data_root: Path) -> dict | None:
    canonical = _apply_kyuji(entry)
    if canonical == entry:
        return None
    readings = formal_index.values.get(canonical, {})
    if len(readings) != 1:
        return None
    reading, evidence = next(iter(readings.items()))
    direct = sorted(value for value in evidence if value.startswith("formal:") and not value.endswith(":variant"))
    if not direct:
        return None
    location = direct[0][len("formal:"):]
    relative, raw_row = location.rsplit("#", 1)
    return {
        "canonical": canonical, "reading": reading,
        "target_path": str(data_root / relative), "target_row": int(raw_row),
        "evidence": direct,
    }


def parse_weblio_readings(page: str, entry: str) -> list[str]:
    values: list[str] = []
    for match in _WEBLIO_HEADING_RE.finditer(page):
        if html.unescape(match.group(1)).strip() != entry:
            continue
        reading_match = _WEBLIO_READING_RE.search(html.unescape(match.group(2)))
        if not reading_match:
            continue
        for raw in re.split(r"[・、,/／]", reading_match.group(1)):
            reading = modernize_display_reading(raw.strip())
            if is_valid_reading(reading) and reading not in values:
                values.append(reading)
    return values


def fetch_weblio(entry: str, timeout: int) -> dict:
    from urllib.parse import quote
    url = "https://www.weblio.jp/content/" + quote(entry, safe="")
    request = urllib.request.Request(url, headers={"User-Agent": "Genji-reading-audit/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            page = response.read().decode("utf-8", "replace")
        readings = parse_weblio_readings(page, entry)
        return {"entry": entry, "status": "matched" if readings else "no_reading",
                "readings": readings, "url": url}
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {"entry": entry, "status": "not_found", "readings": [], "url": url}
        return {"entry": entry, "status": f"http_{exc.code}", "readings": [], "url": url}
    except OSError as exc:
        return {"entry": entry, "status": "error", "readings": [], "url": url,
                "error": str(exc)}


def populate_weblio_cache(path: Path, entries: list[str], workers: int, timeout: int) -> dict[str, dict]:
    cache = load_kotobank_cache(path)  # Same compact JSONL schema.
    missing = [entry for entry in entries if entry not in cache or cache[entry].get("status") == "error"]
    if not missing:
        return cache
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream, ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fetch_weblio, entry, timeout): entry for entry in missing}
        completed = 0
        for future in as_completed(futures):
            row = future.result()
            cache[row["entry"]] = row
            stream.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
            stream.flush()
            completed += 1
            if completed % 100 == 0 or completed == len(missing):
                print(f"weblio: {completed}/{len(missing)}", file=sys.stderr)
    return cache


@dataclass(frozen=True)
class Decision:
    action: str
    reading: str = ""


def parse_response(content: str, expected: dict[int, list[str]]) -> dict[int, Decision]:
    decisions: dict[int, Decision] = {}
    for raw_line in content.strip().splitlines():
        line = raw_line.strip()
        match = _LINE_RE.fullmatch(line)
        if not match:
            continue
        identifier, code, raw_index = int(match.group(1)), match.group(2), match.group(3)
        if identifier not in expected or identifier in decisions:
            continue
        if code == "P":
            if raw_index is None or int(raw_index) >= len(expected[identifier]):
                continue
            decisions[identifier] = Decision("promote", expected[identifier][int(raw_index)])
        elif raw_index is None:
            decisions[identifier] = Decision("discard" if code == "D" else "review")
    if set(decisions) != set(expected):
        missing = sorted(set(expected) - set(decisions))
        raise ValueError(f"invalid or incomplete LLM response; missing ids: {missing[:10]}")
    return decisions


def make_prompt(rows: list[dict], pass_number: int) -> list[dict[str, str]]:
    lines = [
        "入力順と同じ長さのJSON整数配列をxに返す。候補番号0以上=P、-1=D、-2=R。",
        "読みに複数の可能性がある時、候補は機械的な標準度順。文脈に反しない限り前方を優先。",
    ]
    for row in rows:
        contexts = " ⏐ ".join(text.replace("\t", " ").replace("\n", " ") for text in row["examples"])
        numbered = ",".join(f"{index}={value}" for index, value in enumerate(row["candidates"]))
        lines.append(f'{row["entry"]}\t{numbered}\t{contexts}')
    return [
        {"role": "system", "content": PASS_PROMPTS[pass_number]},
        {"role": "user", "content": "\n".join(lines)},
    ]


def call_lm_studio(endpoint: str, model: str, rows: list[dict], pass_number: int,
                   timeout: int, retries: int = 3) -> dict[int, Decision]:
    local_rows = [{**row, "id": index} for index, row in enumerate(rows)]
    expected = {row["id"]: row["candidates"] for row in local_rows}
    response_schema = {
        "type": "object",
        "properties": {"x": {
            "type": "array", "items": {"type": "integer", "minimum": -2,
                                      "maximum": max(len(row["candidates"]) for row in rows) - 1},
            "minItems": len(rows), "maxItems": len(rows),
        }},
        "required": ["x"], "additionalProperties": False,
    }
    payload = {
        "model": model,
        "temperature": 0,
        "reasoning_effort": "none",
        "max_tokens": max(64, len(rows) * 4),
        "response_format": {"type": "json_schema", "json_schema": {
            "name": "compact_decisions", "strict": True, "schema": response_schema,
        }},
        "messages": make_prompt(local_rows, pass_number),
    }
    request = urllib.request.Request(
        endpoint.rstrip("/") + "/v1/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    error: Exception | None = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                loaded = json.load(response)
            content = loaded["choices"][0]["message"]["content"]
            indexes = json.loads(content)["x"]
            if len(indexes) != len(rows):
                raise ValueError(f"expected {len(rows)} decisions, got {len(indexes)}")
            local_decisions: dict[int, Decision] = {}
            for index, selected in enumerate(indexes):
                if selected == -1:
                    local_decisions[index] = Decision("discard")
                elif selected == -2:
                    local_decisions[index] = Decision("review")
                elif isinstance(selected, int) and 0 <= selected < len(expected[index]):
                    local_decisions[index] = Decision("promote", expected[index][selected])
                else:
                    raise ValueError(f"candidate index out of range for item {index}: {selected}")
            return {rows[index]["id"]: decision for index, decision in local_decisions.items()}
        except (OSError, KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
            error = exc
            # Local models occasionally omit one row or reject an oversized
            # prompt.  Bisecting is deterministic and safer than weakening the
            # parser or accepting a partial batch.
            if len(rows) > 1:
                middle = len(rows) // 2
                left = call_lm_studio(endpoint, model, rows[:middle], pass_number, timeout, retries)
                right = call_lm_studio(endpoint, model, rows[middle:], pass_number, timeout, retries)
                return {**left, **right}
            if attempt + 1 < retries:
                time.sleep(1 + attempt * 2)
    # One pathological entry must not abort a multi-hour resumable audit.
    print(f"LLM left id {rows[0]['id']} for review after {retries} failures: {error}", file=sys.stderr)
    return {rows[0]["id"]: Decision("review")}


def load_checkpoint(path: Path) -> dict[tuple[int, int], Decision]:
    values: dict[tuple[int, int], Decision] = {}
    if not path.exists():
        return values
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            row = json.loads(line)
            values[(int(row["pass"]), int(row["id"]))] = Decision(row["action"], row.get("reading", ""))
    return values


def append_checkpoint(path: Path, pass_number: int, decisions: dict[int, Decision]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        for identifier, decision in sorted(decisions.items()):
            stream.write(json.dumps({
                "pass": pass_number, "id": identifier,
                "action": decision.action, "reading": decision.reading,
            }, ensure_ascii=False, separators=(",", ":")) + "\n")
        stream.flush()


def consensus(first: Decision, second: Decision) -> Decision:
    return first if first == second and first.action in {"promote", "discard"} else Decision("review")


def apply_rejections(rows: list[dict], pending_rows: list[tuple[Path, int, object]],
                     pending_root: Path, rejected_root: Path) -> int:
    rejected_keys = {(row["source_path"], row["source_row"]) for row in rows if row["final_status"] == "discard"}
    by_source: dict[Path, set[int]] = defaultdict(set)
    for source_path, row_number in rejected_keys:
        by_source[Path(source_path)].add(row_number)
    moved = 0
    for source, positions in by_source.items():
        loaded = json.loads(source.read_text(encoding="utf-8"))
        values = loaded if isinstance(loaded, list) else [loaded]
        rejected = [value for number, value in enumerate(values, 1) if number in positions]
        kept = [value for number, value in enumerate(values, 1) if number not in positions]
        target = rejected_root / source.relative_to(pending_root)
        existing: list[object] = []
        if target.exists():
            old = json.loads(target.read_text(encoding="utf-8"))
            existing = old if isinstance(old, list) else [old]
        _atomic_json(target, [*existing, *rejected])
        if kept:
            _atomic_json(source, kept)
        else:
            source.unlink()
        moved += len(rejected)
    _remove_empty_parents(pending_root)
    return moved


def adjudicate(args: argparse.Namespace) -> dict:
    if not args.kanjidic.is_file():
        raise ValueError(f"KANJIDIC2 file does not exist: {args.kanjidic}")
    machine, pending_rows, indexes = build_report(
        args.data_dir, args.pending_dir, args.jmdict, args.jmnedict, args.aozora_dir
    )
    kanjidic = load_kanjidic(args.kanjidic)
    normalized_wanted = {
        _apply_kyuji(item.get("entry", "")) for _, _, item in pending_rows
        if isinstance(item, dict) and isinstance(item.get("entry"), str)
    }
    variant_index = build_formal_index(args.data_dir, normalized_wanted)
    kotobank: dict[str, dict] = {}
    weblio: dict[str, dict] = {}
    if args.kotobank_cache is not None:
        entries = [
            item.get("entry", "") for _, _, item in pending_rows
            if isinstance(item, dict) and isinstance(item.get("entry"), str)
        ]
        kotobank = populate_kotobank_cache(
            args.kotobank_cache, entries, args.kotobank_workers, args.web_timeout
        )
    if args.weblio_cache is not None:
        entries = [
            item.get("entry", "") for _, _, item in pending_rows
            if isinstance(item, dict) and isinstance(item.get("entry"), str)
        ]
        weblio = populate_weblio_cache(
            args.weblio_cache, entries, args.weblio_workers, args.web_timeout
        )
    machine_by_key = {
        (row["source_path"], row["source_row"]): row for row in machine["entries"]
    }
    work: list[dict] = []
    final_rows: list[dict] = []
    for identifier, (path, row_number, item) in enumerate(pending_rows):
        key = (str(path), row_number)
        machine_row = machine_by_key[key]
        entry = machine_row["entry"]
        if machine_row["final_status"] == STATUS_RESOLVED:
            final_rows.append({**machine_row, "id": identifier, "decision_source": "machine"})
            continue
        candidates = reading_candidates(entry, kanjidic, args.candidate_cap)
        if machine_row["final_status"] == "invalid_unicode":
            final_rows.append({**machine_row, "id": identifier, "final_status": "discard",
                               "decision_source": "machine_invalid_unicode"})
            continue
        variant = formal_variant_match(entry, variant_index, args.data_dir)
        if variant:
            canonical, reading = variant["canonical"], variant["reading"]
            final_rows.append({
                **machine_row,
                "id": identifier,
                "source_entry": entry,
                "entry": canonical,
                "final_status": STATUS_RESOLVED,
                "resolved_reading": reading,
                "new_uuid": compute_uuid_v5(canonical, reading),
                "target_path": variant["target_path"],
                "candidates": [{
                    "reading": reading, "sources": ["formal_normalized_variant"],
                    "evidence_count": len(variant["evidence"]), "evidence": variant["evidence"],
                }],
                "decision_source": "machine_normalized_variant",
            })
            continue
        dictionary_reading = exact_dictionary_reading(kotobank.get(entry), candidates)
        dictionary_source = "kotobank"
        source_row = kotobank.get(entry)
        if not dictionary_reading:
            dictionary_reading = exact_dictionary_reading(weblio.get(entry), candidates)
            dictionary_source = "weblio"
            source_row = weblio.get(entry)
        if dictionary_reading and source_row:
            final_rows.append({
                **machine_row,
                "id": identifier,
                "final_status": STATUS_RESOLVED,
                "resolved_reading": dictionary_reading,
                "new_uuid": compute_uuid_v5(entry, dictionary_reading),
                "target_path": str(expected_data_path(args.data_dir, dictionary_reading)),
                "candidates": [{
                    "reading": dictionary_reading,
                    "sources": [dictionary_source],
                    "evidence_count": 1,
                    "evidence": [source_row["url"]],
                }],
                "decision_source": "machine_exact_dictionary",
            })
            continue
        if not candidates:
            final_rows.append({**machine_row, "id": identifier, "final_status": "review",
                               "decision_source": "machine_no_bounded_candidates"})
            continue
        if (args.llm_status != "all" and
                machine_row["final_status"] != args.llm_status):
            final_rows.append({**machine_row, "id": identifier, "final_status": "review",
                               "decision_source": "llm_out_of_scope"})
            continue
        work.append({
            "id": identifier, "entry": entry,
            "candidates": candidates[:args.llm_candidate_limit],
            "examples": example_texts(item), "machine_status": machine_row["final_status"],
            "machine_row": machine_row,
        })

    checkpoint = load_checkpoint(args.checkpoint)
    if not args.skip_llm:
        for pass_number in range(2):
            missing = [row for row in work if (pass_number, row["id"]) not in checkpoint]
            batches = [missing[start:start + args.batch_size] for start in range(0, len(missing), args.batch_size)]
            completed = 0
            with ThreadPoolExecutor(max_workers=args.llm_workers) as pool:
                futures = {
                    pool.submit(call_lm_studio, args.endpoint, args.model, batch,
                                pass_number, args.timeout): batch
                    for batch in batches
                }
                for future in as_completed(futures):
                    decisions = future.result()
                    append_checkpoint(args.checkpoint, pass_number, decisions)
                    checkpoint.update({
                        (pass_number, identifier): decision for identifier, decision in decisions.items()
                    })
                    completed += len(futures[future])
                    print(f"pass {pass_number + 1}/2: {completed}/{len(missing)}", file=sys.stderr)

    for row in work:
        if args.skip_llm:
            decision = Decision("review")
            proposed_decision = decision
            passes: list[dict] = []
        else:
            decision = consensus(checkpoint[(0, row["id"])], checkpoint[(1, row["id"])])
            proposed_decision = decision
            # A local model is an adviser, not evidence.  Applying its consensus is
            # an explicit opt-in, and missing context can never be auto-promoted.
            if decision.action == "promote" and not row["examples"]:
                decision = Decision("review")
            if not args.trust_llm and decision.action != "review":
                decision = Decision("review")
            passes = [checkpoint[(number, row["id"])].__dict__ for number in range(2)]
        machine_row = row["machine_row"]
        resolved = decision.reading if decision.action == "promote" else None
        final_rows.append({
            **machine_row,
            "id": row["id"],
            "candidate_readings": row["candidates"],
            "llm_passes": passes,
            "llm_consensus_proposal": proposed_decision.__dict__ if not args.skip_llm else None,
            "final_status": STATUS_RESOLVED if decision.action == "promote" else decision.action,
            "resolved_reading": resolved,
            "new_uuid": compute_uuid_v5(row["entry"], resolved) if resolved else None,
            "target_path": str(expected_data_path(args.data_dir, resolved)) if resolved else None,
            "decision_source": ("llm_skipped" if args.skip_llm else
                                "llm_consensus" if decision.action != "review" else
                                "llm_proposal_only" if proposed_decision.action != "review" else
                                "llm_disagreement"),
        })
    final_rows.sort(key=lambda row: row["id"])
    counts = Counter(row["final_status"] for row in final_rows)
    report = {
        "generated_at": now_iso(),
        "mode": "apply" if args.apply else "dry-run",
        "model": args.model,
        "endpoint": args.endpoint,
        "sources": {**machine["sources"], "kanjidic2": {
            "path": str(args.kanjidic), "sha256": sha256(args.kanjidic),
        }, "kotobank": {"cache": str(args.kotobank_cache)} if args.kotobank_cache else None,
           "weblio": {"cache": str(args.weblio_cache)} if args.weblio_cache else None},
        "policy": {"passes": 2, "consensus": "exact_unanimous", "free_form_readings": False},
        "statistics": {"total": len(final_rows), "by_status": dict(sorted(counts.items()))},
        "entries": final_rows,
    }
    _atomic_json(args.report, report)
    if args.apply:
        variant_by_key = {
            (row["source_path"], row["source_row"]): row
            for row in final_rows if row.get("decision_source") == "machine_normalized_variant"
        }
        apply_rows = []
        for path, row_number, item in pending_rows:
            variant = variant_by_key.get((str(path), row_number))
            if variant and isinstance(item, dict):
                item = copy.deepcopy(item)
                original_entry = item.get("entry")
                item["entry"] = variant["entry"]
                meta = item.setdefault("meta", {})
                values = meta.setdefault("variant_writings", [])
                if isinstance(values, list) and isinstance(original_entry, str) and original_entry not in values:
                    values.append(original_entry)
            apply_rows.append((path, row_number, item))
        promoted = _apply_resolutions(report, apply_rows, args.ledger, args.pending_dir)
        rejected = apply_rejections(final_rows, pending_rows, args.pending_dir, args.rejected_dir)
        report["statistics"].update({"promoted": promoted, "rejected": rejected})
        _atomic_json(args.report, report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--pending-dir", type=Path, default=DEFAULT_PENDING)
    parser.add_argument("--rejected-dir", type=Path, default=DEFAULT_REJECTED)
    parser.add_argument("--kanjidic", type=Path, required=True)
    parser.add_argument("--jmdict", type=Path)
    parser.add_argument("--jmnedict", type=Path)
    parser.add_argument("--aozora-dir", type=Path)
    parser.add_argument("--endpoint", default="http://127.0.0.1:1234")
    parser.add_argument("--model", default="google/gemma-4-31b-qat")
    parser.add_argument("--batch-size", type=int, default=40)
    parser.add_argument("--llm-workers", type=int, default=1,
                        help="concurrent LM Studio requests (server may serialize them)")
    parser.add_argument("--candidate-cap", type=int, default=256)
    parser.add_argument("--llm-candidate-limit", type=int, default=16,
                        help="highest-ranked machine candidates exposed to the LLM")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--skip-llm", action="store_true",
                        help="apply/report only deterministic decisions; leave residual rows pending")
    parser.add_argument("--llm-status", choices=("all", "unmatched", "suspected_fragment"),
                        default="all", help="limit costly LLM review to one machine status")
    parser.add_argument("--trust-llm", action="store_true",
                        help="allow unanimous LLM decisions to mutate data (not recommended)")
    parser.add_argument("--kotobank-cache", type=Path,
                        help="enable resumable exact-headword lookup using this JSONL cache")
    parser.add_argument("--kotobank-workers", type=int, default=4)
    parser.add_argument("--weblio-cache", type=Path,
                        help="enable a second resumable exact-headword lookup")
    parser.add_argument("--weblio-workers", type=int, default=4)
    parser.add_argument("--web-timeout", type=int, default=20)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, help="promotion UUID ledger; required with --apply")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    if args.apply and args.ledger is None:
        parser.error("--apply requires --ledger")
    if (args.batch_size < 1 or args.llm_workers < 1 or args.candidate_cap < 1 or
            args.llm_candidate_limit < 1):
        parser.error("batch size and candidate limits must be positive")
    report = adjudicate(args)
    print(json.dumps(report["statistics"], ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
