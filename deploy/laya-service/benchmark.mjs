/** Service-owned benchmark. One queue ticket covers warm-up and all timed samples. */
export const BENCHMARK_STATE = {
  workflow: 'candidate_evaluation',
  candidates: [{
    key: 'benchmark', question: 'Will this market resolve YES by its deadline?',
    description: 'Synthetic open prediction market; no trade or external side effects.',
    status: 'OPEN', liquidity_usdt: 12000, volume_usdt: 45000,
    prices: [0.48, 0.52],
  }],
};

export const BENCHMARK_QUESTIONS = {
  route_benchmark: { type: 'choice', instructions: 'Choose the next research action from supplied facts only.', criteria: {
    PRIORITIZE: 'research now', DEFER: 'research later',
    NEEDS_DATA: 'missing facts', REJECT: 'unusable',
  } },
  quality_benchmark: { type: 'score', instructions: 'Rate current research value.', criteria: { min: 0, max: 5 } },
  series_benchmark: { type: 'noul', instructions: 'This is one window in a recurring market series.' },
  evidence_benchmark: { type: 'noul', instructions: 'The supplied facts are enough to prioritize without another read.' },
};

export function createBenchmark({ exclusive, sample, now = () => Date.now() }) {
  if (typeof exclusive !== 'function' || typeof sample !== 'function') {
    throw new TypeError('benchmark requires an exclusive queue and sample function');
  }
  let current = { status: 'not_run', active: false };
  let running = null;
  let started = false;
  const snapshot = () => ({ ...current });
  const median = (values) => {
    const sorted = [...values].sort((a, b) => a - b);
    return sorted[Math.floor(sorted.length / 2)];
  };

  function trigger() {
    if (running) return { accepted: false, benchmark: snapshot() };
    const startedAt = now();
    current = {
      ...current, status: 'queued', active: true, started_at: startedAt / 1000,
      completed_at: null, error: null,
    };
    // Set active before joining the GPU queue. Requests arriving while earlier work drains
    // get 429, rather than accumulating behind a benchmark they cannot distinguish.
    running = exclusive(async () => {
      current = { ...current, status: 'running' };
      const latencies = [];
      let usage = {};
      for (let index = 0; index < 4; index += 1) {
        const start = now();
        const observed = await sample(BENCHMARK_STATE, BENCHMARK_QUESTIONS);
        if (observed?.usage && typeof observed.usage === 'object') usage = observed.usage;
        if (index > 0) latencies.push(now() - start);
      }
      current = {
        ...current, status: 'ok', active: false, measured_at: now() / 1000,
        completed_at: now() / 1000, samples: latencies.length,
        median_ms: Math.round(median(latencies) * 10) / 10,
        max_ms: Math.round(Math.max(...latencies) * 10) / 10,
        usage,
      };
    }).catch((error) => {
      current = {
        ...current, status: 'failed', active: false, completed_at: now() / 1000,
        error: String(error?.message || error).slice(0, 300),
      };
    }).finally(() => { running = null; });
    return { accepted: true, benchmark: snapshot() };
  }

  function start() {
    if (started) return;
    started = true;
    trigger();
  }

  function stop() {
    started = false;
  }

  return { snapshot, trigger, start, stop, wait: () => running || Promise.resolve() };
}
