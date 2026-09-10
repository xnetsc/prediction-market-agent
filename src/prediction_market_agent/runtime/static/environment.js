/* Runtime diagnostics use the existing authenticated management transport. */
const environmentTime = value => value ? new Date(value).toLocaleString() : '—';
const environmentBytes = value => typeof value==='number' ? (value/1024/1024/1024).toFixed(2)+' GiB' : '—';
let ENVIRONMENT_REQUEST = null, ENVIRONMENT_ROUTES = [];
function resultAddress(route){return route?.result?.ips?.length?route.result.ips.join(' / '):route?.result?'未查到':'尚未查询'}
function renderEgressComparison(){
    const root=document.getElementById('egressComparison'),direct=ENVIRONMENT_ROUTES.find(r=>r.id==='direct'),inherited=ENVIRONMENT_ROUTES.find(r=>r.id==='inherited');
    root.replaceChildren();
    for(const [title,route] of [['服务器直连出口',direct],['统一继承代理出口',inherited]]){const card=controlNode('div','',root);card.className='egress-compare-card';controlNode('small',title,card);controlNode('strong',route?.error?'连接未就绪':resultAddress(route),card);controlNode('p',route?.error||(!route?.result?'点击“对比两种出口”后查询同一组目标。':route.proxy==='DIRECT'?'实际为直连。':'请求已通过继承代理。'),card).className=route?.error?'danger':'description'}
}
async function refreshEnvironment() {
    if (ENVIRONMENT_REQUEST) return ENVIRONMENT_REQUEST;
    ENVIRONMENT_REQUEST = (async () => {
        const status = document.getElementById('environmentStatus');
        try {
            const value = await get('/api/environment');
            const facts = [
                ['应用 / Python',value.app_version+' / '+value.python],
                ['系统 / 架构',value.os+' '+value.os_release+' / '+value.architecture],
                ['主机名 / 进程 ID',value.hostname+' / '+value.pid],
                ['检测到容器标记',value.container_marker?'是（不代表宿主机系统）':'未发现（不保证不是容器）'],
                ['逻辑 CPU 数量',value.cpu_count??'—'],
                ['管理服务启动 / 运行时长',environmentTime(value.started_at)+' / '+Math.floor(value.uptime_seconds/60)+' 分钟'],
                ['工作目录',value.working_directory],
                ['磁盘剩余 / 总量',environmentBytes(value.disk.free_bytes)+' / '+environmentBytes(value.disk.total_bytes)],
                ['主机名解析地址（非完整网卡列表）',value.hostname_addresses.join(', ')||'不可用'],
                ['本次请求来源（可能为反向代理）',value.request_peer||'不可用'],
                ['ASGI 服务地址 / 访问入口',JSON.stringify(value.asgi_server)+' / '+value.request_origin],
                ...Object.entries(value.files).map(([name,item])=>[name,item.path+' · '+(item.error||(!item.exists?'文件未创建':item.bytes+' bytes'))])
            ];
            document.getElementById('environmentFacts').innerHTML='<details><summary>查看系统、进程、路径与存储详情</summary>'+table(facts,[['项目',row=>esc(row[0])],['当前值',row=>esc(row[1])]])+'</details>';
            document.getElementById('environmentSummary').textContent=value.os+' · '+(value.container_marker?'容器中运行':'服务器环境')+' · 已运行 '+Math.floor(value.uptime_seconds/60)+' 分钟 · 磁盘剩余 '+environmentBytes(value.disk.free_bytes);
            ENVIRONMENT_ROUTES=value.routes;
            renderEgressComparison();
            const select=document.getElementById('egressRoute'),selected=select.value;
            select.replaceChildren();
            for(const route of value.routes){const option=document.createElement('option');option.value=route.id;option.textContent=routeDisplayName(route)+(route.error?'（需要配置）':'');select.append(option)}
            if([...select.options].some(o=>o.value===selected))select.value=selected;
            renderEgressSelection();
            status.textContent='状态更新于 '+environmentTime(value.sampled_at);status.className='status muted';
        } catch(error) {status.textContent=error.message;status.className='status danger'}
    })();
    try {await ENVIRONMENT_REQUEST} finally {ENVIRONMENT_REQUEST=null}
}
async function probeEgress() {
    const button=document.getElementById('probeEgress'),status=document.getElementById('environmentStatus');
    button.disabled=true;status.textContent='正在查询所选网络路径…';
    try {await post('/api/environment/probe',{route_id:document.getElementById('egressRoute').value});await refreshEnvironment()}
    catch(error){status.textContent=error.message;status.className='status danger'}
    finally {renderEgressSelection()}
}
async function probeEgressComparison(){
    const button=document.getElementById('probeEgressComparison'),status=document.getElementById('environmentStatus');
    button.disabled=true;status.textContent='正在用直连和统一继承代理查询相同目标…';status.className='status muted';
    const errors=[];
    for(const routeId of ['direct','inherited'])try{await post('/api/environment/probe',{route_id:routeId})}catch(error){errors.push((routeId==='direct'?'直连':'继承代理')+'：'+error.message)}
    await refreshEnvironment();button.disabled=false;
    if(errors.length){status.textContent=errors.join('；');status.className='status danger'}else{status.textContent='对比完成：下方分别显示直连与继承代理的出口 IP。';status.className='status good'}
}
async function copyEgressIP(ip) {
    try {
        if(navigator.clipboard&&window.isSecureContext)await navigator.clipboard.writeText(ip);
        else {const field=document.createElement('textarea');field.value=ip;document.body.append(field);field.select();try{if(!document.execCommand('copy'))throw Error('浏览器不支持自动复制，请选中 IP 复制')}finally{field.remove()}}
        document.getElementById('environmentStatus').textContent='已复制 IP；请核对交易平台实际观察到的出口。';
    } catch(error){document.getElementById('environmentStatus').textContent=error.message}
}
function routeDisplayName(route){
    if(route.id==='direct')return '机器人服务器 · 不使用应用代理';
    if(route.id==='inherited')return '统一继承代理 · 插件选择 INHERIT 时使用';
    if(route.id==='environment')return '机器人服务器 · 跟随运行环境设置';
    const [kind,name]=route.id.split(':');
    return (kind==='decision_provider'?serviceTitle(name):name)+' · '+(LABELS[kind]||'插件')+'连接';
}
function renderEgressSelection(){
    const route=ENVIRONMENT_ROUTES.find(r=>r.id===document.getElementById('egressRoute').value),root=document.getElementById('egressResults');
    root.replaceChildren();document.getElementById('probeEgress').disabled=!route||Boolean(route.error);
    if(!route)return;
    const description=route.error?'这个连接还没有准备好，请先填写对应插件的网络设置。':route.proxy==='DIRECT'?'此连接不使用应用层代理，直接由服务器对外访问。系统 VPN 或云端网络仍可能改变出口。':'此连接通过代理服务器访问外部服务，查询显示的通常是代理的出口 IP。';
    controlNode('p',description,root).className='muted';
    if(route.error){const parts=route.id.split(':');if(parts.length>1){const link=controlNode('a','前往配置 →',root);link.href=configLink(parts[0],parts[1])}}
    const result=route.result;
    if(result){
        const headline=controlNode('div','',root);headline.className='egress-answer';
        controlNode('small',route.stale?'之前的结果已过期，请重新查询':'本次观察到的公网 IP',headline);
        controlNode('strong',result.ips.length?result.ips.join(' / '):result.status==='disabled'?'尚未启用查询服务':'未查到公网 IP',headline);
        controlNode('p','查询时间：'+environmentTime(result.checked_at),headline).className='description';
        if(!route.stale)for(const ip of result.ips){const button=controlNode('button','复制 '+ip,headline);button.onclick=()=>copyEgressIP(ip)}
        if(result.same_family_disagreement)controlNode('p','各查询服务看到了不同出口，请不要只选一个 IP 当作确定结果。',root).className='danger';
        if(result.status==='partial'||result.status==='error')controlNode('p','部分或全部查询失败，展开查询明细查看原因。',root).className='danger';
        const observations=controlNode('details','',root);observations.className='diagnostic-detail';controlNode('summary','查看查询来源、耗时和失败原因',observations);
        for(const observation of result.observations){const box=controlNode('div','',observations);box.className='observation';controlNode('strong',observation.service,box);controlNode('p',observation.status==='ok'?observation.family+' · '+observation.ip:observation.error,box);controlNode('small',observation.url+' · '+observation.elapsed_ms+' ms · 临时源端口：'+(observation.observed_source_port??'未提供'),box)}
        controlNode('p','临时源端口是查询连接的信息，不是服务器开放的端口，也通常不用于 IP 白名单。',observations).className='description';
    }else controlNode('p',route.error?'完善配置后即可查询。':'还没有查询。点击“查询公网 IP”后才会访问第三方查询服务。',root).className='empty-hint';
    const technical=controlNode('details','',root);technical.className='diagnostic-detail';controlNode('summary','查看连接技术参数',technical);
    controlNode('p','代理地址：'+(route.proxy==='DIRECT'?'无（DIRECT）':route.proxy||'未配置'),technical);
    controlNode('p','以下目标不经过代理（NO_PROXY）：'+(route.no_proxy||'未设置'),technical);
    controlNode('small','内部连接标识：'+route.id,technical);
}
refreshEnvironment();
