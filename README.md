# 年度配额公平分配后端

行业主管部门向多类企业分配有限的过渡配额，需兼顾产量、技术路线、历史履约与最低保障，
避免简单按比例导致小型企业失去基本额度。本项目在领域契约骨架上搭建了**零第三方依赖**
（Python 3.11+ 标准库 + SQLite）的完整后端，覆盖资格快照、规则版本、多方案试算与差异
解释、原子发布、以及发布后的放弃 / 资格撤销 / 候补递补 / 尾差分配，所有规则均为**确定
规则**，API 对每家企业返回获得或未获得额度的原因链。

## 为什么小企业有基本额度

1. **阶段一保底**：按企业规模（large/medium/small/micro）给整额最低保障；
2. **阶段二水填**：剩余池按得分（产量×产量权重 + 履约分×履约权重，再乘技术路线系数）
   在各自有效上限内按比例分配，触顶者冻结、余量在未触顶者间重分；
3. **阶段三尾差**：整数化采用最大余数法——先下取整，余量按余数（并列按企业编号升序）
   逐分发放；无正得分者不参与尾差，全员触顶后余量确定留存，不强行发放。

全程整数/定点运算（系数精度万分之一，`SCALE=10000`），没有浮点导致的不确定尾差。

## 确定式发布后处理（放弃 / 撤销共用同一规则）

固定顺序：①释放主体清零 → ②候补按**企业编号升序、整额保底**递补，余量不足整额保底即
关闭递补通道（确定，不切分保底）→ ③剩余池在在册活跃企业（含新递补者）之间按得分
比例、受上限余量约束重分，最大余数法整数化 → ④仍有余量则确定留存。每次操作在单个
`BEGIN IMMEDIATE` 事务内追加事件、更新持有量，并逐企业记录原因链与规则步骤。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/quota_alloc/allocation.py`：确定性分配内核（保底 + 上限水填 + 最大余数尾差；
  放弃/撤销的候补递补与尾差重分）。
- `src/quota_alloc/storage.py`：SQLite 存储，快照/规则版本按内容哈希幂等去重，发布与
  发布后事件单事务原子写入。
- `src/quota_alloc/services.py`：业务编排（快照、版本、试算、差异解释、发布、递补）。
- `src/quota_alloc/api.py`：零依赖 HTTP API（`http.server` + `ThreadingHTTPServer`）。
- `tools/check_contract.py`：命令行摘要检查。
- `tools/demo.py`：完整业务链路演示（内存库，不落盘）。
- `tests/`：内核性质、服务层事务与递补、HTTP 端到端共 25 个回归测试。

## 快速开始

```bash
# 演示完整链路（快照→版本→两方案→差异→发布→放弃→撤销→持有量/事件链）
python3 tools/demo.py

# 启动 HTTP 服务（默认 data/quota.sqlite3；内容寻址，重复提交幂等）
PYTHONPATH=src python3 -m quota_alloc.api --host 0.0.0.0 --port 8080 --db data/quota.sqlite3
# 或 pip install -e . 后使用 quota-api 命令
```

## API 总览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/snapshots` | 保存资格快照（内容哈希幂等） |
| GET  | `/api/snapshots`，`/api/snapshots/{id}` | 列表 / 详情 |
| POST | `/api/rule-versions` | 保存规则版本（权重、路线系数、分规模保底/上限） |
| GET  | `/api/rule-versions`，`/api/rule-versions/{id}` | 列表 / 详情 |
| POST | `/api/scenarios` | 运行试算方案（同输入同 ID，可复现） |
| GET  | `/api/scenarios`，`/api/scenarios/{id}` | 列表 / 详情 |
| GET  | `/api/scenarios/{id}/diff?against=other_id` | 两方案差异：根因 + 逐企业增减与原因变化 |
| POST | `/api/publications` | 正式发布（单事务原子写入全量配额分录，含 0 额度行） |
| GET  | `/api/publications`，`/api/publications/{id}` | 列表 / 分录、当前持有量、事件链 |
| POST | `/api/publications/{id}/relinquish` | 企业放弃（确定式候补递补 + 尾差重分） |
| POST | `/api/publications/{id}/revoke` | 资格撤销（同套确定规则） |
| GET  | `/healthz` | 健康检查 |

### 关键请求体示例

```json
POST /api/snapshots
{"note": "2026 资格", "subjects": [
  {"subject_id": "E03", "name": "小企丙", "size": "small", "tech_route": "B",
   "output": 30, "compliance_score": 60},
  {"subject_id": "E04", "name": "微企丁", "size": "micro", "tech_route": "B",
   "output": 5, "compliance_score": 55, "waitlist": true},
  {"subject_id": "E05", "name": "不合格戊", "size": "small", "tech_route": "A",
   "output": 200, "compliance_score": 40, "eligible": false}
]}

POST /api/rule-versions
{"name": "基线",
 "output_weight": 1.0, "compliance_weight": 0.5,
 "route_coeff": {"A": 1.0, "B": 0.6},
 "floors": {"large": 200, "medium": 80, "small": 40, "micro": 20},
 "caps":   {"large": 5000, "medium": 2000, "small": 800, "micro": 400}}

POST /api/scenarios
{"snapshot_id": "snp_…", "rule_version_id": "rlv_…",
 "total_quota": 3000, "name": "基线方案"}

POST /api/publications {"scenario_id": "scn_…"}
POST /api/publications/pub_…/relinquish {"subject_id": "E01"}
POST /api/publications/pub_…/revoke     {"subject_id": "E02"}
```

### 原因码

API 同时返回机器可读原因码与中文解释，稳定可引用：

| 原因码 | 含义 |
| --- | --- |
| `not_eligible` / `revoked` / `relinquished` | 不合格 / 资格撤销 / 主动放弃 |
| `waitlist` / `waitlist_promoted` / `promotion_floor` | 候补轮空 / 递补获得 / 按规模保底整额补足 |
| `floor_guarantee` | 适用最低保障（小企业基本额度） |
| `pro_rata` / `capped` / `zero_score` | 比例分配 / 触及上限 / 得分为 0 |
| `round_up` / `round_down` | 最大余数法尾差 +1 / 舍入 |
| `residual` / `residual_retained` / `quota_exhausted` | 参与释放额度尾差重分 / 余量确定留存 / 可分额度已尽 |
| `total_too_small` | 总额度不足以覆盖保底之和（优先小微足额，差额逐户留痕） |

## 验证

```bash
# 全部 25 个测试（含真实 socket 的 HTTP 端到端）
python3 -m unittest discover -s tests -v

# 编译检查
python3 -m compileall -q src tools tests

# 契约检查
python3 tools/check_contract.py domain/contract.json
```
