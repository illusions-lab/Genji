package server

import (
	"context"
	"database/sql"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	_ "github.com/mattn/go-sqlite3"

	"github.com/Iktahana/Genji/api/internal/api"
	"github.com/Iktahana/Genji/api/internal/cache"
	"github.com/Iktahana/Genji/api/internal/heat"
	"github.com/Iktahana/Genji/api/internal/store"
)

// memCache はテスト用の計測可能なインメモリキャッシュ。
type memCache struct {
	mu       sync.Mutex
	data     map[string][]byte
	getCalls int
	setCalls int
}

func newMemCache() *memCache { return &memCache{data: map[string][]byte{}} }

func (m *memCache) Get(_ context.Context, key string) ([]byte, bool) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.getCalls++
	v, ok := m.data[key]
	return v, ok
}

func (m *memCache) Set(_ context.Context, key string, val []byte, _ time.Duration) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.setCalls++
	cp := make([]byte, len(val))
	copy(cp, val)
	m.data[key] = cp
}

func (m *memCache) Enabled() bool { return true }
func (m *memCache) Close() error  { return nil }
func (m *memCache) keys() []string {
	m.mu.Lock()
	defer m.mu.Unlock()
	ks := make([]string, 0, len(m.data))
	for k := range m.data {
		ks = append(ks, k)
	}
	return ks
}

const sampleRawJSON = `{
  "uuid": "u1", "entry": "雪",
  "reading": {"primary": "ゆき", "alternatives": [], "is_heteronym": false},
  "grammar": {"pos": ["名詞"]},
  "definitions": [{"index": 1, "gloss": "snow", "register": "standard"}],
  "relations": {"homophones": [], "synonyms": [], "antonyms": [], "related": []},
  "meta": {"version": "1.0.0", "source": "test", "updated_at": "2026-01-01T00:00:00Z"}
}`

const compactSampleRawJSON = `{"uuid":"u1","entry":"雪","reading":{"primary":"ゆき","alternatives":[],"is_heteronym":false},"grammar":{"pos":["名詞"]},"definitions":[{"index":1,"gloss":"snow","register":"standard"}],"relations":{"homophones":[],"synonyms":[],"antonyms":[],"related":[]},"meta":{"version":"1.0.0","source":"test","updated_at":"2026-01-01T00:00:00Z"}}`

func buildTestDB(t *testing.T) string {
	return buildTestDBWithRawJSON(t, sampleRawJSON)
}

func buildTestDBWithRawJSON(t *testing.T, rawJSON string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "test.db")
	db, err := sql.Open("sqlite3", path)
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	defer db.Close()

	schema := `
	CREATE TABLE entries (uuid TEXT PRIMARY KEY, entry TEXT NOT NULL, reading_primary TEXT,
		reading_alternatives TEXT, is_heteronym INTEGER DEFAULT 0, pos TEXT, ctype TEXT,
		inflections TEXT, relations TEXT, meta TEXT, raw_json TEXT NOT NULL);
	CREATE TABLE definitions (id INTEGER PRIMARY KEY AUTOINCREMENT, entry_uuid TEXT NOT NULL,
		def_index INTEGER, gloss TEXT, register TEXT, nuance TEXT, scenarios TEXT,
		sensory_tags TEXT, collocations TEXT, examples TEXT);
	CREATE TABLE _metadata (key TEXT PRIMARY KEY, value TEXT);
	CREATE VIRTUAL TABLE fts_entries USING fts5(uuid UNINDEXED, entry, reading_primary, tokenize='unicode61');
	CREATE VIRTUAL TABLE fts_definitions USING fts5(entry_uuid UNINDEXED, gloss, tokenize='unicode61');`
	if _, err := db.Exec(schema); err != nil {
		t.Fatalf("schema: %v", err)
	}
	if _, err := db.Exec(`INSERT INTO entries (uuid, entry, reading_primary, pos, raw_json) VALUES (?,?,?,?,?)`,
		"u1", "雪", "ゆき", `["名詞"]`, rawJSON); err != nil {
		t.Fatalf("insert: %v", err)
	}
	db.Exec(`INSERT INTO definitions (entry_uuid, def_index, gloss) VALUES ('u1',1,'snow')`)
	db.Exec(`INSERT INTO fts_entries (uuid, entry, reading_primary) VALUES ('u1','雪','ゆき')`)
	db.Exec(`INSERT INTO fts_definitions (entry_uuid, gloss) VALUES ('u1','snow')`)
	db.Exec(`INSERT INTO _metadata (key,value) VALUES ('version','e2e'),('entry_count','1')`)
	return path
}

