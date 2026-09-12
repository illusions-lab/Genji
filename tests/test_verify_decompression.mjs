import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdtemp, readFile, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { pipeline } from "node:stream/promises";
import { createReadStream, createWriteStream } from "node:fs";
import { createGzip, createZstdCompress } from "node:zlib";
import test from "node:test";

const ROOT = path.resolve(import.meta.dirname, "..");
const VERIFY = path.join(ROOT, "script", "verify_decompression.mjs");
const fixture = Buffer.from("Genji Node 24 streaming decompression fixture\n".repeat(128));
const fixtureHash = createHash("sha256").update(fixture).digest("hex");

async function compressedFixture(format) {
  const directory = await mkdtemp(path.join(os.tmpdir(), "genji-release-assets-"));
  const source = path.join(directory, "genji.db");
  const asset = `${source}.${format === "gzip" ? "gz" : "zst"}`;
  await writeFile(source, fixture);
  await pipeline(
    createReadStream(source),
    format === "gzip" ? createGzip() : createZstdCompress(),
    createWriteStream(asset),
  );
  return asset;
}

for (const format of ["gzip", "zstd"]) {
  test(`${format} streaming verifier accepts the expected hash`, async () => {
    const asset = await compressedFixture(format);
    const result = spawnSync(process.execPath, [VERIFY, format, asset, fixtureHash], {
      encoding: "utf8",
    });
    assert.equal(result.status, 0, result.stderr);
  });

  test(`${format} streaming verifier rejects the wrong hash`, async () => {
    const asset = await compressedFixture(format);
    const result = spawnSync(process.execPath, [VERIFY, format, asset, "0".repeat(64)], {
      encoding: "utf8",
    });
    assert.notEqual(result.status, 0);
    assert.match(result.stderr, /hash mismatch/);
  });

  test(`${format} streaming verifier rejects a corrupt stream`, async () => {
    const asset = await compressedFixture(format);
    const contents = await readFile(asset);
    await writeFile(asset, contents.subarray(0, Math.max(1, contents.length - 8)));
    const result = spawnSync(process.execPath, [VERIFY, format, asset, fixtureHash], {
      encoding: "utf8",
    });
    assert.notEqual(result.status, 0);
  });
}
