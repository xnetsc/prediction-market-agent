/** One GPU scheduler with separate lanes. Text work may pass queued vision work, while a running
 * task remains exclusive so two runtimes cannot contend for the same device or exhaust memory. */
export function createGpuQueue(maxPending = 64) {
  if (!Number.isSafeInteger(maxPending) || maxPending < 1) {
    throw new RangeError('maxPending must be a positive integer');
  }
  const lanes = { benchmark: [], text: [], vision: [] };
  let pending = 0;
  let running = false;

  function take() {
    return lanes.benchmark.shift() || lanes.text.shift() || lanes.vision.shift() || null;
  }

  function pump() {
    if (running) return;
    const item = take();
    if (!item) return;
    running = true;
    const finish = () => {
      pending -= 1;
      running = false;
      pump();
    };
    Promise.resolve().then(item.task).then(
      (value) => { finish(); item.resolve(value); },
      (error) => { finish(); item.reject(error); },
    );
  }

  return function exclusive(task, options = {}) {
    if (pending >= maxPending) {
      const error = new Error('Laya GPU queue is full');
      error.code = 'GPU_QUEUE_FULL';
      return Promise.reject(error);
    }
    const lane = options.lane || 'text';
    if (!Object.prototype.hasOwnProperty.call(lanes, lane)) {
      return Promise.reject(new RangeError(`unknown GPU queue lane: ${lane}`));
    }
    pending += 1;
    const result = new Promise((resolve, reject) => lanes[lane].push({ task, resolve, reject }));
    pump();
    return result;
  };
}
