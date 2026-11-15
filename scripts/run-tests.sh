#!/usr/bin/env bash
# 运行测试并显示实时进度条（tools/pytest_progress.py）。参数原样传给 pytest，例如：
#   scripts/run-tests.sh                         全量
#   scripts/run-tests.sh tests/acceptance -k ac108
# 输出到文件或管道时自动改为每 5% 一行的纯文本进度；QA_PROGRESS=0 关闭进度条。
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH="tools${PYTHONPATH:+:$PYTHONPATH}"
exec .venv/bin/python -m pytest -p pytest_progress "$@"
