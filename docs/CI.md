# 持续集成检查

`.github/workflows/ci.yml` 在 `main` 推送、PR 和手动触发时运行。
四个独立任务采用 Ubuntu 与 Python 3.12；每个任务只获得仓库内容读取权限，
不持有模型密钥，不调用真实模型，不发布安装包或部署服务。

| 任务 | 检查内容 | 产物 |
| --- | --- | --- |
| Offline tests and regression demo | 安装 dev/agent 依赖；Ruff；所有非 PostgreSQL 测试；固定脚本演示；32 seed 脚本评测 | JUnit、演示与跨种子证据 |
| PostgreSQL migrations and integration tests | 临时 PostgreSQL 16；空库与重复升级；模型漂移检查；真实仓储测试 | JUnit |
| Installed wheel without Agent extras | 构建 wheel；在全新环境仅安装基础依赖；源码目录外隔离运行演示 | 演示证据 |
| Containerized API regression and recovery | 构建应用镜像；启动独立数据库、迁移与 API；真实 HTTP 基线、重放及重启恢复 | 运行/重放/恢复报告与服务日志 |

数据库任务只连接本次 runner 内的 `gamepilot_test` 服务。
工作流中的数据库口令是临时测试配置，不是开发库或线上凭据。
TCP 健康检查用于等待最终数据库服务，避免误认初始化阶段的临时 Unix socket 服务。
容器任务使用专用 Compose 项目 `gamepilot-ci`，API 只映射 runner 回环地址的
18770 端口。它复用 `scripts/container_smoke.py`，在同一持久化卷上重启 API、
数据库并重建 API 容器，最后要求 API 容器为 healthy。测试结束只停止该项目；
runner 结束时其临时卷随 runner 销毁。
仓库无需配置任何 GitHub Secret。任务超时分别为 30、15、15、25 分钟。

任一步骤失败会使所在任务失败；`always()` 上传已生成证据，保存 14 天。
跨种子检查在进程内运行四个隔离内存靶场，不请求模型；固定 0–31 种子、
两条脚本和四个 profile 共 256 格，并逐格无模型重放。它验证的是规则判定与
确定性，不能代表 Agent 在不同 seed 下的规划能力。
同分支的新执行会取消旧执行，避免重复消耗 runner 时间。
配置不包括分支保护，只有所有者将这些检查设为 required 后才会阻止未通过的合并。

## 本地复验

开发环境安装 `.[dev,agent]` 后，可使用 README 中的完整非数据库与数据库测试命令。
数据库测试务必显式指定专用 `gamepilot_test`；它不会回退到开发库。
安装包检查应在新虚拟环境里安装 `pip wheel --no-deps .` 生成的 wheel，
而不是再次执行 editable install；从项目目录外用 `python -I -m gamepilot.defects demo` 运行。
`-I` 排除当前目录和 `PYTHONPATH` 对导入的影响。

如果本机 Docker 通过 WSL 包装器调用，且脱离前台会话后发生容器终止，
可在单独终端以前台方式保持临时数据库运行，测试结束后只停止该临时容器。
不要用清空开发卷的方式解决连接问题。
容器门禁可在本机独立项目中以前台 Compose 保持运行，另开终端运行
`gamepilot.testing run`、`replay` 和 `scripts/container_smoke.py`；
完整复验记录见 `docs/validation/TASK-005C-container-ci-gate.md`。

## 证据与限制

截至 2026-10-10，本机完整离线测试沿用 2026-10-09 的
508 passed、1 skipped、29 deselected；
临时 PostgreSQL 测试 29 passed；空库升级、重复升级、模型漂移检查通过。
独立 wheel 在无 anthropic/langgraph 的干净环境中完成演示，exit 0。
容器门禁的本机等价流程在 18770 端口通过 8/8 基线、8/8 重放与四项恢复检查，
完成后 API 容器 healthy。GitHub 上新增的第四任务尚未远程触发。
2026-10-12 新增的跨种子步骤在本机执行 256/256 格、重放 256/256 match，
触发与目标规则检出详见 `docs/validation/TASK-006A-cross-seed-scripted-eval.md`；
该新增步骤也尚未在 GitHub runner 运行。
本机是 Windows 环境，不能将这些结果称为 GitHub Linux runner 已通过。
工作流首次远程运行须在所有者授权推送后观察，当前没有发布 CI 成功徽章。

依赖仍按 `pyproject.toml` 的版本范围安装；此工作流验证安装兼容性，
不承诺所有未来依赖版本永久兼容。报告里部分证据引用含执行机器的绝对路径，
下载产物可用于检查内容；迁移到新机器后需要重新运行演示生成有效引用。
