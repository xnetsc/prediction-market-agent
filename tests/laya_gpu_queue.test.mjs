import assert from 'node:assert/strict';
import test from 'node:test';
import { createGpuQueue } from '../deploy/laya-service/gpu-queue.mjs';
import { createBenchmark } from '../deploy/laya-service/benchmark.mjs';

test('GPU work stays serialized after a caller stops waiting', async () => {
  const exclusive = createGpuQueue();
  const order = [];
  let releaseFirst;
  const first = exclusive(async () => {
    order.push('first-start');
    await new Promise((resolve) => { releaseFirst = resolve; });
    order.push('first-end');
  });
  // In production the HTTP client may disconnect here. Not awaiting first does not release GPU.
  const second = exclusive(async () => { order.push('second'); });
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(order, ['first-start']);
  releaseFirst();
  await Promise.all([first, second]);
  assert.deepEqual(order, ['first-start', 'first-end', 'second']);
});

test('a failed GPU task does not poison later work', async () => {
  const exclusive = createGpuQueue();
  await assert.rejects(exclusive(async () => { throw new Error('GPU failed'); }), /GPU failed/);
  assert.equal(await exclusive(async () => 'recovered'), 'recovered');
});

test('unknown queue lanes are rejected without consuming capacity', async () => {
  const exclusive = createGpuQueue(1);
  await assert.rejects(exclusive(async () => {}, { lane: 'other' }), /unknown GPU queue lane/);
  assert.equal(await exclusive(async () => 'ok'), 'ok');
});

test('direct clients cannot grow the GPU queue without bound', async () => {
  const exclusive = createGpuQueue(1);
  let release;
  const first = exclusive(() => new Promise((resolve) => { release = resolve; }));
  await new Promise((resolve) => setImmediate(resolve));
  await assert.rejects(exclusive(async () => 'overflow'), {
    message: 'Laya GPU queue is full', code: 'GPU_QUEUE_FULL',
  });
  release();
  await first;
  assert.equal(await exclusive(async () => 'next'), 'next');
});

test('benchmark waits for active inference, then holds one exclusive ticket for all samples', async () => {
  const exclusive = createGpuQueue();
  let releaseInference;
  const events = [];
  const inference = exclusive(async () => {
    events.push('inference-start');
    await new Promise((resolve) => { releaseInference = resolve; });
    events.push('inference-end');
  });
  await new Promise((resolve) => setImmediate(resolve));
  const benchmark = createBenchmark({ exclusive, sample: async () => {
    events.push('benchmark');
    return { usage: { input_tokens: 480, encoder_tokens: 480, encoder_passes: 4 } };
  } });
  assert.equal(benchmark.trigger().accepted, true);
  assert.equal(benchmark.snapshot().status, 'queued');
  assert.equal(benchmark.snapshot().active, true);
  assert.equal(benchmark.trigger().accepted, false);
  assert.deepEqual(events, ['inference-start']);
  releaseInference();
  await inference;
  await benchmark.wait();
  assert.deepEqual(events, ['inference-start', 'inference-end',
    'benchmark', 'benchmark', 'benchmark', 'benchmark']);
  assert.equal(benchmark.snapshot().status, 'ok');
  assert.equal(benchmark.snapshot().active, false);
  assert.equal(benchmark.snapshot().samples, 3);
  assert.deepEqual(benchmark.snapshot().usage,
    { input_tokens: 480, encoder_tokens: 480, encoder_passes: 4 });
});

test('failed benchmark publishes failure and releases inference gate', async () => {
  const exclusive = createGpuQueue();
  const benchmark = createBenchmark({ exclusive, sample: async () => {
    throw new Error('GPU unavailable');
  } });
  benchmark.trigger();
  await benchmark.wait();
  assert.equal(benchmark.snapshot().status, 'failed');
  assert.match(benchmark.snapshot().error, /GPU unavailable/);
  assert.equal(benchmark.snapshot().active, false);
  assert.equal(await exclusive(async () => 'recovered'), 'recovered');
});

test('service benchmark runs at startup once and only repeats when manually triggered', async () => {
  const exclusive = createGpuQueue();
  let calls = 0;
  const benchmark = createBenchmark({ exclusive, sample: async () => {
    calls += 1;
  } });
  benchmark.start();
  await benchmark.wait();
  assert.equal(calls, 4);
  await new Promise((resolve) => setTimeout(resolve, 60));
  assert.equal(calls, 4);
  benchmark.start();
  assert.equal(calls, 4);
  assert.equal(benchmark.trigger().accepted, true);
  await benchmark.wait();
  benchmark.stop();
  assert.equal(calls, 8);
  assert.equal(benchmark.snapshot().status, 'ok');
  assert.equal('interval_seconds' in benchmark.snapshot(), false);
});
