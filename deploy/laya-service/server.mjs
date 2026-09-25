/* laya over HTTP, in the shape everything already speaks.
 *
 * The model runs in a browser because that is where WebGPU is: webtorch is CPython compiled to
 * WebAssembly with a WebGPU backend, and the alternative on a machine without one is numpy on the
 * CPU, which for an 800 MB encoder is not an alternative. So this holds a headless Chrome open on
 * one page, keeps the model loaded in it, and answers HTTP.
 *
 * The API is OpenRouter's chat-completions shape rather than one of its own, so that anything
 * already able to call a model can call this by changing a base URL - including the evaluator in
 * this repository, which sends `{"state": …, "questions": {…}}` as the user message and asks for a
 * json_schema back. A decision model is not a chat model, and the adapter is exactly that: the
 * request carries the state and the questions, the reply carries the answers, and nothing here
 * decides anything - no threshold is applied and no probability is turned into a verdict.
 *
 *   node server.mjs --webtorch /path/to/webtorch [--port 8899]
 */
import { createServer } from 'node:http';
import { mkdir, readFile, rename, rm, stat, writeFile } from 'node:fs/promises';
import { createReadStream, createWriteStream, existsSync } from 'node:fs';
import { once } from 'node:events';
import { createHash } from 'node:crypto';
import { dirname, extname, join, resolve, sep } from 'node:path';
import { spawn } from 'node:child_process';
import { chromium } from 'playwright';
import { syncWebtorch } from './sync-webtorch.mjs';
import { createGpuQueue } from './gpu-queue.mjs';
import { createBenchmark } from './benchmark.mjs';

/* Node's own fetch ignores HTTPS_PROXY unless it is told not to, and it is told with an environment
 * variable read before any of this runs. On a machine that reaches the model host through a proxy -
 * which is the machine this was written on - the first download otherwise fails with a connect
 * timeout that says nothing about proxies. Re-exec once, with the flag, rather than making every
 * operator remember it. */
