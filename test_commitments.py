"""整改基线扩展的领域测试。

覆盖：整改承诺、复测固化、单项关闭、复测幂等与接续、复发关联提议制、
逾期升级、通知回执只增、监管视图与角色权限。
"""

import unittest

from domain import (
    COMMITMENT_FULFILLED,
    COMMITMENT_OPEN,
    FINDING_CONFIRMED,
    RELAPSE_CONFIRMED,
    RELAPSE_PROPOSED,
    RELAPSE_REJECTED,
    RETEST_PASSED,
    RETEST_PENDING,
    ROLE_PARTY,
    ROLE_REVIEWER,
    RULE_AUTO_JUMP,
    RULE_NO_CLOSE_PATH,
    SUBJECT_APP,
    SUBJECT_SDK,
    AccessDeniedError,
    DomainError,
    Lab,
    NotFoundError,
)

NOW = 1_700_000_000
APP_ID = "com.example.news"
SDK_ID = "shake-sdk-9"
DEV_A = "device-A"
DEV_B = "device-B"

REVIEWER = {"role": ROLE_REVIEWER, "id": "复核员乙"}
APP_PARTY = {"role": ROLE_PARTY, "subject_type": SUBJECT_APP,
             "subject_id": APP_ID, "id": "应用整改联系人"}
SDK_PARTY = {"role": ROLE_PARTY, "subject_type": SUBJECT_SDK,
             "subject_id": SDK_ID, "id": "SDK整改联系人"}
OTHER_PARTY = {"role": ROLE_PARTY, "subject_type": "advertiser",
               "subject_id": "ad-brand-x", "id": "广告主联系人"}


class FakeClock:
    def __init__(self, start=NOW):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds
        return self.t


def ad(ad_id, placement="splash", advertiser=None):
    payload = {"ad_id": ad_id, "placement": placement}
    if advertiser:
        payload["advertiser"] = {"id": advertiser}
    return {"event_id": f"e-{ad_id}-shown", "seq": 1, "type": "ad_shown",
            "occurred_at": NOW + 100, "payload": payload}


def close(ad_id, seq, after, size, reader=True):
    return {"event_id": f"e-{ad_id}-close-{seq}", "seq": seq, "type": "close_affordance",
            "occurred_at": NOW + 100 + after,
            "payload": {"ad_id": ad_id, "present": True, "visible_after_seconds": after,
                        "touch_target_dp": size, "screen_reader_actionable": reader,
                        "label": "跳过"}}


def jump(ad_id, seq, at, trigger="auto", target="https://x.example/", sdk=None,
         advertiser=None, **sensor):
    payload = {"ad_id": ad_id, "trigger": trigger, "target_url": target}
    if sdk:
        payload["sdk"] = {"id": sdk, "name": "摇一摇SDK"}
    if advertiser:
        payload["advertiser"] = {"id": advertiser}
    payload.update(sensor)
    return {"event_id": f"e-{ad_id}-jump-{seq}", "seq": seq, "type": "jump",
            "occurred_at": at, "payload": payload}


def sensor(ad_id, seq, at, accel=8, rotation=10, seconds=1):
    return {"event_id": f"e-{ad_id}-sensor-{seq}", "seq": seq, "type": "sensor_reading",
            "occurred_at": at,
            "payload": {"ad_id": ad_id, "peak_acceleration": accel,
                        "peak_rotation_deg": rotation, "reading_seconds": seconds}}


COMP_CLOSE_OLD = {"component_id": "comp-ad-sdk-1", "kind": "sdk",
                  "name": "某广告SDK", "version": "1.0"}
COMP_CLOSE_NEW = {"component_id": "comp-ad-sdk-2", "kind": "sdk",
                  "name": "某广告SDK", "version": "2.0"}
FP_CLOSE = {"type": "behavior", "value": "behavior:R-CLOSE-001:splash"}


class CommitmentBaselineTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.lab = self._seed()

    def _seed(self):
        lab = Lab(clock=self.clock)
        lab.register_regulation({
            "version": "v2025.1", "effective_at": 0,
            "params": {"rectification_days": 10}})
        lab.register_script({"script_id": "ad-trip", "version": "1.0",
                             "created_at": self.clock()})
        lab.register_device({"device_id": DEV_A, "model": "Pixel 6",
                             "os_version": "Android 12"})
        lab.register_device({"device_id": DEV_B, "model": "Pixel 8",
                             "os_version": "Android 14"})
        lab.register_build({
            "app_id": APP_ID, "app_name": "某新闻", "developer": "某新闻运营有限公司",
            "version_code": 1001, "version_name": "8.1.0",
            "components": [COMP_CLOSE_OLD], "fingerprints": [FP_CLOSE]})
        lab.register_build({
            "app_id": APP_ID, "app_name": "某新闻", "developer": "某新闻运营有限公司",
            "version_code": 1002, "version_name": "8.2.0",
            "components": [COMP_CLOSE_NEW]})
        return lab

    def _confirmed_findings(self, build="com.example.news:1001", device=DEV_A,
                            events=None):
        task = self.lab.create_task(
            {"build_id": build, "device_id": device, "track": "normal"})
        self.lab.ingest_events(task["task_id"], events)
        self.lab.complete_task(task["task_id"])
        confirmed = []
        for f in list(self.lab.findings.values()):
            if f["task_id"] == task["task_id"]:
                self.lab.review_finding(f["finding_id"],
                                        {"decision": FINDING_CONFIRMED,
                                         "reviewer": "复核员乙"}, actor=REVIEWER)
                confirmed.append(self.lab.findings[f["finding_id"]])
        return task, confirmed

    def _app_case(self):
        task, findings = self._confirmed_findings(events=[
            ad("a1"), close("a1", 2, after=5, size=36)])
        notice = self.lab.generate_notice(
            SUBJECT_APP, APP_ID, {"issued_by": "承办人甲"}, actor=REVIEWER)
        return task, findings, notice

    def _commitment(self, finding, target="com.example.news:1002",
                    deadline=None, tracks=None, actor=APP_PARTY):
        payload = {"finding_id": finding["finding_id"], "target_build_id": target,
                   "promised_by": "应用整改联系人"}
        if deadline is not None:
            payload["deadline"] = deadline
        if tracks is not None:
            payload["target_tracks"] = tracks
        return self.lab.submit_commitment(
            SUBJECT_APP, APP_ID, payload, actor=actor)

    def _clean_retest_task(self, device=DEV_A, build="com.example.news:1002",
                           ad_id="c1"):
        task = self.lab.create_task(
            {"build_id": build, "device_id": device, "track": "normal"})
        self.lab.ingest_events(task["task_id"],
                               [ad(ad_id), close(ad_id, 2, after=1, size=48)])
        self.lab.complete_task(task["task_id"])
        return task

    # ---------------------------------------------------------------- #

    def test_commitment_connects_finding_party_build_tracks_and_deadline(self):
        _, findings, notice = self._app_case()
        commitment = self._commitment(findings[0], tracks=["normal", "elderly"])
        self.assertEqual(commitment["status"], COMMITMENT_OPEN)
        self.assertEqual(commitment["finding_id"], findings[0]["finding_id"])
        self.assertEqual(commitment["target_build_id"], "com.example.news:1002")
        self.assertEqual(commitment["target_tracks"], ["normal", "elderly"])
        # 未显式给定期限时取告知材料期限
        self.assertEqual(commitment["deadline"], notice["rectification_deadline"])
        self.assertEqual(commitment["responsible_subject"]["id"], APP_ID)
        self.assertEqual(commitment["source_notice_id"], notice["notice_id"])

        view = self.lab.subject_view(SUBJECT_APP, APP_ID, actor=REVIEWER)
        self.assertEqual([c["commitment_id"] for c in view["open_commitments"]],
                         [commitment["commitment_id"]])

        # 同一问题未履行前禁止重复承诺
        with self.assertRaises(DomainError):
            self._commitment(findings[0])
        # 广告主不能替应用提交承诺
        with self.assertRaises(AccessDeniedError):
            self._commitment(findings[0], actor=OTHER_PARTY)

    def test_single_item_closure_does_not_close_cycle(self):
        _, findings, _ = self._app_case()
        # 同一任务再补一个归应用的自动跳转发现，形成两条待整改问题
        task2 = self.lab.create_task(
            {"build_id": "com.example.news:1001", "device_id": DEV_B, "track": "normal"})
        self.lab.ingest_events(task2["task_id"], [
            ad("a9"), close("a9", 2, after=1, size=48),
            jump("a9", 3, NOW + 106)])
        self.lab.complete_task(task2["task_id"])
        jump_finding = next(f for f in self.lab.findings.values()
                            if f["task_id"] == task2["task_id"]
                            and f["rule_id"] == RULE_AUTO_JUMP)
        self.assertEqual(jump_finding["responsible_subject"]["type"], SUBJECT_APP)
        self.lab.review_finding(jump_finding["finding_id"],
                                {"decision": FINDING_CONFIRMED, "reviewer": "复核员乙"},
                                actor=REVIEWER)

        c_close = self._commitment(findings[0])
        c_jump = self._commitment(jump_finding)
        self.assertEqual(len(self.lab.subject_view(SUBJECT_APP, APP_ID, actor=REVIEWER)
                             ["cycles"][-1]["commitments"]), 2)

        # 第一次复测只验关闭路径承诺：单项关闭，周期不结束
        retest_task = self._clean_retest_task(device=DEV_A, ad_id="c1")
        first = self.lab.record_retest(SUBJECT_APP, APP_ID, {
            "task_id": retest_task["task_id"], "commitment_ids": [c_close["commitment_id"]],
            "by": "复测员丙"}, actor=REVIEWER)
        self.assertEqual(first["result"], RETEST_PASSED)
        self.assertEqual(self.lab.commitments[c_close["commitment_id"]]["status"],
                         COMMITMENT_FULFILLED)
        self.assertEqual(self.lab.commitments[c_jump["commitment_id"]]["status"],
                         COMMITMENT_OPEN)
        view = self.lab.subject_view(SUBJECT_APP, APP_ID, actor=REVIEWER)
        self.assertEqual(view["status"], "open")
        self.assertEqual(len(view["cycles"][-1]["retests"]), 1)

        # 第二次复测验剩余承诺：全部履行后周期才关闭
        retest_task2 = self._clean_retest_task(device=DEV_B, ad_id="c2")
        second = self.lab.record_retest(SUBJECT_APP, APP_ID, {
            "task_id": retest_task2["task_id"], "commitment_ids": [c_jump["commitment_id"]]},
            actor=REVIEWER)
        self.assertEqual(second["result"], RETEST_PASSED)
        view = self.lab.subject_view(SUBJECT_APP, APP_ID, actor=REVIEWER)
        self.assertEqual(view["status"], "rectified")
        self.assertEqual(view["cycles"][-1]["status"], "rectified")

    def test_retest_pins_script_rule_device_and_build_summary(self):
        _, findings, _ = self._app_case()
        self._commitment(findings[0])
        retest_task = self._clean_retest_task()
        retest = self.lab.record_retest(SUBJECT_APP, APP_ID, {
            "task_id": retest_task["task_id"]}, actor=REVIEWER)
        baseline = retest["baseline"]
        self.assertEqual(baseline["script"], {"script_id": "ad-trip", "version": "1.0"})
        self.assertEqual(baseline["regulation_version"], "v2025.1")
        self.assertEqual(baseline["device"]["model"], "Pixel 6")
        self.assertEqual(baseline["build"]["version_code"], 1002)
        self.assertEqual(baseline["build"]["components"][0]["component_id"], "comp-ad-sdk-2")
        self.assertEqual(baseline["build"]["fingerprints"], [])
        self.assertEqual(baseline["track"], "normal")

    def test_retest_is_idempotent_across_reupload_and_concurrent_keys(self):
        _, findings, _ = self._app_case()
        self._commitment(findings[0])
        retest_task = self._clean_retest_task()
        payload = {"task_id": retest_task["task_id"], "idempotency_key": "client-key-7"}

        first = self.lab.record_retest(SUBJECT_APP, APP_ID, payload, actor=REVIEWER)
        # 重复上传：同一幂等键原样返回首次结果
        second = self.lab.record_retest(SUBJECT_APP, APP_ID, payload, actor=REVIEWER)
        self.assertEqual(second["retest_id"], first["retest_id"])
        # 离线补传：换了幂等键，但同一（主体，任务）只推进一次
        third = self.lab.record_retest(
            SUBJECT_APP, APP_ID,
            {"task_id": retest_task["task_id"], "idempotency_key": "offline-key-9"},
            actor=REVIEWER)
        self.assertEqual(third["retest_id"], first["retest_id"])
        cycle = self.lab.subject_view(SUBJECT_APP, APP_ID, actor=REVIEWER)["cycles"][-1]
        self.assertEqual(len(cycle["retests"]), 1)

    def test_uncovered_confirmed_finding_keeps_cycle_open(self):
        _, findings, _ = self._app_case()
        commitment = self._commitment(findings[0])
        # 复测构建上出现另一条无承诺覆盖的应用侧已确认问题
        retest_task = self.lab.create_task(
            {"build_id": "com.example.news:1002", "device_id": DEV_A, "track": "normal"})
        self.lab.ingest_events(retest_task["task_id"], [
            ad("z1"), close("z1", 2, after=1, size=48),
            jump("z1", 3, NOW + 106)])
        self.lab.complete_task(retest_task["task_id"])
        new_jump = next(f for f in self.lab.findings.values()
                        if f["task_id"] == retest_task["task_id"]
                        and f["rule_id"] == RULE_AUTO_JUMP)
        self.lab.review_finding(new_jump["finding_id"],
                                {"decision": FINDING_CONFIRMED, "reviewer": "复核员乙"},
                                actor=REVIEWER)
        retest = self.lab.record_retest(SUBJECT_APP, APP_ID, {
            "task_id": retest_task["task_id"],
            "commitment_ids": [commitment["commitment_id"]]}, actor=REVIEWER)
        self.assertEqual(retest["result"], "failed")
        self.assertEqual(retest["uncovered_confirmed_finding_ids"],
                         [new_jump["finding_id"]])
        self.assertEqual(self.lab.commitments[commitment["commitment_id"]]["status"],
                         COMMITMENT_FULFILLED)  # 单项仍关闭
        self.assertEqual(self.lab.subject_view(SUBJECT_APP, APP_ID, actor=REVIEWER)
                         ["status"], "open")

    def test_pending_retest_resumes_on_completion_and_restart(self):
        _, findings, _ = self._app_case()
        self._commitment(findings[0])
        # 复测任务尚未完成：进入待复测队列
        pending_task = self.lab.create_task(
            {"build_id": "com.example.news:1002", "device_id": DEV_A, "track": "normal"})
        queued = self.lab.record_retest(SUBJECT_APP, APP_ID, {
            "task_id": pending_task["task_id"], "idempotency_key": "k1"}, actor=REVIEWER)
        self.assertEqual(queued["result"], RETEST_PENDING)

        # 服务在任务未完成时重启：待复测事项保留
        snapshot = self.lab.to_snapshot()
        restored = Lab.from_snapshot(snapshot, clock=self.clock)
        view = restored.subject_view(SUBJECT_APP, APP_ID, actor=REVIEWER)
        self.assertEqual(len(view["pending_retests"]), 1)

        # 补传完成采集：自动接续，只产生一次推进
        restored.ingest_events(pending_task["task_id"],
                               [ad("c1"), close("c1", 2, after=1, size=48)])
        restored.complete_task(pending_task["task_id"])
        view = restored.subject_view(SUBJECT_APP, APP_ID, actor=REVIEWER)
        self.assertEqual(view["pending_retests"], [])
        self.assertEqual(len(view["cycles"][-1]["retests"]), 1)
        self.assertEqual(view["cycles"][-1]["retests"][0]["result"], RETEST_PASSED)
        self.assertEqual(view["status"], "rectified")

        # 再次重启不会重复处理
        restored2 = Lab.from_snapshot(restored.to_snapshot(), clock=self.clock)
        view2 = restored2.subject_view(SUBJECT_APP, APP_ID, actor=REVIEWER)
        self.assertEqual(len(view2["cycles"][-1]["retests"]), 1)


class RelapseAndEnforcementTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.lab = self._seed()

    def _seed(self):
        lab = Lab(clock=self.clock)
        lab.register_regulation({
            "version": "v2025.1", "effective_at": 0,
            "params": {"rectification_days": 10}})
        lab.register_script({"script_id": "ad-trip", "version": "1.0",
                             "created_at": self.clock()})
        lab.register_device({"device_id": DEV_A, "model": "Pixel 6",
                             "os_version": "Android 12"})
        lab.register_device({"device_id": DEV_B, "model": "Pixel 8",
                             "os_version": "Android 14"})
        return lab

    def _build(self, code, components=None, fingerprints=None):
        self.lab.register_build({
            "app_id": APP_ID, "app_name": "某新闻", "developer": "某新闻运营有限公司",
            "version_code": code, "version_name": f"8.{code}",
            "components": components or [], "fingerprints": fingerprints or []})
        return f"{APP_ID}:{code}"

    def _confirmed_close(self, build, ad_id, device=DEV_A, track="normal",
                         after=5, size=36):
        task = self.lab.create_task(
            {"build_id": build, "device_id": device, "track": track})
        self.lab.ingest_events(task["task_id"], [ad(ad_id), close(ad_id, 2, after, size)])
        self.lab.complete_task(task["task_id"])
        finding = next(f for f in self.lab.findings.values()
                       if f["task_id"] == task["task_id"])
        self.lab.review_finding(finding["finding_id"],
                                {"decision": FINDING_CONFIRMED, "reviewer": "复核员乙"},
                                actor=REVIEWER)
        return task, self.lab.findings[finding["finding_id"]]

    def _close_cycle(self, build, ad_id, device=DEV_A):
        task = self.lab.create_task(
            {"build_id": build, "device_id": device, "track": "normal"})
        self.lab.ingest_events(task["task_id"], [ad(ad_id), close(ad_id, 2, 1, 48)])
        self.lab.complete_task(task["task_id"])
        retest = self.lab.record_retest(SUBJECT_APP, APP_ID,
                                        {"task_id": task["task_id"]}, actor=REVIEWER)
        return task, retest

    def test_relapse_is_proposed_with_basis_but_only_staff_confirms(self):
        v1 = self._build(1001, components=[COMP_CLOSE_OLD], fingerprints=[FP_CLOSE])
        _, finding = self._confirmed_close(v1, "a1")
        self.lab.generate_notice(SUBJECT_APP, APP_ID,
                                 {"issued_by": "承办人甲"}, actor=REVIEWER)
        self.lab.submit_commitment(SUBJECT_APP, APP_ID, {
            "finding_id": finding["finding_id"], "target_build_id": v1}, actor=APP_PARTY)
        v2 = self._build(1002, components=[COMP_CLOSE_NEW])
        self._close_cycle(v2, "c1")

        # 新版本复用旧组件 + 命中行为指纹 + 同规则重现
        self.clock.advance(20 * 86400)
        v3 = self._build(1003, components=[COMP_CLOSE_OLD], fingerprints=[FP_CLOSE])
        _, relapsed = self._confirmed_close(v3, "d1", track="elderly")
        self.assertIsNotNone(relapsed["relapse_id"])
        relapse = self.lab.relapses[relapsed["relapse_id"]]
        self.assertEqual(relapse["status"], RELAPSE_PROPOSED)
        self.assertEqual(relapse["proposed_by"], "system")
        kinds = {b["kind"] for b in relapse["bases"]}
        self.assertEqual(kinds, {"same_rule", "component", "behavior"})
        self.assertTrue(any(COMP_CLOSE_OLD["component_id"] in b["detail"]
                            for b in relapse["bases"] if b["kind"] == "component"))
        self.assertEqual(relapse["bases"][0]["prior_build_id"], v1)

        # 责任方无权确认复发
        with self.assertRaises(AccessDeniedError):
            self.lab.decide_relapse(relapse["relapse_id"], {
                "decision": RELAPSE_CONFIRMED, "reviewer": "应用自己"}, actor=APP_PARTY)
        decided = self.lab.decide_relapse(relapse["relapse_id"], {
            "decision": RELAPSE_CONFIRMED, "reviewer": "审核主管丁",
            "comment": "组件与指纹双重命中"}, actor=REVIEWER)
        self.assertEqual(decided["status"], RELAPSE_CONFIRMED)
        # 决定只增一次
        with self.assertRaises(DomainError):
            self.lab.decide_relapse(relapse["relapse_id"], {
                "decision": RELAPSE_REJECTED, "reviewer": "审核主管丁"}, actor=REVIEWER)

    def test_no_proposal_without_reuse_basis_and_rejection_is_recorded(self):
        v1 = self._build(1001, components=[COMP_CLOSE_NEW])
        _, finding = self._confirmed_close(v1, "a1")
        self.lab.submit_commitment(SUBJECT_APP, APP_ID, {
            "finding_id": finding["finding_id"], "target_build_id": v1}, actor=APP_PARTY)
        self._close_cycle(v1, "c1")

        self.clock.advance(20 * 86400)
        v2 = self._build(1002, components=[{"component_id": "comp-other", "kind": "sdk",
                                            "name": "另一家SDK", "version": "9"}])
        task = self.lab.create_task(
            {"build_id": v2, "device_id": DEV_A, "track": "normal"})
        # 不同规则、无组件/指纹复用：不应提议复发
        self.lab.ingest_events(task["task_id"], [
            ad("q1"), close("q1", 2, 1, 48), jump("q1", 3, NOW + 106)])
        self.lab.complete_task(task["task_id"])
        jump_finding = next(f for f in self.lab.findings.values()
                            if f["task_id"] == task["task_id"]
                            and f["rule_id"] == RULE_AUTO_JUMP)
        self.lab.review_finding(jump_finding["finding_id"],
                                {"decision": FINDING_CONFIRMED, "reviewer": "复核员乙"},
                                actor=REVIEWER)
        self.assertIsNone(jump_finding["relapse_id"])
        self.assertEqual(self.lab.relapses, {})

        # 同规则但无复用依据也不提议
        self.clock.advance(5 * 86400)
        v3 = self._build(1003)
        _, close_again = self._confirmed_close(v3, "d1")
        # 同规则仍构成依据（同规则重现本身即依据）
        relapse = self.lab.relapses.get(close_again["relapse_id"])
        self.assertIsNotNone(relapse)
        rejected = self.lab.decide_relapse(relapse["relapse_id"], {
            "decision": RELAPSE_REJECTED, "reviewer": "审核主管丁",
            "comment": "与旧问题无实质关联"}, actor=REVIEWER)
        self.assertEqual(rejected["status"], RELAPSE_REJECTED)
        # 驳回不改变原始发现与周期
        self.assertEqual(close_again["status"], FINDING_CONFIRMED)

    def _sdk_case(self):
        v0 = self._build(1000, components=[])
        task = self.lab.create_task(
            {"build_id": v0, "device_id": DEV_B, "track": "normal"})
        self.lab.ingest_events(task["task_id"], [
            ad("s1"), close("s1", 2, 1, 48),
            sensor("s1", 3, NOW + 104),
            jump("s1", 4, NOW + 105, trigger="shake", sdk=SDK_ID)])
        self.lab.complete_task(task["task_id"])
        finding = next(f for f in self.lab.findings.values()
                       if f["task_id"] == task["task_id"])
        self.assertEqual(finding["responsible_subject"]["type"], SUBJECT_SDK)
        self.lab.review_finding(finding["finding_id"],
                                {"decision": FINDING_CONFIRMED, "reviewer": "复核员乙"},
                                actor=REVIEWER)
        self.lab.generate_notice(SUBJECT_SDK, SDK_ID,
                                 {"issued_by": "承办人甲"}, actor=REVIEWER)
        commitment = self.lab.submit_commitment(SUBJECT_SDK, SDK_ID, {
            "finding_id": finding["finding_id"], "target_build_id": v0,
            "deadline": NOW + 30 * 86400}, actor=SDK_PARTY)
        return commitment

    def test_overdue_escalation_targets_only_responsible_subject_once(self):
        v1 = self._build(1001)
        _, app_finding = self._confirmed_close(v1, "a1")
        self.lab.generate_notice(SUBJECT_APP, APP_ID,
                                 {"issued_by": "承办人甲"}, actor=REVIEWER)
        app_commitment = self.lab.submit_commitment(SUBJECT_APP, APP_ID, {
            "finding_id": app_finding["finding_id"], "target_build_id": v1,
            "deadline": NOW + 5 * 86400}, actor=APP_PARTY)
        sdk_commitment = self._sdk_case()

        # 超过应用期限、未到 SDK 期限
        self.clock.advance(6 * 86400)
        result = self.lab.run_overdue_checks(actor=REVIEWER)
        self.assertEqual([e["commitment_id"] for e in result["escalated"]],
                         [app_commitment["commitment_id"]])
        app_logs = self.lab.subject_view(SUBJECT_APP, APP_ID, actor=REVIEWER)["cycles"][-1]
        kinds = {n["kind"] for n in app_logs["notifications"]}
        self.assertEqual(kinds, {"notice_delivered", "escalation_overdue"})
        # 升级回执只触达应用本身
        escalation = next(n for n in app_logs["notifications"]
                          if n["kind"] == "escalation_overdue")
        self.assertEqual(escalation["recipient"]["id"], APP_ID)
        sdk_logs = self.lab.subject_view(SUBJECT_SDK, SDK_ID, actor=REVIEWER)["cycles"][-1]
        self.assertNotIn("escalation_overdue",
                         {n["kind"] for n in sdk_logs["notifications"]})

        # 重复扫描不产生二次处罚
        again = self.lab.run_overdue_checks(actor=REVIEWER)
        self.assertEqual(again["escalated"], [])
        self.assertEqual(
            self.lab.commitments[app_commitment["commitment_id"]]["escalation_notification_id"],
            escalation["notification_id"])

        # SDK 到期后才升级 SDK
        self.clock.advance(30 * 86400)
        result = self.lab.run_overdue_checks(actor=REVIEWER)
        self.assertEqual([e["commitment_id"] for e in result["escalated"]],
                         [sdk_commitment["commitment_id"]])

        # 责任方不能自行触发升级扫描
        with self.assertRaises(AccessDeniedError):
            self.lab.run_overdue_checks(actor=APP_PARTY)

    def test_regulator_oversight_and_party_isolation(self):
        v1 = self._build(1001, components=[COMP_CLOSE_OLD], fingerprints=[FP_CLOSE])
        _, finding = self._confirmed_close(v1, "a1")
        self.lab.generate_notice(SUBJECT_APP, APP_ID,
                                 {"issued_by": "承办人甲"}, actor=REVIEWER)
        commitment = self.lab.submit_commitment(SUBJECT_APP, APP_ID, {
            "finding_id": finding["finding_id"], "target_build_id": v1,
            "deadline": NOW + 5 * 86400}, actor=APP_PARTY)

        oversight = self.lab.oversight_build(v1, actor=REVIEWER)
        self.assertEqual([c["commitment_id"] for c in oversight["open_commitments"]],
                         [commitment["commitment_id"]])
        self.assertEqual(oversight["findings"][0]["responsible_subject"]["id"], APP_ID)
        self.assertTrue(any(n["kind"] == "notice_delivered"
                            for n in oversight["findings"][0]["notifications"]))

        # 责任方不能看监管视图
        with self.assertRaises(AccessDeniedError):
            self.lab.oversight_build(v1, actor=APP_PARTY)

        # 广告主只能取得自己的材料：看不到应用案件
        with self.assertRaises(AccessDeniedError):
            self.lab.subject_view(SUBJECT_APP, APP_ID, actor=OTHER_PARTY)
        with self.assertRaises(AccessDeniedError):
            self.lab.party_materials(SUBJECT_APP, APP_ID, actor=OTHER_PARTY)

        # 应用责任方能取得自己的材料（含告知快照、承诺、回执）
        materials = self.lab.party_materials(SUBJECT_APP, APP_ID, actor=APP_PARTY)
        notice = materials["cycles"][-1]["notices"][0]
        self.assertEqual(notice["findings"][0]["finding_id"], finding["finding_id"])
        self.assertEqual(materials["cycles"][-1]["commitments"][0]["commitment_id"],
                         commitment["commitment_id"])
        self.assertTrue(materials["cycles"][-1]["notifications"])

        # 责任方不能复核、出告知、登记复测
        with self.assertRaises(AccessDeniedError):
            self.lab.review_finding(finding["finding_id"],
                                    {"decision": FINDING_CONFIRMED, "reviewer": "x"},
                                    actor=APP_PARTY)
        with self.assertRaises(AccessDeniedError):
            self.lab.generate_notice(SUBJECT_APP, APP_ID,
                                     {"issued_by": "x"}, actor=APP_PARTY)
        with self.assertRaises(AccessDeniedError):
            self.lab.record_retest(SUBJECT_APP, APP_ID,
                                   {"task_id": "none"}, actor=APP_PARTY)

        # 逾期后监管视图含逾期承诺与复发待决（构造一次复发）
        v2 = self._build(1002)
        task = self.lab.create_task(
            {"build_id": v2, "device_id": DEV_A, "track": "normal"})
        self.lab.ingest_events(task["task_id"], [ad("c1"), close("c1", 2, 1, 48)])
        self.lab.complete_task(task["task_id"])
        self.lab.record_retest(SUBJECT_APP, APP_ID,
                               {"task_id": task["task_id"]}, actor=REVIEWER)
        self.clock.advance(20 * 86400)
        v3 = self._build(1003, components=[COMP_CLOSE_OLD])
        _, relapsed = self._confirmed_close(v3, "d1")
        oversight3 = self.lab.oversight_build(v3, actor=REVIEWER)
        self.assertEqual([r["relapse_id"] for r in oversight3["pending_relapse_decisions"]],
                         [relapsed["relapse_id"]])
        self.assertEqual(oversight3["findings"][0]["relapse"]["status"],
                         RELAPSE_PROPOSED)

    def test_old_evidence_and_notifications_are_retained(self):
        v1 = self._build(1001)
        task, finding = self._confirmed_close(v1, "a1")
        self.lab.generate_notice(SUBJECT_APP, APP_ID,
                                 {"issued_by": "承办人甲"}, actor=REVIEWER)
        commitment = self.lab.submit_commitment(SUBJECT_APP, APP_ID, {
            "finding_id": finding["finding_id"], "target_build_id": v1}, actor=APP_PARTY)
        v2 = self._build(1002)
        self._close_cycle(v2, "c1")

        view = self.lab.subject_view(SUBJECT_APP, APP_ID, actor=REVIEWER)
        self.assertEqual(view["cycles"][0]["status"], "rectified")
        # 旧周期的告知、承诺、回执、问题时段全部保留
        self.assertTrue(view["cycles"][0]["notices"])
        self.assertEqual(view["cycles"][0]["commitments"][0]["status"],
                         COMMITMENT_FULFILLED)
        self.assertIsNotNone(view["cycles"][0]["problem_period"])
        self.assertEqual(view["cycles"][0]["notifications"][0]["receipt"]["status"],
                         "delivered")
        old_report = self.lab.task_report(task["task_id"])
        self.assertEqual(old_report["build"]["version_code"], 1001)
        self.assertEqual(old_report["findings"][0]["status"], FINDING_CONFIRMED)
        self.assertEqual(commitment["commitment_id"], commitment["commitment_id"])

    def test_sdk_party_cannot_reach_app_materials(self):
        # SDK 责任方访问应用主体的材料被拒（主体不匹配），而不是看到他人数据
        with self.assertRaises(AccessDeniedError):
            self.lab.party_materials(SUBJECT_APP, APP_ID, actor=SDK_PARTY)
        with self.assertRaises(AccessDeniedError):
            self.lab.subject_view(SUBJECT_APP, APP_ID, actor=SDK_PARTY)


if __name__ == "__main__":
    unittest.main()