// newTestServer はテスト用の gin router を返す（heat 無効）。
func newTestServer(t *testing.T, c cache.Cache) *gin.Engine {
	return newTestServerWithHeat(t, c, heat.Noop{})
}

func newTestServerWithRawJSON(t *testing.T, rawJSON string) *gin.Engine {
	t.Helper()
	gin.SetMode(gin.TestMode)
	st, err := store.Open(buildTestDBWithRawJSON(t, rawJSON))
	if err != nil {
		t.Fatalf("store.Open: %v", err)
	}
	t.Cleanup(func() { st.Close() })
	h := NewHandler(st, cache.NoopCache{}, heat.Noop{}, time.Minute)
	r := gin.New()
	api.RegisterHandlers(r, api.NewStrictHandler(h, nil))
	return r
}

// newTestServerWithHeat は heat サービスを指定してテスト用 router を返す。
func newTestServerWithHeat(t *testing.T, c cache.Cache, hsvc heat.Service) *gin.Engine {
	t.Helper()
	gin.SetMode(gin.TestMode)

	st, err := store.Open(buildTestDB(t))
	if err != nil {
		t.Fatalf("store.Open: %v", err)
	}
	t.Cleanup(func() { st.Close() })

	h := NewHandler(st, c, hsvc, time.Minute)
	r := gin.New()
	api.RegisterDocs(r)
	api.RegisterHandlers(r, api.NewStrictHandler(h, nil))
	return r
}

func doGet(t *testing.T, r http.Handler, path string) *httptest.ResponseRecorder {
	t.Helper()
	req := httptest.NewRequest(http.MethodGet, path, nil)
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)
	return w
}

func TestHealthz(t *testing.T) {
	r := newTestServer(t, cache.NoopCache{})
	w := doGet(t, r, "/healthz")
	if w.Code != 200 {
		t.Fatalf("status = %d, want 200", w.Code)
	}
	var h api.Health
	if err := json.Unmarshal(w.Body.Bytes(), &h); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if h.Status != "ok" {
		t.Errorf("status = %q, want ok", h.Status)
	}
	if h.Cache == nil || *h.Cache != "disabled" {
		t.Errorf("cache = %v, want disabled", h.Cache)
	}
}

func TestHealthzCacheEnabled(t *testing.T) {
	r := newTestServer(t, newMemCache())
	w := doGet(t, r, "/healthz")
	var h api.Health
	json.Unmarshal(w.Body.Bytes(), &h)
	if h.Cache == nil || *h.Cache != "enabled" {
		t.Errorf("cache = %v, want enabled", h.Cache)
	}
}

func TestLookupByEntryEndpoint(t *testing.T) {
	r := newTestServer(t, cache.NoopCache{})
	w := doGet(t, r, "/v1/lookup/entry?word=雪")
	if w.Code != 200 {
		t.Fatalf("status = %d, body=%s", w.Code, w.Body.String())
	}
	var list api.EntryList
	if err := json.Unmarshal(w.Body.Bytes(), &list); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if list.Count != 1 || len(list.Entries) != 1 {
		t.Fatalf("count = %d, want 1", list.Count)
	}
	if list.Entries[0].Entry != "雪" {
		t.Errorf("entry = %q, want 雪", list.Entries[0].Entry)
	}
}

func TestLookupByReadingEndpoint(t *testing.T) {
	r := newTestServer(t, cache.NoopCache{})
	w := doGet(t, r, "/v1/lookup/reading?reading=ゆき")
	if w.Code != 200 {
		t.Fatalf("status = %d", w.Code)
	}
	var list api.EntryList
	json.Unmarshal(w.Body.Bytes(), &list)
	if list.Count != 1 {
		t.Errorf("count = %d, want 1", list.Count)
	}
}

