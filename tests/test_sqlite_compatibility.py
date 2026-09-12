import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = ROOT / "script"
sys.path.insert(0, str(SCRIPT_DIR))

import check_sqlite_compatibility as compatibility  # noqa: E402
import json_to_sqlite as builder  # noqa: E402


FIXED_BUILD_DATE = "2026-09-11T00:00:00Z"


def sample_entries():
    return [
        {
            "uuid": "00000000-0000-0000-0000-000000000001",
            "entry": "雪",
            "reading": {
                "primary": "ゆき",
                "alternatives": ["ユキ", "ゆき\u0301"],
                "is_heteronym": True,
            },
            "grammar": {
                "pos": ["名詞", "字詞"],
                "ctype": None,
                "ctype_source": None,
                "ctype_confidence": None,
                "inflections": {"ordered": [1, 1.0, True, False, None]},
            },
            "definitions": [
                {
                    "index": 1,
                    "gloss": "snow 雪 ❄️",
                    "register": "standard",
                    "nuance": "",
                    "scenarios": ["冬", "山"],
                    "sensory_tags": {
                        "cold": True,
                        "temperature": -0.0,
                        "depth": 2,
                    },
                    "collocations": ["深い雪", "雪々"],
                    "examples": {
                        "standard": [{"text": "雪が降る。", "citation": None}],
                        "literary": [],
                    },
                },
                {
                    "index": 2,
                    "gloss": "",
                    "register": None,
                    "nuance": None,
                    "scenarios": [],
                    "sensory_tags": {},
                    "collocations": [],
                    "examples": {},
                },
            ],
            "relations": {
                "homophones": [],
                "synonyms": ["霙"],
                "antonyms": [],
                "related": ["雪󠄀"],
            },
            "meta": {
                "version": "1.0.0",
                "source": "synthetic",
                "updated_at": FIXED_BUILD_DATE,
                "variant_writings": ["雪󠄀", "ゆき雪"],
                "freq_rank": 1,
            },
        },
        {
            "uuid": "00000000-0000-0000-0000-000000000002",
            "entry": "ＡＢＣ",
            "reading": {
                "primary": None,
                "alternatives": [],
                "is_heteronym": False,
            },
            "grammar": {"pos": [], "inflections": None},
            "definitions": [
                {
                    "index": 1,
                    "gloss": None,
                    "register": None,
                    "nuance": None,
                    "scenarios": [],
                    "sensory_tags": {},
                    "collocations": [],
                    "examples": {},
                }
            ],
            "relations": {},
            "meta": {"needs_gloss": True},
        },
    ]


def build_database(path: Path, *, compact: bool, optimize: bool = False) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA page_size=4096")
    conn.execute("PRAGMA foreign_keys=ON")
    builder.create_schema(conn)
    for item in sample_entries():
        builder.insert_entry(conn, item, compact_json=compact)
    builder.create_metadata(conn, 2, build_date=FIXED_BUILD_DATE)
    builder.create_fts(conn, optimize=optimize)
    conn.commit()
    conn.execute("ANALYZE")
    conn.commit()
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("VACUUM")
    conn.close()


class SQLiteCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.legacy = root / "legacy.db"
        self.compact = root / "compact.db"
        build_database(self.legacy, compact=False)
        build_database(self.compact, compact=True, optimize=True)

    def tearDown(self):
        self.temp.cleanup()

    def test_compact_json_preserves_schema_v3_contract(self):
        report = compatibility.compare_databases(self.legacy, self.compact)
        self.assertTrue(report["compatible"])
        self.assertEqual(report["schema_version"], 3)
        self.assertEqual(report["rows"]["entries"], 2)
        self.assertEqual(report["rows"]["definitions"], 3)
        legacy_conn = sqlite3.connect(str(self.legacy))
        compact_conn = sqlite3.connect(str(self.compact))
        self.addCleanup(legacy_conn.close)
        self.addCleanup(compact_conn.close)
        legacy = legacy_conn.execute(
            "SELECT sum(length(raw_json)) FROM entries"
        ).fetchone()[0]
        compact = compact_conn.execute(
            "SELECT sum(length(raw_json)) FROM entries"
        ).fetchone()[0]
        self.assertLess(compact, legacy)

    def test_validate_only_accepts_release_contract(self):
        conn = compatibility._connect(self.compact)
        self.addCleanup(conn.close)
        compatibility.validate_database(conn, path="compact")

    def test_type_aware_json_detects_bool_integer_change(self):
        conn = sqlite3.connect(str(self.compact))
        raw = conn.execute(
            "SELECT raw_json FROM entries WHERE entry='雪'"
        ).fetchone()[0]
        item = json.loads(raw)
        item["grammar"]["inflections"]["ordered"][2] = 1
        conn.execute(
            "UPDATE entries SET raw_json=? WHERE entry='雪'",
            (builder.serialize_json(item),),
        )
        conn.commit()
        conn.close()
        with self.assertRaisesRegex(compatibility.CompatibilityError, "raw_json"):
            compatibility.compare_databases(self.legacy, self.compact)

    def test_sql_null_and_json_null_are_not_equivalent(self):
        conn = sqlite3.connect(str(self.compact))
        conn.execute("UPDATE entries SET inflections='null' WHERE entry='ＡＢＣ'")
        conn.commit()
        conn.close()
        with self.assertRaisesRegex(compatibility.CompatibilityError, "SQLite type"):
            compatibility.compare_databases(self.legacy, self.compact)

    def test_rowid_changes_are_rejected(self):
        conn = sqlite3.connect(str(self.compact))
        conn.execute("UPDATE variant_lookup SET rowid=100 WHERE rowid=1")
        conn.commit()
        conn.close()
        with self.assertRaisesRegex(compatibility.CompatibilityError, "rowid differs"):
            compatibility.compare_databases(self.legacy, self.compact)

    def test_schema_changes_are_rejected(self):
        conn = sqlite3.connect(str(self.compact))
        conn.execute("CREATE INDEX unexpected_index ON definitions(gloss)")
        conn.commit()
        conn.close()
        with self.assertRaisesRegex(compatibility.CompatibilityError, "named indexes"):
            compatibility.compare_databases(self.legacy, self.compact)


class CompactSerializerTests(unittest.TestCase):
    def test_compact_serializer_preserves_order_and_unicode(self):
        value = {"雪": [1, True, None], "a": {"z": "異體字󠄀"}}
        compact = builder.serialize_json(value)
        legacy = builder.serialize_json(value, compact=False)
        self.assertEqual(compact, '{"雪":[1,true,null],"a":{"z":"異體字󠄀"}}')
        self.assertEqual(json.loads(compact), json.loads(legacy))
        self.assertLess(len(compact), len(legacy))


if __name__ == "__main__":
    unittest.main()
