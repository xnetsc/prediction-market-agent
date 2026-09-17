/* Presentation only: configuration and actions still use plugin-owned callbacks. */
const CATEGORY_HELP = {
    api: {title:'交易平台', role:'连接市场与账户', description:'读取市场、持仓和订单，并接收机器人发出的操作。平台自己决定什么时候扫描，发现机会后通知机器人。', steps:['扫描市场','提交发现','接收查询或订单'], next:'启用你要使用的平台，填写它要求的账户、网络与扫描配置。不使用的平台保持关闭。'},
    decision_provider: {title:'AI 模型服务', role:'让模型理解信息并做判断', description:'提供实际执行推理的模型。客户端账号和兼容 API 是并列方式，与“采用什么交易策略”不是一回事。', steps:['接收策略与证据','调用所选模型','返回判断或工具请求'], next:'至少配置一种可用服务。顺序数字越小越先尝试，不可用时再尝试下一个；不会同时向所有服务发请求。'},
    decision_strategy: {title:'决策策略', role:'可选地告诉模型如何分析', description:'定义模型需要关注的证据、判断过程和输出要求。它是可选分析方法，不是模型账号，也不是机器人启动条件。', steps:['选择插件或使用内置策略','读取策略文本与实测叠加层','形成交易建议'], next:'可安装并选择一项策略插件；不选时由内置决策策略工作，它的当前全文可在下方导出。'},
    market_discovery: {title:'标的发现策略', role:'决定每轮先看哪些标的', description:'机器人每轮只能深入分析少数标的。发现策略决定把这几个名额给谁：宽扫平台、按实测结果排序、再由模型挑最终名单。未安装插件时使用内置策略。', steps:['宽扫平台全部标的','按实测优先级排序','模型挑出本轮名单'], next:'不装插件也在工作。要用自己的发现逻辑再安装插件；内置策略的当前全文可在下方导出查看。'},
    research_tool: {title:'信息与研究', role:'帮助模型补充证据', description:'在已有市场数据不够时，让模型主动查询外部资料。是否调用、查询什么，由当次决策过程决定。', steps:['模型提出问题','工具收集信息','结果回到决策'], next:'启用需要的信息工具，并补齐其访问配置。工具可用不代表每次都会被调用。'},
    agent_policy: {title:'Agent 行为风控', role:'管住模型发起的每一次工具调用', description:'模型每次调用工具都要经过这里。工具里既有市场 API（下单、买卖），也有网页搜索、执行命令等其它工具。它问的是“这个 Agent 被允许发起这类调用吗”，不问这笔动作的业务后果。', steps:['模型提出调用','逐个询问已启用的插件','任意一个拒绝即整体失败'], next:'出厂不启用。可以同时启用多个，它们按启用顺序串成一条链：全部通过才放行；任意一个拒绝、或者它自己抛异常，这次调用就失败。'},
    risk: {title:'业务风控', role:'管住一切市场 API 动作', description:'审核每一次市场 API 调用——下单、撤单、赎回、转账这些写动作，以及查行情、查订单簿这些只读调用，与是谁发起的无关：模型提的、结算扫单产生的、手动触发的都一样。同一笔下单会先后经过 Agent 行为风控和业务风控两道检查，这是有意的重复。', steps:['接收市场 API 动作','逐个询问已启用的插件','任意一个拒绝即整体失败'], next:'出厂不启用任何业务风控，而且框架里根本没有内置的仓位上限或止损——要限额，要么在这里启用你自己的规则插件，要么把标准写进决策策略文本让模型读账执行。可以同时启用多个，它们按启用顺序串成一条链：全部通过才放行；任意一个拒绝、或者它自己抛异常，这次动作就失败。不要把“已启用”理解成已经配置了止损或保证不会亏损。'},
};
const SERVICE_TITLES = {codex:'Codex',claude:'Claude',openai_compatible:'兼容 API · OpenRouter / 自定义'};
const PLUGIN_CENTER_KINDS = ['api','market_discovery','decision_strategy','research_tool','agent_policy','risk'];
const STRATEGY_LANES = {market_discovery:'discovery', decision_strategy:'decision'};
const serviceTitle = name => SERVICE_TITLES[name] || name || '没有服务名';
function downloadCredentialBundle(name,status){
    const bundle=status?.credential_export;
    if(!bundle)throw Error('服务端没有返回凭据内容');
    const stamp=new Date().toISOString().slice(0,19).replace(/[:T]/g,'-');
    const url=URL.createObjectURL(new Blob([JSON.stringify(bundle,null,2)],{type:'application/json'}));
    const link=document.createElement('a');link.href=url;link.download=name+'-credentials-'+stamp+'.json';
    document.body.appendChild(link);link.click();link.remove();
    setTimeout(()=>URL.revokeObjectURL(url),10000);
    document.getElementById('clientControlError').textContent='';
}
function renderProviderHealth(status){
    const host=document.getElementById('providerHealth');if(!host)return;
    const health=status?.decision_provider_health,rows=health?.providers||[];
    if(!rows.length){host.innerHTML='<p class="muted">还没有调用记录。机器人跑起来后，这里显示每个模型服务的可用状态、限流退避剩余时间和实测质量。</p>';return}
    const measured=health.measured||{};
    host.innerHTML='<div class="table-scroll"><table><thead><tr><th>服务</th><th>状态</th><th>成功率</th><th>校准 Brier</th><th>他评</th><th>质量权重</th><th>最近失败</th></tr></thead><tbody>'
        +rows.map(r=>{const m=measured[r.provider]||{};
            const state=r.available?'<span class="badge ready">可用</span>':'<span class="badge">退避中 '+r.cooldown_seconds_remaining+'s</span>';
            return '<tr><td>'+esc(serviceTitle(r.provider))+'</td><td>'+state+'</td><td>'+(r.attempts?Math.round(r.success_rate*100)+'%':'—')
                +'</td><td>'+(m.brier_score??'—')+(m.settled_decisions?' <span class="muted">n='+m.settled_decisions+'</span>':'')
                +'</td><td>'+(m.peer_average??'—')+(m.peer_reviews?' <span class="muted">n='+m.peer_reviews+'</span>':'')
                +'</td><td>'+esc(String(r.quality??'—'))+'</td><td>'+(r.last_error_kind?esc(r.last_error_kind)+'：'+esc(String(r.last_error).slice(0,80)):'—')+'</td></tr>'}).join('')
        +'</tbody></table></div><p class="muted">限流或掉线的服务按失败类型退避，退避结束自动放行一次探测，恢复即回到轮换。质量权重由送达率、已结算校准和他评合成，样本不足时向 1.0 收缩；服务给自己打的分不计入。</p>';
}
function strategyLanePanel(kind,m){
    const lane=STRATEGY_LANES[kind],on=m.strategy_evolution!==false;
    return '<article class="plugin configuration-card strategy-lane" id="lane_'+kind+'">'
        +'<div class="section-heading"><h4>策略进化与导出</h4><span class="badge ready">内置策略持续进化</span></div>'
        +'<p class="muted">未安装插件时由内置策略工作。内置策略不作为可编辑配置提供，但它实际发给模型的全文可以随时导出查看。它按已完成决策的实测结果自动调整，并且在你使用自己的插件期间也继续测量，所以切回来时不会是旧的。</p>'
        +'<div class="selection-row"><label><input type="checkbox" id="evolutionToggle" '+(on?'checked':'')+'> 我自己的策略插件也自动进化</label></div>'
        +'<p class="muted">关闭后，你的插件只用你写的原文；框架学到的内容不再附加。你的策略文件任何时候都不会被改写，进化只是可随时关闭的叠加层。内置策略不受此开关影响。</p>'
        +'<div class="toolbar form-actions"><button onclick="exportStrategy(\''+lane+'\')">导出当前生效全文</button>'
        +'<span id="exportStatus_'+lane+'" class="status" role="status"></span></div>'
        +'<div id="exportResult_'+lane+'"></div></article>';
}
async function exportStrategy(lane){
    const status=document.getElementById('exportStatus_'+lane),target=document.getElementById('exportResult_'+lane);
    setOperationStatus(status,'正在导出…','pending');
    try{
        const result=await post('/api/strategies/export',{lane}),view=result.lanes[lane];
        const blob=new Blob([JSON.stringify(result,null,2)],{type:'application/json'});
        const parts=[section(view,'当前生效')];
        if(view.built_in)parts.push(section(view.built_in,'内置策略（在你的插件运行期间仍在进化）'));
        target.innerHTML=parts.join('')+'<p class="muted"><a download="strategy-'+lane+'.json" href="'+URL.createObjectURL(blob)+'">下载完整 JSON（含全部教训与测量表）</a></p>';
        setOperationStatus(status,'已导出 '+view.prompt_chars+' 字符的策略全文');
    }catch(e){setOperationStatus(status,e.message,'danger')}
    function section(v,title){
        return '<details class="diagnostic-detail"><summary>'+esc(title)+'：'+esc(v.strategy)+'（'+esc(v.source)+'，'+v.prompt_chars+' 字符，教训 '+v.lessons_applied.length+' 条生效 / '+v.lessons_all.length+' 条在库）</summary>'
            +'<pre class="export-text">'+esc(v.prompt_text)+'</pre></details>';
    }
}
const actionTitle = value => ({BUY:'买入',SELL:'卖出',HOLD:'观望',CANCEL:'撤单'}[value] || value || '尚未形成');
const decisionStatusTitle = value => ({STARTED:'分析中',PROVIDER_ERROR:'模型调用失败',RISK_REJECTED:'规则拒绝，未执行',EXECUTION_ERROR:'执行失败',COMPLETED:'流程已完成',HOLD:'观望，未下单',ERROR:'出现错误',FAILED:'失败',PENDING:'处理中',EXECUTED:'已提交执行',REJECTED:'被拒绝'}[String(value).toUpperCase()]||(value?'平台状态：'+value:'没有状态'));
function managementFeedback(){return document.getElementById(location.hash.startsWith('#model')?'modelManageStatus':'manageStatus')}
function processStrip(steps){return '<ol class="process-strip">'+steps.map(s=>'<li>'+esc(s)+'</li>').join('')+'</ol>'}
function configLink(kind,name){return kind==='decision_provider'?'#model-config/'+encodeURIComponent(name):'#plugin_'+kind+'_'+name}
function configurationFieldsHtml(kind,name,fields){
    if(kind!=='decision_provider')return '<div class="config-fields">'+fields.map(f=>fieldHtml(kind,name,f)).join('')+'</div>';
    const primary=fields.filter(f=>/(?:_MODEL|_EFFORT|_HTTP_PROXY|_TIMEOUT_SECONDS|_API_BASE|_API_KEY|_EXTRA_HEADERS_JSON)$/.test(f.name));
    const advanced=fields.filter(f=>!primary.includes(f));
    const primaryHtml='<div class="config-field-section"><div class="config-section-heading"><h5>模型、凭据与网络连接</h5><p>代理设置就在本区：INHERIT 跟随统一代理，DIRECT 直连，也可以填写该服务专用的完整 HTTP(S) 地址。</p></div><div class="config-fields">'+primary.map(f=>fieldHtml(kind,name,f)).join('')+'</div></div>';
    return primaryHtml+(advanced.length?'<details class="client-advanced-settings"><summary>客户端文件、登录与升级高级设置 · '+advanced.length+' 项</summary><div class="config-fields">'+advanced.map(f=>fieldHtml(kind,name,f)).join('')+'</div></details>':'');
}

/* Something a plugin needs a person for is worthless if the person is on another page.
   Every plugin that can speak is asked directly - the framework has no idea what any of it
   means and does not cache a judgement about it - and whatever comes back is listed here with
   a link straight to the card that raised it. The banner is outside the per-view sections, so
   it is on screen whichever page the operator is looking at. */
async function refreshAttention(m){
    const host=document.getElementById('attention');
    if(!host)return;
    const asks=[];
    for(const kind of KINDS)for(const p of m.plugins[kind]||[]){
        if(!p.enabled||!p.has_notices)continue;
        asks.push(post('/api/plugins/notices',{kind,name:p.name})
            .then(r=>({kind,plugin:p,notices:r.notices||[],error:r.error}))
            .catch(e=>({kind,plugin:p,notices:[],error:e.message})));
    }
    const answers=await Promise.all(asks);
    host.replaceChildren();
    const items=[];
    for(const a of answers){
        const target='#plugin_'+a.kind+'_'+a.plugin.name;
        if(a.error)items.push({target,label:a.plugin.name,title:'插件状态读取失败：'+a.error,act:false});
        for(const n of a.notices)items.push({
            target,label:a.plugin.name,title:n.title,
            act:Boolean(n.action_label||n.dismiss_label),
        });
    }
    host.hidden=!items.length;
    if(!items.length)return;
    const actionable=items.filter(i=>i.act).length;
    const head=controlNode('div','',host);head.className='attention-head';
    controlNode('strong',actionable?'有 '+actionable+' 件事需要你处理':'有 '+items.length+' 条插件消息',head);
    controlNode('span',actionable&&actionable<items.length?'另有 '+(items.length-actionable)+' 条只是通知':'',head).className='muted';
    const list=controlNode('ul','',host);list.className='attention-list';
    for(const item of items){
        const row=controlNode('li','',list);
        controlNode('span',item.label+' · '+item.title,row);
        const go=controlNode('a',item.act?'去处理 →':'去查看 →',row);
        go.href=item.target;go.className='attention-go';
    }
}

