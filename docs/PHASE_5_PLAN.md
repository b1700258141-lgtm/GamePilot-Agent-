# 第五阶段：本地容器交付

状态：2026-10-10，Codex 暂代实施 TASK-005A、TASK-005B 与 TASK-005C，本机验收通过。
沿用既定 Docker 交付方向，先完成本地单机部署，不增加云部署或公开发布。

## TASK-005A 范围

- 多阶段应用镜像：构建并安装基础 wheel，单进程运行 FastAPI，非 root 用户执行。
- 严格限制构建上下文，只包含包源码、元数据及迁移；排除本机配置与历史报告。
- 独立 `compose.local.yaml`：PostgreSQL 16、一次性 Alembic 迁移、API。
  数据库只在容器网络内访问，API 只发布本机回环端口。
- 数据卷由独立 Compose 项目管理，不使用第二阶段 `gamepilot-pgdata` 开发卷。
  启动命令明确运行迁移服务；应用自身不偷偷执行迁移。
- 文档解释健康检查、首次启动、重复启动、保留数据的停止与故障排查。

## 验收

真实构建镜像并运行服务；健康端点及创建/动作/读取接口通过；
正常脚本基线在容器服务通过；重启后会话快照及下一步行为保持一致；
新容器继续读取原数据卷；迁移失败不能启动 API。
确认容器非 root，基础依赖不包含模型 SDK，镜像不包含本机配置或历史产物。

不运行真实模型，不更改游戏规则、API 契约或数据库 schema。
停止验证服务时保留命名卷，不执行卷清除、全局 prune 或开发库清理。
跨种子稳定性、缺陷状态持久化、RAG/MCP 与网页操作另行规划。

## 本轮结果

真实镜像构建、迁移与非 root 运行通过；HTTP 基线 8/8、重放 8/8。
API/数据库重启、API 重建与后续动作对照四项通过；迁移失败时 API 未启动。
详细证据见 `docs/validation/ARCHIVED_REPORTS.md`。

## TASK-005B：运行期就绪检查

现有 `/health` 只表明 API 进程仍在运行，无法反映数据库运行期中断。
新增 `/ready`：PostgreSQL 模式每次执行只读连接探测；断库返回稳定的 503，
恢复后自动返回 200。内存模式直接就绪。容器健康检查改用 `/ready`，
保留 `/health` 的原有语义。用独立 Compose 项目验证断库和恢复，
不修改游戏规则或数据库 schema。验收见
`docs/validation/ARCHIVED_REPORTS.md`。

## TASK-005C：容器回归接入 CI

在现有离线、PostgreSQL 和安装包检查之外，增加独立的容器任务：
Linux runner 构建 `compose.local.yaml`，真实运行迁移与 API；从宿主机执行
8 场基线、逐字段重放和重启恢复脚本，并确认容器保持 healthy。
失败时保留已生成的报告与服务日志，随后停止专用服务。
本机等价流程已验收；GitHub runner 首次执行仍待推送后确认。
任务与证据见 `docs/claude-tasks/TASK-005C-container-ci-gate.md` 和
`docs/validation/ARCHIVED_REPORTS.md`。
