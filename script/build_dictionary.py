#!/usr/bin/env python3
"""
scripts/build_dictionary.py

日本語語彙データセット構築スクリプト

データソース:
  - JMdict (EDRDG 公式 XML)         → 語彙・定義・品詞
  - hingston/japanese               → 頻度ランク
  - aozorahack/aozorabunko_text     → 文学例句
"""

import argparse
import gzip
import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
import tracemalloc
import unicodedata
import urllib.request
import uuid
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Iterator, Optional
from xml.etree import ElementTree as ET

log = logging.getLogger(__name__)



# ──────────────────────────────────────────────────────────
# 定数
# ──────────────────────────────────────────────────────────

JMDICT_URL = "http://ftp.edrdg.org/pub/Nihongo/JMdict.gz"

REPOS = [
    ("https://github.com/yomidevs/jmdict-yomitan", "jmdict-yomitan"),
    ("https://github.com/hingston/japanese",        "japanese"),
    ("https://github.com/aozorahack/aozorabunko_text", "aozorabunko_text"),
]

UUID_NAMESPACE = uuid.UUID(bytes=b"\x00" * 16)

# スクリプトファイルの場所から repo root を確定（どこから実行しても同じ結果）
_SCRIPT_DIR = Path(__file__).resolve().parent   # …/scripts/
_REPO_ROOT  = _SCRIPT_DIR.parent                # …/Genji/

# JMdict エンティティ名 → 日本語品詞名
_POS_ENTITY_MAP: dict[str, str] = {
    # 名詞
    "n":        "名詞",
    "n-adv":    "副詞的名詞",
    "n-pref":   "名詞-接頭辞",
    "n-suf":    "名詞-接尾辞",
    "n-t":      "名詞-時相名詞",
    "num":      "数詞",
    # 動詞（一段）
    "v1":       "動詞-一段",
    "v1-s":     "動詞-一段-くれる",
    "vz":       "動詞-ずる変",
    # 動詞（五段）
    "v5aru":    "動詞-五段-ある",
    "v5b":      "動詞-五段-バ行",
    "v5g":      "動詞-五段-ガ行",
    "v5k":      "動詞-五段-カ行",
    "v5k-s":    "動詞-五段-行く",
    "v5m":      "動詞-五段-マ行",
    "v5n":      "動詞-五段-ナ行",
    "v5r":      "動詞-五段-ラ行",
    "v5r-i":    "動詞-五段-ラ行-不規則",
    "v5s":      "動詞-五段-サ行",
    "v5t":      "動詞-五段-タ行",
    "v5u":      "動詞-五段-ウ行",
    "v5u-s":    "動詞-五段-ウ行-特殊",
    "v5uru":    "動詞-五段-ウル",
    # 動詞（その他）
    "vi":       "動詞-自動詞",
    "vk":       "動詞-来る",
    "vn":       "動詞-ぬ変",
    "vr":       "動詞-り変",
    "vs":       "動詞-サ変",
    "vs-c":     "動詞-サ変-す",
    "vs-i":     "動詞-サ変-する",
    "vs-s":     "動詞-サ変-特殊",
    "vt":       "動詞-他動詞",
    "v2a-s":    "動詞-二段-ウ行-古典",
    "v4h":      "動詞-四段-ハ行-古典",
    "v4r":      "動詞-四段-ラ行-古典",
    # 形容詞
    "adj-i":    "形容詞",
    "adj-ix":   "形容詞-良い型",
    "adj-ku":   "形容詞-ク活用",
    "adj-shiku":"形容詞-シク活用",
    "adj-na":   "形容動詞",
    "adj-nari": "形容動詞-なり活用",
    "adj-no":   "名詞-の形容詞",
    "adj-pn":   "連体詞",
    "adj-t":    "形容詞-たる",
    "adj-f":    "形容詞-語幹",
    # 副詞・接続詞
    "adv":      "副詞",
    "adv-to":   "副詞-と",
    "conj":     "接続詞",
    # 助詞・助動詞
    "aux":      "助動詞",
    "aux-adj":  "補助形容詞",
    "aux-v":    "補助動詞",
    "cop":      "コピュラ",
    "prt":      "助詞",
    # その他
    "ctr":      "助数詞",
    "exp":      "表現",
    "int":      "感動詞",
    "pn":       "代名詞",
    "pref":     "接頭辞",
    "suf":      "接尾辞",
    "unc":      "未分類",
}

# JMdict エンティティ解決後の英語説明文 → 日本語品詞名
# （Python ET が DTD を自動解決した場合に使用）
_POS_ENGLISH_MAP: dict[str, str] = {
    "noun (common) (futsuumeishi)":                                 "名詞",
    "adverbial noun (fukushitekimeishi)":                           "副詞的名詞",
    "noun, used as a prefix":                                       "名詞-接頭辞",
    "noun, used as a suffix":                                       "名詞-接尾辞",
    "noun (temporal) (jisoumeishi)":                                "名詞-時相名詞",
    "numeric":                                                       "数詞",
    "Ichidan verb":                                                  "動詞-一段",
    "Ichidan verb - kureru special class":                          "動詞-一段-くれる",
    "Ichidan verb - zuru verb (alternative form of -jiru verbs)":   "動詞-ずる変",
    "Godan verb - -aru special class":                              "動詞-五段-ある",
    "Godan verb with 'bu' ending":                                  "動詞-五段-バ行",
    "Godan verb with 'gu' ending":                                  "動詞-五段-ガ行",
    "Godan verb with 'ku' ending":                                  "動詞-五段-カ行",
    "Godan verb - Iku/Yuku special class":                          "動詞-五段-行く",
    "Godan verb with 'mu' ending":                                  "動詞-五段-マ行",
    "Godan verb with 'nu' ending":                                  "動詞-五段-ナ行",
    "Godan verb with 'ru' ending":                                  "動詞-五段-ラ行",
    "Godan verb with 'ru' ending (irregular verb)":                 "動詞-五段-ラ行-不規則",
    "Godan verb with 'su' ending":                                  "動詞-五段-サ行",
    "Godan verb with 'tu' ending":                                  "動詞-五段-タ行",
    "Godan verb with 'u' ending":                                   "動詞-五段-ウ行",
    "Godan verb with 'u' ending (special class)":                   "動詞-五段-ウ行-特殊",
    "Godan verb - Uru old class verb (old form of Eru)":            "動詞-五段-ウル",
    "intransitive verb":                                             "動詞-自動詞",
    "Kuru verb - special class":                                    "動詞-来る",
    "irregular nu verb":                                             "動詞-ぬ変",
    "irregular ru verb, plain form ends with -ri":                  "動詞-り変",
    "noun or participle which takes the aux. verb suru":            "動詞-サ変",
    "su verb - precursor to the modern suru":                       "動詞-サ変-す",
    "suru verb - included":                                          "動詞-サ変-する",
    "suru verb - special class":                                    "動詞-サ変-特殊",
    "transitive verb":                                               "動詞-他動詞",
    "Nidan verb with 'u' ending (archaic)":                         "動詞-二段-ウ行-古典",
    "Yodan verb with 'hu/fu' ending (archaic)":                     "動詞-四段-ハ行-古典",
    "Yodan verb with 'ru' ending (archaic)":                        "動詞-四段-ラ行-古典",
    "adjective (keiyoushi)":                                        "形容詞",
    "adjective (keiyoushi) - yoi/ii class":                        "形容詞-良い型",
    "adjective (keiyoushi) - ku adjective (archaic)":              "形容詞-ク活用",
    "adjective (keiyoushi) - shiku adjective (archaic)":           "形容詞-シク活用",
    "adjectival nouns or quasi-adjectives (keiyodoshi)":           "形容動詞",
    "classical Japanese adjective (keiyodoshi) - nari class":      "形容動詞-なり活用",
    "nouns which may take the genitive case particle 'no'":        "名詞-の形容詞",
    "pre-noun adjectival (rentaishi)":                              "連体詞",
    "'taru' adjective":                                              "形容詞-たる",
    "noun or verb acting prenominally":                             "形容詞-語幹",
    "adverb (fukushi)":                                              "副詞",
    "adverb taking the 'to' particle":                              "副詞-と",
    "conjunction":                                                   "接続詞",
    "auxiliary":                                                     "助動詞",
    "auxiliary adjective":                                           "補助形容詞",
    "auxiliary verb":                                                "補助動詞",
    "copula":                                                        "コピュラ",
    "particle":                                                      "助詞",
    "counter":                                                       "助数詞",
    "expressions (phrases, clauses, etc.)":                         "表現",
    "interjection (kandoushi)":                                     "感動詞",
    "pronoun":                                                       "代名詞",
    "prefix":                                                        "接頭辞",
    "suffix":                                                        "接尾辞",
    "unclassified":                                                  "未分類",
}

