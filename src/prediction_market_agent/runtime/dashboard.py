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
from starlette.concurrency import run_in_threadpool

from ..core.config import ApplicationConfigStore, Config
from ..plugin_system.management import PluginManagementService
from ..plugin_system.contracts import platform_state_path
from .reporting import build_report
from .memory import SessionMemory
from .auth import AdminAuthStore, AdminSession, SESSION_COOKIE
from .controller import RobotRuntimeManager
from .local_access import is_local_request, require_local_request
from .environment import EnvironmentDiagnostics
from .setup_guide import setup_guide


HTML = r"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Prediction Agent</title>
<link rel="stylesheet" href="/assets/dashboard.css"></head><body>
<a href="#mainContent" class="skip-link">跳到主要内容</a>
<aside class="sidebar" id="sidebar"><a class="brand" href="#overview"><span class="brand-mark">↗</span><span>Prediction Agent<small>OPERATIONS CONSOLE</small></span></a><p class="nav-label">工作空间</p><nav aria-label="主导航">
<a class="nav-link" href="#overview" aria-current="page"><span class="nav-icon" aria-hidden="true">◫</span>运行概览</a>
<a class="nav-link" href="#decisions"><span class="nav-icon" aria-hidden="true">≡</span>决策账本</a>
<a class="nav-link" href="#models"><span class="nav-icon" aria-hidden="true">◎</span>模型服务</a>
<a class="nav-link" href="#plugins"><span class="nav-icon" aria-hidden="true">◇</span>插件中心</a>
<a class="nav-link" href="#settings"><span class="nav-icon" aria-hidden="true">⚙</span>程序设置</a>
<a class="nav-link" href="#security"><span class="nav-icon" aria-hidden="true">⌑</span>安全与会话</a>
</nav><div class="sidebar-footer"><b>单管理员工作空间</b><span id="accessMode"></span></div></aside>
<button class="nav-backdrop" id="navBackdrop" aria-label="关闭导航" tabindex="-1"></button>
<header class="topbar"><button id="menuToggle" class="menu-toggle" aria-label="打开导航" aria-controls="sidebar" aria-expanded="false">☰</button><span class="topbar-label">工作空间 <b>/ 管理控制台</b></span><div class="topbar-right"><span id="stamp" class="muted" role="status">正在加载状态…</span><span class="avatar" aria-label="管理员">A</span></div></header>
<main id="mainContent" tabindex="-1"><div class="page-heading"><div><p id="pageEyebrow" class="eyebrow">WORKSPACE / OVERVIEW</p><h1 id="pageTitle">运行概览</h1><p id="pageDescription">查看运行状态与关键指标，管理各平台的暂停状态。</p></div><span class="page-tag">管理控制台</span></div>
<section data-view="overview" id="gettingStarted"><h3>开始使用</h3><p class="muted">按下面的顺序完成连接。先确认规则与暂停状态，再让机器人运行。</p><div id="setupSteps" class="setup-grid"></div></section><div id="cards" class="cards" data-view="overview"></div>
<dialog id="loginWizard"><h3 id="loginWizardTitle">客户端网页登录</h3><p>请只在官方页面输入账号密码。机器人仅接收本次授权结果，登录凭据保存在服务器中。</p><label class="field">客户端验证方式<select id="wizardLoginMethod" onchange="switchLoginMethod(this.value)"><option value="auto">自动选择</option><option value="local">本地回调</option><option value="remote">设备码 / 验证码</option></select><small>切换会取消本次客户端登录等待并重新开始，不改变管理员 Passkey 鉴权。</small></label><div id="remoteLoginSteps" hidden><div class="info-banner">远程 / 手机登录不需要本地助手，也不需要向公网开放随机端口。</div><h4>1. 打开官方授权页面</h4><a id="remoteOfficialLink" target="_blank" rel="noopener noreferrer" hidden>打开官方登录页</a><div id="deviceCodeStep" hidden><h4>2. 在官方页面输入设备码</h4><pre id="remoteDeviceCode" aria-label="设备码"></pre><p>需要在账号安全设置或工作空间权限中允许设备码登录。完成后回到此页，客户端会自动确认。</p></div><div id="manualCodeStep" hidden><h4>2. 粘贴官方页面给出的验证码</h4><label class="field">本次验证码<input id="remoteLoginCode" type="password" autocomplete="off" autocapitalize="off" spellcheck="false" placeholder="仅填写官方显示的验证码"></label><button id="submitLoginCode" class="primary" onclick="submitRemoteCode()">提交验证码</button><p>验证码仅传给正在等待的官方客户端，不写入配置或审计记录。</p></div><h4>3. 等待客户端确认</h4><p id="remoteLoginResult" role="status"></p></div><div id="localLoginSteps"><p id="callbackProbeStatus" role="status">等待客户端提供实际回调地址…</p><button id="callbackProbeRetry" onclick="retryCallbackProbe()">重新检测回调映射</button><ol><li data-helper-step hidden><h4>复制本次登录命令</h4><p>选择浏览器所在电脑的系统。命令从当前机器人获取完整脚本后运行，只对本次登录有效、只可获取一次。请确认站点可信，不要分享命令或终端历史。</p><select id="helperPlatform" onchange="resetHelperCommand()"><option value="bash">macOS / Linux（Bash）</option><option value="powershell">Windows（PowerShell）</option></select><textarea id="helperCommand" readonly rows="5" style="width:100%;box-sizing:border-box" aria-label="本次登录助手命令" placeholder="正在生成命令…"></textarea><button id="helperCopy" onclick="copyHelperCommand()" disabled>复制命令</button></li><li data-helper-step hidden><h4>在终端粘贴运行</h4><p>macOS 打开“终端”，Linux 打开终端，Windows 打开 PowerShell，然后粘贴命令并回车。无需手动保存脚本、解压或打开可执行文件，也不会修改系统安全设置。若系统管理策略禁止脚本，请联系管理员。</p><p>macOS/Linux 需要 Python 3.9+，缺少时会明确提示；缺少 cryptography 时在临时 venv 安装加密依赖，结束后清理，不修改系统 Python。Windows 使用 PowerShell 5.1+ 和系统 .NET。</p><p>保持终端开启，显示 Ready 后回到此页面。助手使用本机网络/代理，不继承容器代理；网络失败请检查代理或防火墙，不要关闭 TLS 验证。端口冲突不会自动终止其他程序。取消或超时后助手释放监听。</p><p id="helperConnection" role="status">等待助手连接…</p></li><li><h4>在官方网页授权</h4><p>映射验证成功或助手 Ready 后，点击下面的链接。官方页面自动回调，无需复制代码。</p><a id="officialLoginLink" target="_blank" rel="noopener noreferrer" hidden>打开官方登录页</a></li><li><h4>确认完成</h4><p id="loginWizardResult" role="status">尚未完成。</p><p>只有这里显示“已登录”才算成功；失败或超时点击“重新开始”，获取新命令。助手会自动结束，也可用 Ctrl+C 停止并在此取消登录。</p></li></ol><button onclick="switchRemoteLogin()">改用设备码 / 验证码登录</button></div><p id="loginWizardError" class="danger"></p><div class="toolbar"><button onclick="restartWizard()">重新开始</button><button onclick="cancelWizard()">取消本次登录</button><button onclick="document.getElementById('loginWizard').close()">收起向导</button></div></dialog>
<section data-view="models" hidden><div class="section-heading"><div><h3>连接 AI 模型服务</h3><p>选择账号登录，或填写兼容 API。无需同时配置三种方式；数字越小越先尝试，不可用时依次切换。</p></div></div><div id="clientAlerts" class="status danger" role="alert"></div><div id="clientControls" class="service-grid"></div><div class="toolbar"><button class="primary" onclick="saveSelection()">保存模型启用与顺序</button><span id="modelManageStatus" class="status" role="status"></span></div><p id="clientControlError" class="danger"></p></section>
<section data-view="models" hidden><h3>服务可用性与实测质量</h3><p class="muted">限流、掉线或凭证过期的服务会自动退避，恢复后自动回到轮换；可用的服务按实测质量排序使用。</p><div id="providerHealth"></div></section>
<section data-view="models" id="modelConfigurationSection" hidden><h3>模型与连接配置</h3><p class="muted">保存会立即重新检查服务是否可用；不会自动调用付费模型。</p><div id="modelConfigurations"></div></section>
<section data-view="models" hidden class="advanced-section"><details><summary>安装新的模型服务扩展 <span>开发与自定义部署时使用</span></summary><p class="muted">把受信任的 Python 模型服务源码写入已配置目录。安装后保持禁用且不会初始化，请在上方明确启用并保存。</p><div class="plugin-grid"><label class="field"><b>安装目录</b><select id="modelInstallTarget"></select></label><label class="field"><b>服务名</b><input id="modelInstallName" placeholder="example_provider"></label></div><label class="field"><b>Python 源码</b><textarea id="modelInstallSource" rows="14" placeholder="def initialize_plugin(context): ..."></textarea></label><div class="toolbar"><button class="primary" onclick="installModelPlugin()">安装模型服务</button><span id="modelInstallStatus" class="status muted"></span></div></details></section>
<section data-view="overview"><h3>机器人运行控制</h3><p class="muted">主链只要求至少一个可用 AI 模型服务和至少一个成功启动的平台插件。策略、研究与两类过滤插件都是可选增强；插件自行报告能否启动，通用框架不会猜测其私有参数。这里的暂停设置会保留到下次启动。</p><div id="runtimeControl"></div><div class="toolbar"><button class="primary" onclick="saveRuntimeControl()">保存暂停状态</button><button onclick="refreshRuntime()">刷新运行状态</button><span id="runtimeStatus" class="status muted"></span></div></section>
<section data-view="security" hidden><h3>管理员安全</h3><div id="securityAccessNote" class="info-banner" hidden>当前通过本地 / 私网入口访问，没有创建需要退出的管理员登录会话。公网入口仍需要 Passkey；下方管理的是服务器已保存的登录凭据和会话。</div><p class="muted">Passkey 是设备上的登录凭据，可用指纹、面容或设备解锁验证。可以添加备用凭据，但必须保留至少一个。下方可查看登录设备并撤销会话。</p><div class="toolbar"><input id="newPasskeyName" placeholder="新 Passkey 名称"><button onclick="addPasskey()">添加 Passkey</button><button id="logoutSession" onclick="logout()">退出当前会话</button></div><h4>Passkey</h4><div id="passkeys"></div><h4>登录设备与会话</h4><div class="toolbar"><button onclick="kickSelectedSessions()">踢出选中会话</button></div><div id="sessions"></div></section>
<section data-view="settings" hidden id="environmentPanel"><div class="section-heading"><div><h3>服务器与网络</h3><p>查看机器人运行在哪里，以及对外访问时使用哪个公网 IP。</p></div><button onclick="refreshEnvironment()">刷新状态</button></div><div id="environmentSummary" class="summary-line"></div><div class="query-box"><h4>查找 API 白名单需要的 IP</h4><p class="muted">先对比服务器直连与统一继承代理访问同一组检测目标时的出口。选择 INHERIT 的 Binance、Polymarket 等插件会使用第二项；插件另有独立代理时，再从下拉框单独查询。</p><button id="probeEgressComparison" class="primary" onclick="probeEgressComparison()">对比两种出口</button><div id="egressComparison" class="egress-comparison"></div><div class="filter-bar"><label>单独检查哪个连接？<select id="egressRoute" aria-label="公网出口查询路径" onchange="renderEgressSelection()"></select></label><button id="probeEgress" onclick="probeEgress()">查询所选连接</button></div><div id="egressResults"></div><p id="environmentStatus" class="status" role="status"></p><p class="description">不会修改白名单或发送交易。查询服务和交易平台可能经过不同的网络出口；配置白名单前请再向平台核对。</p></div><div id="environmentFacts"></div></section>
<section data-view="settings" hidden class="advanced-section"><div class="section-intro"><h2>统一网络代理</h2><p>除 OpenAI 兼容 API 外，内置联网插件默认继承这里的设置；Codex、Claude、平台和研究插件仍可在各自配置中选择直连或填写独立代理。</p></div><div id="sharedProxySettings" class="plugin-grid"></div><div class="toolbar"><button class="primary" onclick="saveApplicationSettings()">保存程序配置</button><span id="settingsStatus" class="status muted"></span></div><details><summary>其它程序运行参数 <span>数据库、刷新频率与部署选项</span></summary><p class="muted">大部分选项无需修改。数据库和工作目录须在启动前确定，修改后需重启；公网查询服务与超时在下一次查询生效。</p><div id="applicationSettings" class="plugin-grid"></div><div class="toolbar"><button class="primary" onclick="saveApplicationSettings()">保存程序配置</button><button class="danger" onclick="resetAllApplicationSettings()">恢复全部默认值</button></div></details></section>
<section data-view="settings" hidden class="advanced-section"><details><summary>插件文件位置 <span>开发与自定义部署时修改</span></summary><p class="muted">程序从这些目录发现插件，每行一个目录。普通使用者无需改动；新增插件可在插件中心安装。</p><div id="pluginDirectories" class="plugin-grid"></div><div class="toolbar"><button class="primary" onclick="savePluginDirectories()">保存插件目录</button><button class="danger" onclick="resetPluginDirectories()">恢复默认目录</button></div></details></section>
<section data-view="plugins" hidden id="pluginWorkspace"><div id="pluginCategoryHome"></div><nav id="pluginCategoryNav" class="subnav" aria-label="插件分类"></nav><div id="pluginCategoryGuide"></div><div class="toolbar plugin-management-tools"><button onclick="refreshPlugins()">重新扫描插件文件</button><span class="muted">新增、删除或更新文件后使用；不会替你启用插件。</span></div><div id="pluginManager"></div><div class="toolbar plugin-management-tools"><button class="primary" onclick="saveSelection()">保存启用与顺序</button><span id="manageStatus" class="status" role="status"></span></div></section>
<section data-view="plugins" hidden><details><summary>安装新的自定义插件</summary><p class="muted">把受信任的 Python 插件源码写入已配置的类别目录。新插件安装后保持禁用，只扫描文件名；启用后才会导入并调用初始化函数。</p><div class="plugin-grid"><label class="field"><b>类别</b><select id="installKind" onchange="renderInstallTargets()"></select></label><label class="field"><b>安装目录</b><select id="installTarget"></select></label><label class="field"><b>插件名</b><input id="installName" placeholder="example_plugin"></label></div><label class="field"><b>Python 源码</b><textarea id="installSource" rows="14" placeholder="def initialize_plugin(context): ..."></textarea></label><div class="toolbar"><button class="primary" onclick="installPlugin()">安装并刷新</button><span id="installStatus" class="status muted"></span></div></details></section>
<section data-view="decisions" hidden><h3>查看机器人为什么这样做</h3><p class="muted">一条记录是一次判断，不等于一笔成交。先看“最终动作”和“执行状态”，再展开查看证据与规则检查；没有后续观察时不能判断盈亏。</p><ol class="process-strip"><li>发现市场</li><li>收集证据</li><li>模型判断</li><li>风险检查</li><li>执行与跟踪</li></ol><div class="filter-bar"><label>平台<input id="platform" placeholder="全部平台"></label><label>模型服务<input id="providerFilter" placeholder="全部服务"></label><label>记录状态<select id="statusFilter"><option value="">全部状态</option><option value="STARTED">分析中</option><option value="PROVIDER_ERROR">模型调用失败</option><option value="RISK_REJECTED">规则拒绝，未执行</option><option value="EXECUTION_ERROR">执行失败</option><option value="COMPLETED">流程已完成</option></select></label><label>最终动作<select id="actionFilter"><option value="">全部动作</option><option value="BUY">买入</option><option value="SELL">卖出</option><option value="HOLD">观望</option><option value="CANCEL">撤单</option></select></label><button class="primary" onclick="refreshAudit()">查询记录</button></div></section>
<section data-view="decisions" hidden><div class="section-heading"><div><h3>决策记录</h3><p>显示最近 100 条匹配记录。点击一条记录的“查看决策过程”追溯原因。</p></div></div><div id="decisions"></div></section>
<section data-view="decisions" hidden class="advanced-section"><details><summary>平台操作明细 <span>排查问题时展开</span></summary><p class="muted">发送给平台的操作和返回结果；请求失败不代表成交。</p><div id="actions"></div></details></section>
<section data-view="decisions" hidden class="advanced-section"><details><summary>模型对话明细 <span>排查问题时展开</span></summary><p class="muted">一次决策可能多次询问模型。这里用于排查模型调用失败。</p><div id="turns"></div></details></section>
<section data-view="decisions" hidden class="advanced-section"><details><summary>信息收集明细 <span>排查问题时展开</span></summary><p class="muted">模型为收集信息而调用的工具及结果。通常无需查看。</p><div id="steps"></div></details></section>
<section data-view="settings" hidden class="advanced-section"><details><summary>技术运行清单 <span>排查问题时查看</span></summary><pre id="manifest"></pre></details></section></main><button id="globalFeedback" hidden aria-label="关闭操作提示" role="status"></button>
<script src="/assets/login-probe.js"></script>
<script src="/assets/dashboard-views.js"></script>
<script>
const TOKEN='CSRF_TOKEN',SESSION_ID='SESSION_ID',KINDS=['api','decision_provider','decision_strategy','market_discovery','research_tool','agent_policy','risk'];
const LOCAL_ACCESS=LOCAL_ACCESS_VALUE;
const LABELS={api:'交易平台',decision_provider:'AI 模型服务',decision_strategy:'决策策略',market_discovery:'标的发现策略',research_tool:'信息与研究',agent_policy:'Agent 行为风控',risk:'业务风控'};
let LAST_MANAGER=null;
const esc=s=>String(s??'').replace(/[&<>\"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;'}[c]));
const detail=o=>'<details><summary>查看完整 JSON</summary><pre>'+esc(JSON.stringify(o,null,2))+'</pre></details>';
const b64u=b=>btoa(String.fromCharCode(...new Uint8Array(b))).replaceAll('+','-').replaceAll('/','_').replaceAll('=','');
const unb64u=s=>Uint8Array.from(atob(s.replaceAll('-','+').replaceAll('_','/')+'==='.slice((s.length+3)%4)),c=>c.charCodeAt(0));
function keyDb(){return new Promise((ok,no)=>{let r=indexedDB.open('prediction-agent-keys',1);r.onupgradeneeded=()=>r.result.createObjectStore('sessions');r.onsuccess=()=>ok(r.result);r.onerror=()=>no(r.error)})}
async function storedKey(){let d=await keyDb();return new Promise((ok,no)=>{let r=d.transaction('sessions').objectStore('sessions').get(SESSION_ID);r.onsuccess=()=>ok(r.result);r.onerror=()=>no(r.error)})}
async function dropKey(id=SESSION_ID){let d=await keyDb();return new Promise((ok,no)=>{let r=d.transaction('sessions','readwrite').objectStore('sessions').delete(id);r.onsuccess=()=>ok();r.onerror=()=>no(r.error)})}
let SESSION_KEY;
async function secure(u,v=null){if(!SESSION_KEY)SESSION_KEY=await storedKey();if(!SESSION_KEY){location='/api/auth/clear';throw Error('本机缺少此登录会话的加密密钥，请重新登录')}let nonce=crypto.getRandomValues(new Uint8Array(12)),plain=new TextEncoder().encode(JSON.stringify({url:u,body:v})),cipher=await crypto.subtle.encrypt({name:'AES-GCM',iv:nonce,additionalData:new TextEncoder().encode('POST /api/secure')},SESSION_KEY,plain),r=await fetch('/api/secure',{method:'POST',headers:{'Content-Type':'application/json','X-Admin-CSRF':TOKEN},body:JSON.stringify({nonce:b64u(nonce),ciphertext:b64u(cipher)})}),envelope=await r.json(),decoded;try{decoded=JSON.parse(new TextDecoder().decode(await crypto.subtle.decrypt({name:'AES-GCM',iv:unb64u(envelope.nonce),additionalData:new TextEncoder().encode('RESPONSE /api/secure')},SESSION_KEY,unb64u(envelope.ciphertext))))}catch(e){if(r.status===401){await dropKey();location='/api/auth/clear'}throw Error('加密响应认证失败')}if(!r.ok)throw Error(decoded.error||r.statusText);return decoded}
async function business(u,v=null){if(!LOCAL_ACCESS)return secure(u,v);let r=await fetch('/api/local',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url:u,body:v})}),result=await r.json();if(!r.ok)throw Error(result.error||result.detail||r.statusText);return result}
async function get(u){return business(u)}
async function post(u,v){return business(u,v)}
function table(rows,cols){if(!rows.length)return '<div class="empty-state"><strong>暂无记录</strong><p>数据产生后会显示在这里；也可以调整筛选条件。</p></div>';return '<div class="table-scroll" tabindex="0" role="region" aria-label="数据表格，可横向滚动"><table><thead><tr>'+cols.map(c=>'<th>'+c[0]+'</th>').join('')+'</tr></thead><tbody>'+rows.map(r=>'<tr>'+cols.map(c=>'<td>'+c[1](r)+'</td>').join('')+'</tr>').join('')+'</tbody></table></div>'}
function fieldHtml(kind,name,f){let id='cfg_'+kind+'_'+name+'_'+f.name,attrs=' id="'+esc(id)+'" data-field="'+esc(f.name)+'" data-type="'+esc(f.type)+'"';let input;if(f.selection_only)return selectionFieldHtml(kind,name,f);if(f.dynamic_choices)return dynamicChoiceHtml(kind,name,f);if(f.type==='boolean')input='<input type="checkbox"'+attrs+(f.value?' checked':'')+'>';else if(f.type==='enum')input='<select'+attrs+'>'+f.options.map(o=>'<option'+(o===f.value?' selected':'')+'>'+esc(o)+'</option>').join('')+'</select>';else if(f.sensitive)input=credentialInputHtml(id,f);else input='<input type="'+(f.sensitive?'password':(f.type==='integer'||f.type==='number'?'number':'text'))+'"'+attrs+' value="'+esc(f.value??'')+'" '+(f.type==='number'?'step="any"':'')+' placeholder="'+(f.sensitive&&f.configured?'已保存；留空保持不变':'')+'">';return '<div class="field"><label><b>'+esc(f.label)+(f.required?' *':'')+'</b>'+input+'<span class="description">'+esc(f.description)+(f.has_default?' 默认值：'+esc(JSON.stringify(f.default)):'')+'</span></label>'+'<button onclick="resetPluginField(\''+kind+'\',\''+name+'\',\''+f.name+'\')">删除此字段值</button></div>'}
function controlNode(tag,text,parent){let node=document.createElement(tag);if(text)node.textContent=text;if(parent)parent.append(node);return node}
let ACTIVE_LOGIN=null,HELPER_COMMAND_KEY='';
const LOGIN_METHODS=new Map();
function loginAction(name){const selected=LOGIN_METHODS.get(name)||'auto';return (selected==='auto'?preferredLoginMethod():selected)==='remote'?'login_remote':'login'}
function showLoginWizard(item){document.getElementById('remoteLoginCode').value='';ACTIVE_LOGIN={kind:item.kind,name:item.name};document.getElementById('loginWizardTitle').textContent=item.name+' 网页登录向导';document.getElementById('loginWizardError').textContent='';document.getElementById('wizardLoginMethod').value=LOGIN_METHODS.get(item.name)||'auto';let dialog=document.getElementById('loginWizard');if(!dialog.open)dialog.showModal();updateLoginWizard(item.status)}
let LAST_LOGIN_STATUS=null;
const CALLBACK_PROBES=new Map();
function updateLoginWizard(s){
    LAST_LOGIN_STATUS=s;
    const remote=s.login_mode==='remote';
    document.getElementById('remoteLoginSteps').hidden=!remote;
    document.getElementById('localLoginSteps').hidden=remote;
    if(remote){
        const link=document.getElementById('remoteOfficialLink');
        link.hidden=true;link.removeAttribute('href');
        if(s.state==='authorizing'&&s.authorization_url){const url=new URL(s.authorization_url);if(url.protocol==='https:'){link.href=url.href;link.hidden=false}}
        document.getElementById('deviceCodeStep').hidden=ACTIVE_LOGIN?.name!=='codex'||s.state!=='authorizing';
        document.getElementById('manualCodeStep').hidden=ACTIVE_LOGIN?.name!=='claude'||s.state!=='authorizing';
        document.getElementById('remoteDeviceCode').textContent=s.device_code||'正在获取设备码…';
        document.getElementById('submitLoginCode').disabled=!!s.code_submitted||!s.authorization_url;
        document.getElementById('remoteLoginResult').textContent=s.state==='authenticated'?'已登录，服务器已保存会话。':s.message;
        if(s.state!=='authorizing')document.getElementById('remoteLoginCode').value='';
        return;
    }
    const probe=CALLBACK_PROBES.get(s.flow_id),direct=s.callback_mode==='direct';
    document.getElementById('callbackProbeStatus').textContent=direct?'已验证：实际回调地址映射到当前容器的本次登录入口，无需助手。':(s.callback_mode==='helper'?'助手已接管本次回调。':(probe?.message||'等待客户端提供实际回调地址…'));
    document.querySelectorAll('[data-helper-step]').forEach(node=>node.hidden=direct||(!s.helper_ready&&probe?.state!=='failed'));
    document.getElementById('helperConnection').textContent=s.helper_message||'等待助手连接…';
    document.getElementById('callbackProbeRetry').disabled=s.state!=='authorizing'||!!s.helper_ready||probe?.state==='checking';
    let link=document.getElementById('officialLoginLink');link.hidden=true;link.removeAttribute('href');
    if(s.helper_ready&&s.authorization_url&&s.state==='authorizing'){let url=new URL(s.authorization_url);if(url.protocol==='https:'){link.href=url.href;link.hidden=false}}
    document.getElementById('loginWizardResult').textContent=s.state==='authenticated'?'已登录。容器已保存会话，可以关闭此向导。':(s.state==='authorizing'&&s.helper_ready?s.helper_message:s.message);
    if(s.state!=='authorizing'||s.helper_ready){document.getElementById('helperCommand').value='';document.getElementById('helperCopy').disabled=true}
    else if(probe?.state==='failed'&&!browserNeedsDesktopHelper())ensureHelperCommand();
    if(document.getElementById('loginWizard').open&&s.state==='authorizing'&&s.redirect_uri&&!s.helper_ready&&!probe)checkCallbackMapping(s);
}
async function checkCallbackMapping(s){
    const selection={...ACTIVE_LOGIN},flow=s.flow_id;
    CALLBACK_PROBES.set(flow,{state:'checking',message:'正在从当前浏览器验证 '+s.redirect_uri+' 的容器映射；如浏览器提示本地网络访问，请允许后重试。'});
    updateLoginWizard(s);
    try{
        let proof=false;
        for(let attempt=0;attempt<6;attempt++){
            if(ACTIVE_LOGIN?.name!==selection.name||LAST_LOGIN_STATUS?.flow_id!==flow||LAST_LOGIN_STATUS?.state!=='authorizing')return;
            proof=s.callback_probe&&await probeLoginCallback(s.redirect_uri,s.callback_probe);
            if(proof)break;
            if(attempt<5)await new Promise(resolve=>setTimeout(resolve,1000));
        }
        if(!proof)throw Error(s.callback_probe_error||'无法确认该地址映射到当前容器：可能没有映射、被本机其他客户端占用，或被浏览器策略阻止。可重新检测或使用下方助手。');
        const status=await post('/api/plugins/controls/action',{...selection,action:'callback_direct_ready',values:{flow_id:flow,redirect_uri:s.redirect_uri,proof}});
        CALLBACK_PROBES.set(flow,{state:'verified',message:'映射验证通过'});
        if(ACTIVE_LOGIN?.name===selection.name&&ACTIVE_LOGIN?.kind===selection.kind&&LAST_LOGIN_STATUS?.flow_id===flow)updateLoginWizard(status);
    }catch(e){
        CALLBACK_PROBES.set(flow,{state:'failed',message:e.message});
        if(ACTIVE_LOGIN?.name===selection.name&&ACTIVE_LOGIN?.kind===selection.kind&&LAST_LOGIN_STATUS?.flow_id===flow)updateLoginWizard(LAST_LOGIN_STATUS);
    }
}
function retryCallbackProbe(){if(LAST_LOGIN_STATUS){CALLBACK_PROBES.delete(LAST_LOGIN_STATUS.flow_id);updateLoginWizard(LAST_LOGIN_STATUS)}}
async function wizardAction(action,values={}){return post('/api/plugins/controls/action',{...ACTIVE_LOGIN,action,values})}
async function ensureHelperCommand(){let platform=document.getElementById('helperPlatform').value,flow=LAST_LOGIN_STATUS?.flow_id,key=ACTIVE_LOGIN?.name+':'+flow+':'+platform;if(!flow||HELPER_COMMAND_KEY===key)return;HELPER_COMMAND_KEY=key;let field=document.getElementById('helperCommand'),button=document.getElementById('helperCopy');field.value='';button.disabled=true;try{let result=await wizardAction('helper_command',{platform,flow_id:flow});if(HELPER_COMMAND_KEY===key&&LAST_LOGIN_STATUS?.flow_id===flow&&LAST_LOGIN_STATUS?.state==='authorizing'&&!LAST_LOGIN_STATUS.helper_ready){field.value=result.command;button.disabled=false}}catch(e){if(HELPER_COMMAND_KEY===key)document.getElementById('loginWizardError').textContent=e.message}}
function resetHelperCommand(){HELPER_COMMAND_KEY='';if(LAST_LOGIN_STATUS?.state==='authorizing'&&!LAST_LOGIN_STATUS.helper_ready)ensureHelperCommand()}
async function copyHelperCommand(){let field=document.getElementById('helperCommand');try{if(navigator.clipboard&&window.isSecureContext)await navigator.clipboard.writeText(field.value);else{field.focus();field.select();if(!document.execCommand('copy'))throw Error('请选中命令手动复制')}document.getElementById('loginWizardError').textContent='命令已复制。请在这台电脑的终端粘贴运行，保持窗口开启。'}catch(e){document.getElementById('loginWizardError').textContent=e.message}}
async function restartWizard(){try{await wizardAction('cancel');let status=await wizardAction(loginAction(ACTIVE_LOGIN.name));updateLoginWizard(status)}catch(e){document.getElementById('loginWizardError').textContent=e.message}}
async function switchRemoteLogin(){return switchLoginMethod('remote')}
async function switchLoginMethod(method){LOGIN_METHODS.set(ACTIVE_LOGIN.name,method);document.getElementById('wizardLoginMethod').value=method;document.getElementById('remoteLoginCode').value='';try{await wizardAction('cancel');updateLoginWizard(await wizardAction(loginAction(ACTIVE_LOGIN.name)));await refreshClientControls()}catch(e){document.getElementById('loginWizardError').textContent=e.message}}
async function submitRemoteCode(){const field=document.getElementById('remoteLoginCode'),code=field.value;field.value='';try{updateLoginWizard(await wizardAction('login_code',{flow_id:LAST_LOGIN_STATUS?.flow_id,code}))}catch(e){document.getElementById('loginWizardError').textContent=e.message}}
async function cancelWizard(){try{await wizardAction('cancel');document.getElementById('loginWizard').close();await refreshClientControls()}catch(e){document.getElementById('loginWizardError').textContent=e.message}}
setInterval(()=>{if(document.getElementById('loginWizard').open)refreshClientControls()},2000);
function renderConfigurationPresets(m){for(let [kind,plugins]of Object.entries(m.plugins)){for(let plugin of plugins){let presets=plugin.configuration?.presets||[];if(!presets.length)continue;let root=document.getElementById('plugin_'+kind+'_'+plugin.name).querySelector('.preset-slot'),bar=controlNode('div','',null);bar.className='toolbar';controlNode('span','预置配置（可编辑，保存后生效）：',bar);for(let preset of presets){let button=controlNode('button',preset.label,bar);button.onclick=()=>{if(!confirm('套用 '+preset.label+'？请重新填写此服务的 API Key；保存时会清除旧密钥。'))return;for(let [name,value]of Object.entries(preset.values)){let input=document.getElementById('cfg_'+kind+'_'+plugin.name+'_'+name);if(!input)continue;if(input.type==='checkbox')input.checked=!!value;else input.value=value;if(input.dataset.type==='secret'){input.dataset.clearSecret='true';input.placeholder='请填写此服务的密钥，保存时不会保留旧密钥'}}}}root.prepend(bar)}}}



