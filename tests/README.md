# 测试目录（验收尚未执行）

按 [测试与验收方案](../docs/qa-flow-rebuild/02-validation-plan.md) 组织。用户已决定本轮暂不执行测试与验收方案，
因此这里只保留目录结构，尚未编写独立验收程序，也没有任何验收结果。

| 目录 | 计划内容（方案 §6、§10.2） |
|---|---|
| `unit/` | 纯函数单元测试 |
| `contracts/` | 契约形状与跨对象关系 |
| `acceptance/` | 通过子进程调用公共 CLI 的独立验收（144 个场景） |
| `fault_injection/` | 崩溃点、屏障、平台故障模式 |
| `parity/` | 冻结旧规则等价比较 |
| `integration/` | 真实平台与模型接入（默认禁用网络） |
| `fixtures/` | `build_lab.py` 夹具工厂 |

执行测试统一用 `scripts/run-tests.sh [pytest 参数...]`，它加载 `tools/pytest_progress.py`：

- 在终端里运行时，显示原地刷新的进度条：百分比、完成数、通过/失败/跳过计数、已用时间、预计剩余时间和当前用例；失败的用例立即打印在进度条上方。
- 输出重定向到文件或管道时，改为每完成 5% 打印一行进度。
- `QA_PROGRESS=0` 关闭进度条，`QA_PROGRESS=plain` 强制纯文本。

产品已提供的受控测试钩子（仅 `QA_FLOW_TEST_MODE=1` 时生效）：

- `QA_FLOW_TEST_CLOCK=<ISO UTC>`：可注入时钟。
- `QA_FLOW_CRASH_AT=<点名>`：在 `after_begin`／`after_begin:<命令>`（事务开始）、`evidence_tmp`（证据临时文件已写出、尚未移入内容寻址目录）、`before_commit:<命令>`、`after_commit:<命令>`、`before_send`、`after_send` 处终止进程。
- `QA_FLOW_PAUSE_AT=before_send_check` + `QA_FLOW_PAUSE_DIR=<目录>`：发送前重新核验处的同步屏障。
- `python -m qa_flow api lab fault --name <故障模式> [--times N]`：实验平台桩的 9 种故障模式。`--times 0` 清除；不填时持续型故障（`attachment_uploaded_link_failed`、`rate_limited`、`etag_conflict`）持续到清除，其余只触发一次。故障触发的失败调用先记入平台调用账本再报错。

验收器不得调用产品内部函数计算期望值，也不得以本 README 或开发者演示代替验收。
