#!/usr/bin/env python3
"""
new_words.txt から幻辭の新語条を作成し、表記揺れを既存エントリへ吸収するスクリプト。

処理の概要
==========
1. `new_words.txt`（`語<TAB>頻度`）を読み込む。
2. 各語を **正規化**して canonical 形を求める：
     canonical = 旧字体→新字体テーブル( Sudachi.normalized_form(語) )
   これにより歴史的仮名遣い（ゐる→居る・思ふ→思う）と旧字体（來→来・顏→顔）を
   現代表記へ畳み込む。読み・品詞も canonical を再解析して導出する。
3. canonical（または元表記）が既存 DB（entry ∪ reading_primary）に在れば
   **吸収（absorb）**：その既存エントリ JSON に
     - 旧表記を `meta.variant_writings` へ追加（重複排除）
     - 旧表記で青空文庫から拾った例句を `definitions[0].examples.literary` へ付与
   無ければ **新語（new）**：canonical を見出しにスケルトン・エントリを生成
   （読み・品詞・青空例句のみ。`gloss` は空、`meta.needs_gloss=true`。語義は後で AI 付与）。
4. 青空例句は `build_dictionary.build_aozora_index` を再利用して1回のコーパス走査で収集。
5. 確定読音は `data/<頭文字>/<カタカナ読み>.json`、Unicode 上の仮名として
   完結しない読音は `pending/needs_reading/` へ出力する。ファイル単位で
   load → 変更/追記 → atomic replace し、正式データへの混入を防ぐ。

このスクリプトは語義（gloss）を生成しない。空欄のまま残し、後工程の AI 付与を
`meta.needs_gloss=true` で追跡できるようにする（GitHub issue 参照）。

依存: `.venv`（homebrew python3.13 + requirements.txt の sudachipy / sudachidict_core）。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import Optional

# build_dictionary を同ディレクトリから import 再利用（副作用なし＝定義のみ）
_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent
sys.path.insert(0, str(_SCRIPT_DIR))
import build_dictionary as bd  # noqa: E402
from dictionary_rules import (  # noqa: E402
    expected_data_path,
    is_valid_reading,
    quarantine_relative_path,
    reading_to_file_key,
)

log = logging.getLogger("create_entries")

# ──────────────────────────────────────────────────────────
# 既定パス
# ──────────────────────────────────────────────────────────
_DEFAULT_NEW_WORDS = _PROJECT_ROOT / "new_words.txt"
_DEFAULT_DB = _PROJECT_ROOT / "genji.db"
_DEFAULT_DATA = _PROJECT_ROOT / "data"
_DEFAULT_PENDING = _PROJECT_ROOT / "pending" / "needs_reading"
_DEFAULT_TMP = Path("/tmp")
_AOZORA_NAME = "aozorabunko_text"
_CKPT_NAME = "aozora_newword_examples_checkpoint.json.gz"

# ──────────────────────────────────────────────────────────
# 旧字体 → 新字体 文字テーブル（高信頼の常用漢字・旧字体対応の抜粋）
#
# Sudachi の normalized_form が拾えない単独 OOV 旧字体（來/顏/樣 等）を補う。
# 誤った合併はデータ汚染になるため、確度の高いものだけを収録する。char 単位で
# canonical 文字列の各文字に適用する（來客→来客 のような複合語も畳み込める）。
# ──────────────────────────────────────────────────────────
_KYUJI_TO_SHINJI: dict[str, str] = {
    "亞": "亜", "惡": "悪", "壓": "圧", "圍": "囲", "醫": "医", "壹": "壱",
    "逸": "逸", "隱": "隠", "榮": "栄", "營": "営", "衞": "衛", "驛": "駅",
    "謁": "謁", "圓": "円", "緣": "縁", "艷": "艶", "鹽": "塩", "奧": "奥",
    "應": "応", "橫": "横", "歐": "欧", "毆": "殴", "黃": "黄", "溫": "温",
    "穩": "穏", "假": "仮", "價": "価", "禍": "禍", "畫": "画", "會": "会",
    "悔": "悔", "海": "海", "繪": "絵", "壞": "壊", "懷": "懐", "慨": "慨",
    "槪": "概", "擴": "拡", "殼": "殻", "覺": "覚", "學": "学", "嶽": "岳",
    "樂": "楽", "喝": "喝", "渴": "渇", "褐": "褐", "罐": "缶", "卷": "巻",
    "陷": "陥", "勸": "勧", "寬": "寛", "漢": "漢", "關": "関", "歡": "歓",
    "館": "館", "顏": "顔", "氣": "気", "祈": "祈", "器": "器", "僞": "偽",
    "戲": "戯", "犧": "犠", "卻": "却", "糺": "糾", "舊": "旧", "據": "拠",
    "擧": "挙", "虛": "虚", "峽": "峡", "挾": "挟", "狹": "狭", "鄕": "郷",
    "響": "響", "曉": "暁", "勤": "勤", "謹": "謹", "區": "区", "驅": "駆",
    "勳": "勲", "薰": "薫", "羣": "群", "徑": "径", "惠": "恵", "揭": "掲",
    "溪": "渓", "經": "経", "繼": "継", "莖": "茎", "螢": "蛍", "輕": "軽",
    "藝": "芸", "缺": "欠", "儉": "倹", "劍": "剣", "圈": "圏", "檢": "検",
    "權": "権", "獻": "献", "硏": "研", "縣": "県", "險": "険", "顯": "顕",
    "驗": "験", "嚴": "厳", "戶": "戸", "吳": "呉", "誤": "誤", "效": "効",
    "廣": "広", "恆": "恒", "鑛": "鉱", "號": "号", "國": "国", "穀": "穀",
    "黑": "黒", "齋": "斎", "劑": "剤", "櫻": "桜", "册": "冊", "殺": "殺",
    "雜": "雑", "參": "参", "慘": "惨", "棧": "桟", "蠶": "蚕", "贊": "賛",
    "殘": "残", "祉": "祉", "視": "視", "齒": "歯", "兒": "児", "辭": "辞",
    "濕": "湿", "實": "実", "寫": "写", "者": "者", "煮": "煮", "釋": "釈",
    "壽": "寿", "收": "収", "臭": "臭", "從": "従", "澁": "渋", "獸": "獣",
    "縱": "縦", "祝": "祝", "肅": "粛", "處": "処", "緖": "緒", "署": "署",
    "諸": "諸", "敍": "叙", "尙": "尚", "奬": "奨", "將": "将", "牀": "床",
    "涉": "渉", "燒": "焼", "證": "証", "乘": "乗", "剩": "剰", "壤": "壌",
    "孃": "嬢", "條": "条", "淨": "浄", "狀": "状", "疊": "畳", "讓": "譲",
    "釀": "醸", "囑": "嘱", "觸": "触", "寢": "寝", "愼": "慎", "晉": "晋",
    "眞": "真", "神": "神", "盡": "尽", "圖": "図", "粹": "粋", "醉": "酔",
    "穗": "穂", "瀨": "瀬", "齊": "斉", "靑": "青", "靜": "静", "稅": "税",
    "蹟": "跡", "說": "説", "絕": "絶", "專": "専", "戰": "戦", "淺": "浅",
    "潛": "潜", "纖": "繊", "踐": "践", "錢": "銭", "禪": "禅", "曾": "曽",
    "插": "挿", "巢": "巣", "爭": "争", "莊": "荘", "搜": "捜", "插": "挿",
    "騷": "騒", "增": "増", "憎": "憎", "藏": "蔵", "贈": "贈", "臟": "臓",
    "卽": "即", "屬": "属", "續": "続", "墮": "堕", "體": "体", "對": "対",
    "帶": "帯", "滯": "滞", "臺": "台", "瀧": "滝", "擇": "択", "澤": "沢",
    "擔": "担", "膽": "胆", "團": "団", "斷": "断", "彈": "弾", "遲": "遅",
    "癡": "痴", "蟲": "虫", "晝": "昼", "鑄": "鋳", "著": "著", "廳": "庁",
    "徵": "徴", "聽": "聴", "懲": "懲", "敕": "勅", "鎭": "鎮", "塚": "塚",
    "遞": "逓", "鐵": "鉄", "點": "点", "轉": "転", "傳": "伝", "都": "都",
    "黨": "党", "盜": "盗", "燈": "灯", "當": "当", "鬪": "闘", "稻": "稲",
    "德": "徳", "獨": "独", "讀": "読", "突": "突", "屆": "届", "難": "難",
    "貳": "弐", "惱": "悩", "腦": "脳", "霸": "覇", "拜": "拝", "廢": "廃",
    "賣": "売", "梅": "梅", "麥": "麦", "發": "発", "髮": "髪", "拔": "抜",
    "繁": "繁", "晚": "晩", "卑": "卑", "祕": "秘", "碑": "碑", "彥": "彦",
    "賓": "賓", "敏": "敏", "甁": "瓶", "侮": "侮", "福": "福", "拂": "払",
    "佛": "仏", "倂": "併", "竝": "並", "塀": "塀", "餠": "餅", "邊": "辺",
    "變": "変", "辨": "弁", "瓣": "弁", "辯": "弁", "勉": "勉", "步": "歩",
    "穗": "穂", "寶": "宝", "襃": "褒", "豐": "豊", "墨": "墨", "沒": "没",
    "飜": "翻", "每": "毎", "萬": "万", "滿": "満", "免": "免", "麵": "麺",
    "默": "黙", "彌": "弥", "藥": "薬", "譯": "訳", "與": "与", "豫": "予",
    "餘": "余", "譽": "誉", "搖": "揺", "樣": "様", "謠": "謡", "來": "来",
    "賴": "頼", "亂": "乱", "覽": "覧", "欄": "欄", "龍": "竜", "壘": "塁",
    "淚": "涙", "類": "類", "勵": "励", "禮": "礼", "隸": "隷", "靈": "霊",
    "齡": "齢", "曆": "暦", "歷": "歴", "戀": "恋", "練": "練", "鍊": "錬",
    "爐": "炉", "勞": "労", "廊": "廊", "朗": "朗", "樓": "楼", "錄": "録",
    "灣": "湾", "脇": "脇", "鷗": "鴎", "每": "毎", "卷": "巻",
    "聲": "声", "攜": "携", "醬": "醤", "顚": "顛", "卷": "巻", "蠟": "蝋",
    "醱": "醗", "燄": "焔", "搔": "掻",
    # 第2弾: 残存 new_words の oov 熟語から確認した旧字/異体字
    "兩": "両", "飮": "飲", "隨": "随", "舍": "舎", "裝": "装", "濟": "済",
    "駈": "駆", "鷄": "鶏", "龜": "亀", "塲": "場", "稱": "称", "劒": "剣",
    "雙": "双", "濤": "涛", "耻": "恥", "覘": "覗", "鎗": "槍", "甞": "嘗",
    "姙": "妊", "歸": "帰", "莚": "筵", "樞": "枢", "廏": "厩", "獵": "猟",
    "蠅": "蝿", "驒": "騨", "屆": "届", "凖": "準", "劔": "剣",
    "侫": "佞", "凉": "涼", "冐": "冒", "彎": "湾", "决": "決",
}


def _apply_kyuji(text: str) -> str:
    """canonical 文字列の各文字に旧字体→新字体テーブルを適用する。"""
    return "".join(_KYUJI_TO_SHINJI.get(c, c) for c in text)


# ──────────────────────────────────────────────────────────
# 品詞マッピング（Sudachi UniDic 大分類 → 幻辭ラベル）
# ──────────────────────────────────────────────────────────
_SUDACHI_POS_MAP: dict[str, str] = {
    "名詞": "名詞",
    "代名詞": "代名詞",
    "形状詞": "形容動詞",
    "連体詞": "連体詞",
    "副詞": "副詞",
    "接続詞": "接続詞",
    "感動詞": "感動詞",
    "動詞": "動詞",
    "形容詞": "形容詞",
    "助動詞": "助動詞",
    "接頭辞": "接頭辞",
    "接尾辞": "接尾辞",
}


import re  # noqa: E402

_RE_DIGITS = re.compile(r"^[0-9０-９.,\-]+$")
_RE_ASCII = re.compile(r"^[\x00-\x7F]+$")


def _is_kana_char(c: str) -> bool:
    return ("ぁ" <= c <= "ゖ") or ("ァ" <= c <= "ヶ") or c in "ーゝゞヽヾ"


def _has_kana(s: str) -> bool:
    return any(_is_kana_char(c) for c in s)


def _has_cjk(s: str) -> bool:
    return any(0x3400 <= ord(c) <= 0x9FFF for c in s)


# 数字とみなす文字（半角/全角アラビア数字・漢数字・桁・区切り）
_NUM_CHARS = set("0123456789０１２３４５６７８９〇零一二三四五六七八九十拾百千万萬億兆")
_NUM_PUNCT = set("・.,，．、-－―ー/／:：")


def _is_numeric_word(s: str) -> bool:
    """全文字が数字・桁・区切りで、かつ数字を1つ以上含むなら True（例: 一〇一一, 二十, 4.5）。"""
    if not s:
        return True
    has_num = False
    for c in s:
        if c in _NUM_CHARS:
            has_num = True
        elif c in _NUM_PUNCT:
            continue
        else:
            return False
    return has_num


# 1 語として長すぎる＝Sudachi の誤分割（文丸ごと等）。読みがファイル名になるため
# 長すぎると Linux(ext4, 255B) で checkout 不能になる。実在語は十分これ未満。
_MAX_WORD_LEN = 30


def is_too_long(canonical: str, reading: str) -> bool:
    return len(canonical) > _MAX_WORD_LEN or len(reading) > _MAX_WORD_LEN


def is_low_quality_new(canonical: str, reading: str) -> bool:
    """新語スケルトンとして作るに値しない低品質 canonical を弾く。"""
    if not canonical:
        return True
    if is_too_long(canonical, reading):
        return True
    if _RE_DIGITS.match(canonical):           # 数字（二十→20 等）
        return True
    if _RE_ASCII.match(canonical):            # ラテン文字・記号のみ
        return True
    if len(canonical) == 1 and _is_kana_char(canonical):  # 単独かなフラグメント
        return True
    return False


def _kata_to_hira(text: str) -> str:
    return "".join(
        chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in text
    )


# ──────────────────────────────────────────────────────────
# 正規化（旧字体・旧仮名 → 現代表記）
# ──────────────────────────────────────────────────────────
_TOKENIZER = None
_SPLIT_MODE = None


def _get_tokenizer():
    global _TOKENIZER, _SPLIT_MODE
    if _TOKENIZER is None:
        from sudachipy import dictionary, tokenizer
        _TOKENIZER = dictionary.Dictionary(dict="core").create()
        _SPLIT_MODE = tokenizer.Tokenizer.SplitMode.C
    return _TOKENIZER, _SPLIT_MODE


class Analyzed:
    """1 語の解析結果。"""

    __slots__ = ("surface", "canonical", "reading", "pos", "ctype",
                 "reading_failure_reason")

    def __init__(self, surface: str, canonical: str, reading: str,
                 pos: list[str], ctype: Optional[str],
                 reading_failure_reason: Optional[str] = None) -> None:
        self.surface = surface
        self.canonical = canonical
        self.reading = reading
        self.pos = pos
        self.ctype = ctype
        self.reading_failure_reason = reading_failure_reason


def analyze_word(word: str) -> Optional[Analyzed]:
    """元表記を正規化し、canonical・読み・品詞を導出する。失敗時 None。"""
    tk, mode = _get_tokenizer()
    try:
        morphs = tk.tokenize(word, mode)
    except Exception:
        return None
    if not morphs:
        return None

    # canonical = 旧字体テーブル( normalized_form の連結 )
    canonical = _apply_kyuji("".join(m.normalized_form() for m in morphs))
    canonical = unicodedata.normalize("NFC", canonical)
    if not canonical:
        return None

    # 読みは canonical を再解析して導出（旧仮名読みの混入を防ぐ）
    try:
        rmorphs = tk.tokenize(canonical, mode)
        reading = _kata_to_hira("".join(m.reading_form() for m in rmorphs))
    except Exception:
        reading = ""
    if not is_valid_reading(reading):
        original_reading = _kata_to_hira("".join(m.reading_form() for m in morphs))
        reading = original_reading if is_valid_reading(original_reading) else ""
    failure_reason = None if reading else "sudachi_no_valid_reading"

    # 品詞は元表記先頭形態素の大分類から
    raw_pos = morphs[0].part_of_speech()
    major = raw_pos[0] if raw_pos else ""
    label = _SUDACHI_POS_MAP.get(major, major or "未分類")
    ctype = raw_pos[4] if len(raw_pos) > 4 and raw_pos[4] not in ("", "*") else None

    return Analyzed(word, canonical, reading, [label], ctype, failure_reason)


# ──────────────────────────────────────────────────────────
# 入力読み込み
# ──────────────────────────────────────────────────────────
def load_new_words(path: Path, min_count: int, limit: Optional[int]) -> list[tuple[str, int]]:
    """new_words.txt を (語, 頻度) のリストで返す（頻度降順）。"""
    rows: list[tuple[str, int]] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line:
                continue
            parts = line.split("\t")
            word = parts[0]
            count = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 1
            if count < min_count:
                continue
            rows.append((word, count))
    rows.sort(key=lambda wc: (-wc[1], wc[0]))
    if limit is not None:
        rows = rows[:limit]
    return rows


def prune_new_words(path: Path, processed: set[str]) -> tuple[int, int]:
    """処理済み（吸収/新語化）の語を new_words.txt から削除して書き戻す。

    Returns: (残存行数, 削除行数)。アトミック書き込み。
    """
    kept: list[str] = []
    removed = 0
    with path.open(encoding="utf-8") as f:
        for line in f:
            raw = line.rstrip("\n")
            if not raw:
                continue
            word = raw.split("\t", 1)[0]
            if word in processed:
                removed += 1
            else:
                kept.append(raw)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            f.write("\n".join(kept))
            if kept:
                f.write("\n")
        tmp.rename(path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    return len(kept), removed


def load_known_index(db_path: Path) -> tuple[set[str], dict[str, list[tuple[str, str]]]]:
    """
    DB から既知語を読む。

    Returns:
      known:        entry ∪ reading_primary の集合（新語判定用）
      by_entry:     entry → [(reading_primary, uuid), ...]（吸収先特定用）
    """
    import sqlite3
    known: set[str] = set()
    by_entry: dict[str, list[tuple[str, str]]] = defaultdict(list)
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        for entry, reading, uid in conn.execute(
            "SELECT entry, reading_primary, uuid FROM entries"
        ):
            if entry:
                known.add(entry)
                by_entry[entry].append((reading or "", uid))
            if reading:
                known.add(reading)
    finally:
        conn.close()
    return known, dict(by_entry)


# ──────────────────────────────────────────────────────────
# 分類: 吸収 / 新語
# ──────────────────────────────────────────────────────────
class AbsorbOp:
    """既存エントリへ旧表記＋例句を吸収する操作。"""

    __slots__ = ("target_uuid", "target_entry", "target_reading", "variant", "count")

    def __init__(self, target_uuid: str, target_entry: str, target_reading: str,
                 variant: str, count: int) -> None:
        self.target_uuid = target_uuid
        self.target_entry = target_entry
        self.target_reading = target_reading
        self.variant = variant
        self.count = count


class NewWord:
    """新語エントリ（canonical でグループ化）。"""

    __slots__ = ("canonical", "reading", "pos", "ctype", "variants", "count",
                 "reading_failure_reason")

    def __init__(self, canonical: str, reading: str, pos: list[str],
                 ctype: Optional[str], reading_failure_reason: Optional[str] = None) -> None:
        self.canonical = canonical
        self.reading = reading
        self.pos = pos
        self.ctype = ctype
        self.reading_failure_reason = reading_failure_reason
        self.variants: set[str] = set()
        self.count = 0


def classify(
    rows: list[tuple[str, int]],
    known: set[str],
    by_entry: dict[str, list[tuple[str, str]]],
    force_new: bool = False,
) -> tuple[list[AbsorbOp], dict[str, NewWord], int, set[str]]:
    """新語行を吸収 / 新語に振り分ける。

    force_new=True のとき、低品質フィルタや読み一致スキップを上書きし、
    **非数字**の語はすべて新語として登録する（数字は除外＝残置）。
    """
    absorbs: list[AbsorbOp] = []
    news: dict[str, NewWord] = {}
    skipped = 0
    low_quality = 0
    processed: set[str] = set()  # 吸収 or 新語化された元表記（new_words.txt から削除する対象）

    def _add_new(canonical: str, an: "Analyzed", word: str, count: int) -> None:
        nw = news.get(canonical)
        if nw is None:
            nw = NewWord(canonical, an.reading, an.pos, an.ctype,
                         an.reading_failure_reason)
            news[canonical] = nw
        if word != canonical:
            nw.variants.add(word)
        nw.count += count
        processed.add(word)

    for word, count in rows:
        an = analyze_word(word)
        if an is None:
            skipped += 1
            continue
        canonical = an.canonical

        # 長すぎる語（誤分割）は force_new でも作らない。ただし既存への吸収は許可
        too_long = is_too_long(canonical, an.reading)

        # canonical が既存エントリに一致 → 吸収（旧表記が元表記と異なる場合のみ）
        target = None
        if canonical in by_entry:
            cands = by_entry[canonical]
            # 読み一致を優先、無ければ先頭
            target = next((c for c in cands if c[0] == an.reading), cands[0])
            tgt_reading, tgt_uuid = target
            if word != canonical:
                absorbs.append(AbsorbOp(tgt_uuid, canonical, tgt_reading, word, count))
            processed.add(word)  # 既存エントリに解決済み
            continue

        # canonical 自体は entry に無いが、読みが既知（かな表記の既存語）→ 吸収せずスキップ
        # （読みだけ一致は誤吸収を招くため、新語化もしない安全側）
        if canonical in known or word in known:
            if force_new and not too_long and not _is_numeric_word(canonical) and not _is_numeric_word(word):
                _add_new(canonical, an, word, count)
            else:
                skipped += 1
            continue

        # 真の新語（低品質はスキップ。force_new 時は非数字を強制登録。長すぎは常に除外）
        if is_low_quality_new(canonical, an.reading):
            if force_new and not too_long and not _is_numeric_word(canonical):
                _add_new(canonical, an, word, count)
            else:
                low_quality += 1
            continue
        nw = news.get(canonical)
        if nw is None:
            nw = NewWord(canonical, an.reading, an.pos, an.ctype,
                         an.reading_failure_reason)
            news[canonical] = nw
        if word != canonical:
            nw.variants.add(word)
        nw.count += count
        processed.add(word)  # 新語エントリに寄与

    return absorbs, news, skipped + low_quality, processed


# ──────────────────────────────────────────────────────────
# 青空例句インデックス
# ──────────────────────────────────────────────────────────
def build_examples_index(
    absorbs: list[AbsorbOp],
    news: dict[str, NewWord],
    aozora_dir: Path,
    max_per_word: int,
    n_workers: int,
    checkpoint_path: Optional[Path],
    resume: bool,
) -> dict[str, list[dict]]:
    """全対象表記を1回のコーパス走査で例句収集。{表記: [{text,author,title}]}。"""
    targets: set[str] = set()
    for op in absorbs:
        targets.add(op.variant)
    for nw in news.values():
        targets.add(nw.canonical)
        targets.update(nw.variants)
    targets.discard("")
    if not targets:
        return {}
    return bd.build_aozora_index(
        target_words=targets,
        aozora_dir=aozora_dir,
        max_per_word=max_per_word,
        n_workers=n_workers,
        checkpoint_path=checkpoint_path,
        resume=resume,
    )


def _to_literary(examples: list[dict]) -> list[dict]:
    return [
        {
            "text": ex["text"],
            "citation": {
                "source": ex.get("title") or "青空文庫",
                "author": ex.get("author", ""),
                "note": "青空文庫",
            },
        }
        for ex in examples
    ]


# ──────────────────────────────────────────────────────────
# 出力（ファイル単位でアトミック書き換え）
# ──────────────────────────────────────────────────────────
def _data_file_for(reading_primary: str, data_dir: Path) -> Optional[Path]:
    """確定済みの仮名読みに対する正式データパス。未確定なら None。"""
    return expected_data_path(data_dir, reading_primary)


def _pending_file_for(entry: str, reading_primary: str, pending_dir: Path) -> Path:
    """読み未確定候補を Unicode コードポイント別の待審パスへ振り分ける。"""
    key = reading_to_file_key(reading_primary) if reading_primary else entry
    return pending_dir / quarantine_relative_path(entry, Path(f"{key}.json"))


def make_new_record(nw: NewWord, examples: list[dict], updated_at: str) -> dict:
    """新語スケルトン・エントリ（gloss 空・needs_gloss）。"""
    uid = bd.compute_uuid_v5(nw.canonical, nw.reading)
    literary = _to_literary(examples)
    meta: dict = {
        "version": "1.0.0",
        "source": "Aozora-Crawler, Illusions-NewWord",
        "updated_at": updated_at,
        "needs_gloss": True,
    }
    # Unicode 上の仮名読みとして完結しない場合は正式データへ入れず、後段で補完する。
    if not is_valid_reading(nw.reading):
        meta["needs_reading"] = True
        meta["reading_failure_reason"] = (
            nw.reading_failure_reason or "reading_is_not_valid_kana"
        )
    if nw.count:
        meta["frequencies"] = {"aozora": nw.count}
    if nw.variants:
        meta["variant_writings"] = sorted(nw.variants)
    return {
        "uuid": uid,
        "entry": nw.canonical,
        "reading": {
            "primary": nw.reading,
            "alternatives": [],
            "is_heteronym": False,
        },
        "grammar": {
            "pos": nw.pos,
            "ctype": nw.ctype,
            "inflections": None,
        },
        "definitions": [
            {
                "index": 1,
                "gloss": "",
                "register": "standard",
                "nuance": None,
                "scenarios": [],
                "sensory_tags": {"colors": [], "temperature": None, "sounds": [], "emotions": []},
                "collocations": [],
                "examples": {"standard": [], "literary": literary},
            }
        ],
        "relations": {"homophones": [], "synonyms": [], "antonyms": [], "related": []},
        "meta": meta,
    }


def _absorb_into_item(item: dict, variant: str, examples: list[dict],
                      max_examples: int, updated_at: str) -> bool:
    """既存エントリ item に旧表記＋例句を吸収（in-place）。変更したら True。"""
    changed = False
    meta = item.setdefault("meta", {})
    vw = meta.get("variant_writings") or []
    if variant not in vw and variant != item.get("entry"):
        vw.append(variant)
        meta["variant_writings"] = sorted(set(vw))
        changed = True

    if examples:
        defs = item.get("definitions")
        if defs:
            ex_block = defs[0].setdefault("examples", {"standard": [], "literary": []})
            lit = ex_block.setdefault("literary", [])
            seen = {e.get("text") for e in lit}
            for new_ex in _to_literary(examples):
                if len(lit) >= max_examples:
                    break
                if new_ex["text"] not in seen:
                    lit.append(new_ex)
                    seen.add(new_ex["text"])
                    changed = True

    if changed:
        meta["updated_at"] = updated_at
    return changed


def _load_file(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else [data]
    except Exception:
        return []


def _atomic_write(path: Path, data: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.rename(path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def apply_operations(
    absorbs: list[AbsorbOp],
    news: dict[str, NewWord],
    index: dict[str, list[dict]],
    data_dir: Path,
    pending_dir: Path,
    updated_at: str,
    max_examples: int,
    dry_run: bool,
    do_absorb: bool,
    do_new: bool,
) -> dict[str, int]:
    """吸収・新語をファイル単位で集約し、アトミックに書き換える。"""
    # 絶対ファイルパス → {"absorbs":[AbsorbOp], "news":[NewWord]}
    plan: dict[Path, dict] = defaultdict(lambda: {"absorbs": [], "news": []})

    if do_absorb:
        for op in absorbs:
            path = _data_file_for(op.target_reading, data_dir)
            if path is not None:
                plan[path]["absorbs"].append(op)
    if do_new:
        for nw in news.values():
            path = _data_file_for(nw.reading, data_dir)
            if path is None:
                path = _pending_file_for(nw.canonical, nw.reading, pending_dir)
            plan[path]["news"].append(nw)

    stats = {"files": 0, "absorbed": 0, "new_entries": 0, "pending_entries": 0,
             "examples_added": 0, "skipped_existing_new": 0}

    bd.Progress.group(f"Phase 6 │ エントリ書き出し  ({len(plan):,} ファイル予定)"
                      + ("  [dry-run]" if dry_run else ""))

    total = len(plan)
    import time
    last = time.perf_counter()
    done = 0

    for path, ops in sorted(plan.items(), key=lambda pair: str(pair[0])):
        items = _load_file(path)
        by_uuid = {it.get("uuid"): it for it in items}
        by_entry_reading = {(it.get("entry"), (it.get("reading") or {}).get("primary")): it for it in items}
        file_changed = False

        # 吸収
        for op in ops["absorbs"]:
            target = by_uuid.get(op.target_uuid) or by_entry_reading.get((op.target_entry, op.target_reading))
            if target is None:
                # DB には在るが data/ JSON に未反映（稀）→ スキップ
                continue
            exs = index.get(op.variant, [])
            before = len((target.get("definitions") or [{}])[0].get("examples", {}).get("literary", [])) if target.get("definitions") else 0
            if _absorb_into_item(target, op.variant, exs, max_examples, updated_at):
                file_changed = True
                stats["absorbed"] += 1
                after = len(target["definitions"][0]["examples"]["literary"])
                stats["examples_added"] += max(0, after - before)

        # 新語追記
        for nw in ops["news"]:
            uid = bd.compute_uuid_v5(nw.canonical, nw.reading)
            if uid in by_uuid:
                # 既に同 uuid のエントリが存在 → 新語化しない（冪等・衝突回避）
                stats["skipped_existing_new"] += 1
                continue
            exs: list[dict] = []
            seen: set[str] = set()
            for surf in [nw.canonical, *sorted(nw.variants)]:
                for ex in index.get(surf, []):
                    if ex["text"] not in seen:
                        seen.add(ex["text"])
                        exs.append(ex)
                    if len(exs) >= max_examples:
                        break
                if len(exs) >= max_examples:
                    break
            rec = make_new_record(nw, exs, updated_at)
            items.append(rec)
            by_uuid[uid] = rec
            file_changed = True
            stats["new_entries"] += 1
            if pending_dir in path.parents:
                stats["pending_entries"] += 1
            stats["examples_added"] += len(exs)

        if file_changed:
            stats["files"] += 1
            if not dry_run:
                _atomic_write(path, items)

        done += 1
        now = time.perf_counter()
        if now - last >= bd._REPORT_SEC:
            last = now
            bd.Progress.bar_line(done, total,
                                 f"{done:>6,} / {total:,} files  "
                                 f"absorbed: {stats['absorbed']:,}  new: {stats['new_entries']:,}")

    bd.Progress.ok(
        f"{stats['files']:,} ファイル変更  "
        f"吸収 {stats['absorbed']:,}  新語 {stats['new_entries']:,}  "
        f"要読音確認 {stats['pending_entries']:,}  "
        f"例句 +{stats['examples_added']:,}"
    )
    bd.Progress.endgroup()
    return stats


# ──────────────────────────────────────────────────────────
# メイン
# ──────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(
        description="new_words.txt から新語条を作成し、表記揺れを既存エントリへ吸収する",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--new-words", type=Path, default=_DEFAULT_NEW_WORDS)
    parser.add_argument("--db", type=Path, default=_DEFAULT_DB)
    parser.add_argument("--data-dir", type=Path, default=_DEFAULT_DATA)
    parser.add_argument("--pending-dir", type=Path, default=_DEFAULT_PENDING,
                        help="読み解析失敗候補の隔離先（data/ の外であること）")
    parser.add_argument("--tmp-dir", type=Path, default=_DEFAULT_TMP)
    parser.add_argument("--aozora-dir", type=Path, default=None,
                        help="既定: <tmp-dir>/aozorabunko_text")
    parser.add_argument("--workers", type=int, default=os.cpu_count() or 4)
    parser.add_argument("--min-count", type=int, default=1,
                        help="この頻度未満の語は無視（既定: 全件）")
    parser.add_argument("--max-examples", type=int, default=30,
                        help="1 エントリあたりの青空例句上限")
    parser.add_argument("--limit", type=int, default=None,
                        help="先頭 N 語のみ処理（テスト用）")
    parser.add_argument("--no-absorb", action="store_true", help="吸収を行わない")
    parser.add_argument("--no-new", action="store_true", help="新語作成を行わない")
    parser.add_argument("--no-examples", action="store_true",
                        help="青空例句収集をスキップ（分類・統計のみ）")
    parser.add_argument("--resume", action="store_true",
                        help="例句インデックスをチェックポイントから再開")
    parser.add_argument("--force-new", action="store_true",
                        help="低品質フィルタ/読み一致を上書きし、非数字の語をすべて新語登録（数字は残置）")
    parser.add_argument("--prune-input", action="store_true",
                        help="処理済み（吸収/新語化）の語を new_words.txt から削除")
    parser.add_argument("--dry-run", action="store_true",
                        help="ファイルを書き換えず統計のみ")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    args.data_dir = args.data_dir.resolve()
    args.pending_dir = args.pending_dir.resolve()
    if args.pending_dir == args.data_dir or args.data_dir in args.pending_dir.parents:
        parser.error("--pending-dir must be outside --data-dir so pending entries are not built")

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    aozora_dir = args.aozora_dir or (args.tmp_dir / _AOZORA_NAME)
    updated_at = bd._now_iso() if hasattr(bd, "_now_iso") else _now_iso()

    # Phase 1: 入力
    bd.Progress.group("Phase 1 │ 入力読み込み")
    rows = load_new_words(args.new_words, args.min_count, args.limit)
    bd.Progress.step(f"new_words: {len(rows):,} 語  (min-count={args.min_count}"
                     + (f", limit={args.limit}" if args.limit else "") + ")")
    known, by_entry = load_known_index(args.db)
    bd.Progress.step(f"既知語: {len(known):,}  既知 entry: {len(by_entry):,}")
    bd.Progress.endgroup()

    # Phase 2: 分類
    bd.Progress.group("Phase 2 │ 正規化・分類（旧字体/旧仮名の吸収判定）")
    absorbs, news, skipped, processed = classify(rows, known, by_entry, force_new=args.force_new)
    bd.Progress.ok(f"吸収候補 {len(absorbs):,}  新語 {len(news):,}  スキップ {skipped:,}")
    bd.Progress.endgroup()

    # Phase 3: 青空例句
    index: dict[str, list[dict]] = {}
    if not args.no_examples:
        ckpt = args.tmp_dir / _CKPT_NAME
        index = build_examples_index(
            absorbs, news, aozora_dir,
            max_per_word=args.max_examples,
            n_workers=args.workers,
            checkpoint_path=ckpt,
            resume=args.resume,
        )

    # Phase 6: 出力
    stats = apply_operations(
        absorbs, news, index, args.data_dir, args.pending_dir, updated_at,
        max_examples=args.max_examples,
        dry_run=args.dry_run,
        do_absorb=not args.no_absorb,
        do_new=not args.no_new,
    )

    # new_words.txt から処理済み語を削除
    pruned = (0, 0)
    if args.prune_input and not args.dry_run:
        kept, removed = prune_new_words(args.new_words, processed)
        pruned = (kept, removed)
        bd.Progress.step(f"new_words.txt 更新: {removed:,} 語削除 / {kept:,} 語残存")

    # サマリ
    bd.Progress.group("完了サマリ")
    bd.Progress.step(f"new_words 入力           : {len(rows):,}")
    bd.Progress.step(f"吸収（既存へ統合）        : {stats['absorbed']:,}")
    bd.Progress.step(f"新語エントリ作成          : {stats['new_entries']:,}")
    bd.Progress.step(f"うち読音待ち（data外）      : {stats['pending_entries']:,}")
    bd.Progress.step(f"青空例句 付与             : +{stats['examples_added']:,}")
    bd.Progress.step(f"変更ファイル              : {stats['files']:,}")
    bd.Progress.step(f"分類スキップ              : {skipped:,}")
    if stats["skipped_existing_new"]:
        bd.Progress.step(f"既存 uuid 衝突でスキップ   : {stats['skipped_existing_new']:,}")
    if args.prune_input and not args.dry_run:
        bd.Progress.step(f"new_words.txt 削除/残存    : {pruned[1]:,} / {pruned[0]:,}")
    if args.dry_run:
        bd.Progress.warn("dry-run: ファイルは変更していません")
    bd.Progress.endgroup()


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


if __name__ == "__main__":
    main()
