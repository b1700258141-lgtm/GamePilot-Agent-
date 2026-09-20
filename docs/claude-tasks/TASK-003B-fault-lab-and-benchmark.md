# TASK-003B：可控缺陷靶场与评测集

状态：2026-09-20 Codex 经所有者授权完成 R1 修复与复验，TASK-003B 验收通过；学习复盘待完成。原范围冻结于 2026-09-18，首轮审查及最终验证记录见 TASK-003B-R1-review-fixes.md。
里程碑：第三阶段第二步；前置 TASK-003A-R1 已验收。主要实施交给 Claude Code，Codex 独立审查。

## 1. 必须阅读

- AGENTS.md、docs/PHASE_3_PLAN.md、docs/DECISIONS.md。
- docs/PHASE_1_PLAN.md 第 5 节游戏规则。
- docs/claude-tasks/TASK-003A-R1-review-fixes.md，特别是最终复验与失败案例。
- 当前 domain/combat.py、api/routes.py、main.py、repositories 和 testing 包。

当前基础：固定 8 场景、独立判定器、JSON 证据和实际动作前缀重跑；报告 schema=1.1、rules=1.1.0。上一轮 171 项非 PostgreSQL 测试通过，不作为本轮自动通过的依据。

## 2. 目标和明确不做

让三个有标准答案的游戏缺陷真实存在于独立靶场中，经公开 HTTP 动作触发，再由现有执行器发现、记录和复现。形成可供未来 Agent 对比的固定脚本评测基线。

交付：独立内存靶场、三个单缺陷变体、评测清单、批量评测命令、正常对照、逐项原始证据和指标摘要。

不接入 LLM、LangGraph、MCP、RAG、前端、视觉测试、多 Agent、并发评测、新数据库表或迁移。不得改写 HTTP 响应假装产生缺陷，不用 MockTransport 代替真实靶场；响应注入只能用于执行器异常路径测试。

## 3. 隔离与最小接入方案

本任务允许对共享领域层和路由做以下小范围改动，因此必须重新验证数据库回归。正常规则、公开 HTTP 路径与响应结构、数据库模型和恢复策略保持原契约。

### 3.1 三个窄规则扩展点

在 CombatSession 中提取三个受保护方法（允许等价命名）：

- 计算药水实际治疗量，默认仍取 min(25, 缺失 HP)。
- 计算喝药后的药水数量，默认减一。
- 判断攻击后是否反击，默认只在敌人存活时反击。

正常实现仍在领域层，完整动作流程、随机数推进、事件生成与快照只维护一份。独立 lab 子类仅覆盖这些小方法，不复制整份 CombatSession，不 monkeypatch 全局方法，不从外部改私有 HP 或历史。

攻击后先按原规则判定敌人死亡，再经反击扩展点决定是否反击；normal 分支的事件、状态与随机数调用顺序必须与修改前完全一致。提取扩展点不引入规则注册框架、通用插件系统或任意代码加载。

### 3.2 会话创建依赖与独立应用

在创建会话的路由提取可注入的 session factory 依赖，默认返回原 create_combat_session。正常 main.py 继续使用默认依赖；profile 不进入正常 Settings、环境开关或 HTTP 请求。

新增 gamepilot.lab 应用工厂，独立组装 FastAPI、既有路由和异常处理，显式创建新的 InMemorySessionRepository，并仅在本应用实例覆盖 session factory。不要导入 main.py 的模块级 app，避免导入时读取正常数据库配置或创建 Engine。

lab 工厂只接收合法 profile，不接受外部仓储、数据库连接串或可调用代码路径；固定使用内存，不读取 .env 来选择仓储。即使正常环境设置 REPOSITORY_BACKEND=postgres 或 DATABASE_URL 无效，lab 也不创建数据库 Engine。正常服务的既有配置行为不受影响。

每个 lab 应用实例固定一种 profile，所有新会话继承该 profile，运行中不能切换或叠加缺陷。非法 profile 明确报错，不退回 normal。profile 仅由靶场启动者选择，原有游戏 API 不暴露标签或新增切换端点。

lab 可依赖领域模块；gamepilot.testing 保持独立性，不得导入 lab、domain、repositories 或 benchmark 的标准答案。

## 4. 三个缺陷与公开触发轨迹

以下是必须实现的有限集合，不扩展缺陷数量。每次只启用一个缺陷。

