// Executes the browser-language default for real, against several browsers.
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync(process.argv[2], 'utf8');

function contextFor(navigatorValue) {
  const saved = [];
  const context = {
    console,
    navigator: navigatorValue,
    window: { scrollY: 0, addEventListener() {} },
    IntersectionObserver: function () { return { observe() {}, disconnect() {} }; },
    document: {
      getElementById() { return null; }, querySelectorAll() { return []; },
      querySelector() { return null; }, addEventListener() {},
      createElement() { return { style: {}, append() {}, setAttribute() {}, classList: { add() {}, remove() {} } }; },
    },
    esc: v => String(v ?? ''), detail: () => '', controlNode: () => ({}),
    post: async (url, body) => { saved.push({ url, body }); return {}; },
    get: async () => ({}),
  };
  vm.createContext(context);
  vm.runInContext(source, context);
  return { context, saved };
}

(async () => {
  const failures = [];
  const cases = [
    [{ languages: ['zh-CN', 'en-US'], language: 'zh-CN' }, 'zh'],
    [{ languages: ['en-GB'], language: 'en-GB' }, 'en'],
    [{ languages: ['ja-JP', 'en-US'], language: 'ja-JP' }, 'en'],
    [{ languages: ['ja-JP'], language: 'ja-JP' }, 'zh'],
    [{ languages: [], language: '' }, 'zh'],
    [undefined, 'zh'],
  ];
  for (const [nav, expected] of cases) {
    const { context } = contextFor(nav);
    const got = vm.runInContext('browserAgentLanguage()', context);
    if (got !== expected) failures.push(`navigator ${JSON.stringify(nav)} -> ${got}, expected ${expected}`);
  }
  const unchosen = contextFor({ languages: ['en-US'], language: 'en-US' });
  const adopted = await vm.runInContext(
    'adoptBrowserLanguageIfUnchosen({fields:[{name:"agent_language",configured:false,value:"zh"}]})',
    unchosen.context);
  if (!adopted || unchosen.saved.length !== 1 || unchosen.saved[0].body.values.agent_language !== 'en')
    failures.push('an unchosen language was not adopted from the browser');
  const chosen = contextFor({ languages: ['en-US'], language: 'en-US' });
  const overwritten = await vm.runInContext(
    'adoptBrowserLanguageIfUnchosen({fields:[{name:"agent_language",configured:true,value:"zh"}]})',
    chosen.context);
  if (overwritten || chosen.saved.length) failures.push('an explicit choice was overwritten by the browser');
  if (failures.length) { console.error('FAIL: ' + failures.join('; ')); process.exit(1); }
  console.log('OK');
})();
