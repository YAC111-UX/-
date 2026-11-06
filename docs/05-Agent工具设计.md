# 知行职业学院排课选课助手 · Agent Function Calling 工具设计

> 版本：v1.0（2026-11-06）｜阶段 6｜依据：01-需求清单 + 02-架构设计
> 核心红线：**LLM 只解释不判定**——冲突判定 100% 由纯代码规则引擎完成；AI 只读，调课必须走草稿人工确认；学生选课数据透传不落库。

## 一、Agent 执行循环（阶段 7 编码，本阶段先定契约）

```
用户提问 → 意图判断（LLM Function Calling 自动选择）
  → 第 N 轮（N ≤ 5）：LLM 返回 tool_calls → 执行工具 → 结果作为 tool 消息回填 → 再次调用 LLM
  → 第 5 轮仍要调工具 → 强制收尾："请联系排课专员人工核实"
  → LLM 输出最终回答（流式 delta）→ 落库 + 审计
```

- 工具调用全程写 `agent_tool_logs`（round_no 1~5、入参出参摘要、耗时），审计可完整回放
- SSE 新增事件：`tool_call`（开始调用某工具）、`tool_result`（工具返回摘要），前端显示"正在查询先修关系…"类状态
- 降级约定：工具执行异常不中断会话，把错误摘要回填给 LLM，由它向用户解释并建议转人工

## 二、工具清单（OpenAI Function Calling 格式，共 6 个）

| # | 工具名 | 用途 | 判定方 | 写库 |
|---|---|---|---|---|
| 1 | `rag_search` | 制度文档混合检索 | RAG（已有） | 否 |
| 2 | `check_prerequisite` | 先修冲突校验 | **规则引擎** | 是（回放记录在草稿确认后落） |
| 3 | `check_room_conflict` | 场地/实训室冲突校验 | **规则引擎** | 同上 |
| 4 | `generate_adjust_draft` | 生成调课调整草稿 | LLM 起草 + 结构化校验 | drafts 表 pending_confirm |
| 5 | `query_handover` | 历史交接单据查询 | SQL 只读 | 否 |
| 6 | `notify_manual_fallback` | 人工兜底通知（4 类必转） | 规则触发 | handover_tasks 建单 |

### 1. rag_search 制度检索（包装阶段 3 已有能力）

```json
{ "name": "rag_search",
  "description": "检索教务制度/培养方案/场地规则条文。凡涉及'规定/条件/能不能/怎么办理'的提问必须先调本工具，禁止凭记忆回答。",
  "parameters": { "type": "object",
    "properties": { "query": { "type": "string", "description": "检索关键词或问题" } },
    "required": ["query"] } }
```
返回：`{"hits": [{"filename", "chunk", "score"}], "count": N}`（复用 rag.pipeline.search，top3）。

### 2. check_prerequisite 先修冲突校验（规则引擎）

```json
{ "name": "check_prerequisite",
  "description": "校验学生已修课程是否满足目标课程的先修要求（含新旧课程代码映射）。只做判定，不做任何选课操作。",
  "parameters": { "type": "object",
    "properties": {
      "student_id":  { "type": "string", "description": "学生伪 ID（脱敏）" },
      "course_code": { "type": "string", "description": "目标课程代码，新旧代码均可" }
    },
    "required": ["student_id", "course_code"] } }
```
返回：
```json
{ "course_code": "SW301", "course_name": "软件工程", "semester": "2026春",
  "data_source": "realtime", "snapshot_at": null,
  "passed": false,
  "missing": [ { "required_course": "SW201 数据结构", "reason": "未修" } ],
  "mapping_note": "旧代码 C202 已按官方映射表换算为 SW201" }
```
判定逻辑（纯代码，见 `app/rules/prerequisite.py`）：
1. 课程代码经 `course_mapping.py` 归一化（旧→新）
2. 从适配器取该生已修课程集合（**实时透传，不落库**）
3. 逐条比对目标课程先修集合 → `missing` 列表
4. 结果 + 输入摘要写 `conflict_replay_records`（business_no 生成规则见第五节）

### 3. check_room_conflict 场地冲突校验（规则引擎）

