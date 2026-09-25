import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { mkdtemp, mkdir, readFile, rm, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { syncWebtorch } from '../deploy/laya-service/sync-webtorch.mjs';

const required = [
  'LICENSE', 'NOTICE', 'webtorch/__init__.py', 'webtorch/_core.py',
  'webtorch/decision.py', 'webtorch/js/webtorch-main.js',
  'webtorch/js/webtorch-host.js', 'webtorch/js/decision-vision.js',
  'webtorch/js/decision-vision-worker.js', 'webtorch/js/decision-vision-runtime.js',
  'webtorch/modules.json',
  'dist/wgpy-main.js', 'dist/wgpy-worker.js',
  'dist/wgpy_webgpu-1.0.0-py3-none-any.whl',
  'dist/wgpy_webgl-1.0.0-py3-none-any.whl',
];
const paths = [...required, ...Array.from({ length: 18 }, (_, index) => `webtorch/file${index}.py`)];
const hash = (bytes) => createHash('sha1').update(`blob ${bytes.length}\0`).update(bytes).digest('hex');

function fixture(commit, corrupt = '') {
  const files = new Map(paths.map((path) => [path, Buffer.from(`${commit}:${path}`)]));
  const tree = paths.map((path) => ({ path, type: 'blob', sha: hash(files.get(path)) }));
  const bySha = new Map(tree.map((entry) => [entry.sha, entry.path]));
  const download = async (url) => {
    if (url.endsWith('/commits/main')) return Buffer.from(JSON.stringify({ sha: commit }));
    if (url.includes('/git/trees/')) return Buffer.from(JSON.stringify({ tree, truncated: false }));
    const sha = url.split('/git/blobs/')[1];
    const path = bySha.get(sha);
    if (!files.has(path)) throw Error(`Unexpected download: ${url}`);
    const bytes = path === corrupt ? Buffer.from('tampered') : files.get(path);
    return Buffer.from(JSON.stringify({ sha, size: bytes.length, encoding: 'base64',
      content: bytes.toString('base64') }));
  };
  return download;
}

const root = await mkdtemp(join(tmpdir(), 'webtorch-sync-test-'));
const target = join(root, 'vendor', 'webtorch');
const model = join(root, 'models', 'laya', 'model.safetensors');
try {
  await mkdir(join(root, 'models', 'laya'), { recursive: true });
  await writeFile(model, 'private-local-model');
  const first = 'a'.repeat(40);
  const second = 'b'.repeat(40);
  assert.equal((await syncWebtorch({ target, download: fixture(first), log() {} })).changed, true);
  assert.equal((await readFile(join(target, 'UPSTREAM_SHA'), 'utf8')).trim(), first);
  assert.equal((await syncWebtorch({ target, download: fixture(first), log() {} })).changed, false);
  await assert.rejects(syncWebtorch({ target, download: fixture(second, 'webtorch/_core.py'), log() {} }), /hash mismatch/);
  assert.equal((await readFile(join(target, 'UPSTREAM_SHA'), 'utf8')).trim(), first);
  assert.equal((await syncWebtorch({ target, download: fixture(second), log() {} })).changed, true);
  assert.equal((await readFile(join(target, 'UPSTREAM_SHA'), 'utf8')).trim(), second);
  assert.equal((await readFile(model, 'utf8')), 'private-local-model');
  console.log('webtorch sync: update, no-op, integrity rejection, and model preservation passed');
} finally {
  await rm(root, { recursive: true, force: true });
}
