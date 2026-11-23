"""设备 IP 台账 · 数据接入层。

读取单位真实的设备 IP 台账 Excel（.xls），按运维值班台的纯函数模式组织，
供工具层（agent_tools）与 Web 界面调用。

安全边界：
- 只读：只读 Excel，不写、不改、不登录、不联网。
- 数据不出域：不打印、不日志化真实 IP / 人员姓名；对外展示由上层负责脱敏。
- 容错：xlrd 不可用、文件缺失、sheet 缺失都返回结构化错误，不抛异常。

依赖：xlrd（读 .xls）。未安装时给出明确提示，其余功能不受影响。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

# 台账路径通过环境变量 IP_LEDGER_PATH 指定（不硬编码真实路径，避免泄露内网/账号信息）。
# 设置示例：
#   set IP_LEDGER_PATH=D:\你的台账目录\20221010 IP地址.xls
# 未设置时工具会返回友好提示，不会读取任何文件。

# 最新最全的 sheet 名（历史 sheet 见 LATEST_SHEET 说明）
LATEST_SHEET = "202209"

# 台账字段 -> 规范化字段名
# 源表头：IP号 / 购入日期 / 设备型号 / 固定资产编号 / 信息委编号 /
#         所在房间 / 人员 / MAC地址 / 硬盘编号 / 主机编号
FIELD_MAP = {
    "ip号": "ip",
    "购入日期": "purchase_date",
    "设备型号": "model",
    "固定资产编号": "asset_no",
    "信息委编号": "info_committee_no",
    "所在房间": "room",
    "人员": "person",
    "mac地址": "mac",
    "硬盘编号": "disk_no",
    "主机编号": "host_no",
    # 无名备注列（源表第 1 列 / 第 12 列起有零散内容）按位置单独处理
}


def ledger_path() -> Optional[Path]:
    """返回台账文件路径（环境变量 IP_LEDGER_PATH，未设置返回 None）。"""
    raw = os.environ.get("IP_LEDGER_PATH", "").strip()
    return Path(raw) if raw else None


def _norm_header(text: str) -> str:
    """表头规范化：去空白、转小写，便于匹配 FIELD_MAP。"""
    return re.sub(r"\s+", "", str(text or "")).strip().lower()


def _cell(sheet, row: int, col: int) -> str:
    """安全取单元格并转字符串（xlrd 对空单元格返回 ''）。"""
    try:
        val = sheet.cell_value(row, col)
    except IndexError:
        return ""
    if val is None:
        return ""
    if isinstance(val, float) and val == int(val):
        # 避免 10.0 这类整数被读成小数（IP、编号常见）
        return str(int(val))
    return str(val).strip()


def load_sheets(path: Optional[Path] = None) -> dict:
    """列出台账里所有 sheet（= IP 地址的历史快照）。

    返回 {"ok": bool, "path": str, "sheets": [{"name","rows"}], "error": str?}
    """
    path = path or ledger_path()
    if path is None:
        return {"ok": False, "path": "",
                "error": "未指定台账路径。请设环境变量 IP_LEDGER_PATH 指向 .xls 台账文件。"}
    if not path.exists():
        return {"ok": False, "path": str(path),
                "error": f"台账文件不存在：{path}（可设环境变量 IP_LEDGER_PATH 指定）"}
    try:
        import xlrd
    except ImportError:
        return {"ok": False, "path": str(path),
                "error": "缺少依赖 xlrd，无法读取 .xls。请 `pip install xlrd` 后重试。"}
    try:
        wb = xlrd.open_workbook(str(path))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "path": str(path), "error": f"打开台账失败：{e}"}
    sheets = [{"name": sh.name, "rows": sh.nrows} for sh in wb.sheets()]
    return {"ok": True, "path": str(path), "sheets": sheets}


def load_records(path: Optional[Path] = None, sheet: Optional[str] = None) -> dict:
    """读取指定 sheet（默认最新）的全部 IP 记录。

    返回：
    {
      "ok": bool, "sheet": str, "total": int, "records": [ {...规范化字段...} ],
      "columns": [原始表头], "filled": {字段: 填充数}, "error": str?
    }
    记录字段：ip, model, asset_no, info_committee_no, room, person,
              mac, disk_no, host_no, purchase_date, remark, source_row
    """
    path = path or ledger_path()
    sheet = sheet or LATEST_SHEET
    base = load_sheets(path)
    if not base["ok"]:
        return {**base, "sheet": sheet, "total": 0, "records": [], "columns": [], "filled": {}}

    import xlrd
    wb = xlrd.open_workbook(str(path))
    try:
        sh = wb.sheet_by_name(sheet)
    except Exception:  # noqa: BLE001
        return {"ok": False, "sheet": sheet, "total": 0, "records": [],
                "columns": [], "filled": {},
                "error": f"没有名为「{sheet}」的 sheet。可用：{[s['name'] for s in base['sheets']]}"}

    # 第 0 行是表头
    headers = [_cell(sh, 0, c) for c in range(sh.ncols)]

    records: list[dict] = []
    for r in range(1, sh.nrows):
        ip = _cell(sh, r, 0)  # 列0 = IP号
        if not ip:
            continue
        rec: dict = {"ip": ip, "source_row": r + 1}
        for c in range(sh.ncols):
            raw = headers[c] if c < len(headers) else ""
            key = FIELD_MAP.get(_norm_header(raw))
            if not key:
                continue
            rec[key] = _cell(sh, r, c)
        # 无名备注列：表头为空但单元格有内容的位置（源表常见于第 1 列 index1、第 12 列起）
        remarks = []
        for c in range(sh.ncols):
            raw = headers[c] if c < len(headers) else ""
            if not raw:
                val = _cell(sh, r, c)
                if val:
                    remarks.append(val)
        rec["remark"] = " | ".join(remarks)
        records.append(rec)

    # 填充率统计
    fields = ["ip", "model", "asset_no", "info_committee_no", "room",
              "person", "mac", "disk_no", "host_no", "purchase_date", "remark"]
    filled = {f: sum(1 for x in records if x.get(f)) for f in fields}

    return {"ok": True, "sheet": sheet, "total": len(records), "records": records,
            "columns": [h for h in headers if h], "filled": filled}


def search_records(query: str, path: Optional[Path] = None,
                   sheet: Optional[str] = None, limit: int = 30) -> dict:
    """在台账里按关键词搜索：匹配 IP / 人员 / 房间 / 型号 / MAC / 备注。

    query 为空返回空结果。返回结构与 load_records 一致，外加 query/matched。
    """
    query = (query or "").strip()
    base = load_records(path, sheet)
    if not query:
        return {**base, "query": query, "matched": 0}
    if not base.get("ok"):
        return {**base, "query": query, "matched": 0}

    q = query.lower()
    hits = []
    for rec in base["records"]:
        hay = " ".join(str(rec.get(k, "")) for k in
                       ("ip", "person", "room", "model", "mac", "asset_no", "remark")).lower()
        if q in hay:
            hits.append(rec)
    return {**base, "query": query, "matched": len(hits),
            "records": hits[:limit], "total": len(hits)}


if __name__ == "__main__":
    import json
    info = load_sheets()
    print("sheets:", json.dumps(info.get("sheets", []), ensure_ascii=False))
    data = load_records()
    if data.get("ok"):
        print(f"\nsheet={data['sheet']} 总记录={data['total']}")
        print("列:", data["columns"])
        print("填充率:", json.dumps(data["filled"], ensure_ascii=False))
        # 自检：只打印 IP 后两段掩码，不泄露完整内网信息
        sample = [r["ip"].rsplit(".", 2)[0] + ".x.x" for r in data["records"][:5]]
        print("前5条IP(掩码):", sample)
    else:
        print("错误:", data.get("error"))
