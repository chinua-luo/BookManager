#!/bin/bash
# BookManager source setup: project-local dependencies, no system-wide changes.
set -eu
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd -P)"
cd "$PROJECT_DIR"

fail() { printf '\n初始化失败：%s\n' "$1" >&2; exit 1; }
case "${1:-}" in
  "") ;;
  --setup-only) ;;
  *) fail '未知参数。请直接运行脚本，或使用 --setup-only 仅安装和构建。' ;;
esac
[ "$(uname -s)" = Darwin ] || fail '此脚本仅支持 macOS。'
command -v python3 >/dev/null 2>&1 || fail '未找到 Python 3.10 或更新版本；请先从 python.org 安装并重试。'
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' || fail 'Python 版本过低，需要 3.10 或更新版本。'
command -v node >/dev/null 2>&1 || fail '未找到 Node.js；请先从 nodejs.org 安装 Node.js 22.12 或更新版本并重试。'
node -e 'const [a,b]=process.versions.node.split(".").map(Number); process.exit(a>22 || (a===22 && b>=12) ? 0 : 1)' || fail 'Node.js 版本过低，需要 22.12 或更新版本。'
command -v npx >/dev/null 2>&1 || fail '未找到 npx；请安装包含 npm 的 Node.js 后重试。'

printf 'BookManager：将在项目目录安装依赖并联网下载，首次运行可能需要几分钟。\n'
if [ ! -e "$PROJECT_DIR/.venv" ]; then
  printf '[1/4] 创建项目 Python 虚拟环境…\n'
  python3 -m venv "$PROJECT_DIR/.venv" || fail '无法创建虚拟环境，请检查 Python 的 venv 支持及目录权限。'
fi
PYTHON="$PROJECT_DIR/.venv/bin/python"
[ -x "$PYTHON" ] || fail '现有 .venv 中没有可用的 Python；请检查该环境，脚本不会自动覆盖。'
printf '[2/4] 安装 Python 依赖…\n'
"$PYTHON" -m pip install -r "$PROJECT_DIR/requirements.txt" || fail 'Python 依赖安装失败，请检查网络、磁盘空间与权限后重试。'
printf '[3/4] 安装前端依赖…\n'
cd "$PROJECT_DIR/electron"
npx --yes pnpm@9.15.9 install --frozen-lockfile || fail '前端依赖安装失败，请检查网络、磁盘空间与权限后重试。'
printf '[4/4] 构建前端…\n'
npx --yes pnpm@9.15.9 run build || fail '前端构建失败；请检查上方输出。macOS 原生模块签名不匹配时请使用官方 Node.js 发行版。'
if [ "${1:-}" = --setup-only ]; then
  printf '初始化和构建完成。\n'
  exit 0
fi
printf '正在启动 BookManager；请保留此终端窗口，关闭应用后可关闭终端。\n'
export BOOKMANAGER_PYTHON="$PYTHON"
export BOOKMANAGER_PROJECT_DIR="$PROJECT_DIR"
[ -x "$PROJECT_DIR/electron/node_modules/.bin/electron" ] || fail 'Electron 程序缺失，请检查前端依赖安装结果后重试。'
"$PROJECT_DIR/electron/node_modules/.bin/electron" . || fail 'Electron 启动失败，请检查上方输出。'
