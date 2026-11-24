# 从源码准备可运行的绿幕

以下命令在解压后的仓库根目录运行，适用 Windows x64。拿到预编译程序包的使用者只需生成布景；改插件代码的开发者再编译 DLL。表中的路径均为示例。

## 从自己的客户端生成布景

安装 Python 3.10 或更新版本。确认本机 FF14 客户端的 `game\sqpack` 下有 `ffxiv`、`ex1` 和 `ex4` 数据目录。日常构建使用 Python 标准库。

```powershell
python build.py --studio --sqpack "D:\Games\FFXIV\game\sqpack"
```

程序从自己的游戏数据生成四个棚包，检查每个成品的 SHA-256，通过后一起写入 `output/studio/`。日常使用只需把所选的稳定棚放在 `RoundTableStudio.dll` 旁。已有同名输出时，用 `--out output/studio-2` 指向新目录。若游戏数据更新导致校验失败，反馈时请写明客户端版本与第一条错误。

| 棚包 | 适用场景 | 本次从国服客户端生成的 SHA-256 |
|---|---|---|
| `RoundTable_StableStage_v0.18.pmp` | 默认六色棚，含中灰 | `f2aae46b95b445cf6c1fc7688dd6b6248dd18787d16b6d2a41a2204d17ef3fb4` |
| `RoundTable_CaptureStage_v0.19.pmp` | 六色棚，可选的天幕渐变修正 | `24b47b21dbaac6dcffed0fce5648c103acbb50451fb7e87ce2f38ffddfd7054f` |
| `RoundTable_StableStage_v0.16.pmp` | 旧版五色稳定棚 | `6556cbdc8cff5793aef80e900ce53a273b77ec5252fafd746d71066120388bfe` |
| `RoundTable_CaptureStage_v0.17.pmp` | 旧版五色渐变试验棚 | `5824fe4a956ae8de59ddc3df65950686c4573e6ab3716c1d7c6f9351d452903b` |

这里列的是用 Windows Python 3.14.7 得到的整包校验值。另一种已核对的 ZIP 压缩结果有相同的包内文件，构建脚本和预览版插件也接受相应的整包校验值。[构建脚本](../build.py) · [插件认可的四组校验值](../plugin/RoundTableStudio/StagePackage.cs)

## 编译插件

安装 .NET 10 SDK，准备匹配游戏版本的 **Dalamud API 15** 程序集目录。所需的七个程序集列在工程文件中，包括 `Dalamud.dll`、`FFXIVClientStructs.dll` 和 `Lumina.dll`。它们由使用者从已安装的卫月环境提供。[插件工程](../plugin/RoundTableStudio/RoundTableStudio.csproj) · [依赖与来源](dependencies.md)

```powershell
dotnet build plugin/RoundTableStudio -c Release "-p:DalamudLibPath=D:\Dalamud\API15" -p:DebugType=None -p:DebugSymbols=false -p:IncludeSourceRevisionInInformationalVersion=false
```

编译产物位于 `plugin/RoundTableStudio/bin/Release/net10.0-windows/`。将 `RoundTableStudio.dll`、`RoundTableStudio.json` 和 `RoundTableStudio.deps.json` 保存在同一目录，按上一节放好布景，然后在卫月中加载 DLL。用户的摄影棚偏好由插件首次运行时创建。[安装步骤](../README.md#第一次进棚)

向他人分发程序时，将源码中的 `plugin/THIRD-PARTY-NOTICES.txt` 随程序附上，并按该版本的项目许可证提供许可文本及相应源码。[第三方声明](../plugin/THIRD-PARTY-NOTICES.txt)

经常构建的开发者可以复制环境模板并填写自己的路径，之后在同一 PowerShell 窗口运行以下命令。[环境模板](../config/local-build.example.ps1)

```powershell
Copy-Item config/local-build.example.ps1 config/local-build.ps1
# 修改 config/local-build.ps1 中的两个路径，再运行以下命令。
. ./config/local-build.ps1
python build.py --studio --out output/studio-local
dotnet build plugin/RoundTableStudio -c Release -p:DebugType=None -p:DebugSymbols=false -p:IncludeSourceRevisionInInformationalVersion=false
```

## 自行构建蓝扇采集器与 AE 整理器

Windows 程序包已经带有这两项。要从同标签源码重建，需在 x64 Visual Studio C++ 环境中，用 Windows SDK 和 ReShade 6.8.0 SDK 编译采集器与 D3D11 回放器，再用 64 位 Python 3.11 打包 AE 整理器。编译完成后，采集器放入 ReShade 扩展搜索目录，整个 `FanExport` 放在绿幕 DLL 旁，回放器放在 `FanExport` 内。具体命令、固定的 Python 依赖版本、离线测试和 ReShade 配置见[蓝扇采集器构建与设置](../tools/graphics/draw_probe/README.md)。

## 基本验证

以下五组 .NET 检查和一组 PowerShell 检查可在游戏关闭时执行。蓝扇检查需使用 Windows x64，它验证录制请求、时间限制、取消、整理器重试和结果匹配。测试数据适合核对流程，实际机位、画面和施放时机请在游戏中复核。需实际画面判断的颜色、粒子或骨架问题，请按游戏内清单测试。[游戏内复核](studio-plugin.md#实机复核)

```powershell
dotnet run --project plugin/checks/CaptureChecks.csproj -c Release
dotnet run --project plugin/matte-checks/MatteChecks.csproj -c Release
dotnet run --project plugin/skill-checks/SkillChecks.csproj -c Release
dotnet run --project plugin/export-flow-checks/ExportFlowChecks.csproj -c Release
dotnet run --project plugin/fan-sequence-checks/FanSequenceChecks.csproj -c Release
powershell.exe -NoProfile -File ./tools/checks/Test-AlphaExport.ps1
```

工具还保留恩欧五色棚的独立构建入口。复现这套较早布景时运行 `python build.py --sqpack "D:\Games\FFXIV\game\sqpack"`，再运行 `python verify.py output/packages` 检查包内文件。当目标是插件面板里的摄影棚时，使用本页第一节的 `--studio`。