function renderManager(m) {
    LAST_MANAGER=m;
    // The provider form has only one DOM instance, even when reached from two pages.
    document.getElementById('modelConfigurations').replaceChildren();
    const manager=document.getElementById('pluginManager');manager.replaceChildren();
    document.getElementById('pluginCategoryHome').innerHTML='<h3>给机器人选择扩展能力</h3><p class="muted">交易平台、策略、研究，以及 Agent 行为风控和业务风控这两类过滤插件，都在这里管理。AI 账号、兼容 API、模型与调用顺序统一放在左侧“模型服务”，不在插件中心重复出现。</p>'+processStrip(['平台发现市场','策略 + 模型分析','工具补充证据','行为与风控检查','平台执行'])+'<div class="category-grid">'+PLUGIN_CENTER_KINDS.map(kind=>{const h=CATEGORY_HELP[kind],all=m.plugins[kind]||[],on=all.filter(p=>p.enabled).length;return '<a class="category-tile" href="#plugins/'+kind+'"><span class="eyebrow">'+esc(on+' / '+all.length+' 已启用')+'</span><h4>'+h.title+' <span aria-hidden="true">→</span></h4><p>'+h.role+'</p></a>'}).join('')+'</div>';
    document.getElementById('pluginCategoryNav').innerHTML='<a href="#plugins">全部分类</a>'+PLUGIN_CENTER_KINDS.map(k=>'<a href="#plugins/'+k+'">'+LABELS[k]+'</a>').join('');
    refreshAttention(m);
    for(const kind of KINDS){
        const parent=kind==='decision_provider'?document.getElementById('modelConfigurations'):manager;
        const group=controlNode('div','',parent);group.id='category_'+kind;group.className='plugin-category';group.dataset.kind=kind;
        const grid=controlNode('div','',group);grid.className='config-grid';
        if(STRATEGY_LANES[kind])group.insertAdjacentHTML('afterbegin',strategyLanePanel(kind,m));
        if(kind==='decision_strategy'){
            const none=controlNode('article','',grid);none.className='plugin configuration-card strategy-none';
            const selected=!(m.plugins[kind]||[]).some(p=>p.enabled);
            none.innerHTML='<div class="section-heading"><h4>使用内置决策策略</h4><span class="badge '+(selected?'ready':'')+'">'+(selected?'当前选择':'可选择')+'</span></div><div class="selection-row"><label><input type="radio" name="strategy" data-kind="decision_strategy" data-name="none" value="" '+(selected?'checked':'')+'> 使用内置默认策略</label></div><p id="selectionStatus_decision_strategy_none" class="status selection-status" role="status"></p><p class="muted">不安装插件时由内置决策策略工作，它按已结算结果校准自己的概率估计。内置策略不作为可编辑配置提供，全文可在上方导出查看。</p>';
        }
        for(const p of [...(m.plugins[kind]||[])].sort((a,b)=>(a.priority??999)-(b.priority??999))){
            const card=controlNode('article','',grid);card.id='plugin_'+kind+'_'+p.name;card.className='plugin configuration-card'+(p.enabled?'':' off');
            const label=kind==='decision_provider'&&p.enabled?serviceTitle(p.name):p.name;
            const ready=p.readiness?.ready;
            const selector=kind==='decision_provider'?'':kind==='decision_strategy'?'<label><input type="radio" name="strategy" data-kind="'+kind+'" data-name="'+esc(p.name)+'" value="'+esc(p.name)+'" '+(p.enabled?'checked':'')+'> 使用此策略</label>':'<label><input class="enable" type="checkbox" data-kind="'+kind+'" data-name="'+esc(p.name)+'" '+(p.enabled?'checked':'')+'> 启用</label><label>顺序 <input aria-label="'+esc(p.name)+' 顺序" class="priority" type="number" min="1" data-kind="'+kind+'" data-name="'+esc(p.name)+'" value="'+esc(p.priority??99)+'"></label>';
            card.innerHTML='<div class="section-heading"><h4>'+esc(label)+'</h4><span class="badge '+(p.enabled&&ready?'ready':'')+'">'+(!p.enabled?'未启用':ready?'已就绪':'待检查 / 配置')+'</span></div>'+(selector?'<div class="selection-row">'+selector+'</div><p id="selectionStatus_'+kind+'_'+esc(p.name)+'" class="status selection-status" role="status"></p>':'')+'<p class="muted">'+esc(p.enabled?p.description:'尚未加载。请先在“模型服务”启用并保存，才能查看此服务的配置。')+'</p>';
            if(p.enabled&&p.readiness&&!ready)card.insertAdjacentHTML('beforeend','<details class="diagnostic-detail"><summary>为什么还未就绪？</summary><p>'+esc(p.readiness.reasons.join('；'))+'</p></details>');
            /* has_notices is what the summary carries; p.notices never existed, so this
               gate was always false and no plugin has ever shown a notice here. */
            if(p.enabled&&p.has_notices)renderPluginNotices(card,kind,p);
            if(p.enabled){
                const fields=p.configuration?.fields;
                card.insertAdjacentHTML('beforeend','<details class="plugin-config"><summary>编辑配置'+(fields?' · '+fields.length+' 项':'')+'</summary><div class="preset-slot"></div><p class="description">带 * 为必填。密钥留空会保留已保存值；删除需要明确操作。保存后将重新检查运行条件。</p>'+(fields?configurationFieldsHtml(kind,p.name,fields):'<p>此插件无需配置。</p>')+(fields?'<div class="toolbar form-actions"><button class="primary" onclick="savePluginConfig(\''+kind+'\',\''+p.name+'\')">保存配置</button><details class="inline-menu"><summary>重置配置</summary><button class="danger" onclick="deletePluginConfig(\''+kind+'\',\''+p.name+'\')">删除配置并恢复默认</button></details><span id="pluginStatus_'+kind+'_'+esc(p.name)+'" class="status plugin-card-status" role="status"></span></div>':'')+'</details>');
            }
            card.insertAdjacentHTML('beforeend','<details class="diagnostic-detail"><summary>文件信息</summary><p>'+esc(p.origin)+'</p></details>');
            for(const input of card.querySelectorAll('[data-field]'))input.dataset.savedValue=JSON.stringify(input.type==='checkbox'?input.checked:input.value);
            if(kind==='decision_provider'&&p.enabled)card.insertAdjacentHTML('afterbegin','<p class="description config-load-status" role="status"></p>');
        }
        if(!(m.plugins[kind]||[]).length)grid.innerHTML='<div class="empty-state"><strong>此类别还没有插件</strong><p>不需要这项能力时无需操作；也可以在下方安装可信插件。</p></div>';
    }
    document.getElementById('installKind').innerHTML=PLUGIN_CENTER_KINDS.map(k=>'<option value="'+k+'">'+LABELS[k]+'</option>').join('');
    document.getElementById('modelInstallTarget').innerHTML=(m.plugin_directories.categories.decision_provider||[]).map(d=>'<option value="'+esc(d)+'">'+esc(d)+'</option>').join('');
    manager.onchange=event=>{const input=event.target;if(input.matches('.enable,.priority,input[name="strategy"]'))markPluginSelectionDirty(input)};
    renderInstallTargets();renderConfigurationPresets(m);
    window.showDashboardView?.();renderServiceConnections();
}

const CONFIG_READS=new WeakMap();
async function loadCurrentModelConfig(name){
    const card=document.getElementById('plugin_decision_provider_'+name),note=card?.querySelector('.config-load-status');
    if(!note)return;
    const ticket={};CONFIG_READS.set(card,ticket);note.textContent='正在读取当前保存的配置…';note.className='description config-load-status';
    try{
        const manifest=await get('/api/plugins/manage');
        if(CONFIG_READS.get(card)!==ticket||!card.isConnected)return;
        const plugin=manifest.plugins.decision_provider.find(p=>p.name===name);
        if(!plugin?.configuration)throw Error('插件已禁用或不存在，请刷新插件列表。');
        let draft=false,changed=false;
        for(const field of plugin.configuration.fields){
            const input=document.getElementById('cfg_decision_provider_'+name+'_'+field.name);if(!input)continue;
            const current=input.type==='checkbox'?input.checked:input.value;
            if(JSON.stringify(current)!==input.dataset.savedValue||input.dataset.clearSecret==='true'){draft=true;continue}
            const value=input.type==='checkbox'?Boolean(field.value):String(field.value??'');
            changed=changed||current!==value;
            if(input.tagName==='SELECT'&&![...input.options].some(o=>o.value===value)){const option=controlNode('option',value||'使用客户端默认',input);option.value=value}
            if(input.type==='checkbox')input.checked=value;else input.value=value;
            if(field.sensitive)input.placeholder=field.configured?'已保存；留空保持不变':'';
            input.dataset.savedValue=JSON.stringify(value);
        }
        note.textContent=draft?'已读取服务器当前配置；保留了本页未保存修改。保存后才会生效。':'已加载当前保存的配置；密钥只显示是否已保存，不回显内容。';
        if(changed&&!draft)for(const input of card.querySelectorAll('[data-dynamic-list],[data-selection-only]')){
            if(input.dataset.dynamicList)loadFieldChoices('decision_provider',name,input.dataset.field);
            else loadSelectionChoices('decision_provider',name,input.dataset.field);
        }
    }catch(e){if(CONFIG_READS.get(card)===ticket&&card.isConnected){note.textContent='读取当前配置失败：'+e.message+'；保留原有输入，请重新打开页面重试。';note.className='description config-load-status danger'}}
}
let CLIENT_STATUS=[];
function renderServiceConnections(){
    const root=document.getElementById('clientControls');
    if(root.contains(document.activeElement))return;
    const expanded=new Set([...root.querySelectorAll('details[open]')].map(d=>d.dataset.client));
    root.replaceChildren();
    const providers=[...(LAST_MANAGER?.plugins.decision_provider||[])].sort((a,b)=>(a.priority??999)-(b.priority??999));
    const names=[...new Set([...providers.map(p=>p.name),...CLIENT_STATUS.map(p=>p.name)])];
    for(const name of names){
        const plugin=providers.find(p=>p.name===name),item=CLIENT_STATUS.find(p=>p.name===name),s=item?.status;
        const card=controlNode('article','',root);card.className='service-card';card.id='control_decision_provider_'+name;
        const head=controlNode('div','',card);head.className='section-heading';controlNode('h4',plugin?.enabled===false?name:serviceTitle(name),head);
        const badge=controlNode('span',s?({authenticated:'已登录',authorizing:'等待授权',login_required:'需要登录',missing:'未安装'}[s.state]||'待检查'):plugin?.enabled===false?'未启用':plugin?.readiness?.ready?'已配置':'需要配置',head);badge.className='badge '+(s?.state==='authenticated'||plugin?.readiness?.ready?'ready':'');
        const selection=controlNode('div','',card);selection.className='selection-row model-selection';
        const enabledLabel=controlNode('label','',selection),enabled=controlNode('input','',enabledLabel);enabled.type='checkbox';enabled.className='model-enable';enabled.dataset.name=name;enabled.checked=plugin?.enabled!==false;enabledLabel.append(' 启用此服务');
        const priorityLabel=controlNode('label','顺序 ',selection),priority=controlNode('input','',priorityLabel);priority.type='number';priority.min='1';priority.className='model-priority';priority.dataset.name=name;priority.value=plugin?.priority??99;priority.setAttribute('aria-label',name+' 模型服务顺序');
        const selectionStatus=controlNode('p','',card);selectionStatus.id='modelSelectionStatus_'+name;selectionStatus.className='status selection-status';selectionStatus.setAttribute('role','status');
        controlNode('p',s?'使用客户端账号提供模型；登录和模型配置分别管理。':plugin?.enabled===false?'启用后才能加载配置。保存启用状态会重新检查运行条件。':'填写服务地址、API Key 和模型。支持 OpenRouter 预置，也可以连接自己的兼容服务。',card).className='muted';
        const bar=controlNode('div','',card);bar.className='toolbar service-actions';
        if(plugin?.enabled!==false){const config=controlNode('a',s?'配置模型':'配置 API 服务',bar);config.className='button-link '+(!s?'primary':'');config.href=configLink('decision_provider',name)}
        let more;
        if(s){
            more=controlNode('details','',card);more.className='client-options';more.dataset.client=name;more.open=expanded.has(name);controlNode('summary','登录选项与客户端维护',more);
            const methodLabel=controlNode('label','登录方式',more);methodLabel.className='field';const select=controlNode('select','',methodLabel);select.setAttribute('aria-label',name+' 验证方式');
            for(const [value,label] of [['auto','自动选择（'+(preferredLoginMethod()==='remote'?'设备码 / 验证码':'本机网页回调')+'）'],['local','本机网页回调'],['remote','设备码 / 验证码']]){const option=controlNode('option',label,select);option.value=value}
            select.value=LOGIN_METHODS.get(name)||'auto';select.onchange=()=>LOGIN_METHODS.set(name,select.value);
            controlNode('p',s.message||'',more).className='description';
            controlNode('p','客户端版本：'+(s.installed_version||'未检测')+' · '+(s.update_available?'有新版可升级':s.update_message||'尚未检查更新'),more).className='description';
            if(s.update_checked_at)controlNode('p','最近检查：'+new Date(s.update_checked_at*1000).toLocaleString(),more).className='description';
            const diagnostics=controlNode('details','',more);diagnostics.className='diagnostic-detail';controlNode('summary','连接技术信息',diagnostics);controlNode('p',s.proxy_message||'未提供连接信息',diagnostics);
            const usage=s.usage||{};
            if(usage.checked_at||usage.error){
                const box=controlNode('div','',more);box.className='usage-readout';
                const fresh=new Date(usage.checked_at*1000).toLocaleString();
                const rows=(usage.windows||[]).map(w=>{
                    const reset=w.resets_text||(w.resets_at?new Date(w.resets_at*1000).toLocaleString():'');
                    const pct=w.used_percent==null?'—':Math.round(w.used_percent)+'%';
                    return '<li><span>'+esc(w.label)+'</span><b>'+esc(pct)+' 已用</b>'+(reset?'<small>重置 '+esc(reset)+'</small>':'')+'</li>'}).join('');
                box.innerHTML='<h5>账号状态</h5>'
                    +'<p class="description">'+(usage.error?'查询失败：'+esc(usage.error)
                        :(usage.available===false?'额度已用尽':usage.available===true?'额度可用':'额度未知')
                         +(usage.note?' · '+esc(usage.note):''))+'</p>'
                    +(rows?'<ul class="usage-windows">'+rows+'</ul>':'')
                    +'<p class="description">模型：'+esc(s.model||'客户端默认')+' · 查询于 '+esc(fresh)
                    +(usage.source?' · '+esc(usage.source):'')+'</p>';
            }
            const maintenance=controlNode('div','',more);maintenance.className='toolbar';
            if(s.state==='authorizing'){const resume=controlNode('button','继续登录',bar);resume.className='primary';resume.onclick=()=>showLoginWizard(item)}
            const groups=new Map();
            for(const action of s.actions||[]){
                if(action.id==='cancel'&&s.state!=='authorizing')continue;
                const prominent=action.id==='login'&&s.state!=='authenticated'&&s.state!=='authorizing';
                // Actions the plugin puts in a group get their own labelled block. A plain toolbar
                // row cannot hold one that carries a file picker: the label collapses to a single
                // character column once the row runs out of width.
                let host=prominent?bar:maintenance;
                if(action.group){
                    if(!groups.has(action.group)){
                        const block=controlNode('div','',more);block.className='action-group';
                        controlNode('h5',action.group,block);
                        if(action.group_note)controlNode('p',action.group_note,block).className='description';
                        groups.set(action.group,block);
                    }
                    host=groups.get(action.group);
                }
                const box=controlNode('div','',host);box.className='action-item';const inputs={};
                for(const field of action.fields||[]){const label=controlNode('label',field.label,box);label.className='field';const input=controlNode('input','',label);input.type=field.type==='secret'?'password':field.type==='file'?'file':'text';if(field.type==='file'){input.accept='application/json,.json'}else{input.autocomplete='off'}input.setAttribute('aria-label',field.label);inputs[field.name]=input;controlNode('small',field.description,box)}
                const button=controlNode('button',action.label,box);button.disabled=!!action.disabled;button.className=prominent?'primary':action.id==='logout'?'danger':'';
                button.onclick=async()=>{if(action.confirm&&!confirm(action.confirm))return;button.disabled=true;const values={flow_id:s.flow_id};try{for(const [key,input]of Object.entries(inputs)){if(input.type==='file'){const file=input.files&&input.files[0];if(!file)throw Error('请先选择凭据文件');values[key]=await file.text()}else{values[key]=input.value}input.value=''}const status=await post('/api/plugins/controls/action',{kind:item.kind,name,action:action.id==='login'?loginAction(name):action.id,values});if(['login','login_remote'].includes(action.id))showLoginWizard({...item,status});if(action.id==='export_credentials')downloadCredentialBundle(name,status);document.getElementById('clientControlError').textContent='';button.blur();await refreshClientControls()}catch(e){document.getElementById('clientControlError').textContent=e.message}finally{button.disabled=!!action.disabled}};
            }
        }
    }
    root.onchange=event=>{if(event.target.matches('.model-enable,.model-priority'))markModelSelectionDirty(event.target)};
    if(!names.length)root.innerHTML='<div class="empty-state"><strong>尚未发现模型服务</strong><p>请在程序设置中检查模型服务插件目录，或安装包含模型服务扩展的版本。</p></div>';
}
async function refreshClientControls(){try{const response=await get('/api/plugins/controls');CLIENT_STATUS=response.items;document.getElementById('clientAlerts').textContent=response.items.filter(x=>x.status.state==='login_required').map(x=>serviceTitle(x.name)+' 需要重新登录').join('；');for(const item of response.items)if(ACTIVE_LOGIN?.kind===item.kind&&ACTIVE_LOGIN?.name===item.name)updateLoginWizard(item.status);renderServiceConnections()}catch(e){document.getElementById('clientControlError').textContent=e.message}}

