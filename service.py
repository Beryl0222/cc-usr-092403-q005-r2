"""移动广告合规实验室的 HTTP 入口。

除健康检查外提供 JSON API（详见 README「接口一览」）。仓储默认在内存中，
设置环境变量 LAB_DATA_FILE 后会把只增证据快照落盘，重启自动恢复并接续待复测。

访问角色通过请求头声明：
* X-Role: regulator | reviewer —— 监管人员/复核员，可复核、出告知、审复发、看全局；
* X-Role: party 且 X-Subject-Type/X-Subject-Id 指定主体 —— 责任方，只能取得
  本主体的整改材料（广告主看不到他人材料）。
"""

import argparse
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

from domain import (
    AccessDeniedError,
    DomainError,
    Lab,
    NotFoundError,
    ROLE_PARTY,
)

SERVICE_ID = "mobile-ad-audit"
SERVICE_NAME = "移动广告合规实验室"

DATA_FILE = os.environ.get("LAB_DATA_FILE")


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


class Store:
    """带锁与快照持久化的 Lab 仓储。"""

    def __init__(self, path=None):
        self.path = path
        self.lock = threading.RLock()
        self.lab = self._load()

    def _load(self):
        if self.path and os.path.exists(self.path):
            with open(self.path, "r", encoding="utf-8") as handle:
                return Lab.from_snapshot(json.load(handle))
        return Lab()

    def save(self):
        if not self.path:
            return
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(self.lab.to_snapshot(), handle, ensure_ascii=False)
        os.replace(tmp, self.path)

    def call(self, fn, *args, persist=False, **kwargs):
        with self.lock:
            result = fn(*args, **kwargs)
            if persist:
                self.save()
            return result


STORE = Store(DATA_FILE)


def reset_store(path=None):
    """清空并重建全局仓储（供测试隔离使用）。"""
    global STORE
    STORE = Store(path)
    return STORE