if (!process.env.NODE_USE_ENV_PROXY && (process.env.HTTPS_PROXY || process.env.https_proxy)) {
  const { spawn } = await import('node:child_process');
  const again = spawn(process.execPath, process.argv.slice(1), {
    stdio: 'inherit',
    env: { ...process.env, NODE_USE_ENV_PROXY: '1' },
  });
  // Signals are forwarded and the exit is mirrored, so that this stays one process to whoever
  // started it. Without this, stopping the parent leaves the child holding the port, and the next
  // start fails with EADDRINUSE against a service nobody can see.
  for (const signal of ['SIGINT', 'SIGTERM', 'SIGHUP']) {
    process.on(signal, () => again.kill(signal));
  }
  again.on('exit', (code, signal) => process.exit(signal ? 1 : (code ?? 0)));
} else {

const argv = process.argv.slice(2);
const option = (name, fallback) => {
  const index = argv.indexOf('--' + name);
  return index >= 0 && argv[index + 1] ? argv[index + 1] : fallback;
};
const WEBTORCH = resolve(option(
  'webtorch', process.env.WEBTORCH_DIR || join(HERE_DIR(), 'vendor', 'webtorch'),
));
const PORT = Number(option('port', process.env.PORT || 8899));
const HOST = option('host', process.env.LAYA_HOST || '0.0.0.0');
const HEADLESS = option('headless', 'true') !== 'false';

/* The one model this service exists for. Not a parameter: the SDK's demo page can load anything,
 * and this is a service for laya. */
const MODEL = 'convaiinnovations/laya';
const MODELS_DIR = resolve(option('models', process.env.LAYA_MODELS || join(HERE_DIR(), 'models')));
const MODEL_DIR = join(MODELS_DIR, 'laya');
const VISION_MODEL = 'thaitea/laya-vision';
const VISION_WEB_MODEL = 'thaitea/laya-vision-web';
const VISION_MODEL_DIR = join(MODELS_DIR, 'laya-vision');
const ENDPOINT = option('endpoint', process.env.HF_ENDPOINT || 'https://huggingface.co');
const MODEL_URL = process.env.LAYA_MODEL_URL || '';

function HERE_DIR() { return resolve(new URL('.', import.meta.url).pathname); }

if (!existsSync(join(WEBTORCH, 'webtorch', 'js', 'webtorch-main.js'))) {
  console.error(`找不到 webtorch SDK：${WEBTORCH}\n资源包里应带有 vendor/webtorch，或用 --webtorch 指定检出目录`);
  process.exit(2);
}

const TYPES = {
  '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8', '.json': 'application/json; charset=utf-8',
  '.wasm': 'application/wasm', '.whl': 'application/octet-stream', '.py': 'text/plain; charset=utf-8',
  '.css': 'text/css; charset=utf-8', '.png': 'image/png', '.svg': 'image/svg+xml',
  '.data': 'application/octet-stream', '.zip': 'application/zip',
};

/* The GPU backend reaches the device from the worker over SharedArrayBuffer, which needs the page
 * cross-origin isolated. Because this server owns the responses it sends the two headers itself and
 * no service worker is needed - the SDK's `installServiceWorker` is for hosts that cannot. The
 * values are the ones the SDK documents and its own serve-coi.mjs sends; model files are fetched
 * from Hugging Face in CORS mode, which is what require-corp accepts. Without isolation nothing
 * fails: it quietly runs on the CPU, and seconds become minutes. */
const ISOLATION = {
  'Cross-Origin-Opener-Policy': 'same-origin',
  'Cross-Origin-Embedder-Policy': 'require-corp',
  'Cross-Origin-Resource-Policy': 'same-origin',
};

const HERE = HERE_DIR();

/* ------------------------------------------------------ the copy on this disk */

/** Files of the repo that a load actually reads; the rest of it is pictures and eval notes. */
const MODEL_FILES = [
  'model.safetensors', 'rl_agent_config.json', 'encoder/config.json',
  'tokenizer/tokenizer.json', 'tokenizer/tokenizer_config.json',
];

async function present(path) {
  try { return (await stat(path)).size > 0; } catch { return false; }
}

async function presentSize(path, bytes) {
  try { return (await stat(path)).size === Number(bytes); } catch { return false; }
}

async function verifiedFile(path, bytes, sha256) {
  if (!await presentSize(path, bytes) || !sha256) return false;
  const hash = createHash('sha256');
  for await (const chunk of createReadStream(path)) hash.update(chunk);
  return hash.digest('hex') === sha256;
}

function completeUrlSource(url, probe) {
  if (!url) return null;
  const target = new URL(url);
  const encoded = probe.split('/').map(encodeURIComponent).join('/');
  const base = target.pathname.endsWith('/' + encoded)
    ? target.href.slice(0, target.href.length - encoded.length)
    : target.href.replace(/\/?$/, '/');
  return { name: 'listed-url', base };
}

const MODEL_SOURCES = [
  completeUrlSource(MODEL_URL, 'model.safetensors'),
  { name: 'huggingface', base: `${ENDPOINT.replace(/\/$/, '')}/${MODEL}/resolve/main/` },
  { name: 'modelscope', base: `https://modelscope.cn/models/${MODEL}/resolve/master/` },
  { name: 'huggingface-mirror', base: `https://hf-mirror.com/${MODEL}/resolve/main/` },
].filter(Boolean).filter((source, index, all) =>
  all.findIndex((item) => item.base === source.base) === index);
let chosenModelSource = null;

async function probeModelSource(source) {
  const started = performance.now();
  const response = await fetch(source.base + 'model.safetensors', {
    signal: AbortSignal.timeout(15000), headers: { Range: 'bytes=0-1048575' },
  });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const reader = response.body.getReader();
  let bytes = 0;
  while (bytes < 1048576) {
    const { done, value } = await reader.read();
    if (done) break;
    bytes += Math.min(value.length, 1048576 - bytes);
  }
  try { await reader.cancel(); } catch { /* response already ended */ }
  if (!bytes) throw new Error('empty response');
  const elapsed = Math.max(0.001, (performance.now() - started) / 1000);
  return { ...source, sampleBytesPerSecond: bytes / elapsed };
}

async function chooseModelSource(log = console.log) {
  if (chosenModelSource) return chosenModelSource;
  const available = [];
  const direct = MODEL_SOURCES.find((source) => source.name === 'listed-url');
  if (direct) {
    try { available.push(await probeModelSource(direct)); }
    catch { /* hubs remain eligible; the URL is only a fallback when it works */ }
  }
  const hubs = MODEL_SOURCES.filter((source) => source !== direct);
  const results = await Promise.allSettled(hubs.map(probeModelSource));
  available.push(...results.filter((item) => item.status === 'fulfilled').map((item) => item.value));
  available.sort((a, b) => b.sampleBytesPerSecond - a.sampleBytesPerSecond);
  if (!available.length) {
    const detail = results.map((item, index) =>
      `${hubs[index].name}: ${sourceFailure(item.reason)}`).join(' | ');
    throw new Error(`no Laya model source is reachable (${detail})`);
  }
  chosenModelSource = available[0];
  log(`Laya source: ${chosenModelSource.name} (${(chosenModelSource.sampleBytesPerSecond / 1048576).toFixed(1)} MB/s sample)`);
  return chosenModelSource;
}

/** Fetch the model once, to disk, so that every boot after this one reads from here.
 *
 * The browser could do it - a hub reader plus `default_io_write` keeps the weights in origin
 * storage - but then the copy lives inside a profile nobody can see, it is refetched whenever that
 * profile is cleared, and moving this service to another machine means downloading again. On disk
 * it is a directory you can copy, mount, or ship in an image. */
async function ensureLocalModel(log = console.log) {
  const marker = join(MODEL_DIR, 'model.safetensors');
  if (await present(marker)) return true;
  log(`fetching ${MODEL} to ${MODEL_DIR} (once)`);
  const source = await chooseModelSource(log);
  for (const name of MODEL_FILES) {
    const target = join(MODEL_DIR, name);
    if (await present(target)) continue;
    const response = await fetch(source.base + name);
    if (!response.ok) throw new Error(`${name}: HTTP ${response.status}`);
    await mkdir(dirname(target), { recursive: true });
    // Streamed to disk rather than read into memory: the weights are 800 MB, and a download with
    // no progress is one nobody can tell from a hang - which is exactly how the first attempt
    // looked. Written beside and renamed, so an interruption cannot leave a short file that the
    // next boot mistakes for a complete one.
    const total = Number(response.headers.get('content-length') || 0);
    const handle = createWriteStream(target + '.part');
    let read = 0;
    let announced = 0;
    for await (const chunk of response.body) {
      read += chunk.length;
      if (!handle.write(chunk)) await once(handle, 'drain');
      if (total > 20 * 1048576 && read - announced >= 50 * 1048576) {
        announced = read;
        log(`  ${name} ${(read / 1048576).toFixed(0)} / ${(total / 1048576).toFixed(0)} MB`);
      }
    }
    await new Promise((done, fail) => handle.end((error) => (error ? fail(error) : done())));
    await rename(target + '.part', target);
    log(`  ${name} ${(read / 1048576).toFixed(1)} MB`);
  }
  return true;
}

const VISION_ONNX_FILES = ['vision_fp16.onnx', 'text_fp16.onnx', 'head_fp16.onnx'];
const VISION_RELEASE = 'https://github.com/xnetsc/webpytorch/releases/download/laya-vision-web-201m-v1/';
const VISION_SOURCES = [
  { name: 'github-release', base: VISION_RELEASE },
  { name: 'huggingface-mirror', base: `https://hf-mirror.com/${VISION_WEB_MODEL}/resolve/main/` },
  { name: 'huggingface', base: `${ENDPOINT.replace(/\/$/, '')}/${VISION_WEB_MODEL}/resolve/main/` },
  { name: 'modelscope', base: `https://modelscope.cn/models/${VISION_WEB_MODEL}/resolve/master/` },
].filter((source, index, all) => all.findIndex((item) => item.base === source.base) === index);
let visionDownload = null;
let chosenVisionSource = null;
let visionSourceName = null;

function visionTokenizerFiles(manifest) {
  const files = manifest.tokenizer;
  if (!Array.isArray(files) || files.length !== 2) throw new Error('manifest has no tokenizer files');
  for (const name of files) {
    if (typeof name !== 'string' || name.startsWith('/') || name.split('/').includes('..')) {
      throw new Error('manifest contains an unsafe tokenizer path');
    }
  }
  if (!files.some((name) => name.endsWith('tokenizer.json')) ||
      !files.some((name) => name.endsWith('tokenizer_config.json'))) {
    throw new Error('manifest has unexpected tokenizer files');
  }
  return files;
}

function sourceFailure(error) {
  const message = String(error?.message || error);
  if (/HTTP 401|HTTP 403/.test(message)) return 'requires authorization';
  if (/HTTP 404/.test(message)) return 'has no published browser export';
  if (error?.name === 'TimeoutError' || error?.name === 'AbortError') return 'timed out';
  return message;
}

async function probeVisionSource(source) {
  const started = performance.now();
  const response = await fetch(source.base + 'laya_web.json', {
    signal: AbortSignal.timeout(15000), headers: { Accept: 'application/json' },
  });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const bytes = new Uint8Array(await response.arrayBuffer());
  const manifest = JSON.parse(new TextDecoder().decode(bytes));
  if (manifest.format_version !== 1 || manifest.source !== VISION_MODEL ||
      !VISION_ONNX_FILES.every((name) => manifest.files?.[name]?.bytes &&
        manifest.files?.[name]?.sha256)) {
    throw new Error('not the expected vision export');
  }
  visionTokenizerFiles(manifest);
  const latency = performance.now() - started;
  let sampled = 0, sampleBytesPerSecond = 0;
  const sampleStarted = performance.now();
  try {
    const sample = await fetch(source.base + 'text_fp16.onnx', {
      signal: AbortSignal.timeout(12000), headers: { Range: 'bytes=0-1048575' },
    });
    if (!sample.ok) throw new Error(`sample HTTP ${sample.status}`);
    const reader = sample.body.getReader();
    while (sampled < 1048576) {
      const { done, value } = await reader.read();
      if (done) break;
      sampled += value.length;
    }
    await reader.cancel();
    sampleBytesPerSecond = sampled /
      Math.max(0.001, (performance.now() - sampleStarted) / 1000);
  } catch { /* a valid manifest remains usable when a range speed sample is blocked */ }
  return { ...source, manifest, manifestBytes: bytes, latency,
    sampleBytesPerSecond };
}

async function chooseVisionSource(log = console.log) {
  if (chosenVisionSource) return chosenVisionSource;
  const available = [];
  const direct = VISION_SOURCES.find((source) => source.name === 'github-release');
  if (direct) {
    try { available.push(await probeVisionSource(direct)); }
    catch { /* the hubs may still carry it */ }
  }
  const hubs = VISION_SOURCES.filter((source) => source !== direct);
  const results = await Promise.allSettled(hubs.map(probeVisionSource));
  available.push(...results.filter((item) => item.status === 'fulfilled').map((item) => item.value));
  if (!available.length) {
    const detail = results.map((item, index) =>
      `${hubs[index].name}: ${sourceFailure(item.reason)}`).join(' | ');
    throw new Error(`no Laya Vision source is reachable (${detail})`);
  }
  available.sort((a, b) => b.sampleBytesPerSecond - a.sampleBytesPerSecond || a.latency - b.latency);
  chosenVisionSource = available[0];
  visionSourceName = chosenVisionSource.name;
  log(`Laya Vision source: ${chosenVisionSource.name} (${(chosenVisionSource.sampleBytesPerSecond / 1048576).toFixed(1)} MB/s sample)`);
  return chosenVisionSource;
}

async function downloadVerified(url, target, expected, sha256, log = console.log) {
  if (await verifiedFile(target, expected, sha256)) return;
  const response = await fetch(url);
  if (!response.ok) throw new Error(`${url}: HTTP ${response.status}`);
  await mkdir(dirname(target), { recursive: true });
  const handle = createWriteStream(target + '.part');
  const hash = createHash('sha256');
  let read = 0;
  let announced = 0;
  for await (const chunk of response.body) {
    read += chunk.length; hash.update(chunk);
    if (!handle.write(chunk)) await once(handle, 'drain');
    if (expected > 20 * 1048576 && read - announced >= 50 * 1048576) {
      announced = read;
      log(`  ${target.slice(VISION_MODEL_DIR.length + 1)} ${(read / 1048576).toFixed(0)} / ${(expected / 1048576).toFixed(0)} MB`);
    }
  }
  await new Promise((done, fail) => handle.end((error) => (error ? fail(error) : done())));
  if (read !== Number(expected) || hash.digest('hex') !== sha256) {
    throw new Error(`${target}: downloaded bytes or SHA-256 do not match laya_web.json`);
  }
  await rename(target + '.part', target);
}

async function downloadSmallFile(url, target) {
  if (await present(target)) return;
  const response = await fetch(url);
  if (!response.ok) throw new Error(`${url}: HTTP ${response.status}`);
  const bytes = new Uint8Array(await response.arrayBuffer());
  if (!bytes.length) throw new Error(`${url}: empty response`);
  await mkdir(dirname(target), { recursive: true });
  await writeFile(target + '.part', bytes);
  await rename(target + '.part', target);
}

async function ensureLocalVisionModel(log = console.log) {
  if (visionDownload) return visionDownload;
  visionDownload = (async () => {
    try {
      const manifest = JSON.parse(await readFile(join(VISION_MODEL_DIR, 'laya_web.json'), 'utf8'));
      const complete = manifest.format_version === 1 && manifest.source === VISION_MODEL &&
        (await Promise.all(VISION_ONNX_FILES.map((name) =>
          verifiedFile(join(VISION_MODEL_DIR, name), manifest.files?.[name]?.bytes,
            manifest.files?.[name]?.sha256)))).every(Boolean) &&
        (await Promise.all(visionTokenizerFiles(manifest).map((name) =>
          present(join(VISION_MODEL_DIR, name))))).every(Boolean);
      if (complete) {
        visionSourceName = 'local';
        return { source: 'local', model: VISION_MODEL };
      }
    } catch { /* no complete local vision export yet */ }
    const source = await chooseVisionSource(log);
    const manifest = source.manifest;
    await mkdir(VISION_MODEL_DIR, { recursive: true });
    for (const name of VISION_ONNX_FILES) {
      const expected = manifest.files?.[name];
      if (!expected?.bytes || !expected?.sha256) throw new Error(`${name} is missing from laya_web.json`);
      await downloadVerified(source.base + name, join(VISION_MODEL_DIR, name),
        expected.bytes, expected.sha256, log);
    }
    for (const name of visionTokenizerFiles(manifest)) {
      await downloadSmallFile(source.base + name, join(VISION_MODEL_DIR, name));
    }
    await writeFile(join(VISION_MODEL_DIR, 'laya_web.json'), source.manifestBytes);
    return { source: source.name, model: VISION_MODEL };
  })().catch((error) => { visionDownload = null; throw error; });
  return visionDownload;
}

async function serveStatic(request, response, url) {
  // `/laya/...` is this service's own page, `/models/...` the weights it keeps on disk, and
  // everything else the webtorch checkout, mounted at the root so the page's relative paths are
  // the ones the SDK's own documentation uses.
  const mount = url.pathname.startsWith('/laya/')
    ? [HERE, url.pathname.slice('/laya/'.length)]
    : url.pathname.startsWith('/models/laya-vision/')
      ? [VISION_MODEL_DIR, url.pathname.slice('/models/laya-vision/'.length)]
    : url.pathname.startsWith('/models/')
      ? [MODEL_DIR, url.pathname.slice('/models/laya/'.length)]
      : [WEBTORCH, url.pathname === '/' ? 'index.html' : url.pathname.slice(1)];
  const [root, relative] = mount;
  const path = join(root, relative);
  if (!resolve(path).startsWith(root + sep) && resolve(path) !== root) {
    response.writeHead(403).end('forbidden');
    return;
  }
  try {
    const info = await stat(path);
    if (info.isDirectory()) throw new Error('directory');
    const type = TYPES[extname(path)] || 'application/octet-stream';
    // Ranged reads are how this SDK works, not an optimisation. It decides what a checkpoint IS by
    // reading the first few bytes of its safetensors index, and it streams multi-hundred-megabyte
    // shards the same way. A server that answers every range with the whole file breaks the first
    // of those silently: the decision model was not recognised, so it was loaded as a language
    // model and failed on a tensor it never had.
    const range = /^bytes=(\d*)-(\d*)$/.exec(String(request.headers.range || ''));
    if (range) {
      const size = info.size;
      const start = range[1] ? Number(range[1]) : Math.max(0, size - Number(range[2] || 0));
      const end = range[1] ? (range[2] ? Math.min(Number(range[2]), size - 1) : size - 1) : size - 1;
      if (!(start >= 0 && start <= end && end < size)) {
        response.writeHead(416, { 'Content-Range': `bytes */${size}`, ...ISOLATION }).end();
        return;
      }
      response.writeHead(206, {
        'Content-Type': type,
        'Content-Length': end - start + 1,
        'Content-Range': `bytes ${start}-${end}/${size}`,
        'Accept-Ranges': 'bytes',
        ...ISOLATION,
      });
      createReadStream(path, { start, end }).pipe(response);
      return;
    }
    const body = await readFile(path);
    response.writeHead(200, {
      'Content-Type': type,
      'Content-Length': body.length,
      'Accept-Ranges': 'bytes',
      ...ISOLATION,
    }).end(body);
  } catch {
    // Logged, not swallowed: a load that fails inside Pyodide reports "HTTP 404" with no URL, and
    // without this line there is no way to tell which file it went looking for.
    console.log('[404]', url.pathname);
    response.writeHead(404, ISOLATION).end('not found');
  }
}

/* ---------------------------------------------------------------- the browser */

let page = null;
let booted = null;
const exclusive = createGpuQueue();
const benchmark = createBenchmark({
  exclusive,
  sample: async (state, questions) => {
    await browser();
    const result = await page.evaluate(
      (payload) => window.__laya.decide(payload), { state, questions },
    );
    shaped(result.answers, questions);
    return { usage: result.usage || {} };
  },
});
const SDK_CHECK_MS = 6 * 60 * 60 * 1000;

async function refreshSdk() {
  const result = await syncWebtorch({ target: WEBTORCH, keepPrevious: true });
  if (!result.changed) return;
  try {
    if (booted) await (await booted).close();
    page = null;
    booted = null;
    await ensureLocalModel();
    await browser();
    if (result.previous) await rm(result.previous, { recursive: true, force: true });
  } catch (error) {
    if (result.previous) {
      if (booted) await booted.then((instance) => instance.close(), () => {});
      page = null;
      booted = null;
      await rm(WEBTORCH, { recursive: true, force: true });
      await rename(result.previous, WEBTORCH);
      await browser();
    }
    throw new Error(`新版 webtorch SDK 未能启动，已恢复上一版：${error.message || error}`);
  }
}

/* Launch options that matter: Playwright turns the GPU off by default, which is the one thing this
 * service needs on, and WebGPU is only exposed to a secure context - which the loopback origin in
 * front of this page is. */
const LAUNCH = {
  headless: HEADLESS,
  ignoreDefaultArgs: ['--disable-gpu'],
  args: ['--enable-unsafe-webgpu', '--use-angle=metal', '--enable-features=Vulkan'],
};

function run(command, args) {
  return new Promise((done, fail) => {
    const child = spawn(command, args, { stdio: 'inherit' });
    child.on('error', fail);
    child.on('exit', (code) => (code === 0 ? done() : fail(new Error(`${command} exited ${code}`))));
  });
}

/** A browser to run the model in, installing one if this machine has none.
 *
 * The service is useless without it, and "install a browser" is a worse thing to put in a README
 * than to do: on a fresh GPU-equipped host, the downloaded bundle should be self-contained. The
 * installed Chromium ships with Playwright and supports WebGPU the
 * same way; the system Chrome is tried first only because it is already there. */
async function ensureBrowser(log = console.log) {
  const attempts = [
    ['系统 Chrome', { ...LAUNCH, channel: 'chrome' }],
    ['Playwright 自带的 Chromium', { ...LAUNCH }],
  ];
  const failures = [];
  for (const [label, options] of attempts) {
    try {
      const instance = await chromium.launch(options);
      log(`浏览器：${label}`);
      return instance;
    } catch (error) {
      failures.push(`${label}: ${String((error && error.message) || error).split('\n')[0]}`);
    }
  }
  log('没有可用的无头浏览器，正在安装 Chromium…');
  await run(process.execPath, [
    resolve(HERE, 'node_modules', 'playwright', 'cli.js'), 'install', 'chromium', '--with-deps',
  ]).catch(() => run('npx', ['--yes', 'playwright', 'install', 'chromium']));
  try {
    const instance = await chromium.launch(LAUNCH);
    log('浏览器：刚安装的 Chromium');
    return instance;
  } catch (error) {
    throw new Error(`装完仍然起不来：${(error && error.message) || error}\n之前的尝试：${failures.join(' | ')}`);
  }
}

async function browser() {
  if (booted) return booted;
  booted = (async () => {
    const instance = await ensureBrowser();
    page = await instance.newPage();
    page.on('console', (message) => console.log('[page]', message.text()));
    await page.goto(`http://127.0.0.1:${PORT}/laya/page.html?local=${encodeURIComponent('/models/laya/')}`);
    await page.waitForFunction(
      () => window.__laya && (window.__laya.status().ready || window.__laya.status().error),
      null,
      { timeout: 20 * 60 * 1000 },
    );
    const status = await page.evaluate(() => window.__laya.status());
    if (!status.ready) throw new Error(status.error || 'the page never became ready');
    console.log(`laya ready: ${status.model} on ${status.backend}`);
    return instance;
  })();
  return booted;
}

/* ------------------------------------------------------- the OpenRouter shape */

const QUESTION_TYPES = new Set(['choice', 'score', 'noul']);

function hasVisionState(state) {
  if (!state || typeof state !== 'object' || Array.isArray(state)) return false;
  if (state.type === 'image') return Boolean(state.data);
  if (state.type === 'multimodal') return Boolean(state.image) ||
    (Array.isArray(state.images) && state.images.length > 0);
  return Boolean(state.image) || (Array.isArray(state.images) && state.images.length > 0);
}

const observedLatency = { text: [], vision: [] };
function recordLatency(mode, milliseconds, usage) {
  const rows = observedLatency[mode];
  rows.push({ milliseconds, at: Date.now() / 1000, usage });
  if (rows.length > 20) rows.shift();
}
function latencySnapshot() {
  return Object.fromEntries(Object.entries(observedLatency).map(([mode, rows]) => {
    const times = rows.map((item) => item.milliseconds).sort((a, b) => a - b);
    const last = rows[rows.length - 1];
    return [mode, { samples: rows.length,
      median_ms: times.length ? times[Math.floor(times.length / 2)] : null,
      last_ms: last?.milliseconds ?? null, measured_at: last?.at ?? null,
      usage: last?.usage || {} }];
  }));
}

/** Pull `{state, questions}` out of a chat request: it is what the caller put in the last message. */
function requested(body) {
  const messages = Array.isArray(body.messages) ? body.messages : [];
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const content = messages[index] && messages[index].content;
    const text = typeof content === 'string'
      ? content
      : Array.isArray(content) ? content.map((part) => part && part.text).filter(Boolean).join('') : '';
    try {
      const parsed = JSON.parse(text);
      if (parsed && typeof parsed === 'object' && parsed.questions) return parsed;
    } catch { /* not this message */ }
  }
  throw new Error('no {"state": …, "questions": {…}} object in the messages');
}

