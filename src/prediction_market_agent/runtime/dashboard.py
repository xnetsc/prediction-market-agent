from __future__ import annotations

import json
import secrets
import sqlite3
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from ..core.config import Config
from ..sdk.management import PluginManagementService
from ..sdk.contracts import platform_state_path
from ..sdk.registry import load_api_plugins
from .reporting import build_report
from .memory import SessionMemory


HTML = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Prediction Agent</title>
<style>
body{font:14px system-ui;margin:0;background:#0b1020;color:#e8eefc}header{position:sticky;top:0;z-index:4;background:#151d33;padding:16px 24px}main{padding:20px;max-width:1500px;margin:auto}.cards,.plugin-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px}.card,section,.plugin{background:#151d33;border:1px solid #293453;border-radius:10px;padding:14px;margin-bottom:16px}.plugin{margin:0}.plugin.off{opacity:.62}button,select,input{background:#263453;color:#fff;border:1px solid #526180;border-radius:6px;padding:7px;box-sizing:border-box}button{cursor:pointer}.primary{background:#325cc7}.danger{color:#ff8a8a}.good{color:#8ee6ac}.muted{color:#9fb0d0}.field{display:grid;gap:5px;margin:12px 0}.field input,.field select{width:100%}.description{font-size:12px;color:#9fb0d0}.toolbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap}table{border-collapse:collapse;width:100%}th,td{text-align:left;vertical-align:top;border-bottom:1px solid #2b3654;padding:8px;max-width:520px}pre{white-space:pre-wrap;word-break:break-word;max-height:360px;overflow:auto}details{max-width:760px}h3{margin-top:0}.status{min-height:20px}
</style></head><body>
<header><b>Prediction Agent</b> <span id="stamp" class="muted"></span></header><main>
<div id="cards" class="cards"></div>
<section><h3>插件管理</h3><p class="muted">SDK 保存有序启用名单；禁用插件不会被导入或初始化，只按文件名展示。插件自己的配置由其初始化函数提供的读写回调管理。</p><div class="toolbar"><button onclick="refreshPlugins()">刷新插件</button><span>刷新后以磁盘和启用名单的最新状态为准。</span></div><div id="pluginManager"></div><div class="toolbar"><button class="primary" onclick="saveSelection()">保存启用状态与顺序</button><span id="manageStatus" class="status muted"></span></div></section>
<section><h3>决策账本筛选</h3><div class="toolbar"><input id="platform" placeholder="平台（留空为全部）"><input id="providerFilter" placeholder="Provider"><input id="statusFilter" placeholder="状态"><select id="actionFilter"><option value="">全部动作</option><option>BUY</option><option>SELL</option><option>HOLD</option><option>CANCEL</option></select><button onclick="refreshAudit()">刷新</button></div></section>
<section><h3>决策账本：前因 → 判断 → 风控 → 执行 → 后续观察</h3><p class="muted">每行对应一次完整决策，展开 JSON 可检查当时上下文、研究证据、原始模型输出和插件判定。</p><div id="decisions"></div></section>
<section><h3>执行动作 / 失败</h3><div id="actions"></div></section>
<section><h3>Agent 决策轮次</h3><div id="turns"></div></section>
<section><h3>Agent 工具步骤</h3><div id="steps"></div></section>
<section><h3>运行清单</h3><pre id="manifest"></pre></section></main>
<script>
const TOKEN='CSRF_TOKEN',KINDS=['api','decision_provider','decision_strategy','research_tool','risk','hook'];
const LABELS={api:'市场 API',decision_provider:'决策 Provider',decision_strategy:'决策策略',research_tool:'研究工具',risk:'风控',hook:'Hook'};
const esc=s=>String(s??'').replace(/[&<>\"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;'}[c]));
const detail=o=>'<details><summary>查看完整 JSON</summary><pre>'+esc(JSON.stringify(o,null,2))+'</pre></details>';
async function get(u){let r=await fetch(u);if(!r.ok)throw Error(await r.text());return r.json()}
async function post(u,v){let r=await fetch(u,{method:'POST',headers:{'Content-Type':'application/json','X-Plugin-Management-Token':TOKEN},body:JSON.stringify(v)});if(!r.ok)throw Error((await r.json()).error||r.statusText);return r.json()}
function table(rows,cols){return '<table><thead><tr>'+cols.map(c=>'<th>'+c[0]+'</th>').join('')+'</tr></thead><tbody>'+rows.map(r=>'<tr>'+cols.map(c=>'<td>'+c[1](r)+'</td>').join('')+'</tr>').join('')+'</tbody></table>'}
function fieldHtml(kind,name,f){let id='cfg_'+kind+'_'+name+'_'+f.name,attrs=' id="'+esc(id)+'" data-field="'+esc(f.name)+'" data-type="'+esc(f.type)+'"';let input;if(f.type==='boolean')input='<input type="checkbox"'+attrs+(f.value?' checked':'')+'>';else if(f.type==='enum')input='<select'+attrs+'>'+f.options.map(o=>'<option'+(o===f.value?' selected':'')+'>'+esc(o)+'</option>').join('')+'</select>';else input='<input type="'+(f.sensitive?'password':(f.type==='integer'||f.type==='number'?'number':'text'))+'"'+attrs+' value="'+esc(f.value??'')+'" '+(f.type==='number'?'step="any"':'')+' placeholder="'+(f.sensitive&&f.configured?'已保存；留空保持不变':'')+'">';return '<label class="field"><b>'+esc(f.label)+(f.required?' *':'')+'</b>'+input+'<span class="description">'+esc(f.description)+(f.has_default?' 默认值：'+esc(JSON.stringify(f.default)):'')+'</span></label>'}
function renderManager(m){let out='';for(let kind of KINDS){out+='<h3>'+LABELS[kind]+'</h3><div class="plugin-grid">';for(let p of m.plugins[kind]){let key=kind+'_'+p.name;let selector=kind==='decision_strategy'?'<label><input type="radio" name="strategy" value="'+esc(p.name)+'" '+(p.enabled?'checked':'')+'> 使用此策略</label>':'<label><input class="enable" type="checkbox" data-kind="'+kind+'" data-name="'+esc(p.name)+'" '+(p.enabled?'checked':'')+'> 启用</label> <label>优先级 <input class="priority" type="number" min="1" data-kind="'+kind+'" data-name="'+esc(p.name)+'" value="'+esc(p.priority??99)+'" style="width:70px"></label>';let fields=p.configuration?p.configuration.fields.map(f=>fieldHtml(kind,p.name,f)).join(''):'<p class="muted">此插件没有私有配置。</p>';let save=p.configuration?'<button onclick="savePluginConfig(\''+kind+'\',\''+p.name+'\')">保存插件配置</button>':'';out+='<div class="plugin '+(p.enabled?'':'off')+'" id="plugin_'+key+'"><div class="toolbar"><b>'+esc(p.name)+'</b>'+selector+'</div><p>'+esc(p.description)+'</p><div class="description">来源：'+esc(p.origin)+'</div>'+fields+save+'</div>'}out+='</div>'}document.getElementById('pluginManager').innerHTML=out}
async function refreshManager(){let m=await get('/api/plugins/manage');renderManager(m)}
async function refreshPlugins(){let s=document.getElementById('manageStatus');try{let m=await post('/api/plugins/refresh',{});renderManager(m);s.className='status good';s.textContent='插件已按最新文件和启用名单刷新'}catch(e){s.className='status danger';s.textContent=e.message}}
async function savePluginConfig(kind,name){let root=document.getElementById('plugin_'+kind+'_'+name),values={};for(let e of root.querySelectorAll('[data-field]')){let v=e.type==='checkbox'?e.checked:e.value;if(e.dataset.type==='integer')v=Number.parseInt(v,10);if(e.dataset.type==='number')v=Number(v);values[e.dataset.field]=v}let s=document.getElementById('manageStatus');try{await post('/api/plugins/config',{kind,name,values});s.className='status good';s.textContent=kind+':'+name+' 配置已保存，重启后生效';await refreshManager()}catch(e){s.className='status danger';s.textContent=e.message}}
async function saveSelection(){let enabled={};for(let kind of KINDS)enabled[kind]=[];for(let kind of KINDS.filter(x=>x!=='decision_strategy')){let rows=[...document.querySelectorAll('.enable[data-kind="'+kind+'"]:checked')].map(e=>({name:e.dataset.name,priority:Number(document.querySelector('.priority[data-kind="'+kind+'"][data-name="'+e.dataset.name+'"]').value)||99})).sort((a,b)=>a.priority-b.priority);enabled[kind]=rows.map(x=>x.name)}let strategy=document.querySelector('input[name="strategy"]:checked')?.value||'';enabled.decision_strategy=strategy?[strategy]:[];let s=document.getElementById('manageStatus');try{await post('/api/plugins/selection',{enabled,decision_strategy:strategy});s.className='status good';s.textContent='启用状态和优先级已保存，重启后生效';await refreshManager()}catch(e){s.className='status danger';s.textContent=e.message}}
async function refreshAudit(){try{let p=document.getElementById('platform').value,q=p?'&platform='+encodeURIComponent(p):'',dq=q+'&provider='+encodeURIComponent(document.getElementById('providerFilter').value)+'&status='+encodeURIComponent(document.getElementById('statusFilter').value)+'&action='+encodeURIComponent(document.getElementById('actionFilter').value);let [s,d,a,t,g,m]=await Promise.all([get('/api/summary'),get('/api/decisions?limit=100'+dq),get('/api/records?kind=actions&limit=100'+q),get('/api/records?kind=turns&limit=100'+q),get('/api/records?kind=steps&limit=100'+q),get('/api/manifest')]);let ag=s.aggregate_account||{},metrics=ag.risk_metrics||{},cards=[['权益',ag.equity],...Object.entries(metrics).map(([k,v])=>['风控指标：'+k,v]),['决策',s.summary?.decisions],['Provider 错误',s.summary?.provider_errors],['Agent 步骤',s.summary?.agent_steps]];document.getElementById('cards').innerHTML=cards.map(x=>'<div class=card><div class=muted>'+esc(x[0])+'</div><h2>'+esc(x[1]??0)+'</h2></div>').join('');document.getElementById('decisions').innerHTML=table(d.items,[['时间/ID',r=>new Date(r.created_at).toLocaleString()+'<br>#'+esc(r.id)],['市场',r=>esc(r.platform+' / '+(r.context?.market?.title||r.market_topic_id))],['Provider/策略',r=>esc((r.provider||'—')+' / '+r.strategy_name)],['证据',r=>esc(r.agent_steps+' steps / '+(r.research?.length||0)+' results')+detail({research:r.research,model_raw_output:r.model_raw_output})],['决策链',r=>detail({proposed:r.proposed_decision,risk:r.risk_decision,final:r.final_decision})],['执行/状态',r=>esc(r.status)+detail({execution:r.execution,error:r.error})],['后续观察',r=>detail(r.subsequent_observation)]]);document.getElementById('actions').innerHTML=table(a.items,[['时间',r=>new Date(r.created_at).toLocaleString()],['平台',r=>esc(r.platform)],['动作',r=>esc(r.action)],['结果',r=>detail(r)]]);document.getElementById('turns').innerHTML=table(t.items,[['时间',r=>new Date(r.created_at).toLocaleString()],['平台/Provider',r=>esc(r.platform+' / '+r.provider)],['状态',r=>esc(r.status)],['内容',r=>detail(r)]]);document.getElementById('steps').innerHTML=table(g.items,[['时间',r=>new Date(r.created_at).toLocaleString()],['平台/Provider',r=>esc(r.platform+' / '+r.provider)],['工具/状态',r=>esc((r.tool_name||'control')+' / '+r.status)],['内容',r=>detail(r)]]);document.getElementById('manifest').textContent=JSON.stringify(m,null,2);document.getElementById('stamp').textContent='更新 '+new Date().toLocaleTimeString()}catch(e){document.getElementById('stamp').textContent='错误: '+e}}
document.getElementById('platform').onchange=refreshAudit;Promise.all([refreshManager(),refreshAudit()]);setInterval(refreshAudit,REFRESH_MS);
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


def serve(config: Config) -> None:
    management = PluginManagementService(config)
    data = AuditData(config, management)
    csrf_token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            return

        def send_json(self, value: Any, status: int = 200) -> None:
            body = json.dumps(value, ensure_ascii=False, indent=2, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def request_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 1_000_000:
                raise ValueError("Request body size is invalid")
            value = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(value, dict):
                raise ValueError("Request body must be a JSON object")
            return value

        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            query = urllib.parse.parse_qs(parsed.query)
            try:
                if parsed.path == "/":
                    body = HTML.replace("REFRESH_MS", str(config.dashboard_refresh_seconds * 1000)).replace("CSRF_TOKEN", csrf_token).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(body)
                elif parsed.path in {"/api/summary", "/api/report"}:
                    self.send_json(data.summary())
                elif parsed.path == "/api/manifest":
                    self.send_json(data.manifest())
                elif parsed.path == "/api/plugins/manage":
                    self.send_json(management.manifest())
                elif parsed.path == "/api/decisions":
                    limit = max(1, min(200, int(query.get("limit", ["50"])[0])))
                    offset = max(0, int(query.get("offset", ["0"])[0]))
                    self.send_json(
                        data.decisions(
                            limit,
                            offset,
                            query.get("platform", [""])[0],
                            query.get("provider", [""])[0],
                            query.get("status", [""])[0],
                            query.get("action", [""])[0],
                        )
                    )
                elif parsed.path == "/api/records":
                    limit = max(1, min(200, int(query.get("limit", ["50"])[0])))
                    offset = max(0, int(query.get("offset", ["0"])[0]))
                    platform = query.get("platform", [""])[0]
                    self.send_json(data.records(query.get("kind", ["turns"])[0], limit, offset, platform))
                else:
                    self.send_json({"error": "not found"}, 404)
            except Exception as error:
                self.send_json({"error": str(error)}, 500)

        def do_POST(self) -> None:
            if not secrets.compare_digest(
                self.headers.get("X-Plugin-Management-Token", ""), csrf_token
            ):
                self.send_json({"error": "invalid management token"}, 403)
                return
            try:
                payload = self.request_json()
                if self.path == "/api/plugins/config":
                    result = management.save_plugin_configuration(
                        str(payload.get("kind", "")),
                        str(payload.get("name", "")),
                        payload.get("values", {}),
                    )
                elif self.path == "/api/plugins/selection":
                    result = management.save_enabled(payload)
                elif self.path == "/api/plugins/refresh":
                    result = management.refresh()
                else:
                    self.send_json({"error": "not found"}, 404)
                    return
                self.send_json(result)
            except (ValueError, KeyError, json.JSONDecodeError) as error:
                self.send_json({"error": str(error)}, 400)
            except Exception as error:
                self.send_json({"error": str(error)}, 500)

    server = ThreadingHTTPServer((config.dashboard_host, config.dashboard_port), Handler)
    print(f"Audit and plugin dashboard: http://{config.dashboard_host}:{config.dashboard_port}")
    try:
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            return
    finally:
        management.shutdown()
        server.server_close()
