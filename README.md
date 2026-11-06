# 知行职业学院 · 排课选课教务协作 AI 智能体系统

面向职业院校教务场景的 AI 智能体：**冲突前置提示 + 教务制度问答 + 调课协作留痕**。
冲突判定由纯代码规则引擎完成，LLM 只负责解释结果与组织语言；AI 不自动选课、不修改排课与培养计划。

## 业务背景

来自《业务需求发现报告》（36 条访谈事实，6 个业务角色）的核心痛点：

- 学生选课后才发现先修课 / 教室 / 实训室场地冲突，返工量大（每学期约 360 个冲突，目标降到 80）
- 实训室设备维护占用场地，排课表仍标可用，一线靠便签和微信群交接、缺少随单留痕
- 教务系统、场地预约、学籍库三套系统数据分散，课程新旧代码并存
- 管理层目标：每学期冲突 360 → 80，调课处理 3 天 → 1 天
- 试点验收口径：单专业试点，时空冲突识别率 100%，人工调整工时下降 30%

**硬约束**：接口只读，禁止自动选课与修改培养计划；四类场景必转人工（场地占用失真、资料缺依据、规则版本冲突、系统不可用）；错误建议不覆盖原记录；教学质量专员拥有上线否决权。

## MVP 说明

一期最小可验证闭环（对应 docs/01 需求清单 REQ-1 + REQ-2）：

1. **制度知识库问答（RAG）**：培养方案、先修规则、场地规则文档 → 500 字符切片 → BM25 + 向量混合检索 top3 → LLM 依据条文回答，末尾附引用来源；无依据时回复【制度中未找到对应说明，请联系排课专员人工核实】
2. **冲突校验工具（规则引擎）**：先修冲突 `check_prerequisite`、场地/实训室冲突 `check_room_conflict`，与知识库旧代码映射 `course_mapping`
3. **调课草稿人工确认流**：`generate_adjust_draft` 生成草稿前强制中断，排课专员确认后才落单
4. **协作交接单**：业务编号 + 版本 + 状态 + 未解决事项 + 接手人，对方确认接手才算完成，超时升级教务主任
5. **管理后台**：RBAC 登录、知识库管理、系统提示词在线编辑（存库即生效）、对话审计与高风险标记

评测集：[evaluation/qa_cases.json](evaluation/qa_cases.json)，共 30 条（知识库问答 10 / 工具调用 10 / 违规意图 5 / 多轮对话 5）。

## 部署步骤

环境要求：Python 3.11+（Windows / macOS / Linux 均可）

```sh
# 1. 进入项目目录，安装依赖
cd zhixing-course-agent
python -m venv .venv
.venv\Scripts\activate        # Windows（macOS/Linux 用 source .venv/bin/activate）
pip install -r requirements.txt

# 2. 配置环境变量（LLM Key、JWT 密钥等）
copy .env.example .env        # macOS/Linux 用 cp，然后编辑 .env 填入 API Key

# 3. 启动服务（首次启动自动建库、写种子账号与默认提示词模板）
uvicorn app.main:app --port 8000
```

启动后：

- 学生/教职工聊天页：`http://127.0.0.1:8000/`
- 管理后台：`http://127.0.0.1:8000/web/admin/login.html`
- 默认管理账号：`scheduler` / `quality` / `director` / `counselor` / `program_leader`，初始密码 `Zx@123456`（上线前必须修改）
- 裸 RAG 命令行原型（阶段 1 验证用）：`python scripts/prototype_rag.py`

## 角色说明

| 角色 | 账号 | 职责与权限 |
|------|------|-----------|
| 学生 | 注册/聊天页 | 冲突问答、选课冲突前置查询、查看冲突提示；无管理权限 |
| 班级辅导员 | counselor | 现场异常登记、查看冲突提示与交接进度 |
| 排课专员 | scheduler | 知识库维护、调课草稿确认、交接单派发与核对 |
| 教学质量专员 | quality | 对话审计、提示词查看、**试点上线一票否决** |
| 教务主任 | director | 试点开关、超时升级处理、指标口径确认 |
| 专业课程负责人 | program_leader | 服务结果验收、争议受理 |

