# 前置插件与第三方来源

在 Windows 国服客户端使用绿幕的纯色摄影棚，需要能加载 API 15 插件的卫月（Dalamud）、Penumbra、Hyperborea，以及用自己客户端资源生成的 Stable Stage v0.18 六色棚包或已有的 v0.16 五色棚包。只拍角色施放的技能，用这套基础组合就够了。准备好旅馆和棚包后，输入 `/rtstudio` 并点击「进入摄影棚」。[安装来源、版本校验与操作步骤](studio-plugin.md)

## 按功能安装

| 想做什么 | 需要加载 | 绿幕怎样使用它 |
| --- | --- | --- |
| 一键进退棚、棚色切换、站位 | Penumbra 1.7.2.1、Hyperborea 1.0.1.2 | 绿幕调用 Penumbra 的插件接口（IPC）导入棚包、临时启用并在退棚时撤销。Hyperborea 负责载入场地、记录回程和返回旅馆。若它的 Enable 开关由绿幕打开，退棚后绿幕会恢复原状态。 |
| 拉远视距、接近垂直俯拍、隐藏本人名牌、角色淡出 | 基础组合 | 绿幕直接使用客户端的镜头和角色数据。调视距和俯仰范围都能在没有 Cammy 的情况下工作。 |
| 把当前目标替换为怪物或 Boss | 基础组合 | 绿幕从本机游戏表读取模型行，调用 Penumbra 重绘目标。Anamnesis 和 Brio 都无需安装。 |
| 自由移动镜头 | 基础组合，再加 Cammy 2.1.1.2 | 绿幕调用 Cammy 的 `/cammy freecam` 命令，读取其开关状态。 |
| 拍角色实际施放的技能 | 基础组合 | 绿幕截取黑棚原帧并生成供「添加」混合使用的发光图。 |
| 暂停一个地面预览，拍摄黑、白、绿三种底色 | 基础组合，再加 VFXEditor 1.9.4.6 和 Brio 0.8.0.12 | 从 VFXEditor 的 Spawn 窗口取唯一的 On Ground 预览，请 Brio 暂停和恢复该实例，由绿幕完成换色、截图和黑白复拍。恢复时预览可能从头播放。 |
| 在普通场地录蓝扇 AE 序列 | 卫月 API 15、ReShade 6.8.0 完整扩展版、程序包内的采集器及 `FanExport` | 扩展在 DX11 游戏内记录蓝扇绘制，整理器在 Windows 上另行运行。浏览分层和换底需要使用自己的 After Effects。普通摄影棚和以往单帧拍摄无需 ReShade。 |

Penumbra 通过 IPC 工作。Hyperborea、Cammy、VFXEditor 和 Brio 的适配还会检查已加载程序的构建标识。上游同名插件升级后，应重新核对兼容性。[适配版本及主 DLL 校验值](studio-plugin.md)

## 源码和数据的来源

