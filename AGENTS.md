# AGENTS.md

## 1. 文档定位

本文件是项目的长期协作约定和进度入口，供项目所有参与者与 AI Agent 阅读。

进入项目后，应先阅读本文件，再开始方案设计、编码、审查或生成任务。后续若项目目标、技术路线、人员分工、阶段状态或关键约束发生变化，应同步更新本文件，并在“更新记录”中留下简要记录。

本文件不替代详细设计文档。架构、路线、学习计划和具体实施任务将在后续文档中展开；本文件只保存长期有效的信息和当前阶段摘要。

## 2. 项目目标

开发一个面向游戏公司测试开发岗位的、成熟且可公开展示的 Agent 工程项目，并最终发布到项目所有者的个人 GitHub 账户。

项目需要同时满足以下目标：

1. **岗位相关性**：围绕游戏质量保障、自动化测试、测试数据生成、结果校验、缺陷分析和测试闭环展开，而不是通用聊天机器人或简单的 LLM API 套壳。
2. **Agent 技术深度**：真实使用 Agent 规划、工具调用、工作流编排、RAG、MCP、Skills、状态管理、失败恢复、评测和可观测性。
3. **工程成熟度**：具备清晰架构、自动化测试、结构化日志、异常处理、数据库持久化、Docker 部署、文档和可复现演示。
4. **可解释和可学习**：项目所有者不仅要能演示项目，还应能解释核心原理、技术选型、关键代码、工程权衡和失败案例。
5. **作品集质量**：仓库应具有专业 README、架构图、演示材料、运行说明、评测结果和合理的 Git 历史，能够作为面试作品独立阅读和运行。
6. **持续推进**：采用阶段化交付，每个阶段都有明确范围、验收标准、学习目标和复盘，避免只有宏大设计而长期没有可运行成果。

## 3. 当前项目方向

项目正式采用暂命名为 **GamePilot** 的游戏智能测试 Agent 平台方向：由 Agent 理解游戏规则或设计文档，规划测试路径，通过工具操作一个可控的游戏测试环境，校验游戏状态，发现异常，收集证据，生成缺陷报告，并将缺陷沉淀为回归测试。

为控制范围，游戏部分倾向于采用“最小可控游戏测试靶场”，而不是开发完整商业小游戏。可从无界面的回合制游戏状态机开始，随后按需要增加简单网页 UI、Playwright 操作和视觉测试能力。

第一阶段范围已经冻结为“确定性回合制游戏核心与 FastAPI 接口”。该阶段先建立可靠的被测对象，不接入 LLM、数据库、Docker、前端或 MCP。详细范围见 `docs/PHASE_1_PLAN.md`。

## 4. 项目所有者的角色

项目所有者是最终决策者和主要学习者，负责：

- 决定最终产品方向、范围和优先级；
- 对重要技术选型和范围变化作最终确认；
- 使用并理解项目，而不是只接受 AI 生成的结果；
- 按阶段完成必要的原理学习、代码阅读和复盘；
- 决定何时公开仓库、部署项目或向外部展示；
- 在 Codex 与 Claude Code 意见不一致时作最终裁决。

## 5. Codex 的分工与能力边界

Codex 在本项目中担任**项目构思官、架构顾问、进度监督者和独立审查者**。

### 5.1 Codex 负责

- 澄清项目目标，控制范围并维护总体路线；
- 设计产品方案、系统架构、模块边界和阶段里程碑；
- 将阶段目标拆解为可交给 Claude Code 的结构化任务；
- 为每个任务定义背景、范围、接口、约束、测试和验收标准；
- 检查 Claude Code 修改后的代码、Git Diff、测试结果和文档；
- 识别过度设计、实现偏航、技术债、安全风险和测试缺口；
- 根据审查结果生成返工任务或下一阶段任务；
- 维护项目进度、关键决策、风险和学习路线；
- 向项目所有者解释技术原理，并准备面试讲解材料；
- 在重要节点判断项目是否达到“可运行、可靠、可评测、可交付”的成熟度。