async function installModelPlugin(){let s=document.getElementById('modelInstallStatus');try{let result=await post('/api/plugins/install',{kind:'decision_provider',target_directory:document.getElementById('modelInstallTarget').value,name:document.getElementById('modelInstallName').value,source:document.getElementById('modelInstallSource').value});renderManager(result.management);s.className='status good';s.textContent='已安装：'+result.installed+'；请在上方启用并保存'}catch(e){s.className='status danger';s.textContent=e.message}}

/* An open tab keeps running the script it loaded. After an update it goes on drawing with it - a label
   since fixed, a card since redesigned - with nothing on screen to say the page itself is out of date.
   The server reports the version it serves now; a tab that was served a different one offers to reload. */
function noticeConsoleUpdate(serving){
    const banner=typeof document!=='undefined'&&document.getElementById('consoleUpdate');
    if(!banner||typeof CONSOLE_VERSION==='undefined'||!serving)return false;
    banner.hidden=serving===CONSOLE_VERSION;
    return !banner.hidden;
}

function renderRuntime(r){
    noticeConsoleUpdate(r.console_version);
    if(r.setup)renderSetupGuide(r.setup);
    const root=document.getElementById('runtimeControl');
    // Periodic status refresh must not erase an unsaved pause choice.
    if(!root.dataset.dirty){
        root.innerHTML='<label class="pause-switch"><input id="pauseAll" type="checkbox" '+(r.robot_paused?'checked':'')+'> 暂停全部平台</label><div class="config-grid">'+Object.entries(r.platforms||{}).map(([name,p])=>'<article class="plugin"><div class="section-heading"><h4>'+esc(name)+'</h4><span class="badge '+(p.running?'ready':'')+'">'+(p.running?(p.runtime&&p.runtime.holding?'已暂停：AI 不可用':'运行中'):p.paused?'已暂停':p.ready?'可启动但未运行':'插件报告未就绪')+'</span></div><label><input class="pausePlatform" type="checkbox" value="'+esc(name)+'" '+(p.paused?'checked':'')+'> 暂停此平台</label>'+(!p.ready?'<p><a href="'+configLink('api',name)+'">打开平台插件 →</a></p>':'')+(p.startup_reasons?.length?'<details class="diagnostic-detail"><summary>查看插件报告</summary><p>'+esc(p.startup_reasons.join('；'))+'</p></details>':'')+'</article>').join('')+'</div><p class="status '+(r.global_ready?'good':'muted')+'">'+(r.global_ready?'AI 服务和至少一个平台已报告可启动；是否正常运行以平台的“运行中”为准。':'机器人主链尚未满足：需要至少一个可用 AI 服务和一个可启动平台。')+'</p>'+(!r.global_ready?'<details><summary>查看具体原因</summary><p>'+esc((r.global_reasons||[]).join('；'))+'</p></details>':'');
        root.onchange=()=>{root.dataset.dirty='true';document.getElementById('runtimeStatus').textContent='暂停选项尚未保存'};
    }
}

let SETUP_GUIDE_TAB='required';
function selectSetupGuideTab(tab,focus=true){
    SETUP_GUIDE_TAB=tab==='optional'?'optional':'required';
    const root=document.getElementById('setupChecklist');
    if(!root)return;
    for(const button of root.querySelectorAll('[data-setup-tab]')){
        const selected=button.dataset.setupTab===SETUP_GUIDE_TAB;
        button.setAttribute('aria-selected',String(selected));
        button.tabIndex=selected?0:-1;
        if(selected&&focus)button.focus();
    }
    for(const panel of root.querySelectorAll('[data-setup-panel]'))panel.hidden=panel.dataset.setupPanel!==SETUP_GUIDE_TAB;
}
function setupStepHtml(step,index){
    const defaultLink=step.id==='models'?'#models':step.id==='platforms'?'#plugins/api':step.id==='strategy'?'#plugins/decision_strategy':'#overview';
    const actions=(step.actions||[]).map(a=>'<div class="setup-action"><a class="button-link" href="'+configLink(a.kind,a.name)+'">'+esc(a.label)+' →</a></div>').join('');
    return '<li class="'+(step.ready?'complete':'incomplete')+'"><span class="step-number">'+(step.ready?'✓':step.required===false?'·':index+1)+'</span><div><strong>'+esc(step.title)+'</strong><span class="badge">'+(step.ready?'已满足':step.required===false?'可选项未就绪':'需要处理')+'</span>'+(!step.ready?'<p>'+esc(step.description)+'</p><div class="toolbar">'+(actions||'<a class="button-link" href="'+defaultLink+'">前往设置 →</a>')+'</div>'+(step.issues.length?'<details class="diagnostic-detail"><summary>查看插件报告</summary><p>'+step.issues.map(esc).join('<br>')+'</p></details>':''):'')+'</div></li>';
}
/* ---- Nothing can decide: the robot is paused, and the overview says why and until when. ----
   Platforms keep their threads while they hold, so "running" is technically true - but nothing is
   collected and no record is written, which to the person watching is a pause. What they need is
   the reason per service and, where the service said, when it comes back. */
const PROVIDER_WAIT_TEXT={rate_limit:'额度用完',auth:'登录失效，需要重新登录',transient:'连接不稳定（超时或网络故障）',contract:'返回的内容不符合要求',unknown:'调用失败',unavailable:'没能启用'};
let RECHECK_NOTE=null;

function waitDurationText(seconds){
    const s=Math.max(0,Math.round(seconds));
    const d=Math.floor(s/86400),h=Math.floor(s%86400/3600),m=Math.floor(s%3600/60);
    if(d)return d+' 天'+(h?' '+h+' 小时':'');
    if(h)return h+' 小时'+(m?' '+m+' 分钟':'');
    return Math.max(1,m)+' 分钟';
}
function clockText(epochSeconds){
    const when=new Date(epochSeconds*1000),today=new Date();
    const time=String(when.getHours()).padStart(2,'0')+':'+String(when.getMinutes()).padStart(2,'0');
    return when.toDateString()===today.toDateString()?'今天 '+time:(when.getMonth()+1)+' 月 '+when.getDate()+' 日 '+time;
}
function providerWaitText(name,p,nowSeconds){
    const now=nowSeconds??Date.now()/1000;
    let text=serviceTitle(name)+'：'+(PROVIDER_WAIT_TEXT[p.kind]||'暂时不可用');
    if(p.kind==='rate_limit'){
        if(p.recovers_at&&p.recovers_at>now)text+='，预计 '+waitDurationText(p.recovers_at-now)+'后恢复（'+clockText(p.recovers_at)+'）';
        else if(p.confirming)text+='，已到恢复时间，正在确认';
        else text+='，没说何时恢复，会定期免费查询额度';
    }else if(p.kind==='auth'){
        return text;
    }else{
        if(p.kind==='unknown'&&p.error)text+='（'+String(p.error).slice(0,80)+'）';
        if(p.next_check_at&&p.next_check_at>now)text+='，约 '+waitDurationText(p.next_check_at-now)+'后自动重试';
        else if(p.confirming)text+='，正在确认是否恢复';
    }
    return text;
}
function aiPauseTitle(setup){
    const kinds=Object.values(setup.decision_capacity?.providers||{}).filter(p=>!p.ready).map(p=>p.kind);
    return kinds.length&&kinds.every(k=>k==='rate_limit')?'机器人已暂停：AI 额度用完':'机器人已暂停：没有可用的 AI 模型服务';
}
function aiPauseHtml(setup,nowSeconds){
    const known=setup.decision_capacity?.providers||{};
    const rows=Object.entries(known).filter(([,p])=>!p.ready)
        .map(([name,p])=>'<li>'+esc(providerWaitText(name,p,nowSeconds))+(p.kind==='auth'||p.kind==='unavailable'?' <a href="#models">去处理 →</a>':'')+'</li>');
    // A service that could not even start is not in the robot's own list, but it is still a reason.
    for(const [name,p] of Object.entries(setup.decision_providers||{}))
        if(!p.ready&&!known[name])rows.push('<li>'+esc(serviceTitle(name)+'：'+((p.reasons||[]).join('；')||'没有就绪'))+' <a href="#models">去处理 →</a></li>');
    return '<ul class="ai-pause-list">'+rows.join('')+'</ul>'
        +'<p>暂停期间不采集市场数据，也不产生决策记录；AI 恢复后自动继续，不需要操作。</p>'
        +'<div class="toolbar"><button onclick="recheckProviders(this)">已换套餐或已充值？立即重新检测</button>'
        +(RECHECK_NOTE?'<span class="status '+esc(RECHECK_NOTE.tone)+'" role="status">'+esc(RECHECK_NOTE.text)+'</span>':'')+'</div>';
}
async function recheckProviders(button){
    if(button)button.disabled=true;
    RECHECK_NOTE={tone:'muted',text:'正在向客户端查询额度…'};
    try{
        const result=await post('/api/providers/recheck',{});
        RECHECK_NOTE=result.capacity&&result.capacity.available
            ?{tone:'good',text:'已恢复，机器人继续运行'}
            :{tone:'muted',text:'仍不可用，已按客户端的回答更新恢复时间（'+new Date().toLocaleTimeString()+'）'};
    }catch(e){
        RECHECK_NOTE={tone:'danger',text:e.message};
    }finally{
        if(button)button.disabled=false;
    }
    await refreshRuntime();
}

function renderSetupGuide(setup){
    const titles={management_only:'界面验收测试实例：未启动机器人进程',not_running:'机器人未运行',paused:'机器人已暂停',partial:'机器人正在运行，但部分平台未运行',running:'机器人正在运行'};
    const descriptions={management_only:'当前访问的是仅用于界面验收的测试实例，未启用机器人运行进程；这不是配置问题。正常启动的实例会在模型服务和至少一个平台就绪且未暂停后立即运行。',not_running:'先处理“启动必需”中的未完成项。AI 服务和至少一个平台报告可启动且未暂停时，平台会立即自动启动。若条件已满足仍未运行，请展开启动错误。',paused:'当前是用户主动暂停状态，不是缺少必备配置。到本页下方取消暂停并保存后，已就绪平台会立即启动。',partial:'正在运行的平台不受其他平台或可选增强项影响。你可以继续处理未运行的平台，或关闭不使用的平台。',running:'平台扫描已启动，等待市场事件。可选增强项不影响启动；运行中也不代表一定会产生交易或盈利。'};
    const root=document.getElementById('gettingStarted'),previous=root.querySelector('#setupChecklist');
    const aiPaused=setup.state==='ai_paused';
    const open=previous?previous.open:setup.state!=='running'&&!aiPaused;
    const requiredSteps=setup.steps.filter(step=>step.required!==false),optionalSteps=setup.steps.filter(step=>step.required===false);
    const optionalRemaining=optionalSteps.filter(step=>!step.ready).length;
    const requiredSelected=SETUP_GUIDE_TAB!=='optional';
    const optionalList=optionalSteps.length?'<ol class="setup-checklist">'+optionalSteps.map(setupStepHtml).join('')+'</ol>':'<div class="empty-state compact"><strong>当前没有可选增强项</strong><p>无需处理，机器人仍可按启动必需项运行。</p></div>';
    root.className='readiness-panel '+(setup.state==='running'?'ready':'needs-attention');
    root.innerHTML='<div class="section-heading"><div><span class="eyebrow">运行状态</span><h3>'+esc(aiPaused?aiPauseTitle(setup):titles[setup.state])+'</h3>'+(aiPaused?aiPauseHtml(setup):'<p>'+esc(descriptions[setup.state]+(setup.state==='not_running'&&setup.start_retry_at?' 不需要手动重启：条件满足后会自动启动（约每分钟检查一次）。':''))+'</p>')+'</div><button onclick="refreshManager()">重新检查</button></div><details id="setupChecklist" '+(open?'open':'')+'><summary>启动向导 · '+setup.remaining+' 项必须处理</summary><div class="setup-tabs" role="tablist" aria-label="启动向导分类"><button id="setupRequiredTab" type="button" role="tab" data-setup-tab="required" aria-controls="setupRequiredPanel" aria-selected="'+requiredSelected+'" tabindex="'+(requiredSelected?'0':'-1')+'" onclick="selectSetupGuideTab(\'required\')">启动必需 <span class="setup-tab-count">'+setup.remaining+'</span></button><button id="setupOptionalTab" type="button" role="tab" data-setup-tab="optional" aria-controls="setupOptionalPanel" aria-selected="'+(!requiredSelected)+'" tabindex="'+(requiredSelected?'-1':'0')+'" onclick="selectSetupGuideTab(\'optional\')">可选增强 <span class="setup-tab-count">'+optionalRemaining+'</span></button></div><div id="setupRequiredPanel" class="setup-panel" role="tabpanel" aria-labelledby="setupRequiredTab" data-setup-panel="required" '+(requiredSelected?'':'hidden')+'><p class="setup-tab-intro">这些条件决定机器人能否启动。顶部数字只统计尚未完成的必需项。</p><ol class="setup-checklist">'+requiredSteps.map(setupStepHtml).join('')+'</ol></div><div id="setupOptionalPanel" class="setup-panel" role="tabpanel" aria-labelledby="setupOptionalTab" data-setup-panel="optional" '+(requiredSelected?'hidden':'')+'><p class="setup-tab-intro">这些能力用于增强分析、研究，或对动作做过滤检查；未就绪不会阻止机器人启动。</p>'+optionalList+'</div></details>'+(setup.runtime_reasons?.length?'<details class="diagnostic-detail"><summary>查看启动错误 / 运行条件</summary><p>'+setup.runtime_reasons.map(esc).join('<br>')+'</p><a href="#settings">检查程序设置 →</a></details>':'');
}

