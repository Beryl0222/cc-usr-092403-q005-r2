# 移动广告合规实验室

归集多设备广告行为、无障碍操作证据、规则判定与整改复测，支撑对锁屏画报、开屏弹窗、
摇一摇广告的专项整治。

## 业务保证

- **三条操作轨迹并存**：正常用户（`normal`）、读屏用户（`screen_reader`）、老人模式
  （`elderly`）；同一构建在不同设备、不同轨道上的证据分别留存，互不合并。
- **证据只增不改**：设备、构建、事件、规范版本只允许登记不允许覆盖。开发者提交新版
  得到新 `build_id`，旧构建证据原样保留；告知材料出具时对发现做快照，事后任何操作
  不改变材料内容；已发送通知与送达回执永久保留。
- **采集幂等**：事件按 `(task_id, event_id)` 去重，重传统一进入 `duplicates`；任务
  完成后的迟到事件照常追加（标记 `late=true`）并触发追加判定，判定按指纹去重，不
  产生重复发现，也不回改既有结论。
- **版本固化**：创建任务时固化当时生效的规范版本与最新脚本版本；规范或脚本更新只
  影响之后创建的任务，旧任务永远按原版本判定。复测记录再固定当时的脚本、规范、
  设备与构建摘要（含组件与行为指纹），事后基线变化不改变复测结论。
- **规则与复核分离**：自动规则只能产生 `suspected`（涉嫌）发现；复核员 `confirmed`
  后才建立整改案件，已确认且尚未告知的发现才能进入告知材料，整改期限取所依据各版
  规范中最短者。
- **承诺驱动整改**：每条已确认问题都连接责任方、整改承诺、目标构建、适用人群轨道
  与期限。复测按承诺逐项验证：单项履行只关闭该条承诺，不结束整个周期；周期内全部
  承诺履行完毕才关闭周期。复测出现无承诺覆盖的已确认问题或错用旧构建时，整体判
  失败，周期保持开启。
- **复测幂等与可接续**：复测以 `idempotency_key` 与（责任方+任务）自然键双重去重，
  重复上传、离线补传、并发复测只产生一次处罚或状态推进；复测任务未完成时进入待复测
  队列，任务完成或服务重启后自动接续，只执行一次。
- **复测不抹历史、回潮可累计**：复测通过只关闭当前整改周期，周期内问题时段
  （首末观测时间）永久保留；已整改周期之后再次确认问题即开启新周期，
  `relapse_count` 加一，承办人员可按责任主体看到历次周期、告知、承诺、复测
  （含失败记录）、复发关联与期限。
- **复发关联提议制**：新版本复用旧第三方组件、命中行为指纹或同规则问题重现时，
  系统只提出复发关联（`proposed`）并给出依据；确认（`confirmed`）或驳回
  （`rejected`）的权力只属于监管/复核人员，责任方无权确认。
- **逾期升级只触达责任方**：承诺到期未履行触发一次升级并留存送达回执，只送达该
  责任主体，不抄送其他主体；重复扫描不产生二次升级。
