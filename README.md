# GamePilot

面向游戏测试开发场景的**确定性回合制战斗靶场**。GamePilot 的长期目标是让 Agent
理解游戏规则、规划测试路径、通过工具操作可控的游戏测试环境并生成缺陷报告；
当前已交付一个规则明确、可通过 HTTP 操作、可确定性回放的
「玩家 vs 史莱姆」回合制战斗核心，并把战斗状态与事件持久化到 PostgreSQL，
应用重启后可以继续同一场确定性战斗，作为后续测试 Agent 的稳定基础。

## 当前已实现

- 玩家（HP 100/100，2 瓶药水，攻击 18~25）对战史莱姆（HP 60/60，攻击 20~35）的回合制战斗
- `attack` 与 `use_potion` 两种动作，含反击、胜负判定与回合计数
- 固定随机种子：相同 seed + 相同动作序列 ⇒ 完全相同的伤害、事件与最终状态
- 结构化战斗事件（回合、行动方、动作类型、数值、动作后双方 HP、剩余药水）
- FastAPI 接口：创建会话、查询状态、执行动作、健康检查（含 `/docs` 交互式文档）
- 内存仓储（默认）与 PostgreSQL 仓储，二者实现同一仓储协议，可由配置切换
- PostgreSQL 16 表结构、SQLAlchemy ORM 映射、Alembic 迁移与 Docker Compose 开发服务
- 持久化数据可确定性恢复：读取时按事件历史重放校验，损坏会显式报错而不是给出猜测结果
- 确定性测试执行器 `gamepilot.testing`：固定场景 + 独立规则判定 + 可重跑的 JSON 证据报告
- 可控缺陷靶场 `gamepilot.lab`：三个真实状态缺陷、独立内存入口，正常服务与公开 API 不受影响
- 固定评测集 `gamepilot.benchmark`：4 个 profile × 8 个基线场景 = 32 个组合、同 profile 重跑、
  负向验证与 7 项带分子/分母的指标，退出码区分「符合预期 / 存在偏差 / 执行错误」
- 完整的领域单元测试、API 集成测试与真实 PostgreSQL 集成测试

## 当前未实现（属于后续阶段）

Agent 接入（LLM/规划）、应用容器化与线上部署、前端与视觉测试、MCP。
这些不会提前引入。

## 环境要求

- Python 3.12+（已在 3.14.6 上验证）
- Docker（第二阶段用于启动 PostgreSQL 16，见「数据库基础」一节）

## 安装

在项目根目录执行（PowerShell）：

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

## 启动服务

```powershell
python -m uvicorn gamepilot.main:app --reload
```

启动后访问：

- 交互式文档：http://127.0.0.1:8000/docs
- 健康检查：http://127.0.0.1:8000/health

## 运行测试与检查

默认只跑不依赖数据库的单元测试与 API 测试：

```powershell
python -m pytest -q -p no:cacheprovider -m "not postgres"
python -m ruff check .
python -m ruff format --check .
```