## 目录结构

```
zhixing-course-agent/
├── app/
│   ├── main.py              # FastAPI 入口：建库 → RAG 流水线 → 路由
│   ├── config.py            # 全局配置（.env 读取）
│   ├── api/                 # chat / auth / admin_kb / admin_prompt / admin_audit / admin_workflow / co
│   ├── agent/tools.py       # 6 个 Function Calling 工具定义与执行
│   ├── rules/               # 冲突规则引擎（先修 / 场地 / 新旧代码映射）
│   ├── rag/                 # 切片、向量化、检索、流水线
│   ├── adapters/            # 教务 / 场地只读适配器（试点期读 Mock，支持快照降级）
│   ├── core/                # 安全(JWT+RBAC)、LLM 网关、业务编号、统一错误
│   ├── db/                  # SQLite 初始化、种子数据
│   ├── services/            # 聊天编排、协作工作流
│   └── web/                 # 聊天页 + 管理后台静态页
├── data/
│   ├── kb_source/           # 教务制度源文档（markdown）
│   └── mock/                # 教务/场地 Mock 数据（只读）
├── docs/                    # 01 需求清单 ~ 08 上线检查清单
├── evaluation/qa_cases.json # 30 条评测用例
├── scripts/prototype_rag.py # 阶段 1 裸 RAG 原型
├── requirements.txt
├── .env.example             # 环境变量模板（含 LLM Key / JWT 密钥位）
└── .gitignore
```

## 已知业务限制（来自需求发现报告，均为待验证假设）

1. **识别率基线缺失**：冲突识别率 100% 的验收尚无分环节耗时基线，须先用历史资料回放统计漏报误报，由排课专员与教学质量专员确认识别口径后再谈验收
2. **指标分母未定**：360 → 80 的冲突计数口径（按事件数还是受影响学生数）未定，验收前须由教务主任固定分母、排除规则与统计周期；不能用通知数量冒充完成量
3. **数据口径风险**：三系统无统一视图、课程新旧代码并存，字段口径盘点与歧义记录人工修复队列是冲突识别可信度的前置条件
4. **过期数据边界**：数据每 15 分钟同步、超周期标「待刷新」不作实时承诺；过期状态是否参与冲突判断尚未核实，试点前必须明确
5. **便签转正式记录时限未核实**：交接「对方确认接手才算完成」已定，但转单时限与确认人待排课专员确认
6. **留存期限暂定**：评价记录 90 天为暂定值，真实期限须按组织规则由教学质量专员另行确定
7. **预算约束**：首期 10 万元覆盖集成、培训与验证，收益核算须扣除维护与人工复核成本
8. **试点范围**：仅一个专业（软件技术）试点，历史资料回放 → 小范围并行，不直接替换现有业务；触发停止条件即暂停并复盘

## 推送 GitHub 指引

1. 在 GitHub 建一个空仓库（建议 Private，如 `zhixing-course-agent`），**不要**勾选自动生成 README/.gitignore
2. 在项目根目录执行：

```sh
cd zhixing-course-agent
git init
git add .
git status                 # 确认 .env、data/app.db 等敏感/运行文件未被暂存
git commit -m "feat: 排课选课教务协作 AI 智能体系统（RAG + 规则引擎 + 人工确认工作流）"
git branch -M main
git remote add origin https://github.com/<你的用户名>/zhixing-course-agent.git
git push -u origin main
```

3. 提交前自查：`.gitignore` 已排除 `.env`、`data/app.db`、`__pycache__/`、`.venv/`；若误提交了 `.env`，先轮换 API Key 再清理历史
4. 建议在仓库 Settings → Branch protections 开启 main 分支保护；协作走 PR + 至少一人 Review（对齐教学质量专员复核机制）
