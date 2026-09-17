# TASK-002A：建立 PostgreSQL 数据库骨架与 Alembic 迁移

## 1. 任务定位

本任务属于 GamePilot 第二阶段“PostgreSQL 持久化基础”的第一个实施任务。

开始前必须完整阅读：

1. `AGENTS.md`；
2. `docs/PHASE_2_PLAN.md`；
3. `src/gamepilot/domain/`；
4. `src/gamepilot/repositories/`；
5. 当前 `pyproject.toml`、README 和测试。

本轮只建立数据库配置、SQLAlchemy ORM 表、Alembic 迁移和 PostgreSQL 开发容器。不得在本任务中实现 PostgreSQL 仓储，也不得改变当前 API 的运行行为。

## 2. 目标

完成后，开发者应能：

1. 通过 Docker Compose 启动一个健康的 PostgreSQL 16 服务；
2. 通过环境变量配置数据库连接；
3. 使用 Alembic 从空数据库创建 `game_sessions` 和 `combat_events`；
4. 使用 SQLAlchemy Inspector 或等价只读方式确认表和约束存在；
5. 继续使用原有内存仓储启动 FastAPI，并通过原有全部测试。

## 3. 技术约束

使用：

- PostgreSQL 16；
- SQLAlchemy 2.x 同步 ORM；
- Psycopg 3；
- Alembic；
- Pydantic Settings；
- Docker Compose v2。

不得改用异步 SQLAlchemy、SQLModel、Django ORM 或其他数据库框架。不得引入 Redis、向量数据库或测试容器框架。

## 4. 依赖与配置

### 4.1 Python 依赖

在 `pyproject.toml` 中加入适当版本下限：

- `sqlalchemy`；
- `psycopg[binary]`；
- `alembic`；
- `pydantic-settings`。

更新锁文件，并验证这些依赖与当前 Python 版本兼容。不要无理由升级已有依赖的主版本。

### 4.2 Settings

新增独立配置模块，例如：

```text
src/gamepilot/config.py
```

最低要求：

- 使用 `BaseSettings`；
- 支持从 `DATABASE_URL` 读取 SQLAlchemy 数据库 URL；
- 代码中不得硬编码真实密码；
- 导入配置模块时不得立即连接数据库；
- 不得改变 `create_app()` 当前默认使用内存仓储的行为。

提供 `.env.example`，只包含安全的本地示例值和必要注释，不提交真实 `.env`。

## 5. SQLAlchemy 基础设施

建议结构：

```text
src/gamepilot/persistence/
├─ __init__.py
├─ database.py
└─ models.py
```

允许在保持职责清晰的前提下微调命名。

### 5.1 `database.py`

提供：

- SQLAlchemy Declarative Base；
- 根据显式 URL 创建同步 Engine 的函数；
- 根据 Engine 创建 Session Factory 的函数。

约束：

- 不在模块导入时创建 Engine 或连接数据库；
- 不创建全局长生命周期 ORM Session；
- 不调用 `metadata.create_all()`；
- Engine 和 Session 生命周期应便于后续测试和依赖注入；
- 不在此处加入仓储业务逻辑。

### 5.2 ORM 与领域模型边界

数据库 ORM 类只负责关系表映射，不得替换或继承以下领域/API 模型：

- `CombatSession`；
- `GameSnapshot`；
- FastAPI 请求或响应 Schema。

不得把伤害计算、胜负判断或随机逻辑写进 ORM 类。

## 6. 表结构要求

### 6.1 `game_sessions`

至少包含：

| 字段 | 建议类型 | 约束 |
|---|---|---|
| `session_id` | `String(32)` | 主键 |
| `seed` | `BigInteger` | 非空 |
| `status` | `String` | 非空，限制为 active/won/lost |
| `turn` | `Integer` | 非空且不小于 0 |
| `player_hp` | `Integer` | 非空且不小于 0 |
| `player_max_hp` | `Integer` | 非空且大于 0 |
| `potions` | `Integer` | 非空且不小于 0 |
| `slime_hp` | `Integer` | 非空且不小于 0 |
| `slime_max_hp` | `Integer` | 非空且大于 0 |
| `created_at` | 带时区时间 | 数据库生成，非空 |
| `updated_at` | 带时区时间 | 数据库生成或更新，非空 |

生命值不能超过对应最大生命值。建议通过表级检查约束表达。

### 6.2 `combat_events`

至少包含：

| 字段 | 建议类型 | 约束 |
|---|---|---|
| `id` | `BigInteger` | 数据库生成主键 |
| `session_id` | `String(32)` | 外键、非空、删除会话时级联删除 |
| `sequence` | `Integer` | 非空且大于 0 |
| `turn` | `Integer` | 非空且大于 0 |
| `actor` | `String` | 非空，限制为 player/slime |
| `kind` | `String` | 非空，限制为 attack/potion/retaliate |
| `value` | `Integer` | 非空且不小于 0 |
| `player_hp` | `Integer` | 非空且不小于 0 |
| `slime_hp` | `Integer` | 非空且不小于 0 |
| `potions` | `Integer` | 可空；非空时不得小于 0 |

必须建立：

