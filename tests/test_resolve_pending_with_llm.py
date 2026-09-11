import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "script"))

from resolve_pending_with_llm import (  # noqa: E402
    Decision,
    clean_kanjidic_reading,
    consensus,
    exact_dictionary_reading,
    formal_variant_match,
    parse_kotobank_readings,
    parse_weblio_readings,
    parse_response,
    reading_candidates,
)


class CandidateTests(unittest.TestCase):
    def test_dotted_kun_reading_produces_full_and_stem_forms(self):
        self.assertEqual(clean_kanjidic_reading("おご.る"), {"おご", "おごる"})

    def test_compound_candidates_are_machine_bounded(self):
        index = {"倨": ("きょ", "こ"), "然": ("ぜん", "ねん")}
        candidates = reading_candidates("倨然", index)
        self.assertIn("きょぜん", candidates)
        self.assertNotIn("ごぜん", candidates)

    def test_unbounded_or_unknown_candidates_are_not_guessed(self):
        self.assertEqual(reading_candidates("未知", {"未": ("み",)}), [])

    def test_newly_confirmed_old_forms_are_normalized(self):
        from create_entries import _apply_kyuji
        self.assertEqual(_apply_kyuji("凖備と仮裝と劔術"), "準備と仮装と剣術")


class ResponseTests(unittest.TestCase):
    def test_exact_dictionary_display_reading_matches_small_kana_candidate(self):
        page = '<h1>圜丘<span>（読み）えんきゆう（ゑんきう）</span></h1>'
        self.assertEqual(parse_kotobank_readings(page), ["えんきゅう"])
        self.assertEqual(parse_kotobank_readings(page, "別見出し"), [])
        self.assertEqual(parse_kotobank_readings(page, "圜丘"), ["えんきゅう"])
        self.assertEqual(exact_dictionary_reading(
            {"status": "matched", "readings": ["えんきゅう"]}, ["かんきゅう", "えんきゅう"]
        ), "えんきゅう")

    def test_exact_dictionary_jukujikun_overrides_character_composition(self):
        self.assertEqual(exact_dictionary_reading(
            {"status": "matched", "readings": ["うじ"]}, ["とどう", "うさぎみち"]
        ), "うじ")

    def test_weblio_requires_exact_heading(self):
        page = ('<h2 class=midashigo title="俚歌">りか</h2>'
                '<div><!--AVOID-->読み方：りか<!--/AVOID--></div>')
        self.assertEqual(parse_weblio_readings(page, "俚歌"), ["りか"])
        self.assertEqual(parse_weblio_readings(page, "俚"), [])

    def test_parser_rejects_reading_outside_candidates(self):
        with self.assertRaises(ValueError):
            parse_response("0:P1", {0: ["きょぜん"]})

    def test_parser_accepts_compact_complete_tsv(self):
        result = parse_response("0:P0\n1:D\n2:R", {
            0: ["きょぜん"], 1: ["れんしょう"], 2: ["はるか"],
        })
        self.assertEqual(result[0], Decision("promote", "きょぜん"))
        self.assertEqual(result[1], Decision("discard"))
        self.assertEqual(result[2], Decision("review"))

    def test_consensus_requires_exact_agreement(self):
        self.assertEqual(consensus(Decision("promote", "きょぜん"), Decision("promote", "きょぜん")),
                         Decision("promote", "きょぜん"))
        self.assertEqual(consensus(Decision("promote", "きょぜん"), Decision("discard")),
                         Decision("review"))


if __name__ == "__main__":
    unittest.main()
