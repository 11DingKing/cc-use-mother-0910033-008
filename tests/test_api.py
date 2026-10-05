"""HTTP API 端到端测试（真实 socket，零第三方依赖）。"""
from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from quota_alloc.api import make_server
from quota_alloc.storage import Repository

SUBJECTS = [
    {"subject_id": "E01", "name": "大企甲", "size": "large", "tech_route": "A",
     "output": 5000, "compliance_score": 95},
    {"subject_id": "E02", "name": "中企乙", "size": "medium", "tech_route": "A",
     "output": 800, "compliance_score": 70},
    {"subject_id": "E03", "name": "小企丙", "size": "small", "tech_route": "B",
     "output": 30, "compliance_score": 60},
    {"subject_id": "E04", "name": "微企丁", "size": "micro", "tech_route": "B",
     "output": 5, "compliance_score": 55, "waitlist": True},
]

RULES = {
    "name": "基线",
    "output_weight": 1.0,
    "compliance_weight": 0.5,
    "route_coeff": {"A": 1.0, "B": 0.6},
    "floors": {"large": 200, "medium": 80, "small": 40, "micro": 20},
    "caps": {"large": 5000, "medium": 2000, "small": 800, "micro": 400},
}


class ApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = Repository(":memory:")
        self.server = make_server("127.0.0.1", 0, self.repo)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        self.repo.close()

    def call(self, method: str, path: str, body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_full_lifecycle_and_reasons(self) -> None:
        status, body = self.call("GET", "/healthz")
        self.assertEqual(status, 200)

        status, snap = self.call("POST", "/api/snapshots",
                                 {"subjects": SUBJECTS, "note": "2026 资格"})
        self.assertEqual(status, 200)
        self.assertEqual(snap["subject_count"], 4)

        # 非法快照：负产量
        bad = [dict(SUBJECTS[0], output=-1)]
        status, err = self.call("POST", "/api/snapshots", {"subjects": bad})
        self.assertEqual(status, 400)
        self.assertEqual(err["error"], "invalid_subject")

        status, rlv = self.call("POST", "/api/rule-versions", RULES)
        self.assertEqual(status, 200)

        # 规则版本保底 > 上限应被拒
        bad_rules = dict(RULES, floors={"large": 9999, "medium": 80,
                                        "small": 40, "micro": 20})
        status, err = self.call("POST", "/api/rule-versions", bad_rules)
        self.assertEqual(status, 400)
        self.assertEqual(err["error"], "invalid_rules")

        status, scn = self.call("POST", "/api/scenarios", {
            "snapshot_id": snap["snapshot_id"],
            "rule_version_id": rlv["rule_version_id"],
            "total_quota": 3000, "name": "基线方案",
        })
        self.assertEqual(status, 200)
        lines = {ln["subject_id"]: ln for ln in scn["result"]["lines"]}
        self.assertGreaterEqual(lines["E03"]["allocated"], 40)
        self.assertTrue(lines["E03"]["reasons"])
        self.assertIn("waitlist", lines["E04"]["reasons"])

        # 第二方案：扶小规则 + 更大池子，用于差异解释
        pro_small = dict(RULES, name="扶小",
                         floors={"large": 200, "medium": 80, "small": 300, "micro": 100})
        _, rlv2 = self.call("POST", "/api/rule-versions", pro_small)
        _, scn2 = self.call("POST", "/api/scenarios", {
            "snapshot_id": snap["snapshot_id"],
            "rule_version_id": rlv2["rule_version_id"],
            "total_quota": 3200, "name": "扶小方案",
        })
        status, diff = self.call(
            "GET", f"/api/scenarios/{scn['scenario_id']}/diff?against={scn2['scenario_id']}"
        )
        self.assertEqual(status, 200)
        self.assertTrue(len(diff["root_causes"]) >= 2)  # 规则与总额度均不同
        e03 = next(p for p in diff["per_subject"] if p["subject_id"] == "E03")
        self.assertGreater(e03["delta_b_minus_a"], 0)
        self.assertIn("方案B多分", e03["explanation"])

        # 同输入复算 → 同方案 id（确定性）
        _, scn_again = self.call("POST", "/api/scenarios", {
            "snapshot_id": snap["snapshot_id"],
            "rule_version_id": rlv["rule_version_id"],
            "total_quota": 3000,
        })
        self.assertEqual(scn_again["scenario_id"], scn["scenario_id"])

        # 发布（原子）
        status, pub = self.call("POST", "/api/publications",
                                {"scenario_id": scn["scenario_id"]})
        self.assertEqual(status, 200)
        self.assertEqual(pub["entry_count"], 4)
        status, err = self.call("POST", "/api/publications",
                                {"scenario_id": scn["scenario_id"]})
        self.assertEqual(status, 409)
        self.assertEqual(err["error"], "already_published")

        # 查看发布：每家企业的原因（含中文解释）
        status, detail = self.call("GET", f"/api/publications/{pub['publication_id']}")
        self.assertEqual(status, 200)
        for e in detail["entries"]:
            self.assertTrue(e["reasons"])
        self.assertEqual(detail["allocated_sum"], 3000)

        # E01 放弃 → E04 候补递补
        status, rel = self.call(
            "POST", f"/api/publications/{pub['publication_id']}/relinquish",
            {"subject_id": "E01"},
        )
        self.assertEqual(status, 200)
        self.assertIn("E04", rel["promoted"])
        self.assertEqual(rel["released_amount"], lines["E01"]["allocated"])
        self.assertEqual(rel["distributed"] + rel["retained"], rel["released_amount"])

        # 发布详情反映新持有量与事件
        _, detail2 = self.call("GET", f"/api/publications/{pub['publication_id']}")
        hold = {e["subject_id"]: e for e in detail2["entries"]}
        self.assertEqual(hold["E01"]["current_status"], "relinquished")
        self.assertEqual(hold["E01"]["current_amount"], 0)
        self.assertEqual(hold["E04"]["current_status"], "active")
        self.assertGreater(hold["E04"]["current_amount"], 0)
        self.assertEqual(len(detail2["events"]), 1)

        # 重复放弃 → 409
        status, err = self.call(
            "POST", f"/api/publications/{pub['publication_id']}/relinquish",
            {"subject_id": "E01"},
        )
        self.assertEqual(status, 409)

        # 撤销 E02 资格
        status, rev = self.call(
            "POST", f"/api/publications/{pub['publication_id']}/revoke",
            {"subject_id": "E02"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(rev["event_type"], "revoke")

    def test_unknown_routes_and_missing_fields(self) -> None:
        status, err = self.call("GET", "/api/nope")
        self.assertEqual(status, 404)
        status, err = self.call("POST", "/api/scenarios", {"snapshot_id": "x"})
        self.assertEqual(status, 400)
        self.assertEqual(err["error"], "missing_field")
        status, err = self.call("POST", "/api/snapshots", {"subjects": "not-a-list"})
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
