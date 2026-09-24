/** Serialize work on one WebGPU page, including work whose HTTP caller has disconnected. */
export function createGpuQueue(maxPending = 64) {
  if (!Number.isSafeInteger(maxPending) || maxPending < 1) {
    throw new RangeError('maxPending must be a positive integer');
  }
  let tail = Promise.resolve();
  let pending = 0;
  return function exclusive(task) {
    if (pending >= maxPending) {
      const error = new Error('Laya GPU queue is full');
      error.code = 'GPU_QUEUE_FULL';
      return Promise.reject(error);
    }
    pending += 1;
    const work = tail.then(task);
    // A failed task must not poison the queue. Even if the caller times out, this promise still
    // tracks the actual GPU task until completion before allowing the next one to start.
    tail = work.then(() => {}, () => {});
    return work.then(
      (value) => { pending -= 1; return value; },
      (error) => { pending -= 1; throw error; },
    );
  };
}
