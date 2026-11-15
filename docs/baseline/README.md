# M0 旧规则冻结基线

`legacy-rule-manifest.json` 由以下命令生成，只读取旧仓库，不修改其文件、索引或安装副本：

```bash
python -m qa_flow admin legacy-freeze --legacy-root /Users/donnie/PycharmProjects/qa-skills --output docs/baseline/legacy-rule-manifest.json
```

- 内容：`qa-mainflow/` 下 838 个文件（含 `AGENTS.md`）的 SHA-256、大小，以及 5 个阶段对应的旧纯校验器。
- 旧仓库提交：`07488e54c661dd12c790f2c2e364c26a60271c19`，工作区与提交不一致（`worktree_dirty=true`）。
- 清单只证明字节一致，不是完整源码备份，也不能只凭提交号复现未提交源码。
  是否提交或受控打包旧源码，由用户决定。

真实项目配置 `rules.kind=legacy_bridge` 时引用本清单。运行前逐个核对摘要，任一文件变化即判定规则不可用；
校验器在私有临时目录的只读冻结副本中以 `python -I -B` 运行，不会写入旧仓库的 `__pycache__`。
