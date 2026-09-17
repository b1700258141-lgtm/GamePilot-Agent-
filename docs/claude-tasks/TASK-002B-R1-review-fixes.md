# TASK-002B-R1：独立审查结果与返工要求

完成状态：Codex 已于 2026-09-17 完成修复和复验，TASK-002B 最终通过；验证结果记录在 `AGENTS.md`。以下内容保留为首轮审查记录。

审查日期：2026-09-17。结论：TASK-002B 暂不通过，修复以下三个实现问题并补充回归测试后复审。保留当前实现范围，不开始后续阶段。

先阅读 AGENTS.md、TASK-002B 和本文件。不要改写已应用的迁移、删除开发库或数据卷、提交或推送。

## 1. 已通过的独立验证

- 无数据库回归：68 passed，28 deselected。
- 真实 PostgreSQL 集成测试：28 passed，68 deselected，未跳过。
- Ruff check 通过，format --check 为 41 files already formatted；Git diff --check 通过。
- Compose 数据库 healthy，专用 gamepilot_test 处于 0002_session_seed_text (head)。
- Alembic check：No new upgrade operations detected。
- 原测试实际覆盖了重放续战、终局、重复保存、历史冲突、原子回滚、HTTP 应用重建及种子迁移。

首次普通测试因沙箱无法访问 pytest 临时目录出现 9 个 setup 错误；正常用户权限重跑后 68 项全通过，不属于项目缺陷。当前存在两条第三方弃用警告，不是阻塞项，不需要为此升级依赖。

以下结论来自额外独立探针，不是现有测试失败。此次审查只新增本返工单和更新进度，没有修改实现代码。

## 2. R1 — [P1] 通用异常处理仍允许服务器记录原始 SQL 和参数

位置：src/gamepilot/api/errors.py 的 handle_unexpected_error（约第 62 行）；同时检查 main.py 启动异常链。

触发：仓储抛出未被映射的 SQLAlchemy ProgrammingError，带原始 SQL 与参数。现有通用 Exception handler 返回安全 500，但 Starlette ServerErrorMiddleware 会在响应后重新抛出原异常，Uvicorn 捕获并通过 exc_info 写出完整异常。

独立复现：注入带人工标记 REVIEW_FAKE_SECRET 的 ProgrammingError，将真实 FastAPI 应用交给已安装 Uvicorn 的 RequestResponseCycle.run_asgi 执行，用内存日志 handler 捕获输出。没有真实凭据参与探针。

结果：HTTP=500；响应包含标记=false；服务器日志包含标记=true；服务器日志包含原始 SQL=true。只检查 response.text 或应用自定义 logger 不足以验收日志脱敏。

修复要求：

- 在可控数据库异常边界完成安全映射/处理，使原始 SQLAlchemy 异常不再被服务器完整记录。例如注册具体 SQLAlchemyError 的处理器，使其成为已处理异常；不能只继续依赖 Exception 兜底。
- 保留正确分类：编程/未知完整性错误仍是内部错误，不全部映射 503。
- 检查启动探测失败路径的 `raise ... from exc`，避免 lifespan 的异常日志打印原始敏感信息；必要时使用不含原始消息的安全异常及受控日志。
- 测试同时检查 HTTP 响应、应用日志和实际服务器异常输出路径。使用虚构标记验证 SQL 参数、连接 URL 不出现，不使用真实密码作探针。

## 3. R2 — [P2] 保存新回合前未校验已存状态，会静默修复损坏数据

位置：src/gamepilot/repositories/postgres.py 的 _update_existing，约第 141–145 行。

当前只有事件数量相等时才比较 stored_state 和 incoming state；追加新事件时只校验序号及事件前缀，随后直接覆盖状态行，未验证已有状态是否与已有事件一致。

独立真实数据库复现（仅使用本次创建的随机 id，结束后精确删除）：

1. seed=42 创建会话，attack 后 save。
2. 通过测试 SQL 将该记录 slime_hp 改成 1，事件不变。
3. get 正确抛 PersistenceInconsistentError。
4. 使用步骤 1 保留的合法领域对象继续 attack，再 save。
5. save 居然成功，损坏的 slime_hp=1 被改为 19，原损坏被掩盖。

这也能发生在 API 的 get 与 save 之间被外部修改状态时。任务明确要求更新前校验已有状态/事件，不能自动修复历史。

修复要求：在状态行锁保护下，修改任何字段前验证已有状态与已有事件的重放一致性（可复用 rebuild_session），然后再比较待保存历史的前缀。损坏时返回 persistence_inconsistent，事务不写入任何新状态/事件。

新增真实数据库测试：保留领域对象 → 篡改状态 → 继续动作/save → 拒绝 → 验证被篡改的旧值、事件数和 updated_at 均未被悄悄改变。保留现有重复保存和冲突行为。

## 4. R3 — [P2] 启动探测失败时 Engine 未被 dispose

位置：src/gamepilot/main.py 的 lifespan，约第 53–61 行。

_probe_database(engine) 位于 try/finally 之前，探测抛异常就不会进入清理块。现有测试只断言启动失败，以及成功启动后的关闭清理，漏掉失败启动的资源释放。

独立复现：让应用自建的 Engine.connect 抛 OperationalError，进入 lifespan 并捕获 PersistenceUnavailableError；dispose 调用次数为 0。

修复要求：将启动探测纳入同一个资源清理 try/finally，保证启动失败、运行正常、退出异常都释放应用自建 Engine，保持外部注入资源由调用方拥有。必要时一并调整 Engine 创建位置，使生命周期注释与代码一致。

新增测试覆盖失败启动必须调用 dispose，保留注入资源不被释放的断言。

## 5. 文档收尾（必须随实现修复完成）

- README 约第 137 行声称“未迁移会在启动探测阶段失败”，但实际只 SELECT 1。独立测试连接将 search_path 限定为 pg_catalog，使业务表不可见，结果启动成功、health=200、创建战斗=500。最小修正是准确写明启动只检查连通性，迁移必须显式执行；本次不要求扩大为新健康检查系统或自动迁移。
- README 测试说明把 DATABASE_URL 改为 TEST_DATABASE_URL 后没有恢复提示；补充保存/恢复原环境值的说明，避免同一终端随后启动应用写进测试库。
- README 的 tests/api 路径不存在，应指向 tests/integration/test_api.py。
- 删除“规则变更后应清库重建”的默认处理建议，改为保留原数据、明确报告不兼容，再单独设计迁移/版本兼容策略。数据删除仍需用户批准。
- 0002 迁移注释“BIGINT 与 NUMERIC 都无法存放例如 2**80”表述不准确：NUMERIC 能表示该例；采用 Text 的原因是保留现有整数输入范围且种子不参与 SQL 数值计算。修正说明即可，不改迁移行为。

## 6. 复验和报告

- 先补充能暴露 R1/R2/R3 的测试，再完成修复。真实库测试仍只使用 TEST_DATABASE_URL 指向 gamepilot_test，并只清理测试自身记录。
- 运行普通回归、全部 PostgreSQL 测试、Ruff check、Ruff format --check、alembic current/check。
- 报告每条问题的修复文件、测试证据、通过/跳过数及剩余限制；不能仅以原有 96 项通过作为返工完成依据。
- 更新 AGENTS.md 为“返工实施完成，待 Codex 复审”，不要自行标记独立验收通过。
