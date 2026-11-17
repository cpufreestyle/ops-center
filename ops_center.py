"""运维值班台：告警合并、备份时效、资产台账。

演示数据全部为合成数据，不含任何真实内网资产。
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("OPS_DATA_DIR", str(BASE_DIR / "data" / "inbox")))
CST = timezone(timedelta(hours=8))
SEVERITY_RANK = {"blocker": 0, "warning": 1, "info": 2}
ALERT_RANK = {"critical": 3, "warning": 2, "info": 1}
ALERT_TO_FINDING = {"critical": "blocker", "warning": "warning", "info": "info"}

ASSET_FIELDS = [
    "id", "name", "type", "ip", "host", "port", "owner", "vendor", "env",
    "criticality", "expires", "last_change",
]
BACKUP_FIELDS = [
    "id", "name", "type", "host", "path", "schedule", "last_success",
    "target", "env", "owner", "criticality",
]
ALERT_FIELDS = ["id", "time", "host", "service", "severity", "title", "message"]


def parse_dt(value) -> datetime | None:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str) and value.strip():
        text = value.strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=CST)
    return dt.astimezone(CST)


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _read_csv(path: Path) -> list[dict]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = []
            for row in csv.DictReader(handle):
                cleaned = {
                    str(key).strip(): ("" if value is None else str(value).strip())
                    for key, value in row.items()
                    if key
                }
                if any(cleaned.values()):
                    rows.append(cleaned)
            return rows
    except (OSError, csv.Error):
        return []


def _merge_by_id(rows: list[dict]) -> list[dict]:
    merged: dict[str, dict] = {}
    for row in rows:
        key = _text(row.get("id") or row.get("name") or row.get("host"))
        merged[key] = row
    return list(merged.values())


def data_source() -> str:
    names = [name for name in ("assets.csv", "alerts.csv", "backups.csv") if (DATA_DIR / name).exists()]
    return "csv:" + ",".join(names) if names else "demo"


def load_data() -> tuple[list[dict], list[dict]]:
    assets_path = DATA_DIR / "assets.csv"
    alerts_path = DATA_DIR / "alerts.csv"
    backups_path = DATA_DIR / "backups.csv"
    if any(path.exists() for path in (assets_path, alerts_path, backups_path)):
        assets = _read_csv(assets_path) if assets_path.exists() else []
        backups = _read_csv(backups_path) if backups_path.exists() else []
        for backup in backups:
            backup.setdefault("type", "backup")
        alerts = _read_csv(alerts_path) if alerts_path.exists() else []
        for asset in assets:
            port = _text(asset.get("port"))
            if port.isdigit():
                asset["port"] = int(port)
        return _merge_by_id(assets + backups), alerts
    assets = load_json(BASE_DIR / "config" / "ops_assets.json", {}).get("assets", [])
    alerts = load_json(BASE_DIR / "data" / "ops_alerts.json", {}).get("alerts", [])
    return assets, alerts


def _text(value) -> str:
    return "" if value is None else str(value)


def _asset_ref(asset: dict) -> str:
    return _text(asset.get("id") or asset.get("name") or asset.get("host"))


def dedupe_alerts(alerts: list[dict]) -> list[dict]:
    groups: dict[tuple[str, str, str], dict] = {}
    for index, alert in enumerate(alerts):
        key = (_text(alert.get("host")), _text(alert.get("service")), _text(alert.get("title")))
        event_time = parse_dt(alert.get("time"))
        severity = _text(alert.get("severity")).lower()
        rank = ALERT_RANK.get(severity, 0)
        if key not in groups:
            groups[key] = {
                "latest": dict(alert),
                "latest_time": event_time,
                "latest_index": index,
                "first_time": event_time,
                "last_time": event_time,
                "count": 1,
                "severity": severity,
                "rank": rank,
            }
            continue
        group = groups[key]
        group["count"] += 1
        if event_time:
            if group["first_time"] is None or event_time < group["first_time"]:
                group["first_time"] = event_time
            if group["last_time"] is None or event_time > group["last_time"]:
                group["last_time"] = event_time
            if group["latest_time"] is None or event_time > group["latest_time"]:
                group["latest"] = dict(alert)
                group["latest_time"] = event_time
                group["latest_index"] = index
        if rank > group["rank"]:
            group["severity"] = severity
            group["rank"] = rank
    merged: list[dict] = []
    for group in groups.values():
        item = dict(group["latest"])
        item["count"] = group["count"]
        item["first_seen"] = group["first_time"].isoformat() if group["first_time"] else None
        item["last_seen"] = group["last_time"].isoformat() if group["last_time"] else None
        item["severity"] = group["severity"] or "info"
        merged.append(item)
    merged.sort(key=lambda item: parse_dt(item.get("last_seen")) or datetime.min.replace(tzinfo=CST), reverse=True)
    return merged


def _finding(
    severity: str,
    source: str,
    title: str,
    detail: str,
    suggestion: str,
    evidence: list[dict],
    owner: str = "",
) -> dict:
    return {
        "severity": severity,
        "source": source,
        "title": title,
        "detail": detail,
        "suggestion": suggestion,
        "evidence": evidence,
        "owner": owner,
    }


def _backup_limits(asset: dict) -> tuple[float, float]:
    schedule = _text(asset.get("schedule"))
    if "每周" in schedule or "周" in schedule:
        return 168.0, 240.0
    return 26.0, 48.0


def check_backups(assets: list[dict], now: datetime) -> tuple[list[dict], dict]:
    findings: list[dict] = []
    stats = {"total": 0, "ok": 0, "warning": 0, "blocker": 0}
    for asset in assets:
        if _text(asset.get("type")).lower() != "backup":
            continue
        stats["total"] += 1
        ref = _asset_ref(asset)
        last = parse_dt(asset.get("last_success"))
        if last is None:
            stats["blocker"] += 1
            findings.append(
                _finding(
                    "blocker",
                    "backup",
                    "备份无有效成功记录",
                    f"备份资产 {ref} 的 last_success 为空或无法解析。",
                    "立即核查备份任务状态，补做一次可验证的备份。",
                    [{"asset": ref, "field": "last_success", "value": _text(asset.get("last_success"))}],
                    _text(asset.get("owner")),
                )
            )
            continue
        age_hours = (now - last).total_seconds() / 3600
        warn, block = _backup_limits(asset)
        if age_hours > block:
            severity, title = "blocker", "备份超时超过阈值"
        elif age_hours > warn:
            severity, title = "warning", "备份时效接近阈值"
        else:
            stats["ok"] += 1
            continue
        stats[severity] += 1
        findings.append(
            _finding(
                severity,
                "backup",
                title,
                f"备份 {ref} 距最近成功备份 {age_hours:.1f} 小时（计划：{_text(asset.get('schedule')) or '未填写'}）。",
                "检查备份调度与执行日志；补做备份并抽验恢复。",
                [{"asset": ref, "field": "last_success", "value": last.isoformat()}],
                _text(asset.get("owner")),
            )
        )
    return findings, stats


def check_assets(assets: list[dict], now: datetime) -> tuple[list[dict], dict]:
    findings: list[dict] = []
    backup_hosts = {
        _text(asset.get("host")).casefold()
        for asset in assets
        if _text(asset.get("type")).lower() == "backup" and _text(asset.get("host"))
    }
    expiring = 0
    prod_services: dict[str, list[str]] = {}
    for asset in assets:
        asset_type = _text(asset.get("type")).lower()
        env = _text(asset.get("env"))
        ref = _asset_ref(asset)
        owner = _text(asset.get("owner"))
        if env in {"生产", "prod", "production"} and asset_type in {"service", "database"}:
            prod_services.setdefault(_text(asset.get("host")), []).append(ref)
        if asset_type in {"certificate", "license"}:
            expires = parse_dt(asset.get("expires"))
            if expires:
                days = (expires - now).total_seconds() / 86400
                if days <= 30:
                    expiring += 1
                label = "证书" if asset_type == "certificate" else "授权"
                if days < 0:
                    severity, title = "blocker", f"{label}已过期"
                    suggestion = "立即续期并验证业务是否已受影响。"
                elif days <= 7:
                    severity, title = "blocker", f"{label}将在 7 天内到期"
                    suggestion = "立即办理续期或更换，并验证更新后的有效期。"
                elif days <= 30:
                    severity, title = "warning", f"{label}将在 30 天内到期"
                    suggestion = "安排续期，避免影响生产运行。"
                else:
                    severity = ""
                if severity:
                    findings.append(
                        _finding(
                            severity,
                            "asset",
                            title,
                            f"资产 {ref} 到期时间 {expires.date().isoformat()}，剩余 {days:.1f} 天。",
                            suggestion,
                            [{"asset": ref, "field": "expires", "value": expires.date().isoformat()}],
                            owner,
                        )
                    )
        if env in {"生产", "prod", "production"} and not owner:
            findings.append(
                _finding(
                    "warning",
                    "asset",
                    "生产资产缺少负责人",
                    f"生产资产 {ref} 的 owner 为空。",
                    "补充明确的资产负责人并纳入值班联系清单。",
                    [{"asset": ref, "field": "owner", "value": ""}],
                    owner,
                )
            )
        if asset_type == "service" and env in {"生产", "prod", "production"}:
            host = _text(asset.get("host"))
            if not host or host.casefold() not in backup_hosts:
                findings.append(
                    _finding(
                        "warning",
                        "asset",
                        "生产服务缺少对应备份",
                        f"生产服务 {ref} 在主机 {host or '未填写'} 上没有匹配的备份资产。",
                        "为该服务配置备份，或确认已有备份已登记到资产台账。",
                        [{"asset": ref, "field": "host", "value": host}],
                        owner,
                    )
                )
    for host, refs in prod_services.items():
        if host and len(refs) >= 2:
            findings.append(
                _finding(
                    "info",
                    "asset",
                    "单机承载多个关键服务",
                    f"主机 {host} 上登记了 {len(refs)} 个生产服务：{'、'.join(refs)}。",
                    "评估单点故障风险；关键服务建议做冗余或冷备。",
                    [{"asset": host, "field": "services", "value": "、".join(refs)}],
                    "信息中心",
                )
            )
    return findings, {"expiring": expiring}


def check_alerts(alerts: list[dict]) -> tuple[list[dict], dict]:
    findings: list[dict] = []
    merged = dedupe_alerts(alerts)
    for alert in merged:
        severity = ALERT_TO_FINDING.get(_text(alert.get("severity")).lower(), "info")
        host = _text(alert.get("host"))
        service = _text(alert.get("service"))
        title = _text(alert.get("title")) or "未命名告警"
        message = _text(alert.get("message"))
        count = int(alert.get("count") or 1)
        detail = message
        if count > 1:
            detail += f"；合并 {count} 条重复告警，首次 {alert.get('first_seen')}，最近 {alert.get('last_seen')}。"
        findings.append(
            _finding(
                severity,
                "alert",
                title,
                detail,
                "核查相关主机/服务，确认告警已恢复；必要时按 runbook 升级。",
                [
                    {"asset": host or service, "field": "host", "value": host},
                    {"asset": host or service, "field": "service", "value": service},
                    {"asset": host or service, "field": "alert_id", "value": _text(alert.get("id"))},
                ],
                "",
            )
        )
    return findings, {"raw": len(alerts), "merged": len(merged)}


def _dedupe_cross_source(findings: list[dict]) -> list[dict]:
    seen: dict[str, dict] = {}
    result: list[dict] = []
    for finding in findings:
        key = finding["title"]
        if key in seen and seen[key]["source"] != finding["source"]:
            existing = seen[key]
            existing["evidence"].extend(finding["evidence"])
            if SEVERITY_RANK.get(finding["severity"], 9) < SEVERITY_RANK.get(existing["severity"], 9):
                existing["severity"] = finding["severity"]
            continue
        if key in seen:
            key = f"{key}#{len(result)}"
        seen[key] = finding
        result.append(finding)
    return result


def build_findings(assets: list[dict], alerts: list[dict], now: datetime) -> dict:
    alert_findings, alert_stats = check_alerts(alerts)
    backup_findings, backup_stats = check_backups(assets, now)
    asset_findings, asset_stats = check_assets(assets, now)
    findings = _dedupe_cross_source(alert_findings + backup_findings + asset_findings)
    findings.sort(key=lambda item: (SEVERITY_RANK.get(item["severity"], 9), item["source"], item["title"]))
    for index, finding in enumerate(findings, start=1):
        finding["id"] = f"F{index}"
    counts = {"blocker": 0, "warning": 0, "info": 0}
    for finding in findings:
        counts[finding["severity"]] = counts.get(finding["severity"], 0) + 1
    return {
        "findings": findings,
        "counts": counts,
        "alerts": alert_stats,
        "backups": backup_stats,
        "assets": {"total": len(assets), "expiring": asset_stats["expiring"]},
    }


def asset_search(assets: list[dict], q: str) -> list[dict]:
    query = (q or "").strip().casefold()
    if not query:
        return assets
    fields = ("name", "ip", "owner", "vendor", "host", "type", "id")
    return [asset for asset in assets if any(query in _text(asset.get(field)).casefold() for field in fields)]


def handover_markdown(state: dict, now: datetime) -> str:
    counts = state["counts"]
    lines = [
        f"# 运维值班交接班 · {now.date().isoformat()}",
        "",
        "## 一、总体",
        f"- 资产 {state['assets']['total']} 项；原始告警 {state['alerts']['raw']} 条，合并后 {state['alerts']['merged']} 条。",
        f"- 发现：阻断 {counts['blocker']}、警告 {counts['warning']}、提示 {counts['info']}。",
        f"- 备份：正常 {state['backups']['ok']}、警告 {state['backups']['warning']}、异常 {state['backups']['blocker']}。",
        "",
        "## 二、必须处理",
    ]
    blockers = [item for item in state["findings"] if item["severity"] == "blocker"]
    if not blockers:
        lines.append("- 无阻断项。")
    for item in blockers:
        lines.append(f"- [{item['id']}] {item['title']}：{item['detail']}")
        lines.append(f"  - 建议：{item['suggestion']}")
    lines += ["", "## 三、近期变更（24 小时）"]
    changes = [
        asset
        for asset in state["assets_list"]
        if asset.get("last_change")
        and parse_dt(asset["last_change"])
        and (now - parse_dt(asset["last_change"])).total_seconds() <= 86400
    ]
    if not changes:
        lines.append("- 无登记变更。")
    for asset in changes:
        lines.append(f"- {asset.get('name', asset.get('id'))}：{asset.get('last_change')}")
    lines += ["", "## 四、待观察"]
    warnings = [item for item in state["findings"] if item["severity"] == "warning"]
    for item in warnings[:6]:
        lines.append(f"- [{item['id']}] {item['title']}：{item['suggestion']}")
    lines += ["", "## 五、交接备注", "- 备份异常未恢复前，明早复查备份任务和恢复演练。", "- 证书/授权到期项按 owner 跟进。"]
    return "\n".join(lines)


def xhx_prompt(state: dict) -> str:
    lines = [
        "你是商汤办公小浣熊。请根据下面的运维值班发现，生成一份 8 页的交接班汇报 PPT 大纲。",
        "每页给出：页标题、3 个要点、图表建议、口播一句。不要编造数字。",
        "",
        f"资产总数：{state['assets']['total']}；原始告警 {state['alerts']['raw']} 条，合并后 {state['alerts']['merged']} 条。",
        f"阻断 {state['counts']['blocker']}、警告 {state['counts']['warning']}、提示 {state['counts']['info']}。",
        "",
        "发现清单：",
    ]
    for item in state["findings"][:12]:
        lines.append(f"- [{item['severity']}] {item['title']}：{item['detail']}")
    lines += [
        "",
        "请输出 JSON：{\"slides\":[{\"title\":\"...\",\"bullets\":[\"...\"],\"chart\":\"...\",\"narration\":\"...\"}]}",
    ]
    return "\n".join(lines)


def build_state(now: datetime | None = None) -> dict:
    now = now or datetime.now(CST)
    assets, alerts = load_data()
    computed = build_findings(assets, alerts, now)
    state = {
        "generated_at": now.isoformat(),
        "source": data_source(),
        "assets_list": assets,
        "alerts_list": dedupe_alerts(alerts),
        "findings": computed["findings"],
        "counts": computed["counts"],
        "alerts": computed["alerts"],
        "backups": computed["backups"],
        "assets": computed["assets"],
    }
    state["handover"] = handover_markdown(state, now)
    state["xhx_prompt"] = xhx_prompt(state)
    return state


def kindle_page(state: dict) -> str:
    counts = state["counts"]
    blockers = [item for item in state["findings"] if item["severity"] == "blocker"][:3]
    items = "".join(
        f"<li><strong>{escape(item['title'])}</strong><br>{escape(item['detail'][:80])}</li>"
        for item in blockers
    ) or "<li>本班没有阻断项。</li>"
    source = "CSV" if state.get("source", "").startswith("csv") else "演示数据"
    stamp = state["generated_at"][:16].replace("T", " ")
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta http-equiv="refresh" content="300">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>运维值班台</title>
<style>
body{{margin:0;background:#fff;color:#000;font-family:-apple-system,"PingFang SC","Noto Sans CJK SC",sans-serif;padding:28px 30px}}
.top{{display:flex;justify-content:space-between;align-items:baseline;border-bottom:3px solid #000;padding-bottom:12px}}
h1{{font-size:34px;margin:0;letter-spacing:-1px}}
.time{{font-size:18px;color:#333}}
.stats{{display:flex;gap:18px;margin:22px 0 20px}}
.stat{{flex:1;border:2px solid #000;padding:10px 12px}}
.stat b{{display:block;font-size:56px;line-height:1.1}}
.stat span{{font-size:18px}}
.label{{font-size:18px;font-weight:700;margin:18px 0 8px}}
ul{{margin:0;padding-left:22px}}
li{{font-size:20px;line-height:1.55;margin-bottom:12px}}
li strong{{font-size:22px}}
.foot{{margin-top:24px;border-top:2px solid #000;padding-top:10px;font-size:15px;color:#333;display:flex;justify-content:space-between}}
</style></head>
<body>
<div class="top"><h1>运维值班台</h1><div class="time">{escape(stamp)}</div></div>
<div class="stats">
  <div class="stat"><b>{counts['blocker']}</b><span>阻断</span></div>
  <div class="stat"><b>{counts['warning']}</b><span>警告</span></div>
  <div class="stat"><b>{counts['info']}</b><span>提示</span></div>
</div>
<div class="label">本班必须处理</div>
<ul>{items}</ul>
<div class="label">值班概况</div>
<div style="font-size:20px;line-height:1.7">
资产 {state['assets']['total']} 项；30 天内到期 {state['assets']['expiring']} 项。<br>
备份：正常 {state['backups']['ok']}，警告 {state['backups']['warning']}，阻断 {state['backups']['blocker']}。<br>
告警：原始 {state['alerts']['raw']} 条，合并后 {state['alerts']['merged']} 条。
</div>
<div class="foot"><span>数据源：{escape(source)}</span><span>每 5 分钟自动刷新</span></div>
</body></html>"""


