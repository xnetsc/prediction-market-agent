// Executes the readable ledger card against the record shapes the runtime actually writes. The
// earlier string-matching tests passed while the page threw at runtime, so this runs the code.
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync(process.argv[2], 'utf8');
const options = [];
const context = {
  console, navigator: {}, URLSearchParams, window: { scrollY: 0, addEventListener() {} },
  IntersectionObserver: function () { return { observe() {}, disconnect() {} }; },
  document: {
    getElementById(id) { return id === 'statusFilter' ? { options } : null; },
    querySelectorAll() { return []; }, querySelector() { return null; }, addEventListener() {},
    createElement() { return { style: {}, append() {}, setAttribute() {}, classList: { add() {}, remove() {} } }; },
  },
  esc: v => String(v ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])),
  detail: (o, key) => '<details data-detail-key="' + key + '"></details>',
  controlNode: (tag, text, parent) => { const n = { value: '', text }; parent.options.push(n); return n; },
  serviceTitle: n => n, actionTitle: a => a || '', OPEN_DETAILS: new Set(),
  decisionStatusTitle: v => ({PROVIDER_ERROR: '模型调用失败'}[v] || v),
};
vm.createContext(context);
vm.runInContext(source, context);
const render = rows => vm.runInContext('decisionEntriesHtml(rows)', Object.assign(context, { rows }));
const failures = [];
const expect = (label, html, fragments) => {
  for (const f of fragments) if (!html.includes(f)) failures.push(`${label}: missing ${JSON.stringify(f)}`);
};

const base = { platform: 'polymarket', provider: 'claude', strategy_name: 'built_in', created_at: 1789000000000 };
const hold = { ...base, id: 1, status: 'NO_ACTION', group: 'concluded', result: 'HOLD',
  context: { market: { title: 'Merz 在 10 月 31 日前离任' }, outcome: { name: 'Yes', displayed_probability: 0.125 },
             order_book: { best_bid: 0.12, best_ask: 0.13 }, seconds_remaining: 9147008 },
  final_decision: { action: 'HOLD', estimated_probability: 0.14, confidence: 0.55, rationale: '价差已反映', headline: '观望：卖价 0.13 已反映 14% 估计' },
  risk_decision: { outcome: 'ALLOW', reason: 'no filter' }, execution: { action: 'HOLD', status: 'NO_ACTION' }, agent_steps: 3 };
const buy = { ...base, id: 2, status: 'COMPLETED', group: 'concluded', result: 'BUY',
  context: { market: { title: 'BTC 年底站上 10 万' }, outcome: { name: 'Yes', displayed_probability: 0.42 }, order_book: { best_bid: 0.41, best_ask: 0.42 } },
  final_decision: { action: 'BUY', notional_usdt: 12, order_type: 'MARKET', estimated_probability: 0.6, confidence: 0.7 },
  execution: { action: 'BUY', status: 'FILLED', order_id: 'paper-abc', order: { order_id: 'paper-abc', side: 'BUY', status: 'FILLED', quantity: 28.5, price: 0.42, notional: 12, fee: 0.04 } },
  settlement: { settled: true, won: true, payout: 28.5, cost: 12.04, profit: 16.46 } };
const discovery = { ...base, id: 3, status: 'OK', group: 'concluded', result: 'OK', strategy_name: 'built_in:discovery',
  context: { stage: 'discovery', candidate_count: 24, priced_count: 6, candidate_titles: { '48930': 'NATO x Russia 冲突' } },
  final_decision: { selections: [{ topic_id: '48930', reason: '成交量 +67%' }], next_scan_seconds: 900, next_survey_queries: ['fed decision'], headline: '选出 1 个：NATO 冲突成交量 +67%' } };
const failed = { ...base, id: 4, status: 'PROVIDER_ERROR', group: 'failed', result: 'PROVIDER_ERROR', error: 'session limit', context: {} };
const rejected = { ...base, id: 5, status: 'RISK_REJECTED', group: 'concluded', result: 'RISK_REJECTED',
  context: { market: { title: 'X' } }, final_decision: { action: 'BUY', notional_usdt: 50, order_type: 'LIMIT', limit_price: 0.3 },
  risk_decision: { outcome: 'REJECT', reason: '单笔超过上限' } };

expect('hold', render([hold]), ['发现了什么', 'Merz 在 10 月 31 日前离任', '买一 0.12 / 卖一 0.13', '市场隐含 12.5%', '模型估计 14%',
  '>观望<', '按结论不下单', '观望：卖价 0.13 已反映 14% 估计', '细节', '原始数据']);
