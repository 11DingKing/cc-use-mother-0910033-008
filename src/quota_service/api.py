"""基于标准库的 JSON HTTP 接口。

接口一览：
- GET  /health                            健康检查
- POST /snapshots                         保存资格快照 {period, enterprises:[...]}
- POST /rule-versions                     保存规则版本 {name, config:{...}}
- POST /scenarios                         新建试算方案 {period, snapshot_id, rule_version_id, total_quota}
- GET  /scenarios/{id}                    查看试算结果（含每家原因与候补队列）
- GET  /scenarios/diff?a={id}&b={id}      对比两个试算方案并解释差异
- POST /scenarios/{id}/publish            正式发布（原子写入配额分录）
- POST /periods/{period}/surrender        企业放弃额度 {enterprise_id, amount, note?}
- POST /periods/{period}/revoke           撤销企业资格 {enterprise_id, reason}
- POST /periods/{period}/seal             封存周期
- GET  /periods/{period}/allocations      每家企业当前额度与原因
- GET  /periods/{period}/ledger           配额分录流水
"""
from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

from .service import QuotaService, ServiceError


def make_handler(service: QuotaService):
    routes = [
        ("GET", re.compile(r"^/health$"), lambda m, q, b: (200, {"ok": True})),
        ("POST", re.compile(r"^/snapshots$"),
         lambda m, q, b: (201, service.create_snapshot(b.get("period", ""), b.get("enterprises", [])))),
        ("POST", re.compile(r"^/rule-versions$"),
         lambda m, q, b: (201, service.create_rule_version(b.get("name", ""), b.get("config", {})))),
        ("POST", re.compile(r"^/scenarios$"),
         lambda m, q, b: (201, service.run_trial(b.get("period", ""), int(b.get("snapshot_id", 0)),
                                                 int(b.get("rule_version_id", 0)), b.get("total_quota", 0)))),
        ("GET", re.compile(r"^/scenarios/(?P<sid>\d+)$"),
         lambda m, q, b: (200, service.get_scenario(int(m["sid"])))),
        ("GET", re.compile(r"^/scenarios/diff$"),
         lambda m, q, b: (200, service.diff_scenarios(_one(q, "a"), _one(q, "b")))),
        ("POST", re.compile(r"^/scenarios/(?P<sid>\d+)/publish$"),
         lambda m, q, b: (200, service.publish(int(m["sid"])))),
        ("POST", re.compile(r"^/periods/(?P<period>[^/]+)/surrender$"),
         lambda m, q, b: (200, service.surrender(m["period"], str(b.get("enterprise_id", "")),
                                                 b.get("amount", 0), str(b.get("note", ""))))),
        ("POST", re.compile(r"^/periods/(?P<period>[^/]+)/revoke$"),
         lambda m, q, b: (200, service.revoke(m["period"], str(b.get("enterprise_id", "")),
                                              str(b.get("reason", ""))))),
        ("POST", re.compile(r"^/periods/(?P<period>[^/]+)/seal$"),
         lambda m, q, b: (200, service.seal(m["period"]))),
        ("GET", re.compile(r"^/periods/(?P<period>[^/]+)/allocations$"),
         lambda m, q, b: (200, service.allocations(m["period"]))),
        ("GET", re.compile(r"^/periods/(?P<period>[^/]+)/ledger$"),
         lambda m, q, b: (200, service.ledger(m["period"]))),
    ]

    class Handler(BaseHTTPRequestHandler):
        server_version = "QuotaService/0.1"

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def _dispatch(self, method: str) -> None:
            path, _, query = self.path.partition("?")
            try:
                body = self._read_body() if method == "POST" else {}
                params = parse_qs(query)
                for verb, pattern, handler in routes:
                    if verb != method:
                        continue
                    match = pattern.match(path)
                    if match:
                        status, payload = handler(match, params, body)
                        self._send(status, payload)
                        return
                self._send(404, {"error": f"接口不存在：{method} {path}"})
            except ServiceError as exc:
                self._send(exc.status, {"error": exc.message})
            except (ValueError, KeyError, TypeError) as exc:
                self._send(400, {"error": f"请求参数非法：{exc}"})
            except Exception as exc:  # noqa: BLE001 - 兜底，避免连接直接断开
                self._send(500, {"error": f"服务内部错误：{exc}"})

        def _read_body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length == 0:
                return {}
            raw = self.rfile.read(length)
            try:
                value = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError as exc:
                raise ServiceError(400, f"请求体不是合法 JSON：{exc}") from exc
            if not isinstance(value, dict):
                raise ServiceError(400, "请求体必须是 JSON 对象")
            return value

        def _send(self, status: int, payload: dict) -> None:
            data = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format, *args):  # 静默访问日志
            pass

    return Handler


def _one(params: dict, key: str) -> int:
    values = params.get(key)
    if not values:
        raise ServiceError(400, f"缺少查询参数：{key}")
    return int(values[0])


def serve(db_path: str, host: str, port: int) -> None:
    service = QuotaService(db_path)
    server = ThreadingHTTPServer((host, port), make_handler(service))
    print(f"配额分配服务已启动：http://{host}:{server.server_address[1]}（数据库 {db_path}）")
    try:
        server.serve_forever()
    finally:
        service.close()