| profile | 实际状态变化 | 指定触发轨迹 | 必须命中的规则 |
| --- | --- | --- | --- |
| normal | 原始正确行为 | baseline 全部场景 | 全部 pass |
| potion_overheal | 喝药始终治疗 25，不按最大 HP 截断；药水事件与真实 HP 一致 | seed=42，attack → use_potion，第 2 步药水事件 HP=105，随后反击 | R-POTION-CAP |
| potion_not_consumed | 成功喝药仍正确治疗并反击，但事件和实际库存均不扣药 | seed=42，attack → use_potion，第 2 步库存仍为 2 | R-POTION-DECREMENTS |
| retaliate_after_death | 致死攻击后仍执行一次真实反击，产生事件并扣玩家 HP | seed=42，attack × 3，第 3 步敌人 HP=0 后仍反击 | R-NO-RETALIATE-ON-KILL |

治疗超限的错误可能只在中间事件可见，最终 HP 经过反击后可能已低于 100；这正是本缺陷的价值。不能只改事件而保持实际状态正常，也不能只改 API 序列化结果。

为证明缺陷实际触发，lab 维护只对宿主可见的最小触发记录（按 session_id 和 fault_id 标识即可）。记录只在行为真正偏离 normal 时产生：overheal 要求缺失 HP 小于 25，死亡反击要求敌人已经死亡。正常分支不记录命中。

触发记录不进入游戏响应、GameClient 或判定器。它用于评测器核对标准答案是否成立，不能代替 oracle 判定。GET 必须能读到真实已变更的状态与历史；后续合法动作从该真实状态继续，而非返回预写结果。

## 5. 固定评测矩阵

新增独立 gamepilot.benchmark（或同职责小包），可以组装 lab 并调用 testing 公共接口。评测器负责标准答案和评分，执行器/判定器不获得 profile 或预期缺陷标签。

复用 003A 的全部 8 个 baseline 场景，在 normal 和三个 profile 上各执行一次，共 32 个组合。每个场景新建会话，profile 使用独立应用实例与仓储；现有场景和动作预期不能因为开启缺陷而改成错误规则。

建议独立、可版本化的固定清单，保存 profile、case_id、预期是否触发、目标规则集合和对应缺陷编号。清单不在运行后根据 oracle 输出反向生成。下面是根据当前规则得到的矩阵预期，实施时须逐格验证；若不成立，报告差异，不通过修改目标答案掩盖实现问题。

| case_id | normal | potion_overheal | potion_not_consumed | retaliate_after_death |
| --- | --- | --- | --- | --- |
| initial-state | pass | pass | pass | pass |
| potion-at-full-hp-rejected | pass | pass | pass | pass |
| attack-then-potion | pass | 目标 fail | 目标 fail | pass |
| potion-exhaustion | pass | 目标 fail | 目标 fail | pass |
| win-then-rejected | pass | pass | pass | 目标 fail |
| lose-then-rejected | pass | pass | pass | pass |
| rejected-action-keeps-rng | pass | 目标 fail | 目标 fail | pass |
| reproducible-duplicate-run | pass | 目标 fail | 目标 fail | pass |

预计 9 个目标失败组合、23 个通过组合；正常服务 8 个通过组合属于其中一部分。每个原始 RunReport 保留真实 pass/fail/error，不把目标失败重写成 pass；评测摘要另存“是否达到评测预期”。

不能只查看 CaseFailure.rule_id：执行器可能先记录 R-EVENT-COUNT 或 R-POTION-HEAL。目标检测需要从该场景所有 status=fail 的 RuleCheck 中确认指定规则命中，并去重；同一缺陷命中多条规则不能多算发现数量。

## 6. 指标口径与重跑

至少输出原始计数、分母和比例：

1. 缺陷覆盖：三个指定触发场景中，实际触发且命中目标规则的不同缺陷数 / 3，目标 3/3。
2. 触发正确率：目标失败组合中，lab 记录真实触发的组合数 / 9，目标 9/9。
3. 目标检测率：目标失败组合中，触发成立且 oracle 命中指定规则的组合数 / 9，目标 9/9。另列漏检和非目标规则失败，不能用任意 fail 代替指定缺陷。
4. 正常对照误报：normal 上 fail 的场景数 / 8，目标 0/8；error 单列且使评测失败，不能因未执行而降低分母。
5. 未触发变体误报：三个变体的预期 pass 组合上 fail 数 / 15，目标 0/15；意外触发也算矩阵不符。
6. 执行错误数：32 个组合中的 error 数，目标 0，不从分母删除或记为发现缺陷。
7. 重跑一致率：32 个已安排组合中，通过既有 replay 在同一 profile 的新会话复现的组合数 / 32，目标 32/32；失败/不可比较不从分母剔除。另列 9 个缺陷组合的复现情况，目标 9/9。

对照 profile 改为 normal 重放一个缺陷轨迹时，应该 mismatch，不能因为动作相同就声称缺陷已复现。增加一次这种负向验证，不计入上述同 profile 的 32 次重跑。