### 5.2 Codex 默认不负责

- 默认不承担连续的大规模功能编码；主要实现工作交给 Claude Code；
- 不在项目所有者未确认的情况下扩大产品范围或改变主方向；
- 不以未经验证的 Claude Code 汇报代替实际代码和测试审查；
- 不替项目所有者宣称已经理解尚未学习或复盘的技术；
- 不擅自向 GitHub 推送、公开仓库、部署线上服务或执行其他外部发布操作；
- 不假设能够直接调用 VS Code 中的 Claude Code 插件。

Codex 可以为了维护协作机制、完成独立验证或修复阻塞性小问题而修改少量文件，但发生此类修改时应明确说明，并避免与 Claude Code 的进行中任务产生冲突。

## 6. Claude Code 的分工与能力边界

Claude Code 在本项目中担任**主要实施工程师**。

### 6.1 Claude Code 负责

- 阅读本文件以及当前任务引用的设计文档；
- 按任务单实现功能、测试、数据库迁移、配置和必要文档；
- 在编码前检查现有代码和约束，避免重复实现或破坏已有行为；
- 保持修改范围与任务目标一致；
- 运行任务要求的格式化、静态检查、单元测试和集成测试；
- 对无法完成或需要改变设计的事项及时说明，不用临时方案掩盖问题；
- 完成后报告修改文件、关键设计、验证命令、测试结果、遗留问题和风险；
- 保持代码可读、可测试、可维护，并为非显然的设计提供必要说明。

### 6.2 Claude Code 不应

- 未经确认改变项目目标、核心架构或公开接口；
- 超出任务范围进行大规模重构；
- 为追求“看起来完整”而提交不可运行的占位实现；
- 删除、覆盖或回滚项目所有者的现有修改；
- 跳过测试后声称任务已经完成；
- 将全部判断交给 LLM：关键业务约束应尽量使用确定性代码和测试验证；
- 自行向远程仓库推送、发布版本或部署生产环境，除非项目所有者明确授权。

## 7. Codex 与 Claude Code 的协作边界

当前两者之间没有已确认可用的原生直接通信通道。协作以**共享项目文件和项目所有者转交消息**为主：

1. Codex 与项目所有者确定阶段目标；
2. Codex 编写结构化任务单和验收条件；
3. 项目所有者通知 Claude Code 读取并执行任务单；
4. Claude Code 在当前项目目录中实施并完成自检；
5. 项目所有者通知 Codex 实施已完成；
6. Codex直接检查工作区、差异和测试结果；
7. Codex 给出通过、返工或下一阶段建议；
8. 项目所有者对范围和重大争议作最终决定。

建议后续使用以下目录保存协作材料：

```text
docs/
├─ PROJECT_CHARTER.md
├─ ARCHITECTURE.md
├─ ROADMAP.md
├─ DECISIONS.md
├─ PROGRESS.md
├─ LEARNING_PLAN.md
└─ claude-tasks/
   ├─ TASK-001.md
   └─ ...
```

在这些文档创建之前，本文件是项目状态的唯一正式入口。

## 8. 工程原则

- **游戏场景优先**：所有核心能力应能说明其对游戏测试的实际价值。
- **先闭环，再扩展**：优先完成一条端到端可运行链路，再增加多 Agent、视觉或复杂平台能力。
- **确定性与智能结合**：LLM 负责理解、规划和处理模糊信息；状态转换、关键断言和安全边界尽量由确定性程序负责。
- **可观测**：重要 Agent 决策、工具调用、状态变化、耗时、Token 和错误应可追踪。
- **可评测**：不能只凭演示判断效果，应建立固定任务集、已知缺陷和量化指标。
- **可复现**：游戏随机行为应支持固定随机种子；缺陷应保留操作轨迹、状态、日志和证据。
- **安全使用工具**：数据库工具默认最小权限；外部内容不得直接取得代码执行或高风险工具权限。
- **数据库是业务组成部分**：数据库应承担真实的游戏状态和测试记录持久化，而不是为了展示技术栈而存在。
- **Docker 是交付方式**：最终目标是一条明确命令启动必要服务，并具有健康检查和持久化策略。
- **学习与开发同步**：引入新技术前后，都应安排对应的原理学习、最小练习和项目复盘。
- **避免过度工程化**：除非负载或边界确实需要，早期优先采用模块化单体，而不是为了展示而拆分微服务。

