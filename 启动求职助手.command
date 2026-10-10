#!/bin/bash
# 求职助手一键启动：双击运行，自动启动后端并打开管理面板
# 关闭这个终端窗口即停止服务

PROJECT="$(cd "$(dirname "$0")" && pwd)"
# 8000 端口留给秋招看板
PORT=8001
URL="http://127.0.0.1:${PORT}/"

cd "$PROJECT/backend" || { echo "找不到项目目录：$PROJECT"; read -n 1; exit 1; }

# 服务已经在运行：直接打开页面
if curl -s -o /dev/null --max-time 2 "${URL}api/stats"; then
  echo "求职助手已在运行，正在打开页面…"
  open "$URL"
  exit 0
fi

# 端口被占用：如果是残留的求职助手进程就结束它，否则提示用户
PIDS=$(lsof -t -iTCP:${PORT} -sTCP:LISTEN 2>/dev/null)
if [ -n "$PIDS" ]; then
  if ps -o command= -p $PIDS | grep -q "job-hunter/.venv/bin/uvicorn"; then
    echo "发现残留的求职助手进程，正在结束…"
    kill $PIDS
    sleep 1
  else
    echo "端口 ${PORT} 被其他程序占用："
    ps -o pid=,command= -p $PIDS
    echo "请先关闭该程序后再试。按任意键退出…"
    read -n 1
    exit 1
  fi
fi

# 首次运行：自动创建虚拟环境并安装依赖
if [ ! -x "$PROJECT/.venv/bin/uvicorn" ]; then
  echo "首次运行，正在安装依赖（约 1-2 分钟）…"
  python3 -m venv "$PROJECT/.venv" && \
  "$PROJECT/.venv/bin/pip" install -q -r "$PROJECT/requirements.txt" || {
    echo "依赖安装失败，按任意键退出…"; read -n 1; exit 1;
  }
fi

# 服务就绪后自动打开浏览器
(
  for _ in $(seq 1 40); do
    if curl -s -o /dev/null --max-time 1 "${URL}api/stats"; then
      open "$URL"
      break
    fi
    sleep 0.5
  done
) &

echo "求职助手启动中：${URL}"
echo "关闭此窗口即停止服务"
echo "----------------------------------------"
exec "$PROJECT/.venv/bin/uvicorn" main:app --host 127.0.0.1 --port ${PORT} --reload --reload-dir "$PROJECT/backend"