expect('buy', render([buy]), ['>买入<', '12 USDT', '市价', '模拟', '已成交', '28.5 份 @ 0.42', '揭标：赢', '盈亏 +16.46 USDT']);
expect('discovery', render([discovery]), ['24 个候选市场', '6 个读到了真实价差', '选出 1 个', 'NATO x Russia 冲突',
  '交给决策阶段分析 1 个标的', '约 15 分钟后再看', 'fed decision']);
expect('failed', render([failed]), ['没能得出结论', '没有执行：session limit', '模型调用失败，没分析完']);
expect('rejected', render([rejected]), ['>买入<', '限价 0.3', '被规则拒绝，没有下单', '单笔超过上限']);
if (render([hold]).includes('{"action"')) failures.push('hold: raw JSON leaked into the readable card');

// The shapes the live ledger actually holds, which the fixtures above had smoothed over.
const book404 = 'Polymarket read HTTP 404: {"error":"No orderbook exists for the requested token id"}\n';
const candidates = [
  { topic_id: '48930', title: 'NATO x Russia 冲突', book_error: book404, liquidity_usdt: 88300, seconds_remaining: 9500000, verified: true },
  { topic_id: '86832', title: '哪些公司 2027 前被收购', best_bid: 0.14, best_ask: 0.15, spread: 0.01, spread_pct_of_mid: 6.9, verified: true },
  { topic_id: '42365', title: 'Venezuela', why_not_priced: 'no market in this event is open for trading', verified: true },
  { topic_id: '11111', title: '没核实的', verified: false },
];
// An opened discovery round: full context (no candidate_titles), steps in research, agent_steps 0.
const discoveryFull = { ...base, id: 6, status: 'OK', group: 'concluded', result: 'OK', strategy_name: 'built_in:discovery', agent_steps: 0,
  context: { stage: 'discovery', candidates },
  research: [
    { tool: 'TOPIC_HISTORY', arguments: { topic_id: '48930' }, reason: '成交量涨了</reason>' },
    { tool: 'TOPIC_HISTORY', arguments: { topic_id: '86832' }, reason: '看看上次' },
    { tool: 'SEARCH_OTHER_PLATFORMS', arguments: { query: 'fed decision' }, reason: '对照' },
  ],
  final_decision: { selections: [{ topic_id: '48930', reason: '成交量 +67%' }] } };
const discoveryHtml = render([discoveryFull]);
expect('discovery (opened)', discoveryHtml, ['查了 3 次：看以往记录 ×2、跨平台搜同类市场', '选出 1 个', 'NATO x Russia 冲突',
  '没读到价格：平台上这个结果没有订单簿', '0.14 / 0.15', '6.9%', '$88.3k', '这个事件下没有开放交易的市场', '本轮没去核实',
  '「fed decision」', '内置发现策略', '2 个读到了真实价差'.replace('2', '1')]);
if (discoveryHtml.includes('没有调用研究工具')) failures.push('discovery (opened): its steps were not counted');
if (discoveryHtml.includes('&lt;/reason&gt;')) failures.push('discovery (opened): a stray tag reached the page');

// #805: a discovery round that failed on a weekly limit an old classifier called unknown.
const discoveryFailed = { ...base, id: 7, status: 'PROVIDER_ERROR', group: 'failed', result: 'PROVIDER_ERROR', strategy_name: 'built_in:discovery',
  error: "All decision providers failed: claude: [unknown] Claude failed: success / api_error / You've hit your weekly limit · resets 1pm (UTC)",
  context: { stage: 'discovery', candidates } };
expect('discovery (failed)', render([discoveryFailed]), ['发现轮次', '没能得出结论', '模型调用失败，没分析完',
  '没有执行：Claude：本周额度用完，1pm (UTC) 恢复', '原始报错']);

// Two services tried in turn, one timing out.
const twoFailed = { ...failed, id: 8, provider: 'codex>claude', context: { market: { title: 'M' } },
  error: "All decision providers failed: codex: [rate_limit] Codex failed: try again at Sep 19th, 2026 8:13 AM.\n; claude: [transient] Claude timed out after 180s" };
expect('two providers', render([twoFailed]), ['Codex：额度用完或被限流，Sep 19th, 2026 8:13 AM 恢复', 'Claude：连接不稳定（超时或网络故障）（等了 180 秒没有响应）', 'Codex → Claude']);

