@echo off
setlocal
if "%~2"=="" (
  echo Usage: build_draw_probe.cmd OUTPUT_DIRECTORY RESHADE_SDK_INCLUDE
  echo Run from an x64 Visual Studio developer command prompt.
  exit /b 2
)
set "sdk_include=%~f2"
if not exist "%sdk_include%\reshade.hpp" (
  echo Cannot find reshade.hpp in the supplied SDK include directory.
  exit /b 2
)
if not exist "%~1\." mkdir "%~1"
if errorlevel 1 exit /b 1
pushd "%~1"
if errorlevel 1 exit /b 1
cl /nologo /std:c++17 /EHsc /W4 /O2 /MT /utf-8 /I"%sdk_include%" /c "%~dp0draw_probe.cpp"
if errorlevel 1 (
  popd
  exit /b 1
)
link /nologo /dll draw_probe.obj kernel32.lib user32.lib bcrypt.lib d3dcompiler.lib dxguid.lib /out:FFxivGreenScreenDrawProbe.addon64
set status=%errorlevel%
popd
exit /b %status%
