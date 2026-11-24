# Green Screen / 绿幕

Green Screen 是 FF14 的摄影棚插件。在旅馆输入 `/rtstudio`，点「进入摄影棚」，就能用普通视角在黑、白、中灰、红、绿、蓝六种底色间切换。已有五色布景也可以继续用。**v0.9.0-preview.1** 为 Windows x64、卫月 API 15 增加了实验性的「录蓝扇 AE 序列」，可在普通场地录下指定蓝扇，交给 After Effects 换底或逐帧查看。

| 下载 | 适合谁 |
|---|---|
| [预览版 Windows 程序包](https://github.com/BossNotFound404/ffxiv-green-screen/releases/download/v0.9.0-preview.1/GreenScreen-v0.9.0-preview.1-win-x64.zip) · [同标签源码 ZIP](https://github.com/BossNotFound404/ffxiv-green-screen/archive/refs/tags/v0.9.0-preview.1.zip) · [预览版说明](https://github.com/BossNotFound404/ffxiv-green-screen/releases/tag/v0.9.0-preview.1) | 想用中灰棚，或试录蓝扇。完整解压 `GreenScreen-v0.9.0-preview.1-win-x64.zip`。初次搭棚要用同标签源码和自己的客户端生成布景。 |
| [稳定版 v0.8.1](https://github.com/BossNotFound404/ffxiv-green-screen/releases/tag/v0.8.1) | 沿用原五色棚和单帧拍摄流程。 |

## 我需要装哪些插件？

| 要做什么 | 需要加载 | 在绿幕里怎么用 |
|---|---|---|
| 进退棚、换底色、拍角色技能、换目标模型 | **Penumbra 1.7.2.1** 和 **Hyperborea 1.0.1.2** | Penumbra 管理布景并刷新模型。Hyperborea 加载本地场地。进棚后可直接拉远镜头、近垂直俯拍、调整站位、淡出角色、隐藏本人名牌。 |
| 用自由镜头取景 | 基础组合，加 **Cammy 2.1.1.2** | 在「拍摄设置」点「开启 Freecam」。 |
| 暂停并拍摄一件地面预览 | 基础组合，加 **VFXEditor 1.9.4.6 正式版** 和 **Brio 0.8.0.12** | 用 VFXEditor 放置 On Ground 预览，在绿幕里点「拍地面预览」。 |
| 在普通场地录蓝扇 AE 序列 | 卫月、绿幕、**ReShade 6.8.0 完整扩展版**以及程序包中的采集器和整理器 | 固定机位，看到面板提示后施放指定蓝扇。无需进棚。普通进退棚、换色、录屏和单帧拍摄均无需 ReShade。 |

以上是已适配的版本。部分对接会核对插件的具体构建，升级后请先看面板提示。安装来源、文件校验及各项目的代码用在哪，都有逐项说明。[前置插件安装地址](docs/studio-plugin.md#前置插件从哪里安装) · [实现来源与许可证](docs/dependencies.md)

## 第一次进棚

1. **准备插件。** 在卫月安装并启用上表两项基础插件。首次打开 Penumbra 时设置模组目录，启用模组并保留可用的默认集合。取得本版程序包后解压到固定目录。只有源码时，先按构建指南生成插件 DLL。[从源码编译](docs/build.md)
2. **准备摄影棚布景。** 已在 Penumbra 安装对应稳定棚的朋友可直接复用。首次使用，从自己的客户端生成棚包，把 `RoundTable_StableStage_v0.18.pmp` 放在 `RoundTableStudio.dll` 旁，即可使用六种棚色。只有 v0.16 旧棚时会显示原五色。[本机生成命令](docs/build.md#从自己的客户端生成布景)
3. **让卫月加载绿幕。** 输入 `/xlsettings` →「实验性」→「开发插件位置」，添加 `RoundTableStudio.dll` 并保存。输入 `/xlplugins`，启用 **Green Screen / 绿幕**。
4. **试拍一次。** 在旅馆房间的普通视角输入 `/rtstudio`，点「进入摄影棚」。插件会导入布景，并按需开启 Hyperborea 面板内的 Enable。点两种棚色试拍，再点「退棚」。确认回到原旅馆，原先关闭的 Enable 会随之恢复。[棚内操作与常见问题](docs/studio-plugin.md)

## 当前可以拍出什么？

| 拍摄方式 | 得到的素材 |
|---|---|
| 在纯色棚录制整段动作 | 用平时的录屏工具录制纯色底影片，用于后期抠色。 |
| 点「拍角色技能」后实际施放一次 | 一张黑棚原帧 `black.png` 和可用「添加」混合叠加的发光层 `glow-add.png`。 |
| 点「拍地面预览」 | 从黑白底估算单帧透明度，用绿棚实拍和黑白复拍检查。通过时生成 `effect.png`；有误差时保留候选图和报告。 |
| 点「录蓝扇 AE 序列」 | 原画、暗底、相乘、相加图层、空场底图和 AE 导入脚本。需安装蓝扇采集扩展，完整扇形须落入画面中央的 512 × 512 像素区域。[录制、实速播放与换底](docs/studio-plugin.md#录蓝扇-ae-序列) |

换模支持八个常用 Boss 预设，也支持输入本机游戏的 ModelChara 行号。更换目标后仍可还原上一次替换的角色。不同骨架的动作表现和还原效果欢迎实测反馈。[换模与拍摄操作](docs/studio-plugin.md#平时怎么拍) · [透明图和发光层的用法](docs/alpha-export.md)

## 蓝扇预览版已验证到哪里

用预览版整理器处理一份已有的游戏实录，选出包含一次完整蓝扇的 **140 个连续显示帧，时长 2.161 秒**。同一记录上，分层叠回后的特效区域平均颜色误差约 **5.25/255**。AE 的 60 fps 实速合成按游戏记录的显示时刻播放，每秒独立画面的数量取决于采集时的实际帧率。换到另一种地面后，电纹与高光颜色仍可能有偏差。[采集与分层说明](tools/graphics/draw_probe/README.md)

游戏内从点击新版按钮、看到提示、手动施放，到打开 AE 结果目录的整条流程仍待实机验收。反馈时请写明卡在哪一步、是否完成录制，以及换底后哪一处颜色或边缘有差异。[参与实测](CONTRIBUTING.md)

## 许可证

本项目自有代码按 **GNU Affero General Public License 第 3 版或任何更新版本（AGPL-3.0-or-later）** 授权。第三方代码与数据保留各自的版权和许可。游戏素材的权利归原权利人所有。[许可证全文](LICENSE) · [第三方声明](plugin/THIRD-PARTY-NOTICES.txt)