const CHOICE_REQUESTS=new Map();
function credentialInputHtml(id,field){
    const masked=CSS.supports('-webkit-text-security','disc');
    return '<input id="'+esc(id)+'" type="'+(masked?'search':'password')+'" class="credential-entry" name="configuration-entry" data-field="'+esc(field.name)+'" data-type="secret" autocomplete="off" autocorrect="off" autocapitalize="none" spellcheck="false" data-1p-ignore data-lpignore="true" data-bwignore value="" placeholder="'+(field.configured?'已保存；留空保持不变':'粘贴服务商提供的凭据')+'">';
}
const CHOICE_ITEMS=new WeakMap();
function dynamicChoiceHtml(kind,name,f){
    const id='cfg_'+kind+'_'+name+'_'+f.name;
return '<div class="field"><label for="'+id+'"><b>'+esc(f.label)+'</b></label><div class="model-combobox"><div class="model-input-row"><input id="'+id+'" type="search" name="model-catalog-search" role="combobox" aria-autocomplete="list" aria-expanded="false" aria-controls="'+id+'_choices" autocomplete="off" autocapitalize="none" spellcheck="false" data-1p-ignore data-lpignore="true" data-bwignore data-dynamic-list="true" data-kind="'+kind+'" data-plugin="'+name+'" data-field="'+f.name+'" data-field-name="'+f.name+'" data-type="'+f.type+'" value="'+esc(f.value||'')+'" placeholder="搜索名称 / ID，或直接输入模型 ID" onfocus="openModelChoices(\''+id+'\')" onclick="openModelChoices(\''+id+'\')" oninput="filterChoiceList(\''+id+'\')" onkeydown="modelChoiceKey(event,\''+id+'\')"><button type="button" aria-label="展开模型列表" onclick="toggleModelChoices(\''+id+'\')">⌄</button></div><div class="model-options" id="'+id+'_choices" role="listbox" aria-label="可用模型" hidden></div></div><span id="'+id+'_note" class="description" role="status"></span><span class="description">'+esc(f.description)+'</span><div class="toolbar"><button onclick="loadFieldChoices(\''+kind+'\',\''+name+'\',\''+f.name+'\')">刷新模型列表</button><button onclick="resetPluginField(\''+kind+'\',\''+name+'\',\''+f.name+'\')">恢复此项默认值</button></div></div>';
}
function closeModelChoices(id){
    const input=document.getElementById(id);
    input.setAttribute('aria-expanded','false');input.removeAttribute('aria-activedescendant');
    document.getElementById(id+'_choices').hidden=true;
}
function openModelChoices(id){
    const input=document.getElementById(id);
    document.getElementById(id+'_choices').hidden=false;input.setAttribute('aria-expanded','true');
    filterChoiceList(id,'');
}
function toggleModelChoices(id){
    const input=document.getElementById(id);
    if(input.getAttribute('aria-expanded')==='true')closeModelChoices(id);
    else openModelChoices(id);
}
function clearChoiceDependencyErrors(input){
    const root=input.closest('.configuration-card');
    for(const error of root.querySelectorAll('.choice-dependency-error'))error.remove();
    for(const field of root.querySelectorAll('[data-field][aria-invalid=true]'))field.removeAttribute('aria-invalid');
    delete input.dataset.catalogErrorTarget;
}
function useDirectConnection(targetId,errorId){
    const target=document.getElementById(targetId);target.value='DIRECT';target.dispatchEvent(new Event('input',{bubbles:true}));
    target.removeAttribute('aria-invalid');document.getElementById(errorId)?.remove();target.focus();
    managementFeedback().className='status';
    managementFeedback().textContent='已在表单中改为直连；请点击“保存配置”，保存后模型列表会自动重新加载。';
}
function routeChoiceDependencyError(input,message){
    const root=input.closest('.configuration-card');
    const target=[...root.querySelectorAll('[data-field]')].find(field=>field!==input&&message.includes(field.dataset.field));
    if(!target)return null;
    target.setAttribute('aria-invalid','true');
    const label=target.closest('.field')?.querySelector('b')?.textContent||target.dataset.field;
    const proxy=target.dataset.field.endsWith('HTTP_PROXY');
    const error=controlNode('p',proxy
        ? '当前代理设置无效。默认请使用 DIRECT（直连）；只有明确需要时才填写 SYSTEM 或完整 http(s):// 地址。'
        : '此设置导致模型列表无法加载：'+message,target.closest('.field'));
    error.className='description danger choice-dependency-error';error.setAttribute('role','alert');
    error.id=input.id+'_dependency_error';
    if(proxy){
        const action=controlNode('button','在表单中改为直连（DIRECT）',target.closest('.field'));action.type='button';
        action.className='choice-dependency-error';action.onclick=()=>useDirectConnection(target.id,error.id);
    }
    input.dataset.catalogErrorTarget=target.id;
    return {label,targetId:target.id};
}
function focusConfigField(id,sourceId){
    closeModelChoices(sourceId);
    const target=document.getElementById(id);target.scrollIntoView({block:'center'});target.focus();
}
function filterChoiceList(id,query=null){
    const input=document.getElementById(id),state=CHOICE_ITEMS.get(input),list=document.getElementById(id+'_choices');
    if(!state){
        list.replaceChildren();
        controlNode('p',input.dataset.catalogError||'正在加载模型列表，请稍候…',list);
        if(input.dataset.catalogErrorTarget){
            const button=controlNode('button','去修正相关设置',list);button.type='button';
            button.onclick=()=>focusConfigField(input.dataset.catalogErrorTarget,id);
        }
        return;
    }
    const terms=(query??input.value).trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
    const matches=state.items.filter(item=>terms.every(term=>(item.label+' '+item.value).toLocaleLowerCase().includes(term)));
    state.matches=matches;state.active=-1;input.removeAttribute('aria-activedescendant');
    list.replaceChildren();
    for(const [index,item]of matches.entries()){
        const option=controlNode('div',null,list);option.id=id+'_option_'+index;option.setAttribute('role','option');option.setAttribute('aria-selected',String(item.value===input.value));
        controlNode('strong',item.label,option);controlNode('small',item.value,option);
        option.onmousedown=event=>event.preventDefault();
        option.onclick=()=>chooseModel(id,index);
    }
    if(!matches.length)controlNode('p','没有匹配项；可继续修改关键词，或直接保存手动输入的模型 ID。',list);
    const note=document.getElementById(id+'_note');note.className='description';
    note.textContent=(matches.length?'显示 '+matches.length+' / '+state.items.length+' 个模型。':'没有匹配的模型。')+' 从列表选择或直接输入 ID，保存配置后生效。';
    if(input.dataset.catalogError){
        const message=controlNode('p',input.dataset.catalogError+' 以下仍显示上次成功获取的列表。',null);list.prepend(message);
        if(input.dataset.catalogErrorTarget){
            const button=controlNode('button','去修正相关设置',null);button.type='button';
            button.onclick=()=>focusConfigField(input.dataset.catalogErrorTarget,id);list.prepend(button);
        }
        note.textContent=input.dataset.catalogError;note.className='description danger';
    }
    if(query===null){list.hidden=false;input.setAttribute('aria-expanded','true')}
}
function chooseModel(id,index){
    const input=document.getElementById(id),item=CHOICE_ITEMS.get(input)?.matches[index];
    if(!item)return;
    input.value=item.value;closeModelChoices(id);input.dispatchEvent(new Event('change',{bubbles:true}));
}
function modelChoiceKey(event,id){
    if(event.isComposing)return;
    const input=document.getElementById(id),state=CHOICE_ITEMS.get(input);
    if(event.key==='Escape'){event.preventDefault();event.stopPropagation();closeModelChoices(id);return}
    if(event.key==='Tab'){closeModelChoices(id);return}
    if(!state)return;
    if(event.key==='ArrowDown'||event.key==='ArrowUp'){
        event.preventDefault();
        if(input.getAttribute('aria-expanded')!=='true')openModelChoices(id);
        const count=state.matches.length;if(!count)return;
        state.active=(state.active+(event.key==='ArrowDown'?1:state.active<0?0:-1)+count)%count;
        for(const option of document.getElementById(id+'_choices').children)option.classList.remove('active');
        const option=document.getElementById(id+'_option_'+state.active);option.classList.add('active');
        input.setAttribute('aria-activedescendant',option.id);option.scrollIntoView({block:'nearest'});
    }else if(event.key==='Enter'){
        event.preventDefault();
        if(input.getAttribute('aria-expanded')==='true'&&state.active>=0)chooseModel(id,state.active);
        else closeModelChoices(id);
    }
}
document.addEventListener('click',event=>{
    for(const input of document.querySelectorAll('[data-dynamic-list][aria-expanded="true"]'))
        if(!input.closest('.model-combobox').contains(event.target))closeModelChoices(input.id);
});
async function loadFieldChoices(kind,name,field){
    const id='cfg_'+kind+'_'+name+'_'+field,input=document.getElementById(id);
    if(!input)return;
    const ticket={};CHOICE_REQUESTS.set(id,ticket);
    clearChoiceDependencyErrors(input);
    delete input.dataset.catalogError;
    const note=document.getElementById(id+'_note');note.className='description';note.textContent='正在自动获取模型列表…';
    try{
        const response=await post('/api/plugins/config/choices',{kind,name,field});
        if(CHOICE_REQUESTS.get(id)!==ticket||!input.isConnected)return;
        CHOICE_ITEMS.set(input,{items:response.items,matches:[],active:-1});
        input.dataset.loaded='true';filterChoiceList(id,input.getAttribute('aria-expanded')==='true'?input.value:'');
    }catch(e){if(CHOICE_REQUESTS.get(id)===ticket&&input.isConnected){
        const dependency=routeChoiceDependencyError(input,e.message);
        input.dataset.catalogError=dependency
            ? '模型列表暂不可用。请先修正“'+dependency.label+'”并保存，然后刷新列表。'
            : '模型列表暂不可用：'+e.message+'；可点击刷新重试，也可手动填写模型 ID。';
        filterChoiceList(id,input.value);note.className='description danger';note.textContent=input.dataset.catalogError;
    }}
}
function selectionFieldHtml(kind,name,f){
    const id='cfg_'+kind+'_'+name+'_'+f.name;
    return '<div class="field"><label><b>'+esc(f.label)+'</b><select id="'+id+'" data-field="'+f.name+'" data-type="'+f.type+'" data-selection-only="true" data-kind="'+kind+'" data-plugin="'+name+'" data-dependencies="'+esc(JSON.stringify(f.choices_depend_on||[]))+'" onchange="selectionChanged(\''+kind+'\',\''+name+'\',\''+f.name+'\')"><option value="'+esc(f.value||'')+'">'+esc(f.value||'使用客户端默认')+'</option></select><span class="description">'+esc(f.description)+'</span></label><button onclick="loadSelectionChoices(\''+kind+'\',\''+name+'\',\''+f.name+'\')">刷新可选列表</button><span class="description" id="'+id+'_note" role="status"></span><button onclick="resetPluginField(\''+kind+'\',\''+name+'\',\''+f.name+'\')">恢复此项默认值</button></div>';
}
async function loadSelectionChoices(kind,name,field){
    const id='cfg_'+kind+'_'+name+'_'+field,select=document.getElementById(id);
    if(!select)return;
    const dependencies=JSON.parse(select.dataset.dependencies||'[]'),values={};
    for(const dependency of dependencies)values[dependency]=document.getElementById('cfg_'+kind+'_'+name+'_'+dependency)?.value||'';
    const ticket={};CHOICE_REQUESTS.set(id,ticket);
    const note=document.getElementById(id+'_note');note.className='description';note.textContent='正在读取客户端可选项…';
    try{
        const response=await post('/api/plugins/config/choices',{kind,name,field,values});
        if(CHOICE_REQUESTS.get(id)!==ticket||!select.isConnected)return;
        const current=select.value;select.replaceChildren();
        for(const item of response.items){const option=controlNode('option',item.label,select);option.value=item.value}
        if([...select.options].some(o=>o.value===current))select.value=current;
        else {const option=controlNode('option',current+'（当前值未在列表中；请重新选择）',select);option.value=current;select.value=current;}
        select.dataset.loaded='true';note.textContent=response.items.length<=1?'客户端未提供额外选项，可使用默认设置。':'列表来自当前客户端；选择后点击保存配置。';
    }catch(e){if(CHOICE_REQUESTS.get(id)===ticket&&select.isConnected){note.textContent=e.message;note.className='description danger'}}
}
function selectionChanged(kind,name,field){
    const root=document.getElementById('plugin_'+kind+'_'+name);
    for(const target of root.querySelectorAll('[data-selection-only]'))if(JSON.parse(target.dataset.dependencies||'[]').includes(field)){
        target.replaceChildren();const option=controlNode('option','使用客户端默认（模型已切换）',target);option.value='';
        loadSelectionChoices(kind,name,target.dataset.field);
    }
}
function activateChoiceLists(){
    for(const select of document.querySelectorAll('[data-dynamic-list]'))if(select.getClientRects().length&&!select.dataset.started){
        select.dataset.started='true';loadFieldChoices(select.dataset.kind,select.dataset.plugin,select.dataset.fieldName);
    }
    for(const select of document.querySelectorAll('[data-selection-only]'))if(select.getClientRects().length&&!select.dataset.started){
        select.dataset.started='true';loadSelectionChoices(select.dataset.kind,select.dataset.plugin,select.dataset.field);
    }
}

