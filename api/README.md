# 『幻辞』 Genji API (Go + Gin)

幻辞プロジェクトの語彙データベース（約21万5千語）を提供する REST API の Go 実装。
**OpenAPI-first**（[`openapi.yaml`](./openapi.yaml) を真実の源とし、`oapi-codegen` でサーバーコードを生成）。

read-only な API で、単一の `genji.db`（SQLite + FTS5）を参照する。

## エンドポイント

| メソッド・パス | 説明 | パラメータ |
|---|---|---|
| `GET /` | API トップ情報（辞書バージョン・収録語数・エンドポイント一覧） | — |
| `GET /healthz` | ヘルスチェック（DB 疎通） | — |
| `GET /v1/metadata` | ビルドメタデータ（version / commit / entry_count / schema_version 等） | — |
| `GET /v1/entries/{uuid}` | UUID で1件取得 | path: `uuid` |
| `GET /v1/lookup/entry` | 見出し語の完全一致 | query: `word`（必須） |
| `GET /v1/lookup/reading` | 読み（かな）の完全一致 | query: `reading`（必須） |
| `GET /v1/search/entries` | 見出し語・読みの全文検索（FTS5） | query: `q`（必須）, `limit`（既定50, 上限200） |
| `GET /v1/search/definitions` | 語釈(gloss)の全文検索（FTS5） | query: `q`（必須）, `limit`（既定50, 上限200） |
| `GET /v1/random` | ランダム取得 | query: `count`（既定5, 上限100） |
| `GET /v1/sitemap` | 全語彙を熱度順にページング | query: `page`（既定1）, `page_size`（既定1000, 上限50000） |
| `GET /openapi.yaml` | OpenAPI 仕様（バイナリ同梱） | — |
| `GET /docs` | API ドキュメント（Redoc） | — |
| `GET /robots.txt` | クローラ拒否（前端へ誘導） | — |

### リクエスト例

```bash
curl 'http://localhost:8080/healthz'
curl 'http://localhost:8080/v1/lookup/entry?word=雪'
curl 'http://localhost:8080/v1/lookup/reading?reading=ゆき'
curl 'http://localhost:8080/v1/search/entries?q=雪'
curl 'http://localhost:8080/v1/search/definitions?q=snow&limit=10'
curl 'http://localhost:8080/v1/random?count=3'
curl 'http://localhost:8080/v1/metadata'
```

### レスポンス例（`/v1/lookup/entry?word=雪`）

```json
{
  "count": 1,
  "entries": [
    {
      "uuid": "…",
      "entry": "雪",
      "reading": { "primary": "ゆき", "alternatives": [], "is_heteronym": false },
      "grammar": { "pos": ["名詞"] },
      "definitions": [ { "index": 1, "gloss": "snow", "register": "standard" } ],
      "relations": { "homophones": [], "synonyms": [], "antonyms": [], "related": [] },
      "meta": { "version": "1.0.0", "source": "…", "updated_at": "…" }
    }
  ]
}
```

API 仕様の全体はサーバー起動後にブラウザで `http://localhost:8080/docs` から閲覧できる。

## 環境変数

| 変数 | 既定値 | 説明 |
|---|---|---|
| `GENJI_DB_PATH` | `genji.db` | SQLite DB へのパス（Docker イメージ内は `/data/genji.db`） |
| `PORT` | `8080` | HTTP リッスンポート |
| `GENJI_DB_MAX_OPEN_CONNS` | `4` | SQLite 接続プールの最大接続数 |
| `GENJI_DB_MAX_IDLE_CONNS` | `4` | SQLite 接続プールで維持するアイドル接続数 |
| `GENJI_REDIS_ADDR` | （空） | Redis アドレス（例 `localhost:6379`）。**空ならキャッシュ無効** |
| `GENJI_REDIS_PASSWORD` | （空） | Redis パスワード |
| `GENJI_REDIS_DB` | `0` | Redis DB 番号 |
| `GENJI_CACHE_TTL` | `1h` | キャッシュ TTL（`time.ParseDuration` 形式、例 `30m`） |
| `GENJI_HEAT_AGG_INTERVAL` | `15m` | 熱度ランキングの集計間隔 |
| `GENJI_HEAT_W30` | `2` | 直近30日アクセス数の重み |
| `GENJI_HEAT_W365` | `1` | 直近365日アクセス数の重み |
| `GENJI_HEAT_W_FREQ` | `1` | log 正規化したコーパス頻度（`meta.frequencies` 合計）の重み |

