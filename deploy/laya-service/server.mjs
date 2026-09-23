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
import { mkdir, readFile, rename, rm, stat } from 'node:fs/promises';
import { createReadStream, createWriteStream, existsSync } from 'node:fs';
import { once } from 'node:events';
import { dirname, extname, join, resolve, sep } from 'node:path';
import { spawn } from 'node:child_process';
import { chromium } from 'playwright';
import { syncWebtorch } from './sync-webtorch.mjs';

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
const HEADLESS = option('headless', 'true') !== 'false';

/* The one model this service exists for. Not a parameter: the SDK's demo page can load anything,
 * and this is a service for laya. */
const MODEL = 'convaiinnovations/laya';
const MODEL_DIR = resolve(option('models', process.env.LAYA_MODELS || join(HERE_DIR(), 'models')), 'laya');
const ENDPOINT = option('endpoint', process.env.HF_ENDPOINT || 'https://huggingface.co');

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
const WANTED = /^(encoder\/|tokenizer\/|model\.safetensors$|rl_agent_config\.json$|config\.json$)/;

async function present(path) {
  try { return (await stat(path)).size > 0; } catch { return false; }
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
  const index = await fetch(`${ENDPOINT}/api/models/${MODEL}`);
  if (!index.ok) throw new Error(`the model index answered HTTP ${index.status}`);
  const files = ((await index.json()).siblings || [])
    .map((item) => String(item.rfilename || ''))
    .filter((name) => WANTED.test(name));
  if (!files.some((name) => name === 'model.safetensors')) {
    throw new Error('the repo listing has no model.safetensors');
  }
  for (const name of files) {
    const target = join(MODEL_DIR, name);
    if (await present(target)) continue;
    const response = await fetch(`${ENDPOINT}/${MODEL}/resolve/main/${name}`);
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

async function serveStatic(request, response, url) {
  // `/laya/...` is this service's own page, `/models/...` the weights it keeps on disk, and
  // everything else the webtorch checkout, mounted at the root so the page's relative paths are
  // the ones the SDK's own documentation uses.
  const mount = url.pathname.startsWith('/laya/')
    ? [HERE, url.pathname.slice('/laya/'.length)]
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
let modelWork = Promise.resolve();
const SDK_CHECK_MS = 6 * 60 * 60 * 1000;

function exclusive(task) {
  const result = modelWork.catch(() => {}).then(task);
  modelWork = result.catch(() => {});
  return result;
}

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

async function completions(request, response, body) {
  await browser();
  const asked = requested(body);
  const started = Date.now();
  const result = await page.evaluate(
    (payload) => window.__laya.decide(payload),
    { state: asked.state ?? {}, questions: asked.questions },
  );
  const content = JSON.stringify({ answers: shaped(result.answers, asked.questions) });
  const usage = result.usage || {};
  response.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8' }).end(JSON.stringify({
    id: 'laya-' + Date.now().toString(36),
    object: 'chat.completion',
    created: Math.floor(Date.now() / 1000),
    model: body.model || MODEL,
    choices: [{ index: 0, message: { role: 'assistant', content }, finish_reason: 'stop' }],
    usage: {
      prompt_tokens: Number(usage.prompt_tokens || usage.tokens || 0),
      completion_tokens: 0,
      total_tokens: Number(usage.prompt_tokens || usage.tokens || 0),
    },
    // Free, local, and honest about it: nothing was billed because nothing left the machine.
    cost: 0,
    latency_ms: Date.now() - started,
  }));
}

const server = createServer(async (request, response) => {
  const url = new URL(request.url, `http://127.0.0.1:${PORT}`);
  if (process.env.LAYA_TRACE) console.log('[req]', request.method, url.pathname);
  try {
    if (request.method === 'GET' && url.pathname === '/health') {
      const status = page ? await page.evaluate(() => window.__laya.status()) : { ready: false };
      response.writeHead(status.ready ? 200 : 503, { 'Content-Type': 'application/json' })
        .end(JSON.stringify(status));
      return;
    }
    if (request.method === 'GET' && /\/v1\/models$/.test(url.pathname)) {
      response.writeHead(200, { 'Content-Type': 'application/json' }).end(JSON.stringify({
        data: [{
          id: MODEL, name: MODEL, object: 'model',
          description: 'Typed decision model served locally on WebGPU; no tokens are billed.',
          supported_parameters: ['response_format', 'structured_outputs'],
          pricing: { prompt: '0', completion: '0' },
        }],
      }));
      return;
    }
    if (request.method === 'POST' && /chat\/completions$/.test(url.pathname)) {
      const chunks = [];
      for await (const chunk of request) chunks.push(chunk);
      const body = JSON.parse(Buffer.concat(chunks).toString('utf8') || '{}');
      await exclusive(() => completions(request, response, body));
      return;
    }
    await serveStatic(request, response, url);
  } catch (error) {
    // The caller gets the reason in the shape its client already knows how to read.
    response.writeHead(500, { 'Content-Type': 'application/json' }).end(JSON.stringify({
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

server.listen(PORT, '127.0.0.1', async () => {
  console.log(`laya service on http://127.0.0.1:${PORT}  (webtorch: ${WEBTORCH})`);
  await exclusive(async () => {
    try { await refreshSdk(); }
    catch (error) { console.error('GitHub SDK 更新检查失败，继续使用已打包版本：', error.message || error); }
    await ensureLocalModel();
    await browser();
  }).catch((error) => {
    console.error('模型没能就绪：', (error && error.message) || error);
  });
});
setInterval(() => {
  exclusive(refreshSdk).catch((error) => console.error('定时检查 webtorch SDK 失败：', error.message || error));
}, SDK_CHECK_MS);
}