/* Three kinds of row answer three different questions, and mixing them serves none of them.
   What did it decide, what is it doing right now, and what broke - the default is the first,
   because that is what the ledger is for. The counts are shown on every tab so an empty default
   is read as "nothing concluded yet" rather than as a dead robot. */
let LEDGER_TAB='concluded';
const LEDGER_TAB_NOTE={
    concluded:'每一条都是一次得出结论的判断（观望、买入、卖出、规则拒绝）。结论不等于成交。',
    running:'模型正在分析、还没有结论的记录。删除会同时让那次分析停下：不再花模型调用，也不会据此下单。重启前卡住、永远不会结束的记录也在这里，可以直接删。',
    failed:'没能得出结论的记录：模型调用失败、执行失败。它们会拉低 provider 排名，确认无用后可以删掉。',
};

/* The ledger is append-only history and nothing on it changes once written, so it is read when
   asked and never on a timer - a periodic rebuild only ever cost the reader the panel they had
   open and the place they were scrolled to. What a manual refresh must still not lose is that
   same state, so the open JSON blocks are carried across it. */
const OPEN_DETAILS=new Set();

/* Ten to start, five at a time after that. The page used to fetch a hundred rows - each one
   carrying its whole context, research trace and raw model output - before it could draw anything,
   which is why it sat blank long enough to look empty. Almost nobody reads past the first few. */
const LEDGER_FIRST_PAGE=10;
const LEDGER_PAGE=5;
let LEDGER_QUERY='';
let LEDGER_LOADED=0;
let LEDGER_EXHAUSTED=false;
let LEDGER_FETCHING=false;
let LEDGER_WATCHER=null;

/* Filtering by result. Selecting results reorders what the server sends - matches first - rather
   than excluding the rest, and hides non-matches on the page; selecting none shows everything. */
let LEDGER_RESULTS=new Set();
let LEDGER_SERVER_OFFSET=0;
const RESULT_CHIPS={
    concluded:[['HOLD','观望'],['BUY','买入'],['SELL','卖出'],['CANCEL','撤单'],['RISK_REJECTED','规则拒绝'],['OK','选出标的'],['NO_ACTION','本轮跳过']],
    failed:[['PROVIDER_ERROR','模型调用失败'],['EXECUTION_ERROR','执行失败'],['ERROR','出现错误']],
    running:[],
};

function resultMatches(result,selected){
    return !selected.size||selected.has(String(result||'').toUpperCase());
}

function ledgerCompare(a,b){
    /* Newest first, and by id when two share a timestamp - the same order the server uses, so a row
       never jumps position between a refresh and a scroll. */
    return (Number(b.created)-Number(a.created))||(Number(b.id)-Number(a.id));
}

function pageExhausts(rows,pageSize,filtering){
    /* With no filter, a short page is the end. With one, matches arrive first, so the first
       non-matching row proves there are no matches left: fetching on would only bring rows the page
       is going to hide. */
    if(rows.length<pageSize)return true;
    return filtering&&rows.some(row=>row.matches_results===false);
}

function ledgerResultsQuery(){
    return LEDGER_RESULTS.size?'&results='+encodeURIComponent([...LEDGER_RESULTS].join(',')):'';
}

/* Which way the reader is going. The sentinel keeps intersecting while they scroll back up through
   what they just loaded, and fetching - never mind animating a placeholder - because somebody is
   reading upward is the opposite of what they asked for. Older rows are wanted on the way down. */
let LEDGER_SCROLL_DOWN=true;
let LEDGER_LAST_SCROLL=typeof window==='undefined'?0:window.scrollY;

function watchLedgerScrollDirection(){
    if(typeof window==='undefined'||window.__ledgerScrollWatched)return;
    window.__ledgerScrollWatched=true;
    window.addEventListener('scroll',()=>{
        const position=window.scrollY;
        if(position===LEDGER_LAST_SCROLL)return;
        LEDGER_SCROLL_DOWN=position>LEDGER_LAST_SCROLL;
        LEDGER_LAST_SCROLL=position;
        if(!LEDGER_SCROLL_DOWN)clearLedgerSkeleton();
    },{passive:true});
}

function skeletonRowsHtml(count){
    /* Placed where the rows will actually appear, and shaped like them - a spinner somewhere else
       on the page tells you something is happening but not where, and the reader is already looking
       at the gap the new rows will fill. */
    return '<div class="decision-skeleton" aria-hidden="true">'
        + Array.from({length: count}, () =>
            '<div class="skeleton-entry"><span class="skeleton-bar meta"></span>'
            + '<span class="skeleton-bar title"></span>'
            + '<span class="skeleton-bar reason"></span></div>').join('')
        + '</div>';
}

function showLedgerSkeleton(count){
    const more=document.getElementById('ledgerMore');
    if(!more)return;
    more.insertAdjacentHTML('beforebegin', skeletonRowsHtml(count));
}

function clearLedgerSkeleton(){
    for(const node of document.querySelectorAll('#decisions .decision-skeleton'))node.remove();
}

function rememberOpenDetails(){
    /* Only the inner JSON blocks: the entry's own state is carried by `opened` below. */
    OPEN_DETAILS.clear();
    for(const node of document.querySelectorAll('#decisions [data-detail-key]'))
        if(node.open)OPEN_DETAILS.add(String(node.dataset.detailKey));
}

function setLedgerLoading(busy){
    /* Silence while a slow query runs reads as "there is nothing here", which is the one thing it
       must not be mistaken for - especially on a page whose whole job is to show you that the
       robot has been doing something. */
    const root=document.getElementById('decisions');
    if(!root)return;
    if(busy){
        root.setAttribute('aria-busy','true');
        if(!root.querySelector('.decision-entry'))
            root.innerHTML='<p class="muted">正在读取决策记录…</p>'+skeletonRowsHtml(LEDGER_FIRST_PAGE);
        else root.classList.add('is-loading');
    }else{
        root.removeAttribute('aria-busy');
        root.classList.remove('is-loading');
    }
}

function renderResultChips(){
    renderLedgerBulk();
    const host=document.getElementById('ledgerResultChips');
    if(!host)return;
    const chips=RESULT_CHIPS[LEDGER_TAB]||[];
    host.hidden=!chips.length;
    host.innerHTML=chips.length
        ?'<span class="muted">按结论：</span>'+chips.map(([code,label])=>
            '<button class="result-chip" data-result="'+esc(code)+'" aria-pressed="'+String(LEDGER_RESULTS.has(code))+'" onclick="toggleResultChip(\''+esc(code)+'\')">'+esc(label)+'</button>').join('')
            +'<span class="muted result-chip-hint">不选等于不筛选</span>'
        :'';
}

async function toggleResultChip(code){
    if(LEDGER_RESULTS.has(code))LEDGER_RESULTS.delete(code);else LEDGER_RESULTS.add(code);
    renderResultChips();
    /* Instant on what is already here, then fetched again from the top in the new order: rows
       loaded under a narrower selection may be missing ones the wider one wants, and the server's
       order has changed underneath the old position. Anything already on the page is skipped. */
    applyLedgerFilter();
    LEDGER_SERVER_OFFSET=0;
    LEDGER_EXHAUSTED=false;
    await fillLedger();
}

function selectLedgerTab(group){
    LEDGER_TAB=group;
    LEDGER_RESULTS=new Set();
    LEDGER_PICKED.clear();
    renderResultChips();
    for(const tab of document.querySelectorAll('.ledger-tabs [role="tab"]'))
        tab.setAttribute('aria-selected',String(tab.dataset.group===group));
    document.getElementById('ledgerTabNote').textContent=LEDGER_TAB_NOTE[group]||'';
    refreshAudit();
}

async function refreshLedgerTabCounts(platformQuery){
    /* Counted in the database, in one request. Deriving it from the rows on screen would only say
       how many are on screen, and fetching a page per tab to measure its length was most of the
       reason this view took so long to appear. */
    try{
        const counts=await get('/api/decisions/counts?'+platformQuery.replace(/^&/,''));
        for(const group of ['concluded','running','failed']){
            const node=document.getElementById('tabCount_'+group);
            if(node)node.textContent=String(counts[group]??'');
        }
    }catch(e){
        for(const group of ['concluded','running','failed']){
            const node=document.getElementById('tabCount_'+group);
            if(node)node.textContent='';
        }
    }
}

async function loadMoreDecisions(options){
    /* Appending rather than redrawing: a rebuild would close whatever the reader has open, which
       is the whole reason this view stopped refreshing itself. Scrolling only loads on the way down;
       a filter change loads whichever way the reader was last scrolling. */
    const forced=Boolean(options&&options.force);
    if(LEDGER_FETCHING||LEDGER_EXHAUSTED||(!forced&&!LEDGER_SCROLL_DOWN))return 0;
    LEDGER_FETCHING=true;
    const more=document.getElementById('ledgerMore');
    if(more)more.textContent='';
    showLedgerSkeleton(LEDGER_PAGE);
    let shown=0;
    try{
        const answer=await get('/api/decisions?limit='+LEDGER_PAGE+'&offset='+LEDGER_SERVER_OFFSET+LEDGER_QUERY+ledgerResultsQuery());
        const rows=answer.items||[];
        LEDGER_SERVER_OFFSET+=rows.length;
        if(rows.length)shown=appendDecisionEntries(rows);
        LEDGER_EXHAUSTED=pageExhausts(rows,LEDGER_PAGE,LEDGER_RESULTS.size>0);
        if(more)more.textContent=LEDGER_EXHAUSTED?(LEDGER_RESULTS.size?'没有更多符合所选结果的记录了':'没有更多记录了'):'';
    }catch(e){
        if(more)more.textContent='加载更多失败：'+(e&&e.message||e);
        LEDGER_EXHAUSTED=true;
    }finally{clearLedgerSkeleton();LEDGER_FETCHING=false}
    return shown;
}

async function fillLedger(){
    /* Keep fetching until a first page's worth is showing or there is nothing left to show. Bounded:
       each round either shows something, advances past rows already on the page, or ends. */
    while(!LEDGER_EXHAUSTED&&visibleLedgerCount()<LEDGER_FIRST_PAGE){
        if(LEDGER_FETCHING){await new Promise(resolve=>setTimeout(resolve,50));continue}
        await loadMoreDecisions({force:true});
    }
    applyLedgerFilter();
}

function visibleLedgerCount(){
    return document.querySelectorAll('#decisions .decision-entry:not([hidden])').length;
}

function sortLedgerEntries(){
    const list=document.querySelector('#decisions .decision-list');
    if(!list)return;
    const entries=[...list.querySelectorAll(':scope > .decision-entry')];
    entries.sort((a,b)=>ledgerCompare(
        {created:a.dataset.created,id:a.dataset.id},{created:b.dataset.created,id:b.dataset.id}));
    // Moving an existing node keeps it - including whether it is open - so the reader loses nothing.
    for(const entry of entries)list.appendChild(entry);
}

function applyLedgerFilter(){
    let visible=0;
    for(const entry of document.querySelectorAll('#decisions .decision-entry')){
        entry.hidden=!resultMatches(entry.dataset.result,LEDGER_RESULTS);
        if(!entry.hidden)visible+=1;
        // A selection is of rows on screen; one the filter just hid is not something anyone is looking at.
        else if(LEDGER_PICKED.delete(String(entry.dataset.id))){
            const box=entry.querySelector('.decision-pick');
            if(box)box.checked=false;
        }
    }
    renderLedgerBulk();
    const empty=document.getElementById('ledgerFilterEmpty');
    if(empty)empty.hidden=!(LEDGER_RESULTS.size&&!visible&&LEDGER_EXHAUSTED);
    return visible;
}

function watchLedgerEnd(){
    const sentinel=document.getElementById('ledgerSentinel');
    if(LEDGER_WATCHER)LEDGER_WATCHER.disconnect();
    if(!sentinel||typeof IntersectionObserver!=='function')return;
    watchLedgerScrollDirection();
    LEDGER_WATCHER=new IntersectionObserver(entries=>{
        if(entries.some(entry=>entry.isIntersecting)&&LEDGER_SCROLL_DOWN)loadMoreDecisions();
    },{rootMargin:'200px'});
    LEDGER_WATCHER.observe(sentinel);
}

function appendDecisionEntries(rows){
    /* Returns how many of the new rows are showing, which is what decides whether to keep loading. */
    const root=document.getElementById('decisions');
    if(!root)return 0;
    const list=root.querySelector('.decision-list')||root;
    const present=new Set([...list.querySelectorAll('.decision-entry')].map(e=>String(e.dataset.id)));
    const fresh=rows.filter(row=>!present.has(String(row.id)));
    if(fresh.length)list.insertAdjacentHTML('beforeend',decisionEntriesHtml(fresh));
    LEDGER_LOADED+=fresh.length;
    sortLedgerEntries();
    applyLedgerFilter();
    return fresh.filter(row=>resultMatches(row.result,LEDGER_RESULTS)).length;
}