// Rows the built-in strategy wrote while no strategy plugin was ready carry no strategy name.
const unnamed = { ...hold, id: 9, strategy_name: '', market_topic_id: 't9', context: {} };
expect('unnamed strategy', render([unnamed]), ['内置决策策略', '市场 #t9']);

for (const [label, row] of Object.entries({ hold, buy, discovery, failed, rejected, discoveryFull, discoveryFailed, twoFailed, unnamed }))
  if (render([row]).includes('未记录')) failures.push(label + ': shows 未记录 instead of saying what the record is');

// The overview when nothing can decide: the reason per service and, where known, when it is back.
const now = 1789650000;
const paused = { state: 'ai_paused', decision_capacity: { available: false, providers: {
    claude: { ready: false, kind: 'rate_limit', recovers_at: now + 8100, confirming: false },
    codex: { ready: false, kind: 'auth' } } },
  decision_providers: { openai_compatible: { ready: false, reasons: ['请填写 API Key 和模型 ID'] } } };
const pausedHtml = vm.runInContext('aiPauseHtml(setup, now)', Object.assign(context, { setup: paused, now }));
expect('ai pause', pausedHtml, ['Claude：额度用完，预计 2 小时 15 分钟后恢复', 'Codex：登录失效，需要重新登录', '请填写 API Key 和模型 ID',
  '去处理', '不采集市场数据，也不产生决策记录', '立即重新检测']);
const title = setup => vm.runInContext('aiPauseTitle(setup)', Object.assign(context, { setup }));
if (title(paused) !== '机器人已暂停：没有可用的 AI 模型服务') failures.push('ai pause: mixed causes titled ' + title(paused));
const quotaOnly = { decision_capacity: { providers: { claude: { ready: false, kind: 'rate_limit' } } } };
if (title(quotaOnly) !== '机器人已暂停：AI 额度用完') failures.push('ai pause: quota titled ' + title(quotaOnly));
const unknownWhen = vm.runInContext('aiPauseHtml(setup, now)', Object.assign(context, { setup: quotaOnly, now }));
expect('ai pause (no stated time)', unknownWhen, ['没说何时恢复，会定期免费查询额度']);

// A record with nothing written in prose is not sent to a model to be rephrased.
const proseChecks = [[hold, true], [discoveryFull, true], [discoveryFailed, false], [failed, false]];
for (const [row, expected] of proseChecks) {
  const marked = render([row]).includes('data-prose="1"');
  if (marked !== expected) failures.push('prose gate: record ' + row.id + ' marked ' + marked);
}
if (!vm.runInContext('String(requestReadable)', context).includes("dataset.prose!=='1'"))
  failures.push('prose gate: a record without prose would still cost a model call');

// A restatement that arrived with markup around it is not printed with the markup.
const restated = { ...hold, id: 10, readable: { headline: '观望', found: '<found>买一 0.12</found>', analysis: '看过资料。</analysi' } };
const restatedHtml = render([restated]);
if (/&lt;\/?(analysi|found)/.test(restatedHtml))
  failures.push('restated: markup from the model reached the card');
if (!restatedHtml.includes('买一 0.12')) failures.push('restated: the restatement itself was lost');

// Selecting rows to delete: the tick is part of the row and survives the row being redrawn.
if (!render([hold]).includes('class="decision-pick"')) failures.push('pick: rows carry no checkbox');
vm.runInContext('LEDGER_PICKED.add("1")', context);
if (!render([hold]).includes('data-pick="1" checked')) failures.push('pick: a ticked row lost its tick when redrawn');
vm.runInContext('LEDGER_PICKED.clear(); LEDGER_TAB="concluded"; LEDGER_RESULTS=new Set(["HOLD","BUY"]); LEDGER_QUERY="&platform=polymarket&provider=&status=&group=concluded"', context);
const label = vm.runInContext('ledgerCategoryLabel(ledgerCategoryMatch())', context);
if (label !== '有结论 · 观望、买入 · 平台 polymarket') failures.push('category label: ' + label);
const match = vm.runInContext('JSON.stringify(ledgerCategoryMatch())', context);
if (match !== JSON.stringify({ group: 'concluded', results: 'HOLD,BUY', platform: 'polymarket', provider: '', status: '' }))
  failures.push('category match: ' + match);

if (failures.length) { console.error('FAIL:\n' + failures.join('\n')); process.exit(1); }
console.log('OK');