## 9. 当前能力与待学习内容

### 9.1 已掌握或已有基础

- Python 基础
- FastAPI
- LangChain
- MCP
- Skills
- RAG

### 9.2 需要在项目中持续补充

- Docker、Docker Compose、镜像构建、容器网络和数据卷
- PostgreSQL 等关系型数据库基础
- 数据建模、SQL、索引、事务、并发和数据库迁移
- LangGraph 或等价的显式工作流与状态管理
- Pytest、接口测试、UI 自动化和测试设计方法
- 异步任务、重试、幂等和故障恢复
- Agent 评测、Prompt 版本管理和回归测试
- 日志、链路追踪、指标和可观测性
- Git、CI/CD、部署与开源仓库维护
- 游戏测试领域知识，包括状态机、数值规则、随机性、存档和回放

## 10. 当前项目进度

**状态日期：2026-09-21**

**当前阶段：第三阶段；TASK-002A、TASK-002B、TASK-003A 与 TASK-003B 已通过独立验收。
TASK-003C-R1 离线工程链路与 V2 预算/费用工程准备通过，DeepSeek Flash 真实 Gate A 与 normal 三目标 Gate B 已通过；Gate C 与学习复盘仍待完成。**

已经完成：

- 明确项目用途：个人 GitHub 作品集和游戏公司测试开发 Agent 岗位面试；
- 梳理了已有技术栈和主要学习缺口；
- 根据目标岗位职责讨论了通用测试 Agent、API 测试、PR 质量门禁、RAG 评测和游戏测试等方向；
- 识别出通用测试平台与游戏公司的联系不够直接；
- 正式采用 GamePilot 游戏智能测试 Agent 项目方向；
- 明确游戏部分应是小型、可控、可植入缺陷的测试靶场，而不是完整游戏产品；
- 初步形成 Codex 负责构思、监督和审查，Claude Code 负责主要实施的协作模式；
- 已初始化 Git 仓库，当前分支为 `main`，远程仓库为 `GamePilot-Agent-`；
- 已冻结第一阶段范围和技术栈，方案见 `docs/PHASE_1_PLAN.md`；
- 已生成并由 Claude Code 实施第一张任务单 `docs/claude-tasks/TASK-001-deterministic-game-core.md`；
- 已完成确定性战斗领域层、内存仓储、FastAPI 接口、结构化事件与统一错误响应；
- Codex 已完成独立审查和收尾修正：失败分支可通过公开动作到达，OpenAPI 已声明 404/409 错误模型，本地 Agent/Skill 配置已移出项目暂存范围；
- 第一阶段自动化验收结果：`30 passed`，Ruff 静态检查和格式检查全部通过；
- 已确定第二阶段采用 PostgreSQL、SQLAlchemy、Alembic 和 Docker Compose，并拆分为数据库骨架与仓储接入两步；
- 已生成 `docs/PHASE_2_PLAN.md` 和 `docs/claude-tasks/TASK-002A-database-foundation.md`。
- Claude Code 已完成 TASK-002A：数据库配置、SQLAlchemy ORM、Alembic 初始迁移和 PostgreSQL 16 Compose 服务；
- Codex 已完成独立验收：Compose 服务健康，空库升级、降级和重复升级通过，实际表/约束/索引符合设计，`50 passed`，Ruff 检查和格式检查通过。
- 已生成 `docs/claude-tasks/TASK-002B-postgres-repository.md`，明确仓储接入、事务、确定性恢复和集成测试；种子兼容修正及接入边界记录在 `docs/DECISIONS.md`。
- Claude Code 已完成 TASK-002B 实施（PostgreSQL 仓储与事务边界、确定性重放恢复、0002 种子文本迁移、后端选择与生命周期、真实数据库集成测试）。
- Codex 已完成 TASK-002B 首轮审查与返工修复：未分类 SQLAlchemy 异常不再把原始 SQL 和参数抛给服务器，追加保存前会重放验证现存状态与事件，启动探测失败也会释放 Engine；文档偏差已同步修正。
- TASK-002B 最终独立验收通过：`70 passed` 普通测试、`29 passed` PostgreSQL 测试，Ruff 检查与格式检查通过，Alembic 位于 `0002_session_seed_text (head)` 且无模型漂移，测试数据已精确清理。
- Claude Code 已完成 TASK-003A 实施：新增 `gamepilot.testing`（HTTP 工具、场景模型、顺序执行器、独立判定器、报告与 `run`/`replay` 命令行），判定规则独立来自规则文档，不导入被测实现；HTTPX 提升为运行依赖。
- TASK-003A 首轮实施时，Claude Code 自测（当时尚未独立验收）：`118 passed`（`-m "not postgres"`）、`ruff check .` 与 `ruff format --check .` 通过，并完成真实本地 HTTP 的 `run` 与 `replay` 各一次（均 exit 0、逐字段一致），报告位于 `artifacts/test-runs/`。

