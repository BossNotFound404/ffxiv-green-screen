using Dalamud.Bindings.ImGui;

namespace RoundTableStudio;

public sealed partial class Plugin
{
    private string vfxEditorNotice = "";

    private void DrawVfxEditorShortcut()
    {
        if (ImGui.Button("打开 VFXEditor"))
            FromPanel(() => vfxEditorNotice = commands.ProcessCommand("/vfxedit")
                ? ""
                : "未找到 /vfxedit。请用 /xlplugins 安装或启用 VFXEditor 正式版后重试。");

        if (vfxEditorNotice.Length != 0)
            ImGui.TextWrapped(vfxEditorNotice);
    }
}