# 小文字ひらがな → 大文字ひらがな
_SMALL_TO_LARGE = {
    "ぁ": "あ", "ぃ": "い", "ぅ": "う", "ぇ": "え", "ぉ": "お",
    "っ": "つ", "ゃ": "や", "ゅ": "ゆ", "ょ": "よ", "ゎ": "わ",
    "ァ": "ア", "ィ": "イ", "ゥ": "ウ", "ェ": "エ", "ォ": "オ",
    "ッ": "ツ", "ャ": "ヤ", "ュ": "ユ", "ョ": "ヨ", "ヮ": "ワ",
}

# 青空文庫記法クリーニング
_RE_RUBY       = re.compile(r"《[^》]*》")
_RE_ANNOTATION = re.compile(r"［＃[^］]*］")
_RE_GAIJI      = re.compile(r"※[^※]*※")

_FOOTER_MARKERS = frozenset([
    "底本：", "底本:", "入力者：", "校正者：", "入力:", "校正:",
    "翻訳者：", "翻訳:",
])
_SENTENCE_END = re.compile(r"(?<=[。！？])")


# ──────────────────────────────────────────────────────────
# 進捗表示
# ──────────────────────────────────────────────────────────

class Progress:
    """
    GitHub Actions web ログ / ローカル端末 両対応の進捗表示。

    GitHub Actions では:
      - ::group:: / ::endgroup:: で折り畳みセクションを生成
      - ::warning:: / ::error:: でアノテーションを生成
      - \r は使わず、定間隔で新しい行を出力
    """

    IS_GHA   = os.environ.get("GITHUB_ACTIONS") == "true"
    BAR_W    = 28  # プログレスバーの幅

    @staticmethod
    def _bar(done: int, total: int) -> str:
        if total <= 0:
            return f"[{'?' * Progress.BAR_W}] ??.?%"
        pct    = min(done / total, 1.0)
        filled = round(pct * Progress.BAR_W)
        return f"[{'█' * filled}{'░' * (Progress.BAR_W - filled)}] {pct:5.1%}"

    @staticmethod
    def group(title: str) -> None:
        if Progress.IS_GHA:
            print(f"::group::{title}", flush=True)
        else:
            print(f"\n┌─ {title}", flush=True)

    @staticmethod
    def endgroup() -> None:
        if Progress.IS_GHA:
            print("::endgroup::", flush=True)
        # ローカルでは省略（group の区切りは次の group で十分）

    @staticmethod
    def step(msg: str) -> None:
        print(f"  │  {msg}", flush=True)

    @staticmethod
    def ok(msg: str) -> None:
        print(f"  └✓ {msg}", flush=True)

    @staticmethod
    def warn(msg: str) -> None:
        if Progress.IS_GHA:
            print(f"::warning::{msg}", flush=True)
        else:
            print(f"  │⚠ {msg}", flush=True)

    @staticmethod
    def bar_line(done: int, total: int, suffix: str = "") -> None:
        """総数が既知の場合のバー付き進捗行（新しい行として出力）"""
        bar = Progress._bar(done, total)
        print(f"  │  {bar}  {suffix}", flush=True)

    @staticmethod
    def count_line(n: int, suffix: str = "") -> None:
        """総数が未知の場合のカウント進捗行"""
        print(f"  │  {n:>10,}  {suffix}", flush=True)


# ──────────────────────────────────────────────────────────
# データクラス
# ──────────────────────────────────────────────────────────

@dataclass
class RawSense:
    pos:       list[str] = field(default_factory=list)
    gloss_jpn: list[str] = field(default_factory=list)
    gloss_eng: list[str] = field(default_factory=list)
    misc:      list[str] = field(default_factory=list)


@dataclass
class RawEntry:
    seq:         str
    kanji_forms: list[str]             = field(default_factory=list)
    readings:    list[str]             = field(default_factory=list)
    re_restr:    dict[str, list[str]]  = field(default_factory=dict)
    senses:      list[RawSense]        = field(default_factory=list)


@dataclass
class SenseOutput:
    index:       int
    gloss:       str
    register:    str              = "standard"
    nuance:      Optional[str]    = None
    scenarios:   list             = field(default_factory=list)
    sensory_tags: dict            = field(default_factory=lambda: {
        "colors": [], "temperature": None, "sounds": [], "emotions": []
    })
    collocations: list            = field(default_factory=list)
    examples:    dict             = field(default_factory=lambda: {
        "standard": [], "literary": []
    })


@dataclass
class OutputRecord:
    uuid:                str
    entry:               str
    reading_primary:     str
    reading_alternatives: list[str]
    is_heteronym:        bool
    pos:                 list[str]
    freq_rank:           Optional[int]
    senses:              list[SenseOutput]
    # 出典別の総出現回数。例: {"aozora": 1234}。疎フィールド（ヒット時のみ設定）
    frequencies:         dict[str, int] = field(default_factory=dict)


# ──────────────────────────────────────────────────────────
# ユーティリティ
# ──────────────────────────────────────────────────────────

def hiragana_to_katakana(text: str) -> str:
    # NFKC で半角カタカナ → 全角カタカナに正規化してからひらがな→カタカナ変換
    text = unicodedata.normalize("NFKC", text)
    return "".join(
        chr(ord(c) + 0x60) if "\u3041" <= c <= "\u3096" else c
        for c in text
    )


def get_initial_hiragana(katakana_key: str) -> str:
    """カタカナ読みの先頭文字をひらがな（ディレクトリ名）に変換"""
    if not katakana_key:
        return "記号"
    # NFKC 正規化で半角カタカナ（ﾀ等）を全角に統一してから処理
    first = unicodedata.normalize("NFKC", katakana_key[0])
    # カタカナ → ひらがな
    if "\u30A1" <= first <= "\u30F6":
        first = chr(ord(first) - 0x60)
    # 小文字 → 大文字正規化
    first = _SMALL_TO_LARGE.get(first, first)
    # ひらがな
    if "\u3041" <= first <= "\u3096":
        return first
    # アルファベット
    if first.isalpha():
        return first.upper()
    return "記号"


def is_japanese_text(text: str) -> bool:
    for ch in text:
        cp = ord(ch)
        if (
            0x3040 <= cp <= 0x30FF   # ひらがな・カタカナ
            or 0x4E00 <= cp <= 0x9FFF  # CJK統合漢字
            or 0x3400 <= cp <= 0x4DBF  # CJK拡張A
        ):
            return True
    return False


def compute_uuid_v5(kanji: str, reading: str) -> str:
    return str(uuid.uuid5(UUID_NAMESPACE, f"{kanji}:{reading}"))


def map_pos(codes: list[str]) -> list[str]:
    """品詞コード（エンティティ名 or 英語説明文）を日本語に変換"""
    result = []
    for code in codes:
        if code in _POS_ENTITY_MAP:
            result.append(_POS_ENTITY_MAP[code])
        elif code in _POS_ENGLISH_MAP:
            result.append(_POS_ENGLISH_MAP[code])
        elif code:
            result.append(code)  # 未知コードはそのまま保持
    return result or ["未分類"]