批量评测使用真实 FastAPI 应用和 ASGITransport，进程内顺序执行即可；normal 与变体必须走同一 GameClient、runner、oracle、replay。禁止直接调用领域方法生成评测结果，禁止重写已有 oracle 来迎合缺陷变体。

32 个组合的每个原始报告和重跑报告都要可追踪。评测摘要保存 benchmark 版本、源码版本（可用时）、profile 映射、case_id、原始 run_id、相对报告路径、触发证据、实际失败规则、指标和运行时环境。

优先新增独立 BenchmarkReport，保持已有 RunReport/ReplayReport 的 schema=1.1 契约不变；profile 映射放评测摘要，不把标准答案塞进执行器报告。评测摘要用于审计，不作为可以自动启动任意目标或执行命令的配置。

## 7. 命令行与演示

建议命令形态（允许等价实现，需 README 提供完整 PowerShell 示例）：

```powershell
# 显式启动一个独立内存靶场，默认仅监听 127.0.0.1
.venv\Scripts\python.exe -m gamepilot.lab serve --profile potion_overheal --port 8765

# 另一个终端：既有执行器对靶场测试，应报告游戏规则失败，exit 1
.venv\Scripts\python.exe -m gamepilot.testing run --base-url http://127.0.0.1:8765 --suite baseline --output-dir artifacts/fault-runs

# 自动组装四个隔离应用，运行固定矩阵和同 profile 重跑，不依赖手动开启的服务
.venv\Scripts\python.exe -m gamepilot.benchmark run --output-dir artifacts/benchmarks
```

benchmark 退出码单独说明：0 表示全部符合评测预期（包含成功发现预植入缺陷）；1 表示漏检、误报、触发不符或重跑差异；2 表示输入/配置、执行或报告 I/O 错误，2 优先。不能简单透传 testing.run 的 1 当作整个评测失败。

所有报告使用生成的标识和排他写入，保留旧产物；不要自动清理会话、开发数据库或磁盘目录。用于验证的服务必须有明确关闭路径，不遗留进程。

## 8. 测试与验收

1. 三个变体分别从公开 HTTP 动作真实触发，目标规则、内部触发记录、事件、GET 状态一致；无触发条件时与 normal 相同。
2. 验证 normal 与变体应用同时存在互不影响，相同 seed 与 profile 的新会话可复现；应用之间的 session_id 不串用。
3. 验证默认正常入口没有 profile HTTP 参数/环境开关，lab 不创建数据库 Engine，非法 profile 拒绝启动。
4. 提取扩展点前后的 normal 固定轨迹完全一致，覆盖胜负、药水、非法动作和随机进度；不能只将新的实现与自身重跑比较后声称与旧版本一致。
5. 矩阵 32 个组合逐项符合预期，同 profile 的 32 次 replay 一致；负向 profile 重跑不一致。
6. 指标计算测试覆盖漏检、非目标 fail、误报、执行错误、规则重复命中、not_comparable、报告写入失败，以及 error 主导退出码。
7. 对四个 profile 都完成真实 localhost HTTP 的 CLI 验证，至少保留一个真实缺陷报告及其同 profile replay 报告；不能只有 ASGI 测试。
8. 完整非 PostgreSQL 回归、真实 PostgreSQL 回归、Ruff 检查和格式检查通过；新增测试数不预先固定，不把上轮 171 当作本轮结果。

验证命令：

```powershell
$testTemp = Join-Path (Get-Location) ('artifacts/test-003b-' + [guid]::NewGuid().ToString('N'))
.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider -m 'not postgres' --basetemp $testTemp
# 按 README 准备并显式指定独立 TEST_DATABASE_URL，再运行：
.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider -m postgres
.venv\Scripts\ruff.exe check .
.venv\Scripts\ruff.exe format --check .
```

数据库测试只使用既有专用测试库，不降级共享库、不清空开发库、不执行 docker compose down -v。环境不可用时明确报告未验证，不能写成验收通过。本任务预计不需要数据库迁移。

## 9. 交付与学习复盘

交付修改文件清单、三个扩展点说明、缺陷与标准答案清单、逐格矩阵结果、所有指标分子/分母、真实 HTTP 示例、报告路径、验证结果和限制。更新 README、AGENTS.md 和已确认的设计决策，独立审查前标记“实施完成，待 Codex 验收”。

不删除、覆盖或回滚已有工作；不批量重构，不提交、推送或发布。若实现必须偏离这份方案，先说明具体阻碍与最小替代方案，不自行扩大范围。

学习目标：解释缺陷触发与缺陷检出为什么不同、真实状态变异与伪造响应的区别、为什么相同 oracle 必须同时测试正常与缺陷版本、为什么缺陷 run 返回 1 而 benchmark 可以返回 0、为什么这只是固定脚本基线而非 Agent 自主发现能力。
