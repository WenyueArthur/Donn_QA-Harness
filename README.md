# qa-harness：QA 全流程受控实现

本项目按 [docs/qa-flow-rebuild](docs/qa-flow-rebuild/README.md) 中的两份方案实现 QA 全流程。
采用一个 Python 包（`qa_flow`）、本地 SQLite 数据库和不可变证据目录，运行时只依赖标准库。

> 当前状态：功能已实现，**测试与验收方案尚未执行**。没有任何验收场景、新人实测或真实平台接入被宣称通过。
> 详见 [实现状态与未验证范围](docs/implementation-status.md)。

完整操作说明见 [产品使用说明书](docs/使用说明书.md)。

## 快速开始

1. 创建虚拟环境并安装（开发基线 Python 3.13）：

```bash
python3 -m venv .venv
```

```bash
.venv/bin/python -m pip install --no-deps -e .
```

2. 在任意工作目录执行离线实验（合成数据，不需要账号或网络）：

```bash
.venv/bin/python -m qa_flow demo
```

3. 按屏幕提示处理确认、继续并导出：

```bash
.venv/bin/python -m qa_flow review
```

```bash
.venv/bin/python -m qa_flow resume
```

```bash
.venv/bin/python -m qa_flow export --output ./交付
```

实验会依次经历：需求质检 → 业务问题确认 → 技术质检 → 测试分析 → 计划批准与关键预期确认 →
环境实测 → 冒烟准入 → 在缺陷构建上执行失败 → 在实验平台桩建单并回读附件 → 部署修复构建 →
复测与相邻回归 → 按授权关单 → 准出确认 → 知识回流。全部结果标注为合成实验。

## 日常命令

| 想做什么 | 命令 |
|---|---|
| 开始一个飞书需求 | Agent 读取飞书后生成来源包，`python -m qa_flow start --bundle "<来源包>"` |
| 开始一个本地文件需求 | `python -m qa_flow start --source "./需求.md"` |
| 继续未完成工作 | `python -m qa_flow resume` |
| 处理业务确认 | `python -m qa_flow review` |
| 查看或交付 | `python -m qa_flow status`；`python -m qa_flow export --output "./交付"` |

默认输出中文摘要；`--details` 展示证据；`--json` 输出机器回执；`--non-interactive` 不等待输入。
管理员命令见 `python -m qa_flow admin --help`，机器接口见 `python -m qa_flow api --help`。

## 目录

```text
src/qa_flow/
  cli.py / api.py     # 日常入口与机器接口（只负责参数、回执与展示）
  contracts/          # 单一现行契约与形状校验
  domain/             # 判定规则（纯函数）：质检、计划、用例、执行、缺陷、准出、上线、指标、易用性
  application/        # 命令编排、事务边界、操作调度、阶段判定、任务服务
  storage/            # SQLite（journal_mode=DELETE、synchronous=EXTRA）、快照、事件、证据、备份
  security/           # 目标白名单、原文保护、模型输入脱敏
  adapters/           # 本地执行器、实验平台桩、实验模型桩、旧规则只读桥接
  reporting/          # 派生报告与能力展示
  resources/          # 阶段注册表、实验配置、合成素材、受审实验脚本
skills/qa-flow/       # 薄 Skill：只指导 Agent 调用 CLI
docs/qa-flow-rebuild/ # 交接方案（规格原件）
docs/baseline/        # M0 旧规则冻结清单
tests/                # 验收目录结构（尚未编写与执行）
.qa-workspaces/       # 运行数据（不入源码仓库）
```

## 不可破坏规则（摘要）

- AI 只提出内容；状态由系统根据校验、真实执行回执和有效决定推进。
- 未知、未执行、过期、受阻与失败分别记录，不合并成通过。
- 已登记快照不可变；修订生成新快照，依赖结论自动过期。
- 外部操作先登记再执行；结果未知先核对，不盲目重发。
- 本地答复是 `local_attested`，不是已认证签字；实验审批桩只能产生 `simulated`。
- 模拟、实验、测试与生产证据互不冒充；数据不完整时指标为 `null`，不补 0。
