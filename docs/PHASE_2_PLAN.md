# 第二阶段工作方案：PostgreSQL 持久化基础

## 1. 阶段目标

第二阶段为 GamePilot 建立可靠的关系型数据库持久化能力，使游戏会话和战斗事件在服务重启后仍然存在，并能继续按照原随机序列确定性运行。

本阶段不是 Agent 记忆或 RAG 阶段。PostgreSQL 保存的是必须精确、一致、可约束的业务事实；RAG 将在后续阶段用于游戏规则、测试知识和历史缺陷的语义检索。

## 2. 分步实施

第二阶段拆成两张任务单，中间必须经过 Codex 审查：

1. **TASK-002A：数据库骨架与迁移**
   - 引入数据库依赖与配置；
   - 建立 ORM 表结构和 Alembic 首次迁移；
   - 用 Docker Compose 启动 PostgreSQL；
   - 不改变当前 API 和内存仓储运行方式。
2. **TASK-002B：PostgreSQL 仓储与恢复**
   - 实现 PostgreSQL 仓储；
   - 持久化会话和事件；
   - 通过确定性动作重放恢复领域会话；
   - 添加数据库集成测试并接入应用配置。

TASK-002A 验收前不得开始 TASK-002B。

## 3. 技术栈

- PostgreSQL 16；
- SQLAlchemy 2.x，同步 ORM；
- Psycopg 3；
- Alembic；
- Pydantic Settings；
- Docker Compose v2；
- Pytest 和 Ruff。

当前领域层、FastAPI 路由和仓储协议均为同步设计，因此本阶段不引入异步 SQLAlchemy。

## 4. 数据模型

### 4.1 `game_sessions`

保存战斗会话的当前投影：

- `session_id`：主键，保持现有 32 位字符串格式；
- `seed`：随机种子；
- `status`：`active`、`won` 或 `lost`；
- `turn`：当前回合；
- `player_hp`、`player_max_hp`、`potions`；
- `slime_hp`、`slime_max_hp`；
- `created_at`、`updated_at`。

TASK-002B 接入修正：现有 API 的 seed 为 Python 整数，可能超出 BIGINT。
通过新增迁移将 seed 列改为 Text，保存规范十进制文本，读取时还原 int；
保留现有 API 行为与已存值，不改写已应用的初始迁移。详见 `docs/DECISIONS.md`。

### 4.2 `combat_events`

保存结构化战斗事件：

- `id`：数据库生成的主键；
- `session_id`：指向 `game_sessions` 的外键；
- `sequence`：会话内事件序号；
- `turn`、`actor`、`kind`、`value`；
- `player_hp`、`slime_hp`、`potions`。

`(session_id, sequence)` 必须唯一；删除会话时事件应级联删除。生命值、回合、事件序号和药水数量应具有合理的非负约束，枚举型字符串应具有检查约束。

## 5. 恢复策略

不得将 Python `random.Random` 或其他领域对象通过 Pickle 保存进数据库。

TASK-002B 读取会话时，应从持久化事件中提取玩家动作，使用相同 `seed` 和公开领域方法重新执行动作，并将重放结果与数据库保存的状态、事件进行一致性校验。这样既恢复随机数生成器位置，也避免由仓储直接修改领域对象私有字段。

保存状态与新增事件必须共用事务；使用状态行锁和完整事件前缀校验，拒绝过期或分叉历史覆盖。默认保留内存仓储，通过 `REPOSITORY_BACKEND=postgres` 显式启用数据库。实施与验收见 `docs/claude-tasks/TASK-002B-postgres-repository.md`。

## 6. Docker 边界

本阶段 Docker Compose 只启动 PostgreSQL。FastAPI 仍然在本机虚拟环境运行。

应用 Dockerfile、应用容器、容器间网络部署和一条命令启动完整系统放到后续阶段。本阶段不得为了展示 Docker 而提前扩大范围。

## 7. 阶段验收标准

完成 TASK-002A 和 TASK-002B 后，第二阶段需要满足：

1. Alembic 能从空数据库升级到最新版本；
2. PostgreSQL 表、约束、外键和索引与设计一致；
3. 创建会话后重建应用实例，仍能得到相同快照；
4. 重启后继续执行动作，结果与未重启的确定性执行一致；
5. 会话状态和事件在同一事务中保存；
6. 数据库写入失败不会留下半份业务数据；
7. 内存仓储仍可用于快速单元测试；
8. 原有测试、数据库集成测试和 Ruff 检查全部通过；
9. README 说明数据库启动、迁移、测试和清理方式；
10. Codex 完成独立代码审查。

## 8. 明确不做

- Agent、LangGraph、MCP、Skills 或 RAG；
- pgvector 或独立向量数据库；
- Redis、消息队列和后台任务；
- 异步 SQLAlchemy；
- 应用 Dockerfile 和线上部署；
- 用户、登录和权限；
- 多实例并发控制和分布式锁；
- 前端、Playwright 和视觉测试。

## 9. 学习目标

项目所有者应在本阶段理解：

- 主键、外键、唯一约束、检查约束和索引；
- SQL 事务和回滚；
- ORM Model、领域 Model 与 API Schema 的边界；
- SQLAlchemy Engine、Connection 和 Session 的职责；
- Alembic 迁移与 `create_all()` 的区别；
- Docker Compose 服务、健康检查和数据卷；
- 为什么精确业务状态不能由 RAG 代替。
