# TASK-002B：PostgreSQL 仓储、事务与确定性恢复

## 1. 任务定位

TASK-002A 已通过独立验收，数据库骨架、初始迁移与 PostgreSQL 16 Compose 服务可用，50 个测试通过。本任务完成第二阶段剩余能力：应用显式选择 PostgreSQL 后，战斗及事件真正落库，关闭应用再启动后仍能继续相同的战斗。

编码前完整阅读 `AGENTS.md`、`docs/PHASE_2_PLAN.md`、`docs/DECISIONS.md`、TASK-002A，以及当前领域层、仓储协议、API、配置、ORM、迁移和测试。先检查 Git 状态，保留现有修改。

Claude Code 负责实施，完成后由 Codex 独立审查。不得自行进入下一阶段、提交或推送。

## 2. 范围与兼容性

- 继续使用同步 SQLAlchemy 2.x、Psycopg 3、Alembic、PostgreSQL 16。
- 新增 `PostgresSessionRepository`，保持 `SessionRepository.save(session)` / `get(session_id)` 协议。
- 保留内存仓储，未配置后端时继续使用内存；原有 API 测试无需数据库即可运行。
- 保持 API 路径、请求字段、成功响应、战斗规则和现有 404/409 错误码。
- 允许新增持久化错误类型、HTTP 映射及 OpenAPI 声明，不把数据库故障伪装为会话不存在。
- 领域层继续不依赖 SQLAlchemy、配置或 FastAPI；路由不包含 SQL 或重放逻辑。
- Compose 仍只运行 PostgreSQL；不增加应用 Dockerfile、RAG、Agent、MCP、Redis、异步 ORM、分布式锁或通用工作流框架。

建议新增 `repositories/postgres.py`、仓储错误模块和职责单一的恢复辅助模块；具体拆文件按可读性决定，不建立通用事件溯源框架。

## 3. 种子兼容修正与迁移

现有 API 和领域模型的 seed 是 Python 整数，未限制为有符号 64 位；TASK-002A 的 BIGINT 无法保存例如 `2**80`。这是骨架接入时必须处理的兼容缺口。

采用以下方案，决策背景见 `docs/DECISIONS.md`：

1. ORM 的 `game_sessions.seed` 改为 Text，保存 `str(snapshot.seed)` 的规范十进制表示，读取时还原 int；API 请求与响应仍为整数。
2. 新增独立 0002 迁移，显式转换现有 BIGINT 为文本，保留原值；不得改写已应用的 `0001_initial_schema`。
3. downgrade 转回 BIGINT 只在所有值合法且未越界时成功。越界应明确失败并由事务保留原数据和结构，不得截断、取模或删记录。
4. 更新 metadata 测试，增加负数、0、64 位边界外种子的真实数据库往返与恢复测试，无需突破 Python/Pydantic 自身输入限制。

其余表结构延用 TASK-002A。事件自增 id 是内部标识，不进入 API 或重放比较。

## 4. get：读取与重放恢复

每次读取使用独立 ORM Session，退出时释放。返回独立 CombatSession，不返回 ORM 行或缓存的可变领域对象。

步骤：

1. 查询状态行，不存在则抛原有 SessionNotFoundError。
2. 在同一读取事务中按 sequence 升序查询事件，并保证状态和事件来自一致视图。例如读取状态行时取得共享行锁，写入者按同一状态行串行保存；不能在默认 READ COMMITTED 下无保护地做两次查询后假设视图一致。
3. 校验 sequence 从 1 开始连续，构造 `CombatSession(session_id=stored_id, seed=int(stored_seed))`。
4. 仅从玩家事件提取动作：player/attack 调用 `attack()`，player/potion 调用 `use_potion()`。史莱姆反击由领域逻辑生成，不能重复重放。
5. 比较生成的完整事件列表与数据库事件，覆盖顺序、turn、actor、kind、value、双方 HP、potions（含 NULL）。
6. 比较最终状态与状态行，覆盖 id、seed、status、turn、玩家 HP/最大 HP/药水、史莱姆 HP/最大 HP。
7. 非法种子、无法重放、事件或状态不一致应抛明确的数据一致性错误，停止读取；不能自动修复、跳过事件或返回猜测结果。

禁止 Pickle、保存/恢复 random 内部状态、从仓储修改领域私有字段。数据库时间戳与事件内部 id 不参与确定性比较。

恢复验收必须包含“恢复后再执行动作并与不中断基准比较”，只比较恢复瞬间的快照不足以验证随机数进度。

