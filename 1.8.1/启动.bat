@echo off
chcp 936 >nul
title ADS1115 示波器
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 没有找到 python，请先安装 Python，并勾选 "Add Python to PATH"。
    pause
    exit /b 1
)

python -c "import PyQt5, pyqtgraph, serial, numpy" >nul 2>nul
if errorlevel 1 (
    echo 正在安装依赖，请稍候...
    python -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
    if errorlevel 1 (
        echo 镜像源安装失败，升级 pip 后重试...
        python -m pip install --upgrade pip -i https://pypi.tuna.tsinghua.edu.cn/simple
        python -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
    )
    if errorlevel 1 (
        echo 改用官方源重试...
        python -m pip install -r requirements.txt
        if errorlevel 1 (
            echo.
            echo [错误] 依赖安装失败，请检查网络后重试。
            pause
            exit /b 1
        )
    )
)

echo 正在启动 ADS1115 示波器...
python main.py
if errorlevel 1 (
    echo.
    echo 程序异常退出，请把上面的错误信息截图。
    pause
)
