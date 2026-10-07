# 年度配额公平分配

本项目维护年度配额公平分配的领域约定、角色边界与样例数据，并提供完整的 Python 后端服务：保存资格快照与分配规则版本，支持多个试算方案对比，正式发布时原子写入配额分录，放弃、资格撤销、候补递补与尾差分配均采用确定规则，接口逐家返回获得或未获得额度的原因。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/quota_service/`：配额分配后端服务。
  - `allocation.py`：确定性分配引擎（保底 → 加权比例 → 尾差确定归属）。
  - `storage.py`：SQLite 模式；配额分录为只增不改的流水账。
  - `service.py`：快照、规则版本、试算、发布、放弃/撤销/递补、封存。
  - `api.py`：基于标准库的 JSON HTTP 接口。
- `tools/check_contract.py`：契约命令行摘要检查。
- `tools/run_server.py`：启动后端服务。
- `tests/`：契约、引擎、服务与接口回归测试。

## 分配规则（确定性）

相同输入必然得到相同输出，不依赖时间、随机数或遍历顺序：

1. **保底**：每家企业先获得所属类别的最低保障额度（不超过其上限）；保底总额超过额度池时按保底额比例压缩。
2. **加权**：剩余额度按 `产量 × 技术路线系数 × 履约系数` 的权重比例分配；触及类别上限的企业先封顶，余量在未触顶企业间继续分配。
3. **尾差**：取整剩余单位按小数余量降序、企业编号升序逐个分配，确定归属。
4. **递补**：放弃或撤销释放的额度，先按候补队列（未获分配或保底未足额的企业，按权重降序）补足保底缺口，余量再按权重比例分配；被撤销企业与释放来源企业不参与本轮递补。

## HTTP 接口

启动：`python3 tools/run_server.py [数据库路径] [端口]`（默认 `quota.db`、8080）

- `POST /snapshots`：保存资格快照 `{period, enterprises:[...]}`，快照不可修改。
- `POST /rule-versions`：保存规则版本 `{name, config:{tech_factors, compliance_bands, floors, caps, ...}}`。
- `POST /scenarios`：新建试算方案 `{period, snapshot_id, rule_version_id, total_quota}`，同一周期可保留多个方案。
- `GET /scenarios/{id}`：试算结果，含每家企业构成明细（保底/比例/尾差）、原因与候补队列。
- `GET /scenarios/diff?a={id}&b={id}`：对比两个方案，逐家解释差异（系数、保底、上限、总额度、资格变化）。
- `POST /scenarios/{id}/publish`：正式发布，单事务原子写入全部配额分录；同一周期仅允许一个正式方案。
- `POST /periods/{period}/surrender`：企业放弃额度 `{enterprise_id, amount, note?}`，释放量按确定规则递补。
- `POST /periods/{period}/revoke`：撤销企业资格 `{enterprise_id, reason}`，收回全部持有并递补。
- `POST /periods/{period}/seal`：封存周期，之后拒绝一切调整。
- `GET /periods/{period}/allocations`：每家企业当前额度与获得/未获得原因。
- `GET /periods/{period}/ledger`：配额分录流水（初始分配/放弃/撤销/递补）。

方案状态机对应领域契约：草稿 → 已确认 → 执行中 → 已封存。

## 验证

测试命令：`python3 -m unittest discover -s tests -v`

编译命令：`python3 -m compileall -q src tools tests`

命令行检查：`python3 tools/check_contract.py domain/contract.json`