## 5. save：状态与事件原子写入

仓储接收 Session Factory，不持有跨请求 ORM Session。一次 save 的所有修改共用事务，提交成功才返回；异常时回滚并释放连接。

- 新会话：插入状态及已有全部事件。正常创建时事件为空；也支持保存通过公开动作推进过的合法对象。
- sequence 由快照顺序生成，从 1 开始，不使用数据库全局自增 id 代替会话序号。
- 已有会话：锁定状态行，读取并校验已有状态/事件，确认 seed 相同且已存事件是待保存事件的完整前缀，再更新状态并追加新事件。
- 不删除再重建历史，不覆盖既有事件；重复 save 相同快照不产生重复记录。
- 历史缩短、前缀分叉、相同 id 改 seed 等情况拒绝保存；完整内容相同可以无操作成功。
- 行锁与前缀比较用于避免过期副本静默覆盖，不提供分布式请求幂等。确定性动作产生相同结果的重复保存允许合并；README 明确本阶段建议单写入方顺序操作。
- 始终先锁状态行再访问事件，保持锁顺序。主键竞争等已知冲突转成明确仓储冲突错误，不能吞掉其他 IntegrityError。
- created_at 保留；实际状态推进时 updated_at 更新，通过真实数据库验证，不能只检查 onupdate 声明。
- 保存失败后重新 get 必须得到完整旧状态，不能读到缓存中的半成品。

不新增删除 API。事务边界是一次仓储操作；路由现有 get → 领域动作 → save 不占用贯穿 HTTP 请求的长事务。

## 6. 配置、应用装配与生命周期

新增 `REPOSITORY_BACKEND=memory|postgres`，默认 memory，非法后端直接报告配置错误。

- DATABASE_URL 在 memory 模式可缺省，postgres 模式必需，且必须使用 postgresql+psycopg URL；不得静默回退内存。
- 调整 TASK-002A 的 URL 总是必填测试为条件校验，保留环境变量读取覆盖。
- Alembic 无论应用后端是什么，都必须取得有效 DATABASE_URL；缺失时明确报错，不能因 memory 默认值而使用空 URL。
- `create_app(repository=...)` 保持兼容，显式注入优先；不因环境中的 postgres 配置创建额外 Engine 或要求其 URL。
- 未注入时按配置装配。应用自建 Engine 在 lifespan 中管理，关闭时 dispose；外部注入对象的资源由调用方负责。
- 导入配置、领域和数据库模块不得连接数据库；postgres 应用可以在 lifespan 启动时探测连接，失败则明确启动失败。
- 应用不自动运行迁移或 create_all。README 要求先显式 Alembic upgrade。
- `/health` 保持现有存活检查响应；不扩展监控系统。
- PostgreSQL API 测试必须进入/退出 lifespan。httpx ASGITransport 不会自动执行 lifespan，可使用 TestClient 上下文或显式管理。

同步数据库调用保持在同步路由/业务路径，不跨线程共享 ORM Session。

## 7. 异常与日志

| 情况 | HTTP / code |
|---|---|
| 会话不存在 | 保留现有 404 / session_not_found |
| 非法游戏动作 | 保留现有 409 和领域错误码 |
| 已存历史与待保存历史冲突 | 409 / persistence_conflict |
| 数据库连接等可用性故障 | 503 / persistence_unavailable |
| 种子、事件或状态无法一致恢复 | 500 / persistence_inconsistent |

为受影响路由声明 ErrorResponse。只将确定属于连接/可用性故障的异常归为 503；不能把 SQL 编程错误等一律当成临时故障。其余内部错误返回通用 500，不把 str(SQLAlchemyError) 放进响应。

日志记录操作、会话 id、稳定错误码和必要的安全异常类型。不得记录数据库密码、完整凭据 URL、带参数的原始 SQLAlchemy 异常字符串。不要自动重试动作写入。

## 8. 测试与数据隔离

### 8.1 无数据库测试

保留领域、内存仓储与 API 回归测试；覆盖配置选择、显式注入优先、postgres 缺少 URL、非法后端、资源所有权与释放、异常映射。普通测试不依赖 Docker。

### 8.2 PostgreSQL 测试约定

注册 postgres pytest marker；全部真实数据库用例标记它。只使用独立 TEST_DATABASE_URL，不回退开发 DATABASE_URL。未配置时说明原因并 skip；配置了但不可连接或未迁移应失败。