- **角色与材料隔离**：监管人员（`regulator`）与复核员（`reviewer`）可复核、出告知、
  审复发、查看任一构建的监管视图；责任方（`party`）只能取得本主体的整改材料
  （告知快照、承诺、复测结论与通知回执），广告主看不到他人材料。
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
LAB_DATA_FILE=lab.json python3 service.py   # 证据快照落盘，重启恢复并接续待复测
npm test                            # 运行契约 + 领域 + HTTP 共 35 项测试
```

## 接口一览

所有请求/响应均为 UTF-8 JSON；领域错误返回 `400`，实体不存在返回 `404`，越权
访问返回 `403`。

### 访问角色（请求头）

| 头 | 取值 |
| --- | --- |
| `X-Role` | `regulator` 监管人员（默认）、`reviewer` 复核员、`party` 责任方 |
| `X-Subject-Type` / `X-Subject-Id` | `X-Role: party` 时必填，声明责任主体；只能访问本主体材料 |
| `X-Actor-Id` | 操作人标识（仅记录，不参与授权） |

| 方法与路径 | 角色 | 说明 |
| --- | --- | --- |
| `GET /health` | 任意 | 健康检查 |
| `POST /admin/regulations` | 工作人员 | 登记规范版本（`version`、`effective_at`、可选 `params`） |
| `POST /admin/scripts` | 工作人员 | 登记测试脚本版本（`script_id`、`version`） |
| `POST /admin/overdue-checks` | 工作人员 | 扫描逾期未履行承诺并升级（每承诺只升一次），可带 `{"now": ...}` |
| `POST /admin/retests/process-due` | 工作人员 | 手动接续全部到期的待复测事项 |
| `POST /devices` | 工作人员 | 登记设备（`device_id`、`model`、`os_version`） |
| `POST /builds` | 工作人员 | 登记应用构建（可含复用 `components` 与行为 `fingerprints`） |
| `POST /tasks` | 工作人员 | 创建采集任务（`build_id`、`device_id`、`track`），响应含固化版本 |
| `POST /tasks/{id}/events` | 工作人员 | 幂等上报事件批次 `{"events": [...]}`，返回 `accepted/duplicates` |
| `POST /tasks/{id}/complete` | 工作人员 | 完成采集并运行规则；同时接续等待该任务的待复测 |
| `GET /tasks/{id}` | 工作人员 / 应用本人 | 单任务报告：设备/系统/构建/无障碍设置/操作轨迹/发现与证据 |
| `GET /builds/{id}/report` | 工作人员 / 应用本人 | 同一构建跨设备、跨轨迹汇总 |
| `GET /builds/{id}/oversight` | 工作人员 | 监管视图：未履行/逾期承诺、复发依据与待决、通知回执 |
| `POST /findings/{id}/review` | 工作人员 | 复核：`{"decision": "confirmed|dismissed", "reviewer"}`；确认时自动提议复发关联 |
| `POST /relapses/{id}/decision` | 工作人员 | 复发关联审核：`{"decision": "confirmed|rejected", "reviewer"}` |
| `POST /subjects/{type}/{id}/notices` | 工作人员 | 对已确认发现出具告知材料（期限、依据版本、证据快照），并产生送达回执 |
| `POST /subjects/{type}/{id}/commitments` | 工作人员 / 责任方本人 | 提交整改承诺：`finding_id`、`target_build_id`、`target_tracks`、可选 `deadline` |
| `POST /subjects/{type}/{id}/retests` | 工作人员 | 登记复测：`task_id`、可选 `idempotency_key`、可选 `commitment_ids`；返回固化基线与逐项结论；任务未完成返回 `pending` |
| `GET /subjects/{type}/{id}` | 工作人员 / 责任方本人 | 承办视图：各周期、承诺、期限、复测（含固化基线）、复发关联、回执、待复测 |
| `GET /subjects/{type}/{id}/materials` | 工作人员 / 责任方本人 | 责任方整改材料：告知快照、承诺、复测结论与通知回执 |

### 复测结果与幂等

* `result`：`pending`（任务未完成，已入待复测队列）/ `passed` / `failed`。
* `commitment_results[]`：每条承诺的逐项结论（`verified` / `still_present` /
  `wrong_build`），单项 `verified` 只关闭该承诺。
* `baseline`：复测固化的规范版本、脚本、设备、构建摘要（含组件与指纹）。
* 同一 `idempotency_key` 或同一（责任方，任务）重复请求，原样返回首次结果。

### 构建登记（复用组件与行为指纹）

```json
{
  "app_id": "com.example.news",
  "app_name": "某新闻",
  "developer": "某新闻运营有限公司",
  "version_code": 1003,
  "components": [
    {"component_id": "comp-ad-1", "kind": "sdk", "name": "某广告SDK", "version": "1.0"}
  ],
  "fingerprints": [
    {"type": "behavior", "value": "behavior:R-CLOSE-001:splash"}
  ]
}
```

新旧构建出现组件复用、指纹命中或同规则重现时，新确认的发现会附带 `relapse_id`
的复发**提议**；未经工作人员确认，该关联始终停留在 `proposed`。

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
- `service.py`：HTTP 入口、角色鉴权与持久化仓储（`Store`，线程安全、原子写盘）。
- `test_domain.py` / `test_api.py`：领域规则与 HTTP 全链路测试。
- `test_commitments.py` / `test_api_baseline.py`：承诺、复测固化与幂等、复发提议、
  逾期升级、角色隔离、重启接续的领域与 HTTP 测试。
- `fixtures/domain.json`：领域名词与状态词表，供接口联调对齐语义。

## 运行与检查

```bash
python3 service.py --check
npm test
python3 -m compileall -q .
```
