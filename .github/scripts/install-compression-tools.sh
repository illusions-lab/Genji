#!/usr/bin/env bash
# Installs the exact encoders used for reproducible Genji release compression.
set -euo pipefail

readonly PIGZ_VERSION=2.8
readonly ZSTD_VERSION=1.5.7
readonly PREFIX="${RUNNER_TEMP:?RUNNER_TEMP is required}/genji-compression-tools"
readonly BUILD_DIR="${RUNNER_TEMP}/genji-compression-build"

sudo apt-get update
sudo apt-get install --yes --no-install-recommends build-essential curl
rm -rf "$PREFIX" "$BUILD_DIR"
mkdir -p "$PREFIX/bin" "$BUILD_DIR"

curl --fail --location --retry 3 --silent --show-error \
  "https://zlib.net/pigz/pigz-${PIGZ_VERSION}.tar.gz" \
  -o "$BUILD_DIR/pigz.tar.gz"
tar -xzf "$BUILD_DIR/pigz.tar.gz" -C "$BUILD_DIR"
make -C "$BUILD_DIR/pigz-${PIGZ_VERSION}" -j"$(nproc)"
install "$BUILD_DIR/pigz-${PIGZ_VERSION}/pigz" "$PREFIX/bin/pigz"

curl --fail --location --retry 3 --silent --show-error \
  "https://github.com/facebook/zstd/releases/download/v${ZSTD_VERSION}/zstd-${ZSTD_VERSION}.tar.gz" \
  -o "$BUILD_DIR/zstd.tar.gz"
tar -xzf "$BUILD_DIR/zstd.tar.gz" -C "$BUILD_DIR"
make -C "$BUILD_DIR/zstd-${ZSTD_VERSION}" -j"$(nproc)" zstd
install "$BUILD_DIR/zstd-${ZSTD_VERSION}/programs/zstd" "$PREFIX/bin/zstd"

"$PREFIX/bin/pigz" -V | grep -F "pigz ${PIGZ_VERSION}"
"$PREFIX/bin/zstd" --version | grep -F "v${ZSTD_VERSION}"
echo "$PREFIX/bin" >> "$GITHUB_PATH"
