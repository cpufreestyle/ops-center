from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta
import json
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
from ops_center import invalidate_cache  # noqa: E402

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


class CacheTests(unittest.TestCase):
    def setUp(self) -> None:
        invalidate_cache()

    def test_cache_returns_same_object(self) -> None:
        a = build_state()
        b = build_state()
        self.assertIs(a, b, "Second call should return the cached object")

    def test_cache_bypass_with_explicit_now(self) -> None:
        cached = build_state()
        fresh = build_state(now=datetime.now(CST))
        self.assertIsNot(cached, fresh, "Explicit now= should bypass cache")

    def test_invalidate_clears_cache(self) -> None:
        first = build_state()
        invalidate_cache()
        second = build_state()
        self.assertIsNot(first, second, "After invalidation a new object must be built")


class ServeTests(unittest.TestCase):
    def _make_request(self, path: str, method: str = "GET") -> tuple[int, bytes]:
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", 8890, timeout=5)
        conn.request(method, path)
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        return resp.status, body

    def test_healthz(self) -> None:
        try:
            status, body = self._make_request("/healthz")
        except OSError:
            self.skipTest("serve not running on :8890")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertTrue(data.get("ok"))
        self.assertIn("generated_at", data)

    def test_reload(self) -> None:
        try:
            status, body = self._make_request("/api/reload", method="POST")
        except OSError:
            self.skipTest("serve not running on :8890")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertTrue(data.get("ok"))


class KindleShortcutTests(unittest.TestCase):
    def test_shortcut_prints_lan_url(self) -> None:
        import re
        import subprocess
        script = Path(__file__).resolve().parent.parent / "scripts" / "ops_center_kindle.sh"
        result = subprocess.run(
            ["sh", str(script), "--print"],
            capture_output=True,
            text=True,
            check=True,
        )
        url = result.stdout.strip()
        self.assertTrue(url.startswith("http://"), msg=f"unexpected URL: {url}")
        self.assertTrue(url.endswith(":8890/kindle"), msg=f"unexpected URL: {url}")
        self.assertRegex(url, r"^http://[0-9.]+:8890/kindle$")
