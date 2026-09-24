# 移动广告合规实验室

归集多设备广告行为、无障碍操作证据、规则判定与整改复测，支撑对锁屏画报、开屏弹窗、
摇一摇广告的专项整治。

## 业务保证

- **三条操作轨迹并存**：正常用户（`normal`）、读屏用户（`screen_reader`）、老人模式
  （`elderly`）；同一构建在不同设备、不同轨迹上的证据分别留存，互不合并。
- **证据只增不改**：设备、构建、事件、规范版本只允许登记不允许覆盖。开发者提交新版
  得到新 `build_id`，旧构建证据原样保留；告知材料出具时对发现做快照，事后任何操作
  不改变材料内容。
- **采集幂等**：事件按 `(task_id, event_id)` 去重，重传统一进入 `duplicates`；任务
  完成后的迟到事件照常追加（标记 `late=true`）并触发追加判定，判定按指纹去重，不
  产生重复发现，也不回改既有结论。
- **版本固化**：创建任务时固化当时生效的规范版本与最新脚本版本；规范或脚本更新只
  影响之后创建的任务，旧任务永远按原版本判定。
- **规则与复核分离**：自动规则只能产生 `suspected`（涉嫌）发现；复核员 `confirmed`
  后才建立整改案件并逐条生成整改项（item），已确认且尚未告知的发现才能进入告知材料，
  整改期限取所依据各版规范中最短者。
- **问题到整改的完整连接**：每条问题连接责任方、整改承诺、目标构建、适用人群轨道
  （`target_track`）与期限；承诺挂在具体整改项上。
- **复测固化**：复测记录固定当时的脚本、规则（规范版本）、设备与构建摘要
  （`snapshot`），事后脚本/规则/构建更新不改变复测结论。
- **单项关闭不关周期**：复测逐项判定，只关闭命中目标构建与轨道的整改项；周期在全部
  整改项关闭后才关闭。周期内问题时段（首末观测时间）永久保留。
- **复发提议与确认分离**：新版本复用旧组件（`component_reuse`）或命中行为指纹
  （`behavior_fingerprint`）时，系统只提出 `proposed` 复发关联；是否构成回潮由
  具备权限的审核人员 `confirmed`/`dismissed`，确认时才累计 `relapse_count`。
- **逾期升级精准触达且只此一次**：承诺到期未关闭的整改项只向其责任方发送升级通知并
  留回执，不连带其他责任方；重复扫描/重启重跑幂等，不重复处罚或推进。
- **复测幂等与重启接续**：同一复测任务重复上传、离线补传、并发提交只产生一次记录与
  一次状态推进（按 `task_id` 去重，含跨周期回放）；重启后待复测事项仍可接续完成。
- **旧证据与通知只增保留**：复测通过不抹历史，已发送的告知/升级/复发确认通知及回执
  随快照永久保留。
- **角色与可见范围**：监管人员（`regulator`）与复核员（`reviewer`）可看全量，握有
  复核与复发确认权，并可从任一当前构建看到未履行承诺、复发依据与通知回执；责任方
  （`responsible`）只能通过 `/materials` 取得自己绑定主体名下的整改材料，越权返回
  `403`。
- **责任区分**：每条发现记录责任主体（`app` 应用运营者 / `advertiser` 广告主 /
  `sdk` 嵌入SDK）与完整责任链。摇一摇/自动跳转优先归 SDK，其次广告主；关闭路径
  归应用运营者。

## 自动规则（仅标涉嫌）

| 规则 | 触发条件（依据任务固化的规范版本） |
| --- | --- |
| `R-CLOSE-001` | 广告期间无关闭入口；入口出现晚于 `close_max_delay_seconds`（默认 3s）；可点区域小于 44dp（老人模式 56dp）；读屏轨迹下入口不可聚焦或无操作标签 |
| `R-SHAKE-001` | 摇一摇触发跳转时，峰值加速度、旋转角度或读数持续时间任一低于规范下限（默认 15 m/s²、35°、3s） |
| `R-JUMP-001` | `trigger=auto` 的跳转，且跳转前无任何用户操作事件 |

## 运行

```bash
python3 service.py --check          # 基础自检
python3 service.py --port 8000      # 启动服务
LAB_DATA_FILE=lab.json python3 service.py   # 证据快照落盘，重启恢复
npm test                            # 运行契约 + 领域 + HTTP 共 25 项测试
```

