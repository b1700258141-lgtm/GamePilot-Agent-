# 本地 Docker 交付

需要运行中的 Docker Engine 和 Docker Compose v2。首次构建需要下载基础镜像与 Python 依赖。
在仓库根目录运行：

```powershell
docker compose -f compose.local.yaml up --build
```

服务顺序是 PostgreSQL 健康 → 一次性迁移成功 → API 启动。
就绪后访问 `http://127.0.0.1:8000/` 打开战斗测试页：既可创建或恢复手动
会话，也可在“Agent 自动测试”中选择 skill、种子和缺陷，运行默认的离线演练并
查看判定过程、报告与再测对照。页面与 API 同源，无需另启前端服务。
`http://127.0.0.1:8000/docs` 仍可查看接口文档；`/health` 检查进程存活，
`/ready` 检查当前依赖是否可用。容器健康检查使用 `/ready`。
命令保持前台运行；Ctrl+C 会停止服务，命名卷仍保留。
如果本机 Docker 通过 WSL 包装器运行，保持这个终端可避免空闲会话退出导致服务被关闭。

端口已占用时，PowerShell 可先设置 `$env:GAMEPILOT_API_PORT = "18000"`。
若 Docker 由 WSL 包装器调用，Windows 环境变量默认不会传入 WSL；可在本地 `.env`
增加 `GAMEPILOT_API_PORT=18000`（保留其他已有配置），或在 WSL 终端设置变量。
服务只映射 IPv4 回环地址；PostgreSQL 不映射宿主端口。
镜像已包含 Agent 依赖，默认离线演练不调用付费模型。真实模式需要由服务端
环境变量 `GAMEPILOT_AGENT_API_KEY` 提供密钥，网页还要求每次单独确认付费
边界；不要将密钥写入仓库文件。若密钥已在 Windows 本机环境中，可运行
`./scripts/start_local_with_model.ps1`：脚本优先使用 `GAMEPILOT_AGENT_API_KEY`，
也接受指向 DeepSeek Anthropic 入口的 `ANTHROPIC_AUTH_TOKEN`，只在此次启动时
转发给 WSL Docker；已有镜像无需重建，需要重建时加 `-Build`。每次网页运行
最多 50 次模型调用、50 次动作，每次请求最多 512 输出 Token；输入 Token 和
金额无硬上限，费用无可靠单价时为 unknown。网页真实模式已完成一次单格
验证，记录见 `docs/validation/AGENT-WEB-REAL-SMOKE-20261013.md`。

## 接口与持久化演示

在另一个终端创建会话，执行一次攻击，再保存读取结果：

```powershell
$base = "http://127.0.0.1:8000"
$session = Invoke-RestMethod -Method Post -Uri "$base/api/v1/game-sessions" -ContentType "application/json" -Body '{"seed":42}'
$id = $session.session_id
Invoke-RestMethod -Method Post -Uri "$base/api/v1/game-sessions/$id/actions" -ContentType "application/json" -Body '{"action":"attack"}'
docker compose -f compose.local.yaml restart api
Invoke-RestMethod -Uri "$base/api/v1/game-sessions/$id"
```

重启后等待 `/ready` 返回 200 再读取。API 从数据库的初始状态与事件恢复战斗，
相同会话仍能继续执行下一步；它不是内存后端的进程缓存。
已安装项目的本机终端也可按 README 的 `gamepilot.testing run` 命令运行完整基线。

可重复执行自动验收脚本（它会创建两场测试战斗、重启 api/db 并重建 api 容器）：

```powershell
python scripts/container_smoke.py --project gamepilot-local --base-url http://127.0.0.1:8000
```

脚本检查重启和容器重建后的快照，以及后续动作是否与未中断的对照会话一致。
只对本地 HTTP 地址运行，不删除会话或数据卷；请对专用演示实例执行。

## 服务和数据边界

- `Dockerfile` 用包含 Agent 依赖的 wheel 安装应用，以 UID 10001 单 worker 运行；构建上下文是白名单，
  不包含 `.env`、密钥配置、`.git`、测试缓存或历史报告。
- `migrate` 是可见的独立服务，明确执行 `alembic upgrade head`；API 自身不执行迁移。
  迁移失败时依赖条件阻止 API 启动，先检查 `docker compose -f compose.local.yaml logs migrate`。
- 默认 Compose 项目名为 `gamepilot-local`，卷为该项目的 `local-pgdata`。
  原 `compose.yaml` 和 `gamepilot-pgdata` 开发卷不受影响。
- 上传的自定义 skill 作为纯文本保存在独立的 `local-agent-skills` 卷；默认 skill 内置于代码。
  上传内容不作为 Python 或 shell 执行，自定义 skill 只供真实模型使用。
- `compose.local.yaml` 的固定凭据只用于这个本地演示数据库；此配置不是公网生产部署方案。
- `/health` 只检查进程 HTTP 存活；`/ready` 对 PostgreSQL 执行一次只读连接探测，
  数据库不可用时返回 503 与稳定错误码 `persistence_unavailable`。容器健康检查
  以 `/ready` 为准；内存后端不依赖数据库。就绪检查不执行迁移，也不核验业务数据。

## 停止、重启与排错

```powershell
docker compose -f compose.local.yaml ps -a
docker compose -f compose.local.yaml logs --tail 80 api migrate db
docker compose -f compose.local.yaml stop
docker compose -f compose.local.yaml up --build
```

停止服务和再次启动都保留命名卷。不要为了排错执行 `down -v` 或全局 prune。
若构建失败，检查镜像代理/Python 包下载网络；若迁移失败，检查数据库日志与迁移版本。
验证时可用 `-p 独立项目名` 隔离容器和卷；后续命令必须持续使用同一个项目名。
