package store

import (
	"database/sql"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	_ "github.com/mattn/go-sqlite3"
)

// sampleRawJSON は data/ の実エントリと同形の JSON。
const sampleRawJSON = `{
  "uuid": "11111111-1111-1111-1111-111111111111",
  "entry": "雪",
  "reading": {"primary": "ゆき", "alternatives": [], "is_heteronym": false},
  "grammar": {"pos": ["名詞"], "ctype": null, "inflections": null},
  "definitions": [
    {"index": 1, "gloss": "snow", "register": "standard", "nuance": null,
     "scenarios": [], "sensory_tags": {"colors": [], "temperature": null, "sounds": [], "emotions": []},
     "collocations": [], "examples": {"standard": [], "literary": []}}
  ],
  "relations": {"homophones": [], "synonyms": [], "antonyms": [], "related": []},
  "meta": {"version": "1.0.0", "source": "test", "updated_at": "2026-01-01T00:00:00Z"}
}`

const compactSampleRawJSON = `{"uuid":"11111111-1111-1111-1111-111111111111","entry":"雪","reading":{"primary":"ゆき","alternatives":[],"is_heteronym":false},"grammar":{"pos":["名詞"],"ctype":null,"inflections":null},"definitions":[{"index":1,"gloss":"snow","register":"standard","nuance":null,"scenarios":[],"sensory_tags":{"colors":[],"temperature":null,"sounds":[],"emotions":[]},"collocations":[],"examples":{"standard":[],"literary":[]}}],"relations":{"homophones":[],"synonyms":[],"antonyms":[],"related":[]},"meta":{"version":"1.0.0","source":"test","updated_at":"2026-01-01T00:00:00Z"}}`

// buildTestDB は数件のエントリを持つ一時 DB を作り、パスを返す。
func buildTestDB(t *testing.T) string {
	return buildTestDBWithRawJSON(t, sampleRawJSON)
}

func buildTestDBWithRawJSON(t *testing.T, rawJSON string) string {
	t.Helper()
	dir := t.TempDir()
	path := filepath.Join(dir, "test.db")

	db, err := sql.Open("sqlite3", path)
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	defer db.Close()

	schema := `
	CREATE TABLE entries (
		uuid TEXT PRIMARY KEY, entry TEXT NOT NULL, reading_primary TEXT,
		reading_alternatives TEXT, is_heteronym INTEGER DEFAULT 0,
		pos TEXT, ctype TEXT, inflections TEXT, relations TEXT, meta TEXT, raw_json TEXT NOT NULL
	);
	CREATE TABLE definitions (
		id INTEGER PRIMARY KEY AUTOINCREMENT, entry_uuid TEXT NOT NULL,
		def_index INTEGER, gloss TEXT, register TEXT, nuance TEXT,
		scenarios TEXT, sensory_tags TEXT, collocations TEXT, examples TEXT
	);
	CREATE TABLE _metadata (key TEXT PRIMARY KEY, value TEXT);
	CREATE VIRTUAL TABLE fts_entries USING fts5(uuid UNINDEXED, entry, reading_primary, tokenize='unicode61');
	CREATE VIRTUAL TABLE fts_definitions USING fts5(entry_uuid UNINDEXED, gloss, tokenize='unicode61');`
	if _, err := db.Exec(schema); err != nil {
		t.Fatalf("schema: %v", err)
	}

	if _, err := db.Exec(
		`INSERT INTO entries (uuid, entry, reading_primary, pos, raw_json) VALUES (?, ?, ?, ?, ?)`,
		"11111111-1111-1111-1111-111111111111", "雪", "ゆき", `["名詞"]`, rawJSON,
	); err != nil {
		t.Fatalf("insert entry: %v", err)
	}
	db.Exec(`INSERT INTO definitions (entry_uuid, def_index, gloss) VALUES (?, ?, ?)`,
		"11111111-1111-1111-1111-111111111111", 1, "snow")
	db.Exec(`INSERT INTO fts_entries (uuid, entry, reading_primary) VALUES (?, ?, ?)`,
		"11111111-1111-1111-1111-111111111111", "雪", "ゆき")
	db.Exec(`INSERT INTO fts_definitions (entry_uuid, gloss) VALUES (?, ?)`,
		"11111111-1111-1111-1111-111111111111", "snow")
	db.Exec(`INSERT INTO _metadata (key, value) VALUES ('version', 'test-1'), ('entry_count', '1'), ('schema_version', '3')`)

	return path
}

