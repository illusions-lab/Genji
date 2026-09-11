#!/usr/bin/env node
/** Verify Node's streaming decompressor against a known uncompressed hash. */
import { createHash } from "node:crypto";
import { createReadStream } from "node:fs";
import { createGunzip, createZstdDecompress } from "node:zlib";
import { Writable } from "node:stream";
import { pipeline } from "node:stream/promises";

const [format, asset, expectedHash] = process.argv.slice(2);
if (!['gzip', 'zstd'].includes(format) || !asset || !expectedHash) {
  throw new Error('Usage: verify_decompression.mjs <gzip|zstd> <asset> <sha256>');
}

const hash = createHash('sha256');
const decompressor = format === 'gzip' ? createGunzip() : createZstdDecompress();
await pipeline(
  createReadStream(asset),
  decompressor,
  new Writable({ write(chunk, _encoding, callback) { hash.update(chunk); callback(); } }),
);
const actualHash = hash.digest('hex');
if (actualHash !== expectedHash) {
  throw new Error(`${format} decompression hash mismatch: ${actualHash}`);
}
console.log(`${format} Node streaming decompression verified`);
