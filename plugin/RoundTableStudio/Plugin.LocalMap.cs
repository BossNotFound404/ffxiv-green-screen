using System.Diagnostics;
using Dalamud.Bindings.ImGui;

namespace RoundTableStudio;

public sealed partial class Plugin
{
    private const string LocalMapColorsUnavailable = "切换棚色和依赖黑白棚的导出需要摄影棚。这里可以截取原帧。";

    private static bool LocalMapsEnabled =>
#if HYPERBOREA_LOCAL_MAPS
        true;
#else
        false;
#endif

    private sealed record LocalMapCapture(Capture Run, HyperboreaStudio.LocalMap Host, uint Territory, nint Layout);

    private LocalMapCapture? localMap;
    private long? localMapReturnRequestDeadline;
    private long? localMapReturnDeadline;
    private bool LocalMapReturning => localMapReturnRequestDeadline.HasValue || localMapReturnDeadline.HasValue;
    private ulong releasedShotOwner;
    private long nextLocalMapPoll;
    private uint localMapViewport;
    private LocalMapFrame? localMapFrame;
    private string localMapFrameNotice = "";
    private string? localMapFramePath;

    private sealed record LocalMapFrame(LocalMapCapture Owner, CancellationTokenSource Stop, Task<StudioFrame> Read)
    {
        internal Task<string>? Save;
    }

    private bool SameLocalMap(LocalMapCapture capture)
    {
        if (!client.IsLoggedIn || InDuty || ChangingArea || Character != capture.Run.Character) return false;
        var (territory, address) = Stage.LoadedLayout();
        return territory == capture.Territory && address == capture.Layout
            && ReferenceEquals(travel.DetectLocalMap(territory, capture.Host), capture.Host);
    }

    private void ObserveLocalMap()
    {
        if (!LocalMapsEnabled) return;
        RestoreReleasedShot();
        if (localMap is { } current)
        {
            if (SameLocalMap(current))
            {
                if (localMapReturnRequestDeadline is long requestDeadline)
                {
                    if (client.IsGPosing || Environment.TickCount64 >= requestDeadline)
                    {
                        localMapReturnRequestDeadline = null;
                        status = "已取消等待返回原旅馆。请等模型和普通视角恢复后重试。";
                        nextTick = 0;
                    }
                    else if (!targetModel.IsRedrawing)
                    {
                        localMapReturnRequestDeadline = null;
                        ReturnFromLocalMap(current);
                    }
                    return;
                }
                if (localMapReturnDeadline is long deadline && Environment.TickCount64 >= deadline)
                {
                    localMapReturnDeadline = null;
                    status = "暂未回到原旅馆。可重试，或打开 Hyperborea 检查。";
                    nextTick = 0;
                }
                return;
            }
            bool returning = localMapReturnDeadline.HasValue;
            CloseLocalMap(current, restoreScene: false);
            if (returning) status = "正在结束本次本地地图拍摄。";
            nextLocalMapPoll = 0;
        }
        if (!client.IsLoggedIn || InDuty || ChangingArea || client.IsGPosing || Character == 0
            || releasedShotOwner != 0
            || import != null || config.Pending is { ShelfReleased: false }
            || Environment.TickCount64 < nextLocalMapPoll)
            return;
        nextLocalMapPoll = Environment.TickCount64 + 350;
        var (territory, address) = Stage.LoadedLayout();
        if (travel.DetectLocalMap(territory) is not { } host || !IsInn(host.Inn)) return;
        localMap = new(new Capture { Character = Character, SeenStudio = true, ShelfReleased = true },
            host, territory, address);
        requestedPosition = objects.LocalPlayer?.Position ?? default;
        resumeExtendedRange = setPitchOnEntry = false;
        cameraNotice = "";
        localMapFrameNotice = "";
        nextTick = 0;
        status = "";
    }

    private void CloseLocalMap(LocalMapCapture capture, bool restoreScene)
    {
        if (localMapFrame is { Save: null } frame) CancelLocalMapFrame(frame);
        try
        {
            if (restoreScene)
                RestoreShot();
            else
                ReleaseShotOwnership(capture.Run.Character);
        }
        finally
        {
            localMap = null;
            localMapReturnRequestDeadline = null;
            localMapReturnDeadline = null;
            nameplate.Restore(false);
            if (restoreScene) targetModel.ForgetChange();
            RestoreLight(capture.Run);
            showShooting = false;
            nextTick = 0;
        }
    }

    private void ReleaseShotOwnership(ulong owner)
    {
        nameplate.Restore(false);
        targetModel.ReleaseLayout();
        if (releasedShotOwner == 0 && (opacity.Active || camera.HasChanges || freecam.Owned || targetModel.HasReleasedPlayerModel))
            releasedShotOwner = owner;
        RestoreReleasedShot();
    }