- 项目所有者授权 Codex 直接完成 TASK-003A-R1：修复状态约束、会话与请求校验、错误证据保存、实际轨迹重跑、严格报告读取和报告 I/O 错误；报告版本升级到 1.1、规则版本升级到 1.1.0。
- TASK-003A-R1 复验通过：`171 passed` 非 PostgreSQL 测试（`29 deselected`），Ruff 检查及 60 个文件格式检查通过；真实本地 HTTP 的独立 CLI 进程 run/replay 均 exit 0，8 个场景通过、8 个场景重跑一致。数据库相关代码未改动，本轮未复验 PostgreSQL，未提交或推送。
- Claude Code 已完成 TASK-003B 首轮实施（审查结论见下文）：新增 `gamepilot.lab`
  （三个真实状态缺陷 + 独立内存靶场）与 `gamepilot.benchmark`（32 组合固定矩阵、同 profile 重跑、
  负向验证与 7 项带分子/分母的指标），并在领域层新增三个窄扩展点、路由支持注入会话工厂。
  正常入口增加默认会话工厂装配，公开 API 契约、仓储与数据库结构未改动；未接入 LLM/MCP，未提交或推送。
- TASK-003B 首轮实施自测结果（当时尚未独立验收）：`322 passed`（含 `29 passed` PostgreSQL 集成测试；
  非 PostgreSQL 为 `293 passed`、`29 deselected`），`ruff check .` 与
  `ruff format --check .`（84 个文件）通过；`gamepilot.benchmark run` 退出码 0，
  32/32 组合符合清单、9/9 触发与目标规则命中、7/7 指标达标（含 `replay_consistency 32/32`）、
  负向验证 mismatch；四个 profile 均完成真实 localhost HTTP 的 CLI 验证
  （normal exit 0，三个缺陷 profile exit 1，同 profile replay 均 8/8 一致）。