/** The model answers in its own terms; the caller's schema wants the question type alongside. */
function shaped(answers, questions) {
  const out = {};
  for (const [name, question] of Object.entries(questions)) {
    const answer = (answers && answers[name]) || {};
    const type = String(question && question.type);
    if (!QUESTION_TYPES.has(type)) throw new Error(`unsupported question type: ${type}`);
    const confidence = answer.confidence === undefined || answer.confidence === null
      ? null : Number(answer.confidence);
    if (type === 'choice') {
      out[name] = {
        type, choice: String(answer.choice ?? ''), confidence,
        probabilities: answer.probabilities || {},
      };
    } else if (type === 'score') {
      out[name] = { type, confidence, score: Number(answer.score ?? 0) };
    } else {
      out[name] = { type, confidence, noul: Number(answer.noul ?? 0) };
    }
  }
  return out;
}

async function completions(request, response, body, asked, mode) {
  await browser();
  const started = Date.now();
  const result = await page.evaluate(
    (payload) => window.__laya.decide(payload),
    { state: asked.state ?? {}, questions: asked.questions },
  );
  const content = JSON.stringify({ answers: shaped(result.answers, asked.questions) });
  const usage = result.usage || {};
  const promptTokens = Number(usage.input_tokens ?? usage.prompt_tokens ?? usage.tokens ?? 0);
  const latency = Date.now() - started;
  recordLatency(mode, latency, usage);
  response.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8' }).end(JSON.stringify({
    id: 'laya-' + Date.now().toString(36),
    object: 'chat.completion',
    created: Math.floor(Date.now() / 1000),
    model: mode === 'vision' ? VISION_MODEL : MODEL,
    choices: [{ index: 0, message: { role: 'assistant', content }, finish_reason: 'stop' }],
    usage: {
      prompt_tokens: promptTokens,
      completion_tokens: 0,
      total_tokens: promptTokens,
      questions: Number(usage.questions ?? 0),
      sequence_tokens: usage.sequence_tokens || {},
      encoder_tokens: Number(usage.encoder_tokens ?? promptTokens),
      encoder_passes: Number(usage.encoder_passes ?? 0),
      batched: Boolean(usage.batched),
      images: Number(usage.images ?? 0),
      vision_cache_hits: Number(usage.vision_cache_hits ?? 0),
      vision_cache_misses: Number(usage.vision_cache_misses ?? 0),
    },
    // Free, local, and honest about it: nothing was billed because nothing left the machine.
    cost: 0,
    latency_ms: latency,
  }));
}

