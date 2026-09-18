# TASK-003A：确定性测试执行器与规则判定基线

状态：项目所有者已确认实施；2026-09-18 完成 R1 修复与复验，验收通过。详见 TASK-003A-R1-review-fixes.md 第 10 节。
里程碑：第三阶段建议的第一步，仅实现本任务。
所有者明确通知 Claude Code 执行本任务，即确认本任务范围；后续阶段仍是建议。

## 1. 必读与目标

阅读 AGENTS.md、docs/PHASE_1_PLAN.md 第 5 节、docs/PHASE_2_PLAN.md、docs/DECISIONS.md、docs/PHASE_3_PLAN.md，以及当前领域模型、API 和 tests/integration/test_api.py。

交付一条本地命令：在新会话中执行固定场景，按规则判定，输出 JSON 证据；能读取报告，在新会话中重跑相同 seed 和动作尝试序列。Claude Code 负责实施，Codex 独立审查。

只新增 HTTP 工具、场景模型、顺序执行器、独立判定器、报告和 CLI。不修改战斗规则、公开 API、仓储或表结构，不接入 LLM/LangGraph/MCP，不实现缺陷开关、并发、自动重试或轨迹自动最小化。不批量删除、重构或覆盖用户修改。

## 2. 模块边界

建议在 src/gamepilot/testing/ 中组织 models、client、oracle、runner、CLI，可合并小文件，不建立通用插件框架。

- 工具提供 create_session(seed)、get_session(id)、perform_action(id, action)。
- 使用可注入的 httpx.AsyncClient，真实 HTTP 与测试 ASGITransport 共用适配器，明确客户端生命周期。
- HTTPX 目前为 dev 依赖；CLI 依赖时提升为运行依赖，不加入重型框架。
- 输出保留 HTTP 状态码和响应体，包括预期的 409；默认超时 5 秒，不自动重试动作。
- 每场景最多 20 次动作尝试，拒绝动作也计数；达到上限应记录 error。
- 判定器只读请求、预期、快照和事件，不调用 CombatSession、recovery、私有属性或 SQL。
- 规则数值独立来自规则文档，不从被测战斗实现导入常量作为正确答案；记录规则规格版本和来源。

## 3. 场景与结果

场景包含 case_id、说明、seed、顺序动作、各步预期 HTTP 状态及错误码。动作仅允许 attack/use_potion，不执行输入中的代码。

结果分三类：pass 为全部预期和规则满足；fail 为可用观测中的规则或接口预期不符；error 为连接失败、超时、5xx、不可解析响应或执行故障。游戏 lost 或预期内 409 可以是 pass。基础设施失败不得伪装成已确认的游戏规则缺陷。

首个 fail/error 后停止当前场景并保存证据，其他场景独立继续。409 后必须 GET 验证状态和完整事件未变；GET 失败记 error，不推断未变。

内置至少六个独立场景：

1. 创建并查询初始状态。
2. seed=42，满血喝药被拒绝。
3. seed=42，attack → use_potion，验证治疗上限和反击。
4. seed=42，attack 后连续三次 use_potion：前两次成功，第三次 no_potions。
5. seed=42，三次 attack 获胜，继续 attack 返回 battle_not_active。
6. 用公开动作到达 lost，之后动作被拒绝。实施时固定经过验证的 seed 与动作，禁止修改私有 HP。

另加对照验证：插入被拒绝动作后，后续合法动作的随机结果不变。上述 seed=42 的正常路径已对当前领域对象作运行检查，实施仍需通过 HTTP 验证。

## 4. 最低规则覆盖

每条规则有稳定 rule_id，失败保留期望、实际及步骤位置。

