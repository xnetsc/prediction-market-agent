from __future__ import annotations

import json
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse

from ..core.config import ApplicationConfigStore, Config
from ..plugin_system.management import PluginManagementService
from ..plugin_system.contracts import platform_state_path
from ..plugin_system.registry import load_api_plugins
from .reporting import build_report
from .memory import SessionMemory
from .auth import AdminAuthStore, AdminSession, SESSION_COOKIE


HTML = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Prediction Agent</title>
<style>
body{font:14px system-ui;margin:0;background:#0b1020;color:#e8eefc}header{position:sticky;top:0;z-index:4;background:#151d33;padding:16px 24px}main{padding:20px;max-width:1500px;margin:auto}.cards,.plugin-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px}.card,section,.plugin{background:#151d33;border:1px solid #293453;border-radius:10px;padding:14px;margin-bottom:16px}.plugin{margin:0}.plugin.off{opacity:.62}button,select,input,textarea{background:#263453;color:#fff;border:1px solid #526180;border-radius:6px;padding:7px;box-sizing:border-box}button{cursor:pointer}.primary{background:#325cc7}.danger{color:#ff8a8a}.good{color:#8ee6ac}.muted{color:#9fb0d0}.field{display:grid;gap:5px;margin:12px 0}.field input,.field select,.field textarea{width:100%}.description{font-size:12px;color:#9fb0d0}.toolbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap}table{border-collapse:collapse;width:100%}th,td{text-align:left;vertical-align:top;border-bottom:1px solid #2b3654;padding:8px;max-width:520px}pre{white-space:pre-wrap;word-break:break-word;max-height:360px;overflow:auto}details{max-width:760px}h3{margin-top:0}.status{min-height:20px}
</style></head><body>
<header><b>Prediction Agent</b> <span id="stamp" class="muted"></span></header><main>
<div id="cards" class="cards"></div>
<section><h3>管理员安全</h3><p class="muted">单一 admin 账户可持有多个 Passkey；业务数据使用本次 Passkey 登录绑定的 ECDH 会话密钥加密。</p><div class="toolbar"><input id="newPasskeyName" placeholder="新 Passkey 名称"><button onclick="addPasskey()">添加 Passkey</button><button onclick="logout()">退出当前会话</button></div><h4>Passkey</h4><div id="passkeys"></div><h4>登录设备与会话</h4><div class="toolbar"><button onclick="kickSelectedSessions()">踢出选中会话</button></div><div id="sessions"></div></section>
<section><h3>程序运行配置</h3><p class="muted">显示全部通用运行配置；保存或恢复后重启机器人生效。</p><div id="applicationSettings" class="plugin-grid"></div><div class="toolbar"><button class="primary" onclick="saveApplicationSettings()">保存程序配置</button><button onclick="resetAllApplicationSettings()">删除全部覆盖并恢复默认</button><span id="settingsStatus" class="status muted"></span></div></section>
<section><h3>插件扫描目录</h3><p class="muted">每行一个目录；删除自定义配置后恢复安装包内置目录。</p><div id="pluginDirectories" class="plugin-grid"></div><div class="toolbar"><button class="primary" onclick="savePluginDirectories()">保存插件目录</button><button onclick="resetPluginDirectories()">删除自定义目录</button></div></section>
<section><h3>插件管理</h3><p class="muted">插件系统保存有序启用名单；禁用插件不会被导入或初始化，只按文件名展示。插件私有配置由初始化函数提供的读取、保存和删除回调管理。</p><div class="toolbar"><button onclick="refreshPlugins()">刷新插件</button><span>刷新后以磁盘和启用名单的最新状态为准。</span></div><div id="pluginManager"></div><div class="toolbar"><button class="primary" onclick="saveSelection()">保存启用状态与顺序</button><span id="manageStatus" class="status muted"></span></div></section>
<section><h3>决策账本筛选</h3><div class="toolbar"><input id="platform" placeholder="平台（留空为全部）"><input id="providerFilter" placeholder="Provider"><input id="statusFilter" placeholder="状态"><select id="actionFilter"><option value="">全部动作</option><option>BUY</option><option>SELL</option><option>HOLD</option><option>CANCEL</option></select><button onclick="refreshAudit()">刷新</button></div></section>
<section><h3>决策账本：前因 → 判断 → 风控 → 执行 → 后续观察</h3><p class="muted">每行对应一次完整决策，展开 JSON 可检查当时上下文、研究证据、原始模型输出和插件判定。</p><div id="decisions"></div></section>
<section><h3>执行动作 / 失败</h3><div id="actions"></div></section>
<section><h3>Agent 决策轮次</h3><div id="turns"></div></section>
<section><h3>Agent 工具步骤</h3><div id="steps"></div></section>
<section><h3>运行清单</h3><pre id="manifest"></pre></section></main>
<script>
const TOKEN='CSRF_TOKEN',SESSION_ID='SESSION_ID',KINDS=['api','decision_provider','decision_strategy','research_tool','risk','hook'];
const LABELS={api:'市场 API',decision_provider:'决策 Provider',decision_strategy:'决策策略',research_tool:'研究工具',risk:'风控',hook:'Hook'};
const esc=s=>String(s??'').replace(/[&<>\"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;'}[c]));
const detail=o=>'<details><summary>查看完整 JSON</summary><pre>'+esc(JSON.stringify(o,null,2))+'</pre></details>';
const b64u=b=>btoa(String.fromCharCode(...new Uint8Array(b))).replaceAll('+','-').replaceAll('/','_').replaceAll('=','');
const unb64u=s=>Uint8Array.from(atob(s.replaceAll('-','+').replaceAll('_','/')+'==='.slice((s.length+3)%4)),c=>c.charCodeAt(0));
function keyDb(){return new Promise((ok,no)=>{let r=indexedDB.open('prediction-agent-keys',1);r.onupgradeneeded=()=>r.result.createObjectStore('sessions');r.onsuccess=()=>ok(r.result);r.onerror=()=>no(r.error)})}
async function storedKey(){let d=await keyDb();return new Promise((ok,no)=>{let r=d.transaction('sessions').objectStore('sessions').get(SESSION_ID);r.onsuccess=()=>ok(r.result);r.onerror=()=>no(r.error)})}
async function dropKey(id=SESSION_ID){let d=await keyDb();return new Promise((ok,no)=>{let r=d.transaction('sessions','readwrite').objectStore('sessions').delete(id);r.onsuccess=()=>ok();r.onerror=()=>no(r.error)})}
let SESSION_KEY;
async function secure(u,v=null){if(!SESSION_KEY)SESSION_KEY=await storedKey();if(!SESSION_KEY){location='/api/auth/clear';throw Error('本机缺少此登录会话的加密密钥，请重新登录')}let nonce=crypto.getRandomValues(new Uint8Array(12)),plain=new TextEncoder().encode(JSON.stringify({url:u,body:v})),cipher=await crypto.subtle.encrypt({name:'AES-GCM',iv:nonce,additionalData:new TextEncoder().encode('POST /api/secure')},SESSION_KEY,plain),r=await fetch('/api/secure',{method:'POST',headers:{'Content-Type':'application/json','X-Admin-CSRF':TOKEN},body:JSON.stringify({nonce:b64u(nonce),ciphertext:b64u(cipher)})}),envelope=await r.json(),decoded;try{decoded=JSON.parse(new TextDecoder().decode(await crypto.subtle.decrypt({name:'AES-GCM',iv:unb64u(envelope.nonce),additionalData:new TextEncoder().encode('RESPONSE /api/secure')},SESSION_KEY,unb64u(envelope.ciphertext))))}catch(e){if(r.status===401){await dropKey();location='/api/auth/clear'}throw Error('加密响应认证失败')}if(!r.ok)throw Error(decoded.error||r.statusText);return decoded}
async function get(u){return secure(u)}
async function post(u,v){return secure(u,v)}
function table(rows,cols){return '<table><thead><tr>'+cols.map(c=>'<th>'+c[0]+'</th>').join('')+'</tr></thead><tbody>'+rows.map(r=>'<tr>'+cols.map(c=>'<td>'+c[1](r)+'</td>').join('')+'</tr>').join('')+'</tbody></table>'}
function fieldHtml(kind,name,f){let id='cfg_'+kind+'_'+name+'_'+f.name,attrs=' id="'+esc(id)+'" data-field="'+esc(f.name)+'" data-type="'+esc(f.type)+'"';let input;if(f.type==='boolean')input='<input type="checkbox"'+attrs+(f.value?' checked':'')+'>';else if(f.type==='enum')input='<select'+attrs+'>'+f.options.map(o=>'<option'+(o===f.value?' selected':'')+'>'+esc(o)+'</option>').join('')+'</select>';else input='<input type="'+(f.sensitive?'password':(f.type==='integer'||f.type==='number'?'number':'text'))+'"'+attrs+' value="'+esc(f.value??'')+'" '+(f.type==='number'?'step="any"':'')+' placeholder="'+(f.sensitive&&f.configured?'已保存；留空保持不变':'')+'">';return '<div class="field"><label><b>'+esc(f.label)+(f.required?' *':'')+'</b>'+input+'<span class="description">'+esc(f.description)+(f.has_default?' 默认值：'+esc(JSON.stringify(f.default)):'')+'</span></label><button onclick="resetPluginField(\''+kind+'\',\''+name+'\',\''+f.name+'\')">删除此字段值</button></div>'}
function renderManager(m){let out='';for(let kind of KINDS){out+='<h3>'+LABELS[kind]+'</h3><div class="plugin-grid">';for(let p of m.plugins[kind]){let key=kind+'_'+p.name;let selector=kind==='decision_strategy'?'<label><input type="radio" name="strategy" value="'+esc(p.name)+'" '+(p.enabled?'checked':'')+'> 使用此策略</label>':'<label><input class="enable" type="checkbox" data-kind="'+kind+'" data-name="'+esc(p.name)+'" '+(p.enabled?'checked':'')+'> 启用</label> <label>优先级 <input class="priority" type="number" min="1" data-kind="'+kind+'" data-name="'+esc(p.name)+'" value="'+esc(p.priority??99)+'" style="width:70px"></label>';let fields=p.configuration?p.configuration.fields.map(f=>fieldHtml(kind,p.name,f)).join(''):'<p class="muted">此插件没有私有配置。</p>';let save=p.configuration?'<div class="toolbar"><button onclick="savePluginConfig(\''+kind+'\',\''+p.name+'\')">保存插件配置</button><button onclick="deletePluginConfig(\''+kind+'\',\''+p.name+'\')">删除配置并恢复默认</button></div>':'';out+='<div class="plugin '+(p.enabled?'':'off')+'" id="plugin_'+key+'"><div class="toolbar"><b>'+esc(p.name)+'</b>'+selector+'</div><p>'+esc(p.description)+'</p><div class="description">来源：'+esc(p.origin)+'</div>'+fields+save+'</div>'}out+='</div>'}document.getElementById('pluginManager').innerHTML=out}
async function refreshManager(){let m=await get('/api/plugins/manage');renderManager(m)}
async function refreshPlugins(){let s=document.getElementById('manageStatus');try{let m=await post('/api/plugins/refresh',{});renderManager(m);s.className='status good';s.textContent='插件已按最新文件和启用名单刷新'}catch(e){s.className='status danger';s.textContent=e.message}}
async function savePluginConfig(kind,name){let root=document.getElementById('plugin_'+kind+'_'+name),values={};for(let e of root.querySelectorAll('[data-field]')){let v=e.type==='checkbox'?e.checked:e.value;if(e.dataset.type==='integer')v=Number.parseInt(v,10);if(e.dataset.type==='number')v=Number(v);values[e.dataset.field]=v}let s=document.getElementById('manageStatus');try{await post('/api/plugins/config',{kind,name,values});s.className='status good';s.textContent=kind+':'+name+' 配置已保存，重启后生效';await refreshManager()}catch(e){s.className='status danger';s.textContent=e.message}}
async function deletePluginConfig(kind,name){if(!confirm('删除 '+kind+':'+name+' 的私有配置并恢复默认值？'))return;let s=document.getElementById('manageStatus');try{await post('/api/plugins/config/delete',{kind,name});s.className='status good';s.textContent=kind+':'+name+' 配置已删除';await refreshManager()}catch(e){s.className='status danger';s.textContent=e.message}}
async function resetPluginField(kind,name,field){let s=document.getElementById('manageStatus');try{await post('/api/plugins/config/reset',{kind,name,fields:[field]});s.className='status good';s.textContent=kind+':'+name+' 的 '+field+' 已删除';await refreshManager()}catch(e){s.className='status danger';s.textContent=e.message}}
async function saveSelection(){let enabled={};for(let kind of KINDS)enabled[kind]=[];for(let kind of KINDS.filter(x=>x!=='decision_strategy')){let rows=[...document.querySelectorAll('.enable[data-kind="'+kind+'"]:checked')].map(e=>({name:e.dataset.name,priority:Number(document.querySelector('.priority[data-kind="'+kind+'"][data-name="'+e.dataset.name+'"]').value)||99})).sort((a,b)=>a.priority-b.priority);enabled[kind]=rows.map(x=>x.name)}let strategy=document.querySelector('input[name="strategy"]:checked')?.value||'';enabled.decision_strategy=strategy?[strategy]:[];let s=document.getElementById('manageStatus');try{await post('/api/plugins/selection',{enabled,decision_strategy:strategy});s.className='status good';s.textContent='启用状态和优先级已保存，重启后生效';await refreshManager()}catch(e){s.className='status danger';s.textContent=e.message}}
function appFieldHtml(f){let attrs=' data-app-field="'+esc(f.name)+'" data-type="'+esc(f.type)+'"';let input=f.type==='enum'?'<select'+attrs+'>'+f.options.map(o=>'<option'+(o===f.value?' selected':'')+'>'+esc(o)+'</option>').join('')+'</select>':'<input type="'+(f.type==='integer'?'number':'text')+'"'+attrs+' value="'+esc(f.value)+'"'+(f.minimum!==null?' min="'+f.minimum+'"':'')+(f.maximum!==null?' max="'+f.maximum+'"':'')+'>';return '<div class="plugin"><label class="field"><b>'+esc(f.label)+'</b>'+input+'<span class="description">'+esc(f.description)+' 默认值：'+esc(JSON.stringify(f.default))+'；'+(f.configured?'当前为用户覆盖值':'当前使用默认值')+'</span></label><button onclick="resetApplicationSetting(\''+f.name+'\')">删除此覆盖值</button></div>'}
async function refreshConfiguration(){let [settings,manager]=await Promise.all([get('/api/settings'),get('/api/plugins/manage')]);document.getElementById('applicationSettings').innerHTML=settings.fields.map(appFieldHtml).join('');let d=manager.plugin_directories;document.getElementById('pluginDirectories').innerHTML=KINDS.map(k=>'<label class="field plugin"><b>'+LABELS[k]+'</b><textarea rows="3" data-dir-kind="'+k+'">'+esc((d.categories[k]||[]).join('\n'))+'</textarea></label>').join('')}
async function saveApplicationSettings(){let values={};for(let e of document.querySelectorAll('[data-app-field]')){values[e.dataset.appField]=e.dataset.type==='integer'?Number.parseInt(e.value,10):e.value}let s=document.getElementById('settingsStatus');try{await post('/api/settings',{values});s.className='status good';s.textContent='程序配置已保存，重启后生效';await refreshConfiguration()}catch(e){s.className='status danger';s.textContent=e.message}}
async function resetApplicationSetting(name){await post('/api/settings/reset',{names:[name]});await refreshConfiguration()}
async function resetAllApplicationSettings(){if(!confirm('删除全部程序配置覆盖并恢复默认值？'))return;await post('/api/settings/reset',{});await refreshConfiguration()}
async function savePluginDirectories(){let categories={};for(let e of document.querySelectorAll('[data-dir-kind]'))categories[e.dataset.dirKind]=e.value.split('\n').map(x=>x.trim()).filter(Boolean);await post('/api/plugins/directories',{categories});await refreshConfiguration()}
async function resetPluginDirectories(){if(!confirm('删除自定义插件目录并恢复安装包内置目录？'))return;await post('/api/plugins/directories/reset',{});await refreshConfiguration()}
function credentialJson(c){let r={id:c.id,rawId:b64u(c.rawId),type:c.type,response:{clientDataJSON:b64u(c.response.clientDataJSON)}};if(c.response.attestationObject)r.response.attestationObject=b64u(c.response.attestationObject);if(c.response.authenticatorData)r.response.authenticatorData=b64u(c.response.authenticatorData);if(c.response.signature)r.response.signature=b64u(c.response.signature);if(c.response.userHandle)r.response.userHandle=b64u(c.response.userHandle);if(c.authenticatorAttachment)r.authenticatorAttachment=c.authenticatorAttachment;return r}
function publicKeyBuffers(p){p.challenge=unb64u(p.challenge);if(p.user?.id)p.user.id=unb64u(p.user.id);if(p.excludeCredentials)for(let c of p.excludeCredentials)c.id=unb64u(c.id);if(p.allowCredentials)for(let c of p.allowCredentials)c.id=unb64u(c.id);return p}
async function refreshSecurity(){let x=await get('/api/auth/manage'),fmt=t=>t?new Date(t*1000).toLocaleString():'—';document.getElementById('passkeys').innerHTML=table(x.passkeys,[['名称',r=>'<input value="'+esc(r.name)+'" id="pk_'+r.id+'">'],['注册/最近使用',r=>fmt(r.created_at)+'<br>'+fmt(r.last_used_at)],['操作',r=>'<button onclick="renamePasskey(\''+r.id+'\')">重命名</button> <button onclick="deletePasskey(\''+r.id+'\')">删除</button>']]);document.getElementById('sessions').innerHTML=table(x.sessions,[['选择',r=>'<input type="checkbox" class="sessionPick" value="'+esc(r.id)+'">'],['Passkey/当前',r=>esc(r.passkey_name)+(r.current?' <span class="good">当前</span>':'')],['设备',r=>esc(r.user_agent)+'<br>'+esc(r.source_address)],['登录/最近请求/闲置到期/绝对到期',r=>fmt(r.login_at)+'<br>'+fmt(r.last_request_at)+'<br>'+fmt(r.idle_expires_at)+'<br>'+fmt(r.expires_at)],['操作',r=>'<button onclick="kickSessions([\''+r.id+'\'])">踢出</button>']])}
async function addPasskey(){let name=document.getElementById('newPasskeyName').value||'Passkey',o=await get('/api/auth/passkeys/add/options'),cred=await navigator.credentials.create({publicKey:publicKeyBuffers(o.publicKey)});await post('/api/auth/passkeys/add/verify',{ceremony_id:o.ceremony_id,name,credential:credentialJson(cred)});await refreshSecurity()}
async function renamePasskey(id){await post('/api/auth/passkeys/rename',{id,name:document.getElementById('pk_'+id).value});await refreshSecurity()}
async function deletePasskey(id){if(!confirm('删除这个 Passkey？关联登录会话也会被踢出。'))return;await post('/api/auth/passkeys/delete',{id});await refreshSecurity()}
async function kickSessions(ids){let current=ids.includes(SESSION_ID);await post('/api/auth/sessions/kick',{ids});if(current){await dropKey();location='/api/auth/clear'}else await refreshSecurity()}
async function kickSelectedSessions(){await kickSessions([...document.querySelectorAll('.sessionPick:checked')].map(e=>e.value))}
async function logout(){try{await post('/api/auth/logout',{})}finally{await dropKey();location='/'}}
async function refreshAudit(){try{let p=document.getElementById('platform').value,q=p?'&platform='+encodeURIComponent(p):'',dq=q+'&provider='+encodeURIComponent(document.getElementById('providerFilter').value)+'&status='+encodeURIComponent(document.getElementById('statusFilter').value)+'&action='+encodeURIComponent(document.getElementById('actionFilter').value);let [s,d,a,t,g,m]=await Promise.all([get('/api/summary'),get('/api/decisions?limit=100'+dq),get('/api/records?kind=actions&limit=100'+q),get('/api/records?kind=turns&limit=100'+q),get('/api/records?kind=steps&limit=100'+q),get('/api/manifest')]);let ag=s.aggregate_account||{},metrics=ag.risk_metrics||{},cards=[['权益',ag.equity],...Object.entries(metrics).map(([k,v])=>['风控指标：'+k,v]),['决策',s.summary?.decisions],['Provider 错误',s.summary?.provider_errors],['Agent 步骤',s.summary?.agent_steps]];document.getElementById('cards').innerHTML=cards.map(x=>'<div class=card><div class=muted>'+esc(x[0])+'</div><h2>'+esc(x[1]??0)+'</h2></div>').join('');document.getElementById('decisions').innerHTML=table(d.items,[['时间/ID',r=>new Date(r.created_at).toLocaleString()+'<br>#'+esc(r.id)],['市场',r=>esc(r.platform+' / '+(r.context?.market?.title||r.market_topic_id))],['Provider/策略',r=>esc((r.provider||'—')+' / '+r.strategy_name)],['证据',r=>esc(r.agent_steps+' steps / '+(r.research?.length||0)+' results')+detail({research:r.research,model_raw_output:r.model_raw_output})],['决策链',r=>detail({proposed:r.proposed_decision,risk:r.risk_decision,final:r.final_decision})],['执行/状态',r=>esc(r.status)+detail({execution:r.execution,error:r.error})],['后续观察',r=>detail(r.subsequent_observation)]]);document.getElementById('actions').innerHTML=table(a.items,[['时间',r=>new Date(r.created_at).toLocaleString()],['平台',r=>esc(r.platform)],['动作',r=>esc(r.action)],['结果',r=>detail(r)]]);document.getElementById('turns').innerHTML=table(t.items,[['时间',r=>new Date(r.created_at).toLocaleString()],['平台/Provider',r=>esc(r.platform+' / '+r.provider)],['状态',r=>esc(r.status)],['内容',r=>detail(r)]]);document.getElementById('steps').innerHTML=table(g.items,[['时间',r=>new Date(r.created_at).toLocaleString()],['平台/Provider',r=>esc(r.platform+' / '+r.provider)],['工具/状态',r=>esc((r.tool_name||'control')+' / '+r.status)],['内容',r=>detail(r)]]);document.getElementById('manifest').textContent=JSON.stringify(m,null,2);document.getElementById('stamp').textContent='更新 '+new Date().toLocaleTimeString()}catch(e){document.getElementById('stamp').textContent='错误: '+e}}
document.getElementById('platform').onchange=refreshAudit;Promise.all([refreshSecurity(),refreshManager(),refreshConfiguration(),refreshAudit()]);setInterval(refreshAudit,REFRESH_MS);
</script></body></html>"""


AUTH_HTML = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Admin Passkey</title><style>body{font:16px system-ui;background:#0b1020;color:#e8eefc;display:grid;place-items:center;min-height:100vh}.box{width:min(460px,90vw);background:#151d33;border:1px solid #293453;border-radius:12px;padding:28px}button,input{width:100%;box-sizing:border-box;margin-top:12px;padding:10px;border-radius:7px;border:1px solid #526180;background:#263453;color:#fff}button{background:#325cc7;cursor:pointer}.muted{color:#9fb0d0}.danger{color:#ff8a8a}</style></head><body><div class="box"><h2>ADMIN_TITLE</h2><p class="muted">Passkey 验证同时绑定一次 P-256 ECDH 密钥交换。登录成功后，业务请求和响应均使用该会话密钥加密。</p><input id="name" placeholder="Passkey 名称" ADMIN_NAME><button onclick="begin()">ADMIN_ACTION</button><p id="status" class="danger"></p></div><script>
const b64u=b=>btoa(String.fromCharCode(...new Uint8Array(b))).replaceAll('+','-').replaceAll('/','_').replaceAll('=','');const unb64u=s=>Uint8Array.from(atob(s.replaceAll('-','+').replaceAll('_','/')+'==='.slice((s.length+3)%4)),c=>c.charCodeAt(0));function publicKeyBuffers(p){p.challenge=unb64u(p.challenge);if(p.user?.id)p.user.id=unb64u(p.user.id);if(p.excludeCredentials)for(let c of p.excludeCredentials)c.id=unb64u(c.id);if(p.allowCredentials)for(let c of p.allowCredentials)c.id=unb64u(c.id);return p}function credentialJson(c){let r={id:c.id,rawId:b64u(c.rawId),type:c.type,response:{clientDataJSON:b64u(c.response.clientDataJSON)}};if(c.response.attestationObject)r.response.attestationObject=b64u(c.response.attestationObject);if(c.response.authenticatorData)r.response.authenticatorData=b64u(c.response.authenticatorData);if(c.response.signature)r.response.signature=b64u(c.response.signature);if(c.response.userHandle)r.response.userHandle=b64u(c.response.userHandle);if(c.authenticatorAttachment)r.authenticatorAttachment=c.authenticatorAttachment;return r}function keyDb(){return new Promise((ok,no)=>{let r=indexedDB.open('prediction-agent-keys',1);r.onupgradeneeded=()=>r.result.createObjectStore('sessions');r.onsuccess=()=>ok(r.result);r.onerror=()=>no(r.error)})}async function saveKey(id,key){let d=await keyDb();return new Promise((ok,no)=>{let r=d.transaction('sessions','readwrite').objectStore('sessions').put(key,id);r.onsuccess=()=>ok();r.onerror=()=>no(r.error)})}async function begin(){let status=document.getElementById('status');try{status.textContent='等待 Passkey…';let pair=await crypto.subtle.generateKey({name:'ECDH',namedCurve:'P-256'},false,['deriveBits']),publicJwk=await crypto.subtle.exportKey('jwk',pair.publicKey),options=await fetch('OPTIONS_URL',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({client_public_key:publicJwk})}).then(async r=>{let j=await r.json();if(!r.ok)throw Error(j.error);return j}),challenge=unb64u(options.publicKey.challenge),serverKey=await crypto.subtle.importKey('jwk',options.server_public_key,{name:'ECDH',namedCurve:'P-256'},false,[]),shared=await crypto.subtle.deriveBits({name:'ECDH',public:serverKey},pair.privateKey,256),material=await crypto.subtle.importKey('raw',shared,'HKDF',false,['deriveKey']),aes=await crypto.subtle.deriveKey({name:'HKDF',hash:'SHA-256',salt:challenge,info:new TextEncoder().encode('prediction-market-agent-session-v1')},material,{name:'AES-GCM',length:256},false,['encrypt','decrypt']),cred=await navigator.credentials.CREDENTIAL_METHOD({publicKey:publicKeyBuffers(options.publicKey)}),result=await fetch('VERIFY_URL',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ceremony_id:options.ceremony_id,name:document.getElementById('name').value,credential:credentialJson(cred)})}).then(async r=>{let j=await r.json();if(!r.ok)throw Error(j.error);return j});await saveKey(result.session_id,aes);location='/'}catch(e){status.textContent=e.message||String(e)}}
</script></body></html>"""


def _json_value(value: str | None) -> Any:
    if value is None:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _midpoint(book: dict[str, Any]) -> float | None:
    try:
        return (float(book["best_bid"]) + float(book["best_ask"])) / 2
    except (KeyError, TypeError, ValueError):
        return None


class AuditData:
    def __init__(self, config: Config, management: PluginManagementService | None = None):
        self.config = config
        self.management = management or PluginManagementService(config)
        memory = SessionMemory(config.session_db)
        memory.close()

    def state_files(self) -> dict[str, Path]:
        multiple = len(self.config.market_api_plugins) > 1
        return {
            name: platform_state_path(self.config.state_file, name, multiple)
            for name in self.config.market_api_plugins
        }

    def summary(self) -> dict[str, Any]:
        if not self.config.session_db.exists():
            return {
                "summary": {"decisions": 0, "provider_errors": 0, "agent_steps": 0},
                "accounts": {},
                "aggregate_account": {"equity": 0, "risk_metrics": {}},
            }
        return build_report(self.config.session_db, self.state_files())

    def manifest(self) -> dict[str, Any]:
        registry, plugins = load_api_plugins(self.config, self.management.catalog)
        strategy_name = self.config.decision_strategy_name
        strategy = self.management.catalog.get("decision_strategy", strategy_name).factory(
            self.config
        )
        return {
            "configured_plugins": list(self.config.market_api_plugins),
            "registered_plugins": list(registry.registered_names),
            "plugins": {
                plugin.name: {
                    "capabilities": plugin.capabilities.to_dict(),
                    "configuration": plugin.configuration_manifest(),
                }
                for plugin in plugins
            },
            "plugin_management": self.management.manifest(),
            "strategy": {"name": strategy_name, "path": str(strategy.path), "sha256": strategy.sha256},
        }

    def records(self, kind: str, limit: int, offset: int, platform: str) -> dict[str, Any]:
        definitions = {
            "turns": (
                "provider_turns",
                ["id", "created_at", "platform", "provider", "market_topic_id", "token_id", "input_json", "raw_output", "decision_json", "status", "error", "decision_id"],
                {"input_json", "decision_json"},
            ),
            "actions": (
                "execution_actions",
                ["id", "created_at", "platform", "market_topic_id", "token_id", "action", "request_json", "result_json", "decision_id"],
                {"request_json", "result_json"},
            ),
            "steps": (
                "agent_steps",
                ["id", "created_at", "platform", "provider", "market_topic_id", "token_id", "step_index", "input_json", "raw_output", "control_json", "tool_name", "arguments_json", "result_json", "status", "error", "decision_id"],
                {"input_json", "control_json", "arguments_json", "result_json"},
            ),
        }
        if kind not in definitions:
            raise ValueError("kind must be turns, actions, or steps")
        if not self.config.session_db.exists():
            return {"kind": kind, "limit": limit, "offset": offset, "items": []}
        table_name, columns, json_columns = definitions[kind]
        where = " WHERE platform = ?" if platform else ""
        params: list[Any] = [platform] if platform else []
        params.extend([limit, offset])
        connection = sqlite3.connect(self.config.session_db)
        rows = connection.execute(
            f"SELECT {','.join(columns)} FROM {table_name}{where} ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            params,
        ).fetchall()
        connection.close()
        items = []
        for row in rows:
            item = dict(zip(columns, row))
            for column in json_columns:
                item[column.removesuffix("_json")] = _json_value(item.pop(column))
            items.append(item)
        return {"kind": kind, "limit": limit, "offset": offset, "items": items}

    def decisions(
        self,
        limit: int,
        offset: int,
        platform: str,
        provider: str,
        status: str,
        action: str,
    ) -> dict[str, Any]:
        columns = [
            "id", "created_at", "updated_at", "platform", "provider",
            "strategy_name", "strategy_sha256", "market_topic_id", "market_id",
            "token_id", "context_json", "research_json", "model_raw_output",
            "proposed_decision_json", "risk_decision_json", "final_decision_json",
            "execution_json", "status", "error",
        ]
        filters: list[str] = []
        params: list[Any] = []
        for column, value in (("platform", platform), ("provider", provider), ("status", status)):
            if value:
                filters.append(f"{column} = ?")
                params.append(value)
        if action:
            filters.append("json_extract(final_decision_json, '$.action') = ?")
            params.append(action.upper())
        where = " WHERE " + " AND ".join(filters) if filters else ""
        params.extend([limit, offset])
        connection = sqlite3.connect(self.config.session_db)
        rows = connection.execute(
            f"SELECT {','.join(columns)} FROM decision_ledger{where} "
            "ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            params,
        ).fetchall()
        items: list[dict[str, Any]] = []
        json_columns = {
            "context_json", "research_json", "proposed_decision_json",
            "risk_decision_json", "final_decision_json", "execution_json",
        }
        for row in rows:
            item = dict(zip(columns, row))
            for column in json_columns:
                item[column.removesuffix("_json")] = _json_value(item.pop(column))
            later = connection.execute(
                """
                SELECT created_at, context_json FROM decision_ledger
                WHERE platform = ? AND token_id = ? AND created_at > ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (item["platform"], item["token_id"], item["created_at"]),
            ).fetchone()
            context = item.get("context") or {}
            initial_book = context.get("order_book", {}) if isinstance(context, dict) else {}
            initial_mid = _midpoint(initial_book)
            observation: dict[str, Any] = {"initial_mid": initial_mid}
            if later:
                later_context = _json_value(later[1]) or {}
                later_book = later_context.get("order_book", {}) if isinstance(later_context, dict) else {}
                later_mid = _midpoint(later_book)
                observation.update(
                    {
                        "observed_at": later[0],
                        "latest_mid": later_mid,
                        "mid_change": (
                            None if initial_mid is None or later_mid is None else later_mid - initial_mid
                        ),
                    }
                )
            else:
                observation["note"] = "No later observation has been recorded yet"
            item["subsequent_observation"] = observation
            item["agent_steps"] = int(
                connection.execute(
                    "SELECT COUNT(*) FROM agent_steps WHERE decision_id = ?", (item["id"],)
                ).fetchone()[0]
            )
            items.append(item)
        connection.close()
        return {"limit": limit, "offset": offset, "items": items}


def create_app(config: Config) -> FastAPI:
    """Create the importable HTTP application used by local and cloud entry points."""
    management = PluginManagementService(config)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        try:
            yield
        finally:
            management.shutdown()

    app = FastAPI(
        title="Prediction Market Agent", docs_url=None, redoc_url=None, lifespan=lifespan
    )
    application_settings = ApplicationConfigStore(config.application_config_file)
    data = AuditData(config, management)
    auth = AdminAuthStore(
        config.auth_db,
        config.admin_session_hours,
        config.admin_absolute_session_hours,
    )

    def origin(request: Request) -> str:
        # Derive the relying-party origin from the request target.  The Origin
        # header is client-controlled and must not be allowed to select the RP
        # identity during first-admin registration.
        return str(request.base_url).rstrip("/")

    def source_address(request: Request) -> str:
        return request.client.host if request.client else "unknown"

    def current_session(request: Request, *, touch: bool = True) -> AdminSession:
        session = auth.session(request.cookies.get(SESSION_COOKIE, ""), touch=touch)
        if not session:
            raise HTTPException(status_code=401, detail="Passkey login required")
        return session

    def set_session_cookie(response: Response, request: Request, session: AdminSession) -> None:
        response.set_cookie(
            SESSION_COOKIE,
            session.token,
            max_age=max(0, session.expires_at - int(time.time())),
            httponly=True,
            secure=request.url.scheme == "https",
            samesite="strict",
            path="/",
        )

    @app.exception_handler(ValueError)
    async def value_error_handler(request: Request, error: ValueError) -> JSONResponse:
        del request
        return JSONResponse({"error": str(error)}, status_code=400)

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request) -> str:
        session = auth.session(request.cookies.get(SESSION_COOKIE, ""), touch=False)
        if not session:
            initialized = auth.has_admin()
            return (
                AUTH_HTML.replace("ADMIN_TITLE", "管理员登录" if initialized else "初始化管理员")
                .replace("ADMIN_ACTION", "使用 Passkey 登录" if initialized else "注册首个 Passkey")
                .replace("ADMIN_NAME", "hidden" if initialized else "")
                .replace("OPTIONS_URL", "/api/auth/login/options" if initialized else "/api/auth/register/options")
                .replace("VERIFY_URL", "/api/auth/login/verify" if initialized else "/api/auth/register/verify")
                .replace("CREDENTIAL_METHOD", "get" if initialized else "create")
            )
        return (
            HTML.replace("REFRESH_MS", str(config.dashboard_refresh_seconds * 1000))
            .replace("CSRF_TOKEN", session.csrf_token)
            .replace("SESSION_ID", session.session_id)
        )

    @app.get("/healthz")
    def health() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/api/auth/status")
    def auth_status(request: Request) -> dict[str, Any]:
        session = auth.session(request.cookies.get(SESSION_COOKIE, ""), touch=False)
        return {"initialized": auth.has_admin(), "authenticated": bool(session),
                "session_id": session.session_id if session else None,
                "expires_at": session.expires_at if session else None}

    @app.post("/api/auth/register/options")
    async def register_options(request: Request) -> dict[str, Any]:
        payload = await request.json()
        return auth.registration_options(origin(request), adding=False,
                                         client_public_jwk=payload.get("client_public_key"))

    @app.post("/api/auth/register/verify")
    async def register_verify(request: Request) -> JSONResponse:
        payload = await request.json()
        session = auth.verify_registration(
            str(payload.get("ceremony_id", "")), payload.get("credential", {}),
            str(payload.get("name", "")), adding=False,
            user_agent=request.headers.get("user-agent", ""),
            source_address=source_address(request),
        )
        if session is None:
            raise ValueError("Initial registration did not create a session")
        response = JSONResponse({"session_id": session.session_id,
                                 "csrf_token": session.csrf_token,
                                 "expires_at": session.expires_at})
        set_session_cookie(response, request, session)
        return response

    @app.post("/api/auth/login/options")
    async def login_options(request: Request) -> dict[str, Any]:
        payload = await request.json()
        return auth.authentication_options(origin(request), payload.get("client_public_key", {}))

    @app.post("/api/auth/login/verify")
    async def login_verify(request: Request) -> JSONResponse:
        payload = await request.json()
        session = auth.verify_authentication(
            str(payload.get("ceremony_id", "")), payload.get("credential", {}),
            user_agent=request.headers.get("user-agent", ""),
            source_address=source_address(request),
        )
        response = JSONResponse({"session_id": session.session_id,
                                 "csrf_token": session.csrf_token,
                                 "expires_at": session.expires_at})
        set_session_cookie(response, request, session)
        return response

    @app.get("/api/auth/clear")
    def clear_broken_session(request: Request) -> Response:
        token = request.cookies.get(SESSION_COOKIE, "")
        if token:
            auth.logout(token)
        response = Response(status_code=303, headers={"Location": "/"})
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    def query_value(query: dict[str, list[str]], name: str, default: str = "") -> str:
        return query.get(name, [default])[0]

    def dispatch(request: Request, session: AdminSession, route: str, body: Any) -> Any:
        parts = urlsplit(route)
        path = parts.path
        query = parse_qs(parts.query)
        payload = body if isinstance(body, dict) else {}
        if path in {"/api/summary", "/api/report"}:
            return data.summary()
        if path == "/api/manifest":
            return data.manifest()
        if path == "/api/plugins/manage":
            return management.manifest()
        if path == "/api/settings":
            return application_settings.save(payload.get("values", {})) if body is not None else application_settings.manifest()
        if path == "/api/settings/reset":
            return application_settings.reset(payload.get("names") if "names" in payload else None)
        if path == "/api/decisions":
            return data.decisions(
                max(1, min(200, int(query_value(query, "limit", "50")))),
                max(0, int(query_value(query, "offset", "0"))),
                query_value(query, "platform"), query_value(query, "provider"),
                query_value(query, "status"), query_value(query, "action"),
            )
        if path == "/api/records":
            return data.records(
                query_value(query, "kind", "turns"),
                max(1, min(200, int(query_value(query, "limit", "50")))),
                max(0, int(query_value(query, "offset", "0"))),
                query_value(query, "platform"),
            )
        if path == "/api/plugins/config":
            return management.save_plugin_configuration(str(payload.get("kind", "")), str(payload.get("name", "")), payload.get("values", {}))
        if path == "/api/plugins/config/delete":
            return management.delete_plugin_configuration(str(payload.get("kind", "")), str(payload.get("name", "")))
        if path == "/api/plugins/config/reset":
            return management.reset_plugin_configuration_fields(str(payload.get("kind", "")), str(payload.get("name", "")), payload.get("fields", []))
        if path == "/api/plugins/directories":
            return management.save_plugin_directories(payload.get("categories", {}))
        if path == "/api/plugins/directories/reset":
            return management.reset_plugin_directories()
        if path == "/api/plugins/selection":
            return management.save_enabled(payload)
        if path == "/api/plugins/refresh":
            return management.refresh()
        if path == "/api/auth/manage":
            return {"passkeys": auth.credentials(), "sessions": auth.sessions(session.session_id)}
        if path == "/api/auth/passkeys/add/options":
            return auth.registration_options(origin(request), adding=True)
        if path == "/api/auth/passkeys/add/verify":
            auth.verify_registration(str(payload.get("ceremony_id", "")), payload.get("credential", {}), str(payload.get("name", "")), adding=True)
            return {"ok": True}
        if path == "/api/auth/passkeys/rename":
            auth.rename_credential(str(payload.get("id", "")), str(payload.get("name", "")))
            return {"ok": True}
        if path == "/api/auth/passkeys/delete":
            auth.delete_credential(str(payload.get("id", "")))
            return {"ok": True}
        if path == "/api/auth/sessions/kick":
            return {"kicked": auth.kick_sessions(payload.get("ids", []))}
        if path == "/api/auth/logout":
            auth.logout(session.token)
            return {"ok": True}
        raise ValueError(f"Unknown protected operation: {path}")

    @app.post("/api/secure")
    async def secure_api(request: Request) -> JSONResponse:
        session = current_session(request, touch=False)
        if request.headers.get("X-Admin-CSRF", "") != session.csrf_token:
            raise HTTPException(status_code=403, detail="Invalid session request token")
        try:
            message = auth.decrypt(session, await request.json(), aad="POST /api/secure")
            auth.session(session.token, touch=True)
            if not isinstance(message, dict) or not isinstance(message.get("url"), str):
                raise ValueError("Protected operation must include a URL")
            result = dispatch(request, session, message["url"], message.get("body"))
            envelope = auth.encrypt(session, result, aad="RESPONSE /api/secure")
            return JSONResponse(envelope)
        except ValueError as error:
            envelope = auth.encrypt(session, {"error": str(error)}, aad="RESPONSE /api/secure")
            return JSONResponse(envelope, status_code=400)

    return app


def serve(config: Config, *, host: str | None = None, port: int | None = None) -> None:
    listen_host = host or config.dashboard_host
    listen_port = port or config.dashboard_port
    print(f"Robot management application: http://{listen_host}:{listen_port}")
    uvicorn.run(create_app(config), host=listen_host, port=listen_port)
