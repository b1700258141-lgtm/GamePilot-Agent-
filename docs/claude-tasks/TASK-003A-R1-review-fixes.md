# TASK-003A-R1：独立审查返工

日期：2026-09-18。当前状态：所有者已授权 Codex 直接修复，R1 修复与复验完成，TASK-003A 验收通过。

以下第 1～9 节保留首轮审查的问题与返工要求；最终结果以第 10 节为准。

必读 AGENTS.md、TASK-003A-test-runner-baseline.md 和本文件。仅修复本次审查问题，不开始 003B/003C，不修改被测游戏、仓储或数据库，不提交或推送。

## 1. 独立验证结果

- 非 PostgreSQL 回归：118 passed，29 deselected，2 条依赖弃用警告。
- Ruff check 与 format --check 通过，58 个文件格式符合要求。
- 回归包含真实本地 HTTP 的 CLI run/replay；没有以 Claude 汇报代替独立执行。
- 首轮 pytest 因默认临时目录权限失败。改用项目内全新唯一 basetemp 后通过；这是环境问题，不计入功能缺陷。
- 本次没有运行 PostgreSQL 测试。Git 差异确认现有领域、API、应用装配、仓储、迁移未改变，结论仅覆盖新增执行器与正常内存服务，不代表数据库重新验收。
- 独立异常响应探针结果见本地 `artifacts/codex-review-003a/probe-results.json`，产物不入库。下文列出可复现条件，避免依赖本机产物。

## 2. R1 / P1：补齐事件与最终状态之间的约束

位置：oracle.py 的 _potion_checks、_retaliate_checks、_invariant_checks 和 _last_event_checks。

已复现两种真实漏检：

1. seed=42，attack → use_potion；药水事件正确记录剩余 1 瓶，但最终快照仍有 2 瓶。整个 attack-then-potion 场景返回 pass。当前只检查事件扣药，最终库存只要求非负且不增加。
2. 单次 attack 后，攻击事件记录史莱姆 HP=41，但反击事件及最终快照将史莱姆 HP 改回 60；场景返回 pass。当前反击没有约束史莱姆自身 HP 保持不变。

修复要求：按动作定义串起前态、玩家事件、反击事件、终态，校验每个事件所有相关业务字段；攻击不能扣药，喝药后最终库存必须恰好减一，喝药不能伤害/治疗敌人，反击不能改变敌人 HP，事件 HP 必须在边界内。不要从被测实现导入规则。

新增独立反例覆盖上述两例，以及攻击偷偷扣药、药水事件无故改变敌人 HP；正常邻近样本仍通过，断言具体规则标识。

## 3. R2 / P2：同一会话比较不得抹掉身份与请求契约

位置：oracle.py:121-138、326-337；runner.py:213-228、313-324；replay.py:138-175。

已复现：创建后 GET 只改变 session_id，initial-state 仍为 pass；GET 返回 404 但附带有效快照也为 pass；创建/GET 均返回 seed=999 而请求为 42，也为 pass。

修复要求：

- 同一会话创建/GET/拒绝动作后的比较包含 session_id；跨新会话比较才可排除它。
- 所有成功 GET 明确检查 HTTP 200，所有创建快照校验 seed 等于本次请求。
- replay 归一化前验证请求路径和响应中的 session_id 与该次会话关联正确。当前任意长十六进制段都被替换，把路径中的另一个会话 ID 换进去也比较无差异；不能仅以正则替换证明命中了正确会话。
- 新增初始 GET、409 核对 GET 的身份/状态码反例，错误 seed 反例，以及报告内请求路径与本会话 ID 不一致的反例。

## 4. R3 / P2：错误路径必须先保留证据再退出

位置：runner.py:289-322；models.py 的 StepReport、CaseReport。

已复现：第一次动作返回 HTTP 200 和 JSON 对象，但缺少快照字段；或满血喝药返回 409 后核对 GET 返回 503。两种情况均生成 executed_actions=1、steps=0 的报告，load_report 随后因计数不一致拒绝它。

修复要求：每次已发出动作都保存 StepReport，保留原始响应、错误位置、核对 GET 观测及最后已知快照。初始 GET 的失败证据也应保留。不通过放松动作/步骤一致性检查掩盖丢失证据。