func TestSearchEntriesEndpoint(t *testing.T) {
	r := newTestServer(t, cache.NoopCache{})
	w := doGet(t, r, "/v1/search/entries?q=雪")
	if w.Code != 200 {
		t.Fatalf("status = %d", w.Code)
	}
	var list api.SearchResultList
	json.Unmarshal(w.Body.Bytes(), &list)
	if list.Count != 1 || list.Results[0].MatchHighlight == nil {
		t.Errorf("unexpected results: %+v", list)
	}
}

func TestSearchDefinitionsEndpoint(t *testing.T) {
	r := newTestServer(t, cache.NoopCache{})
	w := doGet(t, r, "/v1/search/definitions?q=snow")
	if w.Code != 200 {
		t.Fatalf("status = %d", w.Code)
	}
	var list api.DefinitionSearchResultList
	json.Unmarshal(w.Body.Bytes(), &list)
	if list.Count != 1 || list.Results[0].Gloss == nil || *list.Results[0].Gloss != "snow" {
		t.Errorf("unexpected results: %+v", list)
	}
}

func TestGetEntryByUUIDEndpoint(t *testing.T) {
	r := newTestServer(t, cache.NoopCache{})
	w := doGet(t, r, "/v1/entries/u1")
	if w.Code != 200 {
		t.Fatalf("status = %d", w.Code)
	}
	var e api.Entry
	json.Unmarshal(w.Body.Bytes(), &e)
	if e.Uuid != "u1" {
		t.Errorf("uuid = %q, want u1", e.Uuid)
	}
}

func TestLegacyAndCompactJSONEndpointResponsesMatch(t *testing.T) {
	legacy := newTestServerWithRawJSON(t, sampleRawJSON)
	compact := newTestServerWithRawJSON(t, compactSampleRawJSON)
	for _, path := range []string{
		"/v1/entries/u1",
		"/v1/lookup/entry?word=雪",
		"/v1/lookup/reading?reading=ゆき",
		"/v1/search/entries?q=雪",
		"/v1/search/definitions?q=snow",
	} {
		before := doGet(t, legacy, path)
		after := doGet(t, compact, path)
		if before.Code != after.Code || before.Body.String() != after.Body.String() {
			t.Errorf("%s response differs:\nlegacy=%d %s\ncompact=%d %s",
				path, before.Code, before.Body.String(), after.Code, after.Body.String())
		}
	}
}

func TestGetEntryByUUIDNotFound(t *testing.T) {
	r := newTestServer(t, cache.NoopCache{})
	w := doGet(t, r, "/v1/entries/missing")
	if w.Code != 404 {
		t.Fatalf("status = %d, want 404", w.Code)
	}
	var e api.Error
	json.Unmarshal(w.Body.Bytes(), &e)
	if e.Code != 404 {
		t.Errorf("error code = %d, want 404", e.Code)
	}
}

func TestRandomEndpoint(t *testing.T) {
	r := newTestServer(t, cache.NoopCache{})
	w := doGet(t, r, "/v1/random?count=3")
	if w.Code != 200 {
		t.Fatalf("status = %d", w.Code)
	}
	var list api.EntryList
	json.Unmarshal(w.Body.Bytes(), &list)
	if list.Count != 1 { // テスト DB には1件のみ
		t.Errorf("count = %d, want 1", list.Count)
	}
}

func TestMetadataEndpoint(t *testing.T) {
	r := newTestServer(t, cache.NoopCache{})
	w := doGet(t, r, "/v1/metadata")
	if w.Code != 200 {
		t.Fatalf("status = %d", w.Code)
	}
	var m api.Metadata
	json.Unmarshal(w.Body.Bytes(), &m)
	if m.Version == nil || *m.Version != "e2e" {
		t.Errorf("version = %v, want e2e", m.Version)
	}
}

