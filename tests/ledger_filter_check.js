// Executes the filtering rules the ledger page relies on.
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync(process.argv[2], 'utf8');
const context = {
  console, navigator: {}, window: { scrollY: 0, addEventListener() {} },
  IntersectionObserver: function () { return { observe() {}, disconnect() {} }; },
  document: { getElementById() { return null; }, querySelectorAll() { return []; },
    querySelector() { return null; }, addEventListener() {},
    createElement() { return { style: {}, append() {}, setAttribute() {}, classList: { add() {}, remove() {} } }; } },
  esc: v => String(v ?? ''), detail: () => '', controlNode: () => ({}),
};
vm.createContext(context);
vm.runInContext(source, context);
const run = code => vm.runInContext(code, context);
const failures = [];
const check = (label, got, want) => {
  if (JSON.stringify(got) !== JSON.stringify(want)) failures.push(`${label}: got ${JSON.stringify(got)}, want ${JSON.stringify(want)}`);
};

check('no selection shows everything', run("resultMatches('SELL', new Set())"), true);
check('selected result shows', run("resultMatches('HOLD', new Set(['HOLD']))"), true);
check('unselected result hides', run("resultMatches('BUY', new Set(['HOLD']))"), false);
check('case does not matter', run("resultMatches('hold', new Set(['HOLD']))"), true);

// Newest first, and id breaks a tie in the same direction the server uses.
const ordered = run(`[
  {created: 100, id: 1}, {created: 300, id: 5}, {created: 300, id: 7}, {created: 200, id: 3}
].sort(ledgerCompare).map(x => x.id)`);
check('newest first, then higher id', ordered, [7, 5, 3, 1]);

check('unfiltered: a full page is not the end', run("pageExhausts([{},{},{},{},{}], 5, false)"), false);
check('unfiltered: a short page is the end', run("pageExhausts([{},{}], 5, false)"), true);
check('filtered: all matches means keep going',
  run("pageExhausts([{matches_results:true},{matches_results:true},{matches_results:true},{matches_results:true},{matches_results:true}], 5, true)"), false);
check('filtered: one non-match means matches are exhausted',
  run("pageExhausts([{matches_results:true},{matches_results:false},{matches_results:false},{matches_results:false},{matches_results:false}], 5, true)"), true);
check('the results query is empty with nothing selected', run("LEDGER_RESULTS=new Set(); ledgerResultsQuery()"), '');
check('the results query names the selection', run("LEDGER_RESULTS=new Set(['HOLD','BUY']); ledgerResultsQuery()"), '&results=HOLD%2CBUY');

if (failures.length) { console.error('FAIL: ' + failures.join('\n')); process.exit(1); }
console.log('OK');
