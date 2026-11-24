@echo off
setlocal
if "%~1"=="" (
  echo Usage: build_replay_draw_packet.cmd OUTPUT_DIRECTORY
  echo Run from an x64 Visual Studio developer command prompt.
  exit /b 2
)
if not exist "%~1\." mkdir "%~1"
if errorlevel 1 exit /b 1
pushd "%~1"
if errorlevel 1 exit /b 1
cl /nologo /std:c++17 /EHsc /W4 /O2 /MT /utf-8 "%~dp0replay_draw_packet.cpp" /link d3d11.lib dxgi.lib /out:replay_draw_packet.exe
set status=%errorlevel%
popd
exit /b %status%