function renderDecisionLedger(rows){
    const root=document.getElementById('decisions');
    if(!rows.length){root.innerHTML='<div class="empty-state"><strong>'+esc({concluded:'还没有得出结论的记录',running:'此刻没有正在分析的记录',failed:'没有出错的记录'}[LEDGER_TAB]||'还没有符合条件的决策')+'</strong><p>'+esc({concluded:'机器人可能正在分析，或者这一轮没有标的通过筛选。看看「分析中」和「出错」两个标签。',running:'没有正在跑的分析——上一轮已经结束，下一轮还没开始。',failed:'一次都没失败过，或者失败记录已经被删掉了。'}[LEDGER_TAB]||'配置模型和平台后，机器人收到市场事件才会形成记录。')+'</p><a href="#overview">查看运行状态 →</a></div>';return}
    rememberOpenDetails();
    LEDGER_LOADED=rows.length;
    LEDGER_SERVER_OFFSET=rows.length;
    LEDGER_EXHAUSTED=pageExhausts(rows,LEDGER_FIRST_PAGE,LEDGER_RESULTS.size>0);
    for(const id of [...LEDGER_PICKED])if(!rows.some(row=>String(row.id)===id))LEDGER_PICKED.delete(id);
    root.innerHTML='<div class="decision-list">'+decisionEntriesHtml(rows)+'</div>'
        +'<div class="empty-state" id="ledgerFilterEmpty" hidden><strong>没有符合所选结果的记录</strong><p>取消一些结果标签，或者全部取消来显示所有记录。</p></div>'
        +'<p class="muted" id="ledgerMore">'+(LEDGER_EXHAUSTED?'没有更多记录了':'')+'</p>'
        +'<div id="ledgerSentinel"></div>';
    sortLedgerEntries();
    applyLedgerFilter();
    watchLedgerEnd();
    if(LEDGER_RESULTS.size)fillLedger();
}

/* The language the model writes its reasoning in follows the reader's browser until somebody chooses
   one. The server's own default has to be something, and it cannot see a browser; the page can, and
   it is the operator's browser that will be showing those sentences. Only an unchosen setting is
   filled in this way - an explicit choice, including one made in another browser, is never
   overridden - and anything that cannot be read or is not a supported language falls back to
   Chinese. */
function browserAgentLanguage(){
    try{
        const tags=[...(navigator.languages||[]),navigator.language].filter(Boolean).map(String);
        for(const tag of tags){
            const base=tag.toLowerCase().split('-')[0];
            if(base==='zh')return 'zh';
            if(base==='en')return 'en';
        }
    }catch(e){}
    return 'zh';
}

async function adoptBrowserLanguageIfUnchosen(settings){
    try{
        const field=(settings&&settings.fields||[]).find(f=>f.name==='agent_language');
        if(!field||field.configured)return false;
        const language=browserAgentLanguage();
        await post('/api/settings',{values:{agent_language:language}});
        return true;
    }catch(e){return false}
}

/* The same setting, on the page where its effect is read. Buried among twenty application settings it
   could not be found, and the ledger is where somebody notices the reasoning is in the wrong
   language. It changes what the model writes from the next round on; rows already written keep the
   language they were written in. */
async function syncLedgerLanguage(){
    const select=document.getElementById('ledgerLanguage');
    if(!select)return;
    try{
        const settings=await get('/api/settings');
        const field=(settings.fields||[]).find(f=>f.name==='agent_language');
        if(field)select.value=String(field.value||field.default||'zh');
    }catch(e){}
}

async function saveLedgerLanguage(language){
    const status=document.getElementById('ledgerLanguageStatus');
    try{
        await post('/api/settings',{values:{agent_language:language}});
        if(status){status.className='status good';status.textContent='已保存，下一轮起生效；已写好的记录保持原来的语言。'}
    }catch(e){
        if(status){status.className='status danger';status.textContent='没能保存：'+(e&&e.message||e)}
        syncLedgerLanguage();
    }
}

/* The three diagnostic tables sit behind panels labelled "open when debugging", and were fetched on
   every visit anyway - two and a half megabytes of raw model output for twenty rows, for panels
   almost nobody opens. They load when opened, and again when opened after a refresh. */
let LEDGER_PLATFORM_QUERY='';
const DIAGNOSTIC_PANELS={
    actions:{kind:'actions',columns:()=>[['时间',r=>new Date(r.created_at).toLocaleString()],['平台',r=>esc(r.platform)],['动作',r=>esc(r.action)],['结果',r=>detail(r)]]},
    turns:{kind:'turns',columns:()=>[['时间',r=>new Date(r.created_at).toLocaleString()],['平台/Provider',r=>esc(r.platform+' / '+r.provider)],['状态',r=>esc(r.status)],['内容',r=>detail(r)]]},
    steps:{kind:'steps',columns:()=>[['时间',r=>new Date(r.created_at).toLocaleString()],['平台/Provider',r=>esc(r.platform+' / '+r.provider)],['工具/状态',r=>esc((r.tool_name||'control')+' / '+r.status)],['内容',r=>detail(r)]]},
};

function resetDiagnosticPanels(){
    for(const id of Object.keys(DIAGNOSTIC_PANELS)){
        const host=document.getElementById(id);
        if(!host)continue;
        delete host.dataset.loaded;
        const panel=host.closest('details');
        if(panel&&panel.open)loadDiagnosticPanel(id);
        else host.innerHTML='';
    }
}

async function loadDiagnosticPanel(id){
    const host=document.getElementById(id),spec=DIAGNOSTIC_PANELS[id];
    if(!host||!spec||host.dataset.loaded==='1')return;
    host.dataset.loaded='1';
    host.innerHTML=skeletonRowsHtml(3);
    try{
        const answer=await get('/api/records?kind='+spec.kind+'&limit=20'+LEDGER_PLATFORM_QUERY);
        host.innerHTML=table(answer.items||[],spec.columns());
    }catch(e){
        delete host.dataset.loaded;
        host.innerHTML='<p class="danger">没能读取：'+esc(String(e&&e.message||e))+'</p>';
    }
}

if(typeof document!=='undefined'&&document.addEventListener)
    document.addEventListener('toggle',event=>{
        const panel=event.target;
        if(!panel||!panel.open||!panel.querySelector)return;
        for(const id of Object.keys(DIAGNOSTIC_PANELS))
            if(panel.querySelector('#'+id))loadDiagnosticPanel(id);
    },true);

const decisionReason=d=>d?.rationale||d?.reason||'没有记录说明';

/* A list row arrives without the context the model was given or its raw output - nearly all of a
   row's weight, and nothing the list draws. The first time a row is opened its full record is
   fetched and its timeline redrawn from it, so the evidence is there when somebody actually wants
   to read it and costs nothing when they do not. */
async function hydrateDecisionEntry(entry){
    if(!entry||entry.dataset.slim!=='1'||entry.dataset.hydrating==='1')return;
    entry.dataset.hydrating='1';
    const timeline=entry.querySelector('.decision-timeline');
    if(timeline)timeline.insertAdjacentHTML('beforebegin','<p class="muted decision-hydrating">正在读取完整记录…</p>'+skeletonRowsHtml(1));
    try{
        const full=await get('/api/decisions/detail?id='+encodeURIComponent(entry.dataset.id));
        const wrapper=document.createElement('div');
        wrapper.innerHTML=decisionEntriesHtml([full]);
        const fresh=wrapper.querySelector('.decision-entry');
        if(fresh){
            fresh.open=true;
            entry.replaceWith(fresh);
            requestReadable(fresh);
        }
    }catch(e){
        const note=entry.querySelector('.decision-hydrating');
        if(note)note.textContent='没能读取完整记录：'+(e&&e.message||e);
        delete entry.dataset.hydrating;
    }finally{
        for(const node of entry.querySelectorAll('.decision-skeleton'))node.remove();
    }
}

/* The first time a record without a plain-language restatement is opened, ask for one. It is made
   once and stored, so the cost is paid per record rather than per look; while it is on its way the
   card already shows the same facts assembled from the record itself, so nothing waits on it. */
async function requestReadable(entry){
    if(!entry||entry.dataset.readable==='1'||entry.dataset.readableAsked==='1')return;
    if(entry.dataset.slim==='1')return; // the full record arrives first and asks again
    if(entry.dataset.prose!=='1')return; // nothing written in prose here to restate
    entry.dataset.readableAsked='1';
    const note=entry.querySelector('.ledger-readable-note');
    if(note){note.hidden=false;note.textContent='正在让 AI 把这条整理成要点…'}
    try{
        const answer=await get('/api/decisions/readable?id='+encodeURIComponent(entry.dataset.id));
        if(!answer.available){
            if(note)note.textContent=answer.reason?'没有整理：'+answer.reason:'';
            if(note&&!answer.reason)note.hidden=true;
            return;
        }
        const found=entry.querySelector('[data-slot="found"]');
        const analysis=entry.querySelector('[data-slot="analysis"]');
        if(found&&answer.found)found.textContent=cleanReason(answer.found);
        if(analysis&&answer.analysis)analysis.textContent=cleanReason(answer.analysis);
        if(answer.headline){
            const summary=entry.querySelector('summary');
            let line=summary&&summary.querySelector('.decision-reason');
            if(summary&&!line){
                line=document.createElement('span');
                line.className='decision-reason';
                summary.insertBefore(line,summary.querySelector('.decision-forget'));
            }
            if(line)line.textContent=answer.headline;
        }
        entry.dataset.readable='1';
        if(note){note.hidden=true;note.textContent=''}
    }catch(e){
        if(note)note.textContent='没有整理：'+(e&&e.message||e);
        delete entry.dataset.readableAsked;
    }
}

if(typeof document!=='undefined'&&document.addEventListener)
    document.addEventListener('toggle',event=>{
        const target=event.target;
        if(target&&target.classList&&target.classList.contains('decision-entry')&&target.open){
            hydrateDecisionEntry(target);
            requestReadable(target);
        }
    },true);

function registerSeenStatuses(rows){
    /* A status the filter has never heard of is one an operator cannot filter by. */
    const filter=document.getElementById('statusFilter');
    if(!filter)return;
    for(const row of rows)
        if(row.status&&![...filter.options].some(o=>o.value===row.status)){
            const option=controlNode('option',decisionStatusTitle(row.status),filter);
            option.value=row.status;
        }
}

/* ---- Reading a record: four answers, the numbers, then the details, then the raw data. ----
   A trader scanning the ledger needs, in order: what was found, how it was looked at, what was
   concluded, and what came of it. The last two are recorded facts and are shown exactly as recorded;
   only the first two are prose, and those are the parts an AI may restate. */

const ACTION_LABEL={HOLD:'观望',BUY:'买入',SELL:'卖出',CANCEL:'撤单'};
const pct=v=>(v===null||v===undefined||v==='')?null:Math.round(Number(v)*1000)/10;
const num=v=>(v===null||v===undefined||v==='')?null:Number(v);
const money=v=>num(v)===null?'—':(Math.round(Number(v)*100)/100).toString();

function remainingText(seconds){
    const s=num(seconds);
    if(s===null)return '';
    if(s<=0)return '已到期';
    if(s<3600)return Math.max(1,Math.round(s/60))+' 分钟后结算';
    if(s<172800)return Math.round(s/3600)+' 小时后结算';
    return Math.round(s/86400)+' 天后结算';
}

function isDiscovery(r){
    return r.context?.stage==='discovery'||String(r.strategy_name||'').endsWith(':discovery');
}

/* A row written while no strategy plugin was ready left its strategy empty: the runtime recorded
   the configured name - empty in that case - rather than the built-in strategy it actually ran. */
const STRATEGY_TITLES={'':'内置决策策略',built_in:'内置决策策略','built_in:discovery':'内置发现策略'};
const strategyTitle=name=>STRATEGY_TITLES[name||'']??name;
/* "codex>claude" is the order the services were tried in. */
const providerTitle=name=>name?String(name).split('>').map(serviceTitle).join(' → '):'没有调用到模型';

function marketTitle(r){
    return r.context?.market?.title||(r.market_topic_id?'市场 #'+r.market_topic_id:'这条记录没有关联市场');
}
function candidateTitle(r,id){
    const key=String(id);
    const listed=(r.context?.candidates||[]).find(c=>c&&String(c.topic_id)===key);
    return r.context?.candidate_titles?.[key]||listed?.title||('市场 #'+key);
}

const TOOL_LABEL={
    SEARCH_OTHER_PLATFORMS:'跨平台搜同类市场',TOPIC_DETAIL:'看市场详情',OUTCOME_BOOK:'读订单簿',TOPIC_HISTORY:'看以往记录',RECALL_MEASUREMENTS:'回看效果统计',
    GET_TOPIC:'看市场详情',GET_ORDER_BOOK:'读订单簿',COMPARE_OUTCOMES:'对比各结果价格',LIST_TOPICS:'列出市场',LIST_PLATFORMS:'查已连接的平台',
    ACCOUNT_FUNDS:'查可用资金',ENSURE_FUNDS:'申请资金',FUNDING_STATUS:'查资金申请进度',OUTCOME_WON:'查揭标结果',SYNC_TIME:'对时',GET_QUOTE:'询价',
    PLACE_ORDER:'下单',CANCEL_ORDERS:'撤单',REDEEM:'兑付',TRANSFER:'转账',READ_ACCOUNT:'查账户',
    SEARCH_WEB:'网页搜索',SEARCH_MARKETS:'搜其他预测市场',FETCH_URL:'读网页',REFRESH_MARKET:'刷新行情',GET_KLINES:'看价格走势',RECALL_HISTORY:'回看这个市场的记录',
};
const stepLabel=step=>step.tool?(TOOL_LABEL[String(step.tool).toUpperCase()]||String(step.tool)):(step.consulted_by?'回答反问':'查询');
/* Models sometimes wrap an answer in the tag they were asked for, and a length limit can cut the
   closing tag in half - which is how "</analysi" reached a card. */
const cleanReason=text=>String(text||'').replace(/<\/?[a-zA-Z_][\w-]*\s*\/?>|<\/[a-zA-Z_][\w-]*$/g,'').trim();

function stepSubject(r,step){
    const a=step.arguments||{};
    if(a.query)return '「'+a.query+'」';
    if(a.topic_id)return candidateTitle(r,a.topic_id);
    if(a.url)return a.url;
    return '';
}

function toolsText(r){
    /* What the model went and looked at, by name - not how many turns it took to answer. */
    const steps=Array.isArray(r.research)?r.research:null;
    const count=steps?steps.length:Number(r.research_count||0);
    if(!count)return '';
    if(!steps)return '查了 '+count+' 次';
    const tally=new Map();
    for(const step of steps){const label=stepLabel(step);tally.set(label,(tally.get(label)||0)+1)}
    return '查了 '+count+' 次：'+[...tally].map(([label,n])=>label+(n>1?' ×'+n:'')).join('、');
}

