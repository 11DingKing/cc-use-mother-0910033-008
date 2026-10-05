"""端到端演示：资格快照 → 规则版本 → 多方案试算 → 差异解释 → 原子发布 → 放弃/递补。

运行：python3 tools/demo.py
使用内存库，不产生任何文件。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from quota_alloc.services import QuotaService
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
    {"subject_id": "E05", "name": "不合格戊", "size": "small", "tech_route": "A",
     "output": 200, "compliance_score": 40, "eligible": False},
]

BASE_RULES = {
    "name": "基线规则",
    "output_weight": 1.0,
    "compliance_weight": 0.5,
    "route_coeff": {"A": 1.0, "B": 0.6},
    "floors": {"large": 200, "medium": 80, "small": 40, "micro": 20},
    "caps": {"large": 5000, "medium": 2000, "small": 800, "micro": 400},
}
PRO_SMALL = {
    **BASE_RULES,
    "name": "扶小规则",
    "floors": {"large": 200, "medium": 80, "small": 300, "micro": 120},
}


def show(title: str, obj) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def main() -> None:
    svc = QuotaService(Repository(":memory:"))

    snap = svc.create_snapshot(SUBJECTS, note="2026 年度资格快照")
    show("1. 资格快照已保存（内容哈希寻址，重复提交幂等）", snap)

    r1 = svc.create_rule_version(name=BASE_RULES["name"], params=BASE_RULES)
    r2 = svc.create_rule_version(name=PRO_SMALL["name"], params=PRO_SMALL)
    show("2. 两个规则版本（保底/上限随版本冻结）",
         {"基线": r1["rule_version_id"], "扶小": r2["rule_version_id"]})

    a = svc.run_scenario(snapshot_id=snap["snapshot_id"],
                         rule_version_id=r1["rule_version_id"],
                         total_quota=3000, name="基线-3000")
    b = svc.run_scenario(snapshot_id=snap["snapshot_id"],
                         rule_version_id=r2["rule_version_id"],
                         total_quota=3000, name="扶小-3000")
    show("3. 试算方案A（基线）逐企业结果与原因",
         [{"企业": ln["subject_id"], "额度": ln["allocated"],
           "原因": ln["reasons"], "说明": ln["note"]}
          for ln in a["result"]["lines"]])

    diff = svc.diff_scenarios(a["scenario_id"], b["scenario_id"])
    show("4. 方案差异解释（根因 + 逐企业增减）", {
        "root_causes": diff["root_causes"],
        "summary": diff["summary"],
        "per_subject": diff["per_subject"],
    })

    pub = svc.publish(a["scenario_id"])
    show("5. 方案A 正式发布（单事务原子写入全量配额分录）", pub)

    detail = svc.get_publication(pub["publication_id"])
    show("6. 配额分录（每家企业获得/未获得额度的原因）",
         [{"企业": e["subject_id"], "发布额度": e["allocated"],
           "当前持有": e["current_amount"], "状态": e["current_status"],
           "原因": e["reason_text"]} for e in detail["entries"]])

    rel = svc.relinquish(pub["publication_id"], "E01")
    show("7. E01 放弃 → 候补递补 + 尾差确定式重分", {
        "released_amount": rel["released_amount"],
        "promoted": rel["promoted"],
        "distributed": rel["distributed"],
        "retained": rel["retained"],
        "changes": [{"企业": c["subject_id"], "变更前": c["before"],
                     "变更后": c["after"], "增量": c["delta"],
                     "原因": c["reason_text"], "说明": c["note"]}
                    for c in rel["changes"]],
        "rule_steps": rel["events"],
    })

    rev = svc.revoke(pub["publication_id"], "E02")
    show("8. E02 资格撤销（同套确定规则；此时已无候补，仅尾差重分/留存）", {
        "released_amount": rev["released_amount"],
        "promoted": rev["promoted"],
        "distributed": rev["distributed"],
        "retained": rev["retained"],
        "changes": [{"企业": c["subject_id"], "增量": c["delta"],
                     "原因": c["reason_text"]} for c in rev["changes"]],
    })

    final = svc.get_publication(pub["publication_id"])
    show("9. 发布后最终持有量与事件链", {
        "holdings": [{"企业": e["subject_id"], "持有": e["current_amount"],
                      "状态": e["current_status"]} for e in final["entries"]],
        "events": [{"seq": e["seq"], "类型": e["event_type"],
                    "主体": e["subject_id"], "释放": e["released_amount"],
                    "留存": e["retained"]} for e in final["events"]],
    })


if __name__ == "__main__":
    main()
