/* Read-only browser acceptance against a management-only test deployment. */
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const path = require('node:path');
const fs = require('node:fs');

(async () => {
    const base = process.env.DASHBOARD_TEST_URL || 'http://127.0.0.1:18765';
    const output = process.env.DASHBOARD_SCREENSHOTS;
    // UI-only fixture. Real OpenRouter capability filtering is exercised by Python integration tests.
    const publicCatalog=[
        {value:'fixture/structured-a',label:'Structured A'},
        {value:'fixture/structured-b',label:'Structured B'},
    ];
    if (output) fs.mkdirSync(output, {recursive:true});
    const browser = await chromium.launch({channel:'chrome', headless:true});
    try {
        for (const width of [390, 768, 1440]) {
            const context = await browser.newContext({viewport:{width,height:1000},reducedMotion:'reduce'});
            const page = await context.newPage();
            let remoteModel=null;
            const passiveReads=[];
            await page.route('**/api/local',async route=>{
                const message=route.request().postDataJSON();
                if(['/api/runtime','/api/plugins/controls'].includes(message.url))passiveReads.push(message.url);
                if(message.url==='/api/plugins/controls'){
                    const checked=Math.floor(Date.now()/1000);
                    await route.fulfill({json:{items:[
                        {kind:'decision_provider',name:'codex',status:{control_type:'client',state:'authenticated',message:'已登录',installed_version:'1.0.0',model:'fixture-codex',usage:{checked_at:checked,available:true,source:'fixture',windows:[{label:'当前窗口',used_percent:21}]},actions:[{id:'refresh_usage',label:'刷新账号状态',group:'账号状态'},{id:'login',label:'登录 / 重新登录'}]}},
                        {kind:'decision_provider',name:'claude',status:{control_type:'client',state:'authenticated',message:'已登录',installed_version:'1.0.0',model:'fixture-claude',usage:{checked_at:checked,available:false,source:'fixture',windows:[{label:'5 小时窗口',used_percent:100,resets_at:checked+3600},{label:'周额度',used_percent:82,resets_at:checked+172800}]},actions:[{id:'refresh_usage',label:'刷新账号状态',group:'账号状态'},{id:'login',label:'登录 / 重新登录'}]}},
                        {kind:'decision_provider',name:'openrouter',status:{control_type:'openrouter',state:'configured',message:'已配置',model:'fixture/structured-a',agent_cli:{requested:'AUTO',selected:'CODEX',available:['CODEX','CLAUDE'],unavailable:{}},usage:{checked_at:checked,available:true,source:'fixture',account:{total_credits:100.5,total_usage:25.75,balance:74.75},key:{usage:25.5,usage_daily:1.5,usage_weekly:7.5,usage_monthly:20.5,limit:100,limit_remaining:74.5,limit_reset:'monthly'}},actions:[{id:'refresh_usage',label:'刷新余额与用量',group:'账号与额度'}]}}
                    ]}});return;
                }
                if(message.url==='/api/plugins/config/choices'&&message.body.name==='openrouter'){
                    await route.fulfill({json:{items:publicCatalog}});return;
                }
                if(message.url==='/api/plugins/config/choices'&&['codex','claude'].includes(message.body.name)){
                    const items=message.body.field.endsWith('_MODEL')
                        ?[{value:'',label:'使用客户端默认'},{value:'fixture-model',label:'Fixture model'}]
                        :[{value:'',label:'默认'},{value:'high',label:'高（high）'}];
                    await route.fulfill({json:{items}});return;
                }
                if(message.url==='/api/plugins/manage'&&remoteModel!==null){
                    const response=await route.fetch(),manifest=await response.json();
                    const plugin=manifest.plugins.decision_provider.find(p=>p.name==='openrouter');
                    plugin.configuration.fields.find(f=>f.name==='OPENROUTER_MODEL').value=remoteModel;
                    await route.fulfill({response,json:manifest});return;
                }
                await route.continue();
            });
            const errors = [];
            page.on('pageerror', error => errors.push(error.message));
            await page.goto(base);
            await page.waitForSelector('#cards .card');
            await page.waitForSelector('#setupChecklist');
            assert((await page.locator('#gettingStarted').innerText()).includes('界面验收测试实例'));
            assert((await page.locator('#gettingStarted').innerText()).includes('正常启动的实例'));
            assert(!(await page.locator('#gettingStarted').innerText()).includes('确认暂停设置'));
            assert((await page.locator('#setupChecklist>summary').innerText()).includes('项必须处理'));
            assert.equal(await page.locator('[data-setup-tab="required"]').getAttribute('aria-selected'),'true');
            assert(await page.locator('[data-setup-panel="required"]').isVisible());
            assert.equal(await page.locator('[data-setup-panel="optional"]').isVisible(),false);
            assert((await page.locator('#gettingStarted').innerText()).includes('平台插件自行判断能否启动'));
            assert(!(await page.locator('#gettingStarted').innerText()).includes('未就绪不会阻止机器人启动'));
            await page.locator('[data-setup-tab="optional"]').click();
            assert.equal(await page.locator('[data-setup-tab="optional"]').getAttribute('aria-selected'),'true');
            assert(await page.locator('[data-setup-panel="optional"]').isVisible());
            assert.equal(await page.locator('[data-setup-panel="required"]').isVisible(),false);
            assert((await page.locator('[data-setup-panel="optional"]').innerText()).includes('未就绪不会阻止机器人启动'));
            await page.locator('[data-setup-tab="required"]').click();
            const readsAfterEntry=passiveReads.length;
            await page.waitForTimeout(1200);
            assert.equal(passiveReads.length,readsAfterEntry,'the console must not poll runtime or controls in the background');
            if(output)await page.screenshot({path:path.join(output,`setup-${width}.png`),fullPage:true});
            for (const view of ['overview','decisions','pnl','funds','models','plugins','settings','security']) {
                await page.evaluate(view => {location.hash=view}, view);
                await page.waitForFunction(view => document.querySelector('.nav-link[aria-current="page"]').hash==='#'+view, view);
                assert(await page.locator(`[data-view="${view}"]`).first().isVisible());
                const overflow = await page.evaluate(() => document.documentElement.scrollWidth>innerWidth);
                assert.equal(overflow,false,`${width}px ${view} must not overflow the viewport`);
                if(view==='settings'){
                    await page.waitForSelector('#egressRoute option',{state:'attached'});
                    await page.waitForSelector('[data-app-field="shared_http_proxy"]',{state:'attached'});
                    assert(await page.locator('#environmentPanel').isVisible());
                    assert.equal(await page.locator('#egressRoute option[value="inherited"]').count(),1);
                    assert.equal(await page.locator('#egressComparison .egress-compare-card').count(),2);
                    assert(await page.locator('#sharedProxySettings').isVisible());
                    assert.equal(await page.locator('[data-app-field="shared_http_proxy"]').count(),1);
                    assert.equal(await page.locator('[data-app-field="shared_http_proxy"]').inputValue(),'HOST');
                    assert((await page.locator('[data-view="settings"]').allInnerTexts()).join('\n').includes('OpenRouter'));
                    await page.locator('#environmentFacts summary').click();
                    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
                    if(output)await page.screenshot({path:path.join(output,`environment-${width}.png`)});
                }
                if(view==='decisions'){
                    assert.equal(await page.locator('#discoveryActivity').count(),1);
                }
                if(view==='pnl'){
                    assert.equal(await page.locator('#pnlTotals').count(),1);
                    assert.equal(await page.locator('#pnlAccounts').count(),1);
                    assert.equal(await page.locator('#pnlEvents').count(),1);
                }
            }
            if(width<801){
                await page.locator('#menuToggle').click();
                await page.locator('.nav-link[href="#models"]').click();
                await page.waitForFunction(()=>document.getElementById('menuToggle').getAttribute('aria-expanded')==='false');
                assert.equal(await page.locator('#menuToggle').getAttribute('aria-expanded'),'false');
            }
            await page.evaluate(()=>{location.hash='plugins'});
            await page.waitForSelector('.category-tile');
            assert.equal(await page.locator('.category-tile').count(),7);
            assert.equal(await page.locator('.category-tile').filter({hasText:'AI 模型服务'}).count(),0);
            assert.equal(await page.locator('#pluginCategoryNav a[href="#plugins/decision_provider"]').count(),0);
            assert.equal(await page.locator('#pluginManager .plugin-category:visible').count(),0);
            if(output)await page.screenshot({path:path.join(output,`plugins-${width}.png`),fullPage:true});
            for(const kind of ['api','market_discovery','decision_evaluator','decision_strategy','research_tool','agent_policy','risk']){
                await page.evaluate(kind=>{location.hash='plugins/'+kind},kind);
                await page.waitForSelector('#category_'+kind,{state:'visible'});
                assert.equal(await page.locator('#pluginManager .plugin-category:visible').count(),1);
                assert(await page.locator('#pluginCategoryGuide').innerText().then(s=>s.includes('你需要做什么')));
                if(kind==='decision_strategy'){
                    const original=await page.locator('input[name="strategy"]:checked').getAttribute('value');
                    assert(await page.getByLabel('使用内置默认策略').isVisible());
                    if(original==='')await page.locator('input[name="strategy"][value="general_agent"]').check();
                    await page.getByLabel('使用内置默认策略').check();
                    assert((await page.locator('#selectionStatus_decision_strategy_none').innerText()).includes('尚未保存'));
                    if(original)await page.locator('input[name="strategy"][value="'+original+'"]').check();
                }
                if(kind==='risk'){
                    const toggle=page.locator('#category_risk .enable').first(),original=await toggle.isChecked();
                    const pluginName=await toggle.getAttribute('data-name');
                    await toggle.setChecked(!original);
                    assert((await page.locator('#selectionStatus_risk_'+pluginName).innerText()).includes('尚未保存'));
                    assert(await page.locator('#globalFeedback').isVisible());
                    await toggle.setChecked(original);
                }
                assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
                if(output&&kind==='api')await page.screenshot({path:path.join(output,`platforms-${width}.png`),fullPage:true});
            }
            await page.evaluate(()=>{location.hash='plugins/api'});
            await page.locator('#category_api .plugin-config summary').first().click();
            assert.equal(await page.locator('#category_api .plugin-config').first().getAttribute('open'),'');
            await page.evaluate(()=>{location.hash='models'});
            await page.waitForSelector('#control_decision_provider_openrouter');
            await page.waitForSelector('#control_decision_provider_openrouter .usage-readout');
            assert.equal(await page.locator('.client-options[open]').count(),0);
            assert.equal(await page.locator('#control_decision_provider_openrouter .client-options').count(),0);
            assert((await page.locator('#control_decision_provider_openrouter .usage-readout').innerText()).includes('$74.75'));
            assert((await page.locator('#control_decision_provider_openrouter .usage-readout').innerText()).includes('Agent：CODEX CLI'));
            assert((await page.locator('#control_decision_provider_openrouter .usage-readout').innerText()).includes('刷新余额与用量'));
            const cliPicker=page.locator('#control_decision_provider_openrouter .cli-picker select');
            assert(await cliPicker.isVisible());
            assert.equal(await cliPicker.inputValue(),'AUTO');
            assert.equal(await cliPicker.locator('option').count(),3);
            assert(await page.locator('#control_decision_provider_openrouter .cli-picker').getByRole('button',{name:'保存 Agent CLI'}).isVisible());
            assert((await page.locator('#control_decision_provider_codex .usage-readout').innerText()).includes('21% 已用'));
            assert((await page.locator('#control_decision_provider_claude .usage-readout').innerText()).includes('额度已用尽'));
            assert((await page.locator('#control_decision_provider_claude .usage-readout').innerText()).includes('5 小时窗口'));
            assert((await page.locator('#control_decision_provider_claude .usage-readout').innerText()).includes('周额度'));
            assert(await page.locator('#control_decision_provider_codex .usage-readout').isVisible());
            assert(await page.locator('#control_decision_provider_claude .usage-readout').isVisible());
            const modelEnable=page.locator('#control_decision_provider_openrouter .model-enable');
            assert(await modelEnable.isVisible());
            await modelEnable.setChecked(!(await modelEnable.isChecked()));
            assert((await page.locator('#modelSelectionStatus_openrouter').innerText()).includes('尚未保存'));
            await modelEnable.setChecked(!(await modelEnable.isChecked()));
            if(output)await page.screenshot({path:path.join(output,`models-${width}.png`),fullPage:true});
            for(const name of ['codex','claude']){
                await page.evaluate(name=>{location.hash='model-config/'+name},name);
                const prefix='#cfg_decision_provider_'+name+'_'+name.toUpperCase();
                await page.waitForSelector(prefix+'_MODEL[data-loaded="true"]',{timeout:40000});
                assert.equal(await page.locator(prefix+'_MODEL').evaluate(e=>e.tagName),'SELECT');
                assert((await page.locator(prefix+'_MODEL option').count())>1);
                assert(await page.locator(prefix+'_HTTP_PROXY').isVisible());
                assert(/^(INHERIT|DIRECT|HOST|ENVIRONMENT|SYSTEM|https?:\/\/)/.test(await page.locator(prefix+'_HTTP_PROXY').inputValue()));
                assert((await page.locator('#plugin_decision_provider_'+name+' .config-section-heading').innerText()).includes('网络连接'));
                const model=await page.locator(prefix+'_MODEL option').nth(1).getAttribute('value');
                await page.locator(prefix+'_MODEL').selectOption(model);
                await page.waitForFunction(id=>!document.getElementById(id+'_note').textContent.includes('正在读取'),prefix.slice(1)+'_EFFORT');
                assert.equal(await page.locator(prefix+'_EFFORT').evaluate(e=>e.tagName),'SELECT');
                assert((await page.locator(prefix+'_EFFORT').innerText()).includes('默认'));
                if(output)await page.screenshot({path:path.join(output,`model-options-${name}-${width}.png`),fullPage:true});
            }
            await page.evaluate(()=>{location.hash='models'});
            await page.locator('#control_decision_provider_openrouter a').click();
            await page.waitForSelector('#cfg_decision_provider_openrouter_OPENROUTER_API_KEY');
            assert(await page.locator('#cfg_decision_provider_openrouter_OPENROUTER_API_KEY').isVisible());
            assert.equal(await page.locator('#cfg_decision_provider_openrouter_OPENROUTER_AGENT_CLI').inputValue(),'AUTO');
            const credential=page.locator('#cfg_decision_provider_openrouter_OPENROUTER_API_KEY');
            assert.equal(await credential.getAttribute('type'),'search');
            assert.equal(await credential.getAttribute('autocomplete'),'off');
            assert.equal(await credential.evaluate(e=>getComputedStyle(e).webkitTextSecurity),'disc');
            assert.equal(await page.locator('#cfg_decision_provider_openrouter_OPENROUTER_API_BASE').count(),0);
            assert.equal(await page.locator('#cfg_decision_provider_openrouter_OPENROUTER_EXTRA_HEADERS_JSON').count(),0);
            assert.equal(await page.locator('#cfg_decision_provider_openrouter_OPENROUTER_API_SECRET').count(),0);
            assert(await page.locator('#cfg_decision_provider_openrouter_OPENROUTER_MODEL').isVisible());
            const apiModel='#cfg_decision_provider_openrouter_OPENROUTER_MODEL';
            await page.waitForSelector(apiModel+'[data-loaded="true"]',{timeout:30000});
            assert.equal(await page.locator(apiModel).evaluate(e=>e.tagName),'SELECT');
            assert.equal(await page.locator(apiModel).getAttribute('data-selection-only'),'true');
            const realModels=await page.locator(apiModel+' option').count();
            assert(realModels>=publicCatalog.length&&realModels<=publicCatalog.length+1);
            const availableModels=await page.locator(apiModel+' option').evaluateAll(options=>options.map(option=>option.value));
            assert(publicCatalog.every(item=>availableModels.includes(item.value)));
            const firstModel=await page.locator(apiModel+' option').first().getAttribute('value');
            await page.locator(apiModel).selectOption(firstModel);
            assert.equal(await page.locator(apiModel).inputValue(),firstModel);
            await page.evaluate(id=>{const input=document.getElementById(id);input.dataset.savedValue=JSON.stringify(input.value)},apiModel.slice(1));
            if(output)await page.screenshot({path:path.join(output,`openrouter-models-${width}.png`),fullPage:true});
            remoteModel='fixture/saved-current';
            await page.evaluate(()=>{location.hash='models'});
            await page.waitForSelector('#modelConfigurationSection',{state:'hidden'});
            await page.evaluate(()=>{location.hash='model-config/openrouter'});
            await page.waitForFunction(id=>document.getElementById(id).value==='fixture/saved-current',apiModel.slice(1));
            assert((await page.locator('#plugin_decision_provider_openrouter .config-load-status').innerText()).includes('已加载当前'));
            const draftModel=await page.locator(apiModel+' option').first().getAttribute('value');
            await page.locator(apiModel).selectOption(draftModel);
            remoteModel='fixture/changed-elsewhere';
            await page.evaluate(()=>{location.hash='models'});
            await page.waitForSelector('#modelConfigurationSection',{state:'hidden'});
            await page.evaluate(()=>{location.hash='model-config/openrouter'});
            await page.waitForFunction(()=>document.querySelector('#plugin_decision_provider_openrouter .config-load-status').textContent.includes('保留了'));
            assert.equal(await page.locator(apiModel).inputValue(),draftModel);
            remoteModel=null;
            assert.equal(await page.locator('#cfg_decision_provider_openrouter_OPENROUTER_API_KEY').inputValue(),'');
            // The configuration form exists only in the dedicated model-service page.
            await page.locator(apiModel).selectOption(draftModel);
            await page.evaluate(()=>{location.hash='models'});
            await page.waitForSelector('#modelConfigurationSection',{state:'hidden'});
            await page.evaluate(()=>{location.hash='model-config/openrouter'});
            await page.waitForSelector('#modelConfigurations #category_decision_provider',{state:'visible'});
            assert.equal(await page.locator('#cfg_decision_provider_openrouter_OPENROUTER_MODEL').count(),1);
            assert.equal(await page.locator(apiModel).inputValue(),draftModel);
            // Exercise the real form handlers with browser-local write responses only.
            // Never submit fixture credentials or selections to the running deployment.
            const writes=[];
            let failCatalog=false;
            await page.route('**/api/local',async route=>{
                const message=route.request().postDataJSON();
                if(message.url==='/api/plugins/config'||message.url==='/api/plugins/selection'||message.url==='/api/plugins/openrouter/create'){
                    if(message.url==='/api/plugins/config')remoteModel=message.body.values.OPENROUTER_MODEL;
                    writes.push(message);
                    if(message.url==='/api/plugins/openrouter/create'){
                        const response=await fetch(base+'/api/local',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url:'/api/plugins/manage',body:null})});
                        assert(response.ok);
                        await route.fulfill({json:{installed:'/fixture/openrouter_ui.py',management:await response.json()}});return;
                    }
                    await route.fulfill({json:{}});return;
                }
                if(message.url==='/api/plugins/config/choices'){
                    if(failCatalog){await route.fulfill({status:503,json:{detail:'OPENROUTER_HTTP_PROXY must be INHERIT, DIRECT, SYSTEM, or an http(s) URL'}});return}
                    writes.push(message);await route.fulfill({json:{items:[{value:'fixture/model',label:'Fixture model'}]}});return;
                }
                await route.continue();
            });
            await page.evaluate(()=>{location.hash='model-config/openrouter'});
            await page.waitForSelector('#modelConfigurationSection',{state:'visible'});
            await page.locator('#cfg_decision_provider_openrouter_OPENROUTER_API_KEY').fill('fixture-only-not-a-credential');
            await page.locator('#plugin_decision_provider_openrouter').getByRole('button',{name:'刷新可选列表'}).click();
            await page.waitForFunction(id=>[...document.getElementById(id).options].some(option=>option.value==='fixture/model'),apiModel.slice(1));
            await page.locator(apiModel).selectOption('fixture/model');
            await page.locator('#plugin_decision_provider_openrouter').getByRole('button',{name:'保存配置',exact:true}).click();
            await page.waitForFunction(()=>document.getElementById('pluginStatus_decision_provider_openrouter').textContent.includes('配置已保存'));
            const saved=writes.find(w=>w.url==='/api/plugins/config');
            assert.equal(saved.body.name,'openrouter');
            assert.equal(saved.body.values.OPENROUTER_MODEL,'fixture/model');
            assert.equal(saved.body.values.OPENROUTER_API_KEY,'fixture-only-not-a-credential');
            assert(!saved.body.clear_secrets.includes('OPENROUTER_API_KEY'));
            failCatalog=true;
            await page.locator('#plugin_decision_provider_openrouter').getByRole('button',{name:'刷新可选列表'}).click();
            await page.waitForSelector(apiModel+'_note.danger');
            assert((await page.locator(apiModel+'_note').innerText()).includes('OPENROUTER_HTTP_PROXY'));
            const proxy='#cfg_decision_provider_openrouter_OPENROUTER_HTTP_PROXY';
            assert.equal(await page.locator(proxy).inputValue(),'INHERIT');
            failCatalog=false;
            await page.locator('#plugin_decision_provider_openrouter').getByRole('button',{name:'刷新可选列表'}).click();
            await page.waitForFunction(id=>!document.getElementById(id).classList.contains('danger'),apiModel.slice(1)+'_note');
            await page.evaluate(()=>{location.hash='models'});
            const configWritesBefore=writes.filter(w=>w.url==='/api/plugins/config').length;
            const inlineCli=page.locator('#control_decision_provider_openrouter .cli-picker select');
            await inlineCli.selectOption('CLAUDE');
            const cliSaveResponse=page.waitForResponse(response=>{
                if(!response.url().endsWith('/api/local'))return false;
                try{return response.request().postDataJSON().url==='/api/plugins/config'}catch{return false}
            });
            await page.locator('#control_decision_provider_openrouter .cli-picker').getByRole('button',{name:'保存 Agent CLI'}).click();
            await cliSaveResponse;
            const configWrites=writes.filter(w=>w.url==='/api/plugins/config');
            assert(configWrites.length>configWritesBefore);
            assert.equal(configWrites.at(-1).body.values.OPENROUTER_AGENT_CLI,'CLAUDE');
            assert(await page.locator('#openRouterPluginName').isVisible());
            await page.locator('#openRouterPluginName').fill('openrouter_ui');
            await page.getByRole('button',{name:'再添加一个 OpenRouter 配置'}).click();
            await page.waitForFunction(()=>document.getElementById('openRouterPluginStatus').textContent.includes('openrouter_ui.py'));
            assert.equal(writes.find(w=>w.url==='/api/plugins/openrouter/create').body.name,'openrouter_ui');
            await page.getByRole('button',{name:'保存模型启用与顺序'}).click();
            await page.waitForFunction(()=>document.getElementById('modelManageStatus').textContent.includes('启用状态和优先级已保存'));
            assert.equal(Object.keys(writes.find(w=>w.url==='/api/plugins/selection').body.enabled).length,8);
            await page.unroute('**/api/local');
            await page.evaluate(()=>{location.hash='decisions'});
            await page.waitForFunction(()=>document.querySelector('.nav-link[aria-current="page"]').hash==='#decisions');
            await page.waitForFunction(()=>!document.getElementById('decisions').hasAttribute('aria-busy'));
            await page.evaluate(()=>{LEDGER_TAB='concluded';LEDGER_RESULTS=new Set();renderDecisionLedger([{id:'ui-fixture',created_at:new Date().toISOString(),platform:'demo',market_topic_id:'Only a browser fixture',context:{market:{title:'Very long market '.repeat(20)}},final_decision:{action:'HOLD',rationale:'Evidence is incomplete; do not place an order.'},proposed_decision:{action:'HOLD',rationale:'Need research'},status:'NO_ACTION',group:'concluded',result:'HOLD',agent_steps:2,research:[{source:'fixture'}]}])});
            await page.locator('.decision-entry>summary').click();
            assert.equal(await page.locator('.ledger-four>dt').count(),4);
            await page.locator('.ledger-more>summary').first().click();
            assert(await page.locator('.decision-entry').innerText().then(s=>s.includes('观望')&&s.includes('Evidence is incomplete')));
            assert.equal(await page.evaluate(()=>decisionStatusTitle('RISK_REJECTED')),'规则拒绝，未执行');
            assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
            await page.evaluate(async()=>{document.activeElement?.blur();window.scrollTo(0,0);await new Promise(requestAnimationFrame)});
            if(output)await page.screenshot({path:path.join(output,`decisions-${width}.png`),fullPage:true});
            await page.evaluate(()=>{location.hash='settings'});
            await page.waitForSelector('#egressRoute');
            assert.equal(await page.locator('#egressResults .diagnostic-detail[open]').count(),0);
            assert.equal(await page.locator('#egressResults h4').count(),0);
            assert(!(await page.locator('#egressRoute').innerText()).includes('decision_provider:'));
            if(output)await page.screenshot({path:path.join(output,`network-${width}.png`),fullPage:true});
            await page.evaluate(()=>{location.hash='overview'});
            if(output)await page.screenshot({path:path.join(output,`dashboard-${width}.png`),fullPage:true});
            for(const name of ['codex','claude']){
                await page.evaluate(name=>{
                    showLoginWizard({kind:'decision_provider',name,status:{state:'authorizing',login_mode:'remote',flow_id:'ui-fixture',authorization_url:'https://'+(name==='codex'?'auth.openai.com/codex/device':'claude.com/cai/oauth/authorize'),device_code:'ABCD-EFGH',message:'等待授权'}});
                    /* The fixture's regular account poll always says "authenticated". Detach this
                       synthetic dialog after drawing it so that unrelated polling cannot replace
                       the remote-login state halfway through these visibility assertions. */
                    ACTIVE_LOGIN=null;
                },name);
                assert(await page.locator('#remoteLoginSteps').isVisible());
                assert.equal(await page.locator('#localLoginSteps').isVisible(),false);
                assert.equal(await page.locator('#helperCommand').isVisible(),false);
                assert.equal(await page.locator('#deviceCodeStep').isVisible(),name==='codex');
                if(output)await page.screenshot({path:path.join(output,`login-${name}-${width}.png`)});
                await page.evaluate(()=>document.getElementById('loginWizard').close());
            }
            assert(await page.evaluate(()=>browserNeedsDesktopHelper({userAgent:'iPhone',platform:'iPhone'})));
            assert(await page.evaluate(()=>browserNeedsDesktopHelper({userAgent:'Macintosh',platform:'MacIntel',maxTouchPoints:5})));
            assert.equal(await page.evaluate(()=>browserNeedsDesktopHelper({userAgent:'Linux x86_64',platform:'Linux',maxTouchPoints:0})),false);
            assert.equal(await page.evaluate(()=>preferredLoginMethod({userAgent:'desktop'},'127.0.0.1')),'local');
            assert.equal(await page.evaluate(()=>preferredLoginMethod({userAgent:'desktop'},'robot.example')),'remote');
            assert.equal(await page.evaluate(()=>preferredLoginMethod({userAgent:'desktop'},'192.168.1.2')),'remote');
            assert.equal(await page.evaluate(()=>preferredLoginMethod({userAgent:'iPhone'},'localhost')),'remote');
            assert.equal(await page.evaluate(()=>{LOGIN_METHODS.set('codex','remote');return loginAction('codex')}),'login_remote');
            assert.equal(await page.evaluate(()=>{LOGIN_METHODS.set('codex','local');return loginAction('codex')}),'login');
            assert.deepEqual(errors,[]);
            console.log(`${width}px: navigation, forms, tables, remote login dialogs, no JS errors: passed`);
            await context.close();
        }
    } finally {await browser.close()}
})().catch(error=>{console.error(error);process.exitCode=1});
