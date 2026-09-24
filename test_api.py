"""HTTP 端到端测试：完整跑通采集 → 判定 → 复核 → 告知 → 复测 → 回潮链路。"""

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


def call(method, url, body=None):
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json; charset=utf-8"} if data else {}
    request = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=3) as response:
            return response.status, json.load(response)
    except HTTPError as exc:
        return exc.code, json.load(exc)


def ad(ad_id, placement="splash"):
    return {"event_id": f"e-{ad_id}-shown", "seq": 1, "type": "ad_shown",
            "occurred_at": NOW + 100, "payload": {"ad_id": ad_id, "placement": placement}}


def close(ad_id, seq, after, size, reader=True):
    return {"event_id": f"e-{ad_id}-close-{seq}", "seq": seq, "type": "close_affordance",
            "occurred_at": NOW + 100 + after,
            "payload": {"ad_id": ad_id, "present": True, "visible_after_seconds": after,
                        "touch_target_dp": size, "screen_reader_actionable": reader}}


def jump(ad_id, seq, at, trigger, sdk=None, **extra):
    payload = {"ad_id": ad_id, "trigger": trigger, "target_url": "https://shop.example/p"}
    if sdk:
        payload["sdk"] = {"id": sdk, "name": "摇一摇SDK"}
    payload.update(extra)
    return {"event_id": f"e-{ad_id}-jump-{seq}", "seq": seq, "type": "jump",
            "occurred_at": at, "payload": payload}