## 接口一览

所有请求/响应均为 UTF-8 JSON；领域错误返回 `400`，实体不存在返回 `404`。

| 方法与路径 | 说明 |
| --- | --- |
| `GET /health` | 健康检查 |
| `POST /admin/regulations` | 登记规范版本（`version`、`effective_at`、可选 `params`） |
| `POST /admin/scripts` | 登记测试脚本版本（`script_id`、`version`） |
| `POST /devices` | 登记设备（`device_id`、`model`、`os_version`） |
| `POST /components` | 登记第三方组件/素材版本（`kind`、`component_id`、`version`） |
| `POST /builds` | 登记应用构建（可带 `components`、`behavior_fingerprints`、`material_fingerprints`） |
| `POST /actors` | 登记调用身份：`regulator`/`reviewer`，或绑定责任主体的 `responsible` |
| `POST /tasks` | 创建采集任务（`build_id`、`device_id`、`track`），响应含固化版本 |
| `POST /tasks/{id}/events` | 幂等上报事件批次 `{"events": [...]}`，返回 `accepted/duplicates` |
| `POST /tasks/{id}/complete` | 完成采集并运行规则 |
| `GET /tasks/{id}` | 单任务报告：设备/系统/构建/无障碍设置/操作轨迹/发现与证据 |
| `GET /builds/{id}/report` | 同一构建跨设备、跨轨迹汇总 |
| `GET /builds/{id}/oversight?actor_id=` | 监管视角：未履行承诺、复发依据、通知回执（仅监管/复核） |
| `POST /builds/{id}/relapse-proposals` | 按组件复用/行为指纹提出复发关联（`proposed`） |
| `POST /relapses/{id}/decision` | 审核人员确认/驳回复发关联（`actor_id`+`decision`） |
| `POST /findings/{id}/review` | 复核：`{"decision": "confirmed|dismissed", "reviewer"}`，确认即生成整改项 |
| `POST /items/{id}/commitments` | 提交整改承诺：目标构建、适用轨道、期限（`rectification_days`） |
| `POST /subjects/{type}/{id}/notices` | 对已确认发现出具告知材料（期限、依据版本、证据快照、送达回执） |
| `POST /subjects/{type}/{id}/retests` | 登记复测 `{"task_id": ...}`，固化脚本/规则/设备/构建摘要，逐项关闭 |
| `POST /escalations/scan` | 扫描逾期承诺并升级（只触达对应责任方，逐项仅一次） |
| `GET /subjects/{type}/{id}?actor_id=` | 承办人员视图：各周期、整改项、承诺、复测、复发与通知回执 |
| `GET /materials?actor_id=` | 责任方取得自己名下全部整改材料（他人材料 403） |

### 访问控制

需要鉴权的接口用 `actor_id`（POST 在请求体、GET 在查询参数）表明身份。
`regulator`/`reviewer` 可访问全量并握有确认权；`responsible` 仅能访问其绑定主体，
越权返回 `403 {"error":"forbidden"}`。领域错误 `400`、实体不存在 `404`。

### 事件结构

```json
{
  "event_id": "端上唯一事件号（重传判重依据）",
  "seq": 1,
  "type": "ad_shown | close_affordance | sensor_reading | jump | network_response | gesture",
  "occurred_at": 1700000100,
  "payload": {
    "ad_id": "a1",
    "trigger": "auto | shake",
    "target_url": "https://...",
    "sdk": {"id": "shake-sdk-9", "name": "..."},
    "advertiser": {"id": "ad-brand-x"},
    "visible_after_seconds": 5,
    "touch_target_dp": 36,
    "screen_reader_actionable": false,
    "peak_acceleration": 8,
    "peak_rotation_deg": 10,
    "reading_seconds": 1
  }
}
```

## 模块

- `domain.py`：领域模型与规则引擎（纯 Python，无框架依赖），含快照序列化。
- `service.py`：HTTP 入口与持久化仓储（`Store`，线程安全、原子写盘）。
- `test_domain.py` / `test_api.py`：领域规则与 HTTP 全链路测试。
- `fixtures/domain.json`：领域名词与状态词表，供接口联调对齐语义。

## 运行与检查

```bash
python3 service.py --check
npm test
python3 -m compileall -q .
```