const LIMIT_NAMES={session:'会话额度',weekly:'本周额度',usage:'用量额度',daily:'今日额度',monthly:'本月额度'};
const FAILURE_KIND_TEXT={rate_limit:'额度用完或被限流',auth:'登录失效或没有权限',transient:'连接不稳定（超时或网络故障）',contract:'返回的内容不符合要求',unknown:'调用失败',unavailable:'没有可用的服务'};
function providerFailureText(error){
    /* "All decision providers failed: claude: [rate_limit] Claude failed: … session limit · resets 12:20pm (UTC)"
       becomes "Claude：会话额度用完，12:20pm (UTC) 恢复". The full text stays under 细节. */
    const text=String(error||'');
    const body=text.replace(/^(All decision providers failed|No decision provider is (?:ready|available)):\s*/,'');
    const parts=body===text?[]:[...body.matchAll(/(\w+): \[(\w+)\] ([\s\S]*?)(?=; \w+: \[\w+\] |$)/g)];
    if(!parts.length)return text.slice(0,200);
    return parts.map(([,name,kind,message])=>{
        const limit=(message.match(/hit your ([\w -]+?) limit/i)||[])[1];
        let line=serviceTitle(name)+'：'+(limit?(LIMIT_NAMES[limit.trim().toLowerCase()]||limit+' 额度')+'用完':(FAILURE_KIND_TEXT[kind]||'调用失败'));
        const reset=(message.match(/(?:resets?|try again at)\s+([^\n]+?)(?:\.\s|\.$|\n|$)/i)||[])[1];
        if((limit||kind==='rate_limit')&&reset)line+='，'+reset.trim()+' 恢复';
        const waited=(message.match(/timed out after (\d+)s/i)||[])[1];
        if(waited)line+='（等了 '+waited+' 秒没有响应）';
        return line;
    }).join('；');
}

function venueErrorText(text){
    const s=String(text||'');
    if(/No orderbook exists/i.test(s))return '平台上这个结果没有订单簿';
    const code=(s.match(/HTTP (\d{3})/)||[])[1];
    return code?'平台返回 HTTP '+code:s.slice(0,80);
}
function candidateStateText(c){
    if(c.lookup_error)return '没读到详情：'+venueErrorText(c.lookup_error);
    if(c.book_error)return '没读到价格：'+venueErrorText(c.book_error);
    if(c.why_not_priced)return c.why_not_priced==='no market in this event is open for trading'?'这个事件下没有开放交易的市场':String(c.why_not_priced);
    if(c.book_one_sided)return '只有单边报价';
    if('spread' in c)return '读到了价格'+(c.priced_market?'（按「'+c.priced_market+'」）':'');
    if(c.verified)return '读到了详情，没有读价格';
    return '本轮没去核实';
}
const compactUsd=v=>{const n=num(v);if(n===null)return '—';if(n>=1e6)return '$'+Math.round(n/1e5)/10+'M';if(n>=1e3)return '$'+Math.round(n/100)/10+'k';return '$'+Math.round(n)};

function foundText(r){
    if(isDiscovery(r)){
        const candidates=Array.isArray(r.context?.candidates)?r.context.candidates:null;
        const total=r.context?.candidate_count??candidates?.length;
        const priced=r.context?.priced_count??(candidates?candidates.filter(c=>c&&'spread' in c).length:undefined);
        if(total===undefined)return '这一轮的候选市场没有存下来';
        return total+' 个候选市场'+(priced!==undefined?'，其中 '+priced+' 个读到了真实价差':'');
    }
    const market=marketTitle(r);
    const side=r.context?.outcome?.name;
    const bid=r.context?.order_book?.best_bid,ask=r.context?.order_book?.best_ask;
    const implied=pct(r.context?.outcome?.displayed_probability);
    const parts=[market+(side?' · '+side:'')];
    if(bid!==undefined||ask!==undefined)parts.push('买一 '+(bid??'—')+' / 卖一 '+(ask??'—'));
    if(implied!==null)parts.push('市场隐含 '+implied+'%');
    const left=remainingText(r.context?.seconds_remaining);
    if(left)parts.push(left);
    return parts.join('，');
}

function analysisText(r){
    const failed=r.group==='failed';
    const tools=toolsText(r);
    if(isDiscovery(r)){
        if(failed)return (tools?tools+'，':'')+'模型调用失败，没分析完';
        return tools||'没有追加查询，直接从候选列表里挑';
    }
    const d=r.final_decision||r.proposed_decision||{};
    const est=pct(d.estimated_probability),conf=pct(d.confidence);
    const implied=pct(r.context?.outcome?.displayed_probability);
    const parts=[];
    if(est!==null)parts.push('模型估计 '+est+'%'+(implied!==null?'（市场 '+implied+'%）':''));
    if(conf!==null)parts.push('信心 '+conf+'%');
    if(tools)parts.push(tools);
    if(failed)parts.push('模型调用失败，没分析完');
    else if(r.status==='STARTED')parts.push('还在分析');
    return parts.join('，')||'模型没有给出估计';
}

function conclusionHtml(r){
    /* The decision itself, as a fact: which action and at what size. */
    if(r.group==='failed'||r.status==='PROVIDER_ERROR')return '<span class="conclusion none">没能得出结论</span>';
    if(r.status==='STARTED')return '<span class="conclusion none">还在分析</span>';
    const d=r.final_decision||r.proposed_decision||{};
    if(isDiscovery(r)){
        const picks=d.selections||[];
        if(!picks.length)return '<span class="conclusion hold">本轮不选</span>';
        return '<span class="conclusion buy">选出 '+picks.length+' 个</span> '
            +esc(picks.slice(0,3).map(p=>candidateTitle(r,p.topic_id)).join('、'))
            +(picks.length>3?' 等':'');
    }
    const action=String(d.action||'').toUpperCase();
    const label=ACTION_LABEL[action]||action||'没有给出动作';
    const size=[];
    if(action==='BUY'&&num(d.notional_usdt))size.push(money(d.notional_usdt)+' USDT');
    if(action==='SELL'&&num(d.quantity_fraction))size.push('卖出持仓的 '+Math.round(Number(d.quantity_fraction)*100)+'%');
    if((action==='BUY'||action==='SELL')&&d.order_type)
        size.push(String(d.order_type).toUpperCase()==='LIMIT'?'限价 '+d.limit_price:'市价');
    return '<span class="conclusion '+esc(action.toLowerCase())+'">'+esc(label)+'</span>'
        +(size.length?' '+esc(size.join('，')):'');
}

function resultHtml(r){
    /* What happened when the decision was carried out, and later, whether it was right. */
    if(r.status==='STARTED')return '<span class="muted">还没有结果</span>';
    if(r.status==='PROVIDER_ERROR')return '<span class="danger">没有执行：'+esc(providerFailureText(r.error)||'模型调用失败')+'</span>';
    if(r.group==='failed')return '<span class="danger">'+esc(decisionStatusTitle(r.status))+(r.error?'：'+esc(String(r.error).slice(0,220)):'')+'</span>';
    if(isDiscovery(r)){
        const d=r.final_decision||{};
        const picks=(d.selections||[]).length;
        const next=num(d.next_scan_seconds);
        const parts=[picks?'交给决策阶段分析 '+picks+' 个标的':'本轮没有标的进入决策'];
        if(next)parts.push('约 '+Math.max(1,Math.round(next/60))+' 分钟后再看');
        if((d.next_survey_queries||[]).length)parts.push('下次去搜：'+d.next_survey_queries.join('、'));
        return esc(parts.join('；'));
    }
    if(r.status==='RISK_REJECTED')
        return '<span class="danger">被规则拒绝，没有下单</span>'+(r.risk_decision?.reason?'：'+esc(r.risk_decision.reason):'');
    const x=r.execution||{};
    const status=String(x.status||'').toUpperCase();
    if(status==='NO_ACTION')return '<span class="muted">按结论不下单</span>';
    if(status==='NO_POSITION')return '<span class="muted">没有可卖的持仓，没有下单</span>';
    if(x.canceled||x.failed){
        const done=(x.canceled||[]).length,bad=(x.failed||[]).length;
        return esc('撤掉 '+done+' 个挂单'+(bad?'，'+bad+' 个没撤掉':''))+(x.simulated?' <span class="badge">模拟</span>':'');
    }
    const order=x.order||{};
    if(!order.order_id&&!status)return '<span class="muted">没有执行记录</span>';
    const simulated=String(order.order_id||x.order_id||'').startsWith('paper-');
    const side=ACTION_LABEL[String(order.side||x.action||'').toUpperCase()]||'';
    const statusText={FILLED:'已成交',OPEN:'已挂单，尚未成交',CANCELED:'已撤销',REJECTED:'被平台拒绝',FAILED:'下单失败'}[status]||('平台状态 '+status);
    let text=statusText;
    if(status==='FILLED'||status==='OPEN')
        text+='：'+side+' '+money(order.quantity)+' 份 @ '+order.price+'，金额 '+money(order.notional)+' USDT（手续费 '+money(order.fee)+'）';
    let html=(simulated?'<span class="badge">模拟</span> ':'')+esc(text);
    const settle=r.settlement;
    if(settle&&settle.settled){
        const profit=Number(settle.profit);
        html+='<br><strong>揭标：'+(settle.won?'赢':'输')+'</strong>，回收 '+esc(money(settle.payout))
            +' USDT，<span class="'+(profit>=0?'good':'danger')+'">盈亏 '+(profit>=0?'+':'')+esc(money(profit))+' USDT</span>';
    }else if(settle&&settle.settled===false){
        html+='<br><span class="muted">尚未揭标</span>';
    }
    return html;
}

function hasProse(r){
    /* Whether there is anything a restatement could improve on. A round that failed before it
       reached a conclusion has an error and nothing else: the card already says what happened, and
       asking a model to rephrase it spends a call to produce the same sentence. */
    const d=r.final_decision||r.proposed_decision||{};
    return Boolean(cleanReason(d.rationale)||cleanReason(d.skipped_reason)||cleanReason(d.pacing_reason)
        ||(d.selections||[]).some(p=>cleanReason(p&&p.reason))
        ||cleanReason(r.risk_decision&&r.risk_decision.reason));
}

function headlineText(r){
    return cleanReason(r.readable?.headline)||cleanReason((r.final_decision||{}).headline)||'';
}

function keyNumbersHtml(r){
    if(isDiscovery(r))return '';
    const d=r.final_decision||r.proposed_decision||{};
    const cells=[
        ['买一',r.context?.order_book?.best_bid],['卖一',r.context?.order_book?.best_ask],
        ['市场隐含',pct(r.context?.outcome?.displayed_probability)!==null?pct(r.context?.outcome?.displayed_probability)+'%':null],
        ['模型估计',pct(d.estimated_probability)!==null?pct(d.estimated_probability)+'%':null],
        ['信心',pct(d.confidence)!==null?pct(d.confidence)+'%':null],
    ].filter(([,v])=>v!==null&&v!==undefined);
    if(!cells.length)return '';
    return '<div class="ledger-numbers">'+cells.map(([k,v])=>'<span><small>'+esc(k)+'</small><b>'+esc(v)+'</b></span>').join('')+'</div>';
}

function detailsHtml(r){
    /* Readable, but everything: the full reasoning, each step taken and why, what the rules said. */
    const d=r.final_decision||r.proposed_decision||{};
    const parts=[];
    if(isDiscovery(r)){
        const picks=d.selections||[];
        if(picks.length)parts.push('<h5>为什么选这些</h5><ul>'+picks.map(p=>'<li><b>'+esc(candidateTitle(r,p.topic_id))+'</b>：'+esc(cleanReason(p.reason))+'</li>').join('')+'</ul>');
        if(d.skipped_reason)parts.push('<h5>为什么没选</h5><p>'+esc(d.skipped_reason)+'</p>');
        if(d.pacing_reason)parts.push('<h5>下次什么时候再看、为什么</h5><p>'+esc(d.pacing_reason)+'</p>');
        const candidates=r.context?.candidates;
        if(Array.isArray(candidates)&&candidates.length)
            parts.push('<h5>候选市场</h5><div class="table-scroll"><table><thead><tr><th>市场</th><th>买一 / 卖一</th><th>价差占中间价</th><th>流动性</th><th>结算</th><th>情况</th></tr></thead><tbody>'
                +candidates.map(c=>'<tr><td>'+esc(c.title||('市场 #'+c.topic_id))+'</td><td>'+esc(c.best_bid!==undefined||c.best_ask!==undefined?(c.best_bid??'—')+' / '+(c.best_ask??'—'):'—')+'</td><td>'+esc(c.spread_pct_of_mid!==undefined&&c.spread_pct_of_mid!==null?c.spread_pct_of_mid+'%':'—')+'</td><td>'+esc(compactUsd(c.liquidity_usdt))+'</td><td>'+esc(remainingText(c.seconds_remaining)||'—')+'</td><td>'+esc(candidateStateText(c))+'</td></tr>').join('')
                +'</tbody></table></div>');
    }else{
        if(d.rationale)parts.push('<h5>模型的完整理由</h5><p class="ledger-prose">'+esc(cleanReason(d.rationale))+'</p>');
        if(r.risk_decision)parts.push('<h5>规则检查</h5><p>'+esc(({ALLOW:'放行',REJECT:'拒绝',HALT:'停机',ADJUST:'调整'}[String(r.risk_decision.outcome||'').toUpperCase()]||r.risk_decision.outcome||'')+(r.risk_decision.reason?'：'+r.risk_decision.reason:''))+'</p>');
        if(r.settlement&&r.settlement.settled)parts.push('<h5>盈亏怎么算的</h5><p>'+esc('持有到结算：回收 '+money(r.settlement.payout)+' − 成本 '+money(r.settlement.cost)+'（成交金额加手续费）= '+money(r.settlement.profit)+' USDT')+'</p>');
    }
    const steps=r.research;
    if(Array.isArray(steps)&&steps.length)
        parts.push('<h5>一步步查了什么</h5><ol class="ledger-steps">'+steps.map(step=>{const subject=stepSubject(r,step);return '<li><b>'+esc(stepLabel(step))+'</b>'+(subject?' · '+esc(subject):'')+(step.reason?'：'+esc(cleanReason(step.reason)):'')+'</li>'}).join('')+'</ol>');
    else if(r.slim)parts.push('<p class="muted">正在读取完整记录…</p>');
    if(r.error)parts.push('<h5>原始报错</h5><p class="ledger-prose">'+esc(r.error)+'</p>');
    parts.push('<p class="muted">模型服务：'+esc(providerTitle(r.provider))+' · 策略：'+esc(strategyTitle(r.strategy_name))+'</p>');
    return parts.join('');
}

