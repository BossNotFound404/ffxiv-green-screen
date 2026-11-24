# 蓝扇采集器与 AE 序列整理器

插件面板上的「录蓝扇 AE 序列」会请求连续 240 个游戏显示帧，寻找包含前后空场的一次完整施放。当前支持的效果是 `vfx/monster/gimmick3/eff/n4gb_b_g02c0a1.avfx`。采集器保存游戏实际提交的绘制，整理器校验原画并生成可以换底的暗底、相乘、相加图层。

## 从源码构建

在 Windows x64 安装带 C++ 桌面开发组件与 Windows SDK 的 Visual Studio Build Tools，并单独取得 ReShade v6.8.0 的 `include` 目录。以下命令均从本项目根目录运行。构建两个原生程序时使用 **x64 Native Tools Command Prompt**。示例中的 SDK 路径请换成本机路径。[ReShade v6.8.0 源码](https://github.com/crosire/reshade/tree/v6.8.0/include)

```bat
tools\graphics\draw_probe\build_draw_probe.cmd build\Native "C:\SDK\reshade-6.8.0\include"
tools\graphics\draw_probe\build_replay_draw_packet.cmd build\FanExport
```

第一个目录会生成 `FFxivGreenScreenDrawProbe.addon64`，第二个会生成 `replay_draw_packet.exe`。回放器只依赖 Windows 的 D3D11 与 DXGI。分发采集器时请将 SDK 声明与它放在一起。[ReShade-LICENSE.txt](ReShade-LICENSE.txt)

Windows 整理器使用 64 位 Python 3.11。下面的命令创建独立的 Python 环境，使用这一版本验证过的依赖构建整理器，并把原生回放器放在它旁边。

```bat
py -3.11 -m venv .venv
.venv\Scripts\python -m pip install numpy==2.4.6 Pillow==13.0.0 OpenEXR==3.5.5 PyInstaller==6.16.0
.venv\Scripts\python -m PyInstaller tools\graphics\GreenScreenFanExport.spec
copy build\FanExport\replay_draw_packet.exe dist\FanExport\
dist\FanExport\GreenScreenFanExport.exe --help
```

将整个 `dist\FanExport` 目录放到 `RoundTableStudio.dll` 旁。其中 `_internal` 包含运行时、依赖包、许可文件和 AE 脚本模板。运行下面的离线测试可以检查选段、边缘裁切和底图选取。

```bat
set "PYTHONPATH=tools\graphics"
.venv\Scripts\python -m unittest discover -s tools\graphics\fixtures -p "test_fan*.py" -v
```

## 设置采集器

使用支持加载完整扩展的 ReShade 6.8.0 和 FFXIV DX11 客户端。把 `FFxivGreenScreenDrawProbe.addon64` 放入该客户端的 ReShade 扩展搜索目录，在其旁新建 UTF-8 文件 `GreenScreenDrawProbe.lab.ini`。先在本机固定磁盘上创建一个有足够空间的记录目录，再将它的绝对路径填入 `ReportDirectory`。下面的路径仅为示例。[ReShade 官方下载](https://reshade.me/)

```ini
[Capture]
ReportDirectory=D:\GreenScreenCaptures
FanCast=1
DrawPacket=1
ShaderCache=1
```

启动客户端后，记录目录中会出现该会话的 `*.json.ready.json`。可检查其中的 `fanSequenceRequestTokenBound` 和 `fanSequenceAlphaWitnessSupported` 是否均为 `true`。每次采集最多记录 8 GiB 原始像素，着色器缓存和导出的 AE 文件另占磁盘空间。

## 录制与换底

1. 打开 `/rtstudio`，选择静止且有纹理的地面，固定机位。让完整蓝扇落在画面中央的 512 × 512 像素内，扇尖和外弧四周都要留出余量。
2. 点击「录蓝扇 AE 序列」。单次手动施放时，等面板提示采集请求已发出，先留一段空场，施放后继续保持机位，直至录制结束。机位移动、特效触边、缺少前后空场或地面颜色不足时，整理器会给出失败原因。
3. 素材就绪后，打开「蓝扇 AE 目录」。在 AE 空工程中选择「文件 → 脚本 → 运行脚本文件」，打开导出目录中的 `导入蓝扇序列.jsx`。它会建立 32 bpc 线性 sRGB 工程。「01 实速预览」在 60 fps 时间线上按游戏实测时间播放。「02 逐帧核对与换底」中可替换最下方的底图，或打开最上方的同帧游戏原画来对照。

暗底来自同一笔扇面绘制的 GPU 回放。回放器会先在原底上逐字节核对结果，再测量扇面覆盖。相乘和相加图层使用中性色参考推算，部分电纹会随游戏原场景与后处理变化。每份 `sequence.json` 记录选段依据、底图与颜色校准结果。

已有完整原始记录时，可以离线运行以下命令。将示例参数换成自己的索引和新的输出目录，原始绘制包及像素文件须保留在索引对应的位置。

```bat
dist\FanExport\GreenScreenFanExport.exe --index capture\draws.json --out take-01
```