新增上述两条错误路径与 GET 快照解析失败回归。自产错误报告可以加载，在重跑时明确标注不可比较，并保存新的执行结果。

## 5. R4 / P2：重跑实际轨迹，不执行未发生的计划动作

位置：replay.py:370-371；_validate_case_boundary。

已复现：计划两次 attack，第一次响应因异常 HP 导致停止，原报告只有一次动作；向正常服务 replay 后实际执行两次动作。当前直接 run_case(recorded.scenario)，违反任务单关于重放实际动作尝试的约定。

修复要求：依据已执行证据构造重放轨迹，保留每个动作的原预期及拒绝动作；未执行的尾部动作不得自动发出。原计划作为元数据保留，不能因有意截短产生虚假的计划差异。确需重新跑整份计划时应与 replay 区分，不在本任务扩展新模式。

校验 case/scenario/执行记录中的 seed、动作、索引、预期及已执行前缀彼此一致；校验所有可执行轨迹（包括 control）的长度边界。原运行错误时也不能顺带执行从未发生的尾部动作。重跑结果应保留可检查的新观测，而不只动作摘要。

新增失败后停止、错误后停止、已记录拒绝动作、轨迹字段互相矛盾的回归；用请求计数证明没有多发动作。

## 6. R5 / P2：读取报告时不得填补缺失证据或忽略嵌套字段

位置：models.py 的嵌套报告模型及 RunReport 默认值；replay.py:61-84。

已复现：从正常 initial-state 报告删去 schema_version、删去 initial_snapshot.events，或者给 initial_snapshot 添加未知业务字段，load_report 均接受且 replay 返回 match。原因是默认字段补值与嵌套模型默认忽略 extra；顶层 extra=forbid 不会递归生效。

修复要求：在报告读取边界严格校验证据，必要字段必须实际存在；所有层级的未知字段、错误类型按明确的版本契约处理。可拆分创建模型和读取校验，不要求取消所有内部便利默认值。不要把数字字符串、布尔值等静默转换后声称逐字段一致。

同时修复 tests/integration/test_testing_runner.py 中 test_replay_refuses_unsupported_or_inconsistent_reports：其 _write 每次覆盖同一个 candidate.json，最后循环中的多数路径实际指向最后一份内容，多个分支没有真正被测试。改为每个参数独立执行或唯一文件，并断言具体拒绝原因。

新增删除 schema_version、缺失嵌套字段、嵌套未知字段及证据类型不符测试，分别验证各自原因。

## 7. R6 / P2：兑现报告 I/O 失败的退出码

位置：runner.py:84-90；cli.py 的 main 异常处理。

已用真实本地服务复现：--output-dir 指向一个已有普通文件，所有场景通过后抛出未捕获的 FileExistsError；没有按约定返回执行错误码 2。命令进程通常以未捕获异常退出码 1 结束，会与规则失败混淆。

修复要求：将报告目录创建、写入等 OSError 转为 ReportWriteError 或等价受控错误，CLI 返回 2 并给出简明诊断；不能宣称报告成功保存。现有文件保持不变，创建报告使用排他方式，避免检查存在与写入之间覆盖已有证据。

新增 output-dir 为普通文件的端到端用例，以及无写权限等可通过受控 mock 稳定覆盖的 I/O 失败；验证退出码 2、没有覆盖和误报成功。

## 8. 对专项审查问题的结论

| 专项 | 结论 |
| --- | --- |
| 判定器独立性 | 无被测实现导入，方向正确；但独立性不等于完备性，R1/R2 的漏检必须修复 |
| 治疗后反击顺序 | 按药水事件校验治疗、再从治疗后 HP 扣反击伤害，符合当前明确规则；没有发现所谓第二种合法顺序，问题在其他状态字段未串联 |
| 5xx 与网络分类 | 502 HTML 实测归为 server_error；TimeoutException 在 TransportError 前捕获正确。网络环境会影响实际错误类型，二者仍都是 error，不能要求同类网络故障标签永远相同 |
| replay 排除项 | 跨会话排除生成 ID、时间和耗时合理；需先验证会话内关联，且 R4/R5 的轨迹与读取缺陷不能由归一化掩盖 |
| 退出码 | run 的 error 优先逻辑正确；R6 未捕获 I/O 破坏约定。replay 的 0 代表复现一致，可能一致地复现 fail，不应单独作为游戏质量通过门禁 |
| HTTPX / Ruff | CLI 需要 HTTPX，提升运行依赖合理，锁文件对应变化一致；排除 .codex-temp 仅影响工具临时目录，未发现把产品源文件排除的情况 |
| PostgreSQL 边界 | 此次未重新验证数据库，可接受为本任务的范围限制；不得把 29 deselected 写成 29 passed |