```json
{ "name": "check_room_conflict",
  "description": "校验指定时间段场地/实训室是否被占用（含维护占用）。只读校验。",
  "parameters": { "type": "object",
    "properties": {
      "room":     { "type": "string", "description": "场地/实训室名称或编号" },
      "week_day": { "type": "integer", "description": "星期几，1=周一" },
      "section":  { "type": "string", "description": "节次，如 '3-4节'" }
    },
    "required": ["room", "week_day", "section"] } }
```
返回：`{"room", "time_slot", "conflicts": [{"type": "course|maintenance", "detail", "source"}], "available": bool, "data_source", "snapshot_at"}`
判定逻辑（`app/rules/room_conflict.py`）：排课占用 + 维护占用两路查表取并集；维护占用但课表仍标可用 → 返回特殊标记 `needs_manual=true`（对应必转场景 1）。

### 4. generate_adjust_draft 调课草稿（唯一写操作，强制人工确认）

```json
{ "name": "generate_adjust_draft",
  "description": "根据冲突校验结果起草调课调整建议。仅生成草稿，不生效；须排课专员在管理后台确认后才写入正式记录。",
  "parameters": { "type": "object",
    "properties": {
      "business_no": { "type": "string", "description": "关联的冲突校验业务编号" },
      "adjustments": { "type": "array", "items": { "type": "object",
        "properties": {
          "target":    { "type": "string", "description": "调整对象，如课程/教室" },
          "field":     { "type": "string", "description": "调整字段，如 room/section/week_day" },
          "old_value": { "type": "string", "description": "原值（必填，草稿必须显示原记录）" },
          "new_value": { "type": "string", "description": "建议新值" } },
        "required": ["target", "field", "old_value", "new_value"] } },
      "reason": { "type": "string", "description": "调整理由" } },
    "required": ["business_no", "adjustments", "reason"] } }
```
- 落 `drafts` 表：status=pending_confirm，content_json 含原值+新值（满足审计"建议必须显示原记录"）
- **学生/辅导员会话里 LLM 调用本工具时直接拒绝**（返回 error："调课草稿仅可由排课专员会话发起"），权限由服务端按登录角色判定，不信任 LLM
- 确认流（阶段 8 页面）：`POST /api/admin/drafts/{id}/confirm`（仅排课专员）→ status=confirmed → 补全 conflict_replay_records.confirmed_by

### 5. query_handover 历史交接单据查询（只读）

```json
{ "name": "query_handover",
  "description": "按业务编号或关键词查询历史交接单据（状态/未解决事项/接手人）。只读。",
  "parameters": { "type": "object",
    "properties": {
      "business_no": { "type": "string", "description": "业务编号，可空" },
      "keyword":     { "type": "string", "description": "标题/未解决事项关键词，可空" },
      "status":      { "type": "string", "description": "pending/assigned/accepted/completed/escalated，可空" } } }
```
返回：`{"items": [{"business_no", "version", "status", "title", "unresolved_items", "assignee", "created_at"}], "count"}`，最多 20 条，模糊匹配 LIKE。

### 6. notify_manual_fallback 人工兜底通知（4 类必转场景）

```json
{ "name": "notify_manual_fallback",
  "description": "触发人工兜底：实训室维护占用但课表标可用 / 资料缺少依据 / 两个版本规则冲突 / 关键系统不可用。创建交接单据并通知。",
  "parameters": { "type": "object",
    "properties": {
      "trigger_type": { "type": "string", "enum": ["room_needs_manual", "missing_evidence", "rule_conflict", "system_down"], "description": "必转场景类型" },
      "reason":       { "type": "string", "description": "触发原因说明（用户可读）" },
      "business_no":  { "type": "string", "description": "关联业务编号，可空" } },
    "required": ["trigger_type", "reason"] } }
```
- 写 `handover_tasks`：title=场景类型+摘要，unresolved_items=reason，status=pending，creator=AI（session 关联），assignee=排课专员（房间类）/教务主任（系统类），escalate_at=+24h
- 返回：`{"business_no", "assignee_role", "escalate_at"}`，LLM 据此向用户答复"已转人工，单号 xxx"
- 满足需求清单第六节：均标注触发原因、接手人、确认时间（accepted_at）

## 三、规则引擎与 Mock 适配器（本阶段新增模块）

