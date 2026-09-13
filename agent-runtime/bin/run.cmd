@echo off
setlocal

if defined DSH_NODE_RUNTIME if exist "%DSH_NODE_RUNTIME%\node_modules\@deepseek-ai\dsh\lib\bin.js" goto hasroot
if defined AGENT_HARNESS_ROOT goto harnessroot
if defined DSH_NODE_RUNTIME goto hasroot
set "NODE_ROOT=%USERPROFILE%\Desktop\deepseek-harness-master\python\sdk-runtime\src\deepseek_harness_runtime\runtime\node"
goto check

:harnessroot
set "SOURCE_ENTRY=%AGENT_HARNESS_ROOT%\apps\cli\lib\bin.js"
if exist "%SOURCE_ENTRY%" goto run_source
set "NODE_ROOT=%AGENT_HARNESS_ROOT%\python\sdk-runtime\src\deepseek_harness_runtime\runtime\node"
goto check
:hasroot
set "NODE_ROOT=%DSH_NODE_RUNTIME%"

:check
set "ENTRY=%NODE_ROOT%\node_modules\@deepseek-ai\dsh\lib\bin.js"
if exist "%ENTRY%" goto run
echo dsh dev launcher: runtime entry not found: %ENTRY% 1>&2
echo set DSH_NODE_RUNTIME to the Harness runtime node directory 1>&2
exit /b 2

:run
node "%ENTRY%" %*
exit /b %ERRORLEVEL%

:run_source
node "%SOURCE_ENTRY%" %*
exit /b %ERRORLEVEL%