func TestLegacyAndCompactJSONCompatibility(t *testing.T) {
	legacy, err := Open(buildTestDBWithRawJSON(t, sampleRawJSON))
	if err != nil {
		t.Fatalf("open legacy store: %v", err)
	}
	defer legacy.Close()
	compact, err := Open(buildTestDBWithRawJSON(t, compactSampleRawJSON))
	if err != nil {
		t.Fatalf("open compact store: %v", err)
	}
	defer compact.Close()

	const uuid = "11111111-1111-1111-1111-111111111111"
	legacyEntry, err := legacy.GetByUUID(uuid)
	if err != nil {
		t.Fatalf("legacy GetByUUID: %v", err)
	}
	compactEntry, err := compact.GetByUUID(uuid)
	if err != nil {
		t.Fatalf("compact GetByUUID: %v", err)
	}
	if !reflect.DeepEqual(legacyEntry, compactEntry) {
		t.Errorf("decoded api.Entry differs:\nlegacy=%+v\ncompact=%+v", legacyEntry, compactEntry)
	}

	legacyEntries, err := legacy.SearchEntries("雪", 50)
	if err != nil {
		t.Fatalf("legacy SearchEntries: %v", err)
	}
	compactEntries, err := compact.SearchEntries("雪", 50)
	if err != nil {
		t.Fatalf("compact SearchEntries: %v", err)
	}
	if !reflect.DeepEqual(legacyEntries, compactEntries) {
		t.Errorf("entry FTS snippet/highlight differs: %v != %v", legacyEntries, compactEntries)
	}

	legacyDefinitions, err := legacy.SearchDefinitions("snow", 50)
	if err != nil {
		t.Fatalf("legacy SearchDefinitions: %v", err)
	}
	compactDefinitions, err := compact.SearchDefinitions("snow", 50)
	if err != nil {
		t.Fatalf("compact SearchDefinitions: %v", err)
	}
	if !reflect.DeepEqual(legacyDefinitions, compactDefinitions) {
		t.Errorf("definition FTS snippet/highlight differs: %v != %v", legacyDefinitions, compactDefinitions)
	}
}

func openTestStore(t *testing.T) *Store {
	t.Helper()
	s, err := Open(buildTestDB(t))
	if err != nil {
		t.Fatalf("Open: %v", err)
	}
	t.Cleanup(func() { s.Close() })
	return s
}

func TestGetByUUID(t *testing.T) {
	s := openTestStore(t)
	e, err := s.GetByUUID("11111111-1111-1111-1111-111111111111")
	if err != nil {
		t.Fatalf("GetByUUID: %v", err)
	}
	if e.Entry != "雪" {
		t.Errorf("entry = %q, want 雪", e.Entry)
	}
	if e.Reading == nil || e.Reading.Primary == nil || *e.Reading.Primary != "ゆき" {
		t.Errorf("reading not parsed from raw_json: %+v", e.Reading)
	}
	if e.Definitions == nil || len(*e.Definitions) != 1 {
		t.Fatalf("definitions = %v, want 1", e.Definitions)
	}
}

func TestGetByUUIDNotFound(t *testing.T) {
	s := openTestStore(t)
	if _, err := s.GetByUUID("does-not-exist"); err != ErrNotFound {
		t.Errorf("err = %v, want ErrNotFound", err)
	}
}