> **キャッシュ・熱度は任意**。`GENJI_REDIS_ADDR` が未設定、または Redis に接続できない場合は
> キャッシュ無効・熱度無効（sitemap は DB 順フォールバック）で動作する。API の可用性は Redis に依存しない。

## キャッシュの仕組み

read-only な辞書 API なので、クエリ結果を Redis にキャッシュできる。Redis は**必須ではない**。

### 有効化とフォールバック（`internal/cache`）

- `GENJI_REDIS_ADDR` が**空** → `NoopCache`（一切キャッシュしない）。
- アドレス指定あり → Redis クライアントを生成し **3 秒タイムアウトで PING** を実行。
  - 成功 → `RedisCache` を使用（ログ `cache: enabled`）。
  - **失敗 → エラーで落とさず `NoopCache` にフォールバック**（ログ `cache: redis ping failed ...`）。
    起動も応答も Redis 障害に巻き込まれない。

`GET /healthz` のレスポンスの `cache` フィールドで現在の状態（`enabled` / `disabled`）を確認できる。

### Read-through の流れ（`internal/server` の `cached()` ヘルパー）

1. `cache.Get(key)` を試す。**ヒットして JSON デコードに成功したら、その値をそのまま返す**（DB を引かない）。
2. ミス、またはデコード失敗 → store（DB）を引く。
3. 成功した結果を `json.Marshal` し、`cache.Set(key, value, TTL)` で保存してから返す。
4. `Set` の失敗はログのみで、レスポンスには影響しない。

キャッシュに保存される値は、各エンドポイントのレスポンス構造体（`EntryList` など）の **JSON バイト列**。

### キャッシュ対象とキー

| エンドポイント | キャッシュキー |
|---|---|
| `GET /v1/metadata` | `genji:v1:metadata` |
| `GET /v1/entries/{uuid}` | `genji:v1:entry:{uuid}` |
| `GET /v1/lookup/entry` | `genji:v1:lookup_entry:{word}` |
| `GET /v1/lookup/reading` | `genji:v1:lookup_reading:{reading}` |
| `GET /v1/search/entries` | `genji:v1:search_entries:{limit}:{q}` |
| `GET /v1/search/definitions` | `genji:v1:search_definitions:{limit}:{q}` |

**キャッシュしないエンドポイント**:

- `GET /v1/random` — 毎回異なる結果を返すべきなので、キャッシュを参照も保存もしない。
- `GET /healthz` — 常に DB の即時状態を反映する必要があるため。

### TTL

すべての `Set` に `GENJI_CACHE_TTL`（既定 `1h`）が適用される。データ更新（新しい `genji.db` のデプロイ）を
即時反映したい場合は、TTL を短くするか、デプロイ時に Redis をフラッシュする。
キーは `genji:v1:` プレフィックスで名前空間化されているため、`redis-cli --scan --pattern 'genji:v1:*'` で
まとめて確認・削除できる。

> なお `GET /v1/sitemap` の各ページも短 TTL（`min(GENJI_CACHE_TTL, 集計間隔)`）でキャッシュされる
> （キー `genji:v1:sitemap:{page_size}:{page}`）。

## 熱度ランキング（sitemap）

`GET /v1/sitemap` は前端の sitemap 生成用に、**対象品詞の語彙を熱度（人気度）の降順**でページングして返す。
熱度はアクセス数を Redis でカウントして算出する（`internal/heat`）。

**対象品詞**: 品詞（`pos`）の大分類が **名詞 / 動詞 / 形容詞 / 形容動詞** のいずれかの語のみを収録する
（`名詞-の形容詞`・`動詞-サ変`・`形容詞-語幹` 等も含む）。表現・副詞・助詞などの語、品詞情報の無い語は除外。
この絞り込みは Redis 有効時（土台 `genji:entries:all` を対象品詞のみで seed）・無効時（DB クエリの `WHERE`）の両方に適用される。

### 熱度の定義

```
heat = GENJI_HEAT_W30 × (直近30日のアクセス数) + GENJI_HEAT_W365 × (直近365日のアクセス数)
     + GENJI_HEAT_W_FREQ × log(1 + Σmeta.frequencies)
     = 2 × c30 + 1 × c365 + 1 × log(1 + 頻度合計)   （既定）
```

- 365日窓は30日窓を**含む**。よって直近30日のアクセスは実質 3 倍、31〜365日は 1 倍。
- 「アクセス」としてカウントするのは `GET /v1/entries/{uuid}` / `GET /v1/lookup/entry` / `GET /v1/lookup/reading`
  で**返った各語の uuid**（検索結果は対象外）。記録は非同期・ベストエフォート。
