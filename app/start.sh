#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8
if ! command -v python3 >/dev/null 2>&1; then
  echo "请先安装 Python 3.10 或更新版本。" >&2
  exit 1
fi
if [ ! -x .venv/bin/python ]; then python3 -m venv .venv; fi
if ! .venv/bin/python -c "import sympy; assert sympy.__version__ == '1.14.0'" >/dev/null 2>&1; then
  echo "首次准备依赖，需要联网；安装成功后可离线运行。"
  .venv/bin/python -m pip install -r requirements.txt
fi
if [ "${1:-}" = "--test" ]; then
  .venv/bin/python -m unittest discover -s tests -v
  if command -v node >/dev/null 2>&1; then
    node tests/test_drafts.cjs
  else
    echo "未安装 Node.js，已跳过前端草稿回归；运行应用不需要 Node.js。"
  fi
  exit 0
fi
exec .venv/bin/python server.py "$@"
