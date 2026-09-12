#!/usr/bin/env bash
set -euo pipefail

readonly MINIMUM_MEMORY_BYTES=$((32 * 1000 * 1000 * 1000))
readonly MINIMUM_AVAILABLE_DISK_BYTES=$((100 * 1000 * 1000 * 1000))

if [[ "$(uname -s)" != Linux || "$(uname -m)" != x86_64 ]]; then
  echo "Compression requires an Ubuntu/Linux x64 runner." >&2
  exit 1
fi

memory_kib=$(awk '/^MemTotal:/ { print $2 }' /proc/meminfo)
memory_bytes=$((memory_kib * 1024))
available_disk_bytes=$(df --output=avail --block-size=1 . | awk 'NR == 2 { print $1 }')
if (( memory_bytes < MINIMUM_MEMORY_BYTES )); then
  echo "Compression runner RAM check failed: actual=${memory_bytes} bytes, required=${MINIMUM_MEMORY_BYTES} bytes." >&2
  exit 1
fi
if (( available_disk_bytes < MINIMUM_AVAILABLE_DISK_BYTES )); then
  echo "Compression runner disk check failed: actual=${available_disk_bytes} available bytes, required=${MINIMUM_AVAILABLE_DISK_BYTES} available bytes." >&2
  exit 1
fi

echo "Compression runner RAM check passed: actual=${memory_bytes} bytes, required=${MINIMUM_MEMORY_BYTES} bytes."
echo "Compression runner disk check passed: actual=${available_disk_bytes} available bytes, required=${MINIMUM_AVAILABLE_DISK_BYTES} available bytes."