async function refreshRuntime(){let r=await get('/api/runtime');renderRuntime(r);return r}
let FEEDBACK_TIMER;
function showOperationFeedback(message,state='good',sticky=false){let n=document.getElementById('globalFeedback');clearTimeout(FEEDBACK_TIMER);n.className=state;n.textContent=message;n.hidden=false;n.onclick=()=>{n.hidden=true};if(!sticky)FEEDBACK_TIMER=setTimeout(()=>{n.hidden=true},5000)}
function setOperationStatus(node,message,state='good'){if(node){node.className='status '+state;node.textContent=message}showOperationFeedback(message,state,state==='pending')}
function pluginFeedback(kind,name){return document.getElementById('pluginStatus_'+kind+'_'+name)||managementFeedback()}
function markPluginSelectionDirty(input){let kind=input.dataset.kind,name=input.dataset.name,card=input.closest('.configuration-card'),note=document.getElementById('selectionStatus_'+kind+'_'+name),disabling=input.classList.contains('enable')&&!input.checked;card?.classList.add('pending-change');if(note){note.className='status pending';note.textContent=disabling?'尚未保存：保存后将禁用，并从启动向导的可选项中移除。':'尚未保存：点击本页“保存启用与顺序”后才会生效。'}let s=managementFeedback();s.className='status pending';s.textContent='插件启用或顺序有未保存更改';showOperationFeedback('插件更改尚未保存，请点击“保存启用与顺序”。','pending',true)}
function markModelSelectionDirty(input){let name=input.dataset.name,note=document.getElementById('modelSelectionStatus_'+name),disabled=input.classList.contains('model-enable')&&!input.checked;if(note){note.className='status pending';note.textContent=disabled?'尚未保存：保存后将停止使用此模型服务。':'尚未保存：点击“保存模型启用与顺序”后生效。'}let status=document.getElementById('modelManageStatus');status.className='status pending';status.textContent='模型服务启用或顺序有未保存更改';showOperationFeedback('模型服务更改尚未保存。','pending',true)}
async function saveRuntimeControl(){let s=document.getElementById('runtimeStatus');try{let r=await post('/api/runtime/control',{robot_paused:document.getElementById('pauseAll').checked,paused_platforms:[...document.querySelectorAll('.pausePlatform:checked')].map(e=>e.value)});delete document.getElementById('runtimeControl').dataset.dirty;renderRuntime(r.runtime);s.className='status good';s.textContent='暂停状态已保存并生效'}catch(e){s.className='status danger';s.textContent=e.message}}
function renderInstallTargets(){if(!LAST_MANAGER)return;let kind=document.getElementById('installKind').value||KINDS[0],dirs=LAST_MANAGER.plugin_directories.categories[kind]||[];document.getElementById('installTarget').innerHTML=dirs.map(d=>'<option value="'+esc(d)+'">'+esc(d)+'</option>').join('')}
async function installPlugin(){let s=document.getElementById('installStatus');try{let result=await post('/api/plugins/install',{kind:document.getElementById('installKind').value,target_directory:document.getElementById('installTarget').value,name:document.getElementById('installName').value,source:document.getElementById('installSource').value});renderManager(result.management);s.className='status good';s.textContent='已安装：'+result.installed}catch(e){s.className='status danger';s.textContent=e.message}}
async function refreshManager(){let [m,r]=await Promise.all([get('/api/plugins/manage'),get('/api/runtime')]);renderManager(m);renderRuntime(r);renderProviderHealth(r);await refreshClientControls()}
async function refreshPlugins(){let s=managementFeedback();setOperationStatus(s,'正在重新扫描插件文件…','pending');try{let m=await post('/api/plugins/refresh',{});renderManager(m);setOperationStatus(managementFeedback(),'插件已按最新文件和启用名单刷新')}catch(e){setOperationStatus(s,e.message,'danger')}}
async function savePluginConfig(kind,name){let root=document.getElementById('plugin_'+kind+'_'+name),values={},clear_secrets=[];for(let e of root.querySelectorAll('[data-field]')){if(e.dataset.clearSecret==='true')clear_secrets.push(e.dataset.field);let v=e.type==='checkbox'?e.checked:e.value;if(e.dataset.type==='integer')v=Number.parseInt(v,10);if(e.dataset.type==='number')v=Number(v);values[e.dataset.field]=v}setOperationStatus(pluginFeedback(kind,name),'正在保存 '+name+' 配置…','pending');try{await post('/api/plugins/config',{kind,name,values,clear_secrets});await refreshManager();setOperationStatus(pluginFeedback(kind,name),name+' 配置已保存，运行条件已重新检查')}catch(e){setOperationStatus(pluginFeedback(kind,name),e.message,'danger')}}
async function deletePluginConfig(kind,name){if(!confirm('删除 '+kind+':'+name+' 的私有配置并恢复默认值？'))return;setOperationStatus(pluginFeedback(kind,name),'正在删除 '+name+' 配置…','pending');try{await post('/api/plugins/config/delete',{kind,name});await refreshManager();setOperationStatus(pluginFeedback(kind,name),kind+':'+name+' 配置已删除')}catch(e){setOperationStatus(pluginFeedback(kind,name),e.message,'danger')}}
async function resetPluginField(kind,name,field){setOperationStatus(pluginFeedback(kind,name),'正在恢复 '+field+'…','pending');try{await post('/api/plugins/config/reset',{kind,name,fields:[field]});await refreshManager();setOperationStatus(pluginFeedback(kind,name),kind+':'+name+' 的 '+field+' 已恢复默认值')}catch(e){setOperationStatus(pluginFeedback(kind,name),e.message,'danger')}}
async function saveSelection(){let enabled={};for(let kind of KINDS)enabled[kind]=[];for(let kind of PLUGIN_CENTER_KINDS.filter(x=>x!=='decision_strategy')){let rows=[...document.querySelectorAll('.enable[data-kind="'+kind+'"]:checked')].map(e=>({name:e.dataset.name,priority:Number(document.querySelector('.priority[data-kind="'+kind+'"][data-name="'+e.dataset.name+'"]').value)||99})).sort((a,b)=>a.priority-b.priority);enabled[kind]=rows.map(x=>x.name)}enabled.decision_provider=[...document.querySelectorAll('.model-enable:checked')].map(e=>({name:e.dataset.name,priority:Number(document.querySelector('.model-priority[data-name="'+e.dataset.name+'"]').value)||99})).sort((a,b)=>a.priority-b.priority).map(x=>x.name);let strategy=document.querySelector('input[name="strategy"]:checked')?.value||'';enabled.decision_strategy=strategy?[strategy]:[];let status=managementFeedback();setOperationStatus(status,'正在保存启用状态与顺序…','pending');try{await post('/api/plugins/selection',{enabled,decision_strategy:strategy,strategy_evolution:document.getElementById('evolutionToggle')?.checked!==false});await refreshManager();setOperationStatus(managementFeedback(),'启用状态和优先级已保存；启动向导已按新状态更新')}catch(e){setOperationStatus(status,e.message,'danger')}}
function appFieldHtml(f){let attrs=' data-app-field="'+esc(f.name)+'" data-type="'+esc(f.type)+'"';let input=f.type==='enum'?'<select'+attrs+'>'+f.options.map(o=>'<option'+(o===f.value?' selected':'')+'>'+esc(o)+'</option>').join('')+'</select>':'<input type="'+(f.type==='integer'?'number':'text')+'"'+attrs+' value="'+esc(f.value)+'"'+(f.minimum!==null?' min="'+f.minimum+'"':'')+(f.maximum!==null?' max="'+f.maximum+'"':'')+'>';return '<div class="plugin"><label class="field"><b>'+esc(f.label)+'</b>'+input+'<span class="description">'+esc(f.description)+' 默认值：'+esc(JSON.stringify(f.default))+'；'+(f.configured?'当前为用户覆盖值':'当前使用默认值')+'</span></label><button onclick="resetApplicationSetting(\''+f.name+'\')">删除此覆盖值</button></div>'}
async function refreshConfiguration(){let [settings,manager]=await Promise.all([get('/api/settings'),get('/api/plugins/manage')]),proxyNames=new Set(['shared_http_proxy','shared_no_proxy','host_proxy_file']);document.getElementById('sharedProxySettings').innerHTML=settings.fields.filter(f=>proxyNames.has(f.name)).map(appFieldHtml).join('');document.getElementById('applicationSettings').innerHTML=settings.fields.filter(f=>!proxyNames.has(f.name)).map(appFieldHtml).join('');let d=manager.plugin_directories;document.getElementById('pluginDirectories').innerHTML=KINDS.map(k=>'<label class="field plugin"><b>'+LABELS[k]+'</b><textarea rows="3" data-dir-kind="'+k+'">'+esc((d.categories[k]||[]).join('\n'))+'</textarea></label>').join('')}
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
async function refreshAudit(){try{let p=document.getElementById('platform').value,q=p?'&platform='+encodeURIComponent(p):'',dq=q+'&provider='+encodeURIComponent(document.getElementById('providerFilter').value)+'&status='+encodeURIComponent(document.getElementById('statusFilter').value)+'&action='+encodeURIComponent(document.getElementById('actionFilter').value);let [s,d,a,t,g,m]=await Promise.all([get('/api/summary'),get('/api/decisions?limit=100'+dq),get('/api/records?kind=actions&limit=100'+q),get('/api/records?kind=turns&limit=100'+q),get('/api/records?kind=steps&limit=100'+q),get('/api/manifest')]);let ag=s.aggregate_account||{},metrics=ag.risk_metrics||{},cards=[['权益',ag.equity],...Object.entries(metrics).map(([k,v])=>['风控指标：'+k,v]),['决策',s.summary?.decisions],['模型调用错误',s.summary?.provider_errors],['信息收集步骤',s.summary?.agent_steps]];document.getElementById('cards').innerHTML=cards.map(x=>'<div class=card><div class=muted>'+esc(x[0])+'</div><h2>'+esc(x[1]??'—')+'</h2></div>').join('');renderDecisionLedger(d.items);document.getElementById('actions').innerHTML=table(a.items,[['时间',r=>new Date(r.created_at).toLocaleString()],['平台',r=>esc(r.platform)],['动作',r=>esc(r.action)],['结果',r=>detail(r)]]);document.getElementById('turns').innerHTML=table(t.items,[['时间',r=>new Date(r.created_at).toLocaleString()],['平台/Provider',r=>esc(r.platform+' / '+r.provider)],['状态',r=>esc(r.status)],['内容',r=>detail(r)]]);document.getElementById('steps').innerHTML=table(g.items,[['时间',r=>new Date(r.created_at).toLocaleString()],['平台/Provider',r=>esc(r.platform+' / '+r.provider)],['工具/状态',r=>esc((r.tool_name||'control')+' / '+r.status)],['内容',r=>detail(r)]]);document.getElementById('manifest').textContent=JSON.stringify(m,null,2);document.getElementById('stamp').textContent='更新 '+new Date().toLocaleTimeString()}catch(e){document.getElementById('stamp').textContent='错误: '+e}}
document.addEventListener('toggle',()=>activateChoiceLists(),true);document.getElementById('platform').onchange=refreshAudit;Promise.all([refreshSecurity(),refreshManager(),refreshConfiguration(),refreshAudit()]);setInterval(()=>Promise.all([refreshAudit(),refreshRuntime(),refreshClientControls()]),REFRESH_MS);
</script><script src="/assets/dashboard-shell.js"></script><script src="/assets/environment.js"></script></body></html>"""


AUTH_HTML = r"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Admin Passkey</title><link rel="stylesheet" href="/assets/dashboard.css"></head><body class="auth-page"><div class="box"><div class="brand"><span class="brand-mark">↗</span>Prediction Agent</div><h2>ADMIN_TITLE</h2><p class="muted">Passkey 验证同时绑定一次 P-256 ECDH 密钥交换。登录成功后，业务请求和响应均使用该会话密钥加密。</p><input id="name" placeholder="Passkey 名称" ADMIN_NAME><button onclick="begin()">ADMIN_ACTION</button><p id="status" class="danger"></p></div><script>
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
    def __init__(
        self,
        config: Config,
        management: PluginManagementService | None = None,
        runtime: RobotRuntimeManager | None = None,
    ):
        self.config = config
        self.management = management or PluginManagementService(config)
        self.runtime = runtime
        memory = SessionMemory(config.session_db)
        memory.close()

    def state_files(self) -> dict[str, Path]:
        current = Config.load(self.config.application_config_file)
        multiple = len(current.market_api_plugins) > 1
        return {
            name: platform_state_path(current.state_file, name, multiple)
            for name in current.market_api_plugins
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
        current = Config.load(self.config.application_config_file)
        plugins: dict[str, Any] = {}
        for name in current.market_api_plugins:
            try:
                spec = self.management.catalog.get("api", name)
                plugins[name] = {
                    "readiness": spec.readiness().manifest(),
                    "metadata": spec.manifest(),
                }
            except Exception as error:
                plugins[name] = {"readiness": {"ready": False, "reasons": [str(error)]}}
        return {
            "configured_plugins": list(current.market_api_plugins),
            "plugins": plugins,
            "plugin_management": self.management.manifest(),
            "strategy": {"name": current.decision_strategy_name},
            "runtime": self.runtime.status() if self.runtime else None,
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


def create_app(config: Config, *, start_robot: bool = True) -> FastAPI:
    """Create the importable HTTP application used by local and cloud entry points."""
    management = PluginManagementService(config)
    runtime = RobotRuntimeManager(config.application_config_file)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        try:
            if start_robot:
                runtime.reconcile()
            yield
        finally:
            runtime.stop()
            management.shutdown()

    app = FastAPI(
        title="Prediction Market Agent", docs_url=None, redoc_url=None, lifespan=lifespan
    )
    application_settings = ApplicationConfigStore(config.application_config_file)
    environment = EnvironmentDiagnostics(config, management, application_settings)
    data = AuditData(config, management, runtime)
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
        if is_local_request(request):
            return (
                HTML.replace("REFRESH_MS", str(config.dashboard_refresh_seconds * 1000))
                .replace("CSRF_TOKEN", "")
                .replace("'SESSION_ID'", "''")
                .replace("LOCAL_ACCESS_VALUE", "true")
                .replace("<section><h3>管理员安全</h3>", '<section hidden><h3>管理员安全</h3>')
            )
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
            .replace("'SESSION_ID'", json.dumps(session.session_id))
            .replace("LOCAL_ACCESS_VALUE", "false")
        )

    @app.get("/assets/{asset}")
    def dashboard_asset(asset: str) -> Response:
        if asset not in {"login-probe.js", "dashboard-shell.js", "dashboard.css", "environment.js", "dashboard-views.js"}:
            raise HTTPException(status_code=404, detail="Unknown asset")
        return Response(
            Path(__file__).with_name("static").joinpath(asset).read_text(),
            media_type="text/css" if asset.endswith(".css") else "application/javascript",
            headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"},
        )

    @app.get("/healthz")
    def health() -> dict[str, Any]:
        return {"ok": True, "robot": runtime.status()}

    @app.get("/api/auth/status")
    def auth_status(request: Request) -> dict[str, Any]:
        if is_local_request(request):
            return {"local_access": True, "authentication_required": False,
                    "encryption_required": False, "session_id": None}
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

    def dispatch(request: Request, session: AdminSession | None, route: str, body: Any) -> Any:
        parts = urlsplit(route)
        path = parts.path
        query = parse_qs(parts.query)
        payload = body if isinstance(body, dict) else {}
        if path == "/api/environment":
            return environment.snapshot(request)
        if path == "/api/environment/probe":
            return environment.probe(str(payload.get("route_id", "")))
        if path in {"/api/summary", "/api/report"}:
            return data.summary()
        if path == "/api/manifest":
            return data.manifest()
        if path == "/api/plugins/manage":
            return management.manifest()
        if path == "/api/strategies/export":
            return runtime.export_strategies(str(payload.get("lane", "")))
        if path == "/api/providers/recheck":
            return runtime.recheck_providers(str(payload.get("provider", "")))
        if path == "/api/plugins/controls":
            return management.control_status()
        if path == "/api/plugins/controls/action":
            values = payload.get("values", {})
            if not isinstance(values, dict):
                raise ValueError("Control values must be an object")
            values = {**values, "_public_origin": str(request.base_url).rstrip("/")}
            return management.control_action(str(payload.get("kind", "")), str(payload.get("name", "")),
                                             str(payload.get("action", "")), values)
        if path == "/api/plugins/config/choices":
            return management.configuration_choices(str(payload.get("kind", "")), str(payload.get("name", "")), str(payload.get("field", "")), payload.get("values"))
        if path == "/api/runtime":
            status=runtime.status()
            status["setup"]=setup_guide(status,management.manifest(),automatic_start=start_robot)
            return status
        if path == "/api/runtime/control":
            runtime.stop()
            result = management.save_runtime_control(
                robot_paused=payload.get("robot_paused", False),
                paused_platforms=payload.get("paused_platforms", []),
            )
            if start_robot:
                runtime.reconcile()
            return {**result, "runtime": runtime.status()}
        if path == "/api/settings":
            if body is None:
                return application_settings.manifest()
            return application_settings.save(payload.get("values", {}))
        if path == "/api/settings/reset":
            return application_settings.reset(
                payload.get("names") if "names" in payload else None
            )
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
            runtime.stop()
            result = management.save_plugin_configuration(str(payload.get("kind", "")), str(payload.get("name", "")), payload.get("values", {}), clear_secrets=payload.get("clear_secrets"))
            management.refresh()
            if start_robot:
                runtime.reconcile()
            return result
        if path == "/api/plugins/config/delete":
            runtime.stop()
            result = management.delete_plugin_configuration(str(payload.get("kind", "")), str(payload.get("name", "")))
            management.refresh()
            if start_robot:
                runtime.reconcile()
            return result
        if path == "/api/plugins/config/reset":
            runtime.stop()
            result = management.reset_plugin_configuration_fields(str(payload.get("kind", "")), str(payload.get("name", "")), payload.get("fields", []))
            management.refresh()
            if start_robot:
                runtime.reconcile()
            return result
        if path == "/api/plugins/directories":
            runtime.stop()
            result = management.save_plugin_directories(payload.get("categories", {}))
            management.refresh()
            if start_robot:
                runtime.reconcile()
            return result
        if path == "/api/plugins/directories/reset":
            runtime.stop()
            result = management.reset_plugin_directories()
            management.refresh()
            if start_robot:
                runtime.reconcile()
            return result
        if path == "/api/plugins/selection":
            runtime.stop()
            result = management.save_enabled(payload)
            if start_robot:
                runtime.reconcile()
            return result
        if path == "/api/plugins/refresh":
            runtime.stop()
            result = management.refresh()
            if start_robot:
                runtime.reconcile()
            return result
        if path == "/api/plugins/install":
            runtime.stop()
            result = management.install_plugin(
                str(payload.get("kind", "")),
                str(payload.get("name", "")),
                str(payload.get("source", "")),
                str(payload.get("target_directory", "")),
            )
            if start_robot:
                runtime.reconcile()
            return result
        if path == "/api/auth/manage":
            return {"passkeys": auth.credentials(), "sessions": auth.sessions(session.session_id if session else "")}
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
            if session:
                auth.logout(session.token)
            return {"ok": True}
        raise ValueError(f"Unknown protected operation: {path}")

    @app.post("/api/local")
    async def local_api(request: Request) -> JSONResponse:
        require_local_request(request)
        if request.headers.get("content-type", "").split(";", 1)[0].strip() != "application/json":
            raise HTTPException(status_code=415, detail="Local operations require JSON")
        message = await request.json()
        if not isinstance(message, dict) or not isinstance(message.get("url"), str):
            raise ValueError("Local operation must include a URL")
        return JSONResponse(await dispatch_async(request, None, message))

    @app.post("/api/plugin-helper/{kind}/{name}")
    async def plugin_helper(kind: str, name: str, request: Request):
        if request.headers.get("content-length") and int(request.headers["content-length"]) > 32768:
            raise HTTPException(status_code=413)
        raw = await request.body()
        if len(raw) > 32768:
            raise HTTPException(status_code=413)
        try:
            spec = management.catalog.get(kind, name)
            if spec.controls is None or spec.controls.helper_callback is None:
                raise ValueError("No helper capability")
            header = request.headers.get("authorization", "")
            if not header.startswith("Bearer "):
                raise ValueError("Missing pairing capability")
            result = await run_in_threadpool(spec.controls.helper_callback, header[7:], json.loads(raw))
            return JSONResponse(result, headers={"Cache-Control": "no-store"})
        except Exception:
            raise HTTPException(status_code=401, detail="Helper pairing expired or request rejected") from None

    @app.get("/api/plugin-helper/{kind}/{name}/script/{platform}")
    async def plugin_helper_script(kind: str, name: str, platform: str, request: Request):
        try:
            spec = management.catalog.get(kind, name)
            header = request.headers.get("authorization", "")
            if spec.controls is None or spec.controls.helper_callback is None or not header.startswith("Bearer "):
                raise ValueError("Missing script capability")
            result = await run_in_threadpool(spec.controls.helper_callback, header[7:], {"script_platform": platform})
            return Response(result["script"], media_type="text/plain", headers={
                "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"})
        except Exception:
            raise HTTPException(status_code=401, detail="Login script expired; generate a new command in the wizard") from None

    async def dispatch_async(request, session, message):
        if urlsplit(message["url"]).path in {"/api/plugins/config/choices", "/api/plugins/controls/action", "/api/environment", "/api/environment/probe"}:
            return await run_in_threadpool(dispatch, request, session, message["url"], message.get("body"))
        return dispatch(request, session, message["url"], message.get("body"))

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
            result = await dispatch_async(request, session, message)
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