    private void RestoreReleasedShot()
    {
        if (releasedShotOwner == 0) return;
        if (!client.IsLoggedIn || Character != 0 && Character != releasedShotOwner)
        {
            opacity.Forget();
            camera.Forget();
            freecam.Forget();
            targetModel.ForgetChange();
            releasedShotOwner = 0;
            return;
        }
        if (Character == 0 || ChangingArea || InDuty || client.IsGPosing || Layout == 0
            || objects.LocalPlayer is not { Address: not 0 }) return;
        // The game can retain the player's objects across a local reload. These
        // components recheck the original instance and our applied state before undoing it.
        opacity.Restore(Character);
        camera.Restore();
        freecam.Restore(releaseOwned: true);
        targetModel.RestoreReleasedPlayerModel();
        releasedShotOwner = opacity.Active ? Character : 0;
    }

    private void DrawLocalMap(LocalMapCapture capture)
    {
        var run = capture.Run;
        ImGui.TextUnformatted("本地地图拍摄");
        ImGui.TextWrapped(LocalMapColorsUnavailable);
        if (!showShooting) return;
        var recorderUsingScene = RecorderUsingScene;
        ImGui.BeginDisabled(localMapFrame != null || recorderUsingScene || export != null);
        if (ImGui.Button("截取游戏原帧")) FromPanel(() => TakeLocalMapFrame(capture));
        ImGui.EndDisabled();
        if (ImGui.IsItemHovered())
            ImGui.SetTooltip("在技能出现时截取当前画面，保留地图背景。也可绑定 /rtstudio 截帧。");
        if (localMapFrameNotice.Length != 0) ImGui.TextWrapped(localMapFrameNotice);
        if (localMapFramePath is { } directory && ImGui.Button("打开原帧目录"))
            FromPanel(() => Process.Start(new ProcessStartInfo(directory) { UseShellExecute = true }));
        ImGui.BeginDisabled(recorderUsingScene);
        DrawShot(run);
        if (ImGui.Button("返回原旅馆")) FromPanel(() => ReturnFromLocalMap(capture));
        ImGui.EndDisabled();
    }

    private void ReturnFromLocalMap(LocalMapCapture capture)
    {
        RequireShooting(capture.Run);
        RestoreShot();
        if (targetModel.IsRedrawing)
        {
            localMapReturnRequestDeadline = Environment.TickCount64 + 10000;
            showShooting = false;
            status = "正在还原目标模型，完成后会返回原旅馆。";
            return;
        }
        if (localMapFrame is { Save: null } frame) CancelLocalMapFrame(frame);
        RestoreLight(capture.Run);
        travel.ReturnFromLocalMap(capture.Host, capture.Territory, capture.Layout);
        localMapReturnDeadline = Environment.TickCount64 + 30000;
        showShooting = false;
        status = "正在返回原旅馆…";
    }

    private void TakeLocalMapFrame(LocalMapCapture capture)
    {
        RequireShooting(capture.Run);
        if (localMapFrame != null || export != null) return;
        var stop = new CancellationTokenSource();
        localMapFrame = new(capture, stop, frameReader.CaptureAsync(localMapViewport, stop.Token));
        localMapFrameNotice = "正在读取游戏画面。";
    }

    private void PollLocalMapFrame()
    {
        if (localMapFrame is not { } frame) return;
        try
        {
            if (frame.Save is { } write)
            {
                if (!write.IsCompleted) return;
                localMapFramePath = write.GetAwaiter().GetResult();
                localMapFrameNotice = "已保存游戏原帧。";
                CancelLocalMapFrame(frame);
                return;
            }
            RequireShooting(frame.Owner.Run);
            if (!frame.Read.IsCompleted) return;
            var image = frame.Read.GetAwaiter().GetResult();
            var output = Path.Combine(pi.GetPluginConfigDirectory(), "Exports");
            var token = frame.Stop.Token;
            frame.Save = Task.Run(() => StudioMatte.ExportGameFrame(output, image.Width, image.Height, image.Rgba, token), token);
        }
        catch (Exception error)
        {
            localMapFrameNotice = error.GetBaseException().Message;
            CancelLocalMapFrame(frame);
        }
    }

    private void CancelLocalMapFrame(LocalMapFrame frame)
    {
        localMapFrame = null;
        frame.Stop.Cancel();
        Task pending = (Task?)frame.Save ?? frame.Read;
        _ = pending.ContinueWith(done =>
        {
            _ = done.Exception;
            frame.Stop.Dispose();
        }, CancellationToken.None, TaskContinuationOptions.ExecuteSynchronously, TaskScheduler.Default);
    }
}