func TestApiInfoEndpoint(t *testing.T) {
	r := newTestServer(t, cache.NoopCache{})
	w := doGet(t, r, "/")
	if w.Code != 200 {
		t.Fatalf("status = %d, body=%s", w.Code, w.Body.String())
	}
	var info api.ApiInfo
	if err := json.Unmarshal(w.Body.Bytes(), &info); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if info.Name == "" || info.Description == "" {
		t.Errorf("name/description empty: %+v", info)
	}
	if info.Version == nil || *info.Version != "e2e" {
		t.Errorf("version = %v, want e2e", info.Version)
	}
	if info.EntryCount == nil || *info.EntryCount != "1" {
		t.Errorf("entry_count = %v, want 1", info.EntryCount)
	}
	if info.Docs != "/docs" || info.Openapi != "/openapi.yaml" {
		t.Errorf("docs/openapi = %q/%q", info.Docs, info.Openapi)
	}
	if !strings.Contains(info.Frontend, "dict.illusions.app") {
		t.Errorf("frontend = %q", info.Frontend)
	}
	if len(info.Endpoints) == 0 {
		t.Error("endpoints should not be empty")
	}
}

func TestDocsEndpoints(t *testing.T) {
	r := newTestServer(t, cache.NoopCache{})
	if w := doGet(t, r, "/openapi.yaml"); w.Code != 200 || w.Body.Len() == 0 {
		t.Errorf("/openapi.yaml status=%d len=%d", w.Code, w.Body.Len())
	}
	if w := doGet(t, r, "/docs"); w.Code != 200 {
		t.Errorf("/docs status=%d", w.Code)
	}
}

func TestRobotsTxt(t *testing.T) {
	r := newTestServer(t, cache.NoopCache{})
	w := doGet(t, r, "/robots.txt")
	if w.Code != 200 {
		t.Fatalf("/robots.txt status=%d", w.Code)
	}
	body := w.Body.String()
	if !strings.Contains(body, "Disallow: /") {
		t.Errorf("robots.txt should disallow all, got:\n%s", body)
	}
	if !strings.Contains(body, "dict.illusions.app") {
		t.Errorf("robots.txt should point crawlers to the frontend, got:\n%s", body)
	}
}

// TestCachePopulatedOnMiss は miss 時に結果がキャッシュへ書き込まれることを確認する。
func TestCachePopulatedOnMiss(t *testing.T) {
	mc := newMemCache()
	r := newTestServer(t, mc)

	doGet(t, r, "/v1/lookup/entry?word=雪")
	wantKey := "genji:v1:lookup_entry:雪"
	if _, ok := mc.data[wantKey]; !ok {
		t.Errorf("expected cache key %q to be set, keys=%v", wantKey, mc.keys())
	}
	if mc.setCalls != 1 {
		t.Errorf("setCalls = %d, want 1", mc.setCalls)
	}
}

// TestCacheServedOnHit は preload した値がそのまま返り、store を介さないことを確認する。
func TestCacheServedOnHit(t *testing.T) {
	mc := newMemCache()
	r := newTestServer(t, mc)

	key := "genji:v1:lookup_entry:雪"
	preload := api.EntryList{Count: 42, Entries: []api.Entry{{Uuid: "cached", Entry: "キャッシュ"}}}
	b, _ := json.Marshal(preload)
	mc.data[key] = b

	w := doGet(t, r, "/v1/lookup/entry?word=雪")
	var list api.EntryList
	json.Unmarshal(w.Body.Bytes(), &list)
	if list.Count != 42 || list.Entries[0].Uuid != "cached" {
		t.Errorf("response not served from cache: %+v", list)
	}
	if mc.setCalls != 0 {
		t.Errorf("setCalls = %d, want 0 (hit should not re-set)", mc.setCalls)
	}
}

// TestRandomNotCached は random がキャッシュされないことを確認する。
func TestRandomNotCached(t *testing.T) {
	mc := newMemCache()
	r := newTestServer(t, mc)

	doGet(t, r, "/v1/random?count=3")
	if mc.getCalls != 0 || mc.setCalls != 0 {
		t.Errorf("random touched cache: get=%d set=%d, want 0/0", mc.getCalls, mc.setCalls)
	}
}