- 初始状态：玩家 HP=100，2 瓶药水，敌人 HP=60，active，turn=0，无事件。
- 快照和事件 HP 均在上下限内，药水非负；动作不改变 session_id、seed 或 max_hp。
- 成功动作 turn 恰好 +1，旧事件保持完整，新事件的顺序、类型和回合匹配动作。
- 攻击伤害 18～25；根据事件值校验 HP 扣减并截断到 0，不复制随机数实现。
- 喝药先治疗再反击；治疗量 min(25, max_hp - 动作前 HP)，药水恰好减 1；攻击不改变药水数量。
- 敌人存活时恰好一次反击，伤害 20～35；致死攻击不反击。反击事件 potions=null 是有效契约，不误报。
- won 对应敌人 HP=0，lost 对应玩家 HP=0，双方存活对应 active；最后事件的 HP 与最终快照一致。
- 满血喝药、无药喝药、终局动作返回预期 409 和错误码，HP、药水、回合及事件不变。

特别注意：喝药后的最终 HP 已经过反击，必须看药水事件判断治疗量，不能只比较动作前后 HP。重放相同实现只能检查可复现性，不能替代独立规则判断。

## 5. 报告与重跑

报告至少包含 schema_version、rules_version、case_id、run_id、seed、session_id、运行时版本、场景定义、实际执行动作、逐步预期和响应、前后快照、新增事件、规则检查结果与总状态。区分计划动作和已执行动作，记录时间及耗时。

报告版本标记不等于实现历史存档兼容。不保存环境变量、凭据或完整请求头；文件名用生成的 run_id，不直接用外部文本作路径；不覆盖已有报告，产物目录加入 .gitignore。

重跑校验报告版本和动作边界，在新会话执行原 seed 与已记录的动作尝试，包括被拒绝动作。目标地址必须由本次命令提供，不能自动采用报告中的地址，不执行报告里的命令。

比较状态、事件、响应码、错误码及规则结论，仅排除 session_id、run_id、时间和耗时等明确的非确定字段。报告缺失字段或版本不支持时明确拒绝，不通过忽略业务字段获得一致。超时导致结果未知时可重新执行检查，但不能承诺复现原错误。

建议命令（允许等价实现）：

```powershell
.venv\Scripts\python.exe -m gamepilot.testing run --base-url http://127.0.0.1:8000 --suite baseline --output-dir artifacts/test-runs
.venv\Scripts\python.exe -m gamepilot.testing replay --base-url http://127.0.0.1:8000 --report artifacts/test-runs/<报告文件>.json --output-dir artifacts/test-runs
```

退出码：0 全部通过；1 规则失败或重跑差异；2 输入或执行错误（混合时优先 2）。摘要显示 pass/fail/error 数量和报告路径。不得删除远端会话或直接清理数据库。

## 6. 验证与交付

- 正常内存应用下全部内置场景通过；真实本地 HTTP 完成一次运行和重跑，不只测试模拟客户端。
- 用固定错误观测分别验证治疗超上限、药水未扣除、致死后反击，命中对应 rule_id；正常邻近样本不误报。此处只构造测试数据，不改真实游戏为缺陷模式。
- 覆盖预期 409、连接/超时/5xx、无效 JSON、步数上限、报告版本错误、重跑差异、CLI 退出码。
- 两个新会话同 seed 同动作，明确规范化后状态与事件相同。
- 运行下列命令，记录实际结果和数量，不预先承诺测试总数：

```powershell
.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider -m "not postgres"
.venv\Scripts\ruff.exe check .
.venv\Scripts\ruff.exe format --check .
```

若确需触及应用装配、共享配置或仓储，先说明必要性并补跑真实 PostgreSQL 测试，只使用专用 TEST_DATABASE_URL，不将跳过视为通过，不清空开发库。

交付修改文件、设计说明、规则与场景清单、验证结果、正常报告示例、错误观测识别证据、重跑结果和限制。更新 README 与 AGENTS.md 实施状态，独立审查前不得写成已验收。

Codex 审查重点：判定独立性、事件时序、错误分类、拒绝动作保留、证据可复现性和范围边界。

学习复盘：项目所有者能解释为什么游戏失败也可能测试通过、为什么数值规则不用 LLM 裁决，以及 Pytest、执行器、未来 Agent 各自的职责。学习完成情况不得代替所有者确认。
