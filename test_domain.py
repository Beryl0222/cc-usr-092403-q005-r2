"""领域规则的端到端测试：三轨迹、双设备、幂等、版本固化、复测与回潮。"""

import unittest

from domain import (
    FINDING_CONFIRMED,
    FINDING_DISMISSED,
    FINDING_SUSPECTED,
    ITEM_CLOSED,
    ITEM_COMMITTED,
    RELAPSE_CONFIRMED,
    RELAPSE_DISMISSED,
    RELAPSE_PROPOSED,
    ROLE_REGULATOR,
    ROLE_RESPONSIBLE,
    ROLE_REVIEWER,
    RULE_AUTO_JUMP,
    RULE_NO_CLOSE_PATH,
    RULE_SHAKE_THRESHOLD,
    SUBJECT_ADVERTISER,
    SUBJECT_APP,
    SUBJECT_SDK,
    TRACK_ELDERLY,
    TRACK_NORMAL,
    TRACK_SCREEN_READER,
    DomainError,
    Lab,
    NotFoundError,
    PermissionError_,
)

NOW = 1_700_000_000
APP_ID = "com.example.news"
SDK_ID = "shake-sdk-9"
ADVERTISER_ID = "ad-brand-x"
BUILD_V1 = f"{APP_ID}:1001"
BUILD_V2 = f"{APP_ID}:1002"
BUILD_V3 = f"{APP_ID}:1003"
DEV_A = "device-A"
DEV_B = "device-B"


class FakeClock:
    def __init__(self, start=NOW):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds
        return self.t


def base_lab(clock):
    lab = Lab(clock=clock)
    lab.register_regulation({
        "version": "v2025.1", "effective_at": 0,
        "title": "移动互联网广告合规规范 v2025.1",
        "params": {"rectification_days": 10},
    })
    lab.register_script({"script_id": "ad-trip", "version": "1.0", "created_at": clock()})
    lab.register_device({"device_id": DEV_A, "model": "Pixel 6", "os_version": "Android 12"})
    lab.register_device({"device_id": DEV_B, "model": "Pixel 8", "os_version": "Android 14"})
    lab.register_build({
        "app_id": APP_ID, "app_name": "某新闻", "developer": "某新闻运营有限公司",
        "version_code": 1001, "version_name": "8.1.0",
    })
    return lab


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


def jump(ad_id, seq, at, trigger, target="market://details?id=x", sdk=None,
         advertiser=None, **sensor):
    payload = {"ad_id": ad_id, "trigger": trigger, "target_url": target}
    if sdk:
        payload["sdk"] = {"id": sdk, "name": "摇一摇SDK"}
    if advertiser:
        payload["advertiser"] = {"id": advertiser}
    payload.update(sensor)
    return {"event_id": f"e-{ad_id}-jump-{seq}", "seq": seq, "type": "jump",
            "occurred_at": at, "payload": payload}


def sensor(ad_id, seq, at, accel, rotation, seconds):
    return {"event_id": f"e-{ad_id}-sensor-{seq}", "seq": seq, "type": "sensor_reading",
            "occurred_at": at,
            "payload": {"ad_id": ad_id, "peak_acceleration": accel,
                        "peak_rotation_deg": rotation, "reading_seconds": seconds}}


def gesture(seq, at):
    return {"event_id": f"e-gesture-{seq}", "seq": seq, "type": "gesture",
            "occurred_at": at, "payload": {"kind": "tap"}}


def network(ad_id, seq, at, status, url):
    return {"event_id": f"e-{ad_id}-net-{seq}", "seq": seq, "type": "network_response",
            "occurred_at": at,
            "payload": {"ad_id": ad_id, "http_status": status, "url": url}}


class DomainFlowTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.lab = base_lab(self.clock)

    def _task(self, device, track):
        return self.lab.create_task({"build_id": BUILD_V1, "device_id": device, "track": track})

    # ---------------------------------------------------------------- #

    def test_three_tracks_on_same_build_coexist_and_pin_versions(self):
        t_normal = self._task(DEV_A, TRACK_NORMAL)
        t_reader = self._task(DEV_A, TRACK_SCREEN_READER)
        t_elder = self._task(DEV_A, TRACK_ELDERLY)
        for task in (t_normal, t_reader, t_elder):
            self.assertEqual(task["regulation_version"], "v2025.1")
            self.assertEqual(task["script"], {"script_id": "ad-trip", "version": "1.0"})
        self.assertTrue(t_reader["accessibility"]["screen_reader_enabled"])
        self.assertTrue(t_elder["accessibility"]["elderly_mode_enabled"])
        self.assertNotEqual(t_normal["task_id"], t_reader["task_id"])

        # 同一构建在另一台设备上的轨迹并存，不覆盖设备 A 的结论
        t_b = self._task(DEV_B, TRACK_NORMAL)
        report = self.lab.build_report(BUILD_V1)
        keys = {(row["device_id"], row["track"]) for row in report["tracks"]}
        self.assertEqual(keys, {
            (DEV_A, TRACK_NORMAL), (DEV_A, TRACK_SCREEN_READER),
            (DEV_A, TRACK_ELDERLY), (DEV_B, TRACK_NORMAL),
        })
        self.assertEqual(t_b["device_id"], DEV_B)

    def test_normal_track_flags_late_close_and_auto_jump_blames_sdk(self):
        task = self._task(DEV_A, TRACK_NORMAL)
        self.lab.ingest_events(task["task_id"], [
            ad("a1"),
            close("a1", 2, after=5, size=36),          # 出现太晚且点区过小
            jump("a1", 3, NOW + 106, "auto", sdk=SDK_ID,
                 target="https://shop.example/promo"),
            network("a1", 4, NOW + 106, 302, "https://shop.example/promo"),
        ])
        result = self.lab.complete_task(task["task_id"])
        self.assertEqual(set(result["new_suspected_findings"]),
                         {self._finding(task, RULE_NO_CLOSE_PATH)["finding_id"],
                          self._finding(task, RULE_AUTO_JUMP)["finding_id"]})

        close_finding = self._finding(task, RULE_NO_CLOSE_PATH)
        self.assertEqual(close_finding["status"], FINDING_SUSPECTED)  # 规则只标涉嫌
        self.assertEqual(close_finding["responsible_subject"]["type"], SUBJECT_APP)
        self.assertFalse(close_finding["detail"]["close_path"]["operable"])
        self.assertEqual(len(close_finding["detail"]["violations"]), 2)

        jump_finding = self._finding(task, RULE_AUTO_JUMP)
        self.assertEqual(jump_finding["responsible_subject"]["type"], SUBJECT_SDK)
        self.assertEqual(jump_finding["responsible_subject"]["id"], SDK_ID)
        # 责任链同时保留应用与 SDK，广告主缺失时为空
        self.assertEqual(jump_finding["responsibility_chain"]["app"]["id"], APP_ID)
        self.assertEqual(jump_finding["responsibility_chain"]["sdk"]["id"], SDK_ID)
        self.assertIsNone(jump_finding["responsibility_chain"]["advertiser"])
        self.assertEqual(jump_finding["detail"]["jump"]["target_url"],
                         "https://shop.example/promo")

    def test_screen_reader_track_blames_shake_sdk_on_low_threshold(self):
        task = self._task(DEV_A, TRACK_SCREEN_READER)
        self.lab.ingest_events(task["task_id"], [
            ad("a2"),
            close("a2", 2, after=1, size=48, reader=False),  # 读屏不可聚焦
            sensor("a2", 3, NOW + 104, accel=8, rotation=10, seconds=1),
            jump("a2", 4, NOW + 105, "shake", sdk=SDK_ID,
                 advertiser=ADVERTISER_ID, target="https://shop.example/p"),
        ])
        self.lab.complete_task(task["task_id"])
        shake = self._finding(task, RULE_SHAKE_THRESHOLD)
        self.assertEqual(shake["responsible_subject"]["type"], SUBJECT_SDK)
        self.assertEqual(shake["responsibility_chain"]["advertiser"]["id"], ADVERTISER_ID)
        self.assertEqual(len(shake["detail"]["violations"]), 3)
        close_finding = self._finding(task, RULE_NO_CLOSE_PATH)
        self.assertTrue(
            any("读屏" in v for v in close_finding["detail"]["violations"])
        )

    def test_elderly_track_requires_larger_close_target_and_blames_advertiser(self):
        task = self._task(DEV_A, TRACK_ELDERLY)
        self.lab.ingest_events(task["task_id"], [
            ad("a3", advertiser=ADVERTISER_ID),
            close("a3", 2, after=1, size=48),             # 老人模式需 >=56dp
            jump("a3", 3, NOW + 103, "auto",
                 advertiser=ADVERTISER_ID, target="https://brand.example/"),
        ])
        self.lab.complete_task(task["task_id"])
        close_finding = self._finding(task, RULE_NO_CLOSE_PATH)
        self.assertTrue(any("56" in v for v in close_finding["detail"]["violations"]))
        auto = self._finding(task, RULE_AUTO_JUMP)
        # 无 SDK 信息时自动跳转归广告主，应用仍在责任链中
        self.assertEqual(auto["responsible_subject"]["type"], SUBJECT_ADVERTISER)
        self.assertEqual(auto["responsibility_chain"]["app"]["id"], APP_ID)

    def test_compliant_run_on_second_device_has_no_findings(self):
        task = self._task(DEV_B, TRACK_NORMAL)
        self.lab.ingest_events(task["task_id"], [
            ad("b1"),
            close("b1", 2, after=1, size=48),
            gesture(3, NOW + 104),
            sensor("b1", 4, NOW + 105, accel=22, rotation=45, seconds=4),
            jump("b1", 5, NOW + 106, "shake", target="https://ok.example/"),
        ])
        result = self.lab.complete_task(task["task_id"])
        self.assertEqual(result["new_suspected_findings"], [])

    def test_retransmission_is_deduplicated_and_recheck_does_not_duplicate(self):
        task = self._task(DEV_A, TRACK_NORMAL)
        batch = [ad("a1"), close("a1", 2, after=5, size=36)]
        first = self.lab.ingest_events(task["task_id"], batch)
        self.assertEqual(first["accepted"], ["e-a1-shown", "e-a1-close-2"])
        again = self.lab.ingest_events(task["task_id"], batch)  # 重传
        self.assertEqual(again["duplicates"], ["e-a1-shown", "e-a1-close-2"])
        self.assertEqual(again["accepted"], [])
        self.lab.complete_task(task["task_id"])
        # 补传：重复事件判重 + 一条新证据触发复判，指纹去重保证发现不翻倍
        recheck = self.lab.ingest_events(task["task_id"], batch + [
            network("a1", 9, NOW + 107, 200, "https://ad.example/impression"),
        ])
        self.assertEqual(recheck["duplicates"], ["e-a1-shown", "e-a1-close-2"])
        self.assertEqual(recheck["accepted"], ["e-a1-net-9"])
        self.assertEqual(recheck["review_result"]["new_suspected_findings"], [])

    def test_late_events_after_completion_are_appended_not_backfilled(self):
        task = self._task(DEV_B, TRACK_NORMAL)
        self.lab.ingest_events(task["task_id"], [ad("b1"), close("b1", 2, after=1, size=48)])
        self.lab.complete_task(task["task_id"])
        late = self.lab.ingest_events(task["task_id"], [
            jump("b1", 9, NOW + 300, "auto", sdk=SDK_ID, target="https://x.example/"),
        ])
        self.assertEqual(late["accepted"], ["e-b1-jump-9"])
        self.assertTrue(late["late_arrivals"])
        finding = self.lab.findings[late["review_result"]["new_suspected_findings"][0]]
        self.assertEqual(finding["rule_id"], RULE_AUTO_JUMP)
        evidence = self.lab.events[(task["task_id"], "e-b1-jump-9")]
        self.assertTrue(evidence["late"])

    def test_only_confirmed_findings_can_enter_notice(self):
        task = self._task(DEV_A, TRACK_NORMAL)
        self.lab.ingest_events(task["task_id"], [
            ad("a1"), close("a1", 2, after=5, size=36),
            jump("a1", 3, NOW + 106, "auto", sdk=SDK_ID),
        ])
        self.lab.complete_task(task["task_id"])
        # 没有任何确认发现时不能出告知材料（案件尚不存在）
        with self.assertRaises(NotFoundError):
            self.lab.generate_notice(SUBJECT_APP, APP_ID, {"issued_by": "承办人甲"})

        close_id = self._finding(task, RULE_NO_CLOSE_PATH)["finding_id"]
        jump_id = self._finding(task, RULE_AUTO_JUMP)["finding_id"]
        self.lab.review_finding(close_id, {"decision": FINDING_CONFIRMED,
                                           "reviewer": "复核员乙", "comment": "关闭路径确实不可用"})
        notice = self.lab.generate_notice(SUBJECT_APP, APP_ID, {"issued_by": "承办人甲"})
        self.assertEqual(len(notice["findings"]), 1)            # 涉嫌的跳转发现不在材料中
        self.assertEqual(notice["findings"][0]["finding_id"], close_id)
        self.assertEqual(notice["rectification_days"], 10)
        self.assertEqual(notice["rectification_deadline"], self.clock() + 10 * 86400)
        self.assertEqual(notice["regulation_versions"], ["v2025.1"])

        # 没有新增已确认发现时不得重复出具
        with self.assertRaises(DomainError):
            self.lab.generate_notice(SUBJECT_APP, APP_ID, {"issued_by": "承办人甲"})

        # 驳回的发现不进案件
        self.lab.review_finding(jump_id, {"decision": FINDING_DISMISSED, "reviewer": "复核员乙"})
        self.assertEqual(self.lab.findings[jump_id]["status"], FINDING_DISMISSED)

    def _confirmed_app_case(self):
        task = self._task(DEV_A, TRACK_NORMAL)
        self.lab.ingest_events(task["task_id"], [
            ad("a1"), close("a1", 2, after=5, size=36),
        ])
        self.lab.complete_task(task["task_id"])
        finding = self._finding(task, RULE_NO_CLOSE_PATH)
        self.lab.review_finding(finding["finding_id"],
                                {"decision": FINDING_CONFIRMED, "reviewer": "复核员乙"})
        self.lab.generate_notice(SUBJECT_APP, APP_ID, {"issued_by": "承办人甲"})
        return task, finding

    def test_retest_pass_closes_cycle_but_keeps_problem_period(self):
        task, finding = self._confirmed_app_case()
        self.clock.advance(3 * 86400)
        self.lab.register_build({
            "app_id": APP_ID, "app_name": "某新闻", "developer": "某新闻运营有限公司",
            "version_code": 1002, "version_name": "8.2.0",
        })
        # 整改承诺：目标构建 + 适用人群轨道 + 期限，挂到具体整改项
        item_id = finding["item_id"]
        self.lab.commit_rectification(item_id, {
            "target_build_id": BUILD_V2, "target_track": TRACK_NORMAL,
            "rectification_days": 7, "committed_by": "整改负责人丁"})
        item = self.lab.get_item(item_id)
        self.assertEqual(item["status"], ITEM_COMMITTED)
        self.assertEqual(item["commitment"]["target_build_id"], BUILD_V2)
        self.assertEqual(item["commitment"]["deadline"], self.clock() + 7 * 86400)

        retest_task = self.lab.create_task(
            {"build_id": BUILD_V2, "device_id": DEV_A, "track": TRACK_NORMAL})
        self.lab.ingest_events(retest_task["task_id"], [
            ad("c1"), close("c1", 2, after=1, size=48),
        ])
        self.lab.complete_task(retest_task["task_id"])
        retest = self.lab.record_retest(SUBJECT_APP, APP_ID,
                                        {"task_id": retest_task["task_id"], "by": "复测员丙"})
        self.assertEqual(retest["result"], "passed")
        self.assertEqual(retest["passed_item_ids"], [item_id])

        # 复测固化当时的脚本/规则/设备/构建摘要
        snap = retest["snapshot"]
        self.assertEqual(snap["script"], {"script_id": "ad-trip", "version": "1.0"})
        self.assertEqual(snap["regulation_version"], "v2025.1")
        self.assertEqual(snap["device"]["device_id"], DEV_A)
        self.assertEqual(snap["build"]["build_id"], BUILD_V2)
        self.assertEqual(snap["build"]["version_code"], 1002)

        view = self.lab.subject_view(SUBJECT_APP, APP_ID)
        self.assertEqual(view["status"], "rectified")
        self.assertEqual(view["relapse_count"], 0)
        cycle = view["cycles"][0]
        self.assertEqual(cycle["status"], "rectified")
        # 单项关闭：整改项已关闭
        self.assertEqual(cycle["items"][0]["status"], ITEM_CLOSED)
        self.assertEqual(cycle["items"][0]["close_retest_id"], retest["retest_id"])
        # 问题时段保留
        self.assertEqual(cycle["problem_period"]["first_observed_at"], finding["observed_at"])
        # 旧构建与旧证据未被新版覆盖
        self.assertIn(BUILD_V1, self.lab.builds)
        old_report = self.lab.task_report(task["task_id"])
        self.assertEqual(old_report["build"]["version_code"], 1001)
        self.assertEqual(old_report["findings"][0]["status"], FINDING_CONFIRMED)

    def test_relapse_is_proposed_then_confirmed_and_accumulates_history(self):
        self._confirmed_app_case()
        self.clock.advance(3 * 86400)
        # 第一次整改：新版复测通过
        self.lab.register_build({
            "app_id": APP_ID, "app_name": "某新闻", "developer": "某新闻运营有限公司",
            "version_code": 1002, "version_name": "8.2.0"})
        fixed = self.lab.create_task(
            {"build_id": BUILD_V2, "device_id": DEV_A, "track": TRACK_NORMAL})
        self.lab.ingest_events(fixed["task_id"], [ad("c1"), close("c1", 2, after=1, size=48)])
        self.lab.complete_task(fixed["task_id"])
        self.lab.record_retest(SUBJECT_APP, APP_ID, {"task_id": fixed["task_id"]})

        # 回潮：又一版构建恢复旧行为（同一广告位、同规则、同责任方 -> 同行为指纹）
        self.clock.advance(20 * 86400)
        self.lab.register_build({
            "app_id": APP_ID, "app_name": "某新闻", "developer": "某新闻运营有限公司",
            "version_code": 1003, "version_name": "8.3.0"})
        relapsed = self.lab.create_task(
            {"build_id": BUILD_V3, "device_id": DEV_A, "track": TRACK_ELDERLY})
        self.lab.ingest_events(relapsed["task_id"], [ad("d1"), close("d1", 2, after=6, size=40)])
        self.lab.complete_task(relapsed["task_id"])
        relapse_finding = self._finding(relapsed, RULE_NO_CLOSE_PATH)
        self.lab.review_finding(relapse_finding["finding_id"],
                                {"decision": FINDING_CONFIRMED, "reviewer": "复核员乙"})

        # 新周期已开，但回潮次数在人工确认前保持 0
        view = self.lab.subject_view(SUBJECT_APP, APP_ID)
        self.assertEqual(view["status"], "open")
        self.assertEqual(view["relapse_count"], 0)
        self.assertEqual(len(view["cycles"]), 2)
        self.assertEqual(view["current_cycle_seq"], 2)
        self.assertEqual(view["cycles"][0]["status"], "rectified")
        self.assertEqual(view["cycles"][0]["problem_period"]["last_observed_at"], NOW + 100)

        # 系统按行为指纹提出复发关联（幂等）
        self.lab.register_actor({"actor_id": "rev-1", "role": ROLE_REVIEWER, "name": "复核员乙"})
        proposed = self.lab.propose_relapses_for_build(BUILD_V3)
        self.assertEqual(len(proposed), 1)
        relapse = proposed[0]
        self.assertEqual(relapse["status"], RELAPSE_PROPOSED)
        self.assertEqual(relapse["prior_build_id"], BUILD_V1)
        self.assertEqual(relapse["new_build_id"], BUILD_V3)
        self.assertEqual(relapse["bases"][0]["basis"], "behavior_fingerprint")
        again = self.lab.propose_relapses_for_build(BUILD_V3)
        self.assertEqual(again, [])  # 不重复提议

        # 责任方无权确认；复核员确认后才累计回潮
        self.lab.register_actor({"actor_id": "app-owner", "role": ROLE_RESPONSIBLE,
                                 "subjects": [{"type": SUBJECT_APP, "id": APP_ID}]})
        with self.assertRaises(PermissionError_):
            self.lab.decide_relapse(relapse["relapse_id"],
                                    {"actor_id": "app-owner", "decision": RELAPSE_CONFIRMED})
        self.lab.decide_relapse(relapse["relapse_id"],
                                {"actor_id": "rev-1", "decision": RELAPSE_CONFIRMED,
                                 "comment": "同一关闭路径缺陷回潮"})
        self.assertEqual(self.lab.subject_view(SUBJECT_APP, APP_ID)["relapse_count"], 1)
        # 已结论不可更改，确认权只行使一次
        with self.assertRaises(DomainError):
            self.lab.decide_relapse(relapse["relapse_id"],
                                    {"actor_id": "rev-1", "decision": RELAPSE_DISMISSED})

        # 复测失败时周期保持开启
        failed_retest_task = self.lab.create_task(
            {"build_id": BUILD_V3, "device_id": DEV_B, "track": TRACK_NORMAL})
        self.lab.ingest_events(failed_retest_task["task_id"],
                               [ad("d2"), close("d2", 2, after=8, size=30)])
        self.lab.complete_task(failed_retest_task["task_id"])
        bad = self._finding(failed_retest_task, RULE_NO_CLOSE_PATH)
        self.lab.review_finding(bad["finding_id"],
                                {"decision": FINDING_CONFIRMED, "reviewer": "复核员乙"})
        retest = self.lab.record_retest(SUBJECT_APP, APP_ID,
                                        {"task_id": failed_retest_task["task_id"]})
        self.assertEqual(retest["result"], "failed")
        self.assertEqual(self.lab.subject_view(SUBJECT_APP, APP_ID)["status"], "open")

        # 再次整改通过：周期内两条整改项（elderly 与 normal 轨道）各自复测关闭
        self.lab.register_build({
            "app_id": APP_ID, "app_name": "某新闻", "developer": "某新闻运营有限公司",
            "version_code": 1004, "version_name": "8.3.1"})
        ok_task = self.lab.create_task(
            {"build_id": f"{APP_ID}:1004", "device_id": DEV_A, "track": TRACK_ELDERLY})
        self.lab.ingest_events(ok_task["task_id"], [ad("e1"), close("e1", 2, after=1, size=60)])
        self.lab.complete_task(ok_task["task_id"])
        partial = self.lab.record_retest(SUBJECT_APP, APP_ID, {"task_id": ok_task["task_id"]})
        self.assertEqual(partial["result"], "passed")
        self.assertEqual(self.lab.subject_view(SUBJECT_APP, APP_ID)["status"], "open")  # 仍有一项未关
        ok_normal = self.lab.create_task(
            {"build_id": f"{APP_ID}:1004", "device_id": DEV_A, "track": TRACK_NORMAL})
        self.lab.ingest_events(ok_normal["task_id"], [ad("e2"), close("e2", 2, after=1, size=48)])
        self.lab.complete_task(ok_normal["task_id"])
        self.lab.record_retest(SUBJECT_APP, APP_ID, {"task_id": ok_normal["task_id"]})
        view = self.lab.subject_view(SUBJECT_APP, APP_ID)
        self.assertEqual(view["status"], "rectified")
        self.assertEqual(len(view["cycles"][1]["retests"]), 3)  # 失败记录也保留

    def test_script_and_regulation_updates_only_affect_new_tasks(self):
        old_task = self._task(DEV_A, TRACK_NORMAL)
        self.clock.advance(86400)
        self.lab.register_regulation({
            "version": "v2026.1", "effective_at": self.clock(),
            "params": {"close_max_delay_seconds": 1.0, "rectification_days": 5},
        })
        self.lab.register_script({"script_id": "ad-trip", "version": "2.0",
                                  "created_at": self.clock()})
        new_task = self._task(DEV_A, TRACK_NORMAL)
        self.assertEqual(old_task["regulation_version"], "v2025.1")
        self.assertEqual(old_task["script"]["version"], "1.0")
        self.assertEqual(new_task["regulation_version"], "v2026.1")
        self.assertEqual(new_task["script"]["version"], "2.0")
        # 旧任务上的判定仍按 v1（3 秒内出现即合规），新任务按 v2（1 秒）
        self.lab.ingest_events(old_task["task_id"], [ad("o1"), close("o1", 2, after=2, size=48)])
        self.lab.complete_task(old_task["task_id"])
        self.assertIsNone(self._finding(old_task, RULE_NO_CLOSE_PATH))
        self.lab.ingest_events(new_task["task_id"], [ad("n1"), close("n1", 2, after=2, size=48)])
        self.lab.complete_task(new_task["task_id"])
        self.assertIsNotNone(self._finding(new_task, RULE_NO_CLOSE_PATH))

    def test_registrations_are_append_only(self):
        with self.assertRaises(DomainError):
            self.lab.register_regulation({"version": "v2025.1", "effective_at": 0})
        with self.assertRaises(DomainError):
            self.lab.register_build({
                "app_id": APP_ID, "app_name": "某新闻", "developer": "某新闻运营有限公司",
                "version_code": 1001})

    def test_single_item_close_does_not_close_cycle(self):
        # 同一责任方两条已确认发现 -> 两个整改项
        task = self._task(DEV_A, TRACK_NORMAL)
        self.lab.ingest_events(task["task_id"], [
            ad("a1"), close("a1", 2, after=5, size=36),
            jump("a1", 9, NOW + 109, "auto", target="https://x.example/"),
        ])
        self.lab.complete_task(task["task_id"])
        for rule in (RULE_NO_CLOSE_PATH, RULE_AUTO_JUMP):
            fid = self._finding(task, rule)["finding_id"]
            self.lab.review_finding(fid,
                                    {"decision": FINDING_CONFIRMED, "reviewer": "复核员乙"})
        view = self.lab.subject_view(SUBJECT_APP, APP_ID)
        items = view["cycles"][0]["items"]
        self.assertEqual(len(items), 2)

        self.clock.advance(86400)
        self.lab.register_build({
            "app_id": APP_ID, "app_name": "某新闻", "developer": "某新闻运营有限公司",
            "version_code": 1002, "version_name": "8.2.0"})
        # 只对关闭路径问题承诺并复测
        close_item = next(i for i in items if i["rule_id"] == RULE_NO_CLOSE_PATH)
        self.lab.commit_rectification(close_item["item_id"],
                                      {"target_build_id": BUILD_V2, "target_track": TRACK_NORMAL})
        retest_task = self.lab.create_task(
            {"build_id": BUILD_V2, "device_id": DEV_A, "track": TRACK_NORMAL})
        self.lab.ingest_events(retest_task["task_id"],
                               [ad("c1"), close("c1", 2, after=1, size=48)])
        self.lab.complete_task(retest_task["task_id"])
        self.lab.record_retest(SUBJECT_APP, APP_ID, {"task_id": retest_task["task_id"]})

        view = self.lab.subject_view(SUBJECT_APP, APP_ID)
        cycle = view["cycles"][0]
        self.assertEqual(cycle["status"], "open")  # 单项关闭不结束周期
        by_status = {i["rule_id"]: i["status"] for i in cycle["items"]}
        self.assertEqual(by_status[RULE_NO_CLOSE_PATH], ITEM_CLOSED)
        self.assertNotEqual(by_status[RULE_AUTO_JUMP], ITEM_CLOSED)

    def test_retest_is_idempotent_for_duplicate_offline_and_concurrent_submit(self):
        _, finding = self._confirmed_app_case()
        self.clock.advance(86400)
        self.lab.register_build({
            "app_id": APP_ID, "app_name": "某新闻", "developer": "某新闻运营有限公司",
            "version_code": 1002})
        self.lab.commit_rectification(finding["item_id"],
                                      {"target_build_id": BUILD_V2, "target_track": TRACK_NORMAL})
        rt = self.lab.create_task(
            {"build_id": BUILD_V2, "device_id": DEV_A, "track": TRACK_NORMAL})
        self.lab.ingest_events(rt["task_id"], [ad("c1"), close("c1", 2, after=1, size=48)])
        self.lab.complete_task(rt["task_id"])
        first = self.lab.record_retest(SUBJECT_APP, APP_ID, {"task_id": rt["task_id"]})
        # 重复上传 / 离线补传 / 并发复测：拿到同一条记录，不重复推进
        second = self.lab.record_retest(SUBJECT_APP, APP_ID, {"task_id": rt["task_id"]})
        third = self.lab.record_retest(SUBJECT_APP, APP_ID, {"task_id": rt["task_id"]})
        self.assertEqual(second["retest_id"], first["retest_id"])
        self.assertEqual(third["retest_id"], first["retest_id"])
        view = self.lab.subject_view(SUBJECT_APP, APP_ID)
        self.assertEqual(len(view["cycles"][0]["retests"]), 1)

    def test_overdue_escalation_touches_only_that_subject_once(self):
        _, app_finding = self._confirmed_app_case()
        # 另一个责任方（广告主）也有在办整改项
        adv_task = self._task(DEV_B, TRACK_NORMAL)
        self.lab.ingest_events(adv_task["task_id"], [
            ad("x1", advertiser=ADVERTISER_ID),
            jump("x1", 3, NOW + 203, "auto", advertiser=ADVERTISER_ID,
                 target="https://brand.example/"),
        ])
        self.lab.complete_task(adv_task["task_id"])
        adv_finding = self._finding(adv_task, RULE_AUTO_JUMP)
        self.lab.review_finding(adv_finding["finding_id"],
                                {"decision": FINDING_CONFIRMED, "reviewer": "复核员乙"})

        self.clock.advance(86400)
        self.lab.register_build({
            "app_id": APP_ID, "app_name": "某新闻", "developer": "某新闻运营有限公司",
            "version_code": 1002})
        # 只有应用方承诺了一个很短的期限
        self.lab.commit_rectification(app_finding["item_id"],
                                      {"target_build_id": BUILD_V2, "target_track": TRACK_NORMAL,
                                       "rectification_days": 2})
        self.clock.advance(3 * 86400)  # 应用方逾期；广告主未承诺不升级

        scan = self.lab.escalate_overdue({"issued_by": "督办系统"})
        self.assertEqual(scan["escalated_count"], 1)
        record = scan["escalated"][0]
        self.assertEqual(record["subject"]["type"], SUBJECT_APP)
        self.assertEqual(record["subject"]["id"], APP_ID)

        # 再扫两次：不重复处罚/推进
        self.assertEqual(self.lab.escalate_overdue()["escalated_count"], 0)
        self.assertEqual(self.lab.escalate_overdue()["escalated_count"], 0)

        app_view = self.lab.subject_view(SUBJECT_APP, APP_ID)
        adv_view = self.lab.subject_view(SUBJECT_ADVERTISER, ADVERTISER_ID)
        # 升级通知只触达应用方，广告主没有任何升级通知
        self.assertTrue(any(n["kind"] == "escalation" for n in app_view["notifications"]))
        self.assertFalse(any(n["kind"] == "escalation" for n in adv_view["notifications"]))
        # 回执永久保留
        receipt = app_view["notifications"][-1]
        self.assertEqual(receipt["receipt"]["status"], "delivered")
        self.assertEqual(receipt["ref_id"], app_finding["item_id"])
        item = app_view["cycles"][-1]["items"][0]
        self.assertEqual(len(item["escalations"]), 1)

    def test_component_reuse_is_a_relapse_basis(self):
        self.lab.register_component({"kind": "sdk", "component_id": SDK_ID,
                                     "version": "9.1", "name": "摇一摇SDK"})
        # 给问题构建挂上该组件
        self.lab.builds[BUILD_V1]["components"].append(
            {"kind": "sdk", "component_id": SDK_ID, "version": "9.1", "name": "摇一摇SDK"})
        _, finding = self._confirmed_app_case()
        self.clock.advance(86400)
        # 整改通过（1002 复用同一组件但无问题）
        self.lab.register_build({
            "app_id": APP_ID, "app_name": "某新闻", "developer": "某新闻运营有限公司",
            "version_code": 1002,
            "components": [{"kind": "sdk", "component_id": SDK_ID, "version": "9.1"}]})
        fixed = self.lab.create_task(
            {"build_id": BUILD_V2, "device_id": DEV_A, "track": TRACK_NORMAL})
        self.lab.ingest_events(fixed["task_id"], [ad("c1"), close("c1", 2, after=1, size=48)])
        self.lab.complete_task(fixed["task_id"])
        self.lab.commit_rectification(finding["item_id"],
                                      {"target_build_id": BUILD_V2, "target_track": TRACK_NORMAL})
        self.lab.record_retest(SUBJECT_APP, APP_ID, {"task_id": fixed["task_id"]})

        # 新版仍复用同一问题组件；换广告位使行为指纹不同，仅组件复用成立
        self.clock.advance(20 * 86400)
        self.lab.register_build({
            "app_id": APP_ID, "app_name": "某新闻", "developer": "某新闻运营有限公司",
            "version_code": 1003,
            "components": [{"kind": "sdk", "component_id": SDK_ID, "version": "9.1"}]})
        rel = self.lab.create_task(
            {"build_id": BUILD_V3, "device_id": DEV_A, "track": TRACK_NORMAL})
        self.lab.ingest_events(rel["task_id"],
                               [ad("z9", placement="lockscreen"),
                                close("z9", 2, after=6, size=30)])
        self.lab.complete_task(rel["task_id"])
        new_f = self._finding(rel, RULE_NO_CLOSE_PATH)
        self.lab.review_finding(new_f["finding_id"],
                                {"decision": FINDING_CONFIRMED, "reviewer": "复核员乙"})
        self.assertNotEqual(new_f["behavior_fingerprint"], finding["behavior_fingerprint"])
        proposed = self.lab.propose_relapses_for_build(BUILD_V3)
        self.assertEqual(len(proposed), 1)
        bases = {b["basis"] for b in proposed[0]["bases"]}
        self.assertEqual(bases, {"component_reuse"})
        self.assertEqual(proposed[0]["status"], RELAPSE_PROPOSED)

    def test_permissions_and_responsibility_scoped_materials(self):
        self._confirmed_app_case()
        self.lab.register_actor({"actor_id": "reg-1", "role": ROLE_REGULATOR, "name": "监管戊"})
        self.lab.register_actor({"actor_id": "app-owner", "role": ROLE_RESPONSIBLE,
                                 "subjects": [{"type": SUBJECT_APP, "id": APP_ID}]})
        self.lab.register_actor({"actor_id": "adv-owner", "role": ROLE_RESPONSIBLE,
                                 "subjects": [{"type": SUBJECT_ADVERTISER, "id": ADVERTISER_ID}]})

        # 监管可看应用方案卷
        reg_view = self.lab.subject_view(SUBJECT_APP, APP_ID, actor_id="reg-1")
        self.assertEqual(reg_view["case_id"][:5], "case-")
        # 广告主不能看应用方材料
        with self.assertRaises(PermissionError_):
            self.lab.subject_view(SUBJECT_APP, APP_ID, actor_id="adv-owner")
        # 应用方只能取到自己名下材料
        mine = self.lab.responsible_materials("app-owner")
        self.assertEqual(len(mine["subjects"]), 1)
        self.assertEqual(mine["subjects"][0]["responsible_subject"]["id"], APP_ID)
        self.assertEqual(self.lab.responsible_materials("adv-owner")["subjects"], [])
        # 责任方身份不能调监管的构建视角
        with self.assertRaises(PermissionError_):
            self.lab.oversight_build_view(BUILD_V1, "app-owner")

        # 监管构建视角：未履行承诺/复发依据/通知回执
        self.clock.advance(86400)
        self.lab.register_build({
            "app_id": APP_ID, "app_name": "某新闻", "developer": "某新闻运营有限公司",
            "version_code": 1002})
        item_id = self.lab.subject_view(SUBJECT_APP, APP_ID)["cycles"][0]["items"][0]["item_id"]
        self.lab.commit_rectification(item_id, {"target_build_id": BUILD_V2,
                                                "target_track": TRACK_NORMAL})
        oversight = self.lab.oversight_build_view(BUILD_V2, "reg-1")
        self.assertEqual(len(oversight["unfulfilled_commitments"]), 1)
        self.assertTrue(any(n["kind"] == "notice"
                            for n in oversight["notification_receipts"]))

    def test_snapshot_roundtrip_resumes_pending_retest_and_keeps_notifications(self):
        _, finding = self._confirmed_app_case()
        self.clock.advance(86400)
        self.lab.register_build({
            "app_id": APP_ID, "app_name": "某新闻", "developer": "某新闻运营有限公司",
            "version_code": 1002})
        self.lab.commit_rectification(finding["item_id"],
                                      {"target_build_id": BUILD_V2, "target_track": TRACK_NORMAL})
        rt = self.lab.create_task(
            {"build_id": BUILD_V2, "device_id": DEV_A, "track": TRACK_NORMAL})
        self.lab.ingest_events(rt["task_id"], [ad("c1"), close("c1", 2, after=1, size=48)])
        self.lab.complete_task(rt["task_id"])

        # 重启前尚未复测；快照恢复后仍可接续完成复测，承诺与通知不丢
        restored = Lab.from_snapshot(self.lab.to_snapshot(), clock=self.clock)
        retest = restored.record_retest(SUBJECT_APP, APP_ID, {"task_id": rt["task_id"]})
        self.assertEqual(retest["result"], "passed")
        view = restored.subject_view(SUBJECT_APP, APP_ID)
        self.assertEqual(view["cycles"][0]["status"], "rectified")
        self.assertEqual(view["cycles"][0]["items"][0]["status"], ITEM_CLOSED)
        self.assertTrue(view["notifications"])  # 告知通知随快照保留

    def test_snapshot_roundtrip_preserves_evidence_and_id_sequence(self):
        task, _ = self._confirmed_app_case()
        data = self.lab.to_snapshot()
        restored = Lab.from_snapshot(data, clock=self.clock)
        report = restored.task_report(task["task_id"])
        self.assertEqual(report["findings"][0]["status"], FINDING_CONFIRMED)
        view = restored.subject_view(SUBJECT_APP, APP_ID)
        self.assertEqual(view["cycles"][0]["notices"][0]["finding_count"], 1)
        new_device = restored.register_device(
            {"device_id": "device-C", "model": "Pixel 10", "os_version": "Android 15"})
        self.assertTrue(new_device["device_id"])  # ID 序列不与既有编号冲突

    def _finding(self, task, rule_id):
        rows = [f for f in self.lab.findings.values()
                if f["task_id"] == task["task_id"] and f["rule_id"] == rule_id]
        return rows[0] if rows else None


if __name__ == "__main__":
    unittest.main()