- Codex 已完成 TASK-003B 首轮独立审查：293 项非 PostgreSQL 和 29 项真实 PostgreSQL 回归、Ruff（84 个文件）、32 组合 benchmark 和四个 profile 的真实 HTTP run/replay 均通过；与 Git HEAD 正常领域实现的 21 条轨迹、140 次动作前缀对比一致。
- 首轮独立异常探针发现一项 P2：同 profile replay 或负向验证执行失败时，benchmark 误返回 1 而非 2，摘要漏报该阶段执行错误。当时已生成 `docs/claude-tasks/TASK-003B-R1-review-fixes.md` 并暂缓验收。
- 经所有者授权，Codex 已完成 TASK-003B-R1：保留各阶段执行状态与错误证据，执行错误主导退出 2，同 profile 与负向验证错误分别统计；benchmark 版本为 1.1.0，testing 契约和规则不变。
- R1 复验通过：302 项非 PostgreSQL 测试（含新增 9 项异常路径集成测试）、Ruff（86 个文件）、独立 CLI 固定矩阵通过；首轮两个 503 反例均正确退出 2。本轮未修改数据库相关路径、未重复上一轮已通过的 29 项 PostgreSQL 回归，未提交或推送；TASK-003B 验收完成。
- Claude Code 已完成 TASK-003C 首轮实施：新增 LangGraph 单 Agent 闭环、两个工具、动作/模型/格式/总时限预算、独立 Agent 报告与 12 格配对评测；真实模型默认关闭。
- Codex 首轮审查发现四项阻塞问题，并经所有者授权完成 TASK-003C-R1：评测按 Agent、重跑、对照与报告 I/O 分阶段传播错误，可靠累计 Token/费用，逐请求执行总时限检查，多工具纠正一次回传全部 `tool_result`；Agent schema 升级到 1.1，agent benchmark 升级到 1.1.0。
- TASK-003C-R1 独立复验通过：`443 passed, 29 deselected` 非 PostgreSQL 回归，Ruff 检查与 114 个文件格式检查通过；原 32 组合脚本 benchmark exit 0，离线 12 格 Agent benchmark exit 0、12/12 重跑一致；模型 timeout/429/5xx、重跑 503、对照 503 和各阶段报告写入失败均正确主导 exit 2，普通 mismatch 保持 exit 1。未调用真实模型，未复验 PostgreSQL，未提交或推送。
- 已生成 `TASK-003C-V1-real-model-acceptance.md`，把真实验收拆为单格、normal 三目标和完整 12 格三道独立付费关卡，并先补齐 benchmark 预算、费用汇总与模型响应诊断证据。
- 所有者授权执行一次最小 Gate A。真实 `deepseek-v4-flash` 接口与 usage 链路连通，但两次回复均无 `tool_use`，最终 `model_format_error` / exit 2；实际 2 次调用、0 动作、1,654 Token，费用因无单价为 unknown。Gate B/C 未执行，证据见 `docs/validation/TASK-003C-real-model-acceptance.md`。
- 所有者授权 Codex 直接使用密钥修复真实链路。Agent 报告升级到 schema 1.2，模型调用记录新增供应商 stop reason 与文本响应；诊断确认未指定工具选择时首轮恰好耗尽 1,024 输出 Token、`stop_reason=max_tokens` 且无工具块。
- Anthropic 兼容请求现显式使用 `tool_choice=any` 并禁止并行工具调用，与本地“每轮恰好一个工具”协议一致。修复后真实 Gate A 在 512 输出 Token、0 次格式纠正下以 `attack`、`use_potion` 两次工具调用完成，exit 0、2/2 覆盖满足、replay 1/1 match；用量 772 Token、耗时 3.19 s。完整非 PostgreSQL 回归 `445 passed, 29 deselected`，Ruff 检查与 116 个文件格式检查通过；未复验 PostgreSQL，未提交或推送。
- 已生成下一实施任务 `TASK-003C-V2-benchmark-budget-cost.md`：在不调用真实模型的前提下，让 12 格 Agent benchmark 接受显式 `BudgetSpec` 与价格配置，并以严格 unknown 语义汇总整批 Token/费用；Gate B/C 仍需后续独立决策。
- 经所有者授权，Codex 已完成 TASK-003C-V2：Agent benchmark 接受七项显式预算和严格 all-or-none 价格配置，同一规格贯穿每格独立计数器；摘要按计划格数汇总 usage 已知/未知数，并在任一格未知时保守保持整批 Token/费用 unknown。agent benchmark 升级到 1.2.0，Agent/testing 报告版本不变。
- TASK-003C-V2 验收通过：`466 passed, 29 deselected` 非 PostgreSQL 回归，Ruff 检查与 118 个文件格式检查通过；离线 12 格 exit 0、12/12 重跑一致，原 32 组合 exit 0、7/7 指标达标。未调用真实模型，未复验 PostgreSQL，未提交或推送。
- 所有者随后明确授权直接使用旧密钥执行 Gate B。normal profile 的 full-health、healing、victory 三目标均 goal_met / exit 0、规则失败 0、无模型 replay 3/3 match；共 7 个动作、8 次模型调用、1 次受控格式纠正，prompt 1,998、completion 1,630、total 3,628 Token。没有可靠单价，费用保持 unknown；Gate C 未执行。
- 已生成 `TASK-003C-V3-real-gate-c-benchmark.md`：下一任务只执行真实固定 12 格，不再修改 Agent；统一逐格预算为 6 动作、6 次调用、1 次格式纠正和 512 输出 Token，整批上限 72 次调用与 36,864 输出 Token。Gate A/B 共 10 次有效调用、4,400 Token，为 Gate C 提供 14,512 Token 的矩阵外推与 31,680 Token 的满调用经验估算；执行仍需单独授权并确认价格/金额口径。