| 组件 | 公开源码中用到了什么 | 上游依据 |
| --- | --- | --- |
| Hyperborea | 一键进退棚适配层调用用户已加载的插件，站位调整沿用其 Compass 的操作方式。随附其 BSD 3-Clause 全文。 | [源码及许可证](https://github.com/kawaii/Hyperborea/blob/d059aa30fb037939a90b7b7f0bf3a2ea6ab46e3a/LICENCE.md) |
| Penumbra | 棚包管理与目标重绘调用其 IPC。使用者自行安装。核对的发布源码根目录未提供整库许可证，本项目未打包 Penumbra 的实现或 DLL。 | [1.7.2.1 发布源码](https://github.com/xivdev/Penumbra/tree/1.7.2.1) |
| Cammy | Freecam 由已安装的 Cammy 执行。绿幕负责按钮、状态读取和退棚还原。核对的版本根目录未提供整库许可证，本项目未打包 Cammy 的实现或 DLL。 | [v2.1.1.2 发布源码](https://github.com/UnknownX7/Cammy/tree/v2.1.1.2) |
| Anamnesis | 八个 Boss 预设的 `BNpcBase` / `BNpcName` 行号对应关系来自其 NPC 表。角色名称、模型和骨骼在用户客户端读取。普通视角的天气记录选择、角色透明度对应的字段也参考了 Anamnesis。随附其 MIT 全文和上游署名。 | [对应的 NPC 表](https://github.com/imchillin/Anamnesis/blob/243411ba43eec07868ce23fbcfa730f3e71c75a8/Anamnesis/Data/NpcNames.json) · [MIT](https://github.com/imchillin/Anamnesis/blob/243411ba43eec07868ce23fbcfa730f3e71c75a8/LICENSE) |
| Brio | 切棚色参考了其同步更新天气与过渡时间的方式，绿幕自行实现这一操作。地面预览停帧才会调用已安装的 Brio 播放服务。Brio 的 GPLv3 实现由上游单独分发。 | [v0.8.0.12 源码](https://github.com/Etheirys/Brio/tree/v0.8.0.12) · [GPLv3](https://github.com/Etheirys/Brio/blob/v0.8.0.12/LICENSE) |
| VFXEditor | 地面预览由已安装的编辑器提供。仓库中的离线 AVFX 检查脚本另使用了上游的粒子类型、混合模式名称和顺序，并核对了粒子字段。随附其 MIT 全文。 | [枚举定义](https://github.com/0ceal0t/Dalamud-VFXEditor/blob/49012104303caae373ac4b37b385a3affedca472/VFXEditor/Formats/AvfxFormat/Base/Enums.cs) · [MIT](https://github.com/0ceal0t/Dalamud-VFXEditor/blob/49012104303caae373ac4b37b385a3affedca472/LICENSE) |

维护者可以从 [进退棚适配层](../plugin/RoundTableStudio/HyperboreaStudio.cs)、[Penumbra 棚包接口](../plugin/RoundTableStudio/Shelf.cs)、[Boss 行号映射](../plugin/RoundTableStudio/BossModelCatalog.cs) 和 [地面预览停帧](../plugin/RoundTableStudio/PreviewFreeze.cs) 核对具体实现。分发插件时，请将[第三方声明全文](../plugin/THIRD-PARTY-NOTICES.txt)与程序放在一起。

## 构建和离线工具

编译使用 Windows x64、.NET 10 SDK 及 API 15 对应的卫月开发程序集。下面七个 DLL 由本机卫月提供，项目按引用路径使用，发布包无需携带这些 DLL。[项目引用声明](../plugin/RoundTableStudio/RoundTableStudio.csproj)

| 程序集 | 用途与许可来源 |
| --- | --- |
| `Dalamud`、`Dalamud.Bindings.ImGui` | 卫月宿主和插件界面绑定。Dalamud 工程声明 AGPL-3.0-or-later，绑定的依赖见其子项目。[宿主工程及许可声明](https://github.com/goatcorp/Dalamud/blob/d0e93484f99fdb7d58b3a2f22152cf108f3486ff/Dalamud/Dalamud.csproj) · [ImGui 绑定工程](https://github.com/goatcorp/Dalamud/blob/d0e93484f99fdb7d58b3a2f22152cf108f3486ff/imgui/Dalamud.Bindings.ImGui/Dalamud.Bindings.ImGui.csproj) |
| `FFXIVClientStructs`、`InteropGenerator.Runtime` | 客户端结构及互操作支持，同属 FFXIVClientStructs 工程。[MIT](https://github.com/aers/FFXIVClientStructs/blob/80cb4a7b7e11a6702e2eb8839afd301a918c638e/LICENSE) |
| `Lumina`、`Lumina.Excel` | 从游戏资源读取表和行，为换模等功能提供数据。两个工程均声明 WTFPL v2。[Lumina 许可证](https://github.com/NotAdam/Lumina/blob/6447e12c74c6a6be2598933c0bba961a1e5e337c/LICENSE) · [Lumina.Excel 许可证](https://github.com/NotAdam/Lumina.Excel/blob/f79754d7659243cbc2b2a0d9ec55c66319aa42a9/LICENSE) |
| `HexaGen.Runtime` | 界面绑定所需的运行库。[MIT](https://github.com/HexaEngine/HexaGen/blob/50547a081b978f682328c6644b0e91a774c79512/LICENSE.txt) |

棚包生成脚本从使用者的 `game/sqpack` 读取游戏素材。本仓库提供脚本、资源标识和校验信息。棚包、模型、贴图、特效文件和实际游戏抓帧均留在本机。开发时曾参考 TerritoryViewer 的场景解析和 Physis 的环境字段定义，未引入其程序或源码文件。[TerritoryViewer 的解析代码](https://github.com/Enceladeum/TerritoryViewer/blob/1613660bb6a07585171301cad9350decbf8e6aad/DumpCore.cs) · [TerritoryViewer 的 MIT 许可证](https://github.com/Enceladeum/TerritoryViewer/blob/1613660bb6a07585171301cad9350decbf8e6aad/LICENSE) · [Physis 字段定义及 GPL-3.0-or-later 声明](https://github.com/redstrate/Physis/blob/c923a93e4724/src/envs/environment.rs)

研究绘制事件时，`tools/graphics/inspect_frame.py` 和 `match_tower_draws.py` 使用独立安装的 RenderDoc 的 Python 接口读取已有的本地抓帧。RenderDoc 1.47 用于开发时的离线对照。日常进棚、录制和静帧导出均无需安装 RenderDoc 或修改 GShade。[RenderDoc 1.47 许可证](https://github.com/baldurk/renderdoc/blob/v1.47/LICENSE.md) · [离线检查工具](../tools/graphics/)

## 蓝扇相关组件

- **ReShade 6.8.0。** 运行时由使用者安装支持完整扩展的版本。程序包中 `Native/FFxivGreenScreenDrawProbe.addon64` 是本项目的采集器，编译时使用上游 BSD-3-Clause SDK 头文件。SDK 声明随源码与程序包提供。[ReShade 下载](https://reshade.me/) · [对应 SDK 声明](../tools/graphics/draw_probe/ReShade-LICENSE.txt)
- **D3D11 回放。** 本项目的 `replay_draw_packet.exe` 在 Windows 上通过 D3D11/DXGI 回放采集的蓝扇绘制，逐笔检查原地面 RGBA。开发者使用 Visual Studio C++ Build Tools 和 Windows SDK 编译。[采集器与回放器构建](../tools/graphics/draw_probe/README.md#从源码构建)
- **AE 序列整理器。** 随程序包提供的 `FanExport` 自带 CPython、NumPy、Pillow 和 OpenEXR，使用 PyInstaller 打包；使用预编译程序时无需另装 Python。原始许可、版权声明与运行库说明见程序包内的 `THIRD-PARTY-NOTICES.txt`、`FanExport/third-party/README.txt` 及其指向的文件。After Effects 由使用者自行安装。[打包清单](../tools/graphics/GreenScreenFanExport.spec) · [AE 导入脚本](../tools/graphics/fan_sequence_import.jsx)
