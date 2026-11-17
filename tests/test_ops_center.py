from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ops_center import (  # noqa: E402
    CST,
    asset_search,
    build_state,
    check_assets,
    check_backups,
    dedupe_alerts,
    kindle_page,
    parse_dt,
)

NOW = datetime(2026, 11, 16, 22, 0, tzinfo=CST)


class ParseTests(unittest.TestCase):
    def test_naive_is_cst(self) -> None:
        value = parse_dt("2026-11-16T21:00:00")
        self.assertEqual(value.utcoffset(), timedelta(hours=8))

    def test_z_suffix(self) -> None:
        value = parse_dt("2026-11-16T13:00:00Z")
        self.assertEqual(value.hour, 21)

    def test_invalid(self) -> None:
        self.assertIsNone(parse_dt("not-a-date"))


class AlertTests(unittest.TestCase):
    def test_dedupe_keeps_latest_and_highest_severity(self) -> None:
        alerts = [
            {"id": "1", "time": "2026-11-16T21:00:00+08:00", "host": "h1", "service": "s1", "title": "502", "severity": "warning"},
            {"id": "2", "time": "2026-11-16T21:05:00+08:00", "host": "h1", "service": "s1", "title": "502", "severity": "critical"},
        ]
        merged = dedupe_alerts(alerts)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["count"], 2)
        self.assertEqual(merged[0]["severity"], "critical")
        self.assertEqual(merged[0]["id"], "2")


class BackupTests(unittest.TestCase):
    def test_backup_thresholds(self) -> None:
        assets = [
            {"id": "b1", "type": "backup", "last_success": "2026-11-13T02:00:00+08:00"},
            {"id": "b2", "type": "backup", "last_success": "2026-11-15T19:00:00+08:00"},
            {"id": "b3", "type": "backup", "last_success": "2026-11-16T02:00:00+08:00"},
            {"id": "b4", "type": "backup", "last_success": ""},
        ]
        findings, stats = check_backups(assets, NOW)
        self.assertEqual(stats["total"], 4)
        self.assertEqual(stats["ok"], 1)
        self.assertEqual(stats["warning"], 1)
        self.assertEqual(stats["blocker"], 2)
        self.assertTrue(any(item["title"] == "备份无有效成功记录" for item in findings))

    def test_weekly_uses_longer_window(self) -> None:
        assets = [
            {
                "id": "weekly",
                "type": "backup",
                "schedule": "每周日 04:00",
                "last_success": "2026-11-08T04:00:00+08:00",
            }
        ]
        findings, stats = check_backups(assets, NOW)
        self.assertEqual(stats["warning"], 1)
        self.assertEqual(len(findings), 1)


class AssetTests(unittest.TestCase):
    def test_expiry_and_owner(self) -> None:
        assets = [
            {"id": "c1", "type": "certificate", "env": "生产", "owner": "信息中心", "expires": "2026-11-21"},
            {"id": "c2", "type": "license", "env": "生产", "owner": "信息中心", "expires": "2026-12-06"},
            {"id": "s1", "type": "service", "env": "生产", "owner": "", "host": "h1"},
        ]
        findings, stats = check_assets(assets, NOW)
        titles = [item["title"] for item in findings]
        self.assertIn("证书将在 7 天内到期", titles)
        self.assertIn("授权将在 30 天内到期", titles)
        self.assertIn("生产资产缺少负责人", titles)
        self.assertIn("生产服务缺少对应备份", titles)
        self.assertGreaterEqual(stats["expiring"], 2)

    def test_search(self) -> None:
        assets = [
            {"id": "srv-1", "name": "统计业务服务器", "ip": "10.20.1.11", "owner": "信息中心"},
            {"id": "db-1", "name": "数据库", "ip": "10.20.1.21", "owner": "数据组"},
        ]
        self.assertEqual(len(asset_search(assets, "10.20.1.11")), 1)
        self.assertEqual(len(asset_search(assets, "信息中心")), 1)
        self.assertEqual(len(asset_search(assets, "不存在")), 0)


class StateTests(unittest.TestCase):
    def test_real_demo_data(self) -> None:
        state = build_state(NOW)
        self.assertEqual(state["assets"]["total"], 16)
        self.assertEqual(state["alerts"]["raw"], 6)
        self.assertEqual(state["alerts"]["merged"], 5)
        self.assertEqual(state["backups"]["blocker"], 2)
        self.assertGreaterEqual(state["counts"]["blocker"], 4)
        self.assertIn("健康检查 502", [item["title"] for item in state["findings"]])
        self.assertIn("备份超时超过阈值", [item["title"] for item in state["findings"]])

    def test_handover_sections(self) -> None:
        state = build_state(NOW)
        text = state["handover"]
        for section in ["## 一、总体", "## 二、必须处理", "## 三、近期变更（24 小时）", "## 四、待观察", "## 五、交接备注"]:
            self.assertIn(section, text)
        self.assertIn("统计业务数据库服务器", text)
        self.assertIn("小浣熊", state["xhx_prompt"])

    def test_kindle_page(self) -> None:
        page = kindle_page(build_state(NOW))
        self.assertIn("运维值班台", page)
        self.assertIn('http-equiv="refresh"', page)
        self.assertNotIn("<script", page)
        self.assertIn("健康检查 502", page)


if __name__ == "__main__":
    unittest.main()
