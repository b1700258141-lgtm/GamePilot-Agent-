# 持续集成检查

`.github/workflows/ci.yml` 在 `main` 推送、PR 和手动触发时运行。
四个独立任务采用 Ubuntu 与 Python 3.12；每个任务只获得仓库内容读取权限，
不持有模型密钥，不调用真实模型，不发布安装包或部署服务。

| 任务 | 检查内容 | 产物 |
| --- | --- | --- |
| Offline tests and regression demo | 安装 dev/agent 依赖；Ruff；所有非 PostgreSQL 测试；固定脚本演示；两次独立 32 seed 脚本评测及逐格对比；四 seed 离线 Agent 预检、固定单格 canary 与证据核验、八格胜利门槛 | JUnit、演示、跨运行与 Agent 离线证据 |
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
两条脚本和四个 profile，每次 256 格并逐格无模型重放。CI 再启动第二个独立
进程运行相同清单，严格核对摘要、原始报告及 SHA-256 指纹，逐字段比较两次
轨迹。它验证的是规则判定与独立运行复现性，不能代表 Agent 在不同 seed 下的规划能力。
离线 Agent 预检另跑固定 48 格，模型动作由测试替身选择，逐格保存 Agent、运行、
重放和脚本对照报告；同时穷举动作预算内的缺陷可达性，并由 HTTP 对照与
独立规则判定器复核可达格。其通过只验证工程链路，不能作为真实模型能力成绩。
固定单格 canary 也使用离线测试替身，CI 没有 `--paid` 或模型密钥。
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
原详细复验记录的本地归档入口见 `docs/validation/ARCHIVED_REPORTS.md`。

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
TASK-006B 本机两次独立运行均为 256/256、重放 256/256 match；跨运行
256/256 一致，差异 0。新增 CI 对比步骤也尚未远程运行，详见
`docs/validation/TASK-006B-independent-seed-reproducibility.md`。
TASK-006C 本机离线 Agent 预检执行 48/48、重放 48/48；正常目标 12/12、
误报 0，预定缺陷实际触发 11/12 且检出 11/11。新增 CI 步骤尚未远程运行；
详见 `docs/validation/TASK-006C-agent-seed-preflight.md`。
TASK-006D 将预定 12 格再区分为预算内可达 11 格、不可达 1 格；
seed 0 的治疗越界格完成全部 126 条动作序列穷举。现有 CI 预检步骤会
运行升级后的 1.1.0 摘要；本机记录见
`docs/validation/TASK-006D-agent-seed-reachability.md`，远程仍未运行。
TASK-006E 的单格离线 canary 已在本机 exit 0，四份配对证据均可读；
另有一次真实 seed 2 单格在 CI 外完成。新增 CI 步骤尚未远程运行，
见 `docs/validation/TASK-006E-agent-seed-canary.md`。
TASK-006F 在离线 canary 后严格核验摘要与四份证据；本机验收见
`docs/validation/TASK-006F-agent-seed-evidence-verifier.md`，远程仍待运行。
TASK-006G 增加固定四 seed 八格胜利门槛的默认离线运行；CI 不传 `--paid`
或模型密钥。本机验收见 `docs/validation/TASK-006G-victory-seed-gate.md`，
远程仍待运行。
本机是 Windows 环境，不能将这些结果称为 GitHub Linux runner 已通过。
工作流首次远程运行须在所有者授权推送后观察，当前没有发布 CI 成功徽章。

依赖仍按 `pyproject.toml` 的版本范围安装；此工作流验证安装兼容性，
不承诺所有未来依赖版本永久兼容。报告里部分证据引用含执行机器的绝对路径，
下载产物可用于检查内容；迁移到新机器后需要重新运行演示生成有效引用。