func TestLookupByEntry(t *testing.T) {
	s := openTestStore(t)
	entries, err := s.LookupByEntry("雪")
	if err != nil {
		t.Fatalf("LookupByEntry: %v", err)
	}
	if len(entries) != 1 {
		t.Fatalf("got %d entries, want 1", len(entries))
	}
}

func TestLookupByReading(t *testing.T) {
	s := openTestStore(t)
	entries, err := s.LookupByReading("ゆき")
	if err != nil {
		t.Fatalf("LookupByReading: %v", err)
	}
	if len(entries) != 1 {
		t.Fatalf("got %d entries, want 1", len(entries))
	}
}

func TestSearchEntries(t *testing.T) {
	s := openTestStore(t)
	results, err := s.SearchEntries("雪", 50)
	if err != nil {
		t.Fatalf("SearchEntries: %v", err)
	}
	if len(results) != 1 {
		t.Fatalf("got %d results, want 1", len(results))
	}
	if results[0].Pos == nil || len(*results[0].Pos) != 1 || (*results[0].Pos)[0] != "名詞" {
		t.Errorf("pos not parsed: %+v", results[0].Pos)
	}
}

func TestSearchDefinitions(t *testing.T) {
	s := openTestStore(t)
	results, err := s.SearchDefinitions("snow", 50)
	if err != nil {
		t.Fatalf("SearchDefinitions: %v", err)
	}
	if len(results) != 1 {
		t.Fatalf("got %d results, want 1", len(results))
	}
	if results[0].Gloss == nil || *results[0].Gloss != "snow" {
		t.Errorf("gloss = %v, want snow", results[0].Gloss)
	}
}

func TestSearchHandlesSpecialChars(t *testing.T) {
	s := openTestStore(t)
	// FTS 構文記号を含む入力でもエラーにならず（サニタイズで）安全に処理される。
	for _, q := range []string{`"`, `snow*`, `-foo`, `a OR b`, `   `} {
		if _, err := s.SearchEntries(q, 50); err != nil {
			t.Errorf("SearchEntries(%q) errored: %v", q, err)
		}
	}
}

func TestRandom(t *testing.T) {
	s := openTestStore(t)
	entries, err := s.Random(5)
	if err != nil {
		t.Fatalf("Random: %v", err)
	}
	if len(entries) != 1 { // テスト DB には1件のみ
		t.Fatalf("got %d entries, want 1", len(entries))
	}
}

func TestRandomReturnsDistinctEntries(t *testing.T) {
	path := buildTestDB(t)
	db, err := sql.Open("sqlite3", path)
	if err != nil {
		t.Fatalf("open writable test DB: %v", err)
	}
	secondRaw := strings.Replace(sampleRawJSON, "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222", 1)
	if _, err := db.Exec(
		`INSERT INTO entries (uuid, entry, reading_primary, pos, raw_json) VALUES (?, ?, ?, ?, ?)`,
		"22222222-2222-2222-2222-222222222222", "雨", "あめ", `["名詞"]`, secondRaw,
	); err != nil {
		t.Fatalf("insert second entry: %v", err)
	}
	if err := db.Close(); err != nil {
		t.Fatalf("close writable test DB: %v", err)
	}
	s, err := Open(path)
	if err != nil {
		t.Fatalf("open store: %v", err)
	}
	t.Cleanup(func() { s.Close() })

	entries, err := s.Random(2)
	if err != nil {
		t.Fatalf("Random(2): %v", err)
	}
	if len(entries) != 2 {
		t.Fatalf("got %d entries, want 2", len(entries))
	}
	if entries[0].Uuid == entries[1].Uuid {
		t.Errorf("random entries should be distinct: %+v", entries)
	}
}