真实 PostgreSQL 集成测试需要显式提供专用测试库，见
「[PostgreSQL 集成测试](#postgresql-集成测试)」。

## 最小调用示例

以下命令适用于 Git Bash 或 cmd（Windows PowerShell 中 `curl` 是
Invoke-WebRequest 的别名，请改用 `curl.exe` 或直接使用 Invoke-RestMethod）：

```bash
# 健康检查
curl -s http://127.0.0.1:8000/health

# 创建一场固定种子的战斗（seed 可省略，服务会生成并返回）
curl -s -X POST http://127.0.0.1:8000/api/v1/game-sessions \
  -H "Content-Type: application/json" -d '{"seed": 42}'

# 执行攻击（把 <SESSION_ID> 换成上一步响应中的 session_id）
curl -s -X POST http://127.0.0.1:8000/api/v1/game-sessions/<SESSION_ID>/actions \
  -H "Content-Type: application/json" -d '{"action": "attack"}'

# 使用药水
curl -s -X POST http://127.0.0.1:8000/api/v1/game-sessions/<SESSION_ID>/actions \
  -H "Content-Type: application/json" -d '{"action": "use_potion"}'

# 查询当前完整状态（含事件历史）
curl -s http://127.0.0.1:8000/api/v1/game-sessions/<SESSION_ID>
```

## 确定性测试执行器（`gamepilot.testing`）

服务启动后，一条本地命令即可：按固定场景顺序发动作请求、用独立判定器核对规则、
输出可重跑的 JSON 证据报告。

```powershell
python -m gamepilot.testing run --base-url http://127.0.0.1:8000 --suite baseline --output-dir artifacts/test-runs
python -m gamepilot.testing replay --base-url http://127.0.0.1:8000 --report artifacts/test-runs/<报告文件>.json --output-dir artifacts/test-runs
```

- 内置 `baseline` 套件共 8 个场景：创建并查询初始状态、满血喝药被拒、攻击后喝药、
  药水耗尽（第三次 `no_potions`）、获胜/阵亡后动作被拒，以及两个对照场景
  （被拒绝的动作不影响后续随机结果、同 seed 同动作完全可复现）。
- 判定器只读请求、预期、快照与事件：不调用 `CombatSession`、不读仓储或数据库；
  规则数值独立来自 `docs/PHASE_1_PLAN.md` 第 5 节，不从被测实现导入常量。
- 结果分三类：`pass` / `fail`（规则或接口预期不符，带稳定 `rule_id` 与步骤位置）/
  `error`（连不上、超时、5xx、响应不可解析或执行故障）。基础设施故障不会被写成游戏缺陷。
- 每条主轨迹及对照轨迹最多 20 次动作尝试（被拒绝的动作也计数），达到上限记为 `error`；
  首个 `fail`/`error` 后停止该场景，其余场景继续执行；预期 409 之后必须再用 GET
  证明状态与完整事件未变。
- 报告文件名由生成的 `run_id` 决定，已存在的报告不会被覆盖；报告不含环境变量、
  凭据或完整请求头，写入的地址已去除凭据。
- `replay` 校验 seed、计划和实际执行前缀，在新会话中只重跑实际发生的动作尝试，
  包括被拒绝的动作；不会执行原运行早停后剩余的计划或尚未开始的对照会话。
  新执行的完整证据保存在重跑报告的 `cases[].execution`。
- 同一会话内严格检查 `session_id`、请求 seed 和 GET 状态码；跨会话比较只规范化
  与各自会话身份匹配的 ID，其他业务字段参与比较。目标地址只取本次命令的 `--base-url`。
- 报告格式为 `schema_version=1.1`，判定规则为 `rules_version=1.1.0`。
  读取报告时拒绝缺失字段、嵌套未知字段、错误类型及自相矛盾的轨迹，
  不自动补齐旧证据。旧版 1.0 报告需要重新运行生成，原文件保留，不原地迁移。
- 动作解析失败、核对 GET 失败等场景仍保留动作响应、GET 观测和最后已知快照。
  存储冲突（如 `409 persistence_conflict`）属于执行错误。错误运行可重新检查，
  但 replay 标记为 `not_comparable`，不声称复现了原来的网络或服务故障。
- 退出码：`run` 的 `0` 表示测试通过；`replay` 的 `0` 表示复现一致，可能一致地复现缺陷，
  不能单独作为游戏质量通过的门禁。`1` 表示规则失败或重跑差异，`2` 表示输入或执行错误
  （包括报告写入失败；同时出现时以 2 为准）。报告使用排他创建，不覆盖已有文件。

## 缺陷靶场与固定评测集（`gamepilot.lab` / `gamepilot.benchmark`）

执行器回答「实现是否符合规则」；这一节回答「执行器能不能稳定地发现缺陷」。
做法是先植入三个**真实改变游戏状态**的缺陷，再用同一套执行器、判定器和重跑去测：
如果判定器看不出植入的缺陷，评测就失败。

三个缺陷都只在独立靶场里生效，正常服务的战斗规则、公开 API、仓储与数据库结构均未改动：

| profile | 植入的缺陷 | 真实状态变异 | 目标规则 |
| --- | --- | --- | --- |
| `normal` | 无（与正常实现逐字段一致） | — | — |
| `potion_overheal` | 喝药固定治疗 25 点，不按缺失生命值截断 | 生命值可以越过上限，后续反击从越界值继续扣 | `R-POTION-CAP` |
| `potion_not_consumed` | 喝药后药水数量不减少 | 药水可以反复使用，数量始终不变 | `R-POTION-DECREMENTS` |
| `retaliate_after_death` | 击杀敌人后仍执行一次反击 | 敌人已死亡却真实造成伤害 | `R-NO-RETALIATE-ON-KILL` |

靶场是独立的内存入口：不导入 `main.py`、不读 `.env`/Settings、只使用内存仓储，
即使 `REPOSITORY_BACKEND=postgres` 也不会创建数据库 Engine；非法 profile 直接拒绝启动；
游戏 API 上不存在任何读取或切换 profile 的参数、字段或环境开关。

### 手动演示（PowerShell）

```powershell
# 1. 启动一个独立内存靶场（默认只监听 127.0.0.1:8765，Ctrl+C 关闭）
.venv\Scripts\python.exe -m gamepilot.lab serve --profile potion_overheal --port 8765

# 2. 另一个终端：用既有执行器跑基线套件，缺陷被检出 -> exit 1
.venv\Scripts\python.exe -m gamepilot.testing run --base-url http://127.0.0.1:8765 `
  --suite baseline --output-dir artifacts/fault-runs

# 3. 同 profile 重跑：缺陷稳定复现，逐字段一致 -> exit 0
.venv\Scripts\python.exe -m gamepilot.testing replay --base-url http://127.0.0.1:8765 `
  --report artifacts/fault-runs/<报告文件>.json --output-dir artifacts/fault-runs

# 4. 批量评测：自动组装四个隔离应用（不依赖手动开启的服务）
.venv\Scripts\python.exe -m gamepilot.benchmark run --output-dir artifacts/benchmarks
```

`gamepilot.lab serve` 只监听本机、随终端关闭而退出，不启动也不清理任何数据库或会话。
把第 1 步的 profile 换成另外三个可以逐个复现；`normal` 上同一套件应当 exit 0。

### 评测口径

- **矩阵**：4 个 profile × 8 个基线场景 = 32 个组合，先于运行写死在
  `gamepilot/benchmark/manifest.py` 里（含版本号）。预期结果 9 个 fail
  （`potion_overheal` 4 个、`potion_not_consumed` 4 个、`retaliate_after_death` 1 个）与 23 个 pass。
- **实际触发与预期分离**：靶场在**真的发生偏离**时按 `session_id + fault_id` 记录一条最小触发记录
  （缺失值等于治疗量时不记录，因为那种情况下行为与正常实现完全一致）。
  「触发了」和「判定器命中了目标规则」是两件事，分别记入不同指标。
- **同一次运行内完成**：32 个组合各自同 profile 重跑一次（共 32 次），重跑走的是既有
  `replay`；另有 1 次负向验证——把 `potion_overheal × attack-then-potion` 的缺陷轨迹放到
  `normal` 上重放，动作序列相同但结论必须是 `mismatch`，否则说明判定器分辨不出缺陷。
  这 1 次不计入 32 次重跑。
- **指标**（分子/分母都写进报告，分母来自固定清单，没跑到不会让分母变小）：

  | 指标 | 分子 / 分母 | 目标 |
  | --- | --- | --- |
  | `defect_coverage` | 有指定触发轨迹的缺陷数 / 3 | 3/3 |
  | `trigger_accuracy` | 预期失败组合中靶场真实触发数 / 9 | 9/9 |
  | `target_detection` | 命中目标规则的预期失败组合数 / 9（同一缺陷命中多条规则只计一次） | 9/9 |
  | `normal_false_positive` | normal 上被判失败的场景数 / 8 | 0/8 |
  | `variant_false_positive` | 预期通过的变体组合中失败或意外触发数 / 15 | 0/15 |
  | `execution_errors` | 原始运行的执行错误组合数 / 32（不含额外重放，不记为发现缺陷） | 0/32 |
  | `replay_consistency` | 重跑一致的组合数 / 32（另列 9/9 缺陷组合复现） | 32/32 |

- **判定器不读标准答案**：目标规则只用于评测摘要里的核对与统计；执行器报告里只有
  HTTP 观测、场景结论与占位地址，不含 profile、缺陷标签或触发说明。
- **退出码**：`0` 表示 32 个组合全部符合清单（**包含成功发现预植入缺陷**——这是预期结果）；
  `1` 表示漏检、误报、触发不符或重跑差异；`2` 表示输入/配置、执行或报告 I/O 错误，
  与其他情况同时出现时以 `2` 为准。评测自身跑不出结论时绝不返回 `0`。
- **分阶段错误证据**（benchmark 1.1.0）：组合证据保留 `case_status/case_error` 和
  `replay_status/replay_error`，负向验证保留 `execution_status/execution_error`，
  各自关联原始报告路径。摘要中的 `execution_errors` 只统计 32 次原始运行，
  `replay_execution_errors` 统计 32 次同 profile 重放，`negative_execution_errors`
  统计 1 次负向验证；三个阶段任何一个执行失败，总退出码都为 `2`。
  原运行 error 而新执行成功时，replay 虽为 `not_comparable`，也不重复计为重放执行错误。
  同 profile 真正的 `mismatch` 和负向验证意外 `match` 仍退出 `1`。
  原始 `RunReport/ReplayReport` 的 schema 保持 1.1，原有七项指标及分母不变。
- **报告布局**：`<output-dir>/<run_id>/benchmark.json` 为评测摘要，
  `runs/<profile>/<case_id>/<run_id>.json` 与 `replays/<profile>/<case_id>/<run_id>.json`
  为原始运行与重跑报告，`replays/negative/<case_id>/<run_id>.json` 为负向验证报告。
  全部排他创建，不覆盖已有产物，也不会自动清理任何目录或会话。
- 这套评测是**固定脚本基线**：它证明「缺陷可被稳定检出、指标可复算」，
  不代表 Agent 具备自主发现缺陷的能力——自主探索留给后续阶段。

## 数据库与后端选择

战斗数据可以存在进程内存里，也可以存进 PostgreSQL，由配置切换：

| `REPOSITORY_BACKEND` | 行为 | 需要的配置 |
| --- | --- | --- |
| `memory`（默认） | 会话保存在进程内存中，服务重启后丢失 | 无 |
| `postgres` | 状态与事件写入 PostgreSQL，应用重启后可继续同一场战斗 | 必须提供 `DATABASE_URL` |

- 取值非法（例如 `sqlite`）或选择 `postgres` 却没有 `DATABASE_URL` 时，
  应用直接报配置错误，**不会静默退回内存仓储**；
- `DATABASE_URL` 必须是 `postgresql+psycopg://`（同步 psycopg 3），
  本阶段不使用异步驱动；
- 选择内存后端时导入配置、领域与数据库模块都不会连接数据库。

启动前先准备好本地配置（已存在的 `.env` 请自行保留，不要被示例覆盖）：

```powershell
Copy-Item .env.example .env      # 首次使用时创建本地配置
# 选 postgres 后端时把 .env 里的 REPOSITORY_BACKEND 改成 postgres
```

### 启动数据库

```powershell
& "$env:USERPROFILE\bin\docker.cmd" compose config --quiet
& "$env:USERPROFILE\bin\docker.cmd" compose up -d db
& "$env:USERPROFILE\bin\docker.cmd" compose ps     # 等待 STATUS 显示 (healthy)
```

- 本机 Docker 跑在 WSL Ubuntu-24.04 中，`docker.cmd` 是 Windows 侧的桥接器；
  旧终端 PATH 未刷新时就用上面这种写法。不需要 Docker Desktop。
- Windows 上的应用固定连接 `127.0.0.1`（不要写 `localhost`：会先解析到 IPv6 `::1`，
  该地址没有监听时会先耗到超时才回退到 IPv4）。
- 数据库名、用户、密码与端口来自 `.env`，未提供时使用 `.env.example` 中的本地开发默认值。
  数据保存在命名卷 `gamepilot-pgdata` 中，容器重建后仍然保留。

### 应用迁移（必须显式执行）

```powershell
$env:DATABASE_URL = "postgresql+psycopg://gamepilot:gamepilot@127.0.0.1:5432/gamepilot"
.venv\Scripts\alembic.exe upgrade head
.venv\Scripts\alembic.exe current     # 应显示 0002_session_seed_text (head)
.venv\Scripts\alembic.exe check       # 应显示 No new upgrade operations detected.
```

- 迁移**不会**在应用启动时自动运行，也不使用 `create_all`；
  启动探测只执行数据库连通性检查，不检查业务表版本。必须先显式迁移，
  否则访问业务接口时会返回内部错误；
- 迁移只从环境变量 `DATABASE_URL`（或本地 `.env`）读取连接串，
  `alembic.ini` 中不保存任何凭据；缺少可用 URL 时 Alembic 直接报错；
- `game_sessions.seed` 在 0002 迁移后是**文本列**：领域层与 API 的种子是无界整数，
  超 BIGINT（例如 `2 ** 80`）也能无损保存；
  从 0002 降级回 BIGINT 只在所有存量种子都放得下时成功，
  否则显式失败并保留数据与迁移版本不变（不截断、不取模、不删行）。

### 应用重启后继续战斗

```powershell
$env:REPOSITORY_BACKEND = "postgres"
$env:DATABASE_URL = "postgresql+psycopg://gamepilot:gamepilot@127.0.0.1:5432/gamepilot"
.venv\Scripts\python.exe -m uvicorn gamepilot.main:app
```

用同一个 `session_id` 重新查询即可继续：读取时会用**事件历史重放**校验状态，
确认「种子 + 玩家动作序列」能唯一重建出已保存的状态与事件，
然后才返回一个可以继续动作的会话（史莱姆的反击由领域规则重新生成，不重放）。

- 校验不通过（事件被篡改、序号断裂、状态行与历史不符）时返回
  `500 / persistence_inconsistent`，**不会自动修复、跳过或猜测**；
- 恢复依赖当前战斗规则与兼容的 Python 运行时（随机数实现）。
  规则变更后若旧存档不兼容，应保留原数据并明确报告，再单独设计存档版本和迁移策略。

### 单写入方边界

同一场战斗请按「单写入方、顺序操作」使用：读取 → 动作 → 保存。

- 保存是原子的：状态行与新事件在同一个事务里一起提交、一起回滚，
  重复保存同一快照不会产生重复事件；
- 写入前会对状态行加锁并校验「已存事件是待保存历史的完整前缀」，
  缩短历史或分叉的过期副本会被拒绝（`409 / persistence_conflict`），
  不会静默覆盖已保存历史；
- 这一阶段不提供分布式请求幂等，也不支持多写入方并发推进同一场战斗。

### PostgreSQL 集成测试

集成测试只使用**专用测试库** `gamepilot_test`，绝不回退到开发库 `DATABASE_URL`：

```powershell
# 1. 创建专用测试库（只创建一次；绝不要指向开发库）
& "$env:USERPROFILE\bin\docker.cmd" compose exec db psql -U gamepilot -d postgres `
  -c "CREATE DATABASE gamepilot_test"

# 2. 用测试库 URL 显式执行迁移
$env:TEST_DATABASE_URL = "postgresql+psycopg://gamepilot:gamepilot@127.0.0.1:5432/gamepilot_test"
$previousDatabaseUrl = $env:DATABASE_URL
$env:DATABASE_URL = $env:TEST_DATABASE_URL
.venv\Scripts\alembic.exe upgrade head

# 3. 运行真实数据库集成测试（只认 TEST_DATABASE_URL，与 DATABASE_URL 指向哪里无关）
.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider -m postgres

# 4. 恢复开发库配置，避免同一终端后续误连测试库
$env:DATABASE_URL = $previousDatabaseUrl
```

- 未设置 `TEST_DATABASE_URL` 时，`postgres` 标记的用例会带原因跳过；
  已设置但**连不上或未迁移**时是失败而不是跳过——集成测试全跳过不算通过；
- 测试直接从进程环境变量读取 `TEST_DATABASE_URL`（pytest 不读 `.env`），
  并且会校验目标库名确实是 `gamepilot_test`；
- 清理只删除测试自己创建的那些会话 id（事件行由外键级联删除），
  不使用 `TRUNCATE`、不 drop 整库、不触碰开发库或数据卷；
  迁移降级用例在另行创建的、名字唯一的临时库上验证，不会降级任何共享库；
- `tests/unit` 与 `tests/integration/test_api.py` 不需要 Docker，随时可跑。

### 停止与清理

```powershell
docker compose down     # 停止容器，保留数据卷
```

> 不要执行 `docker compose down -v`：它会删除命名卷 `gamepilot-pgdata`，数据不可恢复。

## 架构说明

```text
API 层（FastAPI 路由 + Pydantic 请求 Schema）
  │  只做参数校验与调用，不含任何游戏规则，也不写 SQL
  ▼
领域层（CombatSession：状态转换、伤害计算、随机、结构化事件）
  │  不依赖 FastAPI、SQLAlchemy 或配置，可直接单元测试
  ▼
仓储协议（SessionRepository：save / get）
  ├─ InMemorySessionRepository     默认，进程内内存
  └─ PostgresSessionRepository     同步 SQLAlchemy 2.x + psycopg 3
         │  事务边界、状态行锁、事件前缀校验
         ▼
      恢复模块 persistence/recovery.py（纯函数：重放校验、前缀校验，无 I/O）
         ▼
      ORM 映射 persistence/models.py ──▶ game_sessions、combat_events
      迁移 alembic/（0001 建表，0002 seed 改文本）
```

- **确定性**：每个会话持有私有的 `random.Random(seed)`，不使用全局随机；
  事件不带时间戳，便于回放比对。恢复时不保存也不还原随机数内部状态，
  而是用同一个 seed 重放玩家动作，让领域层重新生成史莱姆的反击。
- **错误处理**：领域与持久化错误都携带稳定错误码，由 API 统一映射：

  | 情况 | HTTP | 错误码 |
  | --- | --- | --- |
  | 会话不存在 | 404 | `session_not_found` |
  | 动作与当前战斗状态冲突 | 409 | 领域错误码（如 `battle_not_active`） |
  | 已存历史与本次待保存历史冲突 | 409 | `persistence_conflict` |
  | 数据库连接/可用性故障 | 503 | `persistence_unavailable` |
  | 数据无法一致恢复 | 500 | `persistence_inconsistent` |
  | 其他内部错误 | 500 | `internal_error` |

  只有确定的连接/可用性故障才会映射为 503，SQL 编程错误不会被当成临时故障；
  请求校验错误保留 FastAPI 的 HTTP 422；
  错误响应只含错误码与消息，不含堆栈、SQL 或连接串，日志同样不记录凭据；
  写入失败不会自动重试。
- **领域与 ORM 分离**：`persistence/models.py` 只做关系表映射，
  不参与伤害计算、胜负判断或随机逻辑；仓储也不修改领域对象的私有字段。
- **生命周期**：应用自建的 Engine 由 lifespan 管理，关闭时 release；
  外部注入的仓储/Engine 归调用方所有，应用不会释放它。
  连接探测发生在启动阶段，此时不可用会明确失败。
- **测试执行器是外部观察者**：`gamepilot.testing` 把被测服务当成外部 HTTP 服务，
  只通过线上响应取证，不导入 `gamepilot.domain`、仓储、恢复模块或数据库。
  这条边界由 `tests/unit/test_testing_independence.py` 强制检查
  （源码导入的静态检查 + 干净解释器里的 `sys.modules` 运行时检查）。
- **缺陷靶场的接入面很窄**：`gamepilot.lab` 只做三件事——用三个窄扩展点
  （`_heal_amount` / `_potions_after_use` / `_should_retaliate`）派生一个缺陷会话子类、
  给路由注入一个自己的会话工厂、用独立的内存应用组装靶场。
  它不复制战斗引擎，也不改写任何 HTTP 响应；靶场应用与正常应用走同一份路由与异常处理，
  区别只在于新会话由哪个工厂创建。`normal` profile 与正常实现在同一轨迹上逐字段一致。
- **评测与执行器解耦**：`gamepilot.benchmark` 只编排「跑哪一格、重跑哪一格、怎么核对清单」，
  判定本身仍然由 `gamepilot.testing` 的 runner、oracle 与 replay 完成；
  normal 与变体使用同一个 `GameClient`、runner、oracle 与 replay。

## 后续计划

TASK-002A 完成数据库骨架与迁移，TASK-002B 完成 PostgreSQL 仓储、
事务边界与确定性恢复，TASK-003A 完成确定性测试执行器与规则判定基线，
TASK-003B 完成可控缺陷靶场与固定评测集（实施完成，待 Codex 独立验收）；
下一步是 Agent 工具层：由 Agent 自主探索而不是照脚本执行。
详见 `docs/PHASE_2_PLAN.md`、`docs/PHASE_3_PLAN.md` 与 `AGENTS.md`。

## 已知限制

- 默认内存后端下战斗数据仍在进程内存中，服务重启后丢失；
  需要跨重启保留时须显式选择 `postgres` 后端；
- 同一场战斗按单写入方顺序操作使用，不提供多写入方并发与分布式请求幂等；
- 确定性恢复依赖当前战斗规则与兼容的 Python 运行时，
  规则变更后旧存档不承诺继续兼容；
- 目前只有玩家与史莱姆一种战斗，没有商店、背包、存档等内容；
- 没有删除会话的 API，也没有数据保留/清理策略；
- 测试执行器顺序执行、单进程，不做并发，也不自动重试动作；
  超时后结果未知，重跑只重新检查而不承诺复现原错误；
- 缺陷靶场与固定评测集同样是单进程顺序执行，不做并发评测；profile 由启动参数决定，
  不存在运行期开关（这是刻意的隔离边界，不是待补的开关）；
- 评测报告里的地址是进程内 ASGI 应用的占位地址（`benchmark.invalid`），
  只用于说明「不是真实网络请求」；真实 localhost HTTP 的验证由 `gamepilot.lab serve`
  加 `gamepilot.testing run/replay` 手动完成；
- 评测是固定脚本基线，不是 Agent 自主发现能力；
- 尚未接入 LLM/规划、MCP，也没有缺陷开关与轨迹自动最小化（属于后续任务）。
