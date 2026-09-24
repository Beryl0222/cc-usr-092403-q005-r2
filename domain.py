"""移动广告合规实验室的领域模型与规则引擎。

设计要点（与专项整治业务约定一一对应）：

* 证据只增不改：设备、构建、事件一经登记只能追加，新版构建与旧版并存，
  复核员不能修改或删除原始事件，只能对规则发现作确认/驳回。
* 采集幂等：上报事件带客户端事件号，重传去重；迟到事件允许补录，重新判定
  只会追加新发现，不会抹掉已有结论。
* 版本固化：任务创建时固化当时生效的规范版本与测试脚本版本，脚本升级只影响
  之后创建的任务；复测记录固定当时的脚本、规范、设备、构建（含组件与指纹）。
* 规则与复核分离：自动规则只能产生「涉嫌」发现，复核员确认后才能生成告知材料。
* 承诺驱动整改：每条已确认问题连接责任方、整改承诺、目标构建、适用人群轨道与
  期限；复测按承诺逐项验证，单项关闭不结束周期，全部履行才关闭周期。
* 复发关联提议制：新版本复用旧组件或行为指纹时，系统只「提议」复发关联并给出
  依据；确认权属于具备权限的审核人员。
* 逾期升级只触达对应责任方；告知回执与升级通知只增不改，永久保留。
* 复测幂等：重复上传、离线补传、并发复测以幂等键/任务自然键去重，只产生一次
  状态推进；待复测事项可在重启后接续执行。
"""

from dataclasses import dataclass, field
from itertools import count
from time import time

# 三条标准操作轨迹
TRACK_NORMAL = "normal"            # 正常用户
TRACK_SCREEN_READER = "screen_reader"  # 读屏用户（TalkBack 等）
TRACK_ELDERLY = "elderly"          # 老人模式
TRACKS = (TRACK_NORMAL, TRACK_SCREEN_READER, TRACK_ELDERLY)
TRACK_LABELS = {
    TRACK_NORMAL: "正常用户",
    TRACK_SCREEN_READER: "读屏用户",
    TRACK_ELDERLY: "老人模式",
}

EVENT_AD_SHOWN = "ad_shown"                # 广告展示（锁屏画报/开屏弹窗/插屏等）
EVENT_CLOSE_AFFORDANCE = "close_affordance"  # 关闭入口状态
EVENT_SENSOR_READING = "sensor_reading"    # 摇一摇传感器读数
EVENT_JUMP = "jump"                        # 跳转行为
EVENT_NETWORK_RESPONSE = "network_response"  # 网络响应
EVENT_GESTURE = "gesture"                  # 用户真实操作（用于证明有无交互）
EVENT_TYPES = (
    EVENT_AD_SHOWN,
    EVENT_CLOSE_AFFORDANCE,
    EVENT_SENSOR_READING,
    EVENT_JUMP,
    EVENT_NETWORK_RESPONSE,
    EVENT_GESTURE,
)

SUBJECT_APP = "app"
SUBJECT_ADVERTISER = "advertiser"
SUBJECT_SDK = "sdk"
SUBJECT_TYPES = (SUBJECT_APP, SUBJECT_ADVERTISER, SUBJECT_SDK)
SUBJECT_LABELS = {
    SUBJECT_APP: "应用运营者",
    SUBJECT_ADVERTISER: "广告主",
    SUBJECT_SDK: "嵌入SDK",
}

FINDING_SUSPECTED = "suspected"
FINDING_CONFIRMED = "confirmed"
FINDING_DISMISSED = "dismissed"

CASE_OPEN = "open"
CASE_RECTIFIED = "rectified"

COMMITMENT_OPEN = "open"
COMMITMENT_FULFILLED = "fulfilled"

RETEST_PENDING = "pending"
RETEST_PASSED = "passed"
RETEST_FAILED = "failed"

RELAPSE_PROPOSED = "proposed"
RELAPSE_CONFIRMED = "confirmed"
RELAPSE_REJECTED = "rejected"

# 访问角色：监管人员/复核员拥有审核与全局视图，责任方只能取得自己的材料
ROLE_REGULATOR = "regulator"
ROLE_REVIEWER = "reviewer"
ROLE_PARTY = "party"
STAFF_ROLES = (ROLE_REGULATOR, ROLE_REVIEWER)

# 通知类型：告知送达回执、逾期升级
NOTICE_DELIVERED = "notice_delivered"
ESCALATION_OVERDUE = "escalation_overdue"

# 规则编号（对承办人员与告知材料保持稳定）
RULE_NO_CLOSE_PATH = "R-CLOSE-001"       # 不存在可操作的关闭路径
RULE_SHAKE_THRESHOLD = "R-SHAKE-001"     # 摇一摇阈值/读数时间低于规范
RULE_AUTO_JUMP = "R-JUMP-001"            # 无用户操作自动跳转

RULE_LABELS = {
    RULE_NO_CLOSE_PATH: "广告缺少可操作关闭路径",
    RULE_SHAKE_THRESHOLD: "摇一摇广告传感器阈值低于规范",
    RULE_AUTO_JUMP: "无用户操作自动跳转或误导跳转",
}

# 缺省规范参数，可由 register_regulation 按版本覆盖
DEFAULT_REGULATION_PARAMS = {
    "close_max_delay_seconds": 3.0,       # 开屏后关闭入口最迟出现时间
    "close_min_touch_target_dp": 44.0,    # 普通模式关闭入口最小可点尺寸
    "close_elderly_min_touch_target_dp": 56.0,  # 老人模式放大后的要求
    "shake_min_acceleration": 15.0,       # 触发摇一摇的最小加速度 m/s^2
    "shake_min_rotation_deg": 35.0,       # 最小旋转角度
    "shake_min_reading_seconds": 3.0,     # 最短持续读数时间
    "rectification_days": 15,             # 整改期限（自然日）
}


class DomainError(ValueError):
    """请求不符合领域约束（字段缺失、状态不允许等）。"""


class NotFoundError(LookupError):
    """引用的实体不存在。"""


class AccessDeniedError(Exception):
    """当前角色无权执行该操作或查看该材料。"""


def _require(payload, key, entity):
    if key not in payload or payload[key] in (None, ""):
        raise DomainError(f"{entity}缺少必填字段：{key}")
    return payload[key]


def _subject_key(subject):
    return (subject["type"], subject["id"])


def _display_subject(subject):
    return {"type": subject["type"], "id": subject["id"], "name": subject.get("name", "")}