function decisionEntriesHtml(rows){
    registerSeenStatuses(rows);
    const opened=new Set(
        [...document.querySelectorAll('#decisions details.decision-entry[open]')].map(e=>e.dataset.id)
    );
    return rows.map(r=>{
        const readable=r.readable||null;
        const headline=headlineText(r);
        const kind=isDiscovery(r)?'发现':'决策';
        const title=isDiscovery(r)?'发现轮次':marketTitle(r);
        return '<details class="decision-entry" data-id="'+esc(r.id)+'" data-created="'+esc(r.created_at)+'" data-result="'+esc(String(r.result||r.status||'').toUpperCase())+'"'+(r.slim?' data-slim="1"':'')+(readable?' data-readable="1"':'')+(hasProse(r)?' data-prose="1"':'')+' '+(opened.has(String(r.id))?'open':'')+'>'
            +'<summary><input type="checkbox" class="decision-pick" aria-label="选中这条" data-pick="'+esc(r.id)+'"'+(LEDGER_PICKED.has(String(r.id))?' checked':'')+' onclick="event.stopPropagation()" onchange="pickDecision(this)"><span class="decision-meta">'+esc(new Date(r.created_at).toLocaleString())+' · '+esc(r.platform)+' · '+kind+'</span>'
            +'<span class="decision-title">'+esc(title)+'</span>'
            +'<span class="decision-outcome">'+conclusionHtml(r)+'</span>'
            +(headline?'<span class="decision-reason">'+esc(headline)+'</span>':'')
            /* Measurements are read off these rows - which provider gets asked first, how the strategy
               calibrates - so an entry recording a fault since fixed keeps arguing its case until it
               is removed. On the row itself, because deciding a record is junk does not require
               reading it again. What the venue actually did is refused separately and stays. */
            +'<button class="decision-forget" title="删除这条记录" aria-label="删除这条记录" onclick="forgetDecision(event,'+esc(r.id)+')">×</button></summary>'
            +'<div class="ledger-card">'
            +'<dl class="ledger-four">'
            +'<dt>发现了什么</dt><dd data-slot="found">'+esc(cleanReason(readable?.found)||foundText(r))+'</dd>'
            +'<dt>怎么分析的</dt><dd data-slot="analysis">'+esc(cleanReason(readable?.analysis)||analysisText(r))+'</dd>'
            +'<dt>结论</dt><dd>'+conclusionHtml(r)+'</dd>'
            +'<dt>结果</dt><dd>'+resultHtml(r)+'</dd>'
            +'</dl>'
            +(readable?'':'<p class="muted ledger-readable-note" hidden></p>')
            +keyNumbersHtml(r)
            +'<details class="ledger-more"><summary>细节</summary>'+detailsHtml(r)+'</details>'
            +'<details class="ledger-more"><summary>原始数据</summary>'
            +detail({context:r.context,research:r.research,final:r.final_decision,risk:r.risk_decision,execution:r.execution,settlement:r.settlement,model_output:r.model_raw_output},r.id+':raw')
            +'</details>'
            +'</div></details>';
    }).join('');
}


/* ---- Deleting many at once ----
   Two ways, for two situations. Ticking rows is for "these ones": whatever is ticked on screen, no
   more. The category button is for "all of this": everything under the current tab and filters,
   loaded or not, counted first so the confirmation names a number, and bounded to the rows that
   existed when it was counted. Executed trades are kept either way, and a running analysis that is
   deleted is told to stop - the same rules as deleting one. */
let LEDGER_PICKED=new Set();
const LEDGER_TAB_TITLE={concluded:'有结论',running:'分析中',failed:'出错'};

function ledgerCategoryMatch(){
    const applied=new URLSearchParams(String(typeof LEDGER_QUERY==='string'?LEDGER_QUERY:'').replace(/^&/,''));
    return {group:LEDGER_TAB,results:[...LEDGER_RESULTS].join(','),
        platform:applied.get('platform')||'',provider:applied.get('provider')||'',status:applied.get('status')||''};
}
function ledgerCategoryLabel(match){
    const chosen=(RESULT_CHIPS[match.group]||[]).filter(([code])=>LEDGER_RESULTS.has(code)).map(([,label])=>label);
    const parts=[LEDGER_TAB_TITLE[match.group]||match.group];
    if(chosen.length)parts.push(chosen.join('、'));
    if(match.platform)parts.push('平台 '+match.platform);
    if(match.provider)parts.push('模型服务 '+match.provider);
    if(match.status)parts.push('状态 '+decisionStatusTitle(match.status));
    return parts.join(' · ');
}
function shownEntries(){
    return [...document.querySelectorAll('#decisions .decision-entry')].filter(entry=>!entry.hidden);
}
function renderLedgerBulk(){
    if(typeof document==='undefined'||!document.getElementById)return;
    const count=document.getElementById('ledgerPickCount');
    const button=document.getElementById('ledgerForgetPicked');
    const all=document.getElementById('ledgerPickAll');
    const category=document.getElementById('ledgerForgetCategory');
    if(count)count.textContent=LEDGER_PICKED.size?'已选 '+LEDGER_PICKED.size+' 条':'勾选记录可以一起删除';
    if(button){button.disabled=!LEDGER_PICKED.size;button.textContent=LEDGER_PICKED.size?'删除所选 '+LEDGER_PICKED.size+' 条':'删除所选'}
    if(all){
        const shown=shownEntries();
        const picked=shown.filter(entry=>LEDGER_PICKED.has(String(entry.dataset.id))).length;
        all.checked=Boolean(shown.length)&&picked===shown.length;
        all.indeterminate=picked>0&&picked<shown.length;
    }
    if(category)category.textContent='删除「'+ledgerCategoryLabel(ledgerCategoryMatch())+'」全部…';
}
function pickDecision(box){
    if(box.checked)LEDGER_PICKED.add(String(box.dataset.pick));else LEDGER_PICKED.delete(String(box.dataset.pick));
    renderLedgerBulk();
}
function pickAllShown(checked){
    for(const entry of shownEntries()){
        const box=entry.querySelector('.decision-pick');
        if(box)box.checked=checked;
        if(checked)LEDGER_PICKED.add(String(entry.dataset.id));else LEDGER_PICKED.delete(String(entry.dataset.id));
    }
    renderLedgerBulk();
}
function forgetSummary(answer){
    const parts=['已删除 '+answer.decisions+' 条记录'];
    if(answer.cancelled_in_progress)parts.push(answer.cancelled_in_progress+' 条正在分析的已让它停下，不会再花模型调用，也不会据此下单');
    if(answer.kept_executed)parts.push(answer.kept_executed+' 条在平台上真实交易过，保留没删（删掉会让账本和余额对不上）');
    return parts.join('；')+'。';
}
async function forgetPicked(){
    const ids=[...LEDGER_PICKED].map(Number).filter(Number.isFinite);
    if(!ids.length)return;
    if(!confirm('删除选中的 '+ids.length+' 条记录？\n\n这些记录参与模型服务排名和策略校准，删掉后这些统计会变。在平台上真实交易过的记录会保留；正在分析的会让它停下。'))return;
    try{
        const answer=await post('/api/decisions/forget',{decision_ids:ids});
        const kept=new Set((answer.kept_ids||[]).map(String));
        let removed=0;
        // Taken out where they stand, so the reader keeps their place and whatever they have open.
        for(const id of ids){
            if(kept.has(String(id)))continue;
            const entry=document.querySelector('#decisions .decision-entry[data-id="'+id+'"]');
            if(entry){entry.remove();removed+=1}
        }
        LEDGER_SERVER_OFFSET=Math.max(0,LEDGER_SERVER_OFFSET-removed);
        LEDGER_PICKED=new Set([...LEDGER_PICKED].filter(id=>kept.has(id)));
        renderLedgerBulk();
        refreshLedgerTabCounts(typeof LEDGER_PLATFORM_QUERY==='string'?LEDGER_PLATFORM_QUERY:'');
        showOperationFeedback(forgetSummary(answer),answer.decisions?'good':'danger',!answer.decisions);
    }catch(e){showOperationFeedback('删除失败：'+e.message,'danger',true)}
}
async function forgetCategory(){
    const match=ledgerCategoryMatch(),label=ledgerCategoryLabel(match);
    let preview;
    try{preview=await post('/api/decisions/forget',{match,dry_run:true})}
    catch(e){showOperationFeedback('没能统计「'+label+'」：'+e.message,'danger',true);return}
    if(!preview.matching){showOperationFeedback('「'+label+'」下没有记录。');return}
    const lines=['删除「'+label+'」下的全部 '+preview.matching+' 条记录？',''];
    if(preview.kept_executed)lines.push('其中 '+preview.kept_executed+' 条在平台上真实交易过，会保留。');
    if(preview.in_progress)lines.push('其中 '+preview.in_progress+' 条正在分析，会让它停下。');
    lines.push('只删现在已有的记录，确认之后才产生的记录不受影响。这些记录参与模型服务排名和策略校准，删掉后这些统计会变。');
    if(!confirm(lines.join('\n')))return;
    try{
        const answer=await post('/api/decisions/forget',{match,until_id:preview.until_id});
        LEDGER_PICKED.clear();
        showOperationFeedback(forgetSummary(answer),answer.decisions?'good':'danger',!answer.decisions);
        refreshAudit();
    }catch(e){showOperationFeedback('删除失败：'+e.message,'danger',true)}
}

async function forgetDecision(event,id){
    /* Inside a <summary>, a click opens the entry unless it is stopped - so the confirm would be
       answered behind a panel that had just sprung open. */
    event.preventDefault();event.stopPropagation();
    if(!confirm('删除决策 #'+id+'？\n\n这条记录参与 provider 排名与策略校准，删掉之后这些统计会变。已经在平台上发生过的动作不会被删除。'))return;
    try{
        const answer=await post('/api/decisions/forget',{decision_ids:[id]});
        if(!answer.decisions&&answer.kept_executed)
            showOperationFeedback('没有删除：这次决策已经产生 '+answer.kept_executed+' 条真实平台动作，删掉记录会让账本与余额对不上。','danger',true);
        else
            showOperationFeedback('已删除决策 '+answer.decisions+' 条、模型往返 '+answer.provider_turns+' 条、工具步骤 '+answer.agent_steps+' 条'+(answer.cancelled_in_progress?'；其中 '+answer.cancelled_in_progress+' 条正在分析，已让它停下，不会再花模型调用、也不会据此下单':'')+'。');
        LEDGER_PICKED.delete(String(id));
        refreshAudit();
    }catch(e){showOperationFeedback('删除失败：'+e.message,'danger',true)}
}


/* Plugin notices: the plugin declares what it may need to say, the framework only renders it.
   Nothing here knows which plugin it is talking to or what any notice means. */
async function renderPluginNotices(card,kind,plugin){
    const host=controlNode('div','',card);host.className='plugin-notices';
    host.innerHTML='<p class="muted">正在读取插件状态…</p>';
    let payload;
    try{payload=await post('/api/plugins/notices',{kind,name:plugin.name})}
    catch(e){host.innerHTML='<p class="danger">读取插件状态失败：'+esc(e.message)+'</p>';return}
    host.replaceChildren();
    for(const notice of payload.notices||[]){
        const box=controlNode('section','',host);box.className='notice-card';
        const head=controlNode('div','',box);head.className='section-heading';
        controlNode('h5',notice.title,head);
        if(notice.description)controlNode('p',notice.description,box).className='muted';
        if(notice.error){controlNode('p','插件报告错误：'+notice.error,box).className='danger';continue}
        const body=controlNode('pre','',box);body.className='notice-content';
        body.textContent=JSON.stringify(notice.content??{},null,2);
        const verbs=[['confirm',notice.action_label,notice.action_fields||[]],['dismiss',notice.dismiss_label,notice.dismiss_fields||[]]].filter(v=>v[1]);
        if(!verbs.length)continue;
        const inputs=new Map();
        for(const [verb,,fields] of verbs)for(const f of fields){
            if(inputs.has(f.name))continue;
            const wrap=controlNode('label',f.label+' ',box);wrap.className='field';
            const input=controlNode(f.multiline?'textarea':'input','',wrap);
            input.placeholder=f.placeholder||'';inputs.set(f.name,input);
        }
        const bar=controlNode('div','',box);bar.className='toolbar';
        const status=controlNode('span','',bar);status.className='status muted';
        const run=async(action,label)=>{
            const button=controlNode('button',label,bar);
            if(action==='confirm')button.className='primary';
            button.onclick=async()=>{
                button.disabled=true;setOperationStatus(status,'处理中…','pending');
                try{
                    const values={};for(const [name,input] of inputs)values[name]=input.value;
                    const answer=await post('/api/plugins/notices/action',{kind,name:plugin.name,key:notice.key,action,values});
                    setOperationStatus(status,answer.message||(answer.ok?'完成':'未完成'),answer.ok?'':'danger');
                    /* Some answers are the point of pressing the button, not a report on it.
                       A plugin that returns `reveal` has produced something the operator has to
                       read and keep, so it is shown where it can be selected and copied - and the
                       refresh that would redraw this card is skipped, because redrawing it is what
                       used to make the answer vanish the instant it arrived. */
                    if(answer.reveal){
                        const shown=controlNode('div','',box);shown.className='notice-reveal';
                        const text=controlNode('pre',String(answer.reveal),shown);
                        text.className='notice-content';text.tabIndex=0;
                        const copy=controlNode('button','复制',shown);
                        copy.onclick=async()=>{
                            try{await navigator.clipboard.writeText(String(answer.reveal));copy.textContent='已复制'}
                            catch(e){getSelection().selectAllChildren(text);copy.textContent='已选中，按 Cmd/Ctrl+C'}
                        };
                        button.disabled=false;return;
                    }
                    if(answer.ok){renderPluginNotices(card,kind,plugin);host.remove()}
                    if(LAST_MANAGER)refreshAttention(LAST_MANAGER);
                }catch(e){setOperationStatus(status,e.message,'danger')}
                button.disabled=false;
            };
        };
        for(const [verb,label] of verbs)run(verb,label);
    }
    if(!host.children.length)host.remove();
}
