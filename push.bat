@echo off
setlocal enabledelayedexpansion
title Git 一键初始化并提交

echo ============================================
echo    Git 仓库初始化、提交并推送到远程
echo ============================================
echo.

:: 检查 Git
where git >nul 2>nul
if errorlevel 1 (
    echo [错误] 未找到 Git，请先安装 Git 并添加到 PATH。
    pause
    exit /b 1
)

:: 处理已存在的 .git
if exist ".git" (
    echo [警告] 当前目录已是 Git 仓库。
    set /p choice="是否重新初始化（删除 .git）？(y/N): "
    if /i "!choice!"=="y" (
        echo 删除 .git ...
        rmdir /s /q .git
        if errorlevel 1 (
            echo 删除失败，请手动删除或使用管理员权限。
            pause
            exit /b 1
        )
    )
)

:: 初始化
if not exist ".git" (
    echo [1/5] 初始化仓库...
    git init
    if errorlevel 1 (
        echo 初始化失败。
        pause
        exit /b 1
    )
) else (
    echo [1/5] 仓库已存在，跳过初始化。
)

:: 添加文件
echo [2/5] 添加所有文件...
git add .
if errorlevel 1 (
    echo 添加文件失败。
    pause
    exit /b 1
)

:: 检查用户信息
echo [3/5] 检查用户信息...
git config --global user.name >nul 2>nul
if errorlevel 1 (
    echo 未设置 Git 用户信息。
    set /p user_name="请输入用户名: "
    set /p user_email="请输入邮箱: "
    git config --global user.name "!user_name!"
    git config --global user.email "!user_email!"
)

:: 提交
echo [4/5] 提交文件...
git commit -m "Initial commit"
if errorlevel 1 (
    echo 提交失败，可能没有文件变更。
    pause
    exit /b 1
)

:: 获取分支名
for /f "tokens=*" %%i in ('git symbolic-ref --short HEAD 2^>nul') do set "branch=%%i"
if "%branch%"=="" set "branch=master"

:: 远程仓库
echo [5/5] 配置远程并推送...
echo.
set "remote_url="
set /p "remote_url=请输入远程仓库 URL（直接回车跳过）: "
if "%remote_url%"=="" (
    echo 已跳过推送。
    goto :end
)

:: 自动添加 .git
if not "%remote_url:~-4%"==".git" set "remote_url=%remote_url%.git"

:: 移除已有的 origin
git remote get-url origin >nul 2>nul
if not errorlevel 1 (
    echo 移除已存在的 origin...
    git remote remove origin
)

:: 添加远程
echo 添加远程仓库...
git remote add origin "%remote_url%"
if errorlevel 1 (
    echo 添加失败，请检查 URL 和网络。
    pause
    exit /b 1
)

:: 推送
echo 推送至 %branch% 分支...
git push -u origin "%branch%"
if errorlevel 1 (
    echo.
    echo 推送失败。可能原因：
    echo   - 远程仓库非空（需先 pull）
    echo   - 认证失败（GitHub 需用 token）
    echo   - 网络问题
    echo.
    echo 可手动执行：git pull --rebase origin %branch% 后再 push
) else (
    echo.
    echo ============================================
    echo    推送成功！
    echo ============================================
)

:end
echo.
echo 按任意键退出...
pause >nul
exit /b 0