专用测试库名为 gamepilot_test，fixture 校验实际目标库名，不能对其他库执行清理。先通过 Alembic 建表，不使用 create_all 或 SQLite。只清理测试自身创建的会话（记录 id，借助外键级联清理事件）；禁止 TRUNCATE、全库删表、删除开发数据卷。

迁移降级验证使用另外创建的唯一临时测试库，在连接和清理前核对其名称；不能在共享开发库降级。若测试库存在且用途不明，先只读检查，不清空。

### 8.3 必须覆盖的行为

1. 初始会话保存/读取、不存在 id、负 seed 与超 BIGINT seed 往返。
2. 多回合保存后重新创建 Engine/仓储，读取快照完全一致。
3. attack/use_potion 混合序列恢复后继续动作，与不中断基准的事件和状态一致。
4. won/lost 终局均恢复正确，继续动作仍按原语义拒绝。
5. 重复保存不追加重复事件；更新时 created_at 不变、updated_at 推进。
6. 两个独立读取副本分别推进，过期或分叉副本不能覆盖已保存历史；采用确定性顺序交错，无需 sleep 制造并发。
7. 在状态行已 flush、事件尚未成功提交时注入故障，覆盖新建与更新，证明状态与事件都回滚；必须实际执行数据库事务，不只断言 rollback 被调用。
8. 篡改测试记录的事件数值/sequence 或状态后，get 报一致性错误，不自动修复。
9. PostgreSQL 后端 API 创建 → 动作 → 关闭应用 A → 新建应用 B → 查询 → 继续动作，全过程通过 HTTP，与基准一致。
10. PostgreSQL API 的 404/409/503/500 映射及无凭据泄露；人为故障使用测试资源，不关闭共享数据库服务。
11. 带旧种子数据的 0001→0002 升级保留数据；BIGINT 范围内降级成功，超范围降级失败且保留数据与迁移版本。

恢复、原子性和应用重建是核心验收证据，不能只增加 metadata 断言。

## 9. 文档与实际验证

更新 .env.example 和 README，说明后端选择、显式迁移、应用重启恢复、单写入方边界、专用测试库和测试命令。不要覆盖已有 .env，不提交凭据。

说明确定性恢复适用于当前战斗规则和兼容的 Python 运行时，不承诺更改规则后的历史存档兼容。

本机 Docker 位于 WSL Ubuntu-24.04，Windows 用户有 docker.cmd 桥接器。旧终端 PATH 未刷新时使用：

```powershell
& "$env:USERPROFILE\bin\docker.cmd" compose config --quiet
& "$env:USERPROFILE\bin\docker.cmd" compose up -d db
& "$env:USERPROFILE\bin\docker.cmd" compose ps
```

Windows 应用连接 127.0.0.1；无需 Docker Desktop。不得执行 docker compose down -v，也不能因 WSL/Docker 暂不可用就删除集成测试。

实际执行并报告：

```powershell
# DATABASE_URL 指向开发库或指定验证库，报告不输出凭据。
.venv\Scripts\alembic.exe upgrade head
.venv\Scripts\alembic.exe current
.venv\Scripts\alembic.exe check

.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider -m "not postgres"

# 创建专用 gamepilot_test，设置 TEST_DATABASE_URL。
# 以该 URL 显式执行 Alembic upgrade head 后再运行真实数据库测试。
.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider -m postgres
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m ruff format --check .
```

## 10. 验收与完成报告

同时满足以下条件才算完成：

- 两种仓储通过同一协议使用，默认内存与原 API 行为保留。
- postgres 模式真正持久化，应用重建后继续确定性战斗。
- 状态/事件原子保存，重复保存无重复事件，分叉历史不静默覆盖。
- 数据损坏可检测，数据库故障不泄露敏感信息。
- 0002 保留旧种子并支持超 BIGINT 整数，迁移与 metadata 一致。
- PostgreSQL 集成测试实际通过，不能全 skip 后称完成；原有测试和 Ruff 通过。
- 文档准确，未扩大范围或破坏现有数据。

报告包含：修改文件及职责、迁移、重放校验、事务边界、冲突策略、生命周期、错误映射、实际命令与通过/跳过数量、应用重建证据、已知限制。不能包含真实密码或凭据 URL。

更新 AGENTS.md 时仅记录“Claude 实施完成，待 Codex 审查”，不能自行标记独立验收通过。完成后停止，由项目所有者通知 Codex。
