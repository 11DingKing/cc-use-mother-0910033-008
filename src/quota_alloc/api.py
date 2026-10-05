"""零依赖 HTTP API（基于 http.server）。

路由概览
========
- POST   /api/snapshots                   保存资格快照
- GET    /api/snapshots / /api/snapshots/{id}
- POST   /api/rule-versions               保存规则版本（权重/路线系数/保底/上限）
- GET    /api/rule-versions / /api/rule-versions/{id}
- POST   /api/scenarios                   运行试算方案
- GET    /api/scenarios / /api/scenarios/{id}
- GET    /api/scenarios/{id}/diff?against=other_id   解释两方案差异
- POST   /api/publications                正式发布（原子写入配额分录）
- GET    /api/publications / /api/publications/{id}
- POST   /api/publications/{id}/relinquish  企业放弃（触发候补递补+尾差重分）
- POST   /api/publications/{id}/revoke      资格撤销（同套确定规则）
- GET    /healthz

所有响应为 JSON；错误体统一 {"error": code, "message": ...}。
"""
from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from .services import ApiError, QuotaService
from .storage import Repository


class _Handler(BaseHTTPRequestHandler):
    server_version = "QuotaAlloc/1.0"
    service: QuotaService = None  # 由 make_server 注入（类属性，单库）

    # -- 工具 -------------------------------------------------------------

    def _send(self, obj, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ApiError("invalid_json", f"请求体不是合法 JSON：{exc}") from exc
        if not isinstance(value, dict):
            raise ApiError("invalid_json", "请求体必须是 JSON 对象")
        return value

    def _handle(self, fn, *args, **kwargs):
        try:
            result = fn(*args, **kwargs)
        except ApiError as exc:
            self._send({"error": exc.code, "message": exc.message}, exc.status)
        except KeyError as exc:
            self._send({"error": "missing_field", "message": f"缺少字段：{exc}"}, 400)
        except ValueError as exc:
            self._send({"error": "invalid_value", "message": str(exc)}, 400)
        else:
            if result is None:
                self._send({"ok": True})
            else:
                self._send(result)

    def log_message(self, fmt, *args) -> None:  # 安静日志，测试输出更干净
        return

    # -- 路由 -------------------------------------------------------------

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        q = parse_qs(parsed.query)
        svc = self.service

        if path == "/healthz":
            return self._send({"status": "ok"})
        if path == "/api/snapshots":
            return self._handle(svc.list_snapshots)
        if path == "/api/rule-versions":
            return self._handle(svc.list_rule_versions)
        if path == "/api/scenarios":
            return self._handle(svc.list_scenarios)
        if path == "/api/publications":
            return self._handle(svc.list_publications)

        m = re.fullmatch(r"/api/snapshots/([\w-]+)", path)
        if m:
            return self._handle(svc.get_snapshot, m.group(1))
        m = re.fullmatch(r"/api/rule-versions/([\w-]+)", path)
        if m:
            return self._handle(svc.get_rule_version, m.group(1))
        m = re.fullmatch(r"/api/scenarios/([\w-]+)", path)
        if m:
            return self._handle(svc.get_scenario, m.group(1))
        m = re.fullmatch(r"/api/scenarios/([\w-]+)/diff", path)
        if m:
            against = (q.get("against") or [""])[0]
            if not against:
                return self._send({"error": "missing_query",
                                   "message": "需要 ?against=<scenario_id>"}, 400)
            return self._handle(svc.diff_scenarios, m.group(1), against)
        m = re.fullmatch(r"/api/publications/([\w-]+)", path)
        if m:
            return self._handle(svc.get_publication, m.group(1))

        self._send({"error": "not_found", "message": f"无此路由：{path}"}, 404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        svc = self.service

        try:
            body = self._read_json()
        except ApiError as exc:
            return self._send({"error": exc.code, "message": exc.message}, exc.status)

        if path == "/api/snapshots":
            return self._handle(svc.create_snapshot,
                                body.get("subjects", []), body.get("note"))
        if path == "/api/rule-versions":
            return self._handle(self._create_rule_version, body)
        if path == "/api/scenarios":
            return self._handle(self._run_scenario, body)
        if path == "/api/publications":
            sid = body.get("scenario_id")
            if not sid:
                return self._send({"error": "missing_field",
                                   "message": "缺少 scenario_id"}, 400)
            return self._handle(svc.publish, sid)

        m = re.fullmatch(r"/api/publications/([\w-]+)/(relinquish|revoke)", path)
        if m:
            pub_id, action = m.group(1), m.group(2)
            subject_id = body.get("subject_id")
            if not subject_id:
                return self._send({"error": "missing_field",
                                   "message": "缺少 subject_id"}, 400)
            fn = svc.relinquish if action == "relinquish" else svc.revoke
            return self._handle(fn, pub_id, str(subject_id))

        self._send({"error": "not_found", "message": f"无此路由：{path}"}, 404)

    # -- 入参整形 ---------------------------------------------------------

    def _create_rule_version(self, body: dict) -> dict:
        params = body.get("params") if "params" in body else {
            k: body[k] for k in
            ("output_weight", "route_coeff", "compliance_weight", "floors", "caps")
            if k in body
        }
        return self.service.create_rule_version(
            name=str(body.get("name", "未命名规则")),
            params=params,
            note=body.get("note"),
        )

    def _run_scenario(self, body: dict) -> dict:
        for key in ("snapshot_id", "rule_version_id", "total_quota"):
            if key not in body:
                raise ApiError("missing_field", f"缺少字段：{key}")
        return self.service.run_scenario(
            snapshot_id=str(body["snapshot_id"]),
            rule_version_id=str(body["rule_version_id"]),
            total_quota=body["total_quota"],
            name=body.get("name"),
        )


def make_server(host: str, port: int, repo: Repository) -> ThreadingHTTPServer:
    service = QuotaService(repo)

    class BoundHandler(_Handler):
        pass

    BoundHandler.service = service
    server = ThreadingHTTPServer((host, port), BoundHandler)
    server.service = service  # type: ignore[attr-defined]
    server.repo = repo        # type: ignore[attr-defined]
    return server


def main() -> None:
    import argparse
    import os

    parser = argparse.ArgumentParser(description="年度配额公平分配 HTTP 服务")
    parser.add_argument("--host", default=os.environ.get("QUOTA_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("QUOTA_PORT", "8080")))
    parser.add_argument("--db", default=os.environ.get("QUOTA_DB", "data/quota.sqlite3"))
    args = parser.parse_args()

    import pathlib
    if args.db != ":memory:":
        pathlib.Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    repo = Repository(args.db)
    server = make_server(args.host, args.port, repo)
    print(f"配额分配服务监听 http://{args.host}:{args.port} （数据库 {args.db}）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        repo.close()


if __name__ == "__main__":
    main()