- 末尾の頻度項は `meta.frequencies`（青空文庫など出典別の総出現回数）の合計を log 正規化した
  **コーパス頻度の下地**。アクセスが少ない語のコールドスタート時の並び順を、出現頻度で決める。
  アクセスが増えれば人気度が優先される。log でアクセス数（小さい整数）と桁を揃えている。

### Redis データモデルと集計

| キー | 用途 |
|---|---|
| `genji:hits:day:{YYYYMMDD}` | その日の語ごとのアクセス数（ZSET, TTL 366日） |
| `genji:entries:all` | 全 uuid を 0 点で保持する土台（起動時に DB から seed） |
| `genji:heat:freq` | 全 uuid を `log(1+頻度合計)` で保持（起動時に DB から seed）。頻度の下地 |
| `genji:heat:index` | 集計済みランキング（ZSET, uuid→heat）。sitemap が `ZREVRANGE` で読む |

- バックグラウンドで `GENJI_HEAT_AGG_INTERVAL`（既定 15分）ごとに集計：
  直近30日 / 365日の day キーを `ZUNIONSTORE` で合算し、`genji:heat:freq`（重み `W_FREQ`）と
  重み付け合算して `genji:heat:index` を再構築する。
- 多重インスタンスでの重複集計は `genji:heat:agg:lock`（SETNX）で防ぐ。
- DB バージョン（`_metadata.version`）が変わると `genji:entries:all` と `genji:heat:freq` を再 seed する。

### Redis 無効時のフォールバック

`GENJI_REDIS_ADDR` 未設定・接続失敗時は、`/v1/sitemap` は DB の安定順
（`freq_rank` 昇順 → 見出し語順）で全件をページングし、`heat` は `null` を返す。
エンドポイント自体は常に動作する。

## ローカル開発

FTS5 を使うため、**`mattn/go-sqlite3` を `sqlite_fts5` ビルドタグ + cgo（`CGO_ENABLED=1`）でビルドする必要がある**。`Makefile` がこれを内包している。

```bash
make generate   # openapi.yaml からコード生成（*.gen.go と内蔵用 openapi.yaml を更新）
make build      # bin/genji-api をビルド
make test       # テスト実行
make vet        # go vet

# 起動（ローカルの genji.db を指定）
GENJI_DB_PATH=../genji.db make run
```

`genji.db` が手元に無い場合は、リポジトリ root で `make db`（`script/json_to_sqlite.py`）で生成できる。

## Docker

イメージには `genji.db` を**内蔵**する（外部マウント不要の self-contained イメージ）。
DB の供給は `GENJI_DB_SOURCE` で切り替える。

```bash
# 既定 (remote): GitHub Releases から最新 DB を取得して内蔵
docker build -t genji-api ./api
docker run -p 8080:8080 genji-api

# 特定バージョンの DB を内蔵
docker build --build-arg GENJI_DB_VERSION=2026.4.1.120000 -t genji-api ./api

# ローカルの genji.db を内蔵（api/genji.db を配置してから）
cp genji.db api/genji.db
docker build --build-arg GENJI_DB_SOURCE=local -t genji-api ./api
```

### Redis 併用（任意）

```bash
docker run -p 8080:8080 -e GENJI_REDIS_ADDR=host.docker.internal:6379 genji-api
```

`docker compose up` でも `api-go`（+ コメントアウト済みの `redis`）サービスとして起動できる。

## 公開イメージ（GHCR）

`main` への push で CI（`.github/workflows/build-and-release.yml` の `docker` ジョブ）が
multi-arch（amd64/arm64）でビルドし、SQLite リリースと同じ日付ベースのバージョン（`YYYY.M.D.HHMMSS`）+ `latest` で公開する。

```bash
docker pull ghcr.io/illusions-lab/genji-api:latest
# または特定版
docker pull ghcr.io/illusions-lab/genji-api:2026.4.1.120000
```

## アーキテクチャ

```
cmd/genji-api      エントリポイント（DB open, router, CORS, graceful shutdown）
internal/api       OpenAPI 生成コード（型・Gin strict server）+ /openapi.yaml・/docs 配信
internal/server    StrictServerInterface の実装（cache → store の順で参照）
internal/store     genji.db への read-only アクセス（SQL + FTS5 サニタイズ）
internal/cache     Cache 抽象（Redis 実装 / Noop 実装 + フォールバック）
internal/config    環境変数の読み込み
```