@dataclass
class Lab:
    """合规实验室的内存仓储与全部业务操作（可整体序列化为 JSON 快照）。"""

    clock: callable = time
    _seq: count = field(default_factory=lambda: count(1), repr=False)
    regulations: dict = field(default_factory=dict)       # version -> regulation
    regulation_order: list = field(default_factory=list)
    scripts: dict = field(default_factory=dict)           # (script_id, version) -> script
    devices: dict = field(default_factory=dict)
    builds: dict = field(default_factory=dict)
    components: dict = field(default_factory=dict)        # component_id -> 组件档案
    tasks: dict = field(default_factory=dict)
    events: dict = field(default_factory=dict)            # (task_id, event_id) -> event
    findings: dict = field(default_factory=dict)
    cases: dict = field(default_factory=dict)             # (type,id) -> case
    notices: dict = field(default_factory=dict)
    commitments: dict = field(default_factory=dict)       # commitment_id
    relapses: dict = field(default_factory=dict)          # relapse_id
    notifications: dict = field(default_factory=dict)     # 只增通知与送达回执
    pending_retests: dict = field(default_factory=dict)   # dedupe_key -> 待复测
    retest_dedup: dict = field(default_factory=dict)      # 幂等键/自然键 -> 复测结果

    def _new_id(self, prefix):
        return f"{prefix}-{next(self._seq):04d}"

    # ------------------------------------------------------------------ #
    # 角色与权限
    # ------------------------------------------------------------------ #

    @staticmethod
    def _actor(actor):
        if actor is None:
            # 领域内直接调用视为系统/监管操作；HTTP 层必须始终显式传入角色
            return {"role": ROLE_REGULATOR, "subject": None, "id": "system"}
        role = actor.get("role")
        if role not in (ROLE_REGULATOR, ROLE_REVIEWER, ROLE_PARTY):
            raise DomainError(f"未知角色：{role}")
        subject = None
        if actor.get("subject_type") and actor.get("subject_id"):
            subject = (actor["subject_type"], actor["subject_id"])
        if role == ROLE_PARTY and subject is None:
            raise DomainError("责任方访问必须携带 subject_type 与 subject_id")
        return {"role": role, "subject": subject, "id": actor.get("id", "")}

    def _require_staff(self, actor):
        normalized = self._actor(actor)
        if normalized["role"] not in STAFF_ROLES:
            raise AccessDeniedError("该操作仅监管人员或复核员可执行")
        return normalized

    def _require_subject_access(self, actor, key):
        """监管/复核可访问任意主体；责任方只能访问自己。"""
        normalized = self._actor(actor)
        if normalized["role"] in STAFF_ROLES:
            return normalized
        if normalized["subject"] != key:
            raise AccessDeniedError("责任方只能取得本主体的整改材料")
        return normalized

    def _require_self_or_staff(self, actor, key):
        """责任方可对自己提交材料（如整改承诺），其余写操作仅工作人员。"""
        normalized = self._actor(actor)
        if normalized["role"] in STAFF_ROLES:
            return normalized
        if normalized["subject"] != key:
            raise AccessDeniedError("只能对本责任主体提交材料")
        return normalized

    # ------------------------------------------------------------------ #
    # 基础档案：规范版本、脚本版本、设备、应用构建
    # ------------------------------------------------------------------ #

    def register_regulation(self, payload):
        """登记一版规范；参数缺省继承 DEFAULT_REGULATION_PARAMS。"""
        version = _require(payload, "version", "规范")
        if version in self.regulations:
            raise DomainError(f"规范版本已存在且不可覆盖：{version}")
        params = dict(DEFAULT_REGULATION_PARAMS)
        params.update(payload.get("params") or {})
        regulation = {
            "version": version,
            "title": payload.get("title", f"移动广告合规规范 {version}"),
            "effective_at": _require(payload, "effective_at", "规范"),
            "params": params,
            "registered_at": self.clock(),
        }
        self.regulations[version] = regulation
        self.regulation_order.append(version)
        return regulation

    def register_script(self, payload):
        """登记一版测试脚本；已创建的任务不会因新版本而改变。"""
        script_id = _require(payload, "script_id", "测试脚本")
        version = _require(payload, "version", "测试脚本")
        key = (script_id, version)
        if key in self.scripts:
            raise DomainError(f"测试脚本版本已存在：{script_id}@{version}")
        script = {
            "script_id": script_id,
            "version": version,
            "created_at": payload.get("created_at", self.clock()),
            "notes": payload.get("notes", ""),
        }
        self.scripts[key] = script
        return script

    def register_device(self, payload):
        device_id = _require(payload, "device_id", "设备")
        if device_id in self.devices:
            raise DomainError(f"设备已登记：{device_id}")
        device = {
            "device_id": device_id,
            "model": _require(payload, "model", "设备"),
            "os_version": _require(payload, "os_version", "设备"),
            "registered_at": self.clock(),
        }
        self.devices[device_id] = device
        return device

    def _normalize_components(self, raw_components):
        components = []
        for raw in raw_components or []:
            component_id = _require(raw, "component_id", "构建组件")
            component = {
                "component_id": component_id,
                "kind": raw.get("kind", "unknown"),
                "name": raw.get("name", ""),
                "version": raw.get("version"),
            }
            components.append(component)
            # 组件档案只增不改；同一组件再次出现不覆盖既有登记
            if component_id not in self.components:
                self.components[component_id] = {
                    "component_id": component_id,
                    "kind": component["kind"],
                    "name": component["name"],
                }
        return components

    @staticmethod
    def _normalize_fingerprints(raw):
        fingerprints = []
        for item in raw or []:
            if isinstance(item, dict):
                fp_type = _require(item, "type", "行为指纹")
                value = _require(item, "value", "行为指纹")
            else:
                raise DomainError("行为指纹必须包含 type 与 value")
            fingerprints.append({"type": fp_type, "value": value})
        return fingerprints

    def register_build(self, payload):
        """登记应用构建。开发者提交新版得到新 build_id，旧构建证据原样保留。

        可随构建登记复用的第三方组件（components）与行为指纹（fingerprints），
        供新版本复测时提出复发关联。
        """
        app_id = _require(payload, "app_id", "应用")
        version_code = _require(payload, "version_code", "应用构建")
        build_id = payload.get("build_id") or f"{app_id}:{version_code}"
        if build_id in self.builds:
            raise DomainError(f"应用构建已存在，禁止覆盖旧证据：{build_id}")
        build = {
            "build_id": build_id,
            "app_id": app_id,
            "app_name": _require(payload, "app_name", "应用"),
            "developer": _require(payload, "developer", "应用"),
            "version_code": version_code,
            "version_name": payload.get("version_name", str(version_code)),
            "submitted_at": payload.get("submitted_at", self.clock()),
            "components": self._normalize_components(payload.get("components")),
            "fingerprints": self._normalize_fingerprints(payload.get("fingerprints")),
        }
        self.builds[build_id] = build
        return build

    def _regulation_at(self, at, explicit_version=None):
        if explicit_version is not None:
            if explicit_version not in self.regulations:
                raise NotFoundError(f"规范版本不存在：{explicit_version}")
            return self.regulations[explicit_version]
        effective = [
            r for r in self.regulations.values() if r["effective_at"] <= at
        ]
        if not effective:
            raise DomainError("尚无在该时点生效的规范版本，请显式指定 regulation_version")
        return max(effective, key=lambda r: r["effective_at"])

    def _latest_script(self, at):
        available = [s for s in self.scripts.values() if s["created_at"] <= at]
        if not available:
            return None
        return max(available, key=lambda s: (s["created_at"], s["version"]))

    # ------------------------------------------------------------------ #
    # 测试任务：同一构建在不同设备/轨迹上并存
    # ------------------------------------------------------------------ #

    def create_task(self, payload):
        build_id = _require(payload, "build_id", "任务")
        device_id = _require(payload, "device_id", "任务")
        track = payload.get("track", TRACK_NORMAL)
        if build_id not in self.builds:
            raise NotFoundError(f"应用构建不存在：{build_id}")
        if device_id not in self.devices:
            raise NotFoundError(f"设备不存在：{device_id}")
        if track not in TRACKS:
            raise DomainError(f"未知操作轨迹：{track}，允许值：{', '.join(TRACKS)}")
        now = self.clock()
        regulation = self._regulation_at(now, payload.get("regulation_version"))
        script = self._latest_script(now)
        task = {
            "task_id": self._new_id("task"),
            "build_id": build_id,
            "device_id": device_id,
            "track": track,
            "accessibility": {
                "screen_reader_enabled": track == TRACK_SCREEN_READER
                or bool(payload.get("screen_reader_enabled", False)),
                "elderly_mode_enabled": track == TRACK_ELDERLY
                or bool(payload.get("elderly_mode_enabled", False)),
            },
            "regulation_version": regulation["version"],  # 固化
            "script": None if script is None else {"script_id": script["script_id"], "version": script["version"]},
            "status": "collecting",
            "created_at": now,
            "completed_at": None,
            "review_count": 0,
            "event_ids": [],
        }
        self.tasks[task["task_id"]] = task
        return task

    def ingest_events(self, task_id, events):
        """幂等接收一批事件。

        同一 (task_id, event_id) 重传直接判重，不产生重复证据；
        任务完成后到达的迟到事件照常追加，并触发追加判定。
        """
        task = self._get_task(task_id)
        accepted, duplicates = [], []
        late = False
        for raw in events:
            event_id = _require(raw, "event_id", "事件")
            dedup_key = (task_id, event_id)
            if dedup_key in self.events:
                duplicates.append(event_id)
                continue
            event_type = _require(raw, "type", "事件")
            if event_type not in EVENT_TYPES:
                raise DomainError(f"未知事件类型：{event_type}")
            event = {
                "event_id": event_id,
                "task_id": task_id,
                "seq": _require(raw, "seq", "事件"),
                "type": event_type,
                "occurred_at": _require(raw, "occurred_at", "事件"),
                "received_at": self.clock(),
                "late": task["status"] == "completed",
                "payload": raw.get("payload", {}),
            }
            self.events[dedup_key] = event
            task["event_ids"].append(event_id)
            accepted.append(event_id)
            if event["late"]:
                late = True
        result = {"accepted": accepted, "duplicates": duplicates}
        if accepted and task["status"] == "completed":
            result["review_result"] = self._evaluate(task)
            result["late_arrivals"] = late
        return result

    def complete_task(self, task_id):
        task = self._get_task(task_id)
        if not task["event_ids"]:
            raise DomainError("任务尚无任何事件，不能完成")
        task["status"] = "completed"
        task["completed_at"] = self.clock()
        result = self._evaluate(task)
        # 任务完成时接续任何等待该任务的待复测事项（服务重启后也会再扫一遍）
        self._process_due_retests()
        return result

    def _get_task(self, task_id):
        try:
            return self.tasks[task_id]
        except KeyError:
            raise NotFoundError(f"任务不存在：{task_id}")

    def _task_events(self, task):
        rows = [self.events[(task["task_id"], eid)] for eid in task["event_ids"]]
        return sorted(rows, key=lambda e: (e["occurred_at"], e["seq"]))

    # ------------------------------------------------------------------ #
    # 规则引擎：只产出「涉嫌」发现
    # ------------------------------------------------------------------ #

    def _evaluate(self, task):
        """对任务的全部事件运行规则，按指纹去重追加发现。"""
        regulation = self.regulations[task["regulation_version"]]
        params = regulation["params"]
        events = self._task_events(task)
        build = self.builds[task["build_id"]]
        app_subject = {"type": SUBJECT_APP, "id": build["app_id"], "name": build["developer"]}

        ads = [e for e in events if e["type"] == EVENT_AD_SHOWN]
        new_findings = []

        def first_existing(fingerprint):
            for f in self.findings.values():
                if f["task_id"] == task["task_id"] and f["fingerprint"] == fingerprint:
                    return f
            return None

        def add_finding(rule_id, ad_info, subject, chain, evidence, detail, occurred_at,
                        behavior_signatures=()):
            fingerprint = "|".join(
                [rule_id, str(ad_info.get("ad_id", "")), str(ad_info.get("placement", "")),
                 _subject_key(subject)[0], _subject_key(subject)[1]] + sorted(evidence)
            )
            existing = first_existing(fingerprint)
            if existing:
                return existing
            finding = {
                "finding_id": self._new_id("finding"),
                "task_id": task["task_id"],
                "build_id": task["build_id"],
                "rule_id": rule_id,
                "rule_title": RULE_LABELS[rule_id],
                "status": FINDING_SUSPECTED,
                "responsible_subject": _display_subject(subject),
                "responsibility_chain": chain,
                "ad": ad_info,
                "evidence_event_ids": sorted(evidence),
                "detail": detail,
                "observed_at": occurred_at,
                "regulation_version": task["regulation_version"],
                "created_at": self.clock(),
                "reviewed_by": None,
                "reviewed_at": None,
                "review_comment": None,
                "fingerprint": fingerprint,
                # 与具体广告号解耦的行为指纹：供跨构建复发比对
                "behavior_fingerprints": sorted(set(behavior_signatures)),
                "case_id": None,
                "case_cycle_seq": None,
                "notice_id": None,
                "relapse_id": None,
            }
            self.findings[finding["finding_id"]] = finding
            new_findings.append(finding)
            return finding

        for ad_event in ads:
            ad_payload = ad_event["payload"]
            ad_info = {
                "ad_id": ad_payload.get("ad_id", ad_event["event_id"]),
                "placement": ad_payload.get("placement", "unknown"),
                "creative_id": ad_payload.get("creative_id"),
            }
            refs = ad_info["ad_id"]
            related = [
                e for e in events
                if e["occurred_at"] >= ad_event["occurred_at"]
                and e["payload"].get("ad_id") in (refs, None)
            ]
            affs = [e for e in related if e["type"] == EVENT_CLOSE_AFFORDANCE and e["payload"].get("ad_id") == refs]
            jumps = [e for e in related if e["type"] == EVENT_JUMP and e["payload"].get("ad_id") == refs]

            chain = {"app": _display_subject(app_subject), "advertiser": None, "sdk": None}

            # 规则一：关闭路径是否存在且对当前轨迹可操作
            reasons, close_detail = self._assess_close_path(task, affs, params)
            if reasons:
                detail = {"close_path": close_detail, "violations": reasons}
                add_finding(
                    RULE_NO_CLOSE_PATH, ad_info, app_subject, chain,
                    [ad_event["event_id"]] + [e["event_id"] for e in affs],
                    detail, ad_event["occurred_at"],
                    behavior_signatures=[f"behavior:{RULE_NO_CLOSE_PATH}:{ad_info['placement']}"],
                )

            # 跳转类规则：自动跳转 / 摇一摇阈值
            gestures = [e for e in related if e["type"] == EVENT_GESTURE]
            for jump in jumps:
                jp = jump["payload"]
                sdk = self._subject_from_payload(jp.get("sdk"), SUBJECT_SDK)
                advertiser = self._subject_from_payload(
                    jp.get("advertiser") or ad_payload.get("advertiser"), SUBJECT_ADVERTISER
                )
                jump_chain = {
                    "app": _display_subject(app_subject),
                    "advertiser": None if advertiser is None else _display_subject(advertiser),
                    "sdk": None if sdk is None else _display_subject(sdk),
                }
                trigger = jp.get("trigger", "auto")
                evidence = [ad_event["event_id"], jump["event_id"]]
                # 行为指纹优先绑定复用来源（SDK / 广告主），与广告号无关
                source = sdk or advertiser
                source_sig = (
                    f"{source['type']}:{source['id']}" if source is not None
                    else f"{SUBJECT_APP}:{build['app_id']}"
                )

                if trigger == "shake":
                    sensors = [
                        e for e in related
                        if e["type"] == EVENT_SENSOR_READING
                        and e["payload"].get("ad_id") == refs
                        and e["occurred_at"] <= jump["occurred_at"]
                    ]
                    violations, sensor_detail = self._assess_shake(sensors, jp, params)
                    if violations:
                        subject = sdk or advertiser or app_subject
                        detail = {
                            "jump": self._jump_summary(jump, gestures),
                            "sensor": sensor_detail,
                            "violations": violations,
                        }
                        evidence += [e["event_id"] for e in sensors]
                        add_finding(RULE_SHAKE_THRESHOLD, ad_info, subject, jump_chain,
                                    evidence, detail, jump["occurred_at"],
                                    behavior_signatures=[
                                        f"behavior:{RULE_SHAKE_THRESHOLD}:{source_sig}"])
                elif trigger == "auto":
                    subject = sdk or advertiser or app_subject
                    detail = {"jump": self._jump_summary(jump, gestures),
                              "violations": ["跳转发生前无任何用户操作记录"]}
                    add_finding(RULE_AUTO_JUMP, ad_info, subject, jump_chain,
                                evidence, detail, jump["occurred_at"],
                                behavior_signatures=[
                                    f"behavior:{RULE_AUTO_JUMP}:{source_sig}"])

        task["review_count"] += 1
        return {
            "task_id": task["task_id"],
            "regulation_version": task["regulation_version"],
            "new_suspected_findings": [f["finding_id"] for f in new_findings],
            "suspected_total": len([
                f for f in self.findings.values()
                if f["task_id"] == task["task_id"] and f["status"] == FINDING_SUSPECTED
            ]),
        }

    @staticmethod
    def _subject_from_payload(raw, subject_type):
        if not raw or not raw.get("id"):
            return None
        return {"type": subject_type, "id": raw["id"], "name": raw.get("name", "")}

    def _assess_close_path(self, task, aff_events, params):
        """返回（违规原因列表，关闭路径快照）。"""
        detail = {
            "exists": bool(aff_events),
            "operable": False,
            "visible_after_seconds": None,
            "touch_target_dp": None,
            "screen_reader_actionable": None,
            "track": task["track"],
        }
        if not aff_events:
            return ["广告展示期间未上报任何关闭入口"], detail
        aff = min(aff_events, key=lambda e: e["occurred_at"])
        p = aff["payload"]
        detail["visible_after_seconds"] = p.get("visible_after_seconds")
        detail["touch_target_dp"] = p.get("touch_target_dp")
        detail["screen_reader_actionable"] = p.get("screen_reader_actionable")
        detail["dismiss_label"] = p.get("label")

        reasons = []
        if p.get("present") is False:
            reasons.append("关闭入口不存在")
        if p.get("visible_after_seconds") is not None \
                and p["visible_after_seconds"] > params["close_max_delay_seconds"]:
            reasons.append(
                f"关闭入口迟至 {p['visible_after_seconds']}s 出现，"
                f"超过 {params['close_max_delay_seconds']}s"
            )
        size = p.get("touch_target_dp")
        if size is not None:
            floor = (params["close_elderly_min_touch_target_dp"] if task["track"] == TRACK_ELDERLY
                     else params["close_min_touch_target_dp"])
            if size < floor:
                reasons.append(f"可点区域 {size}dp 小于 {TRACK_LABELS[task['track']]}要求的 {floor}dp")
        if task["track"] == TRACK_SCREEN_READER and not p.get("screen_reader_actionable", False):
            reasons.append("读屏模式下关闭入口不可聚焦或无操作标签")
        detail["operable"] = not reasons
        return reasons, detail

    @staticmethod
    def _assess_shake(sensor_events, jump_payload, params):
        peak_accel = jump_payload.get("peak_acceleration")
        peak_rotation = jump_payload.get("peak_rotation_deg")
        reading_seconds = jump_payload.get("reading_seconds")
        for e in sensor_events:
            p = e["payload"]
            peak_accel = p.get("peak_acceleration", peak_accel)
            peak_rotation = p.get("peak_rotation_deg", peak_rotation)
            reading_seconds = p.get("reading_seconds", reading_seconds)
        detail = {
            "peak_acceleration": peak_accel,
            "peak_rotation_deg": peak_rotation,
            "reading_seconds": reading_seconds,
            "thresholds": {
                "min_acceleration": params["shake_min_acceleration"],
                "min_rotation_deg": params["shake_min_rotation_deg"],
                "min_reading_seconds": params["shake_min_reading_seconds"],
            },
        }
        violations = []
        if peak_accel is not None and peak_accel < params["shake_min_acceleration"]:
            violations.append(f"峰值加速度 {peak_accel} m/s² 低于下限 {params['shake_min_acceleration']}")
        if peak_rotation is not None and peak_rotation < params["shake_min_rotation_deg"]:
            violations.append(f"旋转角度 {peak_rotation}° 低于下限 {params['shake_min_rotation_deg']}")
        if reading_seconds is not None and reading_seconds < params["shake_min_reading_seconds"]:
            violations.append(f"读数持续 {reading_seconds}s 短于下限 {params['shake_min_reading_seconds']}s")
        return violations, detail

    @staticmethod
    def _jump_summary(jump_event, gesture_events):
        p = jump_event["payload"]
        prior_gesture = any(g["occurred_at"] <= jump_event["occurred_at"] for g in gesture_events)
        return {
            "event_id": jump_event["event_id"],
            "at": jump_event["occurred_at"],
            "trigger": p.get("trigger"),
            "target_url": p.get("target_url"),
            "user_gesture_before_jump": prior_gesture,
            "sdk": p.get("sdk"),
            "advertiser": p.get("advertiser"),
        }

    # ------------------------------------------------------------------ #
    # 复核、整改案件与复发关联提议
    # ------------------------------------------------------------------ #

    def review_finding(self, finding_id, payload, actor=None):
        self._require_staff(actor)
        try:
            finding = self.findings[finding_id]
        except KeyError:
            raise NotFoundError(f"发现不存在：{finding_id}")
        decision = _require(payload, "decision", "复核结论")
        if decision not in (FINDING_CONFIRMED, FINDING_DISMISSED):
            raise DomainError("复核结论只能是 confirmed 或 dismissed")
        if finding["status"] in (FINDING_CONFIRMED, FINDING_DISMISSED):
            raise DomainError("发现已经复核，结论不可更改")
        reviewer = _require(payload, "reviewer", "复核结论")
        finding["status"] = decision
        finding["reviewed_by"] = reviewer
        finding["reviewed_at"] = self.clock()
        finding["review_comment"] = payload.get("comment", "")
        if decision == FINDING_CONFIRMED:
            case, cycle = self._attach_to_case(finding)
            self._propose_relapse_if_recurring(case, cycle, finding)
        return finding

    def _attach_to_case(self, finding):
        subject = finding["responsible_subject"]
        key = _subject_key(subject)
        case = self.cases.get(key)
        if case is None:
            case = {
                "case_id": self._new_id("case"),
                "responsible_subject": _display_subject(subject),
                "status": CASE_OPEN,
                "relapse_count": 0,
                "cycles": [],
                "created_at": self.clock(),
            }
            self.cases[key] = case
        if case["cycles"] and case["cycles"][-1]["status"] == CASE_RECTIFIED:
            # 已整改后再次出现问题：开启新周期，旧周期作为回潮历史保留
            case["status"] = CASE_OPEN
            case["relapse_count"] += 1
        if not case["cycles"] or case["cycles"][-1]["status"] == CASE_RECTIFIED:
            case["cycles"].append({
                "seq": len(case["cycles"]) + 1,
                "status": CASE_OPEN,
                "opened_at": self.clock(),
                "rectified_at": None,
                "finding_ids": [],
                "notice_ids": [],
                "commitment_ids": [],
                "retests": [],
            })
        cycle = case["cycles"][-1]
        if finding["finding_id"] not in cycle["finding_ids"]:
            cycle["finding_ids"].append(finding["finding_id"])
        finding["case_id"] = case["case_id"]
        finding["case_cycle_seq"] = cycle["seq"]
        return case, cycle

    def _propose_relapse_if_recurring(self, case, cycle, finding):
        """新确认的问题发生在既有「已整改」周期之后时，提出复发关联。

        依据包括：同规则问题重现、新版本复用旧组件、行为指纹命中。
        系统只提议，是否确认复发由具备权限的审核人员决定。
        """
        prior_cycles = [c for c in case["cycles"][:-1] if c["status"] == CASE_RECTIFIED]
        if not prior_cycles:
            return None
        new_build = self.builds.get(finding["build_id"], {})
        new_components = {c["component_id"] for c in new_build.get("components", [])}
        new_declared = {
            fp["value"] for fp in new_build.get("fingerprints", []) if fp["type"] == "behavior"
        }
        new_sigs = set(finding.get("behavior_fingerprints", []))

        bases = []
        seen = set()

        def add_basis(kind, detail, prior):
            marker = (kind, detail)
            if marker in seen:
                return
            seen.add(marker)
            bases.append({
                "kind": kind,
                "detail": detail,
                "prior_finding_id": prior["finding_id"],
                "prior_rule_id": prior["rule_id"],
                "prior_build_id": prior["build_id"],
                "prior_cycle_seq": prior["case_cycle_seq"],
            })

        for prior_cycle in prior_cycles:
            for fid in prior_cycle["finding_ids"]:
                prior = self.findings[fid]
                prior_build = self.builds.get(prior["build_id"], {})
                prior_components = {c["component_id"] for c in prior_build.get("components", [])}
                prior_declared = {
                    fp["value"] for fp in prior_build.get("fingerprints", [])
                    if fp["type"] == "behavior"
                }
                if prior["rule_id"] == finding["rule_id"]:
                    add_basis("same_rule",
                              f"同一规则 {prior['rule_id']} 在已整改周期再次出现", prior)
                for component_id in sorted(new_components & prior_components):
                    meta = self.components.get(component_id, {})
                    add_basis("component",
                              f"新版本复用已整改构建的组件：{component_id}"
                              f"（{meta.get('name', '')}）", prior)
                for value in sorted(new_declared & set(prior.get("behavior_fingerprints", []))):
                    add_basis("behavior", f"行为指纹命中旧问题：{value}", prior)
                for value in sorted(new_sigs & prior_declared):
                    add_basis("behavior", f"行为指纹命中旧构建声明：{value}", prior)
                for value in sorted(new_declared & prior_declared):
                    add_basis("behavior", f"新旧构建声明同一行为指纹：{value}", prior)

        if not bases:
            return None
        relapse = {
            "relapse_id": self._new_id("relapse"),
            "case_id": case["case_id"],
            "cycle_seq": cycle["seq"],
            "responsible_subject": _display_subject(case["responsible_subject"]),
            "new_finding_id": finding["finding_id"],
            "new_build_id": finding["build_id"],
            "new_rule_id": finding["rule_id"],
            "bases": bases,
            "status": RELAPSE_PROPOSED,
            "proposed_by": "system",
            "proposed_at": self.clock(),
            "decided_by": None,
            "decided_at": None,
            "decision_comment": None,
        }
        self.relapses[relapse["relapse_id"]] = relapse
        finding["relapse_id"] = relapse["relapse_id"]
        return relapse

    def decide_relapse(self, relapse_id, payload, actor=None):
        """授权审核人员确认或驳回复发关联；不影响原始发现与周期事实。"""
        self._require_staff(actor)
        try:
            relapse = self.relapses[relapse_id]
        except KeyError:
            raise NotFoundError(f"复发关联不存在：{relapse_id}")
        if relapse["status"] != RELAPSE_PROPOSED:
            raise DomainError("复发关联已经过审核，不可重复决定")
        decision = _require(payload, "decision", "复发审核")
        if decision not in (RELAPSE_CONFIRMED, RELAPSE_REJECTED):
            raise DomainError("复发审核结论只能是 confirmed 或 rejected")
        relapse["status"] = decision
        relapse["decided_by"] = _require(payload, "reviewer", "复发审核")
        relapse["decided_at"] = self.clock()
        relapse["decision_comment"] = payload.get("comment", "")
        return relapse

    def generate_notice(self, subject_type, subject_id, payload, actor=None):
        """对已确认发现生成告知材料；涉嫌发现一律不得进入材料。"""
        self._require_staff(actor)
        key = (subject_type, subject_id)
        case = self.cases.get(key)
        if case is None or not case["cycles"]:
            raise NotFoundError("该责任主体尚无整改案件")
        cycle = case["cycles"][-1]
        if cycle["status"] != CASE_OPEN:
            raise DomainError("当前整改周期已关闭，不能重复出具告知材料")
        reviewer = _require(payload, "issued_by", "告知材料")
        pending = [
            f for f in (self.findings[fid] for fid in cycle["finding_ids"])
            if f["status"] == FINDING_CONFIRMED and not f.get("notice_id")
        ]
        if not pending:
            raise DomainError("没有尚未告知的已确认发现")
        # 整改期限取各发现所依据规范中最严格（最短）的一个
        deadline_days = min(
            self.regulations[f["regulation_version"]]["params"]["rectification_days"]
            for f in pending
        )
        issued_at = self.clock()
        notice = {
            "notice_id": self._new_id("notice"),
            "case_id": case["case_id"],
            "cycle_seq": cycle["seq"],
            "responsible_subject": _display_subject(case["responsible_subject"]),
            "issued_by": reviewer,
            "issued_at": issued_at,
            "rectification_deadline": issued_at + deadline_days * 86400,
            "rectification_days": deadline_days,
            "regulation_versions": sorted({f["regulation_version"] for f in pending}),
            # 快照：后续任何操作都不得改动材料内容
            "findings": [self._finding_snapshot(f) for f in pending],
        }
        self.notices[notice["notice_id"]] = notice
        cycle["notice_ids"].append(notice["notice_id"])
        for f in pending:
            f["notice_id"] = notice["notice_id"]
        # 告知送达即产生只增回执，仅送达该责任主体
        self._notify(NOTICE_DELIVERED, case["responsible_subject"], cycle["seq"],
                     notice["notice_id"], issued_at)
        return notice

    # ------------------------------------------------------------------ #
    # 整改承诺：问题 -> 责任方/目标构建/轨道/期限
    # ------------------------------------------------------------------ #

    def submit_commitment(self, subject_type, subject_id, payload, actor=None):
        """责任方（或工作人员代录）针对已确认问题提交整改承诺。"""
        if subject_type not in SUBJECT_TYPES:
            raise DomainError(f"未知责任主体类型：{subject_type}")
        key = (subject_type, subject_id)
        actor_info = self._require_self_or_staff(actor, key)
        case = self.cases.get(key)
        if case is None or not case["cycles"]:
            raise NotFoundError("该责任主体尚无整改案件")
        cycle = case["cycles"][-1]
        if cycle["status"] != CASE_OPEN:
            raise DomainError("当前整改周期已关闭，不能再提交承诺")
        finding_id = _require(payload, "finding_id", "整改承诺")
        finding = self.findings.get(finding_id)
        if finding is None:
            raise NotFoundError(f"发现不存在：{finding_id}")
        if finding["status"] != FINDING_CONFIRMED:
            raise DomainError("只有已确认的问题才能建立整改承诺")
        if _subject_key(finding["responsible_subject"]) != key:
            raise DomainError("该问题不属于此责任主体")
        if finding.get("case_cycle_seq") != cycle["seq"]:
            raise DomainError("该问题不属于当前整改周期")
        target_build_id = _require(payload, "target_build_id", "整改承诺")
        if target_build_id not in self.builds:
            raise NotFoundError(f"目标构建不存在：{target_build_id}")
        tracks = payload.get("target_tracks") or list(TRACKS)
        unknown = [t for t in tracks if t not in TRACKS]
        if unknown:
            raise DomainError(f"未知适用轨道：{', '.join(unknown)}")
        for cid in cycle["commitment_ids"]:
            existing = self.commitments[cid]
            if existing["finding_id"] == finding_id and existing["status"] == COMMITMENT_OPEN:
                raise DomainError("该问题已存在未履行的整改承诺，禁止重复提交")
        deadline = payload.get("deadline")
        if deadline is None:
            deadline = self._notice_deadline_for(cycle, finding_id)
            if deadline is None:
                days = self.regulations[finding["regulation_version"]]["params"]["rectification_days"]
                deadline = self.clock() + days * 86400
        commitment = {
            "commitment_id": self._new_id("commit"),
            "case_id": case["case_id"],
            "cycle_seq": cycle["seq"],
            "responsible_subject": _display_subject(case["responsible_subject"]),
            "finding_id": finding_id,
            "rule_id": finding["rule_id"],
            "rule_title": finding["rule_title"],
            "target_build_id": target_build_id,
            "target_tracks": tracks,
            "deadline": deadline,
            "promised_at": self.clock(),
            "promised_by": payload.get("promised_by", actor_info["id"]),
            "status": COMMITMENT_OPEN,
            "source_notice_id": finding.get("notice_id"),
            "fulfillment": None,
            "escalation_notification_id": None,
            "escalated_at": None,
        }
        self.commitments[commitment["commitment_id"]] = commitment
        cycle["commitment_ids"].append(commitment["commitment_id"])
        return commitment

    def _notice_deadline_for(self, cycle, finding_id):
        deadlines = [
            n["rectification_deadline"]
            for n in (self.notices[nid] for nid in cycle["notice_ids"])
            if any(f["finding_id"] == finding_id for f in n["findings"])
        ]
        return min(deadlines) if deadlines else None

    # ------------------------------------------------------------------ #
    # 复测：固化基线、逐项验证、幂等、可接续
    # ------------------------------------------------------------------ #

    @staticmethod
    def _natural_retest_key(subject_type, subject_id, task_id):
        return f"natural:{subject_type}:{subject_id}:{task_id}"

    def record_retest(self, subject_type, subject_id, payload, actor=None):
        """登记一次复测。

        * 复测固定当时的脚本、规范、设备与构建摘要（含组件/指纹）；
        * 按承诺逐项验证，单项履行只关闭该承诺，全部履行才关闭周期；
        * 以 idempotency_key 与（主体+任务）自然键去重：重复上传、离线补传与
          并发复测都只产生一次状态推进，重复调用原样返回首次结果；
        * 复测任务尚未完成时进入待复测队列，任务完成或服务重启后自动接续。
        """
        self._require_staff(actor)
        if subject_type not in SUBJECT_TYPES:
            raise DomainError(f"未知责任主体类型：{subject_type}")
        key = (subject_type, subject_id)
        case = self.cases.get(key)
        if case is None or not case["cycles"]:
            raise NotFoundError("该责任主体尚无整改案件")
        cycle = case["cycles"][-1]
        task_id = _require(payload, "task_id", "复测")
        task = self._get_task(task_id)
        idem = payload.get("idempotency_key")
        natural_key = self._natural_retest_key(subject_type, subject_id, task_id)
        dedupe_key = idem or natural_key

        # 幂等键与（主体+任务）自然键任一命中，都原样返回首次结果
        # （即使首次复测已关闭周期，重复/补传请求也不得报错或再次推进）
        for candidate in (idem, natural_key):
            if candidate and candidate in self.retest_dedup:
                return self.retest_dedup[candidate]
        for candidate in (idem, natural_key):
            if candidate and candidate in self.pending_retests:
                pending = self.pending_retests[candidate]
                if task["status"] == "completed" and cycle["status"] == CASE_OPEN:
                    self._drop_pending(dedupe_key, natural_key, idem)
                    return self._execute_retest(case, cycle, task, payload, dedupe_key)
                return pending

        if cycle["status"] != CASE_OPEN:
            raise DomainError("当前周期不在整改中")

        if task["status"] != "completed":
            pending = {
                "result": RETEST_PENDING,
                "dedupe_key": dedupe_key,
                "natural_key": natural_key,
                "idempotency_key": idem,
                "task_id": task_id,
                "responsible_subject": _display_subject(case["responsible_subject"]),
                "cycle_seq": cycle["seq"],
                "queued_at": self.clock(),
                "by": payload.get("by", ""),
                "note": "复测任务尚未完成，已进入待复测队列，任务完成或重启后自动接续",
            }
            self.pending_retests[natural_key] = pending
            if idem:
                self.pending_retests[idem] = pending
            return pending

        return self._execute_retest(case, cycle, task, payload, dedupe_key)

    def _drop_pending(self, *keys):
        seen = set()
        for key in keys:
            if key and key not in seen and key in self.pending_retests:
                seen.add(key)
                del self.pending_retests[key]

    @staticmethod
    def _build_covers_commitment(retest_build, target_build):
        """复测构建须为同一应用、且版本不早于承诺的目标构建。"""
        return (
            retest_build["app_id"] == target_build["app_id"]
            and retest_build["version_code"] >= target_build["version_code"]
        )

    def _retest_baseline(self, task):
        build = self.builds[task["build_id"]]
        device = self.devices[task["device_id"]]
        regulation = self.regulations[task["regulation_version"]]
        return {
            "captured_at": self.clock(),
            "task_id": task["task_id"],
            "track": task["track"],
            "track_label": TRACK_LABELS[task["track"]],
            "regulation_version": task["regulation_version"],
            "regulation_title": regulation["title"],
            "script": task["script"],
            "device": {k: device[k] for k in ("device_id", "model", "os_version")},
            "build": {
                "build_id": build["build_id"],
                "app_id": build["app_id"],
                "app_name": build["app_name"],
                "developer": build["developer"],
                "version_code": build["version_code"],
                "version_name": build["version_name"],
                "components": build.get("components", []),
                "fingerprints": build.get("fingerprints", []),
            },
        }

    def _execute_retest(self, case, cycle, task, payload, dedupe_key):
        key = _subject_key(case["responsible_subject"])
        new_findings = [
            f for f in self.findings.values()
            if f["task_id"] == task["task_id"]
            and _subject_key(f["responsible_subject"]) == key
        ]
        confirmed_new = [f for f in new_findings if f["status"] == FINDING_CONFIRMED]
        suspected_new = [f for f in new_findings if f["status"] == FINDING_SUSPECTED]

        explicit_ids = payload.get("commitment_ids")
        if explicit_ids:
            targets = []
            for cid in explicit_ids:
                commitment = self.commitments.get(cid)
                if commitment is None or commitment["case_id"] != case["case_id"] \
                        or commitment["cycle_seq"] != cycle["seq"]:
                    raise DomainError(f"承诺不属于当前周期：{cid}")
                targets.append(commitment)
        else:
            targets = [self.commitments[cid] for cid in cycle["commitment_ids"]
                       if self.commitments[cid]["status"] == COMMITMENT_OPEN]

        commitment_results = []
        now = self.clock()
        retest_build = self.builds[task["build_id"]]
        for commitment in targets:
            related = [f for f in new_findings if f["rule_id"] == commitment["rule_id"]]
            confirmed_hits = [f for f in related if f["status"] == FINDING_CONFIRMED]
            suspected_hits = [f for f in related if f["status"] == FINDING_SUSPECTED]
            target_build = self.builds[commitment["target_build_id"]]
            build_ok = self._build_covers_commitment(retest_build, target_build)
            if not build_ok:
                result_state = "wrong_build"
            elif confirmed_hits:
                result_state = "still_present"
            else:
                result_state = "verified"
            verified = result_state == "verified"
            commitment_results.append({
                "commitment_id": commitment["commitment_id"],
                "rule_id": commitment["rule_id"],
                "target_build_id": commitment["target_build_id"],
                "retest_build_id": task["build_id"],
                "result": result_state,
                "confirmed_finding_ids": [f["finding_id"] for f in confirmed_hits],
                "suspected_finding_ids": [f["finding_id"] for f in suspected_hits],
            })
            if verified and commitment["status"] == COMMITMENT_OPEN:
                # 单项关闭：仅关闭这一条承诺，不结束整个周期
                commitment["status"] = COMMITMENT_FULFILLED
                commitment["fulfillment"] = {
                    "task_id": task["task_id"],
                    "at": now,
                }

        retest_id = self._new_id("retest")
        covered_rules = {r["rule_id"] for r in commitment_results}
        # 复测中出现没有任何承诺覆盖的已确认问题：整体不得判通过，周期保持开启
        uncovered_confirmed = [
            f["finding_id"] for f in confirmed_new if f["rule_id"] not in covered_rules
        ]
        if targets:
            passed = (all(r["result"] == "verified" for r in commitment_results)
                      and not uncovered_confirmed)
        else:
            # 尚无承诺登记的历史流程：无该主体的已确认新发现即通过
            passed = not confirmed_new
        retest = {
            "retest_id": retest_id,
            "dedupe_key": dedupe_key,
            "idempotency_key": payload.get("idempotency_key"),
            "task_id": task["task_id"],
            "at": now,
            "result": RETEST_PASSED if passed else RETEST_FAILED,
            "suspected_count": len(suspected_new),
            "confirmed_count": len(confirmed_new),
            "commitment_results": commitment_results,
            "uncovered_confirmed_finding_ids": uncovered_confirmed,
            "baseline": self._retest_baseline(task),
            "by": payload.get("by", ""),
            "note": "复测通过仅代表受测承诺在固化基线下未再发现已确认问题；历史问题时段保留"
                    if passed else "复测仍发现已确认问题，未通过的承诺保持开启",
        }
        cycle["retests"].append(retest)

        all_commitments = [self.commitments[cid] for cid in cycle["commitment_ids"]]
        if all_commitments and all(c["status"] == COMMITMENT_FULFILLED for c in all_commitments) \
                and passed:
            # 全部承诺履行完毕且无未覆盖的已确认问题，才关闭整个周期
            cycle["status"] = CASE_RECTIFIED
            cycle["rectified_at"] = now
            case["status"] = CASE_RECTIFIED
        elif not all_commitments:
            # 兼容未登记承诺的旧流程：整周期复测通过即关闭
            if passed:
                cycle["status"] = CASE_RECTIFIED
                cycle["rectified_at"] = now
                case["status"] = CASE_RECTIFIED

        self.retest_dedup[dedupe_key] = retest
        self.retest_dedup[self._natural_retest_key(key[0], key[1], task["task_id"])] = retest
        return retest

    def _process_due_retests(self):
        """接续所有任务已完成的待复测事项（重启恢复后同样调用）。"""
        processed = []
        seen_pending = set()
        for dedupe_key, pending in list(self.pending_retests.items()):
            marker = pending.get("natural_key") or id(pending)
            if marker in seen_pending:
                continue
            seen_pending.add(marker)
            task = self.tasks.get(pending["task_id"])
            if task is None or task["status"] != "completed":
                continue
            key = _subject_key(pending["responsible_subject"])
            case = self.cases.get(key)
            if case is None:
                continue
            cycle = next((c for c in case["cycles"] if c["seq"] == pending["cycle_seq"]), None)
            natural_key = pending.get("natural_key")
            if cycle is None or cycle["status"] != CASE_OPEN:
                self._drop_pending(natural_key, pending.get("idempotency_key"))
                continue
            self._drop_pending(natural_key, pending.get("idempotency_key"))
            retest = self._execute_retest(
                case, cycle, task,
                {"task_id": task["task_id"], "by": pending.get("by", "")},
                natural_key,
            )
            processed.append(retest)
        return processed

    def process_due_retests(self, actor=None):
        self._require_staff(actor)
        return self._process_due_retests()

    # ------------------------------------------------------------------ #
    # 逾期升级与通知回执（只增不改，仅触达对应责任方）
    # ------------------------------------------------------------------ #

    def _notify(self, kind, subject, cycle_seq, related_id, at, extra=None):
        notification = {
            "notification_id": self._new_id("notice-log"),
            "kind": kind,
            "responsible_subject": _display_subject(subject),
            "cycle_seq": cycle_seq,
            "related_id": related_id,
            "sent_at": at,
            "channel": "system",
            # 接收方就是责任方本身，升级不抄送其他主体
            "recipient": _display_subject(subject),
            "receipt": {
                "status": "delivered",
                "delivered_at": at,
                "reference": related_id,
            },
        }
        if extra:
            notification.update(extra)
        self.notifications[notification["notification_id"]] = notification
        return notification

    def run_overdue_checks(self, payload=None, actor=None):
        """扫描逾期未履行的承诺并升级；每条承诺只升级一次。"""
        self._require_staff(actor)
        now = (payload or {}).get("now") if payload else None
        now = now or self.clock()
        escalated = []
        for commitment in self.commitments.values():
            if commitment["status"] != COMMITMENT_OPEN:
                continue
            if commitment["deadline"] > now or commitment.get("escalation_notification_id"):
                continue
            notification = self._notify(
                ESCALATION_OVERDUE,
                commitment["responsible_subject"],
                commitment["cycle_seq"],
                commitment["commitment_id"],
                now,
                extra={"commitment_ids": [commitment["commitment_id"]]},
            )
            commitment["escalation_notification_id"] = notification["notification_id"]
            commitment["escalated_at"] = now
            escalated.append({
                "commitment_id": commitment["commitment_id"],
                "deadline": commitment["deadline"],
                "notification_id": notification["notification_id"],
            })
        return {"checked_at": now, "escalated": escalated}

    def _cycle_notifications(self, key, cycle_seq):
        return [
            n for n in self.notifications.values()
            if _subject_key(n["responsible_subject"]) == key and n["cycle_seq"] == cycle_seq
        ]

    # ------------------------------------------------------------------ #
    # 查询与报告
    # ------------------------------------------------------------------ #

    def task_report(self, task_id, actor=None):
        task = self._get_task(task_id)
        build = self.builds[task["build_id"]]
        normalized = self._actor(actor)
        if normalized["role"] == ROLE_PARTY \
                and normalized["subject"] != (SUBJECT_APP, build["app_id"]):
            raise AccessDeniedError("责任方只能查看本应用相关的任务报告")
        device = self.devices[task["device_id"]]
        findings = [f for f in self.findings.values() if f["task_id"] == task_id]
        return {
            "task": {
                "task_id": task_id,
                "track": task["track"],
                "track_label": TRACK_LABELS[task["track"]],
                "accessibility": task["accessibility"],
                "status": task["status"],
                "created_at": task["created_at"],
                "regulation_version": task["regulation_version"],
                "script": task["script"],
            },
            "build": {k: build[k] for k in ("build_id", "app_id", "app_name", "developer",
                                            "version_code", "version_name",
                                            "components", "fingerprints")},
            "device": device,
            "event_count": len(task["event_ids"]),
            "findings": [self._finding_snapshot(f) for f in findings],
        }

    def build_report(self, build_id, actor=None):
        """同一构建跨设备、跨轨迹的汇总，三轨迹结果并存不合并。"""
        if build_id not in self.builds:
            raise NotFoundError(f"应用构建不存在：{build_id}")
        build = self.builds[build_id]
        # 责任方仅可查看归属于本应用的构建报告；广告主/SDK 走整改材料接口
        normalized = self._actor(actor)
        if normalized["role"] == ROLE_PARTY:
            if normalized["subject"] != (SUBJECT_APP, build["app_id"]):
                raise AccessDeniedError("责任方只能查看本应用的构建报告")
        tasks = [t for t in self.tasks.values() if t["build_id"] == build_id]
        tracks = []
        for t in sorted(tasks, key=lambda x: (x["device_id"], x["track"])):
            report = self.task_report(t["task_id"])
            tracks.append({
                "device_id": t["device_id"],
                "track": t["track"],
                "track_label": TRACK_LABELS[t["track"]],
                "status": t["status"],
                "confirmed": len([f for f in report["findings"] if f["status"] == FINDING_CONFIRMED]),
                "suspected": len([f for f in report["findings"] if f["status"] == FINDING_SUSPECTED]),
                "dismissed": len([f for f in report["findings"] if f["status"] == FINDING_DISMISSED]),
                "findings": report["findings"],
            })
        return {"build": build, "tracks": tracks}

    def subject_view(self, subject_type, subject_id, actor=None):
        """承办人员视图：承诺、整改期限、各周期、复发关联与通知回执。"""
        if subject_type not in SUBJECT_TYPES:
            raise DomainError(f"未知责任主体类型：{subject_type}")
        key = (subject_type, subject_id)
        self._require_subject_access(actor, key)
        case = self.cases.get(key)
        if case is None:
            raise NotFoundError("该责任主体尚无案件")
        cycles = []
        now = self.clock()
        for cycle in case["cycles"]:
            notices = [self.notices[nid] for nid in cycle["notice_ids"]]
            cycles.append({
                "seq": cycle["seq"],
                "status": cycle["status"],
                "opened_at": cycle["opened_at"],
                "rectified_at": cycle["rectified_at"],
                "rectification_deadline": min((n["rectification_deadline"] for n in notices), default=None),
                "notices": [{
                    "notice_id": n["notice_id"],
                    "issued_at": n["issued_at"],
                    "rectification_deadline": n["rectification_deadline"],
                    "regulation_versions": n["regulation_versions"],
                    "finding_count": len(n["findings"]),
                } for n in notices],
                "commitments": [self._commitment_view(self.commitments[cid])
                               for cid in cycle["commitment_ids"]],
                "retests": cycle["retests"],
                "relapses": [self._relapse_view(r) for r in self.relapses.values()
                             if _subject_key(r["responsible_subject"]) == key
                             and r["cycle_seq"] == cycle["seq"]],
                "notifications": self._cycle_notifications(key, cycle["seq"]),
                "problem_period": self._problem_period(cycle),
                "findings": [self._finding_snapshot(self.findings[fid]) for fid in cycle["finding_ids"]],
            })
        open_commitments = [
            self._commitment_view(c) for c in self.commitments.values()
            if _subject_key(c["responsible_subject"]) == key and c["status"] == COMMITMENT_OPEN
        ]
        # 幂等键与自然键会指向同一待复测对象，按自然键去重
        pending = []
        seen_pending = set()
        for p in self.pending_retests.values():
            marker = p.get("natural_key") or id(p)
            if _subject_key(p["responsible_subject"]) != key or marker in seen_pending:
                continue
            seen_pending.add(marker)
            pending.append(p)
        return {
            "case_id": case["case_id"],
            "responsible_subject": _display_subject(case["responsible_subject"]),
            "status": case["status"],
            "relapse_count": case["relapse_count"],
            "current_cycle_seq": case["cycles"][-1]["seq"],
            "open_commitments": open_commitments,
            "overdue_commitments": [c for c in open_commitments if c["deadline"] <= now],
            "pending_retests": pending,
            "cycles": cycles,
        }

    def party_materials(self, subject_type, subject_id, actor=None):
        """责任方取得自己的整改材料：告知快照、承诺、复测结论与通知回执。"""
        if subject_type not in SUBJECT_TYPES:
            raise DomainError(f"未知责任主体类型：{subject_type}")
        key = (subject_type, subject_id)
        self._require_subject_access(actor, key)
        case = self.cases.get(key)
        if case is None:
            raise NotFoundError("该责任主体尚无案件")
        cycles_out = []
        for cycle in case["cycles"]:
            cycles_out.append({
                "seq": cycle["seq"],
                "status": cycle["status"],
                "notices": [self.notices[nid] for nid in cycle["notice_ids"]],
                "commitments": [self._commitment_view(self.commitments[cid])
                               for cid in cycle["commitment_ids"]],
                "retests": [{
                    "retest_id": r["retest_id"],
                    "task_id": r["task_id"],
                    "at": r["at"],
                    "result": r["result"],
                    "by": r["by"],
                    "note": r["note"],
                    "commitment_results": r["commitment_results"],
                    "baseline": r["baseline"],
                } for r in cycle["retests"]],
                "notifications": self._cycle_notifications(key, cycle["seq"]),
            })
        return {
            "responsible_subject": _display_subject(case["responsible_subject"]),
            "cycles": cycles_out,
        }

    def oversight_build(self, build_id, actor=None):
        """监管人员从任一当前构建看到：未履行承诺、复发依据与通知回执。"""
        self._require_staff(actor)
        if build_id not in self.builds:
            raise NotFoundError(f"应用构建不存在：{build_id}")
        build = self.builds[build_id]
        task_ids = {t["task_id"] for t in self.tasks.values() if t["build_id"] == build_id}

        finding_rows = []
        for finding in self.findings.values():
            if finding["task_id"] not in task_ids:
                continue
            linked_commitments = [
                self._commitment_view(c) for c in self.commitments.values()
                if c["finding_id"] == finding["finding_id"]
            ]
            relapse = None
            if finding.get("relapse_id"):
                relapse = self._relapse_view(self.relapses[finding["relapse_id"]])
            key = _subject_key(finding["responsible_subject"])
            finding_rows.append({
                "finding_id": finding["finding_id"],
                "rule_id": finding["rule_id"],
                "status": finding["status"],
                "responsible_subject": finding["responsible_subject"],
                "observed_at": finding["observed_at"],
                "case_id": finding.get("case_id"),
                "cycle_seq": finding.get("case_cycle_seq"),
                "commitments": linked_commitments,
                "relapse": relapse,
                "notifications": [
                    n for n in self.notifications.values()
                    if _subject_key(n["responsible_subject"]) == key
                ],
            })

        now = self.clock()
        open_commitments = [
            self._commitment_view(c) for c in self.commitments.values()
            if c["status"] == COMMITMENT_OPEN and (
                c["target_build_id"] == build_id
                or (self.findings.get(c["finding_id"]) or {}).get("build_id") == build_id
            )
        ]
        return {
            "build": {
                "build_id": build["build_id"],
                "app_id": build["app_id"],
                "app_name": build["app_name"],
                "developer": build["developer"],
                "version_code": build["version_code"],
                "version_name": build["version_name"],
                "components": build.get("components", []),
                "fingerprints": build.get("fingerprints", []),
            },
            "tasks": [{
                "task_id": t["task_id"],
                "device_id": t["device_id"],
                "track": t["track"],
                "status": t["status"],
            } for t in self.tasks.values() if t["build_id"] == build_id],
            "findings": finding_rows,
            "open_commitments": open_commitments,
            "overdue_commitments": [c for c in open_commitments if c["deadline"] <= now],
            "pending_relapse_decisions": [
                self._relapse_view(r) for r in self.relapses.values()
                if r["status"] == RELAPSE_PROPOSED and r["new_build_id"] == build_id
            ],
        }

    def _commitment_view(self, commitment):
        return {
            "commitment_id": commitment["commitment_id"],
            "case_id": commitment["case_id"],
            "cycle_seq": commitment["cycle_seq"],
            "responsible_subject": commitment["responsible_subject"],
            "finding_id": commitment["finding_id"],
            "rule_id": commitment["rule_id"],
            "rule_title": commitment["rule_title"],
            "target_build_id": commitment["target_build_id"],
            "target_tracks": commitment["target_tracks"],
            "deadline": commitment["deadline"],
            "promised_at": commitment["promised_at"],
            "promised_by": commitment.get("promised_by", ""),
            "status": commitment["status"],
            "source_notice_id": commitment.get("source_notice_id"),
            "fulfillment": commitment.get("fulfillment"),
            "escalation_notification_id": commitment.get("escalation_notification_id"),
            "escalated_at": commitment.get("escalated_at"),
        }

    def _relapse_view(self, relapse):
        return {
            "relapse_id": relapse["relapse_id"],
            "case_id": relapse["case_id"],
            "cycle_seq": relapse["cycle_seq"],
            "responsible_subject": relapse["responsible_subject"],
            "new_finding_id": relapse["new_finding_id"],
            "new_build_id": relapse["new_build_id"],
            "new_rule_id": relapse["new_rule_id"],
            "bases": relapse["bases"],
            "status": relapse["status"],
            "proposed_by": relapse.get("proposed_by"),
            "proposed_at": relapse.get("proposed_at"),
            "decided_by": relapse.get("decided_by"),
            "decided_at": relapse.get("decided_at"),
            "decision_comment": relapse.get("decision_comment"),
        }

    def _problem_period(self, cycle):
        """问题时段：周期内发现的实际观测时间范围（复测通过后仍保留）。"""
        findings = [self.findings[fid] for fid in cycle["finding_ids"]]
        if not findings:
            return None
        return {
            "first_observed_at": min(f["observed_at"] for f in findings),
            "last_observed_at": max(f["observed_at"] for f in findings),
        }

    def _finding_snapshot(self, finding):
        events = []
        for eid in finding["evidence_event_ids"]:
            e = self.events.get((finding["task_id"], eid))
            if e is None:
                continue
            events.append({"event_id": e["event_id"], "type": e["type"],
                           "occurred_at": e["occurred_at"], "late": e["late"],
                           "payload": e["payload"]})
        return {
            "finding_id": finding["finding_id"],
            "task_id": finding["task_id"],
            "build_id": finding.get("build_id"),
            "rule_id": finding["rule_id"],
            "rule_title": finding["rule_title"],
            "status": finding["status"],
            "responsible_subject": finding["responsible_subject"],
            "responsibility_chain": finding["responsibility_chain"],
            "ad": finding["ad"],
            "detail": finding["detail"],
            "observed_at": finding["observed_at"],
            "regulation_version": finding["regulation_version"],
            "reviewed_by": finding["reviewed_by"],
            "review_comment": finding["review_comment"],
            "behavior_fingerprints": finding.get("behavior_fingerprints", []),
            "relapse_id": finding.get("relapse_id"),
            "evidence": events,
        }

    # ------------------------------------------------------------------ #
    # 持久化快照：键含元组的仓储与自增序列
    # ------------------------------------------------------------------ #

    _TUPLE_DICTS = ("scripts", "events", "cases")
    _PLAIN_DICTS = (
        "regulations", "devices", "builds", "components", "tasks", "findings",
        "notices", "commitments", "relapses", "notifications",
        "pending_retests", "retest_dedup",
    )

    def to_snapshot(self):
        data = {"next_seq": next(self._seq)}
        data["regulation_order"] = self.regulation_order
        for name in self._PLAIN_DICTS:
            data[name] = getattr(self, name)
        for name in self._TUPLE_DICTS:
            mapping = getattr(self, name)
            data[name] = [{"key": list(key), "value": value} for key, value in mapping.items()]
        return data

    @classmethod
    def from_snapshot(cls, data, clock=time):
        lab = cls(clock=clock)
        lab.regulation_order = data.get("regulation_order", [])
        for name in cls._PLAIN_DICTS:
            setattr(lab, name, data.get(name, {}))
        for name in cls._TUPLE_DICTS:
            setattr(lab, name, {tuple(item["key"]): item["value"] for item in data.get(name, [])})
        lab._seq = count(data.get("next_seq", 1))
        # 重启后接续待复测事项
        lab._process_due_retests()
        return lab
