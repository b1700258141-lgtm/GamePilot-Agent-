# GamePilot（第一阶段）

面向游戏测试开发场景的**确定性回合制战斗靶场**。GamePilot 的长期目标是让 Agent
理解游戏规则、规划测试路径、通过工具操作可控的游戏测试环境并生成缺陷报告；
当前第一阶段先交付一个规则明确、可通过 HTTP 操作、可确定性回放的
「玩家 vs 史莱姆」回合制战斗核心，作为后续测试 Agent 的稳定被测对象。

## 当前已实现

- 玩家（HP 100/100，2 瓶药水，攻击 18~25）对战史莱姆（HP 60/60，攻击 20~35）的回合制战斗
- `attack` 与 `use_potion` 两种动作，含反击、胜负判定与回合计数
- 固定随机种子：相同 seed + 相同动作序列 ⇒ 完全相同的伤害、事件与最终状态
- 结构化战斗事件（回合、行动方、动作类型、数值、动作后双方 HP、剩余药水）
- FastAPI 接口：创建会话、查询状态、执行动作、健康检查（含 `/docs` 交互式文档）
- 内存仓储 + 可替换的仓储协议
- 完整的领域单元测试与 API 集成测试

## 当前未实现（属于后续阶段）

Agent 接入（LLM/规划）、数据库持久化、Docker 部署、前端与视觉测试、MCP。
这些都不属于第一阶段，不会提前引入。

## 环境要求

- Python 3.12+（已在 3.14.6 上验证）

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

```powershell
python -m pytest
python -m ruff check .
python -m ruff format --check .
```

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

## 架构说明

```text
API 层（FastAPI 路由 + Pydantic 请求 Schema）
  │  只做参数校验与调用，不含任何游戏规则
  ▼
领域层（CombatSession：状态转换、伤害计算、随机、结构化事件）
  │  不依赖 FastAPI，可直接单元测试
  ▼
仓储抽象（SessionRepository 协议）
  ▲
内存仓储（InMemorySessionRepository，第一阶段唯一实现）
```

- **确定性**：每个会话持有私有的 `random.Random(seed)`，不使用全局随机；
  事件不带时间戳，便于回放比对。
- **错误处理**：领域错误携带稳定错误码，由 API 统一映射为 HTTP 404/409；
  请求校验错误保留 FastAPI 的 HTTP 422；错误响应只含错误码与消息，不暴露堆栈。

## 后续计划

第一阶段验收后，将依次引入数据库持久化、Docker、Agent 工具层与评测体系，
详见 `docs/PHASE_1_PLAN.md` 与 `AGENTS.md`。

## 已知限制

- 战斗数据只保存在进程内存中，服务重启后丢失（第二阶段替换为数据库）；
- 目前只有玩家与史莱姆一种战斗，没有商店、背包、存档等内容；
- 未做并发写入控制，当前面向单进程演示与测试场景。
