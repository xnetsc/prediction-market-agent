// Runs the ledger's row markup for real. Every earlier test of this view searched the source for
// strings, and a ReferenceError is invisible to that: the page blanked with "reason is not defined"
// while all of them passed.
const fs = require('fs');
const vm = require('vm');

const source = fs.readFileSync(process.argv[2], 'utf8');
const options = [];
const context = {
  console,
  window: { scrollY: 0, addEventListener() {} },
  IntersectionObserver: function () { return { observe() {}, disconnect() {} }; },
  document: {
    getElementById(id) {
      if (id === 'statusFilter') return { options };
      return null;
    },
    querySelectorAll() { return []; },
    querySelector() { return null; },
    addEventListener() {},
    createElement() { return { style: {}, append() {}, setAttribute() {}, classList: { add() {}, remove() {} } }; },
  },
  esc: value => String(value ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])),
  detail: (o, key) => '<details data-detail-key="' + key + '"></details>',
  controlNode: (tag, text, parent) => { const node = { value: '', text }; parent.options.push(node); return node; },
  serviceTitle: name => name,
  actionTitle: action => action || '',
  OPEN_DETAILS: new Set(),
};
vm.createContext(context);
vm.runInContext(source, context);

const rows = [
  {
    id: 7, created_at: 1789000000000, platform: 'polymarket', provider: 'claude',
    strategy_name: 'built_in', status: 'NO_ACTION', group: 'concluded',
    context: { market: { title: 'BTC 年底站上 10 万' } }, research: [], agent_steps: 2,
    proposed_decision: { action: 'HOLD', rationale: '价差太宽' },
    final_decision: { action: 'HOLD', rationale: '价差太宽' },
    risk_decision: { reason: 'no filter applies' }, execution: { status: 'NO_ACTION' },
    model_raw_output: '{}',
  },
  {
    id: 8, created_at: 1789000001000, platform: 'polymarket', provider: 'codex',
    strategy_name: 'built_in:discovery', status: 'PROVIDER_ERROR', group: 'failed',
    context: {}, research: null, error: 'session limit',
    proposed_decision: null, final_decision: null, risk_decision: null, execution: null,
  },
];

const html = vm.runInContext('decisionEntriesHtml(rows)', Object.assign(context, { rows }));
const failures = [];
if (!html.includes('BTC 年底站上 10 万')) failures.push('market title missing');
if (!html.includes('价差太宽')) failures.push('rationale missing');
if (!html.includes('forgetDecision(event,7)')) failures.push('delete control missing');
if ((html.match(/class="decision-entry"/g) || []).length !== 2) failures.push('expected two entries');
if (!options.some(o => o.value === 'PROVIDER_ERROR')) failures.push('status not registered');
if (failures.length) { console.error('FAIL: ' + failures.join('; ')); process.exit(1); }
console.log('OK');
