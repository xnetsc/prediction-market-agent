/* Read-only browser acceptance against a management-only test deployment. */
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const path = require('node:path');
const fs = require('node:fs');

(async () => {
    const base = process.env.DASHBOARD_TEST_URL || 'http://127.0.0.1:18765';
    const output = process.env.DASHBOARD_SCREENSHOTS;
    // Read a public catalog independently of the deployment's editable account/proxy settings.
    const publicResponse=await fetch('https://openrouter.ai/api/v1/models',{signal:AbortSignal.timeout(20000)});
    assert(publicResponse.ok);
    const publicCatalog=(await publicResponse.json()).data.map(item=>({value:item.id,label:item.name||item.id}));
    if (output) fs.mkdirSync(output, {recursive:true});
    const browser = await chromium.launch({channel:'chrome', headless:true});
    try {
        for (const width of [390, 768, 1440]) {
            const context = await browser.newContext({viewport:{width,height:1000},reducedMotion:'reduce'});
            const page = await context.newPage();
            let remoteModel=null;
            await page.route('**/api/local',async route=>{
                const message=route.request().postDataJSON();
                if(message.url==='/api/plugins/config/choices'&&message.body.name==='openai_compatible'){
                    await route.fulfill({json:{items:publicCatalog}});return;
                }
                if(message.url==='/api/plugins/manage'&&remoteModel!==null){
                    const response=await route.fetch(),manifest=await response.json();
                    const plugin=manifest.plugins.decision_provider.find(p=>p.name==='openai_compatible');
                    plugin.configuration.fields.find(f=>f.name==='COMPATIBLE_MODEL').value=remoteModel;
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
            if(output)await page.screenshot({path:path.join(output,`setup-${width}.png`),fullPage:true});
            for (const view of ['overview','models','plugins','decisions','settings','security']) {
                await page.evaluate(view => {location.hash=view}, view);
                await page.waitForFunction(view => document.querySelector('.nav-link[aria-current="page"]').hash==='#'+view, view);
                assert(await page.locator(`[data-view="${view}"]`).first().isVisible());
                const overflow = await page.evaluate(() => document.documentElement.scrollWidth>innerWidth);
                assert.equal(overflow,false,`${width}px ${view} must not overflow the viewport`);
                if(view==='settings'){
                    await page.waitForSelector('#egressRoute option',{state:'attached'});
                    assert(await page.locator('#environmentPanel').isVisible());
                    assert.equal(await page.locator('#egressRoute option[value="inherited"]').count(),1);
                    assert.equal(await page.locator('#egressComparison .egress-compare-card').count(),2);
                    assert(await page.locator('#sharedProxySettings').isVisible());
                    assert.equal(await page.locator('[data-app-field="shared_http_proxy"]').count(),1);
                    assert.equal(await page.locator('[data-app-field="shared_http_proxy"]').inputValue(),'HOST');
                    assert((await page.locator('[data-view="settings"]').allInnerTexts()).join('\n').includes('兼容 API'));
                    await page.locator('#environmentFacts summary').click();
                    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
                    if(output)await page.screenshot({path:path.join(output,`environment-${width}.png`)});
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
            assert.equal(await page.locator('.category-tile').count(),5);
            assert.equal(await page.locator('.category-tile').filter({hasText:'AI 模型服务'}).count(),0);
            assert.equal(await page.locator('#pluginCategoryNav a[href="#plugins/decision_provider"]').count(),0);
            assert.equal(await page.locator('#pluginManager .plugin-category:visible').count(),0);
            if(output)await page.screenshot({path:path.join(output,`plugins-${width}.png`),fullPage:true});
            for(const kind of ['api','decision_strategy','research_tool','risk','hook']){
                await page.evaluate(kind=>{location.hash='plugins/'+kind},kind);
                await page.waitForSelector('#category_'+kind,{state:'visible'});
                assert.equal(await page.locator('#pluginManager .plugin-category:visible').count(),1);
                assert(await page.locator('#pluginCategoryGuide').innerText().then(s=>s.includes('你需要做什么')));
                if(kind==='decision_strategy'){
                    const original=await page.locator('input[name="strategy"]:checked').getAttribute('value');
                    assert(await page.getByLabel('使用中性候选透传').isVisible());
                    if(original==='')await page.locator('input[name="strategy"][value="general_agent"]').check();
                    await page.getByLabel('使用中性候选透传').check();
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
            await page.waitForSelector('#control_decision_provider_openai_compatible');
            assert.equal(await page.locator('.client-options[open]').count(),0);
            const modelEnable=page.locator('#control_decision_provider_openai_compatible .model-enable');
            assert(await modelEnable.isVisible());
            await modelEnable.setChecked(!(await modelEnable.isChecked()));
            assert((await page.locator('#modelSelectionStatus_openai_compatible').innerText()).includes('尚未保存'));
            await modelEnable.setChecked(!(await modelEnable.isChecked()));
            if(output)await page.screenshot({path:path.join(output,`models-${width}.png`),fullPage:true});
            for(const name of ['codex','claude']){
                await page.evaluate(name=>{location.hash='model-config/'+name},name);
                const prefix='#cfg_decision_provider_'+name+'_'+name.toUpperCase();
                await page.waitForSelector(prefix+'_MODEL[data-loaded="true"]',{timeout:40000});
                assert.equal(await page.locator(prefix+'_MODEL').evaluate(e=>e.tagName),'SELECT');
                assert((await page.locator(prefix+'_MODEL option').count())>1);
                assert(await page.locator(prefix+'_HTTP_PROXY').isVisible());
                assert.equal(await page.locator(prefix+'_HTTP_PROXY').inputValue(),'INHERIT');
                assert((await page.locator('#plugin_decision_provider_'+name+' .config-section-heading').innerText()).includes('网络连接'));
                const model=await page.locator(prefix+'_MODEL option').nth(1).getAttribute('value');
                await page.locator(prefix+'_MODEL').selectOption(model);
                await page.waitForFunction(id=>!document.getElementById(id+'_note').textContent.includes('正在读取'),prefix.slice(1)+'_EFFORT');
                assert.equal(await page.locator(prefix+'_EFFORT').evaluate(e=>e.tagName),'SELECT');
                assert((await page.locator(prefix+'_EFFORT').innerText()).includes('默认'));
                if(output)await page.screenshot({path:path.join(output,`model-options-${name}-${width}.png`),fullPage:true});
            }
            await page.evaluate(()=>{location.hash='models'});
            await page.locator('#control_decision_provider_openai_compatible a').click();
            await page.waitForSelector('#cfg_decision_provider_openai_compatible_COMPATIBLE_API_BASE');
            assert(await page.locator('#cfg_decision_provider_openai_compatible_COMPATIBLE_API_KEY').isVisible());
            const credential=page.locator('#cfg_decision_provider_openai_compatible_COMPATIBLE_API_KEY');
            assert.equal(await credential.getAttribute('type'),'search');
            assert.equal(await credential.getAttribute('autocomplete'),'off');
            assert.equal(await credential.evaluate(e=>getComputedStyle(e).webkitTextSecurity),'disc');
            assert.equal(await page.locator('#cfg_decision_provider_openai_compatible_COMPATIBLE_API_SECRET').count(),0);
            assert(await page.locator('#cfg_decision_provider_openai_compatible_COMPATIBLE_MODEL').isVisible());
            const apiModel='#cfg_decision_provider_openai_compatible_COMPATIBLE_MODEL';
            await page.waitForSelector(apiModel+'[data-loaded="true"]',{timeout:30000});
            assert.equal(await page.locator(apiModel).getAttribute('role'),'combobox');
            assert.equal(await page.locator(apiModel).getAttribute('type'),'search');
            assert.equal(await page.locator(apiModel).getAttribute('autocomplete'),'off');
            assert.equal(await page.locator(apiModel+'_choices_search').count(),0);
            const modelToggle=page.locator('#plugin_decision_provider_openai_compatible').getByRole('button',{name:'展开模型列表'});
            await modelToggle.click();
            assert(await page.locator(apiModel+'_choices').isVisible());
            assert.equal(await page.locator(apiModel).evaluate(e=>document.activeElement===e),false);
            await modelToggle.click();
            assert.equal(await page.locator(apiModel+'_choices').isVisible(),false);
            await page.locator(apiModel).click();
            const realModels=await page.locator(apiModel+'_choices [role=option]').count();
            assert(realModels>100,'OpenRouter should load its real public catalog without a button click');
            await page.locator(apiModel).fill('aNtHrOpIc sonnet');
            const matched=await page.locator(apiModel+'_choices [role=option]').evaluateAll(options=>options.map(o=>o.textContent.toLowerCase()));
            assert(matched.length>0&&matched.length<realModels);
            assert(matched.every(text=>text.includes('anthropic')&&text.includes('sonnet')));
            await page.locator(apiModel).press('ArrowDown');
            await page.locator(apiModel).press('Enter');
            assert((await page.locator(apiModel).inputValue()).includes('anthropic/'));
            assert.equal(await page.locator(apiModel).getAttribute('aria-expanded'),'false');
            await page.locator(apiModel).fill('no-such-model-keyword-fixture');
            assert((await page.locator(apiModel+'_note').innerText()).includes('没有匹配'));
            await page.locator(apiModel).press('Enter');
            assert.equal(await page.locator(apiModel).inputValue(),'no-such-model-keyword-fixture');
            await page.locator(apiModel).fill('');
            assert.equal(await page.locator(apiModel+'_choices [role=option]').count(),realModels);
            if(output)await page.screenshot({path:path.join(output,`api-model-search-${width}.png`),fullPage:true});
            await page.locator(apiModel).press('Escape');
            remoteModel='fixture/saved-current';
            await page.evaluate(()=>{location.hash='models'});
            await page.waitForSelector('#modelConfigurationSection',{state:'hidden'});
            await page.evaluate(()=>{location.hash='model-config/openai_compatible'});
            await page.waitForFunction(id=>document.getElementById(id).value==='fixture/saved-current',apiModel.slice(1));
            assert((await page.locator('#plugin_decision_provider_openai_compatible .config-load-status').innerText()).includes('已加载当前'));
            await page.locator(apiModel).fill('fixture/unsaved-draft');
            remoteModel='fixture/changed-elsewhere';
            await page.evaluate(()=>{location.hash='models'});
            await page.waitForSelector('#modelConfigurationSection',{state:'hidden'});
            await page.evaluate(()=>{location.hash='model-config/openai_compatible'});
            await page.waitForFunction(()=>document.querySelector('#plugin_decision_provider_openai_compatible .config-load-status').textContent.includes('保留了'));
            assert.equal(await page.locator(apiModel).inputValue(),'fixture/unsaved-draft');
            remoteModel=null;
            assert.equal(await page.locator('#cfg_decision_provider_openai_compatible_COMPATIBLE_API_KEY').inputValue(),'');
            assert(await page.locator('#plugin_decision_provider_openai_compatible .preset-slot').innerText().then(s=>s.includes('OpenRouter')));
            // The configuration form exists only in the dedicated model-service page.
            await page.locator('#cfg_decision_provider_openai_compatible_COMPATIBLE_MODEL').fill('ui-unsaved-model');
            await page.evaluate(()=>{location.hash='models'});
            await page.waitForSelector('#modelConfigurationSection',{state:'hidden'});
            await page.evaluate(()=>{location.hash='model-config/openai_compatible'});
            await page.waitForSelector('#modelConfigurations #category_decision_provider',{state:'visible'});
            assert.equal(await page.locator('#cfg_decision_provider_openai_compatible_COMPATIBLE_MODEL').count(),1);
            assert.equal(await page.locator('#cfg_decision_provider_openai_compatible_COMPATIBLE_MODEL').inputValue(),'ui-unsaved-model');
            // Exercise the real form handlers with browser-local write responses only.
            // Never submit fixture credentials or selections to the running deployment.
            const writes=[];
            let failCatalog=false;
            await page.route('**/api/local',async route=>{
                const message=route.request().postDataJSON();
                if(message.url==='/api/plugins/config'||message.url==='/api/plugins/selection'){
                    if(message.url==='/api/plugins/config')remoteModel=message.body.values.COMPATIBLE_MODEL;
                    writes.push(message);await route.fulfill({json:{}});return;
                }
                if(message.url==='/api/plugins/config/choices'){
                    if(failCatalog){await route.fulfill({status:503,json:{detail:'COMPATIBLE_HTTP_PROXY must be DIRECT, SYSTEM, or an http(s) URL'}});return}
                    writes.push(message);await route.fulfill({json:{items:[{value:'fixture/model',label:'Fixture model'}]}});return;
                }
                await route.continue();
            });
            await page.evaluate(()=>{location.hash='model-config/openai_compatible'});
            await page.waitForSelector('#modelConfigurationSection',{state:'visible'});
            page.once('dialog',dialog=>dialog.accept());
            await page.getByRole('button',{name:'OpenRouter',exact:true}).click();
            assert.equal(await page.locator('#cfg_decision_provider_openai_compatible_COMPATIBLE_API_BASE').inputValue(),'https://openrouter.ai/api/v1');
            await page.locator('#cfg_decision_provider_openai_compatible_COMPATIBLE_API_BASE').fill('https://model-fixture.example/v1');
            await page.locator('#cfg_decision_provider_openai_compatible_COMPATIBLE_API_KEY').fill('fixture-only-not-a-credential');
            await page.locator('#cfg_decision_provider_openai_compatible_COMPATIBLE_MODEL').fill('fixture/model');
            await page.locator('#plugin_decision_provider_openai_compatible').getByRole('button',{name:'保存配置',exact:true}).click();
            await page.waitForFunction(()=>document.getElementById('pluginStatus_decision_provider_openai_compatible').textContent.includes('配置已保存'));
            const saved=writes.find(w=>w.url==='/api/plugins/config');
            assert.equal(saved.body.name,'openai_compatible');
            assert.equal(saved.body.values.COMPATIBLE_API_BASE,'https://model-fixture.example/v1');
            assert.equal(saved.body.values.COMPATIBLE_API_KEY,'fixture-only-not-a-credential');
            assert(saved.body.clear_secrets.includes('COMPATIBLE_API_KEY'));
            await page.locator('#plugin_decision_provider_openai_compatible').getByRole('button',{name:'刷新模型列表'}).click();
            await page.locator(apiModel).click();
            await page.waitForSelector(apiModel+'_choices [role=option]');
            await page.locator(apiModel+'_choices [role=option]').filter({hasText:'Fixture model'}).click();
            assert.equal(await page.locator('#cfg_decision_provider_openai_compatible_COMPATIBLE_MODEL').inputValue(),'fixture/model');
            await page.locator(apiModel).click();
            await page.locator(apiModel).press('Escape');
            assert.equal(await page.locator(apiModel).getAttribute('aria-expanded'),'false');
            assert.equal(await page.locator(apiModel).inputValue(),'fixture/model');
            failCatalog=true;
            await page.evaluate(id=>CHOICE_ITEMS.delete(document.getElementById(id)),apiModel.slice(1));
            await page.locator('#plugin_decision_provider_openai_compatible').getByRole('button',{name:'刷新模型列表'}).click();
            await page.waitForSelector(apiModel+'_note.danger');
            assert((await page.locator(apiModel+'_note').innerText()).includes('模型列表暂不可用'));
            assert(!(await page.locator(apiModel+'_note').innerText()).includes('DIRECT, SYSTEM'));
            const proxy='#cfg_decision_provider_openai_compatible_COMPATIBLE_HTTP_PROXY';
            assert.equal(await page.locator(proxy).getAttribute('aria-invalid'),'true');
            assert((await page.locator(proxy).locator('xpath=ancestor::*[contains(concat(" ",normalize-space(@class)," ")," field ")]').innerText()).includes('当前代理设置无效'));
            await modelToggle.click();
            assert(await page.locator(apiModel+'_choices').isVisible());
            assert(!(await page.locator(apiModel+'_choices').innerText()).includes('DIRECT, SYSTEM'));
            await modelToggle.click();
            await page.getByRole('button',{name:'在表单中改为直连（DIRECT）'}).click();
            assert.equal(await page.locator(proxy).inputValue(),'DIRECT');
            assert.equal(await page.locator(proxy).getAttribute('aria-invalid'),null);
            assert((await page.locator('#modelManageStatus').innerText()).includes('保存配置'));
            assert.equal(await page.locator(apiModel).inputValue(),'fixture/model');
            failCatalog=false;
            await page.locator('#plugin_decision_provider_openai_compatible').getByRole('button',{name:'刷新模型列表'}).click();
            await page.waitForFunction(id=>!document.getElementById(id).classList.contains('danger')&&document.getElementById(id).textContent.includes('显示'),apiModel.slice(1)+'_note');
            await page.getByRole('button',{name:'保存模型启用与顺序'}).click();
            await page.waitForFunction(()=>document.getElementById('modelManageStatus').textContent.includes('启用状态和优先级已保存'));
            assert.equal(Object.keys(writes.find(w=>w.url==='/api/plugins/selection').body.enabled).length,6);
            await page.unroute('**/api/local');
            await page.evaluate(()=>{location.hash='decisions';renderDecisionLedger([{id:'ui-fixture',created_at:new Date().toISOString(),platform:'demo',market_topic_id:'Only a browser fixture',context:{market:{title:'Very long market '.repeat(20)}},final_decision:{action:'HOLD',rationale:'Evidence is incomplete; do not place an order.'},proposed_decision:{action:'HOLD',rationale:'Need research'},status:'failed',error:'Example request failed',agent_steps:2,research:[{source:'fixture'}]}])});
            await page.locator('.decision-entry>summary').click();
            assert.equal(await page.locator('.decision-timeline>li').count(),5);
            assert(await page.locator('.decision-entry').innerText().then(s=>s.includes('观望')&&s.includes('失败')&&s.includes('Evidence is incomplete')));
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
                await page.evaluate(name=>showLoginWizard({kind:'decision_provider',name,status:{state:'authorizing',login_mode:'remote',flow_id:'ui-fixture',authorization_url:'https://'+(name==='codex'?'auth.openai.com/codex/device':'claude.com/cai/oauth/authorize'),device_code:'ABCD-EFGH',message:'等待授权'}}),name);
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
