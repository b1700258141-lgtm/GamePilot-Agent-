# GamePilot

GamePilot 是面向游戏测试开发岗位的 Agent 工程作品：用可控的回合制战斗作为被测游戏，让 Agent 选择测试动作，工具读取和改变游戏状态，独立规则判定器确认异常，再保存可重放的证据。它目前是**受控测试靶场**，尚不是通用游戏自动测试平台。

## 先跑起来

需要 Docker Engine 与 Docker Compose v2。在仓库根目录运行：

```powershell
docker compose -f compose.local.yaml up --build
```

保持这个终端打开。就绪后访问 [战斗测试页面](http://127.0.0.1:8000/)；[API 文档](http://127.0.0.1:8000/docs) 和 `/ready` 可辅助排错。页面保留创建/恢复会话、攻击、喝药和事件查看，并提供 Agent 自动测试入口。容器使用独立的本地 PostgreSQL 卷，停止服务不会删卷。详见 [Docker 说明](docs/DOCKER.md)。

无需 Docker 时，可用 Python 3.12+ 建立环境并运行默认内存后端：

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev,agent]"
python -m uvicorn gamepilot.main:app --reload
```

只运行游戏、脚本测试和缺陷演示时可以安装 `.[dev]`，不安装 `agent` 可选依赖；页面 Agent 自动测试需要 `.[agent]`。

## 目前有什么

| 部分 | 作用 |
| --- | --- |
| 游戏与 API | 玩家与史莱姆、哥布林或巨魔的确定性战斗；固定 seed + 同一动作序列可重现敌人、状态、掉落和事件。 |
| PostgreSQL | 保存战斗状态与事件；重启后按历史重放恢复。 |
| `gamepilot.testing` | 固定场景、HTTP 动作、独立规则判定、JSON 报告及无模型重放。 |
| `gamepilot.lab` | 可控规则缺陷：治疗越界、药水未消耗、死亡后行动、掉落异常。 |
| `gamepilot.agent` | LangGraph 单 Agent 按目标逐步选动作并调用工具；预算、错误和用量进入报告。 |
| `gamepilot.benchmark` | 正常/缺陷配对、跨 seed 评测、脚本对照与证据核验。 |
| `gamepilot.defects` | 将已确认规则失败导出为缺陷单和回归规格，支持批量比较。 |

**判定由确定性规则程序完成。**游戏规则不通过、Agent 漏检、目标未完成和执行错误分别记录。预植入缺陷被正确发现时，游戏判定为“不通过”，Agent 的检出任务却是成功的。

## 最短的自动测试演示

网页中选择“Agent 自动测试”，默认 skill 会先在满血时喝药，此后受伤且有药水就喝药，药水耗尽后持续攻击，并在战斗结束后再试一次行动限制；无需选择测试目标。可单独选择随机种子与已知缺陷，留空 seed 时服务端生成并记录实际值。也可上传不超过 4,000 字符的 Markdown/纯文本 skill 并选择它；上传内容只作为真实模型的动作策略输入，不执行代码。默认“离线演练”使用只支持默认 skill 的测试替身，不消耗付费额度，也不代表真实模型能力。过程、独立规则检查、最终结论与无模型重放均有证据；PostgreSQL 保存运行记录，上传的 skill 保存在独立 Docker 卷。页面的“deepseek-flash”选项实际请求 `deepseek-v4-flash`，须服务端密钥和每次单独付费确认，每次最多 20 次模型调用、20 次动作、每次请求最多 512 输出 Token（合计请求输出上限 10,240）；输入 Token 和金额无硬上限，费用无可靠单价时为 unknown。[新版战斗规则](docs/BATTLE_RULES_V2.md)写明药水效力、敌人和掉落。[网页真实单格验证](docs/validation/AGENT-WEB-REAL-SMOKE-20261013.md)是此前 6 步预算下的历史验证，不代表本次 skill 变更后的真实模型能力。

Windows 本机已有 DeepSeek 环境变量时，可运行 `./scripts/start_local_with_model.ps1` 启动真实模式；脚本不把密钥写入文件。详细说明见[本地 Docker 交付](docs/DOCKER.md)。

安装基础包后，运行无需模型和数据库的脚本缺陷演示：

```powershell
python -m gamepilot.defects demo
```

它现场生成四类已知缺陷的运行、重放、缺陷单与正常对照。要体验 Agent 自动选动作，需要安装 `agent` 依赖；以下命令**只使用离线测试替身，不调用付费模型**：

```powershell
python -m gamepilot.benchmark agent-seed-gate --output-dir artifacts/agent-seed-gates
```

该门槛固定 seed 0–3 × 正常/死亡后反击 × 胜利目标，共 8 格；输出 Agent、游戏运行、无模型重放和同 seed 对照证据。v2 离线复验 8/8 符合预期；历史真实模型八格证据属于 v1 规则。可用 `python -m gamepilot.benchmark --help` 查看其他离线评测命令。报告默认写入被 Git 忽略的 `artifacts/`，每次创建新目录，不覆盖旧报告。

真实模型测试需要显式 `--provider anthropic-compatible --paid` 和密钥环境变量，**不要把密钥写入命令、文档或提交**。TASK-006G 于 2026-10-13 完成一次固定真实八格：8/8 符合预期，30 次模型调用，输入 6,691 / 输出 1,664 Token；无可靠计价，费用 unknown。固定上限为 48 次调用、24,576 请求输出 Token；输入 Token 与金额没有硬上限。具体门槛和证据见 [第六阶段计划](docs/PHASE_6_PLAN.md)和[TASK-006G 验收](docs/validation/TASK-006G-victory-seed-gate.md)。

## 验证与交付

```powershell
python -m pytest -q -p no:cacheprovider -m "not postgres"
python -m ruff check .
python -m ruff format --check .
```

PostgreSQL 集成测试必须使用显式的专用测试库；迁移、安装包和容器检查见 [CI 说明](docs/CI.md)。GitHub Actions 工作流已配置，但截至 2026-10-12 尚未远程运行或设为合并门禁；本地通过不等于远程通过。

现有真实模型证据限于 seed 42 的固定 12 格小样本、seed 2 的一次缺陷 canary 和固定四 seed 的一次真实八格；[V6 报告](docs/validation/TASK-003C-V6-second-real-gate-c.md)、[seed 2 报告](docs/validation/TASK-006E-agent-seed-canary.md) 与 [八格门槛](docs/validation/TASK-006G-victory-seed-gate.md) 分开记录。这些样本仍不能证明一般游戏测试能力或长期稳定性。

架构与取舍见 [决策记录](docs/DECISIONS.md)；阶段范围见 `docs/PHASE_*_PLAN.md`。文档精简与固定八格真实模型测试已获所有者验收；[Agent 自动测试前端](docs/AGENT_TEST_UI_DESIGN.md)已按设计实施，等待所有者页面验收。
