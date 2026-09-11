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
const serviceTitle = name => SERVICE_TITLES[name] || name || '未记录';
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
const decisionStatusTitle = value => ({STARTED:'分析中',PROVIDER_ERROR:'模型调用失败',RISK_REJECTED:'规则拒绝，未执行',EXECUTION_ERROR:'执行失败',COMPLETED:'流程已完成',HOLD:'观望，未下单',ERROR:'出现错误',FAILED:'失败',PENDING:'处理中',EXECUTED:'已提交执行',REJECTED:'被拒绝'}[String(value).toUpperCase()]||'平台状态：'+(value||'未记录'));
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

function renderManager(m) {
    LAST_MANAGER=m;
    // The provider form has only one DOM instance, even when reached from two pages.
    document.getElementById('modelConfigurations').replaceChildren();
    const manager=document.getElementById('pluginManager');manager.replaceChildren();
    document.getElementById('pluginCategoryHome').innerHTML='<h3>给机器人选择扩展能力</h3><p class="muted">交易平台、策略、研究，以及 Agent 行为风控和业务风控这两类过滤插件，都在这里管理。AI 账号、兼容 API、模型与调用顺序统一放在左侧“模型服务”，不在插件中心重复出现。</p>'+processStrip(['平台发现市场','策略 + 模型分析','工具补充证据','行为与风控检查','平台执行'])+'<div class="category-grid">'+PLUGIN_CENTER_KINDS.map(kind=>{const h=CATEGORY_HELP[kind],all=m.plugins[kind]||[],on=all.filter(p=>p.enabled).length;return '<a class="category-tile" href="#plugins/'+kind+'"><span class="eyebrow">'+esc(on+' / '+all.length+' 已启用')+'</span><h4>'+h.title+' <span aria-hidden="true">→</span></h4><p>'+h.role+'</p></a>'}).join('')+'</div>';
    document.getElementById('pluginCategoryNav').innerHTML='<a href="#plugins">全部分类</a>'+PLUGIN_CENTER_KINDS.map(k=>'<a href="#plugins/'+k+'">'+LABELS[k]+'</a>').join('');
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

function renderRuntime(r){
    if(r.setup)renderSetupGuide(r.setup);
    const root=document.getElementById('runtimeControl');
    // Periodic status refresh must not erase an unsaved pause choice.
    if(!root.dataset.dirty){
        root.innerHTML='<label class="pause-switch"><input id="pauseAll" type="checkbox" '+(r.robot_paused?'checked':'')+'> 暂停全部平台</label><div class="config-grid">'+Object.entries(r.platforms||{}).map(([name,p])=>'<article class="plugin"><div class="section-heading"><h4>'+esc(name)+'</h4><span class="badge '+(p.running?'ready':'')+'">'+(p.running?'运行中':p.paused?'已暂停':p.ready?'可启动但未运行':'插件报告未就绪')+'</span></div><label><input class="pausePlatform" type="checkbox" value="'+esc(name)+'" '+(p.paused?'checked':'')+'> 暂停此平台</label>'+(!p.ready?'<p><a href="'+configLink('api',name)+'">打开平台插件 →</a></p>':'')+(p.startup_reasons?.length?'<details class="diagnostic-detail"><summary>查看插件报告</summary><p>'+esc(p.startup_reasons.join('；'))+'</p></details>':'')+'</article>').join('')+'</div><p class="status '+(r.global_ready?'good':'muted')+'">'+(r.global_ready?'AI 服务和至少一个平台已报告可启动；是否正常运行以平台的“运行中”为准。':'机器人主链尚未满足：需要至少一个可用 AI 服务和一个可启动平台。')+'</p>'+(!r.global_ready?'<details><summary>查看具体原因</summary><p>'+esc((r.global_reasons||[]).join('；'))+'</p></details>':'');
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
function renderSetupGuide(setup){
    const titles={management_only:'界面验收测试实例：未启动机器人进程',not_running:'机器人未运行',paused:'机器人已暂停',partial:'机器人正在运行，但部分平台未运行',running:'机器人正在运行'};
    const descriptions={management_only:'当前访问的是仅用于界面验收的测试实例，未启用机器人运行进程；这不是配置问题。正常启动的实例会在模型服务和至少一个平台就绪且未暂停后立即运行。',not_running:'先处理“启动必需”中的未完成项。AI 服务和至少一个平台报告可启动且未暂停时，平台会立即自动启动。若条件已满足仍未运行，请展开启动错误。',paused:'当前是用户主动暂停状态，不是缺少必备配置。到本页下方取消暂停并保存后，已就绪平台会立即启动。',partial:'正在运行的平台不受其他平台或可选增强项影响。你可以继续处理未运行的平台，或关闭不使用的平台。',running:'平台扫描已启动，等待市场事件。可选增强项不影响启动；运行中也不代表一定会产生交易或盈利。'};
    const root=document.getElementById('gettingStarted'),previous=root.querySelector('#setupChecklist');
    const open=previous?previous.open:setup.state!=='running';
    const requiredSteps=setup.steps.filter(step=>step.required!==false),optionalSteps=setup.steps.filter(step=>step.required===false);
    const optionalRemaining=optionalSteps.filter(step=>!step.ready).length;
    const requiredSelected=SETUP_GUIDE_TAB!=='optional';
    const optionalList=optionalSteps.length?'<ol class="setup-checklist">'+optionalSteps.map(setupStepHtml).join('')+'</ol>':'<div class="empty-state compact"><strong>当前没有可选增强项</strong><p>无需处理，机器人仍可按启动必需项运行。</p></div>';
    root.className='readiness-panel '+(setup.state==='running'?'ready':'needs-attention');
    root.innerHTML='<div class="section-heading"><div><span class="eyebrow">运行状态</span><h3>'+esc(titles[setup.state])+'</h3><p>'+esc(descriptions[setup.state])+'</p></div><button onclick="refreshManager()">重新检查</button></div><details id="setupChecklist" '+(open?'open':'')+'><summary>启动向导 · '+setup.remaining+' 项必须处理</summary><div class="setup-tabs" role="tablist" aria-label="启动向导分类"><button id="setupRequiredTab" type="button" role="tab" data-setup-tab="required" aria-controls="setupRequiredPanel" aria-selected="'+requiredSelected+'" tabindex="'+(requiredSelected?'0':'-1')+'" onclick="selectSetupGuideTab(\'required\')">启动必需 <span class="setup-tab-count">'+setup.remaining+'</span></button><button id="setupOptionalTab" type="button" role="tab" data-setup-tab="optional" aria-controls="setupOptionalPanel" aria-selected="'+(!requiredSelected)+'" tabindex="'+(requiredSelected?'-1':'0')+'" onclick="selectSetupGuideTab(\'optional\')">可选增强 <span class="setup-tab-count">'+optionalRemaining+'</span></button></div><div id="setupRequiredPanel" class="setup-panel" role="tabpanel" aria-labelledby="setupRequiredTab" data-setup-panel="required" '+(requiredSelected?'':'hidden')+'><p class="setup-tab-intro">这些条件决定机器人能否启动。顶部数字只统计尚未完成的必需项。</p><ol class="setup-checklist">'+requiredSteps.map(setupStepHtml).join('')+'</ol></div><div id="setupOptionalPanel" class="setup-panel" role="tabpanel" aria-labelledby="setupOptionalTab" data-setup-panel="optional" '+(requiredSelected?'hidden':'')+'><p class="setup-tab-intro">这些能力用于增强分析、研究，或对动作做过滤检查；未就绪不会阻止机器人启动。</p>'+optionalList+'</div></details>'+(setup.runtime_reasons?.length?'<details class="diagnostic-detail"><summary>查看启动错误 / 运行条件</summary><p>'+setup.runtime_reasons.map(esc).join('<br>')+'</p><a href="#settings">检查程序设置 →</a></details>':'');
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

function renderDecisionLedger(rows){
    const root=document.getElementById('decisions');
    if(!rows.length){root.innerHTML='<div class="empty-state"><strong>还没有符合条件的决策</strong><p>配置模型和平台后，机器人收到市场事件才会形成记录。也可以清空筛选重新查询。</p><a href="#overview">查看运行状态 →</a></div>';return}
    const opened=new Set([...root.querySelectorAll('details.decision-entry[open]')].map(e=>e.dataset.id));
    const filter=document.getElementById('statusFilter');
    for(const row of rows)if(row.status&&![...filter.options].some(o=>o.value===row.status)){const option=controlNode('option',decisionStatusTitle(row.status),filter);option.value=row.status}
    const reason=d=>d?.rationale||d?.reason||'没有记录说明';
    root.innerHTML=rows.map(r=>{const d=r.final_decision||r.proposed_decision||{},stages=[
        ['发现了什么',r.context?.market?.title||r.market_topic_id||'没有记录市场标题',{context:r.context}],
        ['参考了什么',String(r.research?.length||0)+' 条研究结果，'+String(r.agent_steps??0)+' 个工具步骤',{research:r.research}],
        ['模型如何判断',reason(r.proposed_decision),{decision:r.proposed_decision,model_output:r.model_raw_output}],
        ['规则检查后',r.risk_decision?reason(r.risk_decision):'没有记录规则检查结果',{risk:r.risk_decision,final:r.final_decision}],
        ['实际执行与后续',r.error?String(r.error):r.execution?'已记录执行处理结果，请查看详情；这可能是跳过操作的记录，不代表已向平台下单':'未记录平台执行结果',{execution:r.execution,subsequent_observation:r.subsequent_observation}]
    ];return '<details class="decision-entry" data-id="'+esc(r.id)+'" '+(opened.has(String(r.id))?'open':'')+'><summary><span class="decision-meta">'+esc(new Date(r.created_at).toLocaleString())+' · '+esc(r.platform)+' · #'+esc(r.id)+'</span><span class="decision-title">'+esc(r.context?.market?.title||r.market_topic_id||'未记录市场')+'</span><span class="decision-outcome"><span class="badge">'+esc((r.final_decision?'最终：':'建议：')+actionTitle(d.action))+'</span><span>'+esc(decisionStatusTitle(r.status))+'</span></span><span class="decision-reason">'+esc(reason(d))+'</span><span class="text-link">查看决策过程</span></summary><p class="description">模型服务：'+esc(serviceTitle(r.provider))+' · 策略：'+esc(r.strategy_name||'未记录')+'。模型建议不等于成交，后续观察也不自动等于已实现盈亏。</p><ol class="decision-timeline">'+stages.map(([title,summary,raw])=>'<li><h4>'+title+'</h4><p>'+esc(summary)+'</p>'+detail(raw)+'</li>').join('')+'</ol></details>'}).join('');
}
