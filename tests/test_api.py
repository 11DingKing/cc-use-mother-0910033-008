"""HTTP API 端到端冒烟测试。"""
from __future__ import annotations

import http.client
import json
import sys
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from quota_service.api import make_handler
from quota_service.service import QuotaService

ENTERPRISES = [
    {"enterprise_id": "E1", "name": "大型甲", "category": "大型", "output": 1000,
     "tech_route": "路线A", "compliance_rate": 10000, "eligible": True},
    {"enterprise_id": "E2", "name": "小微乙", "category": "小微", "output": 1,
     "tech_route": "路线A", "compliance_rate": 10000, "eligible": True},
    {"enterprise_id": "E3", "name": "失信丙", "category": "大型", "output": 500,
     "tech_route": "路线A", "compliance_rate": 4000, "eligible": False, "note": "履约不达标"},
]
RULES = {
    "tech_factors": {"路线A": 10000},
    "compliance_bands": [{"min_rate": 9000, "factor": 11000}],
    "floors": {"小微": 100},
    "caps": {"大型": 850},
}


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.service = QuotaService(":memory:")
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.service))
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.service.close()

    def request(self, method: str, path: str, body: dict | None = None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"} if body is not None else {}
        conn.request(method, path, body=payload, headers=headers)
        response = conn.getresponse()
        data = json.loads(response.read().decode("utf-8"))
        conn.close()
        return response.status, data

    def test_full_flow(self) -> None:
        status, health = self.request("GET", "/health")
        self.assertEqual((status, health["ok"]), (200, True))

        status, snap = self.request("POST", "/snapshots", {"period": "2026", "enterprises": ENTERPRISES})
        self.assertEqual(status, 201)
        status, rule = self.request("POST", "/rule-versions", {"name": "api-v1", "config": RULES})
        self.assertEqual(status, 201)

        status, scenario = self.request("POST", "/scenarios", {
            "period": "2026", "snapshot_id": snap["snapshot_id"],
            "rule_version_id": rule["rule_version_id"], "total_quota": 1000,
        })
        self.assertEqual(status, 201)
        self.assertEqual(scenario["status"], "草稿")
        results = {r["enterprise_id"]: r for r in scenario["results"]}
        self.assertEqual(results["E3"]["reason_code"], "ineligible")

        status, published = self.request("POST", f"/scenarios/{scenario['id']}/publish")
        self.assertEqual(status, 200)
        self.assertEqual(published["total_allocated"], 1000)

        # 每家企业获得/未获得原因
        status, allocations = self.request("GET", "/periods/2026/allocations")
        self.assertEqual(status, 200)
        items = {i["enterprise_id"]: i for i in allocations["items"]}
        self.assertEqual(items["E3"]["status"], "未获得")
        self.assertIn("履约不达标", items["E3"]["reason"])

        # 放弃 → 确定规则递补
        status, surrender = self.request("POST", "/periods/2026/surrender",
                                         {"enterprise_id": "E1", "amount": 50})
        self.assertEqual(status, 200)
        self.assertEqual(surrender["grants"], {"E2": 50})

        # 撤销 → 收回并递补
        status, revoked = self.request("POST", "/periods/2026/revoke",
                                       {"enterprise_id": "E2", "reason": "申报造假"})
        self.assertEqual(status, 200)
        self.assertEqual(revoked["freed"], 200)  # 初始 150 + 递补 50

        status, ledger = self.request("GET", "/periods/2026/ledger")
        self.assertEqual(status, 200)
        types = [e["entry_type"] for e in ledger["entries"]]
        self.assertEqual(types.count("初始分配"), 2)
        self.assertIn("放弃", types)
        self.assertIn("撤销", types)
        self.assertIn("递补", types)

    def test_error_mapping(self) -> None:
        status, _ = self.request("GET", "/scenarios/999")
        self.assertEqual(status, 404)
        status, body = self.request("POST", "/snapshots", {"period": "", "enterprises": []})
        self.assertEqual(status, 400)
        self.assertIn("error", body)
        status, _ = self.request("POST", "/periods/2027/surrender",
                                 {"enterprise_id": "E1", "amount": 1})
        self.assertEqual(status, 409)
        status, _ = self.request("GET", "/no-such-route")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
