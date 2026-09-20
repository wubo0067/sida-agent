#!/usr/bin/env bash
#
# sida-agent 一键启动脚本（Bash 版）
#
# 用法:
#   ./start.sh <子命令> [main.py 参数...]
#
# 子命令:
#   serve           启动 FastAPI HTTP 服务（默认 127.0.0.1:6173 /docs）
#   chat            多轮对话 REPL（会话落盘，Ctrl+C 退出）
#   ask             单轮问答
#   build           建库（PDF 提取 + 结构化抽取）
#   list-books      列出已导入教材（按逻辑书折叠）
#   list-versions   列出全部内容版本（= list-books --all-versions）
#   list-sessions   列出对话会话
#   help            显示帮助
#
# 常用示例:
#   ./start.sh serve
#   ./start.sh serve --host 0.0.0.0 --port 9000
#   ./start.sh serve --reload
#   ./start.sh chat
#   ./start.sh chat --session s-xxxx
#   ./start.sh ask --query "讲解可变电路的分析思路"
#   ./start.sh build --pdf /path/to/教材.pdf --start-page 1 --end-page 10 --subject physics --yes
#   ./start.sh list-books
#   ./start.sh list-versions
#   ./start.sh set-active-version 8860d10f858ba7eb
#   ./start.sh list-sessions
#
# 环境变量:
#   SKIP_SYNC=1  跳过 uv sync 依赖同步（依赖未变时启动更快）
#
# 子命令之后的参数原样透传给 `uv run python main.py`，
# 完整参数见 `uv run python main.py --help`。

set -euo pipefail

cd "$(dirname "$0")"

step() { printf '\033[36m==> %s\033[0m\n' "$*"; }
ok()   { printf '\033[32m    %s\033[0m\n' "$*"; }
warn() { printf '\033[33m    [warn] %s\033[0m\n' "$*"; }

# ---------- 启动字符画 ----------
show_banner() {
    printf '\n'
    printf '\033[35m%s\033[0m\n' \
        '   _     _                                    _   ' \
        '  ___(_) __| | __ _        __ _  __ _  ___ _ __ | |_ ' \
        ' / __| |/ _` |/ _` |_____ / _` |/ _` |/ _ \ '"'"'_ \| __|' \
        ' \__ \ | (_| | (_| |_____| (_| | (_| |  __/ | | | |_ ' \
        ' |___/_|\__,_|\__,_|      \__,_|\__, |\___|_| |_|\__|' \
        '                                |___/'
    printf '\n'
}
fail() { printf '\033[31m    [error] %s\033[0m\n' "$*" >&2; }

HELP_TEXT='sida-agent 一键启动脚本（Bash）

用法:
  ./start.sh <子命令> [main.py 参数...]

子命令:
  serve           启动 FastAPI HTTP 服务（默认 127.0.0.1:6173 /docs）
  chat            多轮对话 REPL（会话落盘，Ctrl+C 退出）
  ask             单轮问答
  build           建库（PDF 提取 + 结构化抽取）
  list-books      列出已导入教材（按逻辑书折叠，同一教材的多个内容版本并成一本）
  list-versions   列出已导入教材的全部内容版本（= list-books --all-versions）
  list-sessions   列出对话会话
  help            显示本帮助

常用示例:
  ./start.sh serve
  ./start.sh serve --host 0.0.0.0 --port 9000
  ./start.sh serve --reload
  ./start.sh chat
  ./start.sh chat --session s-xxxx
  ./start.sh ask --query "讲解可变电路的分析思路"
  ./start.sh build --pdf /path/to/教材.pdf --start-page 1 --end-page 10 --subject physics --yes
  ./start.sh list-books
  ./start.sh list-versions
  ./start.sh set-active-version 8860d10f858ba7eb   # 指定某本教材的当前版本
  ./start.sh list-sessions

环境变量:
  SKIP_SYNC=1  跳过 uv sync 依赖同步

子命令之后的参数原样透传给 `uv run python main.py`，
完整参数见 `uv run python main.py --help`。'

# ---------- 0. 帮助 / 无参数 ----------
if [[ $# -eq 0 || "${1:-}" == "help" || "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    show_banner
    echo "$HELP_TEXT"
    exit 0
fi

show_banner

CMD="$1"
shift

# ---------- 1. 前置检查 ----------
if ! command -v uv >/dev/null 2>&1; then
    fail '未检测到 uv，请先安装: https://docs.astral.sh/uv/'
    exit 1
fi
step "uv: $(command -v uv)"

if [[ ! -f .env ]]; then
    warn '.env 不存在，请先创建（必填项见 README 3.2: VISION_*/REASONING_*/EMBEDDING_*）。'
else
    ok '.env 已存在'
fi

if [[ "${SKIP_SYNC:-0}" != "1" ]]; then
    step '同步依赖 (uv sync)...'
    if ! uv sync; then
        fail 'uv sync 失败，请检查网络或 uv.lock。'
        exit 1
    fi
    ok '依赖同步完成'
else
    ok '跳过依赖同步 (SKIP_SYNC=1)'
fi

# ---------- 2. 子命令 → main.py 参数 ----------
case "$CMD" in
    serve)         UV_ARGS=(run python main.py --stage serve) ;;
    chat)          UV_ARGS=(run python main.py --stage chat) ;;
    ask)           UV_ARGS=(run python main.py --stage ask) ;;
    build)         UV_ARGS=(run python main.py --stage build) ;;
    list-books)    UV_ARGS=(run python main.py --list-books) ;;
    list-versions) UV_ARGS=(run python main.py --list-books --all-versions) ;;
    set-active-version) UV_ARGS=(run python main.py --set-active-version) ;;
    list-sessions) UV_ARGS=(run python main.py --stage chat --list) ;;
    *)
        fail "未知子命令: $CMD"
        echo
        echo "$HELP_TEXT"
        exit 1
        ;;
esac

step "执行: uv ${UV_ARGS[*]} $*"
uv "${UV_ARGS[@]}" "$@"