def record_to_dict(rec: OutputRecord, updated_at: str) -> dict:
    meta: dict = {
        "version":    "1.0.0",
        "source":     "JMdict, Aozora-Crawler, Illusions-Core",
        "updated_at": updated_at,
    }
    if rec.freq_rank is not None:
        meta["freq_rank"] = rec.freq_rank
    if rec.frequencies:
        # ソース名 → 総出現回数。出現の多い順にソートして安定出力
        meta["frequencies"] = dict(
            sorted(rec.frequencies.items(), key=lambda kv: (-kv[1], kv[0]))
        )

    return {
        "uuid":  rec.uuid,
        "entry": rec.entry,
        "reading": {
            "primary":      rec.reading_primary,
            "alternatives": rec.reading_alternatives,
            "is_heteronym": rec.is_heteronym,
        },
        "grammar": {
            "pos":         rec.pos,
            "ctype":       None,
            "inflections": None,
        },
        "definitions": [
            {
                "index":        s.index,
                "gloss":        s.gloss,
                "register":     s.register,
                "nuance":       s.nuance,
                "scenarios":    s.scenarios,
                "sensory_tags": s.sensory_tags,
                "collocations": s.collocations,
                "examples":     s.examples,
            }
            for s in rec.senses
        ],
        "relations": {
            "homophones": [],
            "synonyms":   [],
            "antonyms":   [],
            "related":    [],
        },
        "meta": meta,
    }


# ──────────────────────────────────────────────────────────
# Phase 0: リポジトリ管理
# ──────────────────────────────────────────────────────────

def _git_op(url: str, name: str, tmp_dir: Path, skip_pull: bool) -> str:
    """スレッドワーカー: 1リポジトリの clone/pull を実行して状態文字列を返す"""
    dest = tmp_dir / name
    if dest.exists():
        if skip_pull:
            return f"skip  {name}"
        try:
            subprocess.run(
                ["git", "-C", str(dest), "pull", "--ff-only"],
                check=True, capture_output=True, timeout=120,
            )
            return f"pull  {name}  → up to date"
        except subprocess.SubprocessError as e:
            return f"WARN  {name}  pull 失敗: {e}"
    else:
        try:
            subprocess.run(
                ["git", "clone", "--depth=1", url, str(dest)],
                check=True, capture_output=True, timeout=600,
            )
            return f"clone {name}  → cloned"
        except subprocess.SubprocessError as e:
            return f"WARN  {name}  clone 失敗: {e}"


def setup_repositories(tmp_dir: Path, skip_pull: bool = False) -> None:
    Progress.group(f"Phase 0 │ リポジトリ準備  (並列 {len(REPOS)} repos)")
    with ThreadPoolExecutor(max_workers=len(REPOS)) as pool:
        futs = {
            pool.submit(_git_op, url, name, tmp_dir, skip_pull): name
            for url, name in REPOS
        }
        for fut in as_completed(futs):
            msg = fut.result()
            if msg.startswith("WARN"):
                Progress.warn(msg[5:])
            else:
                Progress.step(msg)
    Progress.ok("リポジトリ準備完了")
    Progress.endgroup()


# ──────────────────────────────────────────────────────────
# Phase 1: 頻度マップ
# ──────────────────────────────────────────────────────────

def load_frequency_map(freq_file: Path) -> dict[str, int]:
    freq_map: dict[str, int] = {}
    if not freq_file.exists():
        log.warning("頻度ファイルが見つかりません: %s", freq_file)
        return freq_map
    with freq_file.open(encoding="utf-8", errors="replace") as f:
        for rank, line in enumerate(f, start=1):
            word = line.rstrip("\n")
            if word and word not in freq_map:
                freq_map[word] = rank
    log.info("頻度マップ: %d語", len(freq_map))
    return freq_map


# ──────────────────────────────────────────────────────────
# Phase 2: JMdict ダウンロード
# ──────────────────────────────────────────────────────────

def download_and_decompress_jmdict(tmp_dir: Path, force: bool = False) -> Path:
    xml_path = tmp_dir / "JMdict"
    gz_path  = tmp_dir / "JMdict.gz"
    cache_max_age = 86400  # 24時間

    Progress.group("Phase 2 │ JMdict 取得")

    if (
        not force
        and xml_path.exists()
        and (time.time() - xml_path.stat().st_mtime) < cache_max_age
    ):
        Progress.step(f"キャッシュ使用: {xml_path.name}  "
                      f"({xml_path.stat().st_size / 1e6:.1f} MB)")
        Progress.ok("スキップ（24h キャッシュ有効）")
        Progress.endgroup()
        return xml_path

    Progress.step(f"ダウンロード: {JMDICT_URL}")

    # ダウンロード進捗フック（10% ごとに1行出力）
    _last: list[int] = [-1]
    def _hook(block: int, block_size: int, total: int) -> None:
        if total <= 0:
            return
        done = min(block * block_size, total)
        pct  = int(done / total * 100)
        if pct >= _last[0] + 10:
            _last[0] = pct
            Progress.bar_line(done, total,
                              f"{done/1e6:5.1f} MB / {total/1e6:.1f} MB")

    try:
        urllib.request.urlretrieve(JMDICT_URL, str(gz_path), reporthook=_hook)
    except Exception as e:
        raise RuntimeError(f"JMdict ダウンロード失敗: {e}") from e

    Progress.step("解凍中...")
    written = 0
    with gzip.open(gz_path, "rb") as f_in, xml_path.open("wb") as f_out:
        while True:
            chunk = f_in.read(1 << 20)
            if not chunk:
                break
            f_out.write(chunk)
            written += len(chunk)

    size_mb = xml_path.stat().st_size / 1e6
    Progress.ok(f"完了  {xml_path.name}  ({size_mb:.1f} MB)")
    Progress.endgroup()
    return xml_path


# ──────────────────────────────────────────────────────────
# Phase 3: JMdict XML パース
# ──────────────────────────────────────────────────────────

def _make_entity_injected_stream(xml_path: Path) -> BytesIO:
    """
    JMdict の DTD エンティティ定義を手動注入したバイトストリームを返す。
    Python の ET が DTD 解決に失敗した場合のフォールバック。
    """
    # エンティティ名 → 日本語で注入（出力がすでに日本語になる）
    all_entities = {**_POS_ENTITY_MAP}
    # misc / field エンティティも追加（未知でもエラーにならないよう空定義）
    extra = [
        "uk", "ik", "io", "oK", "iK", "oR", "iR",
        "abbr", "arch", "col", "derog", "fam", "fem", "hon", "hum",
        "id", "joc", "m-sl", "male", "obs", "obsc", "on-mim", "poet",
        "pol", "rare", "sens", "sl", "uK", "vulg", "X", "yoji",
        "MA", "anat", "archit", "astron", "baseb", "biol", "bot", "bus",
        "chem", "comp", "econ", "engr", "finc", "food", "geol", "geom",
        "gramm", "grmil", "law", "ling", "logic", "mahj", "math", "med",
        "mil", "music", "ornith", "physics", "print", "shinto", "sports",
        "sumo", "tech", "telec", "tradem", "vidg", "zool",
        "gikun", "ateji", "rK", "sK",
    ]
    for k in extra:
        if k not in all_entities:
            all_entities[k] = k

    entity_block = "\n".join(
        f'<!ENTITY {k} "{v.replace("&", "&amp;").replace(chr(34), "&quot;")}">'
        for k, v in all_entities.items()
    )
    doctype = f"""<!DOCTYPE JMdict [
{entity_block}
]>"""

    raw = xml_path.read_bytes()
    # 既存 DOCTYPE を除去（DTD がインラインの場合）
    raw = re.sub(rb"<!DOCTYPE[^[]*\[.*?\]>", b"", raw, flags=re.DOTALL)
    # xml 宣言の直後に我々の DOCTYPE を挿入
    decl_end = raw.find(b"?>")
    if decl_end == -1:
        decl_end = 0
    else:
        decl_end += 2
    raw = raw[:decl_end] + b"\n" + doctype.encode("utf-8") + b"\n" + raw[decl_end:]
    return BytesIO(raw)


