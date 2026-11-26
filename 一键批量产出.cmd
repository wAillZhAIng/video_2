@echo off
setlocal EnableExtensions
chcp 65001 >nul
title AI Video Automation - Folder Production

set "PROJECT_ROOT=D:\人物一致性"
set "SCRIPT_ROOT=D:\脚本"
set "SOURCE_ROOT=%PROJECT_ROOT%\.local-runtime\publish\AI_Video_automation-20261119"
set "PYTHON=%PROJECT_ROOT%\.venv\Scripts\python.exe"
set "RUNNER=%SOURCE_ROOT%\scripts\one_click_folder_production.py"
set "LAUNCHER=%SOURCE_ROOT%\scripts\run_folder_production.ps1"
set "VIDEO_FACTORY_PROJECT_ROOT=%PROJECT_ROOT%"
set "VIDEO_FACTORY_PYTHON=%PYTHON%"
set "VIDEO_FACTORY_RUNTIME_DIR=%PROJECT_ROOT%\.local-runtime"
set "VIDEO_FACTORY_STATE_FILE=%VIDEO_FACTORY_RUNTIME_DIR%\batch_40_production_20261120\folder_production_state.json"
set "VIDEO_FACTORY_MANIFEST_DIR=%VIDEO_FACTORY_RUNTIME_DIR%\batch_40_production_20261120"
set "VIDEO_FACTORY_EXPECTED_COUNT=40"
set "VIDEO_FACTORY_SSH_CONFIG=%VIDEO_FACTORY_RUNTIME_DIR%\folder-production-ssh.txt"
set "VIDEO_FACTORY_SSH_KNOWN_HOSTS=%VIDEO_FACTORY_RUNTIME_DIR%\vast-known-hosts"
rem The startup button intentionally uses the verified Vast instance tunnel.
set "VIDEO_FACTORY_API_URL="

if not exist "%PYTHON%" (
  echo [错误] 未找到项目 Python：%PYTHON%
  pause
  exit /b 2
)
if not exist "%RUNNER%" (
  echo [错误] 未找到当前批量制作程序：%RUNNER%
  pause
  exit /b 2
)
if not exist "%LAUNCHER%" (
  echo [错误] 未找到当前启动编排：%LAUNCHER%
  pause
  exit /b 2
)
if not exist "%SCRIPT_ROOT%" (
  echo [错误] 脚本目录不存在：%SCRIPT_ROOT%
  pause
  exit /b 2
)
if not exist "%VIDEO_FACTORY_SSH_CONFIG%" (
  echo [错误] 未找到 Vast SSH 目标配置：%VIDEO_FACTORY_SSH_CONFIG%
  echo 请由管理员在本机私有运行目录配置 root@HOST 和 SSH 端口。
  pause
  exit /b 2
)
if not exist "%VIDEO_FACTORY_SSH_KNOWN_HOSTS%" (
  echo [错误] 未找到 SSH 主机密钥：%VIDEO_FACTORY_SSH_KNOWN_HOSTS%
  pause
  exit /b 2
)

echo.
echo ================================================================
echo   40 条 Word 自动视频制作
echo   Word 根目录：%SCRIPT_ROOT%
echo   Git 工作流：%SOURCE_ROOT%
echo   默认：MiniMax H3 Ref2VA + SMD4 Fast 4-step + 4K Real-ESRGAN 超分
echo   画幅：逐份读取 Word；云端服务、付费回退均关闭
echo ================================================================
echo.

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%LAUNCHER%"
set "RESULT=%ERRORLEVEL%"
echo.
if "%RESULT%"=="0" (
  echo 批量运行已结束。请核对私有任务台账和最终验收报告。
) else (
  echo 批量任务有失败或阻断项；状态保存在：%VIDEO_FACTORY_STATE_FILE%
)
pause
exit /b %RESULT%
