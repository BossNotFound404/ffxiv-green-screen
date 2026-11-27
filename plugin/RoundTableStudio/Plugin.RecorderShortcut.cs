using Dalamud.Bindings.ImGui;
using Dalamud.Plugin.Ipc;
using Dalamud.Plugin.Ipc.Exceptions;

namespace RoundTableStudio;

public sealed partial class Plugin
{
    private readonly ICallGateProvider<bool> studioTransition;
    private readonly ICallGateSubscriber<bool> recorderSceneUsage;

    private bool RecorderLoaded => pi.InstalledPlugins.Any(plugin => plugin.InternalName == "VfxRecorder" && plugin.IsLoaded);

    private bool RecorderUsingScene
    {
        get
        {
            if (!recorderSceneUsage.HasFunction) return false;
            try { return recorderSceneUsage.InvokeFunc(); }
            catch (IpcNotReadyError) { return false; }
        }
    }

    private bool IsStudioTransitioning()
    {
        if (disposed || import.HasValue || switchRequested.HasValue || returnAfterModelRestore != null ||
            journey is Journey.AwaitEnable or Journey.Entering or Journey.Returning ||
            LocalMapReturning || localMapFrame != null || export != null || targetModel.IsRedrawing)
            return true;

        if (config.Pending is not { ShelfReleased: false } run) return false;
        if (!run.SeenStudio && !run.Closing && journey == Journey.Ready) return true;
        if (!OwnsStudioLayout(run)) return false;

        if (run.Closing)
            return run.Last.HasValue || run.MutedAmbient || run.OriginalLamps.Count > 0 || journey == Journey.Ready;

        return applyFirstColor || positionOnEntry ||
            run.Last is byte requested && Stage.Target() == requested && Stage.Displayed() != requested;
    }

    private void RequireSceneAvailable()
    {
        if (RecorderUsingScene) throw new InvalidOperationException("特效采集正在使用游戏画面。请等采集完成。");
    }

    private void DrawRecorderShortcut()
    {
        if (RecorderLoaded && ImGui.Button("打开特效采集"))
            FromPanel(() => commands.ProcessCommand("/vfxrec"));
    }
}