def parse_jmdict_stream(xml_path: Path) -> Iterator[RawEntry]:
    """JMdict XML を iterparse でストリーム処理し、RawEntry を yield"""
    log.info("JMdict パース開始...")

    def _iter(source) -> Iterator[RawEntry]:
        current:      Optional[RawEntry] = None
        current_sense: Optional[RawSense] = None
        current_reb:  Optional[str] = None
        re_restr_buf: list[str] = []
        in_k_ele = False
        in_r_ele = False
        in_sense = False

        for event, elem in ET.iterparse(source, events=("start", "end")):
            tag = elem.tag
            if event == "start":
                if   tag == "entry":  current = RawEntry(seq="")
                elif tag == "k_ele":  in_k_ele = True
                elif tag == "r_ele":  in_r_ele = True; current_reb = None; re_restr_buf = []
                elif tag == "sense":  in_sense = True; current_sense = RawSense()

            else:  # end
                if tag == "ent_seq" and current is not None:
                    current.seq = elem.text or ""

                elif tag == "keb" and in_k_ele and current is not None:
                    keb = (elem.text or "").strip()
                    if keb:
                        current.kanji_forms.append(keb)

                elif tag == "k_ele":
                    in_k_ele = False

                elif tag == "reb" and in_r_ele and current is not None:
                    current_reb = (elem.text or "").strip()
                    if current_reb:
                        current.readings.append(current_reb)

                elif tag == "re_restr" and in_r_ele and current is not None:
                    restr = (elem.text or "").strip()
                    if restr:
                        re_restr_buf.append(restr)

                elif tag == "r_ele":
                    in_r_ele = False
                    if current_reb and re_restr_buf and current is not None:
                        current.re_restr[current_reb] = re_restr_buf[:]
                    current_reb = None
                    re_restr_buf = []

                elif tag == "pos" and in_sense and current_sense is not None:
                    t = (elem.text or "").strip()
                    if t:
                        current_sense.pos.append(t)

                elif tag == "gloss" and in_sense and current_sense is not None:
                    lang = elem.get(
                        "{http://www.w3.org/XML/1998/namespace}lang", "eng"
                    )
                    t = (elem.text or "").strip()
                    if t:
                        if lang == "jpn":
                            current_sense.gloss_jpn.append(t)
                        elif lang in ("eng", ""):
                            current_sense.gloss_eng.append(t)

                elif tag == "misc" and in_sense and current_sense is not None:
                    t = (elem.text or "").strip()
                    if t:
                        current_sense.misc.append(t)

                elif tag == "sense":
                    in_sense = False
                    if current_sense is not None and current is not None:
                        current.senses.append(current_sense)
                    current_sense = None

                elif tag == "entry":
                    if current is not None:
                        yield current
                    current = None
                    elem.clear()

    # まず直接 iterparse を試みる（JMdict はインライン DTD を持つため通常成功）
    try:
        yield from _iter(str(xml_path))
    except ET.ParseError as e:
        log.warning("直接パース失敗（エンティティ注入モードで再試行）: %s", e)
        stream = _make_entity_injected_stream(xml_path)
        yield from _iter(stream)


# ──────────────────────────────────────────────────────────
# Phase 4: レコード変換
# ──────────────────────────────────────────────────────────

def _resolve_gloss(sense: RawSense) -> Optional[str]:
    if sense.gloss_jpn:
        return sense.gloss_jpn[0]
    if sense.gloss_eng:
        return sense.gloss_eng[0]
    return None


_REPORT_SEC      = 3.0   # 全フェーズ共通の進捗出力間隔（秒）
_CHECKPOINT_SEC  = 60.0  # 青空文庫チェックポイント保存間隔（秒）
_CHECKPOINT_NAME = "aozora_checkpoint.json.gz"  # {tmp_dir}/ 以下に保存


def build_records(
    xml_path: Path,
    freq_map: dict[str, int],
    limit: Optional[int],
) -> tuple[list[OutputRecord], set[str], int, int]:
    """
    JMdict パース → 変換 → リストとして返す

    Returns: (records, target_words, kept_count, skipped_count)
    """
    Progress.group("Phase 3+4 │ JMdict パース & エントリ変換")
    Progress.step("ストリームパース開始...")

    records:      list[OutputRecord] = []
    target_words: set[str]           = set()
    kept    = 0
    skipped = 0
    phase_t     = time.perf_counter()
    last_report = phase_t

    for entry in parse_jmdict_stream(xml_path):
        if limit and kept >= limit:
            break

        # 日本語エントリフィルタ
        all_forms = entry.kanji_forms + entry.readings
        if not any(is_japanese_text(f) for f in all_forms):
            skipped += 1
            continue

        primary   = entry.readings[0] if entry.readings else ""
        alts      = entry.readings[1:] if len(entry.readings) > 1 else []
        is_het    = bool(entry.re_restr)
        entry_str = entry.kanji_forms[0] if entry.kanji_forms else primary

        # 頻度ランク
        freq_rank: Optional[int] = None
        for cand in [*entry.kanji_forms, primary]:
            if cand in freq_map:
                freq_rank = freq_map[cand]
                break

        # target_words 更新
        for kf in entry.kanji_forms:
            target_words.add(kf)
        if primary:
            target_words.add(primary)

        # 品詞（最初の sense から継承）
        all_pos: list[str] = []
        for sense in entry.senses:
            if sense.pos:
                all_pos = sense.pos
                break

        # sense → SenseOutput
        sense_outputs: list[SenseOutput] = []
        for i, sense in enumerate(entry.senses, start=1):
            gloss = _resolve_gloss(sense)
            if gloss is None:
                continue
            sense_outputs.append(SenseOutput(index=i, gloss=gloss))

        if not sense_outputs:
            skipped += 1
            continue

        records.append(OutputRecord(
            uuid                 = compute_uuid_v5(entry_str, primary),
            entry                = entry_str,
            reading_primary      = primary,
            reading_alternatives = alts,
            is_heteronym         = is_het,
            pos                  = map_pos(all_pos),
            freq_rank            = freq_rank,
            senses               = sense_outputs,
        ))
        kept += 1

        now = time.perf_counter()
        if now - last_report >= _REPORT_SEC:
            last_report = now
            total_seen  = kept + skipped
            rate = total_seen / (now - phase_t) if now > phase_t else 0
            Progress.count_line(
                total_seen,
                f"parsed  ({kept:,} kept / {skipped:,} skip)  "
                f"{rate:,.0f} entries/s",
            )

    elapsed = time.perf_counter() - phase_t
    Progress.ok(
        f"{kept:,} エントリ保持  {skipped:,} スキップ  "
        f"target_words: {len(target_words):,}語  "
        f"({elapsed:.1f}s)"
    )
    Progress.endgroup()
    return records, target_words, kept, skipped


# ──────────────────────────────────────────────────────────
# Phase 4.5: 青空文庫インデックス
# ──────────────────────────────────────────────────────────

def strip_aozora_markup(text: str) -> str:
    text = _RE_RUBY.sub("", text)
    text = _RE_ANNOTATION.sub("", text)
    text = _RE_GAIJI.sub("", text)
    return text


def _parse_aozora_header(lines: list[str]) -> tuple[str, str]:
    """先頭 25 行からタイトル・著者を推定"""
    non_empty = [strip_aozora_markup(l).strip() for l in lines[:25] if l.strip()]
    title  = non_empty[0] if non_empty else ""
    author = ""
    for cand in non_empty[1:4]:
        if cand and not cand.startswith("（") and not any(
            cand.startswith(m) for m in _FOOTER_MARKERS
        ):
            author = cand
            break
    return title, author


def _split_sentences(text: str) -> list[str]:
    parts = _SENTENCE_END.split(text)
    result = []
    for p in parts:
        s = p.strip()
        if 10 <= len(s) <= 150:
            result.append(s)
    return result


