# Genji Word Database

**Genji Word Database** は、[幻辞.com](https://幻辞.com) で使用する語彙データを収録した、オープンソースの日本語語彙データベースです。

複数の辞書・コーパスと独自のクローリングシステムから収集したデータを統合・加工し、アプリケーションから利用しやすい **SQLite 形式** で提供しています。

## 特徴

- **導入が簡単**：データベースファイルをダウンロードするだけで、アプリケーションに組み込めます。
- **継続的なデータ更新**：Genji の自動クローリングシステムを通じて語彙データを収集し、不定期にデータベースを更新します。
- **高速な検索**：インデックスを最適化し、数十万件の語彙データを効率よく検索できます。
- **REST API に対応**：SQLite を直接利用できない環境からも語彙を検索できます。

## データソース

以下のリソースから取得したデータを統合し、独自の加工を加えています。

1. **[JMdict（yomidevs/jmdict-yomitan）](https://github.com/yomidevs/jmdict-yomitan)**：辞書の語義や語彙データ。
2. **[Japanese Word Frequency（hingston/japanese）](https://github.com/hingston/japanese)**：語彙の使用頻度と優先順位に関するデータ。
3. **[青空文庫（aozorahack/aozorabunko_text）](https://github.com/aozorahack/aozorabunko_text)**：文学作品のコーパス。Sudachi による形態素解析を通じた未収録語の抽出、出典別の出現頻度の集計、用例（`examples.literary`）の追加に使用します。
4. **Genji Crawler System**：独自のクローリングシステムで収集した新しい語彙や語法のデータ。

## 利用方法

### REST API

SQLite を直接使用できない環境向けに、**Go と Gin** で実装した OpenAPI-first の読み取り専用 REST API（`api/`）を提供しています。

API は `genji.db` を内蔵した Docker イメージとして配布しており、必要に応じて Redis によるキャッシュも利用できます。Redis を設定しない場合は、キャッシュを無効にした状態で動作します。

~~**公開 API：** https://dict-api.illusions.app~~
(公開APIは、2026年10月22日をもってサービスを終了しました)

#### エンドポイント

| メソッド・パス | 機能 |
| --- | --- |
| `GET /` | API の基本情報（バージョン、収録語数、エンドポイント一覧）を取得 |
| `GET /v1/lookup/entry?word=雪` | 見出し語で完全一致検索 |
| `GET /v1/lookup/reading?reading=ゆき` | 読みで完全一致検索 |
| `GET /v1/search/entries?q=雪` | 見出し語・読みを全文検索（FTS5） |
| `GET /v1/search/definitions?q=snow` | 語義を全文検索（FTS5） |
| `GET /v1/random?count=5` | 語彙をランダムに取得 |
| `GET /v1/entries/{uuid}` | UUID を指定して語彙を取得 |
| `GET /v1/sitemap?page=1&page_size=1000` | 対象品詞（名詞・動詞・形容詞系列）の語彙を熱度順にページ単位で取得（サイトマップ用） |
| `GET /v1/metadata` | メタデータを取得 |
| `GET /healthz` | ヘルスチェック |
| `GET /docs` | API ドキュメント（Redoc）を表示 |

#### Docker で起動する

Docker イメージは GitHub Container Registry（GHCR）で公開しています。`linux/amd64` と `linux/arm64` に対応し、`genji.db` があらかじめ組み込まれています。

```bash
docker pull ghcr.io/illusions-lab/genji-api:latest
docker run -p 8080:8080 ghcr.io/illusions-lab/genji-api:latest
```

ローカルのソースコードからビルドして起動する場合は、次のコマンドを実行します。

```bash
docker compose up -d --build
```

起動後は `http://localhost:8080` で API にアクセスでき、`http://localhost:8080/docs` で API 仕様を確認できます。ビルドオプション（`GENJI_DB_SOURCE` など）や Redis の設定については、[`api/README.md`](./api/README.md) を参照してください。

### SQLite データベース

#### ダウンロードと解凍

[Releases](/releases) ページから `genji.db.gz` をダウンロードし、解凍して使用してください。既存のツールとの互換性を重視する場合は、gzip 版を利用できます。

```bash
gunzip genji.db.gz
```

Electron 44 以降を含む新しいクライアントには、ファイルサイズの小さい Zstandard 版（`genji.db.zst`）を推奨します。

```bash
zstd -d genji.db.zst
```

#### ファイルの整合性確認

各リリースには、機械可読なメタデータファイル `genji-manifest.json` とチェックサムファイル `SHA256SUMS` が含まれています。

`SHA256SUMS` には gzip 版、Zstandard 版、および manifest 自体のチェックサムが記録されています。圧縮ファイルを解凍する前に、次のコマンドで確認できます。

```bash
sha256sum --check SHA256SUMS
```

manifest に記録されている `database.sha256` と `database.size` は、解凍後の `genji.db` のハッシュ値とファイルサイズです。gzip 版では次のように照合できます。

```bash
gunzip -c genji.db.gz > genji.db
test "$(sha256sum genji.db | awk '{print $1}')" = "$(jq -r '.database.sha256' genji-manifest.json)"
test "$(stat --format=%s genji.db)" = "$(jq -r '.database.size' genji-manifest.json)"
```

SHA-256 によって確認できるのはファイルの完全性です。リリースの発行元を認証する電子署名ではありません。

#### リリースメタデータ

`genji-manifest.json` には、次の情報が記録されています。

| フィールド | 内容 |
| --- | --- |
| `manifestVersion` | manifest の仕様バージョン |
| `release` | コミット、ブランチ、リポジトリ、ビルド日時 |
| `database.schema` | SQLite のスキーマバージョン、論理 `sqlite_schema` の SHA-256 ハッシュ、テーブル・列・外部キー・インデックスの構造概要 |
| `database.counts` | `entries`、`definitions`、`variants` および各 FTS テーブルの実際のレコード数 |
| `assets` | 圧縮ファイルごとのサイズ・ハッシュ、および `uncompressedSize`・`uncompressedSha256` |

Zstandard 版については、フレームのウィンドウサイズ、チェックサム、コンテンツサイズも記録されます。サイズの単位はすべてバイトです。

#### SQL クエリの例

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

#### ビルド情報の確認

データベースには、ビルド情報を記録した `_metadata` テーブルがあります。

```sql
SELECT * FROM _metadata;
-- version, commit, commit_short, branch, repository, build_date, entry_count, schema_version
```

## 青空文庫コーパスによる語彙の拡充

青空文庫のテキストを Sudachi で形態素解析し、未収録語を抽出して語彙データを拡充しています。処理には `script/extract_new_words.py` と `script/create_entries.py` を使用します。

この処理では、新しい語彙データとともに以下のメタデータを追加します。

### 追加される `meta` フィールド

| フィールド | 型 | 説明 |
| --- | --- | --- |
| `meta.frequencies` | `object` | 出典別の出現回数。例：`{"aozora": 1234}` |
| `meta.variant_writings` | `string[]` | 既存の見出し語に統合した異表記（旧字体・歴史的仮名遣い）。例：`亜` に `["亞"]`、`居る` に `["ゐる"]`、`来` に `["來"]`。検索時の別表記として利用可能 |
| `meta.needs_gloss` | `bool` | `true` の場合、語義が未登録の暫定エントリ。読み・品詞・青空文庫の用例（`examples.literary`）は確定済みで、語義は後続の処理で補完 |
| `meta.needs_reading` | `bool` | `true` の場合、読み（`reading.primary`）が未確定。正式な `data/` には保存せず、`pending/needs_reading/` に隔離して人手による確認を待つ |

> **注意：** `meta.needs_gloss = true` のエントリでは、`definitions[*].gloss` が空文字列になっています。語義が登録済みのエントリのみを取得するには、次のように絞り込んでください。

```sql
-- 語義が登録済みのエントリのみ取得
SELECT e.entry, d.gloss FROM entries e
JOIN definitions d ON d.entry_uuid = e.uuid
WHERE d.gloss <> '' AND json_extract(e.meta, '$.needs_gloss') IS NULL;
```

旧字体や歴史的仮名遣いの表記は、可能な限り現代表記の見出し語へ正規化して統合します（例：`ゐる → 居る`、`來 → 来`、`氣 → 気`）。元の表記は `meta.variant_writings` に保持します。

## 活用型（`ctype`）の正規化と補完

`grammar.ctype` には、動詞や形容詞の活用型を格納します。活用型が設定されている場合、次の任意フィールドに値の由来と信頼度を記録できます。

| フィールド | 値 | 説明 |
| --- | --- | --- |
| `grammar.ctype_source` | `existing` / `pos-derived` / `manual` | 既存の値、`grammar.pos` に基づく限定的な推定、または手動指定 |
| `grammar.ctype_confidence` | `high` / `medium` / `low` | 活用型の信頼度。自動補完した値は `high`、判定できない既存値は保持して `medium` とする。`low` は将来の手動確認や他のデータソースに備えた値 |

補完スクリプトでは、既存の空でない `ctype` を常に優先します。値が空の場合も、品詞情報から活用型の候補が一つに絞れる場合に限り補完します。候補が複数ある場合や、品詞が単に `動詞`・`助動詞`・`形容動詞` とだけ記録されている場合は変更しません。

標準の動作は、ファイルを変更しない **dry-run** です。

```bash
# 全データを走査して集計結果のみ表示（JSON は変更しない）
python3 script/backfill_ctype.py

# 衝突の詳細を含む機械可読レポートを保存
python3 script/backfill_ctype.py --report ctype-report.json

# 別のデータディレクトリを検査
python3 script/backfill_ctype.py --data-dir /path/to/data

# 明示的に指定した場合のみ、各 JSON ファイルを同じディレクトリ内で原子的に置換
python3 script/backfill_ctype.py --apply
```

`--apply` を実行した後は、`ctype_source` と `ctype_confidence` を SQLite の `entries` テーブルへ反映するため、`make db` で `genji.db` を再構築してください。

SQLite のスキーマバージョンと `PRAGMA user_version` は **3** です。

## 語彙データの品質検査

正式な語彙データは `data/` に格納します。読みが確定していない候補は `pending/needs_reading/` に隔離し、SQLite には収録しません。

隔離の対象には、読みの中に漢字が残っているもの（例：`阿輩だい`）、漢字をそのまま読みとして登録したもの（例：`於蘭`）、不正な濁点を含むものなどがあります。確認待ちのデータは、先頭文字の Unicode コードポイント範囲ごとに保存します。

品質検査には、Python に付属する Unicode Character Database（UCD）を使用します。重大なエラーが見つかった場合は終了コード `1` を返し、SQLite のビルドと CI を停止します。内容の確認が必要なものは警告として扱い、終了コード `0` のまま人手による確認に回します。

### 主な検査項目

- UUIDv5 の形式、データ全体での一意性、`entry:reading.primary` からの再現性
- 読みと代替読みの Unicode 妥当性、配列内の重複、歴史的仮名 `ゟ`、不適切な濁点の位置
- `definition` の連続した `index`、`gloss` と `needs_gloss`、`grammar`、`relations`、`meta`、`frequency` の型と値の範囲
- 用例オブジェクトの形式と空でない `text`、同一定義内における NFC 正規化・前後の空白除去後の重複
- ファイルパスの NFC 正規化、各パス構成要素の 255 バイト制限、正規化や大文字・小文字の違いによる名前の衝突、読みから算出した保存先との整合性
- 正式データにおける `needs_reading` の禁止、および確認待ちデータにおける同フィールドの必須指定

未知の品詞、極端に長い見出し語や読み、標準的な用例の欠落、参照先が存在しない `relation` は警告として扱います。AI による意味の判定や、類似しているだけの用例の削除は行いません。

### 品質検査と修復

```bash
# 全件検査（読み取り専用。重大なエラーがあれば終了コード 1）
make quality

# 安全で冪等な修復（隔離、確認待ちフラグ、配列、index、完全一致する用例、重複レコード）
python3 script/check_data_quality.py --fix

# 重要度・エラーコード・data/pending の区分ごとに整理した機械可読レポート
python3 script/check_data_quality.py --json > quality-report.json

# UUIDv5 への移行は一般の修復処理とは分けて実施。--apply には対応表が必要
python3 script/migrate_uuids.py --map migrations/uuid-v5.json --apply

# 読みを手動で補完し、needs_reading を削除して UUID を移行した候補を正式データに昇格
python3 script/promote_pending.py pending/needs_reading/U+4E00-U+4EFF/候補.json
python3 script/promote_pending.py --apply pending/needs_reading/U+4E00-U+4EFF/候補.json
```

`script/json_to_sqlite.py` は、正式データが入った `data/` の重大なエラーのみをビルド前に検査します。一方、`make quality` と CI では確認待ちのデータも含めて検査します。

通常の `--fix` では UUID を変更しません。UUID を移行する場合は、旧 UUID・新 UUID・見出し語・読み・元のファイルパスを必ず台帳に記録します。

### 確認待ちの読みの解決

確認待ちの読みは、次のいずれかの根拠がある場合に限り、自動的に確定できます。

- JMdict または JMnedict に、見出し語全体と一致する一意な読みがある場合
- 異なる二つ以上の青空文庫テキストで、見出し語全体に対して明示されたルビが一致する場合

漢字一字ごとの音読み・訓読み、語の一部を組み合わせた読み、形態素解析器や生成 AI による推測は採用しません。読みが曖昧な候補、根拠が矛盾する候補、語幹の一部にすぎない可能性がある候補は `pending/needs_reading/` に残します。

```bash
# 読み取り専用。審査用の JSON と CSV レポートを生成
python3 script/resolve_pending_readings.py \
  --jmdict /path/to/JMdict.gz \
  --jmnedict /path/to/JMnedict.xml.gz \
  --aozora-dir /path/to/aozorabunko_text \
  --web-cache-dir /path/to/reviewed-web-cache \
  --report /tmp/pending-readings.json \
  --decision-ledger migrations/pending-reading-decisions-YYYY-MM-DD.json

# 承認済みの判定のみを適用（完全なレポート・決定台帳・UUID 台帳が必要）
python3 script/resolve_pending_readings.py --apply \
  --jmdict /path/to/JMdict.gz \
  --jmnedict /path/to/JMnedict.xml.gz \
  --aozora-dir /path/to/aozorabunko_text \
  --report /tmp/pending-readings.json \
  --decision-ledger migrations/pending-reading-decisions-YYYY-MM-DD.json \
  --ledger migrations/pending-reading-uuids-YYYY-MM-DD.json \
  --review-approvals /path/to/reviewer-approvals.json
```

決定台帳には、確認待ちの各 UUID と元のファイルパスに対して、`promote`、`merge_fragment`、`reject_fragment`、`keep_pending`、`conflict` のいずれか一つの判定を記録します。あわせて、根拠となる URL、情報源の区分（source tier）、取得時刻、内容の SHA-256、根拠の連鎖（evidence chain）も保持します。同じ入力で再実行しても、レポートと台帳は変更されません。

一般の Web 情報を根拠とした正式データへの昇格や、断片的なエントリの変更には、対象の `decision_id`、`action`、`reviewer` を記録した承認が必要です。却下した元のレコードは変更せずに保存するため、決定台帳を使って復元できます。

### ローカル LLM による読みの審査支援

`script/resolve_pending_with_llm.py` では、まず機械的に判定できる候補を処理します。辞書の見出し語との完全一致、旧字体を正規化した既存の見出し語、KANJIDIC2 から構成した有限個の読みの候補を使用します。

OpenAI 互換のローカル API（標準設定では LM Studio）には、残った候補の審査のみを依頼します。LLM が自由生成した読みは採用しません。

```bash
python3 script/resolve_pending_with_llm.py \
  --kanjidic /path/to/kanjidic2.xml.gz \
  --web-cache-dir /tmp/genji-web-cache \
  --checkpoint /tmp/genji-gemma.jsonl \
  --report /tmp/genji-pending-report.json \
  --decision-ledger /tmp/genji-pending-decisions.json \
  --review-queue /tmp/genji-gemma-review.json
```

長時間の処理は、JSONL 形式のチェックポイントから再開できます。二度の判定結果が一致した場合でも、LLM の出力は独立したレビューキューへの提案にとどまり、読みの確定やデータの破棄には直接反映されません。また、`--trust-llm` と正式データへの `--apply` は併用できません。

Web キャッシュには検索結果の要約ではなく、見出し語全体のページを解析した結果、URL、取得時刻、レスポンスの SHA-256 を保存します。検証済みのキャッシュは、一時的なサイト障害によって上書きされません。

---

<div align="right">
  <a href="https://www.art.nihon-u.ac.jp/education/department/literature/">
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/illusions-lab/.github/refs/heads/main/images/NUArt_colored.svg">
      <img src="https://raw.githubusercontent.com/illusions-lab/.github/refs/heads/main/images/NUArt.svg" height="64" alt="日本大学芸術学部">
    </picture>
  </a>
</div>
