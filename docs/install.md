# Green Screen / 绿幕 v0.9.1

Windows x64 · 卫月（Dalamud）API 15

1. 从旧版升级时，先退棚回到旅馆，在卫月中停用原来的绿幕。将本版程序包解压到固定的新目录，保留 `RoundTableStudio.dll`、`RoundTableStudio.json` 和 `RoundTableStudio.deps.json`。
2. 安装并启用 Penumbra 1.7.2.1 和 Hyperborea 1.0.1.2。在 Penumbra 设置好模组目录，开启模组并保留可用的默认集合。[依赖安装地址与适配版本](https://github.com/BossNotFound404/ffxiv-green-screen/blob/v0.9.1/docs/studio-plugin.md)
3. Penumbra 已装兼容棚包时可直接使用。初次搭棚，请下载同版本源码，在源码根目录运行下面的命令，把示例路径换成自己的国服客户端数据目录。[下载 v0.9.1 源码](https://github.com/BossNotFound404/ffxiv-green-screen/archive/refs/tags/v0.9.1.zip)

   ```powershell
   python build.py --studio --sqpack "D:\Games\FFXIV\game\sqpack"
   ```

4. 将生成的 `output/studio/RoundTable_StableStage_v0.18.pmp` 放在 DLL 旁。这个棚包提供黑、红、绿、蓝、白和中灰。已有 v0.16 棚包继续提供原五色。
5. 输入 `/xlsettings` →「实验性」→「开发插件位置」，添加本版的 `RoundTableStudio.dll`。在 `/xlplugins` 启用 **Green Screen / 绿幕**。进入旅馆房间，在普通视角输入 `/rtstudio`，点「进入摄影棚」，选择棚色，再用 OBS 录制。拍完点「退棚」返回原房间。

序列录制已迁入独立的 VFX Recorder。安装后输入 `/vfxrec`，或点绿幕面板的「打开特效采集」。[特效采集项目](https://github.com/BossNotFound404/ffxiv-vfx-recorder)

本项目自有代码按 AGPL-3.0-or-later 提供。包内附有 `LICENSE` 和 `THIRD-PARTY-NOTICES.txt`。[本版对应源码](https://github.com/BossNotFound404/ffxiv-green-screen/tree/v0.9.1)
