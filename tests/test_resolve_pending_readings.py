import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "script"))

import create_entries  # noqa: E402
from dictionary_rules import compute_uuid_v5, expected_data_path  # noqa: E402
from resolve_pending_readings import (  # noqa: E402
    STATUS_AMBIGUOUS,
    STATUS_CONFLICT,
    STATUS_INVALID,
    STATUS_FRAGMENT,
    STATUS_RESOLVED,
    STATUS_SINGLE_RUBY,
    STATUS_UNMATCHED,
    SourceIndex,
    build_aozora_index,
    build_formal_index,
    classify_entry,
    extract_aozora_ruby,
    parse_edrdg,
    resolve,
)


def record(entry: str, reading: str, *, frequency: int = 1, needs_reading: bool = True) -> dict:
    meta = {
        "version": "1.0.0",
        "source": "test",
        "needs_gloss": True,
        "frequencies": {"aozora": frequency},
    }
    if needs_reading:
        meta["needs_reading"] = True
    return {
        "uuid": compute_uuid_v5(entry, reading),
        "entry": entry,
        "reading": {"primary": reading, "alternatives": [], "is_heteronym": False},
        "grammar": {"pos": ["名詞"], "ctype": None, "inflections": None},
        "definitions": [{
            "index": 1,
            "gloss": "",
            "register": "standard",
            "examples": {"standard": [], "literary": [{"text": f"{entry}の例。"}]},
        }],
        "relations": {"homophones": [], "synonyms": [], "antonyms": [], "related": []},
        "meta": meta,
    }


def write_pending(root: Path, row: dict) -> Path:
    path = root / "U+4E00-U+4EFF" / f"{row['entry']}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([row], ensure_ascii=False), encoding="utf-8")
    return path


class EdrdgTests(unittest.TestCase):
    def test_fragment_detection_normalizes_old_forms_in_examples(self):
        item = record("児如", "児如")
        item["definitions"][0]["examples"]["literary"] = [
            {"text": "妻兒如何にと氣遣へば。"},
        ]
        self.assertEqual(classify_entry("児如", [SourceIndex("jmdict")], item)[0], STATUS_FRAGMENT)

    def test_old_say_construction_is_not_a_compound_noun(self):
        item = record("云事", "云事")
        item["definitions"][0]["examples"]["literary"] = [
            {"text": "正しいと云事は言へる。"},
        ]
        self.assertEqual(classify_entry("云事", [SourceIndex("jmdict")], item)[0], STATUS_FRAGMENT)

    def test_jmdict_respects_all_spellings_and_reading_restrictions(self):
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<JMdict><entry><ent_seq>1</ent_seq>
<k_ele><keb>甲</keb></k_ele><k_ele><keb>乙</keb></k_ele>
<r_ele><reb>こう</reb></r_ele>
<r_ele><reb>おつ</reb><re_restr>乙</re_restr></r_ele>
<r_ele><reb>かなだけ</reb><re_nokanji/></r_ele>
</entry></JMdict>"""
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "JMdict.xml"
            path.write_text(xml, encoding="utf-8")
            index = parse_edrdg(path, "jmdict")
        self.assertEqual(set(index.values["甲"]), {"こう"})
        self.assertEqual(set(index.values["乙"]), {"こう", "おつ"})
        empty = SourceIndex("formal")
        self.assertEqual(classify_entry("甲", [empty, index])[0], STATUS_RESOLVED)
        self.assertEqual(classify_entry("乙", [empty, index])[0], STATUS_AMBIGUOUS)

    def test_jmnedict_unique_full_name_is_eligible(self):
        xml = """<JMnedict><entry><ent_seq>2</ent_seq><k_ele><keb>幻司</keb></k_ele>