def _write_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def export_demo(force: bool = False) -> list[Path]:
    demo_assets = load_json(BASE_DIR / "config" / "ops_assets.json", {}).get("assets", [])
    demo_alerts = load_json(BASE_DIR / "data" / "ops_alerts.json", {}).get("alerts", [])
    assets = [asset for asset in demo_assets if _text(asset.get("type")) != "backup"]
    backups = [asset for asset in demo_assets if _text(asset.get("type")) == "backup"]
    targets = [
        (DATA_DIR / "assets.csv", ASSET_FIELDS, assets),
        (DATA_DIR / "backups.csv", BACKUP_FIELDS, backups),
        (DATA_DIR / "alerts.csv", ALERT_FIELDS, demo_alerts),
    ]
    written: list[Path] = []
    for path, fields, rows in targets:
        if path.exists() and not force:
            continue
        _write_csv(path, fields, rows)
        written.append(path)
    return written


def validate_data() -> bool:
    required = {
        "assets.csv": {"name", "type", "owner", "env"},
        "backups.csv": {"name", "host", "schedule", "last_success"},
        "alerts.csv": {"time", "host", "service", "severity", "title"},
    }
    ok = True
    source = data_source()
    if source == "demo":
        print("当前没有 inbox CSV，使用内置演示数据。先运行 `python3 ops_center.py export-demo`。")
        return True
    allowed_types = {"server", "service", "database", "certificate", "license", "backup"}
    allowed_severity = {"critical", "warning", "info"}
    for name, fields in required.items():
        path = DATA_DIR / name
        if not path.exists():
            print(f"[缺失] {name}")
            ok = False
            continue
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            headers = set(reader.fieldnames or [])
            rows = list(reader)
        missing = fields - headers
        if missing:
            ok = False
            print(f"[表头缺失] {name}: {', '.join(sorted(missing))}")
        else:
            print(f"[OK] {name}: {len(rows)} 行")
        for index, row in enumerate(rows, start=2):
            if name == "alerts.csv":
                if row.get("severity") not in allowed_severity:
                    ok = False
                    print(f"[错误] alerts.csv 第 {index} 行 severity={row.get('severity')!r}")
                if not parse_dt(row.get("time")):
                    ok = False
                    print(f"[错误] alerts.csv 第 {index} 行 time 无法解析：{row.get('time')!r}")
                for field in ("host", "service", "title"):
                    if not row.get(field):
                        ok = False
                        print(f"[错误] alerts.csv 第 {index} 行缺少 {field}")
            elif name == "backups.csv":
                if row.get("last_success") and not parse_dt(row.get("last_success")):
                    ok = False
                    print(f"[错误] backups.csv 第 {index} 行 last_success 无法解析：{row.get('last_success')!r}")
                if not row.get("schedule"):
                    print(f"[警告] backups.csv 第 {index} 行 schedule 为空，按日备阈值处理")
            elif name == "assets.csv":
                if row.get("type") and row.get("type") not in allowed_types:
                    ok = False
                    print(f"[错误] assets.csv 第 {index} 行 type={row.get('type')!r}")
                if row.get("expires") and not parse_dt(row.get("expires")):
                    ok = False
                    print(f"[错误] assets.csv 第 {index} 行 expires 无法解析：{row.get('expires')!r}")
    return ok