```
app/rules/
├── prerequisite.py    先修校验（纯函数，输入输出皆结构化，可单测）
├── room_conflict.py   场地校验（纯函数）
└── course_mapping.py  新旧课程代码官方映射表（内置字典，只读）

app/adapters/
├── academic_api.py    教务系统适配器：get_student_courses(伪ID) / get_course_prereq(课程)
├── room_api.py        场地适配器：get_room_schedule(场地) / get_maintenance(场地)
└── snapshot.py        降级封装：实时调用失败 → 读 data/snapshots/ 带时间戳快照，
                       返回 data_source="snapshot"，快照也没有 → 触发必转场景 4
```

- **试点期用本地 Mock 数据**：`data/mock/academic.json`（6 门课含先修关系与新旧代码）、`data/mock/rooms.json`（4 个场地含课表占用与维护记录）；适配器接口与真实 API 对齐，阶段 9 联调只换实现不改调用方
- 学生伪 ID：mock 数据预置 3 个学生（S001~S003）含已修课程集合；不存明文学号，满足数据最小化

## 四、业务编号（business_no）规则

`BUS + 日期 + 4 位当日序号`，如 `BUS20261106-0007`。冲突回放 / 草稿 / 交接单三类单据共用一套生成器（`app/core/biz_no.py`），审计据此串联"校验 → 草稿 → 人工确认 → 交接"全链路。

## 五、SSE 事件与前端扩展

| 事件 | 时机 | data |
|---|---|---|
| `tool_call` | Agent 决定调工具 | `{"tool": "check_prerequisite", "round": 1, "text": "正在校验先修要求…"}` |
| `tool_result` | 工具返回 | `{"tool": "check_prerequisite", "summary": "1 项冲突"}`（截断摘要） |

聊天页 index.html 增加工具状态条展示（复用现有 status-bar 样式），无需改交互骨架。

## 六、新增/沿用错误码

| 错误码 | 含义 | 中文提示 |
|---|---|---|
| 1010 | 工具参数不合法 | 工具调用参数不完整或格式有误 |
| 1011 | 无权发起该工具 | 当前角色无权使用此功能 |
| 2004 | 外部接口不可用且无快照 | 教务数据暂时无法获取，已转人工核实 |
（沿用：2003 知识库维护中 / 2001 2002 LLM 异常）

## 七、改动点清单（编码范围）

1. 新增 `app/rules/`（3 个纯函数模块）+ `data/mock/*.json` Mock 数据
2. 新增 `app/adapters/`（academic/room/snapshot，接口对齐真实 API）
3. 新增 `app/agent/tools.py`（6 个工具的 JSON Schema + 执行器注册表 `TOOL_REGISTRY`）
4. 新增 `app/core/biz_no.py` 业务编号生成器
5. 改造 `app/core/llm.py`：新增 `chat_with_tools()`（非流式轮次，支持 tools 参数与 tool_calls 解析），原流式函数保留做最终回答
6. 改造 `app/services/chat.py`：插入 Agent 循环（≤5 轮），工具事件经 SSE 推送，工具日志落 `agent_tool_logs`
7. `requirements.txt` 不新增依赖（requests 手写 Function Calling 协议）
8. E2E 冒烟：mock LLM 返回 tool_calls 脚本，验证先修校验 / 场地冲突 / 草稿落库 / 兜底建单 / 日志回放

## 八、验收标准（阶段 6 自测口径）

1. 问"S001 能不能选 SW301" → Agent 调 `check_prerequisite`，回答含判定结论（未修数据结构）与数据来源标注
2. 问"实训室 X102 周三 3-4 节能用吗"且 X102 维护占用 → 返回 needs_manual，并自动建兜底交接单（BUS 编号可查）
3. 旧代码选课校验走映射表，mapping_note 显示换算说明
4. 学生会话要求生成调课草稿 → 服务端拒绝（1011），审计留痕
5. 排课专员会话生成草稿 → drafts 表 pending_confirm，学生端与管理端均可见"待确认"，确认后才写正式回放记录
6. 断开外部 mock 数据（模拟故障）→ 快照降级，回答带"基于 xx 时间快照"标注；无快照 → 触发必转场景 4
7. 全部工具调用在 agent_tool_logs 有轮次/入参/出参/耗时记录
