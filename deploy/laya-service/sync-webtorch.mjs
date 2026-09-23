/* Keep the bundled SDK at the GitHub repository's main branch. The webtorch package, compiled
 * browser runtime and license files are used; models, browser profiles and local credentials are
 * outside this directory. Every downloaded blob is checked against Git's tree hash before a
 * complete staged copy replaces the vendor directory. */
import { spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import { existsSync } from 'node:fs';
import { mkdir, mkdtemp, readFile, rename, rm, writeFile } from 'node:fs/promises';
import { dirname, join, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

const REPOSITORY = 'xnetsc/webpytorch';
const ROOT = dirname(fileURLToPath(import.meta.url));
const DEFAULT_TARGET = join(ROOT, 'vendor', 'webtorch');
const REQUIRED = [
  'LICENSE', 'NOTICE', 'webtorch/__init__.py', 'webtorch/_core.py',
  'webtorch/decision.py', 'webtorch/js/webtorch-main.js',
  'webtorch/js/webtorch-host.js', 'webtorch/modules.json',
  'dist/wgpy-main.js', 'dist/wgpy-worker.js',
  'dist/wgpy_webgpu-1.0.0-py3-none-any.whl',
  'dist/wgpy_webgl-1.0.0-py3-none-any.whl',
];

function curl(url) {
  return new Promise((done, fail) => {
    const child = spawn('curl', [
      '--fail', '--location', '--silent', '--show-error', '--retry', '2',
      '--connect-timeout', '10', '--max-time', '90',
      '--header', 'Accept: application/vnd.github+json', url,
    ], { stdio: ['ignore', 'pipe', 'pipe'] });
    const chunks = [];
    const errors = [];
    child.stdout.on('data', (chunk) => chunks.push(chunk));
    child.stderr.on('data', (chunk) => errors.push(chunk));
    child.on('error', fail);
    child.on('close', (code) => code === 0
      ? done(Buffer.concat(chunks))
      : fail(new Error(`GitHub download failed (${code}): ${Buffer.concat(errors).toString('utf8').trim()}`)));
  });
}

function gitBlobHash(bytes) {
  return createHash('sha1').update(`blob ${bytes.length}\0`).update(bytes).digest('hex');
}

function selected(path) {
  return path === 'LICENSE' || path === 'NOTICE' || path.startsWith('webtorch/') || path.startsWith('dist/');
}

function safePath(path) {
  return path && !path.startsWith('/') && !path.split('/').some((part) => !part || part === '.' || part === '..');
}

async function remoteIndex(download = curl) {
  const commit = JSON.parse((await download(`https://api.github.com/repos/${REPOSITORY}/commits/main`)).toString('utf8')).sha;
  if (!/^[a-f0-9]{40}$/.test(String(commit))) throw new Error('GitHub returned no valid upstream commit');
  const tree = JSON.parse((await download(`https://api.github.com/repos/${REPOSITORY}/git/trees/${commit}?recursive=1`)).toString('utf8'));
  if (tree.truncated) throw new Error('GitHub returned a truncated SDK file tree');
  const files = (tree.tree || []).filter((entry) => entry.type === 'blob' && selected(entry.path));
  if (files.length < 25 || REQUIRED.some((name) => !files.some((file) => file.path === name))) {
    throw new Error('Upstream SDK is missing files required by the laya service');
  }
  if (files.some((file) => !safePath(file.path) || !/^[a-f0-9]{40}$/.test(String(file.sha)))) {
    throw new Error('Upstream SDK file tree contains an invalid path or blob hash');
  }
  return { commit, files };
}

export async function syncWebtorch({ target = DEFAULT_TARGET, download = curl, log = console.log, keepPrevious = false } = {}) {
  target = resolve(target);
  const { commit, files } = await remoteIndex(download);
  const marker = join(target, 'UPSTREAM_SHA');
  const current = existsSync(marker) ? (await readFile(marker, 'utf8')).trim() : '';
  if (current === commit && REQUIRED.every((name) => existsSync(join(target, name)))) {
    log(`webtorch SDK current: ${commit.slice(0, 12)}`);
    return { changed: false, commit, files: files.length };
  }
  await mkdir(dirname(target), { recursive: true });
  const stage = await mkdtemp(join(dirname(target), '.webtorch-stage-'));
  let old = '';
  try {
    // Concurrent downloads reduce startup time, while keeping GitHub requests bounded.
    let next = 0;
    const downloads = await Promise.allSettled(Array.from({ length: Math.min(6, files.length) }, async () => {
      for (;;) {
        const entry = files[next++];
        if (!entry) break;
        const bytes = await download(`https://raw.githubusercontent.com/${REPOSITORY}/${commit}/${entry.path}`);
        if (gitBlobHash(bytes) !== entry.sha) throw new Error(`Upstream blob hash mismatch: ${entry.path}`);
        const destination = join(stage, entry.path);
        if (!resolve(destination).startsWith(stage + sep)) throw new Error(`Unsafe SDK path: ${entry.path}`);
        await mkdir(dirname(destination), { recursive: true });
        await writeFile(destination, bytes);
      }
    }));
    const failed = downloads.find((result) => result.status === 'rejected');
    if (failed) throw failed.reason;
    await writeFile(join(stage, 'UPSTREAM_SHA'), commit + '\n');
    if (existsSync(target)) {
      old = join(dirname(target), `.webtorch-previous-${process.pid}-${Date.now()}`);
      await rename(target, old);
    }
    try { await rename(stage, target); }
    catch (error) {
      if (old) await rename(old, target);
      old = '';
      throw error;
    }
    if (old && !keepPrevious) await rm(old, { recursive: true, force: true });
    log(`webtorch SDK updated: ${current.slice(0, 12) || 'unversioned'} -> ${commit.slice(0, 12)} (${files.length} files)`);
    return { changed: true, commit, files: files.length, previous: keepPrevious ? old : '' };
  } finally {
    if (existsSync(stage)) await rm(stage, { recursive: true, force: true });
  }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const index = process.argv.indexOf('--target');
  const target = index >= 0 ? process.argv[index + 1] : DEFAULT_TARGET;
  if (!target) throw new Error('--target needs a directory');
  try {
    const result = await syncWebtorch({ target });
    console.log(JSON.stringify(result));
  } catch (error) {
    console.error(`webtorch SDK sync failed: ${error.message || error}`);
    process.exitCode = 1;
  }
}
