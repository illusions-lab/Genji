# syntax=docker/dockerfile:1
#
# Genji API (Go + Gin) — DB 内蔵のマルチステージビルド。
#
# FTS5 のため mattn/go-sqlite3 を sqlite_fts5 タグ + CGO で固定ビルドする。
#
# DB の供給は GENJI_DB_SOURCE で切替:
#   remote (既定): GitHub Releases から genji.db.gz を取得（ローカル単体ビルド向け）
#   local        : ビルドコンテキストの genji.db を埋め込む（CI でアーティファクト再利用）
#
# 使用例:
#   docker build -t genji-api .                                   # remote, latest
#   docker build --build-arg GENJI_DB_VERSION=2026.4.1.120000 .   # remote, 特定版
#   docker build --build-arg GENJI_DB_SOURCE=local .              # ローカルの genji.db を埋め込む

ARG GENJI_DB_SOURCE=remote
ARG GENJI_DB_VERSION=latest

# --- DB: remote から取得 ---
FROM debian:bookworm-slim AS db-remote
ARG GENJI_DB_VERSION
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl gzip \
    && rm -rf /var/lib/apt/lists/*
RUN set -eux; \
    if [ "$GENJI_DB_VERSION" = "latest" ]; then \
      BASE_URL="https://github.com/Iktahana/Genji/releases/latest/download"; \
    else \
      BASE_URL="https://github.com/Iktahana/Genji/releases/download/${GENJI_DB_VERSION}"; \
    fi; \
    curl -fSL "$BASE_URL/genji.db.gz" -o /tmp/genji.db.gz; \
    curl -fSL "$BASE_URL/SHA256SUMS" -o /tmp/SHA256SUMS; \
    grep '  genji.db.gz$' /tmp/SHA256SUMS > /tmp/genji.db.gz.sha256; \
    test "$(wc -l < /tmp/genji.db.gz.sha256)" -eq 1; \
    cd /tmp; \
    sha256sum --check genji.db.gz.sha256; \
    gunzip -c /tmp/genji.db.gz > /genji.db; \
    rm -f /tmp/genji.db.gz /tmp/SHA256SUMS /tmp/genji.db.gz.sha256

# --- DB: ローカルのコンテキストから ---
FROM debian:bookworm-slim AS db-local
COPY genji.db /genji.db

# --- DB ステージを選択 ---
FROM db-${GENJI_DB_SOURCE} AS db

# --- ビルド ---
FROM golang:1.26-bookworm AS builder
WORKDIR /src
ENV CGO_ENABLED=1
# 依存を先に取得してレイヤーキャッシュを効かせる
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN go build -tags sqlite_fts5 -ldflags="-s -w" -o /out/genji-api ./cmd/genji-api

# --- ランタイム ---
FROM gcr.io/distroless/base-debian12 AS runtime
COPY --from=builder /out/genji-api /usr/local/bin/genji-api
COPY --from=db /genji.db /data/genji.db
ENV GENJI_DB_PATH=/data/genji.db
ENV PORT=8080
EXPOSE 8080
ENTRYPOINT ["/usr/local/bin/genji-api"]