尚未完成：

- 根据 Gate A/B 实际 Token 证据决定并执行 Gate C 固定 12 格；
- 完成本阶段对应的原理学习和代码复盘；
- 最终确认项目公开名称、许可证和完整 README 展示策略。

## 11. 下一决策点

TASK-003B 已完成首轮独立审查和 R1 修复复验，重放执行错误已正确主导退出码。
最终验证记录见 `docs/claude-tasks/TASK-003B-R1-review-fixes.md` 第 5 节。
原任务单和 README 保留范围及矩阵说明；原理学习与代码复盘仍待所有者完成。

`docs/PHASE_3_PLAN.md` 的次序已落实为：确定性执行器与判定基线 → 可控缺陷 → 首次 Agent。
TASK-003C-R1 已修复首轮审查发现的错误传播、usage、总时限与多工具协议问题，离线工程链路
通过独立复验；记录见 `docs/claude-tasks/TASK-003C-R1-review-fixes.md`。模型仍为 DeepSeek
`deepseek-v4-flash`。首次真实 Gate A 的失败证据已定位为 `max_tokens`；协议级强制单工具选择
修复后，真实模型用两次工具调用完成 healing，exit 0 且无模型 replay match，Gate A 已通过。
Gate B 三目标也已全部 exit 0 且 replay 3/3 match；Gate C 未执行，记录见
`docs/validation/TASK-003C-real-model-acceptance.md`。
TASK-003C-V2 已完成无付费工程准备：真实 Gate C 可以在显式逐格预算下运行，报告会记录
价格、已知/未知 usage 与整批费用；项目不内置或猜测供应商价格。

后续需要依次确认：

1. TASK-003A / TASK-003B / TASK-003C 学习复盘；
2. 轮换已进入聊天上下文的密钥，并确认显式单价与 Gate C 金额预算；
3. 根据 Gate A/B 的 4,400 个已知 Token 与 Gate C 技术上限，单独决定 Gate C；
4. 完成 Gate C 后给出 TASK-003C 的最终真实模型结论；
5. 项目公开名称、许可证和仓库展示策略。

## 12. 更新规则

- 目标、角色边界和工程原则只在形成明确决策后更新；
- 日常细节进入 `PROGRESS.md`，本文件只保留阶段摘要；
- 每完成一个里程碑，应更新“当前项目进度”；
- 每次更改架构方向，应在 `DECISIONS.md` 中记录原因和替代方案；
- 每张 Claude Code 任务单必须引用本文件，并说明其对应的里程碑；
- 若代码现状与本文档不一致，应明确指出差异，不能默默假设文档或代码其中一方正确。

## 13. 更新记录

- **2026-09-21**：规划下一任务 TASK-003C-V3，范围冻结为 DeepSeek Flash 真实 Gate C 固定 12 格验收。复用现有 agent benchmark，不新增 Agent 功能；记录 72 次调用和 36,864 输出 Token 技术上限、Gate A/B 经验估算、独立授权、密钥注入、单次执行与不自动重跑规则。Gate C 尚未执行，未调用模型、未提交或推送。