def serve(port: int = 8890) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"运维值班台: http://127.0.0.1:{port}")
    print(f"数据目录: {DATA_DIR}")
    server.serve_forever()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        return

    def _send(self, status: int, body: bytes, content_type: str):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data, status: int = 200):
        self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            path = BASE_DIR / "templates" / "ops.html"
            if path.exists():
                self._send(200, path.read_bytes(), "text/html; charset=utf-8")
            else:
                self._send(404, b"ops.html missing", "text/plain")
            return
        if parsed.path in {"/kindle", "/kindle.html"}:
            self._send(200, kindle_page(build_state()).encode("utf-8"), "text/html; charset=utf-8")
            return
        if parsed.path == "/api/state":
            self._json(build_state())
            return
        if parsed.path == "/api/assets":
            q = parse_qs(parsed.query).get("q", [""])[0]
            assets, _ = load_data()
            self._json({"assets": asset_search(assets, q)})
            return
        if parsed.path == "/api/handover":
            self._json({"handover": build_state()["handover"]})
            return
        if parsed.path == "/api/xhx-prompt":
            self._json({"prompt": build_state()["xhx_prompt"]})
            return
        self._send(404, b"not found", "text/plain")

    def do_POST(self):
        if self.path != "/api/ack":
            self._send(404, b"not found", "text/plain")
            return
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length).decode("utf-8", errors="ignore") if length else "{}"
        try:
            payload = json.loads(raw or "{}")
        except json.JSONDecodeError:
            payload = {}
        record = {
            "time": datetime.now(CST).isoformat(),
            "id": payload.get("id", ""),
            "action": payload.get("action", "ack"),
            "note": payload.get("note", ""),
        }
        audit = BASE_DIR / "data" / "ops_audit.jsonl"
        audit.parent.mkdir(parents=True, exist_ok=True)
        with audit.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._json({"ok": True, "record": record})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ops_center", description="运维值班台")
    sub = parser.add_subparsers(dest="command")

    serve_parser = sub.add_parser("serve", help="启动值班台 Web UI")
    serve_parser.add_argument("--port", type=int, default=int(os.environ.get("OPS_HTTP_PORT", "8890")))

    report_parser = sub.add_parser("report", help="打印今天的交接班 Markdown")
    report_parser.add_argument("--out", default=None, help="同时写入文件")

    export_parser = sub.add_parser("export-demo", help="把演示数据导出成 inbox CSV 模板")
    export_parser.add_argument("--force", action="store_true")

    sub.add_parser("check", help="检查 inbox CSV 表头和数据量")

    args = parser.parse_args(argv)
    if args.command in (None, "serve"):
        port = getattr(args, "port", int(os.environ.get("OPS_HTTP_PORT", "8890")))
        serve(port)
        return 0
    if args.command == "report":
        text = build_state()["handover"]
        if args.out:
            Path(args.out).write_text(text, encoding="utf-8")
            print(f"写入 {args.out}")
        print(text)
        return 0
    if args.command == "export-demo":
        written = export_demo(args.force)
        if written:
            for path in written:
                print(f"写入 {path}")
        else:
            print("inbox 已存在 CSV；加 --force 覆盖。")
        return 0
    if args.command == "check":
        return 0 if validate_data() else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
