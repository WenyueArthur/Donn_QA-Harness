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

产品已提供的受控测试钩子（仅 `QA_FLOW_TEST_MODE=1` 时生效）：

- `QA_FLOW_TEST_CLOCK=<ISO UTC>`：可注入时钟。
- `QA_FLOW_CRASH_AT=<点名>`：在 `before_commit:<命令>`、`after_commit:<命令>`、`before_send`、`after_send` 处终止进程。
- `QA_FLOW_PAUSE_AT=before_send_check` + `QA_FLOW_PAUSE_DIR=<目录>`：发送前重新核验处的同步屏障。
- `python -m qa_flow api lab fault --name <故障模式>`：实验平台桩的 9 种故障模式。

验收器不得调用产品内部函数计算期望值，也不得以本 README 或开发者演示代替验收。