class Handler(BaseHTTPRequestHandler):
    """提供健康检查与合规实验室 JSON API。"""

    def _send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise DomainError(f"请求体不是合法 JSON：{exc}")

    def _actor(self):
        """从请求头解析访问角色；缺省视为监管人员（兼容既有内部调用）。"""
        role = self.headers.get("X-Role", "regulator")
        actor = {"role": role, "id": self.headers.get("X-Actor-Id", "")}
        subject_type = self.headers.get("X-Subject-Type")
        subject_id = self.headers.get("X-Subject-Id")
        if subject_type and subject_id:
            actor["subject_type"] = subject_type
            actor["subject_id"] = subject_id
        if role == ROLE_PARTY and not (subject_type and subject_id):
            raise AccessDeniedError("责任方访问必须携带 X-Subject-Type 与 X-Subject-Id")
        return actor

    def _json_404(self):
        self._send_json(404, {"error": "not_found", "message": "未知接口"})

    def _send_error(self, exc):
        if isinstance(exc, AccessDeniedError):
            self._send_json(403, {"error": "access_denied", "message": str(exc)})
        elif isinstance(exc, NotFoundError):
            self._send_json(404, {"error": "not_found", "message": str(exc)})
        elif isinstance(exc, DomainError):
            self._send_json(400, {"error": "domain_error", "message": str(exc)})
        else:
            raise exc

    def do_GET(self):
        path = urlsplit(self.path).path.rstrip("/") or "/"
        try:
            actor = self._actor()
            if path == "/health":
                self._send_json(200, health_payload())
                return
            if path.startswith("/tasks/"):
                task_id = unquote(path.split("/", 2)[2])
                self._send_json(
                    200, STORE.call(STORE.lab.task_report, task_id, actor=actor))
                return
            if path.startswith("/builds/") and path.endswith("/report"):
                build_id = unquote(path[len("/builds/"):-len("/report")])
                self._send_json(
                    200, STORE.call(STORE.lab.build_report, build_id, actor=actor))
                return
            if path.startswith("/builds/") and path.endswith("/oversight"):
                build_id = unquote(path[len("/builds/"):-len("/oversight")])
                self._send_json(
                    200, STORE.call(STORE.lab.oversight_build, build_id, actor=actor))
                return
            if path.startswith("/subjects/"):
                parts = path.split("/")
                if len(parts) == 4:
                    # /subjects/{type}/{id}
                    subject_type, subject_id = parts[2], unquote(parts[3])
                    self._send_json(
                        200,
                        STORE.call(STORE.lab.subject_view, subject_type, subject_id,
                                   actor=actor),
                    )
                    return
                if len(parts) == 5 and parts[4] == "materials":
                    subject_type, subject_id = parts[2], unquote(parts[3])
                    self._send_json(
                        200,
                        STORE.call(STORE.lab.party_materials, subject_type, subject_id,
                                   actor=actor),
                    )
                    return
                self._json_404()
                return
            self._json_404()
        except (AccessDeniedError, NotFoundError, DomainError) as exc:
            self._send_error(exc)

    def do_POST(self):
        path = urlsplit(self.path).path.rstrip("/") or "/"
        try:
            payload = self._read_json()
            actor = self._actor()
            lab = STORE.lab

            if path == "/admin/regulations":
                self._send_json(201, STORE.call(lab.register_regulation, payload, persist=True))
                return
            if path == "/admin/scripts":
                self._send_json(201, STORE.call(lab.register_script, payload, persist=True))
                return
            if path == "/devices":
                self._send_json(201, STORE.call(lab.register_device, payload, persist=True))
                return
            if path == "/builds":
                self._send_json(201, STORE.call(lab.register_build, payload, persist=True))
                return
            if path == "/tasks":
                self._send_json(201, STORE.call(lab.create_task, payload, persist=True))
                return
            if path == "/admin/overdue-checks":
                self._send_json(200, STORE.call(lab.run_overdue_checks, payload,
                                                actor=actor, persist=True))
                return
            if path == "/admin/retests/process-due":
                self._send_json(200, STORE.call(lab.process_due_retests, actor=actor,
                                                persist=True))
                return

            if path.startswith("/tasks/"):
                rest = path[len("/tasks/"):]
                if rest.endswith("/events"):
                    task_id = unquote(rest[: -len("/events")])
                    result = STORE.call(
                        lab.ingest_events, task_id, payload.get("events", []), persist=True
                    )
                    self._send_json(202, result)
                    return
                if rest.endswith("/complete"):
                    task_id = unquote(rest[: -len("/complete")].rstrip("/"))
                    self._send_json(200, STORE.call(lab.complete_task, task_id, persist=True))
                    return

            if path.startswith("/findings/") and path.endswith("/review"):
                finding_id = unquote(path[len("/findings/"):-len("/review")].rstrip("/"))
                self._send_json(200, STORE.call(lab.review_finding, finding_id, payload,
                                                actor=actor, persist=True))
                return

            if path.startswith("/relapses/") and path.endswith("/decision"):
                relapse_id = unquote(path[len("/relapses/"):-len("/decision")].rstrip("/"))
                self._send_json(200, STORE.call(lab.decide_relapse, relapse_id, payload,
                                                actor=actor, persist=True))
                return

            if path.startswith("/subjects/"):
                parts = path.split("/")
                # /subjects/{type}/{id}/{notices|retests|commitments}
                if len(parts) != 5 or parts[4] not in (
                        "notices", "retests", "commitments"):
                    self._json_404()
                    return
                subject_type, subject_id = parts[2], unquote(parts[3])
                if parts[4] == "notices":
                    self._send_json(
                        201,
                        STORE.call(lab.generate_notice, subject_type, subject_id, payload,
                                   actor=actor, persist=True),
                    )
                elif parts[4] == "commitments":
                    self._send_json(
                        201,
                        STORE.call(lab.submit_commitment, subject_type, subject_id, payload,
                                   actor=actor, persist=True),
                    )
                else:
                    self._send_json(
                        201,
                        STORE.call(lab.record_retest, subject_type, subject_id, payload,
                                   actor=actor, persist=True),
                    )
                return

            self._json_404()
        except (AccessDeniedError, NotFoundError, DomainError) as exc:
            self._send_error(exc)

    def log_message(self, *_args):
        return


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        Lab()  # 领域模块可实例化
        print("基础检查通过")
        return
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
