import assert from 'node:assert/strict';
import test from 'node:test';
import { createGpuQueue } from '../deploy/laya-service/gpu-queue.mjs';

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