func TestClamp(t *testing.T) {
	five := 5
	cases := []struct {
		v             *int
		def, min, max int
		want          int
	}{
		{nil, 50, 1, 200, 50},
		{&five, 50, 1, 200, 5},
		{ptr(0), 50, 1, 200, 1},
		{ptr(999), 50, 1, 200, 200},
	}
	for _, c := range cases {
		if got := clamp(c.v, c.def, c.min, c.max); got != c.want {
			t.Errorf("clamp(%v,%d,%d,%d) = %d, want %d", c.v, c.def, c.min, c.max, got, c.want)
		}
	}
}

func ptr(n int) *int { return &n }

// fakeHeat は sitemap テスト用の制御可能な heat.Service。
type fakeHeat struct {
	ranked []heat.Ranked
	total  int
}

func (fakeHeat) Enabled() bool                                        { return true }
func (fakeHeat) Hit(...string)                                        {}
func (fakeHeat) Seed(context.Context, []heat.SeedEntry, string) error { return nil }
func (fakeHeat) StartAggregator(context.Context)                      {}
func (f fakeHeat) Page(_ context.Context, offset, limit int) ([]heat.Ranked, int, error) {
	if offset >= len(f.ranked) {
		return nil, f.total, nil
	}
	end := offset + limit
	if end > len(f.ranked) {
		end = len(f.ranked)
	}
	return f.ranked[offset:end], f.total, nil
}

const testUUID = "u1" // server テスト DB の sampleRawJSON が持つ uuid

func TestSitemapWithHeat(t *testing.T) {
	fh := fakeHeat{ranked: []heat.Ranked{{UUID: testUUID, Heat: 9}}, total: 1}
	r := newTestServerWithHeat(t, cache.NoopCache{}, fh)

	w := doGet(t, r, "/v1/sitemap?page=1&page_size=50")
	if w.Code != 200 {
		t.Fatalf("status = %d, body=%s", w.Code, w.Body.String())
	}
	var p api.SitemapPage
	if err := json.Unmarshal(w.Body.Bytes(), &p); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if p.Total != 1 || p.Page != 1 || p.PageSize != 50 || p.TotalPages != 1 {
		t.Errorf("pagination = %+v", p)
	}
	if len(p.Items) != 1 {
		t.Fatalf("got %d items, want 1", len(p.Items))
	}
	if p.Items[0].Entry != "雪" {
		t.Errorf("entry = %q, want 雪", p.Items[0].Entry)
	}
	if p.Items[0].Heat == nil || *p.Items[0].Heat != 9 {
		t.Errorf("heat = %v, want 9", p.Items[0].Heat)
	}
}

func TestSitemapFallbackWithoutRedis(t *testing.T) {
	// heat 無効（Noop）→ DB 順フォールバック、heat=null
	r := newTestServerWithHeat(t, cache.NoopCache{}, heat.Noop{})
	w := doGet(t, r, "/v1/sitemap?page=1&page_size=50")
	if w.Code != 200 {
		t.Fatalf("status = %d", w.Code)
	}
	var p api.SitemapPage
	json.Unmarshal(w.Body.Bytes(), &p)
	if p.Total != 1 || len(p.Items) != 1 {
		t.Fatalf("page = %+v", p)
	}
	if p.Items[0].Entry != "雪" {
		t.Errorf("entry = %q, want 雪", p.Items[0].Entry)
	}
	if p.Items[0].Heat != nil {
		t.Errorf("heat should be null without redis, got %v", *p.Items[0].Heat)
	}
}

func TestSitemapPaginationDefaults(t *testing.T) {
	r := newTestServerWithHeat(t, cache.NoopCache{}, heat.Noop{})
	// page/page_size 省略 → 既定 page=1, page_size=1000
	w := doGet(t, r, "/v1/sitemap")
	var p api.SitemapPage
	json.Unmarshal(w.Body.Bytes(), &p)
	if p.Page != 1 || p.PageSize != 1000 {
		t.Errorf("defaults = page %d size %d, want 1/1000", p.Page, p.PageSize)
	}
}