def _aozora_worker(
    args: tuple[list[str], dict[str, list[str]], int],
) -> tuple[dict[str, list[dict]], int, int]:
    """
    ProcessPoolExecutor 用ワーカー（モジュールレベル必須）。

    Args:
        args: (file_path_strings, word_by_first, max_per_word)

    Returns:
        (partial_index, processed_files, processed_sentences)
    """
    file_paths, word_by_first, max_per_word = args
    local: dict[str, list[dict]] = defaultdict(list)
    done_files = 0
    done_sents = 0

    for path_str in file_paths:
        txt_file = Path(path_str)
        try:
            try:
                content = txt_file.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                content = txt_file.read_text(encoding="shift_jis", errors="replace")

            lines = content.splitlines()
            title, author = _parse_aozora_header(lines[:25])

            body_lines: list[str] = []
            in_footer = False
            for line in lines:
                if any(line.strip().startswith(m) for m in _FOOTER_MARKERS):
                    in_footer = True
                if not in_footer:
                    body_lines.append(strip_aozora_markup(line))

            sentences = _split_sentences("".join(body_lines))
            done_sents += len(sentences)

            for sent in sentences:
                for i, ch in enumerate(sent):
                    if ch not in word_by_first:
                        continue
                    for word in word_by_first[ch]:
                        end = i + len(word)
                        if sent[i:end] != word:
                            continue
                        bucket = local[word]
                        if len(bucket) < max_per_word:
                            bucket.append({
                                "text":   sent,
                                "author": author,
                                "title":  title,
                            })
            done_files += 1
        except Exception:
            done_files += 1  # スキップしてもカウント
            continue

    return dict(local), done_files, done_sents


