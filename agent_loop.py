"""运维值班台 · Agent 调度层（安全降级版）。

核心循环：用户消息 → 取本轮快照 → 路由/LLM 决定调哪些工具 → 执行 → 收集轨迹与证据 → 组织回答。
- 安全降级模式（默认，无 API key）：本地意图路由把自然语言映射到工具调用序列，
  回答用模板化组织，所有数字/证据直接来自工具返回值。
- 可插拔 LLM：若设置 OPS_LLM_BASE_URL，则把工具轨迹交给 OpenAI 兼容端点做最终自然语言组织；
  未配置则用本地组织，功能不中断（可降级）。
- 每一步写 data/agent_audit.jsonl。

铁律（与 docs/agent工作台方案.md 一致）：所有数字/状态来自工具；每条结论附 finding id 或证据；
越权动作（重启/执行命令）一律拒绝并说明只读边界；写动作（确认）转人工。
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys_path = str(Path(__file__).resolve().parent)
import sys  # noqa: E402
if sys_path not in sys.path:
    sys.path.insert(0, sys_path)

import ops_center as oc  # noqa: E402
import agent_tools as at  # noqa: E402

CST = timezone(timedelta(hours=8))
MAX_STEPS = 8
AUDIT_PATH = oc.BASE_DIR / "data" / "agent_audit.jsonl"

SEV_CN = {"blocker": "阻断", "warning": "警告", "info": "提示"}
SRC_CN = {"alert": "告警", "backup": "备份", "asset": "资产"}


# ---------------------------------------------------------------------------
# 工具轨迹与结果
# ---------------------------------------------------------------------------

@dataclass
class Trace:
    tool: str
    args: dict
    summary: str
    duration_ms: int


@dataclass
class TurnResult:
    answer: str
    tool_trace: list[Trace] = field(default_factory=list)
    findings_referenced: list[str] = field(default_factory=list)
    forbidden: bool = False


# ---------------------------------------------------------------------------
# 审计
# ---------------------------------------------------------------------------

def _audit(record: dict) -> None:
    try:
        AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with AUDIT_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 意图路由（安全降级版的“大脑”）：自然语言 -> 工具调用计划
# 顺序敏感：越具体的意图越先匹配。
# ---------------------------------------------------------------------------

_FORBIDDEN_PAT = re.compile(
    r"重启|重启服务|重启一下|reboot|restart|关机|shutdown|执行命令|运行命令|登录设备|"
    r"改配置|修改配置|删除|删库|drop\s+|rm\s+-rf|批量确认|全部确认|确认所有",
    re.IGNORECASE,
)


def _is_forbidden(message: str) -> bool:
    return bool(_FORBIDDEN_PAT.search(message))


def _route(message: str) -> list[tuple[str, dict]]:
    """返回该消息应调用的 [(工具名, 参数)] 计划；miss 时返回空列表由上层给引导。"""
    m = message
    plan: list[tuple[str, dict]] = []
    host_match = re.search(r"\b(srv[-\w]*|host[-\w]*|\d{1,3}(?:\.\d{1,3}){3})\b", m)
    fid = re.search(r"\b(F\d+)\b", m)

    # 交接班
    if "交接" in m or "交班" in m or ("生成" in m and "报告" in m):
        plan.append(("get_handover", {}))
        return plan

    # 备份时效
    if "备份" in m and any(k in m for k in ("超时", "时效", "异常", "状态", "哪些", "超", "成功")):
        plan.append(("check_backup_status", {}))
        return plan

    # 为什么（追问证据）—— 提到最前，避免被资产/概览吞掉
    if fid and any(k in m for k in ("为什么", "为啥", "证据", "原因", "阻断", "警告", "详情")):
        plan.append(("get_finding_detail", {"id": fid.group(1)}))
        return plan

    # 资产查询：主机名 / 明确资产词 / 有哪些。不含泛化的“什么”。
    asset_kw = any(k in m for k in ("资产", "台账", "有哪些", "上有什么", "搜索", "查找"))
    if host_match or asset_kw:
        q = host_match.group(1) if host_match else re.sub(r"[？?。！!，,]", "", m).strip()
        if q:
            plan.append(("search_assets", {"q": q}))
            return plan

    # 近期变更 —— 提到证书/概览之前，避免“最近有什么变更”被“什么”吞掉
    if "变更" in m or "改过" in m:
        plan.append(("get_recent_changes", {}))
        return plan

    # 证书/授权到期（取全部发现再按标题过滤，避免漏掉被跨源合并的项）
    if any(k in m for k in ("证书", "授权", "到期", "过期")):
        plan.append(("get_overview", {}))
        plan.append(("list_findings", {"limit": 30}))
        return plan

    # 本班必须处理 / 概览
    if any(k in m for k in ("必须处理", "要处理", "阻断", "blocker", "概览", "概况", "总体", "今天", "什么")):
        plan.append(("get_overview", {}))
        plan.append(("list_findings", {"severity": "blocker"}))
        return plan

    # 数据质量
    if any(k in m for k in ("数据质量", "校验", "表头", "check")):
        plan.append(("check_data_quality", {}))
        return plan

    # 告警
    if "告警" in m:
        plan.append(("list_alerts", {}))
        return plan

    return plan


# ---------------------------------------------------------------------------
# 回答组织（本地模板版）：结论先行、阻断优先、逐条带 id/证据
# ---------------------------------------------------------------------------

def _compose(message: str, plan: list[tuple[str, dict]], results: dict, snapshot: dict) -> str:
    lines: list[str] = []

    # 1) 工具是否报错/空
    overview = results.get("get_overview")

    # 2) 交接班
    if "get_handover" in results:
        md = results["get_handover"].get("handover_markdown", "")
        lines.append("已生成本班交接班，数字与快照一致：\n")
        lines.append(md)
        return "\n".join(lines)

    # 3) 备份
    if "check_backup_status" in results:
        data = results["check_backup_status"]
        stats = data["stats"]
        lines.append(f"备份时效检查：共 {stats['total']} 个备份任务，"
                     f"正常 {stats['ok']}、警告 {stats['warning']}、阻断 {stats['blocker']}。\n")
        for b in data["backups"]:
            if b["status"] == "ok":
                continue
            hours = b.get("hours_since_success")
            hs = "无有效成功记录" if hours is None else f"距最近成功 {hours} 小时（阻断阈值 {b['block_hours']}h）"
            lines.append(f"- 【{SEV_CN[b['status']]}】{b['backup']}（主机 {b['host'] or '未填写'}）：{hs}")
        return "\n".join(lines)

    # 4) 证书到期 / 资产类发现（只显示标题含证书/授权/到期的项）
    if "list_findings" in results and any(k in message for k in ("证书", "授权", "到期", "过期")):
        exp = overview.get("assets_expiring_30d") if overview else None
        head = f"30 天内到期资产 {exp} 项。" if exp is not None else ""
        items = [f for f in results["list_findings"].get("findings", [])
                 if any(k in (f.get("title") or "") for k in ("证书", "授权", "到期", "过期"))]
        if not items:
            return f"{head}数据里没有即将到期的证书或授权（30 天内）。可以再确认台账里是否已登记到期日。"
        lines.append(f"{head}与证书/授权相关的发现 {len(items)} 条：\n")
        for f in items:
            lines.append(f"- [{f['id']}][{SEV_CN[f['severity']]}] {f['title']}：{f['detail']}")
            lines.append(f"    建议：{f['suggestion']}{' · 负责人 ' + f['owner'] if f['owner'] else ''}")
        return "\n".join(lines)

    # 5) 资产搜索
    if "search_assets" in results:
        data = results["search_assets"]
        if data["total"] == 0:
            return f"资产台账里没有匹配“{data['query']}”的记录。可以试试主机名、IP 或负责人。"
        lines.append(f"匹配“{data['query']}”的资产 {data['total']} 项（显示前 {len(data['assets'])}）：\n")
        for a in data["assets"]:
            lines.append(f"- {a['name']}（{a['type']}）· {a['ip'] or a['host'] or '—'}"
                         f" · 负责人 {a['owner'] or '—'} · 环境 {a['env'] or '—'}")
        # 单机多关键服务提示
        multi = [f for f in snapshot["findings"] if "单机承载多个关键服务" in f["title"]
                 and any(a.get("host") and a["host"] in f["detail"] for a in data["assets"])]
        for f in multi[:2]:
            lines.append(f"\n提示 [{f['id']}]：{f['detail']} → {f['suggestion']}")
        return "\n".join(lines)

    # 6) 单条发现证据
    if "get_finding_detail" in results:
        f = results["get_finding_detail"]
        if f.get("error"):
            return f.get("error", "")
        lines.append(f"【{f['id']}·{SEV_CN[f['severity']]}】{f['title']}\n")
        lines.append(f"{f['detail']}\n")
        lines.append(f"建议：{f['suggestion']}{' · 负责人 ' + f['owner'] if f['owner'] else ''}\n")
        lines.append("证据：")
        for e in f.get("evidence", []):
            lines.append(f"- {e.get('asset')} · {e.get('field')} = {e.get('value')}")
        return "\n".join(lines)

    # 7) 本班必须处理 / 概览 + 阻断项
    if overview and "list_findings" in results:
        counts = overview["findings"]
        lines.append(f"本班概况：资产 {overview['assets_total']} 项（30 天内到期 {overview['assets_expiring_30d']}），"
                     f"原始告警 {overview['alerts_raw']} 条合并为 {overview['alerts_merged']} 条；"
                     f"发现 阻断 {counts['blocker']}、警告 {counts['warning']}、提示 {counts['info']}。")
        bstat = overview["backups"]
        lines.append(f"备份：正常 {bstat['ok']}、警告 {bstat['warning']}、阻断 {bstat['blocker']}。\n")
        data = results["list_findings"]
        if data["total"] == 0:
            lines.append("没有阻断项。")
        else:
            lines.append(f"必须处理（阻断 {data['total']} 条，显示前 {len(data['findings'])}）：\n")
            for f in data["findings"]:
                lines.append(f"- [{f['id']}][{SRC_CN.get(f['source'], f['source'])}] {f['title']}")
                lines.append(f"    {f['detail']}")
                lines.append(f"    建议：{f['suggestion']}{' · 负责人 ' + f['owner'] if f['owner'] else ''}")
        return "\n".join(lines)

    # 8) 数据质量
    if "check_data_quality" in results:
        checks = results["check_data_quality"].get("checks", [])
        errors = [c for c in checks if c["level"] == "error"]
        warns = [c for c in checks if c["level"] == "warning"]
        lines.append(f"数据校验：错误 {len(errors)}、警告 {len(warns)}。")
        for c in checks:
            if c["level"] == "ok":
                lines.append(f"- {c['file']}：{c['message']}")
            elif c["level"] != "info":
                loc = f"{c['file']} 第 {c['row']} 行" if c["row"] else c["file"]
                lines.append(f"- 【{c['level']}】{loc} {c['field']}: {c['message']}")
        return "\n".join(lines)

    # 9) 告警
    if "list_alerts" in results:
        data = results["list_alerts"]
        lines.append(f"合并后告警 {data['total']} 条：\n")
        for a in data["alerts"]:
            lines.append(f"- 【{a['severity']}】{a['host']}/{a['service']} {a['title']}"
                         f"（{a['count']} 次，最近 {a['last_seen']}）")
        return "\n".join(lines)

    # 10) 近期变更
    if "get_recent_changes" in results:
        data = results["get_recent_changes"]
        if data["total"] == 0:
            return f"近 {data['hours']} 小时没有登记变更的资产。"
        lines.append(f"近 {data['hours']} 小时变更资产 {data['total']} 项：")
        for c in data["changes"]:
            lines.append(f"- {c['name']}（{c['host'] or '—'}）：{c['last_change']}")
        return "\n".join(lines)

    return ""  # 未匹配到任何工具结果


_LLM_GUIDE = (
    "你是信息中心值班助手。只根据下面提供的【工具返回结果】回答，不要编造数字；"
    "每条结论附 finding id 或证据字段；无数据就明说；不给处置命令只给建议。中文，先结论后依据。\n\n"
)


def _llm_compose(message: str, plan, results: dict, snapshot: dict) -> str | None:
    """可选：把工具结果交给 OpenAI 兼容端点做自然语言组织；未配置或失败返回 None（降级）。"""
    base = os.environ.get("OPS_LLM_BASE_URL")
    if not base:
        return None
    try:
        import urllib.request
        tool_dump = json.dumps(results, ensure_ascii=False)[:6000]
        payload = {
            "model": os.environ.get("OPS_LLM_MODEL", "gpt-4o-mini"),
            "messages": [
                {"role": "system", "content": _LLM_GUIDE},
                {"role": "user", "content": f"用户问：{message}\n\n【工具返回结果】\n{tool_dump}"},
            ],
            "temperature": 0.2,
        }
        req = urllib.request.Request(
            base.rstrip("/") + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        key = os.environ.get("OPS_LLM_API_KEY")
        if key:
            req.add_header("Authorization", f"Bearer {key}")
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"]
    except Exception:
        return None  # 静默降级到本地组织


# ---------------------------------------------------------------------------
# 对外主入口：handle(message) -> TurnResult
# ---------------------------------------------------------------------------

def handle(message: str, session_id: str | None = None) -> TurnResult:
    session_id = session_id or uuid.uuid4().hex[:12]
    message = (message or "").strip()
    t0 = time.time()

    # 越权拒绝：不调任何工具，直接说明只读边界
    if _is_forbidden(message):
        ans = ("这个请求超出了值班助手的只读边界——我不会登录设备、执行命令、改配置或重启服务。\n"
               "我可以：查告警、查备份时效、查资产与证书到期、解释某条发现为什么是阻断、生成交接班。\n"
               "如果是处置建议，告诉我具体现象，我按 runbook 给你建议和负责人，但执行仍由人工完成。")
        tr = Trace(tool="(拒绝)", args={}, summary="越权请求，已拒绝", duration_ms=0)
        _audit({"time": datetime.now(CST).isoformat(), "session": session_id, "message": message,
                "trace": [], "answer_head": ans[:120], "forbidden": True, "duration_ms": 0})
        return TurnResult(answer=ans, tool_trace=[tr], forbidden=True)

    # 取本轮事实基线快照
    now = datetime.now(CST)
    snapshot = oc.build_state(now)

    # 路由出工具计划
    plan = _route(message)
    results: dict = {}
    traces: list[Trace] = []
    for name, args in plan[:MAX_STEPS]:
        ts = time.time()
        res = at.run_tool(name, snapshot, args)
        results[name] = res
        # 生成简短摘要供轨迹条展示
        summary = _summarize(name, res)
        traces.append(Trace(tool=name, args=args, summary=summary,
                            duration_ms=int((time.time() - ts) * 1000)))

    # 组织回答：优先 LLM（若配置），否则本地模板
    answer = _llm_compose(message, plan, results, snapshot)
    if answer is None:
        answer = _compose(message, plan, results, snapshot)
        if not answer and not plan:
            answer = _fallback_guide(snapshot)
        elif not answer:
            answer = "工具已执行，但没有组织出回答；请换个说法再试。"

    findings_ref = [f["id"] for f in snapshot["findings"] if f["id"] in answer]
    duration_ms = int((time.time() - t0) * 1000)
    _audit({
        "time": datetime.now(CST).isoformat(), "session": session_id, "message": message,
        "trace": [{"tool": t.tool, "args": t.args, "summary": t.summary, "ms": t.duration_ms} for t in traces],
        "answer_head": answer[:160], "findings_referenced": findings_ref, "duration_ms": duration_ms,
    })
    return TurnResult(answer=answer, tool_trace=traces, findings_referenced=findings_ref)


def _summarize(name: str, res: dict) -> str:
    if res.get("error"):
        return res["error"]
    if name == "get_overview":
        c = res["findings"]
        return f"阻断 {c['blocker']} / 警告 {c['warning']} / 提示 {c['info']}"
    if name == "list_findings":
        return f"{res['total']} 条发现"
    if name == "get_finding_detail":
        return f"{res.get('id', '')} 证据 {len(res.get('evidence', []))} 项"
    if name == "list_alerts":
        return f"{res['total']} 条告警"
    if name == "check_backup_status":
        s = res["stats"]
        return f"备份 {s['total']}：阻断 {s['blocker']} / 警告 {s['warning']}"
    if name == "search_assets":
        return f"{res['total']} 项资产"
    if name == "check_data_quality":
        errs = sum(1 for c in res.get("checks", []) if c["level"] == "error")
        return f"错误 {errs}"
    if name == "get_handover":
        return f"{len(res.get('handover_markdown', ''))} 字"
    if name == "get_recent_changes":
        return f"{res['total']} 项变更"
    return "完成"


def _fallback_guide(snapshot: dict) -> str:
    counts = snapshot["counts"]
    return (
        "我可以帮你查值班数据。试试这些：\n"
        "- 本班必须处理什么？\n- 哪些备份超时了？\n- 证书快到期了吗？\n"
        "- srv-app-01 上有什么？\n- 为什么 F1 是阻断？\n- 生成交接班\n\n"
        f"当前：阻断 {counts['blocker']}、警告 {counts['warning']}、提示 {counts['info']}。"
    )


if __name__ == "__main__":
    demo = [
        "本班必须处理什么？",
        "哪些备份超时了？",
        "为什么 F1 是阻断？",
        "把数据库重启一下",
    ]
    for q in demo:
        r = handle(q)
        print("=" * 60)
        print("Q:", q)
        print("轨迹:", [f"{t.tool}({t.summary})" for t in r.tool_trace])
        print(r.answer)
