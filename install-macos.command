#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
if [[ "$(uname -s)" != Darwin ]]; then
  printf '%s\n' '此入口面向 macOS。' >&2
  exit 2
fi
if command -v uv >/dev/null; then
  UV_PROJECT_ENVIRONMENT="$PWD/.venv" uv sync --locked --no-editable --python ">=3.11,<3.15"
elif python_cmd="$(command -v python3)" &&
     { [[ "$python_cmd" != /usr/bin/python3 ]] || /usr/bin/xcode-select -p >/dev/null 2>&1; } &&
     "$python_cmd" -c 'import sys; raise SystemExit(not ((3, 11) <= sys.version_info < (3, 15)))' 2>/dev/null; then
  "$python_cmd" -m venv .venv
  .venv/bin/python -m pip install .
else
  printf '%s\n' '未找到可用的 Python 3.11 至 3.14，也未找到 uv。' \
    '请安装 Python 后重新打开 install-macos.command。' \
    'Python 官方下载：https://www.python.org/downloads/macos/' >&2
  exit 2
fi
printf '配对命令：%q pair\nMCP 命令：%q mcp\n' "$PWD/.venv/bin/deskpet-platform" "$PWD/.venv/bin/deskpet-platform"