补充：rules.py 定义 INFRASTRUCTURE_ERROR_CODES，但客户端未使用。409 persistence_conflict 当前落入普通规则失败；返工时明确其为存储执行故障，并补一条分类测试，避免未来连接 PostgreSQL 时混淆游戏规则与持久化冲突。无需为此更改数据库或运行库清理。

## 9. 返工验收

只调整 testing 模块、对应测试和必要文档。遵守原任务边界，不进行大规模重构。

运行非 PostgreSQL 全量回归、Ruff check 和 format --check，再验证真实本地 HTTP run/replay。pytest 使用全新唯一项目内临时目录；不要删除或复用未知旧目录。

必须提交各问题的反例回归、实际命令和结果，说明报告版本是否变化及兼容处理。未改动持久化相关代码可继续声明 PostgreSQL 未复验；若触及共享层则按原任务要求补跑数据库测试。

更新 README 和 AGENTS.md 为“返工完成，待 Codex 复验”，不得自行写成验收通过。报告学习复盘仍待所有者确认。

## 10. Codex 修复与最终复验（2026-09-18）

项目所有者明确指示“你来修复吧”，本轮由 Codex 实施并验证。未修改被测游戏、API、仓储、迁移或应用装配。

- R1：串联事件和最终状态中的 HP、药水与敌人状态；漏扣药水、攻击扣药、敌人异常回血和喝药改变敌人 HP 均有反例回归。
- R2：同会话比较完整身份，检查请求 seed 和 GET 200；重跑先验证路径归属，按实际会话 ID 精确规范化，保留错配身份。
- R3：每次已发动作先记录步骤，随后进行解析/校验；保留初始 GET、409 核对 GET、解析错误及最后已知快照，对照运行错误也保留部分轨迹。
- R4：重跑只执行已发生动作前缀，保留原计划元数据；主轨迹、对照轨迹均校验长度和关联，重跑输出包含完整 execution。规则失败可一致复现，错误运行标为 not_comparable。
- R5：所有嵌套模型拒绝未知字段和类型转换，报告读取额外要求字段实际存在；校验计划与执行证据、会话关联及汇总。非法报告测试改为独立参数化用例，不再共享覆盖候选文件。
- R6：排他创建报告文件，将目录创建/文件写入 OSError 转为受控错误，CLI 返回 2；已有文件保持不变。
- 补充：409 persistence_conflict 等已知存储故障归类为执行错误，5xx 优先级保持不变。

报告 schema 从 1.0 升级至 1.1，规则从 1.0.0 升级至 1.1.0。旧报告缺少新证据，明确拒绝重跑，需要重新执行 run；没有修改或删除旧报告。

最终验证命令：

```powershell
$fixTemp = Join-Path (Get-Location) ('artifacts/fix-003a-final-' + [guid]::NewGuid().ToString('N'))
.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider -m 'not postgres' --basetemp $fixTemp --tb=short
.venv\Scripts\ruff.exe check .
.venv\Scripts\ruff.exe format --check .
```

结果：171 passed，29 deselected，2 条原有第三方弃用警告；Ruff 全部通过，60 个文件格式符合要求。没有把数据库测试排除计为通过。

另用测试启动的独立本机内存服务，通过两个独立 Python CLI 子进程运行 run/replay：均 exit 0，8 pass、8 match。持久保存的本地证据（已由 artifacts 忽略规则排除出 Git）：

- `artifacts/test-runs/r1-verified-90c0cf94/20260918T033623Z-25a7d2.json`
- `artifacts/test-runs/r1-verified-90c0cf94/20260918T033627Z-ef96b2.json`

范围限制：本轮 PostgreSQL 未复验；不声称所有者已经完成学习复盘。未提交、推送或开始下一任务。