class ApiFlowTest(unittest.TestCase):
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

    def _seed(self):
        self.post("/admin/regulations", 201, {
            "version": "v2025.1", "effective_at": 0, "params": {"rectification_days": 10}})
        self.post("/admin/scripts", 201,
                  {"script_id": "ad-trip", "version": "1.0", "created_at": NOW})
        self.post("/devices", 201,
                  {"device_id": "dev-A", "model": "Pixel 6", "os_version": "Android 12"})
        self.post("/devices", 201,
                  {"device_id": "dev-B", "model": "Pixel 8", "os_version": "Android 14"})
        self.post("/builds", 201, {
            "app_id": APP_ID, "app_name": "某新闻", "developer": "某新闻运营有限公司",
            "version_code": 1001, "version_name": "8.1.0"})

    def post(self, path, expected, body):
        status, payload = call("POST", self.base + path, body)
        self.assertEqual(status, expected, payload)
        return payload

    def get(self, path, expected=200):
        status, payload = call("GET", self.base + path)
        self.assertEqual(status, expected, payload)
        return payload

    def _create_task(self, device, track):
        return self.post("/tasks", 201,
                         {"build_id": f"{APP_ID}:1001", "device_id": device, "track": track})

    def test_full_enforcement_flow_over_http(self):
        self._seed()

        # 三条轨迹 + 设备 B 的并存任务
        normal = self._create_task("dev-A", "normal")
        reader = self._create_task("dev-A", "screen_reader")
        elder = self._create_task("dev-A", "elderly")
        normal_b = self._create_task("dev-B", "normal")
        for task in (normal, reader, elder, normal_b):
            self.assertEqual(task["regulation_version"], "v2025.1")
            self.assertEqual(task["script"], {"script_id": "ad-trip", "version": "1.0"})

        # 普通轨迹：关闭入口迟到 + SDK 自动跳转
        batch = [ad("a1"), close("a1", 2, after=5, size=36),
                 jump("a1", 3, NOW + 106, "auto", sdk=SDK_ID)]
        accepted = self.post(f"/tasks/{normal['task_id']}/events", 202, {"events": batch})
        self.assertEqual(len(accepted["accepted"]), 3)
        retried = self.post(f"/tasks/{normal['task_id']}/events", 202, {"events": batch})
        self.assertEqual(retried["accepted"], [])
        self.assertEqual(len(retried["duplicates"]), 3)  # 重传统一去重

        # 读屏轨迹：关闭入口不可聚焦
        self.post(f"/tasks/{reader['task_id']}/events", 202, {"events": [
            ad("a2"), close("a2", 2, after=1, size=48, reader=False)]})
        # 老人轨迹：点区不达标
        self.post(f"/tasks/{elder['task_id']}/events", 202, {"events": [
            ad("a3"), close("a3", 2, after=1, size=48)]})
        # 设备 B：合规
        self.post(f"/tasks/{normal_b['task_id']}/events", 202, {"events": [
            ad("b1"), close("b1", 2, after=1, size=48)]})
        for task in (normal, reader, elder, normal_b):
            self.post(f"/tasks/{task['task_id']}/complete", 200, {})

        report = self.get(f"/builds/{APP_ID}:1001/report")
        by_track = {(r["device_id"], r["track"]): r for r in report["tracks"]}
        self.assertEqual(by_track[("dev-A", "normal")]["suspected"], 2)
        self.assertEqual(by_track[("dev-A", "screen_reader")]["suspected"], 1)
        self.assertEqual(by_track[("dev-A", "elderly")]["suspected"], 1)
        self.assertEqual(by_track[("dev-B", "normal")]["suspected"], 0)

        # 复核：确认应用侧关闭路径问题，驳回 SDK 跳转问题
        normal_report = self.get(f"/tasks/{normal['task_id']}")
        close_finding = next(
            f for f in normal_report["findings"] if f["rule_id"] == "R-CLOSE-001")
        jump_finding = next(
            f for f in normal_report["findings"] if f["rule_id"] == "R-JUMP-001")
        self.assertEqual(close_finding["status"], "suspected")  # 自动规则只标涉嫌
        self.post(f"/findings/{close_finding['finding_id']}/review", 200, {
            "decision": "confirmed", "reviewer": "复核员乙", "comment": "关闭路径不可用"})
        self.post(f"/findings/{jump_finding['finding_id']}/review", 200, {
            "decision": "dismissed", "reviewer": "复核员乙", "comment": "确有后台返回异常"})

        # 未确认不得出告知材料
        status, body = call("POST", f"{self.base}/subjects/sdk/{SDK_ID}/notices",
                            {"issued_by": "承办人甲"})
        self.assertEqual(status, 404)

        notice = self.post(f"/subjects/app/{APP_ID}/notices", 201,
                           {"issued_by": "承办人甲"})
        self.assertEqual(len(notice["findings"]), 1)
        self.assertEqual(notice["rectification_days"], 10)
        evidence_types = {e["type"] for e in notice["findings"][0]["evidence"]}
        self.assertEqual(evidence_types, {"ad_shown", "close_affordance"})
        self.assertEqual(notice["regulation_versions"], ["v2025.1"])

        # 整改：新构建复测通过，问题时段保留
        self.post("/builds", 201, {
            "app_id": APP_ID, "app_name": "某新闻", "developer": "某新闻运营有限公司",
            "version_code": 1002, "version_name": "8.2.0"})
        retest_task = self.post("/tasks", 201, {
            "build_id": f"{APP_ID}:1002", "device_id": "dev-A", "track": "normal"})
        self.post(f"/tasks/{retest_task['task_id']}/events", 202,
                  {"events": [ad("c1"), close("c1", 2, after=1, size=48)]})
        self.post(f"/tasks/{retest_task['task_id']}/complete", 200, {})
        retest = self.post(f"/subjects/app/{APP_ID}/retests", 201,
                           {"task_id": retest_task["task_id"], "by": "复测员丙"})
        self.assertEqual(retest["result"], "passed")

        view = self.get(f"/subjects/app/{APP_ID}")
        self.assertEqual(view["status"], "rectified")
        self.assertEqual(view["relapse_count"], 0)
        self.assertEqual(view["cycles"][0]["problem_period"]["first_observed_at"], NOW + 100)
        self.assertEqual(view["cycles"][0]["notices"][0]["finding_count"], 1)
        # 复测固化脚本/规则/设备/构建摘要
        retest_snap = view["cycles"][0]["retests"][0]["snapshot"]
        self.assertEqual(retest_snap["regulation_version"], "v2025.1")
        self.assertEqual(retest_snap["script"]["version"], "1.0")
        self.assertEqual(retest_snap["build"]["build_id"], f"{APP_ID}:1002")
        self.assertEqual(retest_snap["device"]["device_id"], "dev-A")

        # 回潮：1003 版恢复旧行为
        self.post("/builds", 201, {
            "app_id": APP_ID, "app_name": "某新闻", "developer": "某新闻运营有限公司",
            "version_code": 1003, "version_name": "8.3.0"})
        relapse_task = self.post("/tasks", 201, {
            "build_id": f"{APP_ID}:1003", "device_id": "dev-A", "track": "elderly"})
        self.post(f"/tasks/{relapse_task['task_id']}/events", 202,
                  {"events": [ad("d1"), close("d1", 2, after=6, size=40)]})
        self.post(f"/tasks/{relapse_task['task_id']}/complete", 200, {})
        relapse_report = self.get(f"/tasks/{relapse_task['task_id']}")
        relapse_finding = relapse_report["findings"][0]
        self.post(f"/findings/{relapse_finding['finding_id']}/review", 200,
                  {"decision": "confirmed", "reviewer": "复核员乙"})

        view = self.get(f"/subjects/app/{APP_ID}")
        self.assertEqual(view["status"], "open")
        self.assertEqual(view["relapse_count"], 0)  # 人工确认前不计数
        self.assertEqual(len(view["cycles"]), 2)
        self.assertEqual(view["current_cycle_seq"], 2)
        # 旧周期完整保留，旧构建证据仍可查
        self.assertEqual(view["cycles"][0]["status"], "rectified")
        old = self.get(f"/tasks/{normal['task_id']}")
        self.assertEqual(old["build"]["version_code"], 1001)

        # 系统按行为指纹提议复发，审核员确认后才计数；责任方无权确认（403）
        self.post("/actors", 201, {"actor_id": "rev", "role": "reviewer", "name": "复核员乙"})
        self.post("/actors", 201, {"actor_id": "owner", "role": "responsible",
                                   "subjects": [{"type": "app", "id": APP_ID}]})
        proposals = self.post(f"/builds/{APP_ID}:1003/relapse-proposals", 201, {})
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0]["status"], "proposed")
        relapse_id = proposals[0]["relapse_id"]
        status, _ = call("POST", f"{self.base}/relapses/{relapse_id}/decision",
                         {"actor_id": "owner", "decision": "confirmed"})
        self.assertEqual(status, 403)
        self.post(f"/relapses/{relapse_id}/decision", 200,
                  {"actor_id": "rev", "decision": "confirmed", "comment": "回潮成立"})
        view = self.get(f"/subjects/app/{APP_ID}")
        self.assertEqual(view["relapse_count"], 1)

        # 监管构建视角：复发依据与通知回执
        oversight = self.get(f"/builds/{APP_ID}:1003/oversight?actor_id=rev")
        self.assertTrue(any(r["relapse_id"] == relapse_id
                            for r in oversight["relapse_links"]))
        self.assertTrue(oversight["notification_receipts"])

        # 复测幂等：同一复测任务并发/重复提交只产生一次记录
        dup1 = self.post(f"/subjects/app/{APP_ID}/retests", 201,
                         {"task_id": relapse_task["task_id"]})
        dup2 = self.post(f"/subjects/app/{APP_ID}/retests", 201,
                         {"task_id": relapse_task["task_id"]})
        self.assertEqual(dup2["retest_id"], dup1["retest_id"])
        view = self.get(f"/subjects/app/{APP_ID}")
        self.assertEqual(len(view["cycles"][1]["retests"]), 1)

        # 责任方只能取自己名下材料，越权查看返回 403
        mine = self.get("/materials?actor_id=owner")
        self.assertEqual(len(mine["subjects"]), 1)
        status, _ = call("GET", f"{self.base}/subjects/sdk/{SDK_ID}?actor_id=owner")
        self.assertEqual(status, 403)

        # 非法输入与未知路由
        status, body = call("POST", f"{self.base}/tasks",
                            {"build_id": "missing", "device_id": "dev-A", "track": "normal"})
        self.assertEqual(status, 404)
        status, _ = call("GET", f"{self.base}/unknown")
        self.assertEqual(status, 404)

    def test_commitment_item_close_and_overdue_escalation(self):
        self._seed()
        task = self._create_task("dev-A", "normal")
        # 两条广告位各有一处关闭路径缺陷，均归应用方 -> 两个整改项
        self.post(f"/tasks/{task['task_id']}/events", 202, {"events": [
            ad("a1"), close("a1", 2, after=5, size=36),
            ad("a4", placement="interstitial"),
            close("a4", 6, after=6, size=30)]})
        self.post(f"/tasks/{task['task_id']}/complete", 200, {})
        report = self.get(f"/tasks/{task['task_id']}")
        close_findings = [f for f in report["findings"] if f["rule_id"] == "R-CLOSE-001"]
        self.assertEqual(len(close_findings), 2)
        for f in close_findings:
            self.post(f"/findings/{f['finding_id']}/review", 200,
                      {"decision": "confirmed", "reviewer": "复核员乙"})
        self.post(f"/subjects/app/{APP_ID}/notices", 201, {"issued_by": "承办人甲"})
        view = self.get(f"/subjects/app/{APP_ID}")
        items = view["cycles"][0]["items"]
        self.assertEqual(len(items), 2)
        close_item = items[0]
        other_item = items[1]

        # 承诺一条已逾期（补登承诺时间在过去），另一条不承诺
        self.post("/builds", 201, {
            "app_id": APP_ID, "app_name": "某新闻", "developer": "某新闻运营有限公司",
            "version_code": 1002, "version_name": "8.2.0"})
        item_resp = self.post(f"/items/{close_item['item_id']}/commitments", 201, {
            "target_build_id": f"{APP_ID}:1002", "target_track": "normal",
            "rectification_days": 2, "committed_by": "整改负责人丁",
            "committed_at": NOW})  # NOW+2d 早于当前真实时间，已逾期
        self.assertEqual(item_resp["status"], "committed")

        # 逾期扫描：只升级关闭路径这一项；重复扫描不重复升级
        scan = self.post("/escalations/scan", 200, {"issued_by": "督办系统"})
        self.assertEqual(scan["escalated_count"], 1)
        self.assertEqual(scan["escalated"][0]["item_id"], close_item["item_id"])
        self.assertEqual(scan["escalated"][0]["subject"]["id"], APP_ID)
        self.assertEqual(self.post("/escalations/scan", 200, {})["escalated_count"], 0)
        view = self.get(f"/subjects/app/{APP_ID}")
        closed = next(i for i in view["cycles"][0]["items"]
                      if i["item_id"] == close_item["item_id"])
        self.assertEqual(len(closed["escalations"]), 1)
        self.assertTrue(any(n["kind"] == "escalation" for n in view["notifications"]))

        # 定向复测只关闭承诺项，周期保持开启
        rt = self.post("/tasks", 201,
                       {"build_id": f"{APP_ID}:1002", "device_id": "dev-A", "track": "normal"})
        self.post(f"/tasks/{rt['task_id']}/events", 202,
                  {"events": [ad("c1"), close("c1", 2, after=1, size=48)]})
        self.post(f"/tasks/{rt['task_id']}/complete", 200, {})
        retest = self.post(f"/subjects/app/{APP_ID}/retests", 201,
                           {"task_id": rt["task_id"]})
        self.assertEqual(retest["passed_item_ids"], [close_item["item_id"]])
        self.assertNotIn(other_item["item_id"], retest["passed_item_ids"])
        view = self.get(f"/subjects/app/{APP_ID}")
        self.assertEqual(view["cycles"][0]["status"], "open")

    def test_snapshot_file_persistence(self):
        fd, path = tempfile.mkstemp(prefix="lab-", suffix=".json")
        os.close(fd)
        os.unlink(path)
        try:
            service.reset_store(path)
            self.post("/admin/regulations", 201,
                      {"version": "v2025.1", "effective_at": 0})
            self.post("/devices", 201,
                      {"device_id": "dev-A", "model": "Pixel 6", "os_version": "Android 12"})
            # 用同一文件重建仓储，证据不丢
            service.reset_store(path)
            status, _ = call("POST", f"{self.base}/devices",
                             {"device_id": "dev-A", "model": "Pixel 6",
                              "os_version": "Android 12"})
            self.assertEqual(status, 400)  # 设备已从快照恢复，重复登记被拒
        finally:
            if os.path.exists(path):
                os.unlink(path)


if __name__ == "__main__":
    unittest.main()