- **2026-09-21**：经所有者明确授权，Codex 使用旧密钥完成真实 Gate B。normal 三目标均 goal_met / exit 0、规则失败 0、replay 3/3 match；合计 8 次模型调用、3,628 Token，其中 healing 使用 1 次预算内格式纠正。密钥未写入报告或工作区，进程环境变量已清除，本地服务已停止；费用因无可靠单价保持 unknown，Gate C 未执行，未提交或推送。

- **2026-09-21**：经所有者授权，Codex 完成 TASK-003C-V2。Agent benchmark 新增七项显式预算、严格 all-or-none 价格配置、逐格预算/费用传递和保守的整批 usage/费用汇总，版本升到 1.2.0。`466 passed, 29 deselected`、Ruff（118 个文件）、离线 12 格与原 32 组合均通过；未调用真实模型、未复验 PostgreSQL、未提交或推送。Gate B/C 仍待独立决策。

- **2026-09-21**：规划下一任务 TASK-003C-V2。范围冻结为 Agent benchmark 的七项显式预算、严格价格输入、12 格 usage/费用汇总和 agent benchmark 1.2.0；默认离线成绩与原 32 组合必须保持不变。本任务禁止真实模型调用，Gate B/C 继续作为后续独立决策，未修改功能代码、未提交或推送。

- **2026-09-21**：经所有者授权，Codex 修复真实 Agent 工具链路。Agent schema 1.2 新增 stop reason/文本诊断，确认首轮失败为 `max_tokens`；Anthropic 兼容请求增加 `tool_choice=any` 与禁止并行工具调用。修复后 Gate A 以 2 次工具调用、0 次格式纠正完成 healing，exit 0、replay match，用量 772 Token。`445 passed, 29 deselected`、Ruff 与 116 文件格式检查通过；Gate B/C 未执行，未复验 PostgreSQL，未提交或推送。

- **2026-09-21**：规划 TASK-003C-V1 真实模型分级验收，并经所有者授权执行一次最小 Gate A。真实 DeepSeek Flash 接口、认证与 usage 链路成功，2 次回复均无 tool_use，格式纠正耗尽后 exit 2；0 个游戏动作，1,654 Token，费用 unknown。未自动重跑，Gate B/C 未执行；报告未发现密钥文本。本次密钥曾进入聊天上下文，后续应轮换。下一步先补 stop reason/文本响应诊断和 benchmark 预算/费用汇总，再决定是否重新授权。

- **2026-09-21**：经所有者授权，Codex 完成 TASK-003C-R1 修复与复验。Agent 评测现按原始运行、重跑、对照与报告 I/O 分阶段传播错误，Token/费用可靠累计，总时限在每次 HTTP 请求前检查，多工具纠正闭合全部 tool_use。443 项非 PostgreSQL 回归、Ruff、原 32 组合和离线 12 格评测通过；异常回归均正确 exit 2。未调用真实模型或复验 PostgreSQL，TASK-003C 的离线工程链路通过，最终真实模型验收仍待费用确认，未提交或推送。

- **2026-09-21**：Codex 完成 TASK-003C 首轮独立审查。427 项非 PostgreSQL 回归、Ruff、原 32 组合和离线 12 格正常路径通过；异常探针确认 Agent 评测会降级模型/重跑错误、usage 永久 unknown、总时限后续请求未阻断及多工具纠正协议不完整。已生成 TASK-003C-R1 返工单，暂不验收；未修改功能代码、未调用真实模型或复验 PostgreSQL，未提交或推送。

- **2026-09-20**：应所有者要求规划下一任务，新增 TASK-003C LangGraph 单 Agent 工作流草案，明确增量执行、预算、独立判定、兼容重跑和 12 组合配对子集评测；所有者指定沿用 Claude Code 的 DeepSeek Flash，已核对项目模型为 `deepseek-v4-flash`、进程接口为 Anthropic 兼容入口，并在任务单记录进程 Pro 模型变量的差异。修正 PHASE_3 顶部过时状态；范围、LangGraph 路线与付费预算待确认，未修改功能代码、未调用模型或开始实施。

