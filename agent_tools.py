"""运维值班台 · Agent 工具层（薄包装）。

把 ops_center.py 里已经写好的纯函数，注册成 Agent 可调用的 JSON Schema 工具。
本层只做三件事：声明工具名/描述/参数 schema、调用现有函数、裁剪结果避免塞爆上下文。
不重写任何规则逻辑，所有数字都来自 ops_center 的返回值。

安全边界：本表只注册只读查询工具；写动作（确认/导出）不在此表，
由 agent_loop 转人工确认、走 ops_center 现有 /api/ack 逻辑。
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import json  # noqa: E402  (仅 __main__ 演示用)

# 允许 `python3 agent_tools.py` 与作为模块被 import 两种方式运行
sys.path.insert(0, str(Path(__file__).resolve().parent))

import ops_center as oc  # noqa: E402

CST = timezone(timedelta(hours=8))


# ---------------------------------------------------------------------------
# 结果裁剪：默认只返回摘要 + top N，明细走 get_finding_detail 按需取
# ---------------------------------------------------------------------------

def _finding_summary(finding: dict) -> dict:
    """findings 摘要：去掉完整 evidence，只留 id/级别/标题/建议/负责人。"""
    return {
        "id": finding.get("id"),
        "severity": finding.get("severity"),
        "source": finding.get("source"),
        "title": finding.get("title"),
        "detail": finding.get("detail"),
        "suggestion": finding.get("suggestion"),
        "owner": finding.get("owner", ""),
    }


def _asset_summary(asset: dict) -> dict:
    """资产摘要：去掉冗长字段，保留搜索/台账关键列。"""
    return {
        "id": asset.get("id"),
        "name": asset.get("name"),
        "type": asset.get("type"),
        "ip": asset.get("ip"),
        "host": asset.get("host"),
        "owner": asset.get("owner", ""),
        "vendor": asset.get("vendor", ""),
        "env": asset.get("env", ""),
        "criticality": asset.get("criticality", ""),
        "expires": asset.get("expires", ""),
    }


def _alert_summary(alert: dict) -> dict:
    """合并后告警摘要：保留合并语义（次数/首末时间/最高级别），不暴露原始重复条目。"""
    return {
        "id": alert.get("id"),
        "host": alert.get("host"),
        "service": alert.get("service"),
        "title": alert.get("title"),
        "severity": alert.get("severity"),
        "count": alert.get("count", 1),
        "first_seen": alert.get("first_seen"),
        "last_seen": alert.get("last_seen"),
        "message": (alert.get("message") or "")[:120],
    }


# ---------------------------------------------------------------------------
# 工具实现：每个函数签名统一为 tool(state_snapshot, args) -> dict
# 所有工具读同一份本轮快照 snapshot，保证对话中途数据不漂移。
# ---------------------------------------------------------------------------

def tool_get_overview(snapshot: dict, args: dict) -> dict:
    """本轮快照总览，作为事实基线；不返回全量列表。"""
    counts = snapshot["counts"]
    return {
        "generated_at": snapshot["generated_at"],
        "source": snapshot["source"],
        "assets_total": snapshot["assets"]["total"],
        "assets_expiring_30d": snapshot["assets"]["expiring"],
        "alerts_raw": snapshot["alerts"]["raw"],
        "alerts_merged": snapshot["alerts"]["merged"],
        "findings": counts,
        "backups": snapshot["backups"],
    }


def tool_list_findings(snapshot: dict, args: dict) -> dict:
    """『本班必须处理什么』主入口；可按级别/来源过滤，默认阻断优先。"""
    severity = (args.get("severity") or "").strip().lower()
    source = (args.get("source") or "").strip().lower()
    limit = int(args.get("limit") or 10)
    items = snapshot["findings"]
    if severity in oc.SEVERITY_RANK:
        items = [f for f in items if f["severity"] == severity]
    if source:
        items = [f for f in items if f["source"] == source]
    total = len(items)
    return {
        "total": total,
        "shown": min(total, limit),
        "findings": [_finding_summary(f) for f in items[:limit]],
    }


def tool_get_finding_detail(snapshot: dict, args: dict) -> dict:
    """回答『为什么』时必须引用 evidence，不许编造。"""
    fid = (args.get("id") or "").strip()
    for f in snapshot["findings"]:
        if f.get("id") == fid:
            return {
                "id": f["id"],
                "severity": f["severity"],
                "source": f["source"],
                "title": f["title"],
                "detail": f["detail"],
                "suggestion": f["suggestion"],
                "owner": f.get("owner", ""),
                "evidence": f.get("evidence", []),
            }
    return {"error": f"未找到发现 {fid}", "known_ids": [x.get("id") for x in snapshot["findings"]]}


def tool_list_alerts(snapshot: dict, args: dict) -> dict:
    """合并后告警列表；可按级别/主机过滤。"""
    severity = (args.get("severity") or "").strip().lower()
    host = (args.get("host") or "").strip().casefold()
    limit = int(args.get("limit") or 10)
    items = snapshot["alerts_list"]
    if severity in oc.ALERT_RANK:
        items = [a for a in items if a.get("severity") == severity]
    if host:
        items = [a for a in items if host in oc._text(a.get("host")).casefold()]
    total = len(items)
    return {"total": total, "alerts": [_alert_summary(a) for a in items[:limit]]}


def tool_check_backup_status(snapshot: dict, args: dict) -> dict:
    """『哪些备份超时』直接命中；每项给时效小时数与阈值。"""
    stats = snapshot["backups"]
    now = datetime.now(CST)
    rows = []
    assets, _ = oc.load_data()
    for asset in assets:
        if oc._text(asset.get("type")).lower() != "backup":
            continue
        ref = oc._asset_ref(asset)
        last = oc.parse_dt(asset.get("last_success"))
        warn, block = oc._backup_limits(asset)
        if last is None:
            rows.append({"backup": ref, "host": asset.get("host", ""), "status": "blocker",
                         "hours_since_success": None, "warn_hours": warn, "block_hours": block,
                         "last_success": asset.get("last_success", ""), "note": "无有效成功记录"})
            continue
        age = (now - last).total_seconds() / 3600
        status = "blocker" if age > block else ("warning" if age > warn else "ok")
        rows.append({"backup": ref, "host": asset.get("host", ""), "status": status,
                     "hours_since_success": round(age, 1), "warn_hours": warn, "block_hours": block,
                     "last_success": last.isoformat(), "schedule": asset.get("schedule", "")})
    order = {"blocker": 0, "warning": 1, "ok": 2}
    rows.sort(key=lambda r: order.get(r["status"], 9))
    return {"stats": stats, "backups": rows}


def tool_search_assets(snapshot: dict, args: dict) -> dict:
    """复用现有 asset_search，行为与 UI 搜索框一致。"""
    q = (args.get("q") or "").strip()
    limit = int(args.get("limit") or 10)
    matched = oc.asset_search(snapshot["assets_list"], q)
    total = len(matched)
    return {"query": q, "total": total, "assets": [_asset_summary(a) for a in matched[:limit]]}


def tool_check_data_quality(snapshot: dict, args: dict) -> dict:
    """结构化校验结果（缺失表头/坏行/行号）；依赖 validate_data 的结构化版本。"""
    return {"checks": oc.validate_data_structured()}


def tool_search_ip_ledger(snapshot: dict, args: dict) -> dict:
    """搜索单位设备 IP 台账（真实数据，仅本地）。按 IP/人员/房间/型号/MAC/备注匹配。

    这是工作台第一个非值班场景的工具：台账稀疏字段如实返回，缺就空，不编造。
    """
    import ip_ledger as il
    q = (args.get("q") or "").strip()
    limit = int(args.get("limit") or 20)
    res = il.search_records(q, limit=limit)
    if not res.get("ok"):
        return {"error": res.get("error", "台账读取失败"), "query": q}
    return {
        "query": q,
        "sheet": res["sheet"],
        "matched": res["matched"],
        "total_in_sheet": res["total"],
        "records": [
            {k: rec.get(k, "") for k in
             ("ip", "person", "room", "model", "mac", "asset_no", "host_no", "disk_no", "remark")}
            for rec in res["records"]
        ],
    }


def tool_ip_ledger_stats(snapshot: dict, args: dict) -> dict:
    """设备 IP 台账概况：总记录数、各字段填充率、可用历史快照。"""
    import ip_ledger as il
    data = il.load_records()
    if not data.get("ok"):
        return {"error": data.get("error", "台账读取失败")}
    sheets = il.load_sheets().get("sheets", [])
    return {
        "sheet": data["sheet"],
        "total": data["total"],
        "filled": data["filled"],
        "columns": data["columns"],
        "history_snapshots": [s["name"] for s in sheets],
        "history_count": len(sheets),
    }


def tool_get_handover(snapshot: dict, args: dict) -> dict:
    """交接班 Markdown 全文；Agent 可引用/摘要，但不重写数字。"""
    return {"handover_markdown": snapshot["handover"]}


def tool_get_recent_changes(snapshot: dict, args: dict) -> dict:
    """近期变更资产（默认 24h）；复用 handover 内已有逻辑。"""
    hours = float(args.get("hours") or 24)
    now = oc.parse_dt(snapshot["generated_at"])
    changes = []
    for asset in snapshot["assets_list"]:
        lc = asset.get("last_change")
        dt = oc.parse_dt(lc)
        if dt and (now - dt).total_seconds() <= hours * 3600:
            changes.append({"id": asset.get("id"), "name": asset.get("name"),
                            "host": asset.get("host", ""), "last_change": lc})
    return {"hours": hours, "total": len(changes), "changes": changes}


# ---------------------------------------------------------------------------
# 工具注册表：名称 -> {description, schema, fn}
# Agent 看到的就是这个表；新增工具只需在此登记。
# ---------------------------------------------------------------------------

TOOLS = {
    "get_overview": {
        "description": "获取本轮值班快照总览：资产/告警/备份/发现计数，作为回答的事实基线。",
        "schema": {"type": "object", "properties": {}, "required": []},
        "fn": tool_get_overview,
    },
    "list_findings": {
        "description": "列出值班发现（可按 severity=blocker/warning/info、source=alert/backup/asset 过滤），『本班必须处理什么』的主入口。",
        "schema": {"type": "object", "properties": {
            "severity": {"type": "string", "enum": ["blocker", "warning", "info"]},
            "source": {"type": "string", "enum": ["alert", "backup", "asset"]},
            "limit": {"type": "integer", "minimum": 1, "maximum": 30},
        }, "required": []},
        "fn": tool_list_findings,
    },
    "get_finding_detail": {
        "description": "按 id 取单个发现的完整明细与证据（evidence），回答『为什么』时必须引用证据。",
        "schema": {"type": "object", "properties": {
            "id": {"type": "string", "description": "发现 id，如 F1"},
        }, "required": ["id"]},
        "fn": tool_get_finding_detail,
    },
    "list_alerts": {
        "description": "列出合并后的告警（可按 severity/host 过滤），保留合并次数与首末时间。",
        "schema": {"type": "object", "properties": {
            "severity": {"type": "string", "enum": ["critical", "warning", "info"]},
            "host": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 30},
        }, "required": []},
        "fn": tool_list_alerts,
    },
    "check_backup_status": {
        "description": "检查所有备份任务的时效：每项给距最近成功的小时数、告警/阻断阈值与状态。",
        "schema": {"type": "object", "properties": {}, "required": []},
        "fn": tool_check_backup_status,
    },
    "search_assets": {
        "description": "按名称/IP/负责人/厂商/主机/类型搜索资产台账。",
        "schema": {"type": "object", "properties": {
            "q": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 30},
        }, "required": ["q"]},
        "fn": tool_search_assets,
    },
    "check_data_quality": {
        "description": "校验 inbox CSV 的表头、枚举值与时间格式，返回结构化问题清单（含行号）。",
        "schema": {"type": "object", "properties": {}, "required": []},
        "fn": tool_check_data_quality,
    },
    "get_handover": {
        "description": "获取交接班 Markdown 全文（总体/必须处理/近期变更/待观察/备注）。",
        "schema": {"type": "object", "properties": {}, "required": []},
        "fn": tool_get_handover,
    },
    "get_recent_changes": {
        "description": "列出近 N 小时（默认 24）内登记变更的资产。",
        "schema": {"type": "object", "properties": {
            "hours": {"type": "number", "minimum": 1, "maximum": 168},
        }, "required": []},
        "fn": tool_get_recent_changes,
    },
    "search_ip_ledger": {
        "description": "搜索单位设备 IP 台账（本地真实数据）。按 IP/人员/房间/设备型号/MAC/备注匹配。用于『某个 IP 是谁/在哪个房间』『查某人的设备』『某房间的机器』。",
        "schema": {"type": "object", "properties": {
            "q": {"type": "string", "description": "关键词：IP 片段、人员姓名、房间号、型号、MAC 等"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
        }, "required": ["q"]},
        "fn": tool_search_ip_ledger,
    },
    "ip_ledger_stats": {
        "description": "设备 IP 台账概况：总记录数、各字段填充率、可用历史快照列表。",
        "schema": {"type": "object", "properties": {}, "required": []},
        "fn": tool_ip_ledger_stats,
    },
}

# 越权动作：明确拒绝，不注册成可调用工具
FORBIDDEN_INTENTS = {
    "restart", "reboot", "shutdown", "执行命令", "重启", "关机", "登录设备",
    "改配置", "修改配置", "删库", "drop",
}


def run_tool(name: str, snapshot: dict, args: dict) -> dict:
    """按名执行工具；未知工具返回错误而非抛异常。"""
    spec = TOOLS.get(name)
    if spec is None:
        return {"error": f"未知工具 {name}", "available": sorted(TOOLS)}
    try:
        return spec["fn"](snapshot, args or {})
    except Exception as exc:  # 工具内部出错也要返回结构化信息
        return {"error": f"工具 {name} 执行失败：{exc}"}


if __name__ == "__main__":
    snap = oc.build_state(datetime.now(CST))
    print("已注册工具：", ", ".join(sorted(TOOLS)))
    print(json.dumps(run_tool("get_overview", snap, {}), ensure_ascii=False, indent=2))
