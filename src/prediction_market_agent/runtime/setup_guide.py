"""Turn structural readiness and plugin-owned requirements into actionable UI data."""
from __future__ import annotations


def setup_guide(runtime, manifest, *, automatic_start):
    plugins=manifest["plugins"]
    steps=[]
    def add(key,title,description,ready,actions,issues=(),required=True):
        steps.append({"id":key,"title":title,"description":description,"ready":ready,
                      "actions":actions,"issues":list(issues),"required":required})
    def plugin_action(kind,p):
        return {"kind":kind,"name":p["name"],"label":"打开 "+p["name"]}
    providers=[p for p in plugins.get("decision_provider",[]) if p["enabled"]]
    provider_ready=any(p.get("readiness",{}).get("ready",False) for p in providers)
    add("models","连接至少一种 AI 模型服务","Codex、Claude 或兼容 API 可任选一种；并非每种都要配置。",provider_ready,
        [plugin_action("decision_provider",p) for p in providers if not p.get("readiness",{}).get("ready")],
        [p["name"]+": "+reason for p in providers for reason in p.get("readiness",{}).get("reasons",[])])
    platforms=[p for p in plugins.get("api",[]) if p["enabled"]]
    platform_states=[]
    for p in platforms:
        actual=runtime.get("platforms",{}).get(p["name"],{})
        ready=actual.get("ready",p.get("readiness",{}).get("ready",False))
        platform_states.append((p,actual,ready))
    any_platform_ready=any(ready for _p,_actual,ready in platform_states)
    add("platforms","启动至少一个交易平台","平台插件自行判断能否启动；通用框架只读取其状态，不检查私有字段。",any_platform_ready,
        [plugin_action("api",p) for p,_actual,ready in platform_states if not ready],
        [p["name"]+": "+reason for p,actual,ready in platform_states if not ready for reason in actual.get("startup_reasons",p.get("readiness",{}).get("reasons",[]))])
    for kind,title in [("decision_strategy","决策策略"),("market_discovery","标的发现策略"),("research_tool","信息与研究"),("agent_policy","Agent 行为"),("risk","业务风控"),("hook","流程扩展")]:
        selected=[p for p in plugins.get(kind,[]) if p["enabled"]]
        for p in selected:
            state=runtime.get("global_plugins",{}).get(kind+":"+p["name"],p.get("readiness",{}))
            description="这是可选增强；不可用时不会阻止平台与 AI 主链启动。可修复插件，或在插件中心关闭。"
            add(kind+":"+p["name"],title+" · "+p["name"],description,bool(state.get("ready")),[plugin_action(kind,p)],state.get("reasons",[]),required=False)
    paused=runtime.get("robot_paused",manifest.get("robot_paused",False))
    running=bool(runtime.get("running"))
    actual_platforms=runtime.get("platforms",{})
    partial=running and any(not p.get("running") for p in actual_platforms.values())
    state="management_only" if not automatic_start else "partial" if partial else "running" if running else "paused" if paused else "not_running"
    return {"state":state,"steps":steps,"remaining":sum(s["required"] and not s["ready"] for s in steps),
            "automatic_start":automatic_start,"runtime_reasons":runtime.get("global_reasons",[]) if automatic_start else []}
