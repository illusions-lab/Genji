# Genji Word Database

https://幻辞.com

幻辞.comで使用される全語彙データを収録した、オープンソースの日本語語彙データベースです。

本リポジトリでは、複数の信頼できるソースと独自のクローリングシステムを統合・加工した語彙データを、扱いやすい **SQLite 形式** で提供しています。

## 📦 特徴

- **SQLite 形式**: データベースファイルをダウンロードするだけで、すぐにアプリケーションに組み込み可能です。
- **自動更新**: Genji の自動クローリングシステムにより、不定期にデータがビルドされ、常に最新の語彙が反映されます。
- **軽量かつ高速**: インデックスが最適化されており、数十万件のデータから瞬時に検索が可能です。

## 📂 データソース (Data Sources)

本データベースは、以下の優れたリソースを統合し、独自の加工を施したものです：

1.  **[JMdict (yomidevs/jmdict-yomitan)](https://github.com/yomidevs/jmdict-yomitan)** - 広範な辞書定義および語彙データ。
2.  **[Japanese Word Frequency (hingston/japanese)](https://github.com/hingston/japanese)** - 語彙の頻度・優先順位データ。
3.  **[青空文庫 (aozorahack/aozorabunko_text)](https://github.com/aozorahack/aozorabunko_text)** - 文学作品コーパス。形態素解析（Sudachi）による未収録語の抽出・出典別出現頻度、および実例（`examples.literary`）の付与に使用。
4.  **Genji Crawler System** - 独自のクローリングシステムによる最新のトレンド語彙および語法データ。

## 🚀 使い方

### API

SQLite を直接使用できない環境向けに、Go + Gin 製の **OpenAPI-first** な REST API を提供しています（`api/`、read-only）。`genji.db` を内蔵した self-contained な Docker イメージで配布され、任意で Redis キャッシュを併用できます（未設定ならキャッシュ無効で動作）。

**公開Endpoint: `https://dict-api.illusions.app`**

| メソッド・パス | 説明 |
|---|---|
| `GET /` | API トップ情報（バージョン・収録語数・エンドポイント一覧） |
| `GET /v1/lookup/entry?word=雪` | 見出し語の完全一致 |
| `GET /v1/lookup/reading?reading=ゆき` | 読みの完全一致 |
| `GET /v1/search/entries?q=雪` | 見出し語・読みの全文検索（FTS5） |
| `GET /v1/search/definitions?q=snow` | 語釈の全文検索（FTS5） |
| `GET /v1/random?count=5` | ランダム取得 |
| `GET /v1/entries/{uuid}` | UUID で取得 |
| `GET /v1/sitemap?page=1&page_size=1000` | 対象品詞（名詞・動詞・形容詞系列）の語を熱度順にページング（sitemap 用） |
| `GET /v1/metadata` ・ `GET /healthz` | メタデータ・ヘルスチェック |
| `GET /docs` | API ドキュメント（Redoc） |

```bash
docker pull ghcr.io/illusions-lab/genji-api:latest
docker run -p 8080:8080 ghcr.io/illusions-lab/genji-api:latest
# http://localhost:8080/docs で仕様を閲覧
```

詳細は [`api/README.md`](./api/README.md) を参照してください。

### SQLite を直接使用する

[Releases](/releases) ページから最新の `genji.db.gz` をダウンロードし、解凍して使用してください。

```bash
gunzip genji.db.gz
```

#### クエリ例
```sql
-- 見出し語で検索
SELECT raw_json FROM entries WHERE entry = '幻辞';

-- 読みで検索
SELECT e.entry, d.gloss FROM entries e
JOIN definitions d ON d.entry_uuid = e.uuid
WHERE e.reading_primary = 'ゆき';

-- 全文検索（FTS5）
SELECT e.entry, e.reading_primary FROM fts_entries fts
JOIN entries e ON e.uuid = fts.uuid
WHERE fts_entries MATCH '雪';

-- 頻度順に上位 10 件を取得する
SELECT entry, reading_primary, json_extract(meta, '$.freq_rank') AS freq
FROM entries WHERE freq IS NOT NULL
ORDER BY freq ASC LIMIT 10;
```

#### メタデータの確認

データベースにはビルド情報を格納する `_metadata` テーブルが含まれています。

```sql
SELECT * FROM _metadata;
-- version, commit, branch, repository, build_date, entry_count
```

## 🌸 青空文庫由来の語彙拡充（新語スケルトン・表記揺れ吸収）

青空文庫コーパスを形態素解析（Sudachi）して未収録語を抽出し、語彙を継続的に拡充しています（`script/extract_new_words.py` → `script/create_entries.py`）。この過程で次のデータ・メタフィールドが追加されます。

### 追加された `meta` フィールド

| フィールド | 型 | 意味 |
|---|---|---|
| `meta.frequencies` | `object` | 出典別の総出現回数。例: `{"aozora": 1234}`。 |
| `meta.variant_writings` | `string[]` | 既存エントリへ吸収した**異表記**（旧字体・歴史的仮名遣い）。例: `亜` に `["亞"]`、`居る` に `["ゐる"]`、`来` に `["來"]`。検索のエイリアスとして利用できます。 |
| `meta.needs_gloss` | `bool` | `true` の場合、語義（`definitions[*].gloss`）が**未生成のスケルトン**。読み・品詞・青空文庫実例（`examples.literary`）のみ確定済みで、語義は後続のバックフィルで埋められます。 |
| `meta.needs_reading` | `bool` | `true` の場合、読み（`reading.primary`）が未確定。正式な `data/` には入れず、`pending/needs_reading/` で人工確認を待ちます。 |

> **⚠️ 注意:** `meta.needs_gloss = true` のエントリは `definitions[*].gloss` が空文字です。語義の有無で絞り込む場合は次のように除外してください。
>
> ```sql
> -- 語義が確定済みのエントリのみ
> SELECT e.entry, d.gloss FROM entries e
> JOIN definitions d ON d.entry_uuid = e.uuid
> WHERE d.gloss <> '' AND json_extract(e.meta, '$.needs_gloss') IS NULL;
> ```

旧字体・歴史的仮名遣いの表記は、可能な限り現代表記の見出しへ正規化（`ゐる→居る`・`來→来`・`氣→気` 等）して吸収し、原表記は `meta.variant_writings` に保持します。

## 🔤 活用型（ctype）の正規化・補完

`grammar.ctype` には動詞・形容詞の活用型を格納します。活用型が設定されている
場合は、次の任意フィールドで由来と信頼度も表現できます。

| フィールド | 値 | 意味 |
|---|---|---|
| `grammar.ctype_source` | `existing` / `pos-derived` / `manual` | 既存値、`grammar.pos` からの保守的な推導、または人工指定。 |
| `grammar.ctype_confidence` | `high` / `medium` / `low` | 活用型の信頼度。自動補完は `high`、未知の既存値は保持した上で `medium`。`low` は将来の人工・他ソース用。 |

補完スクリプトは既存の非空 `ctype` を常に優先し、空値については POS が示す
候補が一種類だけのときに限って補完します。複数候補の衝突や、汎用的な
`動詞`・`助動詞`・`形容動詞` だけのエントリは変更しません。既定動作は
読み取り専用の dry-run です。

```bash
# 全データを走査して集計だけ表示（JSON は変更しない）
python3 script/backfill_ctype.py

# 衝突明細を含む機械可読レポートも保存
python3 script/backfill_ctype.py --report ctype-report.json

# 別のデータルートを検査
python3 script/backfill_ctype.py --data-dir /path/to/data

# 明示指定した場合のみ、各 JSON を同一ディレクトリ内で原子的に置換
python3 script/backfill_ctype.py --apply
```

`--apply` 後は `ctype_source` と `ctype_confidence` を SQLite の `entries` 表へ
反映するため、`make db` で `genji.db` を再構築してください。SQLite schema と
`PRAGMA user_version` は version 3 です。

## 🔎 詞条データ品質検査

`data/` は読音が確定した正式データ専用です。読音解析に失敗した候補（漢字が
残る `阿輩だい`、漢字をそのまま読音にした `於蘭`、不正な濁点など）は
`pending/needs_reading/` に隔離され、SQLite には収録されません。待審データは
先頭文字の Unicode コードポイント範囲別に配置されます。

品質規則は Python 同梱の Unicode Character Database (UCD) を参照します。硬錯誤
（終了碼 1）は SQLite 建置與 CI を阻擋し、內容上の疑点は警告（終了碼 0）として
人工審査に回します。

- UUIDv5 の形式、全域一意性、`entry:reading.primary` からの再現性
- 読音（代替読音を含む）の Unicode、配列重複、歴史的仮名 `ゟ`、錯置濁点
- definition の連続 index、gloss / `needs_gloss`、grammar、relations、meta、frequency の型と範囲
- 例句 object と非空 text、および同一定義内の NFC＋trim 後の重複
- パス NFC、255-byte component 制限、正規化／大小文字衝突、読音から計算した配置
- 正式データの `needs_reading` 禁止と、待審データの同 marker 必須

未知詞性、異常に長い見出し／読音、標準例句なし、存在しない relation target は
警告です。AI による意味判断や曖昧な類似例句の削除は行いません。

```bash
# 読み取り専用の全件検査（問題があれば終了コード 1）
make quality

# 安全で冪等な修復（隔離、待審 marker、配列、index、exact 例句、重複 record）
python3 script/check_data_quality.py --fix

# severity → code → data/pending でグループ化した機械可読レポート
python3 script/check_data_quality.py --json > quality-report.json

# UUIDv5 遷移は一般修復と分離。--apply 時は対照表の指定が必須
python3 script/migrate_uuids.py --map migrations/uuid-v5.json --apply

# 読音を人工補完し needs_reading を削除、UUID 遷移後に正式領域へ昇格
python3 script/promote_pending.py pending/needs_reading/U+4E00-U+4EFF/候補.json
python3 script/promote_pending.py --apply pending/needs_reading/U+4E00-U+4EFF/候補.json
```

待審讀音は、完整詞条に対する JMdict / JMnedict の一意な読音、または二つ以上の
異なる青空文庫テキストで一致する明示 Ruby だけを自動解決できます。単字の音訓、
部分語の組み合わせ、形態素解析器や生成 AI による推測は使用しません。曖昧・衝突・
疑似語幹の候補は `pending/needs_reading/` に残ります。

```bash
# 読み取り専用。JSON と CSV の審査レポートを生成
python3 script/resolve_pending_readings.py \
  --jmdict /path/to/JMdict.gz \
  --jmnedict /path/to/JMnedict.xml.gz \
  --aozora-dir /path/to/aozorabunko_text \
  --web-cache-dir /path/to/reviewed-web-cache \
  --report /tmp/pending-readings.json \
  --decision-ledger migrations/pending-reading-decisions-YYYY-MM-DD.json

# 承認済みの決定だけを適用。完全レポート、決定台帳、UUID 台帳が必須
python3 script/resolve_pending_readings.py --apply \
  --jmdict /path/to/JMdict.gz \
  --jmnedict /path/to/JMnedict.xml.gz \
  --aozora-dir /path/to/aozorabunko_text \
  --report /tmp/pending-readings.json \
  --decision-ledger migrations/pending-reading-decisions-YYYY-MM-DD.json \
  --ledger migrations/pending-reading-uuids-YYYY-MM-DD.json \
  --review-approvals /path/to/reviewer-approvals.json
```

決定台帳は各 pending UUID と原パスに対して `promote`、`merge_fragment`、
`reject_fragment`、`keep_pending`、`conflict` のいずれか一件を保持します。根拠 URL、
source tier、取得時刻、内容 SHA-256 と evidence chain を併記し、同一入力の再実行では
report と ledger を変更しません。一般 Web 情報による昇格と全ての fragment 変更は、
対象 `decision_id`、action、reviewer を記した approval がなければ適用できません。
rejected へ移す原 record は変更せず保存されるため、決定台帳から復元できます。

`script/json_to_sqlite.py` は正式 `data/` の硬錯誤だけをビルド前に検査します。
`make quality` と CI は待審領域も含めた完全検査を行います。一般 `--fix` は UUID を
変更せず、UUID 遷移は必ず旧値・新値・見出し・読音・原パスを台帳に残します。

### ローカル LLM を使う残存読音の審査

`resolve_pending_with_llm.py` は、機械で確定できる処理を先に行います。完全一致の
辞書見出し、旧字体を正規化した既存見出し、KANJIDIC2 から組み立てた有限の読音候補
を使い、OpenAI 互換のローカル API（既定は LM Studio）には残存候補の審査だけを
依頼します。API の自由生成した読音は受理しません。

```bash
python3 script/resolve_pending_with_llm.py \
  --kanjidic /path/to/kanjidic2.xml.gz \
  --web-cache-dir /tmp/genji-web-cache \
  --checkpoint /tmp/genji-gemma.jsonl \
  --report /tmp/genji-pending-report.json \
  --decision-ledger /tmp/genji-pending-decisions.json \
  --review-queue /tmp/genji-gemma-review.json
```

長時間の呼出しは JSONL checkpoint から再開できます。二回の判定が一致しても、
LLM の結果は常に独立した review queue の提案に留まり、読音や破棄を直接適用できません。
`--trust-llm` と正式 `--apply` の併用は拒否されます。Web cache は検索摘要ではなく、
完全見出しページの解析結果、URL、取得時刻、response SHA-256 を保存します。既存の
確定 cache は一時的なサイト障害で上書きしません。

### Docker

API の Docker イメージは GHCR で配布しています（`linux/amd64` / `linux/arm64` 対応、`genji.db` 内蔵）。

```bash
docker pull ghcr.io/illusions-lab/genji-api:latest
docker run -p 8080:8080 ghcr.io/illusions-lab/genji-api:latest
```

ローカルからビルドして起動する場合:

```bash
docker compose up -d --build
```

`http://localhost:8080` で API、`http://localhost:8080/docs` で仕様を閲覧できます。
詳細なビルドオプション（`GENJI_DB_SOURCE` や Redis 併用）は [`api/README.md`](./api/README.md) を参照してください。


<div align="right">
  <a href="https://www.art.nihon-u.ac.jp/education/department/literature/">
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/illusions-lab/.github/refs/heads/main/images/NUArt_colored.svg">
      <img src="https://raw.githubusercontent.com/illusions-lab/.github/refs/heads/main/images/NUArt.svg" height="64" alt="日本大学芸術学部">
    </picture>
  </a>
</div>