<r_ele><reb>げんじ</reb></r_ele><trans><name_type>masc</name_type></trans>
</entry></JMnedict>"""
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "JMnedict.xml"
            path.write_text(xml, encoding="utf-8")
            index = parse_edrdg(path, "jmnedict")
        status, reading, _ = classify_entry("幻司", [SourceIndex("formal"), index])
        self.assertEqual((status, reading), (STATUS_RESOLVED, "げんじ"))

    def test_never_composes_partial_dictionary_entries(self):
        index = SourceIndex("jmdict")
        index.add("東京", "とうきょう", "jmdict:1")
        index.add("駅", "えき", "jmdict:2")
        self.assertEqual(classify_entry("東京駅", [index])[0], STATUS_UNMATCHED)

    def test_formal_variant_is_an_exact_eligible_spelling(self):
        with tempfile.TemporaryDirectory() as temp:
            data = Path(temp) / "data"
            row = record("海月", "くらげ", needs_reading=False)
            row["meta"]["variant_writings"] = ["水母"]
            target = expected_data_path(data, "くらげ")
            target.parent.mkdir(parents=True)
            target.write_text(json.dumps([row], ensure_ascii=False), encoding="utf-8")
            index = build_formal_index(data, {"水母"})
        self.assertEqual(classify_entry("水母", [index])[:2], (STATUS_RESOLVED, "くらげ"))

    def test_unique_dictionary_sources_must_agree(self):
        jmdict, jmnedict = SourceIndex("jmdict"), SourceIndex("jmnedict")
        jmdict.add("生", "せい", "jmdict:1")
        jmnedict.add("生", "しょう", "jmnedict:1")
        self.assertEqual(classify_entry("生", [jmdict, jmnedict])[0], STATUS_CONFLICT)

    def test_embedded_single_kanji_stays_a_fragment_despite_name_match(self):
        jmnedict = SourceIndex("jmnedict")
        jmnedict.add("帰", "き", "jmnedict:1")
        item = record("帰", "帰")
        item["definitions"][0]["examples"]["literary"] = [
            {"text": "家に帰ってきた。"},
            {"text": "日帰りの旅だ。"},
        ]
        self.assertEqual(classify_entry("帰", [jmnedict], item)[0], STATUS_FRAGMENT)


class RubyTests(unittest.TestCase):
    def test_explicit_and_implicit_ruby_syntax(self):
        pairs = list(extract_aozora_ruby("｜海月《くらげ》と天河《あまのがわ》"))
        self.assertEqual(pairs, [("海月", "くらげ"), ("天河", "あまのがわ")])

    def test_distinct_files_are_required_and_conflicts_stay_pending(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "one.txt").write_text("｜海月《くらげ》、｜海月《くらげ》", encoding="utf-8")
            one = build_aozora_index(root)
            self.assertEqual(len(one.values["海月"]["くらげ"]), 1)
            self.assertEqual(classify_entry("海月", [one])[0], STATUS_SINGLE_RUBY)

            (root / "two.txt").write_text("海月《くらげ》", encoding="utf-8")
            two = build_aozora_index(root)
            self.assertEqual(classify_entry("海月", [two])[:2], (STATUS_RESOLVED, "くらげ"))

            (root / "three.txt").write_text("｜海月《みづき》", encoding="utf-8")
            conflict = build_aozora_index(root)
            self.assertEqual(classify_entry("海月", [conflict])[0], STATUS_CONFLICT)

    def test_shift_jis_aozora_text_is_parsed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "one.txt").write_bytes("｜海月《くらげ》".encode("shift_jis"))
            index = build_aozora_index(root)
        self.assertEqual(set(index.values["海月"]), {"くらげ"})

    def test_illegal_reading_and_spacing_dakuten_are_rejected(self):
        index = SourceIndex("jmdict")
        index.add("悪読", "あく1", "jmdict:3")
        self.assertEqual(classify_entry("悪読", [index])[0], STATUS_INVALID)
        self.assertEqual(classify_entry("ノワ゛リス", [index])[0], STATUS_INVALID)

    def test_unverified_single_kanji_and_stem_fragment_are_not_promoted(self):
        self.assertEqual(classify_entry("蛩", [SourceIndex("jmdict")])[0], STATUS_UNMATCHED)
        self.assertEqual(classify_entry("食べ", [SourceIndex("jmdict")])[0], STATUS_FRAGMENT)


class ApplyTests(unittest.TestCase):
    def test_apply_creates_promoted_record_with_resolution_timestamp(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            data, pending = base / "data", base / "pending"
            source = write_pending(pending, record("海月", "海月"))
            jmdict = base / "JMdict.xml"
            jmdict.write_text(
                "<JMdict><entry><ent_seq>1</ent_seq><k_ele><keb>海月</keb></k_ele>"
                "<r_ele><reb>くらげ</reb></r_ele></entry></JMdict>",
                encoding="utf-8",
            )

            report = resolve(
                data,
                pending,
                jmdict=jmdict,
                apply=True,
                report_path=base / "report.json",
                ledger_path=base / "ledger.json",
            )

            self.assertFalse(source.exists())
            target = expected_data_path(data, "くらげ")
            promoted = json.loads(target.read_text(encoding="utf-8"))[0]
            self.assertEqual(promoted["meta"]["updated_at"], report["generated_at"])

    def test_apply_merges_examples_uses_max_frequency_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            data, pending = base / "data", base / "pending"
            source = write_pending(pending, record("海月", "海月", frequency=5))

            formal = record("海月", "くらげ", frequency=3, needs_reading=False)
            formal["meta"].pop("needs_gloss")
            formal["definitions"][0]["gloss"] = "jellyfish"
            formal["definitions"][0]["examples"]["literary"][0]["text"] = "海月が漂う。"
            target = expected_data_path(data, "くらげ")
            target.parent.mkdir(parents=True)
            target.write_text(json.dumps([formal], ensure_ascii=False), encoding="utf-8")

            report_path, ledger = base / "report.json", base / "ledger.json"
            first = resolve(data, pending, apply=True, report_path=report_path, ledger_path=ledger)
            self.assertEqual(first["statistics"]["promoted"], 1)
            self.assertFalse(source.exists())
            merged = json.loads(target.read_text(encoding="utf-8"))[0]
            self.assertEqual(merged["meta"]["frequencies"]["aozora"], 5)
            self.assertEqual(merged["meta"]["updated_at"], first["generated_at"])
            self.assertEqual(len(merged["definitions"][0]["examples"]["literary"]), 2)
            self.assertNotIn("needs_reading", merged["meta"])
            self.assertNotIn("needs_gloss", merged["meta"])
            migration = json.loads(ledger.read_text(encoding="utf-8"))["migrations"][0]
            self.assertEqual(migration["new_uuid"], compute_uuid_v5("海月", "くらげ"))

            second = resolve(data, pending, apply=True, report_path=report_path, ledger_path=ledger)
            self.assertEqual(second["statistics"]["promoted"], 0)
            self.assertEqual(len(json.loads(target.read_text(encoding="utf-8"))), 1)
            self.assertEqual(len(json.loads(ledger.read_text(encoding="utf-8"))["migrations"]), 1)

    def test_apply_requires_report_and_ledger(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            with self.assertRaises(ValueError):
                resolve(base / "data", base / "pending", apply=True)


class SudachiFailureTests(unittest.TestCase):
    class Morph:
        def normalized_form(self):
            return "未知漢字"

        def reading_form(self):
            return "未知漢字"

        def part_of_speech(self):
            return ("名詞", "普通名詞", "一般", "*", "*", "*")

    class Tokenizer:
        def tokenize(self, _word, _mode):
            return [SudachiFailureTests.Morph()]

    def test_missing_sudachi_reading_is_empty_and_reason_is_recorded(self):
        with patch.object(create_entries, "_get_tokenizer", return_value=(self.Tokenizer(), object())):
            analysed = create_entries.analyze_word("未知漢字")
        self.assertEqual(analysed.reading, "")
        self.assertEqual(analysed.reading_failure_reason, "sudachi_no_valid_reading")
        new_word = create_entries.NewWord(
            analysed.canonical, analysed.reading, analysed.pos, analysed.ctype,
            analysed.reading_failure_reason,
        )
        item = create_entries.make_new_record(new_word, [], "2026-01-01T00:00:00Z")
        self.assertEqual(item["reading"]["primary"], "")
        self.assertTrue(item["meta"]["needs_reading"])
        self.assertEqual(item["meta"]["reading_failure_reason"], "sudachi_no_valid_reading")


if __name__ == "__main__":
    unittest.main()
