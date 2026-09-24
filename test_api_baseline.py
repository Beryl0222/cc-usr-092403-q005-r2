"""HTTP 端到端测试（整改基线扩展）。

角色头、承诺路由、复测幂等、复发审核授权、逾期升级回执、监管 oversight 视图、
责任方材料隔离与重启后待复测接续。
"""

import json
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import service
from service import Handler

NOW = 1_700_000_000
APP_ID = "com.example.news"
SDK_ID = "shake-sdk-9"

REGULATOR = {"X-Role": "regulator"}
REVIEWER = {"X-Role": "reviewer", "X-Actor-Id": "reviewer-yi"}
APP_PARTY = {"X-Role": "party", "X-Subject-Type": "app",
             "X-Subject-Id": APP_ID, "X-Actor-Id": "app-contact"}
OTHER_PARTY = {"X-Role": "party", "X-Subject-Type": "advertiser",
               "X-Subject-Id": "ad-brand-x"}


def call(method, url, body=None, headers=None):
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    hdrs = {"Content-Type": "application/json; charset=utf-8"} if data else {}
    if headers:
        hdrs.update(headers)
    request = Request(url, data=data, headers=hdrs, method=method)
    try:
        with urlopen(request, timeout=3) as response:
            return response.status, json.load(response)
    except HTTPError as exc:
        return exc.code, json.load(exc)


def ad(ad_id):
    return {"event_id": f"e-{ad_id}-shown", "seq": 1, "type": "ad_shown",
            "occurred_at": NOW + 100, "payload": {"ad_id": ad_id, "placement": "splash"}}


def close(ad_id, seq, after, size):
    return {"event_id": f"e-{ad_id}-close-{seq}", "seq": seq, "type": "close_affordance",
            "occurred_at": NOW + 100 + after,
            "payload": {"ad_id": ad_id, "present": True, "visible_after_seconds": after,
                        "touch_target_dp": size, "screen_reader_actionable": True}}


class BaselineApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        service.reset_store()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self):
        service.reset_store()

    def post(self, path, expected, body, headers=REGULATOR):
        status, payload = call("POST", self.base + path, body, headers)
        self.assertEqual(status, expected, payload)
        return payload

    def get(self, path, expected=200, headers=REGULATOR):
        status, payload = call("GET", self.base + path, None, headers)
        self.assertEqual(status, expected, payload)
        return payload

    def _seed(self):
        self.post("/admin/regulations", 201,
                  {"version": "v2025.1", "effective_at": 0,
                   "params": {"rectification_days": 10}})
        self.post("/admin/scripts", 201,
                  {"script_id": "ad-trip", "version": "1.0", "created_at": NOW})
        self.post("/devices", 201,
                  {"device_id": "dev-A", "model": "Pixel 6", "os_version": "Android 12"})
        self.post("/builds", 201, {
            "app_id": APP_ID, "app_name": "某新闻", "developer": "某新闻运营有限公司",
            "version_code": 1001, "version_name": "8.1.0",
            "components": [{"component_id": "comp-ad-1", "kind": "sdk",
                            "name": "某广告SDK", "version": "1.0"}],
            "fingerprints": [{"type": "behavior",
                              "value": "behavior:R-CLOSE-001:splash"}]})
        self.post("/builds", 201, {
            "app_id": APP_ID, "app_name": "某新闻", "developer": "某新闻运营有限公司",
            "version_code": 1002, "version_name": "8.2.0",
            "components": [{"component_id": "comp-ad-2", "kind": "sdk",
                            "name": "某广告SDK", "version": "2.0"}]})

    def _confirmed_close(self, build, ad_id, after=5, size=36):
        task = self.post("/tasks", 201,
                         {"build_id": build, "device_id": "dev-A", "track": "normal"})
        self.post(f"/tasks/{task['task_id']}/events", 202,
                  {"events": [ad(ad_id), close(ad_id, 2, after, size)]})
        self.post(f"/tasks/{task['task_id']}/complete", 200, {}, REVIEWER)
        report = self.get(f"/tasks/{task['task_id']}", 200, REVIEWER)
        finding = report["findings"][0]
        self.post(f"/findings/{finding['finding_id']}/review", 200,
                  {"decision": "confirmed", "reviewer": "复核员乙"}, REVIEWER)
        return task["task_id"], finding["finding_id"]

    def _clean_build_task(self, code, ad_id, register=True):
        if register:
            self.post("/builds", 201, {
                "app_id": APP_ID, "app_name": "某新闻", "developer": "某新闻运营有限公司",
                "version_code": code, "version_name": f"8.{code}"})
        task = self.post("/tasks", 201,
                         {"build_id": f"{APP_ID}:{code}", "device_id": "dev-A",
                          "track": "normal"})
        self.post(f"/tasks/{task['task_id']}/events", 202,
                  {"events": [ad(ad_id), close(ad_id, 2, 1, 48)]})
        self.post(f"/tasks/{task['task_id']}/complete", 200, {}, REVIEWER)
        return task["task_id"]

    def test_role_headers_enforce_party_isolation(self):
        self._seed()
        # party 角色缺少主体头：403
        status, body = call("GET", f"{self.base}/subjects/app/{APP_ID}",
                            headers={"X-Role": "party"})
        self.assertEqual(status, 403)
        # 广告主不能看应用的案件/材料
        self.assertEqual(call("GET", f"{self.base}/subjects/app/{APP_ID}",
                              headers=OTHER_PARTY)[0], 403)
        self.assertEqual(call("GET", f"{self.base}/subjects/app/{APP_ID}/materials",
                              headers=OTHER_PARTY)[0], 403)
        # 责任方不能复核
        _, finding_id = self._confirmed_close(f"{APP_ID}:1001", "a1")
        status, _ = call("POST", f"{self.base}/findings/{finding_id}/review",
                         {"decision": "confirmed", "reviewer": "x"}, APP_PARTY)
        # 该发现已由复核员确认；重复确认或越权都应失败，越权优先 403
        self.assertEqual(status, 403)

    def test_commitment_retest_baseline_and_idempotency_over_http(self):
        self._seed()
        _, finding_id = self._confirmed_close(f"{APP_ID}:1001", "a1")
        notice = self.post(f"/subjects/app/{APP_ID}/notices", 201,
                           {"issued_by": "承办人甲"}, REVIEWER)

        # 责任方自行提交承诺：责任方、目标构建、轨道、期限齐备
        commitment = self.post(f"/subjects/app/{APP_ID}/commitments", 201, {
            "finding_id": finding_id, "target_build_id": f"{APP_ID}:1002",
            "target_tracks": ["normal", "elderly"],
            "deadline": notice["rectification_deadline"]}, APP_PARTY)
        self.assertEqual(commitment["target_tracks"], ["normal", "elderly"])
        # 广告主不能替应用承诺
        self.assertEqual(call("POST", f"{self.base}/subjects/app/{APP_ID}/commitments",
                              {"finding_id": finding_id,
                               "target_build_id": f"{APP_ID}:1002"}, OTHER_PARTY)[0], 403)

        # 复测固化基线
        retest_task = self.post("/tasks", 201,
                                {"build_id": f"{APP_ID}:1002", "device_id": "dev-A",
                                 "track": "normal"})
        self.post(f"/tasks/{retest_task['task_id']}/events", 202,
                  {"events": [ad("c1"), close("c1", 2, 1, 48)]})
        self.post(f"/tasks/{retest_task['task_id']}/complete", 200, {}, REVIEWER)
        retest = self.post(f"/subjects/app/{APP_ID}/retests", 201, {
            "task_id": retest_task["task_id"], "idempotency_key": "up-1"}, REVIEWER)
        self.assertEqual(retest["result"], "passed")
        self.assertEqual(retest["baseline"]["script"]["version"], "1.0")
        self.assertEqual(retest["baseline"]["build"]["components"][0]["component_id"],
                         "comp-ad-2")

        # 重复上传与离线补传（不同幂等键）只产生一次推进
        again = self.post(f"/subjects/app/{APP_ID}/retests", 201, {
            "task_id": retest_task["task_id"], "idempotency_key": "up-1"}, REVIEWER)
        self.assertEqual(again["retest_id"], retest["retest_id"])
        offline = self.post(f"/subjects/app/{APP_ID}/retests", 201, {
            "task_id": retest_task["task_id"], "idempotency_key": "offline-2"}, REVIEWER)
        self.assertEqual(offline["retest_id"], retest["retest_id"])

        view = self.get(f"/subjects/app/{APP_ID}", 200, REVIEWER)
        self.assertEqual(view["status"], "rectified")
        self.assertEqual(len(view["cycles"][-1]["retests"]), 1)
        # 责任方可以拿到自己的材料（含回执）
        materials = self.get(f"/subjects/app/{APP_ID}/materials", 200, APP_PARTY)
        self.assertTrue(materials["cycles"][-1]["notifications"])
        self.assertEqual(materials["cycles"][-1]["retests"][0]["baseline"]["regulation_version"],
                         "v2025.1")

    def test_relapse_decision_requires_privilege(self):
        self._seed()
        _, finding_id = self._confirmed_close(f"{APP_ID}:1001", "a1")
        self.post(f"/subjects/app/{APP_ID}/notices", 201, {"issued_by": "承办人甲"}, REVIEWER)
        self.post(f"/subjects/app/{APP_ID}/commitments", 201, {
            "finding_id": finding_id, "target_build_id": f"{APP_ID}:1001"}, APP_PARTY)
        clean_task = self._clean_build_task(1002, "c1", register=False)
        self.post(f"/subjects/app/{APP_ID}/retests", 201,
                  {"task_id": clean_task}, REVIEWER)

        # 新版复用旧组件：确认后产生复发提议
        self.post("/builds", 201, {
            "app_id": APP_ID, "app_name": "某新闻", "developer": "某新闻运营有限公司",
            "version_code": 1003, "components": [
                {"component_id": "comp-ad-1", "kind": "sdk", "name": "某广告SDK"}]})
        _, relapsed_id = self._confirmed_close(f"{APP_ID}:1003", "d1", after=6, size=40)
        report = self.get(f"/subjects/app/{APP_ID}", 200, REVIEWER)
        relapse = report["cycles"][-1]["relapses"][0]
        self.assertEqual(relapse["status"], "proposed")
        self.assertIn("component", {b["kind"] for b in relapse["bases"]})

        # 责任方确认被拒
        status, _ = call("POST", f"{self.base}/relapses/{relapse['relapse_id']}/decision",
                         {"decision": "confirmed", "reviewer": "应用自己"}, APP_PARTY)
        self.assertEqual(status, 403)
        # 授权审核人员确认
        decided = self.post(f"/relapses/{relapse['relapse_id']}/decision", 200, {
            "decision": "confirmed", "reviewer": "审核主管丁"}, REVIEWER)
        self.assertEqual(decided["status"], "confirmed")

        # 监管构建视图可见复发待决/依据（在新构建上再提一例）
        oversight = self.get(f"/builds/{APP_ID}:1003/oversight", 200, REVIEWER)
        self.assertEqual(oversight["findings"][0]["relapse"]["new_build_id"],
                         f"{APP_ID}:1003")
        self.assertEqual(call("GET", f"{self.base}/builds/{APP_ID}:1003/oversight",
                              headers=APP_PARTY)[0], 403)

    def test_overdue_escalation_once_and_receipts_visible(self):
        self._seed()
        _, finding_id = self._confirmed_close(f"{APP_ID}:1001", "a1")
        self.post(f"/subjects/app/{APP_ID}/notices", 201, {"issued_by": "承办人甲"}, REVIEWER)
        self.post(f"/subjects/app/{APP_ID}/commitments", 201, {
            "finding_id": finding_id, "target_build_id": f"{APP_ID}:1002",
            "deadline": NOW + 5 * 86400}, APP_PARTY)

        # 未逾期：不升级
        first = self.post("/admin/overdue-checks", 200, {"now": NOW + 4 * 86400}, REVIEWER)
        self.assertEqual(first["escalated"], [])
        # 逾期：只升级这一个责任方一次
        second = self.post("/admin/overdue-checks", 200,
                           {"now": NOW + 6 * 86400}, REVIEWER)
        self.assertEqual(len(second["escalated"]), 1)
        third = self.post("/admin/overdue-checks", 200,
                          {"now": NOW + 7 * 86400}, REVIEWER)
        self.assertEqual(third["escalated"], [])
        # 责任方不能触发扫描
        self.assertEqual(call("POST", f"{self.base}/admin/overdue-checks",
                              {"now": NOW + 99 * 86400}, APP_PARTY)[0], 403)

        view = self.get(f"/subjects/app/{APP_ID}", 200, REVIEWER)
        self.assertEqual(len(view["overdue_commitments"]), 1)
        notifications = view["cycles"][-1]["notifications"]
        kinds = {n["kind"] for n in notifications}
        self.assertEqual(kinds, {"notice_delivered", "escalation_overdue"})
        # 回执永久保留且只送达本责任方
        self.assertTrue(all(n["recipient"]["id"] == APP_ID for n in notifications))

    def test_pending_retest_survives_restart_and_resumes(self):
        fd, path = tempfile.mkstemp(prefix="lab-", suffix=".json")
        os.close(fd)
        os.unlink(path)
        try:
            service.reset_store(path)
            self.post("/admin/regulations", 201,
                      {"version": "v2025.1", "effective_at": 0})
            self.post("/admin/scripts", 201,
                      {"script_id": "ad-trip", "version": "1.0", "created_at": NOW})
            self.post("/devices", 201,
                      {"device_id": "dev-A", "model": "Pixel 6", "os_version": "Android 12"})
            self.post("/builds", 201, {
                "app_id": APP_ID, "app_name": "某新闻", "developer": "某新闻运营有限公司",
                "version_code": 1001})
            self.post("/builds", 201, {
                "app_id": APP_ID, "app_name": "某新闻", "developer": "某新闻运营有限公司",
                "version_code": 1002})
            _, finding_id = self._confirmed_close(f"{APP_ID}:1001", "a1")
            self.post(f"/subjects/app/{APP_ID}/notices", 201,
                      {"issued_by": "承办人甲"}, REVIEWER)
            self.post(f"/subjects/app/{APP_ID}/commitments", 201, {
                "finding_id": finding_id, "target_build_id": f"{APP_ID}:1002"}, APP_PARTY)

            # 复测任务创建后未完成就登记复测：进入待复测队列
            task = self.post("/tasks", 201,
                             {"build_id": f"{APP_ID}:1002", "device_id": "dev-A",
                              "track": "normal"})
            queued = self.post(f"/subjects/app/{APP_ID}/retests", 201,
                               {"task_id": task["task_id"], "idempotency_key": "k1"},
                               REVIEWER)
            self.assertEqual(queued["result"], "pending")

            # 服务重启：待复测事项保留
            service.reset_store(path)
            view = self.get(f"/subjects/app/{APP_ID}", 200, REVIEWER)
            self.assertEqual(len(view["pending_retests"]), 1)

            # 离线补传完成采集：自动接续，周期关闭
            self.post(f"/tasks/{task['task_id']}/events", 202,
                      {"events": [ad("c1"), close("c1", 2, 1, 48)]})
            self.post(f"/tasks/{task['task_id']}/complete", 200, {}, REVIEWER)
            view = self.get(f"/subjects/app/{APP_ID}", 200, REVIEWER)
            self.assertEqual(view["status"], "rectified")
            self.assertEqual(view["pending_retests"], [])
            self.assertEqual(len(view["cycles"][-1]["retests"]), 1)

            # 再次重启不重复推进
            service.reset_store(path)
            view = self.get(f"/subjects/app/{APP_ID}", 200, REVIEWER)
            self.assertEqual(len(view["cycles"][-1]["retests"]), 1)
        finally:
            if os.path.exists(path):
                os.unlink(path)


if __name__ == "__main__":
    unittest.main()
