#!/bin/bash
cd "$(dirname "$0")"

# 尝试寻找包含 tkinter 的 python 环境
# 优先使用 python3 (通常是用户安装的版本，如 Homebrew)，因为它可能包含更新的 Tcl/Tk
if python3 -c "import tkinter" &> /dev/null; then
    PYTHON_CMD="python3"
elif /usr/bin/python3 -c "import tkinter" &> /dev/null; then
    PYTHON_CMD="/usr/bin/python3"
else
    echo "错误：未找到包含 tkinter 的 Python 环境。"
    echo "请尝试安装 python-tk，或者使用系统自带的 python3。"
    read -p "按回车键退出..."
    exit 1
fi

echo "使用 Python: $PYTHON_CMD"
"$PYTHON_CMD" main.py > run.log 2>&1

if [ $? -ne 0 ]; then
    echo "程序异常退出，错误日志如下："
    cat run.log
    read -p "按回车键退出..."
fi