func TestMetadata(t *testing.T) {
	s := openTestStore(t)
	m, err := s.Metadata()
	if err != nil {
		t.Fatalf("Metadata: %v", err)
	}
	if m.Version == nil || *m.Version != "test-1" {
		t.Errorf("version = %v, want test-1", m.Version)
	}
	if m.EntryCount == nil || *m.EntryCount != "1" {
		t.Errorf("entry_count = %v, want 1", m.EntryCount)
	}
	if m.SchemaVersion == nil || *m.SchemaVersion != "3" {
		t.Errorf("schema_version = %v, want 3", m.SchemaVersion)
	}
}

func TestSanitizeFTS(t *testing.T) {
	cases := map[string]string{
		"雪":      `"雪"`,
		"a b":    `"a" "b"`,
		`he"llo`: `"he""llo"`,
		"   ":    `""`,
		"snow*":  `"snow*"`,
	}
	for in, want := range cases {
		if got := sanitizeFTS(in); got != want {
			t.Errorf("sanitizeFTS(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestCountEntries(t *testing.T) {
	s := openTestStore(t)
	n, err := s.CountEntries()
	if err != nil {
		t.Fatalf("CountEntries: %v", err)
	}
	if n != 1 {
		t.Errorf("count = %d, want 1", n)
	}
}

func TestAllUUIDs(t *testing.T) {
	s := openTestStore(t)
	uuids, err := s.AllUUIDs()
	if err != nil {
		t.Fatalf("AllUUIDs: %v", err)
	}
	if len(uuids) != 1 || uuids[0] != "11111111-1111-1111-1111-111111111111" {
		t.Errorf("uuids = %v", uuids)
	}
}

func TestSitemapByUUIDs(t *testing.T) {
	s := openTestStore(t)
	const id = "11111111-1111-1111-1111-111111111111"
	rows, err := s.SitemapByUUIDs([]string{id, "missing"})
	if err != nil {
		t.Fatalf("SitemapByUUIDs: %v", err)
	}
	row, ok := rows[id]
	if !ok {
		t.Fatalf("uuid %s not found in result", id)
	}
	if row.Entry != "雪" {
		t.Errorf("entry = %q, want 雪", row.Entry)
	}
	if _, ok := rows["missing"]; ok {
		t.Error("missing uuid should not be present")
	}
	// 空入力は空 map を返す。
	empty, err := s.SitemapByUUIDs(nil)
	if err != nil || len(empty) != 0 {
		t.Errorf("empty input: %v, %v", empty, err)
	}
}

func TestEntryFreqSums(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "freq.db")
	db, err := sql.Open("sqlite3", path)
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	db.Exec(`CREATE TABLE entries (uuid TEXT PRIMARY KEY, entry TEXT NOT NULL, reading_primary TEXT,
		reading_alternatives TEXT, is_heteronym INTEGER DEFAULT 0, pos TEXT, ctype TEXT,
		inflections TEXT, relations TEXT, meta TEXT, raw_json TEXT NOT NULL)`)
	// a: 複数出典の合計 = 1200 + 34 = 1234。b: 頻度なし。c: meta が NULL。
	// いずれも sitemap 対象品詞（名詞）にしてフィルタを通す。
	db.Exec(`INSERT INTO entries (uuid, entry, pos, meta, raw_json) VALUES
		('a','雪','["名詞"]','{"frequencies":{"aozora":1200,"jmdict":34}}','{}'),
		('b','月','["名詞"]','{"version":"1.0.0"}','{}'),
		('c','花','["名詞"]',NULL,'{}')`)
	db.Close()

	s, err := Open(path)
	if err != nil {
		t.Fatalf("Open: %v", err)
	}
	defer s.Close()

	rows, err := s.EntryFreqSums()
	if err != nil {
		t.Fatalf("EntryFreqSums: %v", err)
	}
	got := make(map[string]int64, len(rows))
	for _, r := range rows {
		got[r.UUID] = r.FreqSum
	}
	if got["a"] != 1234 {
		t.Errorf("freq[a] = %d, want 1234", got["a"])
	}
	if got["b"] != 0 {
		t.Errorf("freq[b] = %d, want 0 (頻度なし)", got["b"])
	}
	if got["c"] != 0 {
		t.Errorf("freq[c] = %d, want 0 (meta NULL)", got["c"])
	}
}

func TestSitemapPOSFilter(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "pos.db")
	db, err := sql.Open("sqlite3", path)
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	db.Exec(`CREATE TABLE entries (uuid TEXT PRIMARY KEY, entry TEXT NOT NULL, reading_primary TEXT,
		reading_alternatives TEXT, is_heteronym INTEGER DEFAULT 0, pos TEXT, ctype TEXT,
		inflections TEXT, relations TEXT, meta TEXT, raw_json TEXT NOT NULL)`)
	// 対象（名詞・動詞・形容詞系列）と非対象（表現・副詞・pos NULL）を混在させる。
	db.Exec(`INSERT INTO entries (uuid, entry, pos, meta, raw_json) VALUES
		('n','名詞語','["名詞"]','{}','{}'),
		('v','動詞語','["動詞-サ変"]','{}','{}'),
		('adj','形容詞語','["形容詞"]','{}','{}'),
		('adjstem','形容詞語幹','["形容詞-語幹"]','{}','{}'),
		('adjna','形容動詞語','["形容動詞"]','{}','{}'),
		('nadj','の形容詞','["名詞-の形容詞","名詞"]','{}','{}'),
		('expr','表現語','["表現"]','{}','{}'),
		('adv','副詞語','["副詞"]','{}','{}'),
		('nullpos','品詞なし',NULL,'{}','{}')`)
	db.Close()

	s, err := Open(path)
	if err != nil {
		t.Fatalf("Open: %v", err)
	}
	defer s.Close()

	wantQualified := map[string]bool{"n": true, "v": true, "adj": true, "adjstem": true, "adjna": true, "nadj": true}

	n, err := s.CountSitemapEntries()
	if err != nil {
		t.Fatalf("CountSitemapEntries: %v", err)
	}
	if n != len(wantQualified) {
		t.Errorf("CountSitemapEntries = %d, want %d", n, len(wantQualified))
	}

	freqs, err := s.EntryFreqSums()
	if err != nil {
		t.Fatalf("EntryFreqSums: %v", err)
	}
	if len(freqs) != len(wantQualified) {
		t.Errorf("EntryFreqSums returned %d rows, want %d", len(freqs), len(wantQualified))
	}
	for _, f := range freqs {
		if !wantQualified[f.UUID] {
			t.Errorf("EntryFreqSums included non-target uuid %q", f.UUID)
		}
	}

	rows, err := s.SitemapByFreq(50, 0)
	if err != nil {
		t.Fatalf("SitemapByFreq: %v", err)
	}
	if len(rows) != len(wantQualified) {
		t.Errorf("SitemapByFreq returned %d rows, want %d", len(rows), len(wantQualified))
	}
	for _, r := range rows {
		if !wantQualified[r.UUID] {
			t.Errorf("SitemapByFreq included non-target uuid %q (%s)", r.UUID, r.Entry)
		}
	}
}

func TestSitemapByFreq(t *testing.T) {
	s := openTestStore(t)
	rows, err := s.SitemapByFreq(10, 0)
	if err != nil {
		t.Fatalf("SitemapByFreq: %v", err)
	}
	if len(rows) != 1 {
		t.Fatalf("got %d rows, want 1", len(rows))
	}
	if rows[0].Entry != "雪" {
		t.Errorf("entry = %q, want 雪", rows[0].Entry)
	}
	if rows[0].Heat != nil {
		t.Errorf("heat should be nil in fallback, got %v", rows[0].Heat)
	}
	// offset で範囲外 → 0 件。
	rows2, _ := s.SitemapByFreq(10, 5)
	if len(rows2) != 0 {
		t.Errorf("offset beyond range should be empty, got %d", len(rows2))
	}
}