- `(session_id, sequence)` 唯一约束；
- 支持按 `session_id` 和 `sequence` 顺序读取事件的索引策略；
- 指向 `game_sessions.session_id` 的外键。

不要使用 PostgreSQL 原生 ENUM；本阶段使用字符串和检查约束，降低后续枚举迁移成本。

## 7. Alembic

初始化 Alembic，并满足：

- `target_metadata` 指向项目 ORM metadata；
- 数据库 URL 从项目配置或环境变量读取，不在 `alembic.ini` 中保存密码；
- 创建一份人工检查过的初始迁移；
- `upgrade()` 创建两张表、约束、外键和必要索引；
- `downgrade()` 以正确依赖顺序撤销本迁移；
- 应用启动时不自动运行迁移；
- 不使用 `create_all()` 代替迁移。

迁移文件应保持可读，不保留无关的自动生成内容。

## 8. Docker Compose

在项目根目录添加 `compose.yaml`，只定义 PostgreSQL 服务。

必须具备：

- 固定到 PostgreSQL 16 的镜像标签；
- 数据库名、用户和密码来自环境变量，并提供本地开发默认值或 `.env.example`；
- 命名数据卷；
- `pg_isready` 健康检查；
- 合理的健康检查间隔、超时和重试次数；
- 仅暴露本地开发所需端口。

本任务不得添加 FastAPI 应用服务或 Dockerfile。

不得在验收或清理过程中执行 `docker compose down -v`，除非项目所有者明确同意删除数据库卷。

## 9. 测试与验证

### 9.1 自动化测试

至少增加不依赖真实数据库的测试，验证：

- Settings 能正确读取显式环境变量；
- Engine 创建函数使用传入 URL，且导入模块不会发起连接；
- ORM metadata 包含两张预期表、关键列、主键、外键、唯一约束和检查约束。

不要为了测试而使用 SQLite 冒充 PostgreSQL 集成测试。真实数据库迁移验证通过命令完成；完整 PostgreSQL Repository 集成测试留到 TASK-002B。

### 9.2 必须运行的命令

根据 Windows PowerShell 环境调整可执行文件路径，并报告实际结果：

```powershell
docker compose config
docker compose up -d db
docker compose ps

$env:DATABASE_URL = "postgresql+psycopg://..."
.venv\Scripts\alembic.exe upgrade head
.venv\Scripts\alembic.exe current

.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m ruff format --check .
```

使用 SQLAlchemy Inspector、`psql` 或等价只读检查确认两张表已经由迁移创建。

完成后可以执行 `docker compose down` 停止容器，但不得附加 `-v`。

## 10. 明确禁止

- 不实现 `PostgresSessionRepository`；
- 不修改 `SessionRepository` 协议；
- 不把 FastAPI 默认仓储改成 PostgreSQL；
- 不修改现有 API 路径、请求或响应结构；
- 不修改战斗数值和领域规则；
- 不实现动作重放或随机状态恢复；
- 不使用 Pickle 保存任何对象；
- 不添加 RAG、pgvector、Agent、LangGraph 或 MCP；
- 不添加应用 Dockerfile；
- 不删除或重建已有 Docker 数据卷；
- 不提交、推送或发布 Git 仓库；
- 不进行任务范围外的大规模重构。

## 11. 需要停止并报告的情况

出现以下情况时，不要自行扩大设计，应停止并向项目所有者报告：

- 依赖无法兼容当前 Python 版本；
- 必须改变现有 API 或领域模型才能继续；
- 需要删除数据库卷或其他本地数据；
- 发现当前仓储协议会阻塞本任务规定的数据库骨架；
- Docker 不可用或端口被占用；
- Alembic 无法从环境变量安全取得连接 URL；
- 现有未提交修改与任务文件发生冲突。

## 12. 验收标准

只有同时满足以下条件，TASK-002A 才算完成：

1. 数据库依赖与锁文件已更新；
2. 配置模块不存在导入时连接副作用；
3. ORM 表结构符合任务要求且与领域模型分离；
4. Alembic 能从空 PostgreSQL 数据库执行 `upgrade head`；
5. `alembic current` 显示最新 revision；
6. Docker Compose 中只有 PostgreSQL 服务，且健康检查通过；
7. 两张表及关键约束经只读检查确认存在；
8. 原有 API 默认仍使用内存仓储；
9. 新旧自动化测试全部通过；
10. Ruff 检查和格式检查通过；
11. README 只增加与本任务实际完成能力一致的数据库基础使用说明；
12. 没有提前实现 TASK-002B 或后续阶段内容。

## 13. 完成报告格式

完成后向项目所有者报告：

1. 修改和新增的文件；
2. 最终表结构、约束与索引；
3. Settings、Engine 和 Session Factory 的职责；
4. Alembic revision 标识；
5. Docker Compose 服务与数据卷名称；
6. 实际执行的验证命令及结果；
7. 是否遇到 Python 版本或依赖兼容问题；
8. 未完成事项和已知风险；
9. 明确确认没有实现 PostgreSQL Repository、没有改变 API 默认运行方式、没有删除数据卷。

Claude Code 完成后不要自行开始 TASK-002B，等待 Codex 独立审查。