def _save_aozora_checkpoint(
    checkpoint_path: Path,
    processed_files: set[str],
    index: dict[str, list[dict]],
) -> None:
    """青空文庫チェックポイントをアトミックに保存する（gzip JSON）"""
    tmp_path = checkpoint_path.with_name(checkpoint_path.name + ".tmp")
    data = {"processed_files": list(processed_files), "index": index}
    try:
        with gzip.open(tmp_path, "wt", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        tmp_path.rename(checkpoint_path)
    except Exception as exc:
        tmp_path.unlink(missing_ok=True)
        log.warning("チェックポイント保存失敗: %s", exc)


def _load_aozora_checkpoint(
    checkpoint_path: Path,
) -> tuple[dict[str, list[dict]], set[str]]:
    """青空文庫チェックポイントを読み込む。失敗時は空を返す。"""
    try:
        with gzip.open(checkpoint_path, "rt", encoding="utf-8") as f:
            data = json.load(f)
        index    = data.get("index", {})
        proc_set = set(data.get("processed_files", []))
        log.info("チェックポイント読み込み: %d 語, %d ファイル処理済み",
                 len(index), len(proc_set))
        return index, proc_set
    except Exception as exc:
        log.warning("チェックポイント読み込み失敗（無視）: %s", exc)
        return {}, set()


def build_aozora_index(
    target_words: set[str],
    aozora_dir: Path,
    max_per_word: int = 30,
    n_workers: int = 1,
    checkpoint_path: Optional[Path] = None,
    resume: bool = False,
) -> dict[str, list[dict]]:
    """
    青空文庫テキストを1回スキャンし、語 → 例句リスト を構築する。

    2パス戦略:
      Pass 1 (完了済み) - target_words は引数として受け取る
      Pass 2 (本関数)   - 全テキストを1回だけストリーム処理

    Returns: {word: [{"text": ..., "author": ..., "title": ...}, ...]}
    """
    if not aozora_dir.exists():
        Progress.warn(f"青空文庫ディレクトリなし: {aozora_dir}")
        return {}

    Progress.group(f"Phase 4.5 │ 青空文庫 例句インデックス構築  (workers={n_workers})")

    # チェックポイントのロード（--resume 時）
    preloaded_index: dict[str, list[dict]] = {}
    already_processed: set[str] = set()
    if resume and checkpoint_path and checkpoint_path.exists():
        preloaded_index, already_processed = _load_aozora_checkpoint(checkpoint_path)
        Progress.step(f"チェックポイント復元: {len(already_processed):,} ファイル処理済み  "
                      f"{len(preloaded_index):,} 語")

    # 先頭文字でグループ化（高速照合のため）。list に変換して pickle 可能にする
    word_by_first: dict[str, list[str]] = defaultdict(list)
    for w in target_words:
        if w:
            word_by_first[w[0]].append(w)

    all_txt_files = sorted(aozora_dir.rglob("*.txt"))
    # 処理済みファイルをスキップ
    txt_files = [f for f in all_txt_files if str(f) not in already_processed]
    total_files     = len(all_txt_files)
    remaining_files = len(txt_files)
    Progress.step(f"対象テキスト: {remaining_files:,} / {total_files:,} ファイル  "
                  f"target_words: {len(target_words):,} 語  "
                  f"workers: {n_workers}")

    # ファイルリストを n_workers*8 チャンクに分割（進捗粒度を確保）
    n_chunks   = max(n_workers, n_workers * 8)
    chunk_size = max(1, (remaining_files + n_chunks - 1) // n_chunks)
    chunks: list[list[str]] = [
        [str(f) for f in txt_files[i: i + chunk_size]]
        for i in range(0, remaining_files, chunk_size)
    ]

    index: dict[str, list[dict]] = defaultdict(list, preloaded_index)
    done_files      = len(already_processed)  # 再開時は処理済み分から始める
    done_sents      = 0
    phase_t         = time.perf_counter()
    last_report     = phase_t
    last_checkpoint = phase_t
    processed_in_run: set[str] = set()

    wbf = dict(word_by_first)  # workers へ渡す（read-only コピー）

    # SIGINT ハンドラー: フラグを立てて後続ループで検知
    shutdown_evt = threading.Event()
    _orig_sigint = signal.getsignal(signal.SIGINT)

    def _sigint_handler(sig: int, frame: object) -> None:
        print("\n[SIGINT] 終了要求を受信しました。チェックポイント保存後に終了します...",
              flush=True)
        shutdown_evt.set()

    signal.signal(signal.SIGINT, _sigint_handler)

    try:
        with ProcessPoolExecutor(max_workers=n_workers) as pool:
            futures_map = {
                pool.submit(_aozora_worker, (chunk, wbf, max_per_word)): chunk
                for chunk in chunks
            }
            for fut in as_completed(futures_map):
                # SIGINT を検知したら残 future をキャンセルしてループを抜ける
                if shutdown_evt.is_set():
                    for pending in futures_map:
                        pending.cancel()
                    break

                chunk_files_list = futures_map[fut]
                partial, chunk_files, chunk_sents = fut.result()
                done_files += chunk_files
                done_sents += chunk_sents
                processed_in_run.update(chunk_files_list)

                # partial を index にマージ（上限 max_per_word を守る）
                for word, examples in partial.items():
                    existing = index[word]
                    needed   = max_per_word - len(existing)
                    if needed > 0:
                        existing.extend(examples[:needed])

                now = time.perf_counter()
                # 定期チェックポイント保存
                if checkpoint_path and now - last_checkpoint >= _CHECKPOINT_SEC:
                    last_checkpoint = now
                    all_proc = already_processed | processed_in_run
                    _save_aozora_checkpoint(checkpoint_path, all_proc, dict(index))
                    log.debug("チェックポイント保存: %d ファイル", len(all_proc))

                if now - last_report >= _REPORT_SEC:
                    last_report = now
                    elapsed_s   = now - phase_t
                    rate = (done_files - len(already_processed)) / elapsed_s if elapsed_s > 0 else 0
                    Progress.bar_line(
                        done_files, total_files,
                        f"{done_files:>6,} / {total_files:,} files  "
                        f"matched: {len(index):,} 語  "
                        f"{rate:.0f} files/s",
                    )
    finally:
        signal.signal(signal.SIGINT, _orig_sigint)
        # 終了時（正常 or SIGINT）にチェックポイントを保存
        if checkpoint_path:
            all_proc = already_processed | processed_in_run
            _save_aozora_checkpoint(checkpoint_path, all_proc, dict(index))
            log.info("チェックポイント保存完了: %s", checkpoint_path)

    if shutdown_evt.is_set():
        print(f"  チェックポイント保存完了: {checkpoint_path}", flush=True)
        sys.exit(130)  # 130 = killed by SIGINT

    Progress.ok(
        f"{done_files:,} ファイル  {done_sents:,} 文  "
        f"{len(index):,} 語にマッチ"
    )
    Progress.endgroup()
    return dict(index)


def attach_aozora_examples(
    records: list[OutputRecord],
    sentence_index: dict[str, list[dict]],
    max_examples: int = 30,
) -> None:
    """OutputRecord の definitions[0].examples.literary に例句を付与（in-place）"""
    for rec in records:
        examples: list[dict] = []
        seen: set[str] = set()

        for key in [rec.entry, rec.reading_primary]:
            for ex in sentence_index.get(key, []):
                if ex["text"] not in seen:
                    seen.add(ex["text"])
                    examples.append(ex)
                if len(examples) >= max_examples:
                    break
            if len(examples) >= max_examples:
                break

        if not examples or not rec.senses:
            continue

        literary = [
            {
                "text": ex["text"],
                "citation": {
                    "source": ex["title"] or "青空文庫",
                    "author": ex["author"],
                    "note":   "青空文庫",
                },
            }
            for ex in examples
        ]
        rec.senses[0].examples["literary"].extend(literary)


# ──────────────────────────────────────────────────────────
# Phase 4.6: 青空文庫 頻度カウント（形態素解析）
#
# 例句収集（部分文字列マッチ）とは別パラダイム。Sudachi で本文を
# 形態素解析し、内容語の dictionary_form（表記を保った原形）を
# キーに総出現回数を数える。語境界・活用を考慮するため、部分文字列
# マッチのような誤検出（短語が他語の一部に一致 等）が起きない。
#
# 注意:
#   - 底本/奥付（フッタ）と、冒頭の記法説明ブロック（2本の区切り線に
#     挟まれた「【テキスト中に現れる記号について】」等）は本文から除外し、
#     頻度に含めない。
#   - dictionary_form をキーにするため、同表記・同原形の異音異義語
#     （例: 辛い=からい/つらい）は合算される（= 総出現回数の定義に一致）。
# ──────────────────────────────────────────────────────────

_FREQ_CHECKPOINT_NAME = "aozora_freq_checkpoint.json.gz"  # {tmp_dir}/ 以下に保存
_FREQ_TABLE_NAME      = "aozora_freq.json.gz"             # 再利用可能な頻度テーブル

# 奥付（底本情報）を検出するための行頭マーカー。これらで始まる行以降は
# 本文ではないと判断し、頻度カウントから除外する。
_FREQ_FOOTER_MARKERS = frozenset([
    "底本：", "底本:", "底本の親本：", "初出：", "初出:",
    "入力：", "入力:", "入力者：", "校正：", "校正:", "校正者：",
    "翻訳者：", "翻訳:", "青空文庫作成ファイル", "●表記について",
    "【テキスト中",
])

# 区切り線（記法説明ブロックの境界）を構成しうる文字
_DELIM_CHARS = set("-‐‑‒–—―─━")

# カウント対象外の品詞（大分類）
_FREQ_SKIP_POS = frozenset(["助詞", "助動詞", "補助記号", "空白", "記号"])

# 各ワーカープロセスで 1 度だけ生成する Sudachi トークナイザ
_FREQ_TOKENIZER = None


def _get_freq_tokenizer():
    """ワーカープロセスごとに Sudachi トークナイザを遅延生成する。"""
    global _FREQ_TOKENIZER
    if _FREQ_TOKENIZER is None:
        from sudachipy import dictionary  # 遅延 import（未インストール時は呼び出し側で警告）
        _FREQ_TOKENIZER = dictionary.Dictionary(dict="core").create()
    return _FREQ_TOKENIZER


def _is_delim_line(line: str) -> bool:
    """記法説明ブロックの区切り線か（ハイフン等が 8 文字以上連続）"""
    s = line.strip()
    return len(s) >= 8 and all(c in _DELIM_CHARS for c in s)


def _extract_aozora_body_text(lines: list[str]) -> str:
    """
    青空文庫テキストから「本文」だけを抽出する（頻度カウント用）。
      - 冒頭の記法説明ブロック（2 本の区切り線に挟まれた部分）を除外
      - 奥付（フッタ）以降を除外
      - ルビ/注記/外字マークアップを除去
    """
    # 冒頭 60 行内に区切り線が 2 本以上あれば、2 本目以降を本文開始とみなす
    delim_idx = [i for i, l in enumerate(lines[:60]) if _is_delim_line(l)]
    start = delim_idx[1] + 1 if len(delim_idx) >= 2 else 0

    body: list[str] = []
    for line in lines[start:]:
        st = line.strip()
        if st and any(st.startswith(m) for m in _FREQ_FOOTER_MARKERS):
            break  # 奥付に到達。以降は本文ではない
        body.append(strip_aozora_markup(line))
    return "\n".join(body)


def _safe_chunks(s: str, limit: int = 40000):
    """Sudachi の入力長上限（約 49149 バイト）を超えないよう句点で分割する。"""
    if len(s.encode("utf-8")) <= limit:
        yield s
        return
    buf = ""
    for part in re.split(r"(?<=。)", s):
        if buf and len((buf + part).encode("utf-8")) > limit:
            yield buf
            buf = part
        else:
            buf += part
    if buf:
        yield buf


def _freq_worker(file_paths: list[str]) -> tuple[dict[str, int], int, int]:
    """
    ProcessPoolExecutor 用ワーカー（モジュールレベル必須）。
    Returns: (partial_counts, processed_files, counted_tokens)
    """
    from sudachipy import SplitMode  # 遅延 import
    tok = _get_freq_tokenizer()
    counts: dict[str, int] = defaultdict(int)
    files_done = 0
    tokens_done = 0

    for path_str in file_paths:
        try:
            txt_file = Path(path_str)
            try:
                content = txt_file.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                content = txt_file.read_text(encoding="shift_jis", errors="replace")

            body = _extract_aozora_body_text(content.splitlines())
            for raw_line in body.split("\n"):
                line = raw_line.strip()
                if not line:
                    continue
                for chunk in _safe_chunks(line):
                    try:
                        morphemes = tok.tokenize(chunk, SplitMode.C)
                    except Exception:
                        continue  # 解析不能な行はスキップ
                    for m in morphemes:
                        if m.part_of_speech()[0] in _FREQ_SKIP_POS:
                            continue
                        lemma = m.dictionary_form()
                        if not lemma or not is_japanese_text(lemma):
                            continue
                        counts[lemma] += 1
                        tokens_done += 1
            files_done += 1
        except Exception:
            files_done += 1  # スキップしてもカウント
            continue

    return dict(counts), files_done, tokens_done


def _save_freq_checkpoint(
    checkpoint_path: Path,
    processed_files: set[str],
    counts: dict[str, int],
) -> None:
    """頻度チェックポイントをアトミックに保存する（gzip JSON）"""
    tmp_path = checkpoint_path.with_name(checkpoint_path.name + ".tmp")
    data = {"processed_files": list(processed_files), "counts": counts}
    try:
        with gzip.open(tmp_path, "wt", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        tmp_path.rename(checkpoint_path)
    except Exception as exc:
        tmp_path.unlink(missing_ok=True)
        log.warning("頻度チェックポイント保存失敗: %s", exc)


def _load_freq_checkpoint(
    checkpoint_path: Path,
) -> tuple[dict[str, int], set[str]]:
    """頻度チェックポイントを読み込む。失敗時は空を返す。"""
    try:
        with gzip.open(checkpoint_path, "rt", encoding="utf-8") as f:
            data = json.load(f)
        counts   = data.get("counts", {})
        proc_set = set(data.get("processed_files", []))
        log.info("頻度チェックポイント読み込み: %d 語, %d ファイル処理済み",
                 len(counts), len(proc_set))
        return counts, proc_set
    except Exception as exc:
        log.warning("頻度チェックポイント読み込み失敗（無視）: %s", exc)
        return {}, set()


def build_corpus_frequency(
    aozora_dir: Path,
    n_workers: int = 1,
    checkpoint_path: Optional[Path] = None,
    table_path: Optional[Path] = None,
    resume: bool = False,
) -> dict[str, int]:
    """
    青空文庫テキストを形態素解析し、{dictionary_form: 総出現回数} を構築する。
    Sudachi 未インストール時は空 dict を返す（警告のみ）。
    """
    if not aozora_dir.exists():
        Progress.warn(f"青空文庫ディレクトリなし: {aozora_dir}")
        return {}

    # Sudachi の存在チェック（メインプロセスで早期に検出）
    try:
        _get_freq_tokenizer()
    except Exception as exc:
        Progress.warn(f"Sudachi が利用できないため頻度カウントをスキップ: {exc}")
        Progress.warn("`pip install -r requirements.txt` で sudachipy / sudachidict_core を導入してください")
        return {}

    Progress.group(f"Phase 4.6 │ 青空文庫 頻度カウント（形態素解析, workers={n_workers}）")

    counts: dict[str, int] = defaultdict(int)
    already_processed: set[str] = set()
    if resume and checkpoint_path and checkpoint_path.exists():
        preloaded, already_processed = _load_freq_checkpoint(checkpoint_path)
        for w, c in preloaded.items():
            counts[w] += c
        Progress.step(f"チェックポイント復元: {len(already_processed):,} ファイル処理済み  "
                      f"{len(counts):,} 語")

    all_txt_files = sorted(aozora_dir.rglob("*.txt"))
    txt_files = [f for f in all_txt_files if str(f) not in already_processed]
    total_files     = len(all_txt_files)
    remaining_files = len(txt_files)
    Progress.step(f"対象テキスト: {remaining_files:,} / {total_files:,} ファイル  workers: {n_workers}")

    n_chunks   = max(n_workers, n_workers * 8)
    chunk_size = max(1, (remaining_files + n_chunks - 1) // n_chunks)
    chunks: list[list[str]] = [
        [str(f) for f in txt_files[i: i + chunk_size]]
        for i in range(0, remaining_files, chunk_size)
    ]

    done_files      = len(already_processed)
    done_tokens     = 0
    phase_t         = time.perf_counter()
    last_report     = phase_t
    last_checkpoint = phase_t
    processed_in_run: set[str] = set()

    shutdown_evt = threading.Event()
    _orig_sigint = signal.getsignal(signal.SIGINT)

    def _sigint_handler(sig: int, frame: object) -> None:
        print("\n[SIGINT] 終了要求を受信しました。チェックポイント保存後に終了します...",
              flush=True)
        shutdown_evt.set()

    signal.signal(signal.SIGINT, _sigint_handler)

    try:
        with ProcessPoolExecutor(max_workers=n_workers) as pool:
            futures_map = {
                pool.submit(_freq_worker, chunk): chunk for chunk in chunks
            }
            for fut in as_completed(futures_map):
                if shutdown_evt.is_set():
                    for pending in futures_map:
                        pending.cancel()
                    break

                chunk_files_list = futures_map[fut]
                partial, chunk_files, chunk_tokens = fut.result()
                done_files  += chunk_files
                done_tokens += chunk_tokens
                processed_in_run.update(chunk_files_list)

                for word, c in partial.items():
                    counts[word] += c

                now = time.perf_counter()
                if checkpoint_path and now - last_checkpoint >= _CHECKPOINT_SEC:
                    last_checkpoint = now
                    all_proc = already_processed | processed_in_run
                    _save_freq_checkpoint(checkpoint_path, all_proc, dict(counts))

                if now - last_report >= _REPORT_SEC:
                    last_report = now
                    Progress.bar_line(
                        done_files, total_files,
                        f"{done_files:>6,} / {total_files:,} files  "
                        f"{len(counts):,} 語  {done_tokens:,} tokens",
                    )
    finally:
        signal.signal(signal.SIGINT, _orig_sigint)
        if checkpoint_path:
            all_proc = already_processed | processed_in_run
            _save_freq_checkpoint(checkpoint_path, all_proc, dict(counts))
            log.info("頻度チェックポイント保存完了: %s", checkpoint_path)

    if shutdown_evt.is_set():
        print(f"  頻度チェックポイント保存完了: {checkpoint_path}", flush=True)
        sys.exit(130)

    result = dict(counts)
    # 再利用可能な頻度テーブルを永続化
    if table_path:
        try:
            with gzip.open(table_path, "wt", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False)
            Progress.step(f"頻度テーブル保存: {table_path}")
        except Exception as exc:
            log.warning("頻度テーブル保存失敗: %s", exc)

    Progress.ok(f"{done_files:,} ファイル  {done_tokens:,} tokens  {len(result):,} 語")
    Progress.endgroup()
    return result


def attach_corpus_frequency(
    records: list[OutputRecord],
    freq_counts: dict[str, int],
    source: str = "aozora",
) -> int:
    """
    各レコードの見出し語（entry = dictionary_form 相当）で頻度テーブルを引き、
    meta.frequencies[source] に総出現回数を付与する（in-place）。
    ヒット件数を返す。
    """
    if not freq_counts:
        return 0
    attached = 0
    for rec in records:
        c = freq_counts.get(rec.entry, 0)
        if c > 0:
            rec.frequencies[source] = c
            attached += 1
    return attached


# ──────────────────────────────────────────────────────────
# Phase 5: 読みでグループ化
# ──────────────────────────────────────────────────────────

def group_by_reading(
    records: list[OutputRecord],
) -> dict[str, list[OutputRecord]]:
    grouped: dict[str, list[OutputRecord]] = defaultdict(list)
    for rec in records:
        key = hiragana_to_katakana(rec.reading_primary) if rec.reading_primary else "記号"
        grouped[key].append(rec)
    return dict(grouped)


# ──────────────────────────────────────────────────────────
# Phase 6: ファイル出力
# ──────────────────────────────────────────────────────────

def _write_batch(
    items: list[tuple[str, list[OutputRecord]]],
    output_base: Path,
    updated_at: str,
    dry_run: bool,
    resume: bool,
) -> tuple[int, int, int]:
    """スレッドワーカー: JSON ファイルを書き出して (entries, files, skipped) を返す"""
    entries = 0
    files   = 0
    skipped = 0
    for katakana_key, recs in items:
        initial   = get_initial_hiragana(katakana_key)
        dir_path  = output_base / initial
        file_path = dir_path / f"{katakana_key}.json"
        data = [record_to_dict(r, updated_at) for r in recs]
        entries += len(data)
        files   += 1
        if dry_run:
            continue
        # --resume: 完全に書き込み済みのファイルはスキップ
        if resume and file_path.exists():
            skipped += 1
            continue
        dir_path.mkdir(parents=True, exist_ok=True)
        # アトミック書き込み: .tmp に書いてから rename（中断しても壊れない）
        tmp_path = file_path.with_suffix(".json.tmp")
        try:
            with tmp_path.open("w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            tmp_path.rename(file_path)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise
    return entries, files, skipped


def write_output_files(
    grouped: dict[str, list[OutputRecord]],
    output_base: Path,
    updated_at: str,
    dry_run: bool = False,
    resume: bool = False,
    n_workers: int = 1,
) -> tuple[int, int]:
    """Returns: (total_entries, total_files)"""
    Progress.group(f"Phase 6 │ JSON ファイル出力  (workers={n_workers})")

    total_grouped = len(grouped)
    flags = []
    if dry_run:
        flags.append("dry-run")
    if resume:
        flags.append("resume")
    flag_str = "  [" + ", ".join(flags) + "]" if flags else ""
    Progress.step(f"出力先: {output_base}  ({total_grouped:,} ファイル予定){flag_str}")

    # 起動時に残留 .json.tmp ファイルを掃除（前回の中断で残ったもの）
    if not dry_run:
        stale = list(output_base.rglob("*.json.tmp"))
        if stale:
            Progress.step(f"残留 .json.tmp を削除: {len(stale)} ファイル")
            for p in stale:
                p.unlink(missing_ok=True)

    # sorted items をスレッド数で均等分割
    all_items  = sorted(grouped.items())
    chunk_size = max(1, (total_grouped + n_workers - 1) // n_workers)
    batches    = [
        all_items[i: i + chunk_size]
        for i in range(0, total_grouped, chunk_size)
    ]

    total_entries  = 0
    total_files    = 0
    total_skipped  = 0
    _lock       = threading.Lock()
    last_report = time.perf_counter()

    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        futs = [
            pool.submit(_write_batch, batch, output_base, updated_at, dry_run, resume)
            for batch in batches
        ]
        for fut in as_completed(futs):
            entries, files, skipped = fut.result()
            with _lock:
                total_entries += entries
                total_files   += files
                total_skipped += skipped
                now = time.perf_counter()
                if now - last_report >= _REPORT_SEC:
                    last_report = now
                    Progress.bar_line(
                        total_files, total_grouped,
                        f"{total_files:>6,} / {total_grouped:,} files  "
                        f"entries: {total_entries:,}"
                        + (f"  skipped: {total_skipped:,}" if total_skipped else ""),
                    )

    skip_msg = f"  (うち {total_skipped:,} スキップ)" if total_skipped else ""
    Progress.ok(f"{total_files:,} ファイル出力  {total_entries:,} エントリ{skip_msg}")
    Progress.endgroup()
    return total_entries, total_files


# ──────────────────────────────────────────────────────────
# メイン
# ──────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="日本語語彙データセット構築スクリプト",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--output-dir",  default=_REPO_ROOT / "data", type=Path, metavar="PATH",
                        help="JSON ファイルの出力先ディレクトリ")
    parser.add_argument("--tmp-dir",     default="/tmp",  type=Path, metavar="PATH",
                        help="一時ファイル・クローン先ディレクトリ")
    parser.add_argument("--force-dl",    action="store_true",
                        help="JMdict.gz を強制的に再ダウンロード")
    parser.add_argument("--no-git-pull", action="store_true",
                        help="git pull をスキップ")
    parser.add_argument("--no-aozora",   action="store_true",
                        help="青空文庫例句付与をスキップ")
    parser.add_argument("--no-frequency", action="store_true",
                        help="青空文庫の頻度カウント（形態素解析）をスキップ")
    parser.add_argument("--verbose",     action="store_true",
                        help="DEBUG ログを出力")
    parser.add_argument("--dry-run",     action="store_true",
                        help="ファイル書き込みをスキップ（パース・変換のみ）")
    parser.add_argument("--limit",       type=int, default=None, metavar="N",
                        help="処理するエントリ数の上限（デバッグ用）")
    parser.add_argument("--workers",     type=int, default=None, metavar="N",
                        help="使用スレッド/プロセス数（省略時: CPU コア数）")
    parser.add_argument("--resume",      action="store_true",
                        help="前回の中断から再開する（既存 JSON スキップ＋青空文庫チェックポイント復元）")
    args = parser.parse_args()

    # 作業ディレクトリを repo root に固定（どこから実行しても同じ挙動）
    os.chdir(_REPO_ROOT)

    # スレッド/プロセス数を決定
    n_workers = args.workers or max(4, (os.cpu_count() or 4) * 4)
    print(f"CPU count: {os.cpu_count()}  →  workers={n_workers}", flush=True)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    start_time = time.perf_counter()
    tracemalloc.start()

    updated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    log.info("workers=%d  (cpu_count=%s)", n_workers, os.cpu_count())

    # ── Phase 0: リポジトリ ────────────────────────────────
    setup_repositories(args.tmp_dir, skip_pull=args.no_git_pull)

    # ── Phase 1: 頻度マップ ────────────────────────────────
    Progress.group("Phase 1 │ 頻度マップ読み込み")
    freq_file = args.tmp_dir / "japanese" / "44998-japanese-words.txt"
    freq_map  = load_frequency_map(freq_file)
    Progress.ok(f"{len(freq_map):,} 語")
    Progress.endgroup()

    # ── Phase 2: JMdict ダウンロード ──────────────────────
    xml_path = download_and_decompress_jmdict(args.tmp_dir, force=args.force_dl)

    # ── Phase 3+4: パース & 変換 ───────────────────────────
    records, target_words, kept, skipped = build_records(
        xml_path, freq_map, args.limit
    )

    # ── Phase 4.5: 青空文庫 ────────────────────────────────
    aozora_matched = 0
    if not args.no_aozora:
        aozora_dir      = args.tmp_dir / "aozorabunko_text"
        checkpoint_path = args.tmp_dir / _CHECKPOINT_NAME
        sentence_index  = build_aozora_index(
            target_words, aozora_dir, n_workers=n_workers,
            checkpoint_path=checkpoint_path, resume=args.resume,
        )
        attach_aozora_examples(records, sentence_index)
        aozora_matched = len(sentence_index)

    # ── Phase 4.6: 青空文庫 頻度カウント ──────────────────
    aozora_freq_attached = 0
    if not args.no_frequency:
        aozora_dir       = args.tmp_dir / "aozorabunko_text"
        freq_ckpt_path   = args.tmp_dir / _FREQ_CHECKPOINT_NAME
        freq_table_path  = args.tmp_dir / _FREQ_TABLE_NAME
        freq_counts = build_corpus_frequency(
            aozora_dir, n_workers=n_workers,
            checkpoint_path=freq_ckpt_path, table_path=freq_table_path,
            resume=args.resume,
        )
        aozora_freq_attached = attach_corpus_frequency(records, freq_counts, source="aozora")

    # ── Phase 5: グループ化 ────────────────────────────────
    Progress.group("Phase 5 │ 読みでグループ化")
    grouped = group_by_reading(records)
    Progress.ok(f"{len(grouped):,} ユニーク読み")
    Progress.endgroup()

    # ── Phase 6: 出力 ──────────────────────────────────────
    total_entries, total_files = write_output_files(
        grouped, args.output_dir, updated_at,
        dry_run=args.dry_run, resume=args.resume, n_workers=n_workers,
    )

    # ── 統計 ───────────────────────────────────────────────
    elapsed     = time.perf_counter() - start_time
    _, peak_mem = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    freq_matched = sum(1 for r in records if r.freq_rank is not None)
    used_dirs    = len({get_initial_hiragana(k) for k in grouped})

    Progress.group("Summary │ Build Statistics")
    print(f"  Workers (threads/procs)   : {n_workers:>10}", flush=True)
    print(f"  Total XML entries parsed  : {kept + skipped:>10,}", flush=True)
    print(f"  Japanese entries kept     : {kept:>10,}", flush=True)
    print(f"  Entries skipped (filter)  : {skipped:>10,}", flush=True)
    print(f"  Unique readings (files)   : {total_files:>10,}", flush=True)
    print(f"  Total entries output      : {total_entries:>10,}", flush=True)
    print(f"  Frequency matched         : {freq_matched:>10,}", flush=True)
    print(f"  Aozora words with examples: {aozora_matched:>10,}", flush=True)
    print(f"  Aozora frequency attached : {aozora_freq_attached:>10,}", flush=True)
    print(f"  Output directories used   : {used_dirs:>10,}", flush=True)
    print(f"  Elapsed time              : {elapsed:>10.1f}s", flush=True)
    print(f"  Peak memory               : {peak_mem / 1e6:>10.1f} MB", flush=True)
    Progress.endgroup()


if __name__ == "__main__":
    main()
