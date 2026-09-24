"""移动广告合规实验室的领域模型与规则引擎。

设计要点（与专项整治业务约定一一对应）：

* 证据只增不改：设备、构建、事件一经登记只能追加，新版构建与旧版并存，
  复核员不能修改或删除原始事件，只能对规则发现作确认/驳回。
* 采集幂等：上报事件带客户端事件号，重传去重；迟到事件允许补录，重新判定
  只会追加新发现，不会抹掉已有结论。
* 版本固化：任务创建时固化当时生效的规范版本与测试脚本版本，脚本升级只影响
  之后创建的任务。
* 规则与复核分离：自动规则只能产生「涉嫌」发现，复核员确认后才能生成告知材料。
* 问题到整改：每条已确认发现挂到对应责任方的整改项上，整改项承载整改承诺
  （目标构建、适用人群轨道、期限）与复测；单项关闭不关闭整个周期，全部整改项
  关闭后周期才关闭；旧证据与已发通知始终保留。
* 复测固化：复测记录固定当时的任务、脚本、规范（规则）、设备与构建摘要，
  同一复测任务重复/离线/并发提交只产生一次记录与一次状态推进。
* 复发提议与确认分离：新版本复用旧组件或命中行为指纹时，系统只提出复发关联
  （proposed），是否构成回潮由具备权限的审核人员确认（confirmed）或驳回。
* 逾期升级：承诺到期未关闭的整改项只向其责任方发送一次升级通知，留下回执；
  重复扫描不重复处罚、不重复推进。
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

# 整改项状态
ITEM_PENDING = "pending"          # 已确认待承诺/待整改
ITEM_COMMITTED = "committed"      # 已作出整改承诺
ITEM_CLOSED = "closed"            # 复测通过，单项关闭
ITEM_STATUSES = (ITEM_PENDING, ITEM_COMMITTED, ITEM_CLOSED)

# 复发关联状态：系统只能提议，确认权属于具备权限的审核人员
RELAPSE_PROPOSED = "proposed"
RELAPSE_CONFIRMED = "confirmed"
RELAPSE_DISMISSED = "dismissed"
RELAPSE_STATUSES = (RELAPSE_PROPOSED, RELAPSE_CONFIRMED, RELAPSE_DISMISSED)
RELAPSE_BASIS_COMPONENT = "component_reuse"
RELAPSE_BASIS_FINGERPRINT = "behavior_fingerprint"
RELAPSE_BASES = (RELAPSE_BASIS_COMPONENT, RELAPSE_BASIS_FINGERPRINT)

# 升级级别
ESCALATION_OVERDUE = "overdue"

# 角色：监管承办人/复核员可看全量并握有确认权；责任方只能取自己的材料
ROLE_REGULATOR = "regulator"          # 监管人员
ROLE_REVIEWER = "reviewer"            # 合规复核员（可确认发现与复发关联）
ROLE_RESPONSIBLE = "responsible"      # 责任方（应用运营者/广告主/SDK）
ROLES = (ROLE_REGULATOR, ROLE_REVIEWER, ROLE_RESPONSIBLE)
ROLE_LABELS = {
    ROLE_REGULATOR: "监管人员",
    ROLE_REVIEWER: "合规复核员",
    ROLE_RESPONSIBLE: "责任方",
}
# 握有复发确认权的角色
REVIEW_ROLES = frozenset({ROLE_REGULATOR, ROLE_REVIEWER})

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


class PermissionError_(PermissionError):
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
    components: dict = field(default_factory=dict)        # (kind, component_id, version) -> component
    tasks: dict = field(default_factory=dict)
    events: dict = field(default_factory=dict)            # (task_id, event_id) -> event（全局幂等）
    findings: dict = field(default_factory=dict)
    cases: dict = field(default_factory=dict)             # (type,id) -> case
    notices: dict = field(default_factory=dict)
    notifications: dict = field(default_factory=dict)     # 通知/回执（只增不改）
    actors: dict = field(default_factory=dict)            # actor_id -> actor（访问控制）
    items: dict = field(default_factory=dict)             # item_id -> 整改项
    relapses: dict = field(default_factory=dict)          # relapse_id -> 复发关联

    def _new_id(self, prefix):
        return f"{prefix}-{next(self._seq):04d}"

    # ------------------------------------------------------------------ #
    # 访问控制：角色与责任方绑定
    # ------------------------------------------------------------------ #

    def register_actor(self, payload):
        """登记一个调用身份：监管/复核角色，或绑定到责任主体的责任方。"""
        actor_id = _require(payload, "actor_id", "调用身份")
        if actor_id in self.actors:
            raise DomainError(f"调用身份已存在：{actor_id}")
        role = _require(payload, "role", "调用身份")
        if role not in ROLES:
            raise DomainError(f"未知角色：{role}，允许值：{', '.join(ROLES)}")
        binds = []
        for raw in payload.get("subjects", []) or []:
            stype = _require(raw, "type", "责任方绑定")
            sid = _require(raw, "id", "责任方绑定")
            if stype not in SUBJECT_TYPES:
                raise DomainError(f"未知责任主体类型：{stype}")
            binds.append({"type": stype, "id": sid,
                          "name": raw.get("name", "")})
        if role == ROLE_RESPONSIBLE and not binds:
            raise DomainError("责任方身份必须绑定至少一个责任主体")
        actor = {
            "actor_id": actor_id,
            "name": payload.get("name", actor_id),
            "role": role,
            "subjects": binds,
            "registered_at": self.clock(),
        }
        self.actors[actor_id] = actor
        return actor

    def _get_actor(self, actor_id):
        if not actor_id:
            raise DomainError("该操作需要提供 actor_id 以校验权限")
        actor = self.actors.get(actor_id)
        if actor is None:
            raise NotFoundError(f"调用身份不存在：{actor_id}")
        return actor

    def _require_role(self, actor_id, roles):
        actor = self._get_actor(actor_id)
        if actor["role"] not in roles:
            raise PermissionError_(
                f"角色 {ROLE_LABELS.get(actor['role'], actor['role'])}无权执行该操作")
        return actor

    def _bound(self, actor, subject_type, subject_id):
        return any(s["type"] == subject_type and s["id"] == subject_id
                   for s in actor["subjects"])

    def _can_access_subject(self, actor, subject_type, subject_id):
        if actor["role"] in REVIEW_ROLES:
            return True
        return self._bound(actor, subject_type, subject_id)

    # ------------------------------------------------------------------ #
    # 基础档案：规范版本、脚本版本、设备、第三方组件、应用构建
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

    def register_component(self, payload):
        """登记第三方组件/素材的具体版本，供构建引用与复发依据（组件复用）。"""
        kind = _require(payload, "kind", "组件")  # sdk | creative | library ...
        component_id = _require(payload, "component_id", "组件")
        version = _require(payload, "version", "组件")
        key = (kind, component_id, version)
        if key in self.components:
            raise DomainError(f"组件版本已存在：{kind}/{component_id}@{version}")
        component = {
            "kind": kind,
            "component_id": component_id,
            "version": version,
            "name": payload.get("name", component_id),
            "registered_at": self.clock(),
        }
        self.components[key] = component
        return component

    def register_build(self, payload):
        """登记应用构建。开发者提交新版得到新 build_id，旧构建证据原样保留。"""
        app_id = _require(payload, "app_id", "应用")
        version_code = _require(payload, "version_code", "应用构建")
        build_id = payload.get("build_id") or f"{app_id}:{version_code}"
        if build_id in self.builds:
            raise DomainError(f"应用构建已存在，禁止覆盖旧证据：{build_id}")
        components = []
        seen = set()
        for raw in payload.get("components", []) or []:
            kind = _require(raw, "kind", "构建组件")
            component_id = _require(raw, "component_id", "构建组件")
            version = _require(raw, "version", "构建组件")
            key = (kind, component_id, version)
            if key not in self.components:
                raise NotFoundError(f"组件未登记：{kind}/{component_id}@{version}")
            if key in seen:
                continue
            seen.add(key)
            comp = self.components[key]
            components.append({"kind": kind, "component_id": component_id,
                               "version": version, "name": comp["name"]})
        build = {
            "build_id": build_id,
            "app_id": app_id,
            "app_name": _require(payload, "app_name", "应用"),
            "developer": _require(payload, "developer", "应用"),
            "version_code": version_code,
            "version_name": payload.get("version_name", str(version_code)),
            "submitted_at": payload.get("submitted_at", self.clock()),
            # 固化到构建摘要：复测与复发比对都依据它
            "components": components,
            "material_fingerprints": list(payload.get("material_fingerprints", []) or []),
            "behavior_fingerprints": list(payload.get("behavior_fingerprints", []) or []),
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
        return self._evaluate(task)

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

        def add_finding(rule_id, ad_info, subject, chain, evidence, detail, occurred_at):
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
                "build_id": task["build_id"],
                "track": task["track"],
                # 行为指纹：规则号+广告位+责任方+素材创意（不含易变的广告实例号），
                # 用于跨构建复发比对；缺素材时按广告位归并。
                "behavior_fingerprint": "|".join([
                    rule_id, str(ad_info.get("placement", "")),
                    _subject_key(subject)[0], _subject_key(subject)[1],
                    str(ad_info.get("creative_id") or ""),
                ]),
                "created_at": self.clock(),
                "reviewed_by": None,
                "reviewed_at": None,
                "review_comment": None,
                "fingerprint": fingerprint,
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
                                    evidence, detail, jump["occurred_at"])
                elif trigger == "auto":
                    subject = sdk or advertiser or app_subject
                    detail = {"jump": self._jump_summary(jump, gestures),
                              "violations": ["跳转发生前无任何用户操作记录"]}
                    add_finding(RULE_AUTO_JUMP, ad_info, subject, jump_chain,
                                evidence, detail, jump["occurred_at"])

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
    # 复核与整改案件 / 整改项
    # ------------------------------------------------------------------ #

    def review_finding(self, finding_id, payload):
        finding = self._get_finding(finding_id)
        decision = _require(payload, "decision", "复核结论")
        if decision not in (FINDING_CONFIRMED, FINDING_DISMISSED):
            raise DomainError("复核结论只能是 confirmed 或 dismissed")
        reviewer = _require(payload, "reviewer", "复核结论")
        if payload.get("actor_id"):
            self._require_role(payload["actor_id"], REVIEW_ROLES)
        if finding["status"] in (FINDING_CONFIRMED, FINDING_DISMISSED):
            raise DomainError("发现已复核，结论不可更改（证据只增不改）")
        finding["status"] = decision
        finding["reviewed_by"] = reviewer
        finding["reviewed_at"] = self.clock()
        finding["review_comment"] = payload.get("comment", "")
        if decision == FINDING_CONFIRMED:
            self._attach_to_case(finding)
        return finding

    def _get_finding(self, finding_id):
        try:
            return self.findings[finding_id]
        except KeyError:
            raise NotFoundError(f"发现不存在：{finding_id}")

    def _get_case(self, subject_type, subject_id):
        if subject_type not in SUBJECT_TYPES:
            raise DomainError(f"未知责任主体类型：{subject_type}")
        case = self.cases.get((subject_type, subject_id))
        if case is None:
            raise NotFoundError("该责任主体尚无整改案件")
        return case

    def _open_cycle(self, case, at):
        """取当前开启周期；若上周期已关闭则开新周期（回潮次数由复发确认累计）。"""
        if not case["cycles"] or case["cycles"][-1]["status"] == CASE_RECTIFIED:
            case["cycles"].append({
                "seq": len(case["cycles"]) + 1,
                "status": CASE_OPEN,
                "opened_at": at,
                "rectified_at": None,
                "item_ids": [],
                "finding_ids": [],
                "notice_ids": [],
                "retests": [],
            })
            case["status"] = CASE_OPEN
        return case["cycles"][-1]

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
        cycle = self._open_cycle(case, self.clock())
        if finding["finding_id"] not in cycle["finding_ids"]:
            cycle["finding_ids"].append(finding["finding_id"])
        item = self._ensure_item(case, cycle, finding)
        finding["case_id"] = case["case_id"]
        finding["case_cycle_seq"] = cycle["seq"]
        finding["item_id"] = item["item_id"]
        return case

    def _ensure_item(self, case, cycle, finding):
        """一条已确认发现对应一个整改项（按发现幂等）。"""
        for iid in cycle["item_ids"]:
            item = self.items[iid]
            if item["finding_id"] == finding["finding_id"]:
                return item
        item = {
            "item_id": self._new_id("item"),
            "case_id": case["case_id"],
            "cycle_seq": cycle["seq"],
            "finding_id": finding["finding_id"],
            "rule_id": finding["rule_id"],
            "responsible_subject": _display_subject(case["responsible_subject"]),
            "status": ITEM_PENDING,
            "track": finding.get("track"),
            "origin_build_id": finding.get("build_id"),
            "commitment": None,
            "closed_at": None,
            "close_retest_id": None,
            "created_at": self.clock(),
        }
        self.items[item["item_id"]] = item
        cycle["item_ids"].append(item["item_id"])
        return item

    def get_item(self, item_id):
        try:
            return self.items[item_id]
        except KeyError:
            raise NotFoundError(f"整改项不存在：{item_id}")

    # ------------------------------------------------------------------ #
    # 整改承诺：责任方、目标构建、适用人群轨道、期限
    # ------------------------------------------------------------------ #

    def commit_rectification(self, item_id, payload):
        item = self.get_item(item_id)
        if item["status"] == ITEM_CLOSED:
            raise DomainError("整改项已关闭，不能再作承诺")
        target_build_id = _require(payload, "target_build_id", "整改承诺")
        if target_build_id not in self.builds:
            raise NotFoundError(f"目标构建不存在：{target_build_id}")
        target_track = payload.get("target_track", item.get("track") or TRACK_NORMAL)
        if target_track not in TRACKS:
            raise DomainError(f"未知适用人群轨道：{target_track}")
        days = payload.get("rectification_days")
        if days is None:
            finding = self.findings[item["finding_id"]]
            days = self.regulations[finding["regulation_version"]]["params"]["rectification_days"]
        if not isinstance(days, (int, float)) or days <= 0:
            raise DomainError("整改期限必须为正数（自然日）")
        now = self.clock()
        committed_at = payload.get("committed_at", now)
        committed_by = payload.get("committed_by", "")
        commitment = {
            "committed_by": committed_by,
            "committed_at": committed_at,
            "target_build_id": target_build_id,
            "target_build_summary": self._build_summary(self.builds[target_build_id]),
            "target_track": target_track,
            "track_label": TRACK_LABELS[target_track],
            "rectification_days": days,
            "deadline": committed_at + int(days * 86400),
            "note": payload.get("note", ""),
            # 承诺一经作出即固化；重复提交形成新的修订版本
            "revision": (item["commitment"]["revision"] + 1) if item["commitment"] else 1,
        }
        item["commitment"] = commitment
        item["status"] = ITEM_COMMITTED
        return item

    def generate_notice(self, subject_type, subject_id, payload):
        """对已确认发现生成告知材料；涉嫌发现一律不得进入材料。"""
        case = self._get_case(subject_type, subject_id)
        cycle = case["cycles"][-1]
        if cycle["status"] != CASE_OPEN:
            raise DomainError("当前整改周期已关闭，不能重复出具告知材料")
        if payload.get("actor_id"):
            actor = self._get_actor(payload["actor_id"])
            if not self._can_access_subject(actor, subject_type, subject_id):
                raise PermissionError_("无权对该责任主体出具告知材料")
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
        self._notify(
            subject_type, subject_id,
            kind="notice", ref_id=notice["notice_id"],
            title=f"整改告知材料 {notice['notice_id']}",
            channel=payload.get("channel", "in_system"),
            issued_by=reviewer,
        )
        return notice

    # ------------------------------------------------------------------ #
    # 复测：固定脚本/规则/设备/构建摘要；同一复测任务幂等
    # ------------------------------------------------------------------ #

    def _retest_snapshot(self, task):
        """把复测当时的脚本、规则（规范版本）、设备、构建摘要固定进记录。"""
        build = self.builds[task["build_id"]]
        return {
            "task_id": task["task_id"],
            "track": task["track"],
            "track_label": TRACK_LABELS[task["track"]],
            "script": task["script"],
            "regulation_version": task["regulation_version"],
            "device": {k: self.devices[task["device_id"]][k]
                       for k in ("device_id", "model", "os_version")},
            "build": self._build_summary(build),
            "completed_at": task["completed_at"],
        }

    @staticmethod
    def _build_summary(build):
        return {
            "build_id": build["build_id"],
            "app_id": build["app_id"],
            "app_name": build["app_name"],
            "version_code": build["version_code"],
            "version_name": build["version_name"],
            "components": build.get("components", []),
            "material_fingerprints": build.get("material_fingerprints", []),
            "behavior_fingerprints": build.get("behavior_fingerprints", []),
        }

    def record_retest(self, subject_type, subject_id, payload):
        """登记一次复测（按 task_id 幂等：重复上传/离线补传/并发复测只产生一次）。

        复测逐项判定：仅关闭本责任方在该周期内、且承诺目标构建为复测构建（或未承诺）
        的整改项；单项关闭不关闭整个周期，全部整改项关闭后周期才关闭。
        """
        case = self._get_case(subject_type, subject_id)
        cycle = case["cycles"][-1]
        task_id = _require(payload, "task_id", "复测")
        task = self._get_task(task_id)
        if task["status"] != "completed":
            raise DomainError("复测任务必须先完成采集与判定")

        # 幂等优先：同一复测任务（含已关闭周期、重复/离线/并发提交）只回首次结果
        for prior_cycle in case["cycles"]:
            for existing in prior_cycle["retests"]:
                if existing["task_id"] == task_id:
                    return existing

        if cycle["status"] != CASE_OPEN:
            raise DomainError("当前周期不在整改中")

        key = (subject_type, subject_id)
        new_findings = [
            f for f in self.findings.values()
            if f["task_id"] == task_id
            and _subject_key(f["responsible_subject"]) == key
        ]
        confirmed_new = {f["finding_id"] for f in new_findings
                         if f["status"] == FINDING_CONFIRMED}

        candidate_items = [
            self.items[iid] for iid in cycle["item_ids"]
            if self.items[iid]["status"] != ITEM_CLOSED
        ]
        # 若本构建是某些整改项承诺的目标构建，则它是「定向整改构建」，
        # 只判定承诺到本构建+本轨道的项，不捎带关闭其他未承诺项；
        # 否则视为一般复测构建，按轨道判定所有未承诺项。
        targeted = any(
            (it.get("commitment") or {}).get("target_build_id") == task["build_id"]
            for it in candidate_items
        )
        passed, failed = [], []
        for item in candidate_items:
            finding = self.findings[item["finding_id"]]
            # 复测任务自身刚确认出的新问题不在本次复测判定范围内，留待后续复测
            if finding["task_id"] == task_id:
                continue
            # 只要新构建上出现同一行为指纹的已确认问题，无论哪条轨道都判失败
            hit = self._matching_new_confirmation(finding, new_findings)
            if hit:
                failed.append(item)
                continue
            # 是否可据本次复测判通过：承诺项须命中目标构建+轨道；
            # 未承诺项在「定向整改构建」上不被捎带，且须轨道相符
            commitment = item.get("commitment")
            if commitment:
                in_scope = commitment["target_build_id"] == task["build_id"] \
                    and commitment["target_track"] == task["track"]
            else:
                in_scope = (not targeted) and item.get("track") == task["track"]
            if in_scope:
                passed.append(item)

        at = self.clock()
        retest = {
            "retest_id": self._new_id("retest"),
            "task_id": task_id,
            "cycle_seq": cycle["seq"],
            "at": at,
            "by": payload.get("by", ""),
            # 固化：复测事后不可因脚本/规则/构建更新而改变
            "snapshot": self._retest_snapshot(task),
            "suspected_count": len([f for f in new_findings if f["status"] == FINDING_SUSPECTED]),
            "confirmed_count": len(confirmed_new),
            "passed_item_ids": [i["item_id"] for i in passed],
            "failed_item_ids": [i["item_id"] for i in failed],
            "result": "failed" if failed else "passed",
            "note": ("复测通过仅关闭对应整改项，周期在全部整改项关闭后才关闭"
                     if not failed else "复测仍发现已确认问题，相关整改项保持开启"),
        }
        cycle["retests"].append(retest)

        for item in passed:
            item["status"] = ITEM_CLOSED
            item["closed_at"] = at
            item["close_retest_id"] = retest["retest_id"]

        still_open = [self.items[iid] for iid in cycle["item_ids"]
                      if self.items[iid]["status"] != ITEM_CLOSED]
        if not still_open:
            cycle["status"] = CASE_RECTIFIED
            cycle["rectified_at"] = at
            case["status"] = CASE_RECTIFIED
        return retest

    @staticmethod
    def _matching_new_confirmation(origin_finding, new_findings):
        """新构建上是否出现同一行为指纹的已确认问题。"""
        for f in new_findings:
            if f["status"] != FINDING_CONFIRMED:
                continue
            if f["behavior_fingerprint"] == origin_finding["behavior_fingerprint"]:
                return f
        return None

    # ------------------------------------------------------------------ #
    # 复发关联：系统提议（组件复用/行为指纹），审核人员确认
    # ------------------------------------------------------------------ #

    def propose_relapses_for_build(self, build_id):
        """为某构建的已确认发现提出复发关联（幂等，不自动计数）。

        依据二选一或兼有：
        * component_reuse：新构建复用了曾出问题的第三方组件版本；
        * behavior_fingerprint：新构建命中既往周期已确认发现的行为指纹。
        系统只产生 proposed，是否回潮由审核人员确认。
        """
        if build_id not in self.builds:
            raise NotFoundError(f"应用构建不存在：{build_id}")
        build = self.builds[build_id]
        new_confirmed = [
            f for f in self.findings.values()
            if f.get("build_id") == build_id and f["status"] == FINDING_CONFIRMED
        ]
        proposed = []
        for new_f in new_confirmed:
            key = _subject_key(new_f["responsible_subject"])
            case = self.cases.get(key)
            if case is None:
                continue
            for prior in self._prior_confirmed_findings(case, new_f):
                bases = self._relapse_bases(build, prior, new_f)
                if not bases:
                    continue
                if self._relapse_exists(prior["finding_id"], new_f["finding_id"]):
                    continue
                relapse = {
                    "relapse_id": self._new_id("relapse"),
                    "case_id": case["case_id"],
                    "subject": _display_subject(case["responsible_subject"]),
                    "prior_finding_id": prior["finding_id"],
                    "prior_cycle_seq": prior["case_cycle_seq"],
                    "prior_build_id": prior.get("build_id"),
                    "new_finding_id": new_f["finding_id"],
                    "new_build_id": build_id,
                    "new_cycle_seq": new_f["case_cycle_seq"],
                    "rule_id": new_f["rule_id"],
                    "bases": bases,
                    "status": RELAPSE_PROPOSED,
                    "proposed_at": self.clock(),
                    "decided_by": None,
                    "decided_at": None,
                    "comment": None,
                }
                self.relapses[relapse["relapse_id"]] = relapse
                proposed.append(relapse)
        return proposed

    def _prior_confirmed_findings(self, case, new_finding):
        """同一案件中早于新发现、且不在同一周期的已确认发现。"""
        prior = []
        for f in self.findings.values():
            if f["status"] != FINDING_CONFIRMED:
                continue
            if _subject_key(f["responsible_subject"]) != _subject_key(new_finding["responsible_subject"]):
                continue
            if f["finding_id"] == new_finding["finding_id"]:
                continue
            if f.get("case_cycle_seq") == new_finding.get("case_cycle_seq"):
                continue
            prior.append(f)
        return prior

    def _relapse_bases(self, new_build, prior_finding, new_finding):
        bases = []
        # 行为指纹
        if new_finding.get("behavior_fingerprint") \
                and new_finding["behavior_fingerprint"] == prior_finding.get("behavior_fingerprint"):
            bases.append({
                "basis": RELAPSE_BASIS_FINGERPRINT,
                "detail": f"新构建命中既往行为指纹 {new_finding['behavior_fingerprint']}",
                "behavior_fingerprint": new_finding["behavior_fingerprint"],
            })
        # 组件复用：新构建引用的组件版本，曾出现在既往问题构建中
        prior_build = self.builds.get(prior_finding.get("build_id") or "")
        if prior_build is not None:
            prior_components = {
                (c["kind"], c["component_id"], c["version"]) for c in prior_build.get("components", [])
            }
            for comp in new_build.get("components", []):
                ckey = (comp["kind"], comp["component_id"], comp["version"])
                if ckey in prior_components:
                    bases.append({
                        "basis": RELAPSE_BASIS_COMPONENT,
                        "detail": f"新构建复用既往问题构建中的组件 {comp['kind']}/{comp['component_id']}@{comp['version']}",
                        "component": {"kind": comp["kind"], "component_id": comp["component_id"],
                                      "version": comp["version"]},
                    })
        return bases

    def _relapse_exists(self, prior_finding_id, new_finding_id):
        return any(
            r["prior_finding_id"] == prior_finding_id
            and r["new_finding_id"] == new_finding_id
            for r in self.relapses.values()
        )

    def decide_relapse(self, relapse_id, payload):
        """确认/驳回一条复发关联。确认权只属于监管或复核角色。"""
        try:
            relapse = self.relapses[relapse_id]
        except KeyError:
            raise NotFoundError(f"复发关联不存在：{relapse_id}")
        actor = self._require_role(_require(payload, "actor_id", "复发确认"), REVIEW_ROLES)
        decision = _require(payload, "decision", "复发确认")
        if decision not in (RELAPSE_CONFIRMED, RELAPSE_DISMISSED):
            raise DomainError("复发结论只能是 confirmed 或 dismissed")
        if relapse["status"] != RELAPSE_PROPOSED:
            raise DomainError("复发关联已作出结论，不可更改")
        relapse["status"] = decision
        relapse["decided_by"] = actor["actor_id"]
        relapse["decided_at"] = self.clock()
        relapse["comment"] = payload.get("comment", "")
        if decision == RELAPSE_CONFIRMED:
            case = next(c for c in self.cases.values() if c["case_id"] == relapse["case_id"])
            # 确认权在人：仅在确认时累计一次回潮；重复确认已被上面的状态校验挡住
            case["relapse_count"] += 1
            self._notify(
                case["responsible_subject"]["type"], case["responsible_subject"]["id"],
                kind="relapse_confirmed", ref_id=relapse["relapse_id"],
                title=f"复发关联 {relapse['relapse_id']} 已确认",
                channel=payload.get("channel", "in_system"),
                issued_by=actor["actor_id"],
            )
        return relapse

    # ------------------------------------------------------------------ #
    # 逾期升级：只触达对应责任方，且每个整改项只升级一次
    # ------------------------------------------------------------------ #

    def _notify(self, subject_type, subject_id, kind, ref_id, title, channel, issued_by):
        """登记一条通知并产生回执（只增不改，永远保留）。"""
        notification = {
            "notification_id": self._new_id("note"),
            "subject": {"type": subject_type, "id": subject_id},
            "kind": kind,                 # notice | escalation | relapse_confirmed
            "ref_id": ref_id,
            "title": title,
            "channel": channel,
            "issued_by": issued_by,
            "sent_at": self.clock(),
            "receipt": {"status": "delivered", "delivered_at": self.clock()},
        }
        self.notifications[notification["notification_id"]] = notification
        return notification

    def escalate_overdue(self, payload=None):
        """扫描当前周期内逾期未关闭的整改项，逐项升级一次。

        只向该整改项的责任方发通知，不连带其他责任方；重复扫描/重启后重跑幂等。
        """
        payload = payload or {}
        now = self.clock()
        escalated = []
        for case in self.cases.values():
            if not case["cycles"]:
                continue
            cycle = case["cycles"][-1]
            if cycle["status"] != CASE_OPEN:
                continue
            for iid in cycle["item_ids"]:
                item = self.items[iid]
                commitment = item.get("commitment")
                if item["status"] == ITEM_CLOSED or commitment is None:
                    continue
                if commitment["deadline"] > now:
                    continue
                if self._escalation_exists(item["item_id"]):
                    continue  # 已升级过，不重复处罚/推进
                subject = case["responsible_subject"]
                notification = self._notify(
                    subject["type"], subject["id"],
                    kind="escalation", ref_id=item["item_id"],
                    title=f"整改项 {item['item_id']} 已逾期，升级处理",
                    channel=payload.get("channel", "in_system"),
                    issued_by=payload.get("issued_by", "system"),
                )
                record = {
                    "escalation_id": self._new_id("escalation"),
                    "item_id": item["item_id"],
                    "case_id": case["case_id"],
                    "subject": _display_subject(subject),
                    "deadline": commitment["deadline"],
                    "escalated_at": now,
                    "level": ESCALATION_OVERDUE,
                    "notification_id": notification["notification_id"],
                }
                item.setdefault("escalations", []).append(record)
                escalated.append(record)
        return {"at": now, "escalated": escalated,
                "escalated_count": len(escalated)}

    def _escalation_exists(self, item_id):
        item = self.items[item_id]
        return bool(item.get("escalations"))

    # ------------------------------------------------------------------ #
    # 查询与报告
    # ------------------------------------------------------------------ #

    def task_report(self, task_id):
        task = self._get_task(task_id)
        build = self.builds[task["build_id"]]
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
                                            "version_code", "version_name")},
            "device": device,
            "event_count": len(task["event_ids"]),
            "findings": [self._finding_snapshot(f) for f in findings],
        }

    def build_report(self, build_id):
        """同一构建跨设备、跨轨迹的汇总，三轨迹结果并存不合并。"""
        if build_id not in self.builds:
            raise NotFoundError(f"应用构建不存在：{build_id}")
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
        return {"build": self.builds[build_id], "tracks": tracks}

    def oversight_build_view(self, build_id, actor_id):
        """监管人员从任一当前构建看到：未履行承诺、复发依据、通知回执。"""
        actor = self._require_role(actor_id, REVIEW_ROLES)
        if build_id not in self.builds:
            raise NotFoundError(f"应用构建不存在：{build_id}")

        open_items, unfulfilled, related_notice_ids = [], [], set()
        for case in self.cases.values():
            for cycle in case["cycles"]:
                for iid in cycle["item_ids"]:
                    item = self.items[iid]
                    commitment = item.get("commitment")
                    if commitment and commitment["target_build_id"] == build_id \
                            and item["status"] != ITEM_CLOSED:
                        open_items.append(item)
                        now = self.clock()
                        unfulfilled.append({
                            "item_id": item["item_id"],
                            "rule_id": item["rule_id"],
                            "responsible_subject": _display_subject(item["responsible_subject"]),
                            "track": item["track"],
                            "deadline": commitment["deadline"],
                            "overdue": commitment["deadline"] <= now,
                            "target_build_id": build_id,
                        })
                        related_notice_ids.update(cycle["notice_ids"])

        relapse_links = [
            r for r in self.relapses.values()
            if r["new_build_id"] == build_id or r["prior_build_id"] == build_id
        ]
        ref_ids = {r["relapse_id"] for r in relapse_links}
        ref_ids |= {u["item_id"] for u in unfulfilled}
        ref_ids |= related_notice_ids
        receipts = [
            self._notification_view(n) for n in self.notifications.values()
            if n["ref_id"] in ref_ids
        ]
        return {
            "build": self._build_summary(self.builds[build_id]),
            "viewed_by": {"actor_id": actor["actor_id"], "role": actor["role"]},
            "unfulfilled_commitments": unfulfilled,
            "open_item_count": len(open_items),
            "relapse_links": [self._relapse_view(r) for r in relapse_links],
            "notification_receipts": receipts,
        }

    def subject_view(self, subject_type, subject_id, actor_id=None):
        """承办人员视图：整改期限、各周期、整改项、复测与回潮记录。"""
        if subject_type not in SUBJECT_TYPES:
            raise DomainError(f"未知责任主体类型：{subject_type}")
        if actor_id:
            actor = self._get_actor(actor_id)
            if not self._can_access_subject(actor, subject_type, subject_id):
                raise PermissionError_("无权查看该责任主体的整改材料")
        case = self.cases.get((subject_type, subject_id))
        if case is None:
            raise NotFoundError("该责任主体尚无案件")
        cycles = []
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
                "retests": cycle["retests"],
                "items": [self._item_view(self.items[iid]) for iid in cycle["item_ids"]],
                "problem_period": self._problem_period(cycle),
                "findings": [self._finding_snapshot(self.findings[fid]) for fid in cycle["finding_ids"]],
            })
        return {
            "case_id": case["case_id"],
            "responsible_subject": _display_subject(case["responsible_subject"]),
            "status": case["status"],
            "relapse_count": case["relapse_count"],
            "current_cycle_seq": case["cycles"][-1]["seq"],
            "cycles": cycles,
            "relapse_links": [
                self._relapse_view(r) for r in self.relapses.values()
                if _subject_key(r["subject"]) == (subject_type, subject_id)
            ],
            "notifications": [
                self._notification_view(n) for n in self.notifications.values()
                if _subject_key(n["subject"]) == (subject_type, subject_id)
            ],
        }

    def responsible_materials(self, actor_id):
        """责任方（如广告主）只能取得自己名下的整改材料。"""
        actor = self._get_actor(actor_id)
        if actor["role"] != ROLE_RESPONSIBLE:
            raise PermissionError_("该接口仅面向责任方身份")
        results = []
        for binding in actor["subjects"]:
            stype, sid = binding["type"], binding["id"]
            case = self.cases.get((stype, sid))
            if case is None:
                continue
            results.append(self.subject_view(stype, sid))
        return {"actor_id": actor_id, "name": actor["name"], "subjects": results}

    def _item_view(self, item):
        commitment = item.get("commitment")
        return {
            "item_id": item["item_id"],
            "finding_id": item["finding_id"],
            "rule_id": item["rule_id"],
            "status": item["status"],
            "track": item.get("track"),
            "origin_build_id": item.get("origin_build_id"),
            "commitment": commitment,
            "closed_at": item.get("closed_at"),
            "close_retest_id": item.get("close_retest_id"),
            "escalations": item.get("escalations", []),
        }

    def _relapse_view(self, relapse):
        return {
            "relapse_id": relapse["relapse_id"],
            "prior_finding_id": relapse["prior_finding_id"],
            "prior_cycle_seq": relapse["prior_cycle_seq"],
            "prior_build_id": relapse["prior_build_id"],
            "new_finding_id": relapse["new_finding_id"],
            "new_build_id": relapse["new_build_id"],
            "new_cycle_seq": relapse["new_cycle_seq"],
            "rule_id": relapse["rule_id"],
            "bases": relapse["bases"],
            "status": relapse["status"],
            "proposed_at": relapse["proposed_at"],
            "decided_by": relapse["decided_by"],
            "decided_at": relapse["decided_at"],
            "comment": relapse["comment"],
        }

    def _notification_view(self, notification):
        return {
            "notification_id": notification["notification_id"],
            "subject": _display_subject(notification["subject"]),
            "kind": notification["kind"],
            "ref_id": notification["ref_id"],
            "title": notification["title"],
            "channel": notification["channel"],
            "issued_by": notification["issued_by"],
            "sent_at": notification["sent_at"],
            "receipt": notification["receipt"],
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
            "rule_id": finding["rule_id"],
            "rule_title": finding["rule_title"],
            "status": finding["status"],
            "responsible_subject": finding["responsible_subject"],
            "responsibility_chain": finding["responsibility_chain"],
            "ad": finding["ad"],
            "detail": finding["detail"],
            "observed_at": finding["observed_at"],
            "regulation_version": finding["regulation_version"],
            "build_id": finding.get("build_id"),
            "track": finding.get("track"),
            "behavior_fingerprint": finding.get("behavior_fingerprint"),
            "reviewed_by": finding["reviewed_by"],
            "review_comment": finding["review_comment"],
            "evidence": events,
        }

    # ------------------------------------------------------------------ #
    # 持久化快照：键含元组的仓储与自增序列
    # ------------------------------------------------------------------ #

    _TUPLE_DICTS = ("scripts", "events", "cases", "components")
    _LIST_STORES = ("notifications", "actors", "items", "relapses")

    def to_snapshot(self):
        data = {"next_seq": next(self._seq)}
        for name in ("regulations", "regulation_order", "devices", "builds",
                     "tasks", "findings", "notices"):
            data[name] = getattr(self, name)
        for name in self._TUPLE_DICTS:
            mapping = getattr(self, name)
            data[name] = [{"key": list(key), "value": value} for key, value in mapping.items()]
        for name in self._LIST_STORES:
            store = getattr(self, name)
            data[name] = list(store.values()) if isinstance(store, dict) else list(store)
        return data

    @classmethod
    def from_snapshot(cls, data, clock=time):
        lab = cls(clock=clock)
        for name in ("regulations", "regulation_order", "devices", "builds",
                     "tasks", "findings", "notices"):
            setattr(lab, name, data.get(name, {} if name != "regulation_order" else []))
        for name in cls._TUPLE_DICTS:
            setattr(lab, name, {tuple(item["key"]): item["value"] for item in data.get(name, [])})
        for name in cls._LIST_STORES:
            rows = data.get(name, [])
            key_field = {
                "notifications": "notification_id",
                "actors": "actor_id",
                "items": "item_id",
                "relapses": "relapse_id",
            }[name]
            setattr(lab, name, {row[key_field]: row for row in rows})
        lab._seq = count(data.get("next_seq", 1))
        return lab