function refuseDuringBenchmark(response) {
  const status = benchmark.snapshot();
  if (!status.active) return false;
  response.writeHead(429, {
    'Content-Type': 'application/json', 'Retry-After': '1',
  }).end(JSON.stringify({
    error: { code: 'benchmark_in_progress', type: 'laya_service',
      message: `Laya benchmark is ${status.status}; inspect /health before retrying` },
    benchmark: status,
  }));
  return true;
}

const server = createServer(async (request, response) => {
  const url = new URL(request.url, `http://127.0.0.1:${PORT}`);
  if (process.env.LAYA_TRACE) console.log('[req]', request.method, url.pathname);
  try {
    if (request.method === 'GET' && url.pathname === '/health') {
      const status = page ? await page.evaluate(() => window.__laya.status()) : { ready: false };
      response.writeHead(status.ready ? 200 : 503, { 'Content-Type': 'application/json' })
        .end(JSON.stringify({ ...status, benchmark: benchmark.snapshot(),
          latency_by_state: latencySnapshot(),
          vision_source: visionSourceName }));
      return;
    }
    if (request.method === 'POST' && url.pathname === '/benchmark') {
      const result = benchmark.trigger();
      response.writeHead(result.accepted ? 202 : 200, { 'Content-Type': 'application/json' })
        .end(JSON.stringify(result));
      return;
    }
    if (request.method === 'GET' && /\/v1\/models$/.test(url.pathname)) {
      response.writeHead(200, { 'Content-Type': 'application/json' }).end(JSON.stringify({
        data: [{
          id: MODEL, name: MODEL, object: 'model',
          description: 'Typed text decisions on local WebGPU; image states route to Laya Vision.',
          supported_parameters: ['response_format', 'structured_outputs'],
          input_modalities: ['text', 'image'],
          pricing: { prompt: '0', completion: '0' },
        }],
      }));
      return;
    }
    if (request.method === 'POST' && /chat\/completions$/.test(url.pathname)) {
      if (refuseDuringBenchmark(response)) return;
      const chunks = [];
      let bytes = 0;
      for await (const chunk of request) {
        bytes += chunk.length;
        if (bytes > 24 * 1024 * 1024) throw new Error('request exceeds 24 MB');
        chunks.push(chunk);
      }
      const body = JSON.parse(Buffer.concat(chunks).toString('utf8') || '{}');
      const asked = requested(body);
      const mode = hasVisionState(asked.state) ? 'vision' : 'text';
      // Reading the body yields to the event loop, so a benchmark may have started meanwhile.
      if (refuseDuringBenchmark(response)) return;
      if (mode === 'vision') await ensureLocalVisionModel();
      await exclusive(() => completions(request, response, body, asked, mode), { lane: mode });
      return;
    }
    await serveStatic(request, response, url);
  } catch (error) {
    // The caller gets the reason in the shape its client already knows how to read.
    const queueFull = error && error.code === 'GPU_QUEUE_FULL';
    response.writeHead(queueFull ? 429 : 500, {
      'Content-Type': 'application/json',
      ...(queueFull ? { 'Retry-After': '1' } : {}),
    }).end(JSON.stringify({
      error: { message: String((error && error.message) || error), type: 'laya_service' },
    }));
  }
});

// A port already in use is an ordinary situation - an older copy of this service is still up -
// and it deserves a sentence, not an unhandled 'error' event and a stack trace about net.js.
server.on('error', (error) => {
  if (error && error.code === 'EADDRINUSE') {
    console.error(`端口 ${PORT} 已被占用：可能上一份服务还在跑。停掉它，或用 --port 换一个。`);
    process.exit(1);
  }
  throw error;
});

server.listen(PORT, HOST, async () => {
  console.log(`laya service on http://${HOST}:${PORT}  (webtorch: ${WEBTORCH})`);
  await exclusive(async () => {
    try { await refreshSdk(); }
    catch (error) { console.error('GitHub SDK 更新检查失败，继续使用已打包版本：', error.message || error); }
    await ensureLocalModel();
    await browser();
    benchmark.start();
  }).catch((error) => {
    console.error('模型没能就绪：', (error && error.message) || error);
  });
});
setInterval(() => {
  exclusive(refreshSdk).catch((error) => console.error('定时检查 webtorch SDK 失败：', error.message || error));
}, SDK_CHECK_MS);
}
