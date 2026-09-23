#!/usr/bin/env bash
set -euo pipefail
LINGKAO_TRIAL_ROOT="$(cd -- "$(dirname -- "$0")/.." && pwd)"
export LINGKAO_DB_PATH="$LINGKAO_TRIAL_ROOT/output/pilot-runs/batch-01/learning.sqlite3"
echo "临考试用批次 batch-01，使用独立学习数据库。"
echo "请使用专门的浏览器配置文件，打开 http://127.0.0.1:8010/"
echo "首次准备需要联网；学生记录留在本机 output/pilot-runs/batch-01。"
exec bash "$LINGKAO_TRIAL_ROOT/app/start.sh" --port 8010