- **2026-09-20**：经所有者授权，Codex 完成 TASK-003B-R1 修复与复验；新增分阶段错误证据与计数，benchmark 1.1.0 正确执行 error 主导退出码。302 项非 PostgreSQL 测试、Ruff、固定矩阵及两个原始反例复验通过；本轮未重复数据库测试，TASK-003B 验收完成，学习复盘待完成，未提交或推送。

- **2026-09-20**：TASK-003B 首轮独立审查完成。322 项分组回归、Ruff、固定矩阵、真实 HTTP 与旧实现轨迹对比通过；新增异常探针复现重放执行错误被误归为 exit 1，生成一项 P2 返工单，暂不验收通过；未修改功能代码或提交推送。

- **2026-09-20**：TASK-003B 实施完成，待 Codex 独立验收；新增 `gamepilot.lab`（三个真实状态缺陷与独立内存靶场）与 `gamepilot.benchmark`（32 组合清单、同 profile 重跑、负向验证与 7 项指标），领域层新增三个窄扩展点、路由支持注入会话工厂；全量测试 322 项（含 29 项 PostgreSQL）、Ruff 与真实 HTTP CLI 验证通过，未提交或推送。

- **2026-09-18**：生成 TASK-003B 可控缺陷靶场与评测集草案，细化三个缺陷、独立内存入口、32 个评测组合与真实数据库回归要求；待确认实施，未修改功能代码。

- **2026-09-18**：经所有者授权，Codex 完成 TASK-003A-R1 修复与复验；171 项非 PostgreSQL 测试、Ruff、真实 HTTP run/replay 通过，TASK-003A 验收完成；本轮未复验数据库，学习复盘仍待完成。

- **2026-09-18**：TASK-003A 首轮独立审查完成，常规回归与 Ruff 通过，补充反例揭示六组验收问题；生成 TASK-003A-R1 返工单，尚未修改功能实现。
- **2026-09-18**：TASK-003A 实施完成，待 Codex 独立验收；新增 `gamepilot.testing`（HTTP 工具、场景模型、顺序执行器、独立判定器、报告与 `run`/`replay` 命令行），HTTPX 提为运行依赖，未改动战斗规则、公开 API、仓储与数据库结构，未提交或推送。
- **2026-09-18**：新增第三阶段方案与 TASK-003A 规划草案，供项目所有者确认；未修改功能代码，未开始实施或变更既有验收结论。
- **2026-09-15**：创建 `AGENTS.md`，记录项目目标、角色分工、能力边界、协作方式和当前进度。
- **2026-09-15**：项目正式开始；确认 GamePilot 方向，冻结第一阶段范围，并生成 PHASE_1 方案与 TASK-001 实施任务单。
- **2026-09-16**：TASK-001 实现、独立审查和收尾修正完成；30 个测试及 Ruff 质量检查通过，第一阶段进入学习复盘。
- **2026-09-16**：确定第二阶段数据库持久化路线，拆分 TASK-002A/TASK-002B，并生成 PHASE_2 方案和 TASK-002A 任务单。
- **2026-09-17**：TASK-002A 实现与独立验收完成；PostgreSQL Compose、Alembic 迁移、真实表结构、50 个测试和 Ruff 质量检查全部通过。
- **2026-09-17**：生成 TASK-002B 实施任务单，明确 PostgreSQL 仓储、原子写入、动作重放、种子兼容迁移和独立测试库要求。
- **2026-09-17**：TASK-002B 首轮独立审查发现三处失败路径缺陷，已生成 TASK-002B-R1 返工单；阶段暂未验收通过。
- **2026-09-17**：Codex 完成 TASK-002B-R1 修复与复验；99 项分组测试、Ruff、Alembic 与测试数据清理检查全部通过，TASK-002B 正式验收完成。
