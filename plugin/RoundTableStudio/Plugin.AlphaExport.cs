using Dalamud.Bindings.ImGui;
using System.Diagnostics;

namespace RoundTableStudio;

public sealed partial class Plugin
{
    private static readonly (byte Color, string Label)[] Exposures =
        [(47, "黑棚"), (46, "白棚"), (48, "绿棚对照"), (47, "黑棚复拍"), (46, "白棚复拍")];
    private readonly StudioFrameReader frameReader;
    private readonly PreviewFreeze previewFreeze;
    private readonly ActorOpacity exportOpacity;
    private ExportJob? export;
    private (Capture Studio, byte Owned, byte Original)? deferredExportColor;
    private string exportNotice = "";
    private string? exportDirectory;

    private sealed class ExportJob(Capture studio, byte originalColor, uint viewport)
    {
        public readonly Capture Studio = studio;
        public readonly byte OriginalColor = originalColor;
        public readonly uint Viewport = viewport;
        public readonly CancellationTokenSource Stop = new();
        public readonly List<StudioFrame> Frames = [];
        public Task<StudioFrame>? Read;
        public Task<StudioMatteResult>? Save;
        public StudioHudScope? Hud;
        public byte? ExpectedColor;
        public long Deadline;
        public long NextSample;
        public bool SampledBackground;
        public StudioFrame? BlackHold;
        public StudioFrame? WhiteHold;
        public string? EffectPath;
        public SkillExposure? Skill;
        public long ReadStarted;
        public bool FreezeAcquired;
        public bool PreviousUiHide;
        public bool SceneReleased;
    }

    private void StartExport(Capture run, uint viewport, bool playerSkill = false)
    {
        if (export != null) return;
        RequireStudio(run);
        RestoreExportColor(run);
        if (positionOnEntry || applyFirstColor)
            throw new InvalidOperationException("请等进棚定位和首次切色完成。");
        if (targetModel.IsRedrawing)
            throw new InvalidOperationException("请等目标模型载入完成，再导出透明特效。");
        if (Stage.Target() is not byte original || !run.SupportsColor(original))
            throw new InvalidOperationException("请先选定一个摄影棚颜色。");
        var job = new ExportJob(run, original, viewport);
        export = job;
        exportDirectory = null;
        job.PreviousUiHide = pi.UiBuilder.DisableUserUiHide;
        try
        {
            if (playerSkill) job.Skill = new SkillExposure(47, 360);
            else
            {
                if (!previewFreeze.TryAcquire(out var reason)) throw new InvalidOperationException(reason);
                job.FreezeAcquired = true;
                job.EffectPath = previewFreeze.EffectPath;
            }
            job.Hud = StudioHudScope.Hide();
            pi.UiBuilder.DisableUserUiHide = true; // Keep the cancel button accessible during the clean viewport capture.
            exportOpacity.HideForExport(run.Character);
            if (playerSkill)
            {
                job.ExpectedColor = 47;
                Color(run, 47, temporary: true);
                job.NextSample = Environment.TickCount64 + 250;
                job.Deadline = Environment.TickCount64 + 90000;
                exportNotice = "正在准备黑棚，看到「请施放技能」后再施放。";
            }
            else BeginExposure(job);
        }
        catch (Exception error) { StopExport(error.GetBaseException().Message); }
    }

    private void BeginExposure(ExportJob job)
    {
        var (color, label) = Exposures[job.Frames.Count];
        job.ExpectedColor = color;
        Color(job.Studio, color, temporary: true);
        job.SampledBackground = false;
        job.NextSample = Environment.TickCount64 + 250;
        job.Deadline = Environment.TickCount64 + 15000;
        exportNotice = $"正在拍{label}（{job.Frames.Count + 1}/{Exposures.Length}），请保持镜头不动。";
    }

    private void TickExport()
    {
        if (export is not { } job) return;
        try
        {
            if (job.Save != null)
            {
                if (!job.Save.IsCompleted) return;
                var result = job.Save.GetAwaiter().GetResult();
                exportNotice = result.Summary;
                exportDirectory = result.DirectoryPath;
                FinishExport(job);
                return;
            }
            RequireStudio(job.Studio);
            var now = Environment.TickCount64;
            if (Stage.Target() != job.ExpectedColor || job.Hud?.IsHidden != true
                || exportOpacity.Read(job.Studio.Character) != 0)
                throw new InvalidOperationException("棚色、角色或界面状态已改变，已取消本次拍摄。");
            if (now >= job.Deadline)
                throw new InvalidOperationException(job.Skill != null ? "等候技能已超时，请确认技能在黑棚中可见。"
                    : $"{Exposures[job.Frames.Count].Label}的画面未在 15 秒内就绪。请检查取景边缘是否露出纯色背景。");
            if (now < job.NextSample || job.Read is { IsCompleted: false }) return;
            if (job.FreezeAcquired && !previewFreeze.Check(out var reason)) throw new InvalidOperationException(reason);
            if (job.Read == null)
            {
                job.ReadStarted = now;
                job.Read = frameReader.CaptureAsync(job.Viewport, job.Stop.Token);
                return;
            }
            var frame = job.Read.GetAwaiter().GetResult();
            job.Read = null;
            if (job.Skill is { } skill)
            {
                skill.Observe(frame, job.ReadStarted + (now - job.ReadStarted) / 2);
                exportNotice = skill.Message;
                if (skill.State == SkillExposureState.Failed) throw new InvalidOperationException(skill.Message);
                if (skill.State == SkillExposureState.Captured && skill.Frame is { } shot)
                {
                    ReleaseExportScene(job);
                    var output = Path.Combine(pi.GetPluginConfigDirectory(), "Exports");
                    var cancellation = job.Stop.Token;
                    job.Save = Task.Run(() => StudioMatte.ExportBlackSkill(output, shot.Width, shot.Height,
                        shot.Rgba, skill.ActualOffsetMs, cancellation), cancellation);
                    exportNotice = "已抓到角色技能，正在写入黑棚原帧和发光层。";
                }
                else
                {
                    job.ReadStarted = Environment.TickCount64;
                    job.Read = frameReader.CaptureAsync(job.Viewport, job.Stop.Token);
                }
                return;
            }
            if (job.Frames.Count > 0 && (job.Frames[0].Width != frame.Width || job.Frames[0].Height != frame.Height))
                throw new InvalidOperationException("拍摄途中游戏分辨率发生变化，请固定窗口后重试。");
            bool matches = frame.HasStudioBackground(job.ExpectedColor!.Value);
            if (!matches || !job.SampledBackground)
            {
                // Keep both frames from the first black/white exposures. A change
                // within one backdrop can be distinguished from a change after a switch.
                if (job.Frames.Count == 0) job.BlackHold = matches ? frame : null;
                if (job.Frames.Count == 1) job.WhiteHold = matches ? frame : null;
                job.SampledBackground = matches;
                job.NextSample = now + (matches && job.Frames.Count < 2 ? 600 : 240);
                return;
            }
            job.Frames.Add(frame);
            if (job.Frames.Count < Exposures.Length) { BeginExposure(job); return; }

            ReleaseExportScene(job);
            var root = Path.Combine(pi.GetPluginConfigDirectory(), "Exports");
            var token = job.Stop.Token;
            job.Save = Task.Run(() => StudioMatte.Export(root, frame.Width, frame.Height,
                job.Frames[0].Rgba, job.Frames[1].Rgba, job.Frames[2].Rgba, job.Frames[3].Rgba, job.Frames[4].Rgba, token,
                job.BlackHold?.Rgba, job.WhiteHold?.Rgba, job.EffectPath), token);
            exportNotice = "拍摄完成，正在生成透明图并对照实拍绿棚。";
        }
        catch (OperationCanceledException) { StopExport("已取消透明导出。"); }
        catch (Exception error) { StopExport(error.GetBaseException().Message); }
    }

    private void ReleaseExportScene(ExportJob job)
    {
        if (job.SceneReleased) return;
        job.SceneReleased = true;
        List<string> errors = [];
        void Restore(Action action)
        {
            try { action(); }
            catch (Exception error) { errors.Add(error.GetBaseException().Message); }
        }
        Restore(() =>
        {
            if (job.ExpectedColor is not byte owned || owned == job.OriginalColor
                || !ReferenceEquals(config.Pending, job.Studio) || job.Studio.Closing) return;
            deferredExportColor = (job.Studio, owned, job.OriginalColor);
            if (job.Studio.CanShoot(Layout, Character) && !client.IsGPosing && !InDuty && !ChangingArea)
                RestoreExportColor(job.Studio);
        });
        Restore(() => exportOpacity.Restore(Character));
        Restore(() => job.Hud?.Dispose());
        Restore(() => pi.UiBuilder.DisableUserUiHide = job.PreviousUiHide);
        if (job.FreezeAcquired) Restore(() => previewFreeze.Release());
        if (errors.Count != 0) throw new InvalidOperationException("恢复拍摄设置时遇到问题。" + string.Join(" ", errors));
    }

    private void RestoreExportColor(Capture run)
    {
        if (deferredExportColor is not { } saved) return;
        if (ReferenceEquals(saved.Studio, run))
        {
            var target = Stage.Target() ?? throw new InvalidOperationException("正在等候恢复拍摄前的棚色。");
            if (target == saved.Owned) Color(run, saved.Original, temporary: true);
        }
        deferredExportColor = null;
    }

    private void StopExport(string message)
    {
        if (export is not { } job) return;
        if (job.Save is { IsCompletedSuccessfully: true })
        {
            var finished = job.Save.Result;
            exportNotice = finished.Summary;
            exportDirectory = finished.DirectoryPath;
            FinishExport(job);
            return;
        }
        job.Stop.Cancel();
        try { ReleaseExportScene(job); }
        catch (Exception error) { message += " " + error.GetBaseException().Message; }
        exportNotice = message;
        // The disk writer commits a complete directory atomically. Let Tick collect its
        // actual outcome if cancellation raced that final commit.
        if (job.Save is { IsCompleted: false } && !disposed) return;
        FinishExport(job);
    }

    private void FinishExport(ExportJob job)
    {
        export = null;
        Task? pending = (Task?)job.Save ?? job.Read;
        if (pending == null) job.Stop.Dispose();
        else _ = pending.ContinueWith(done =>
        {
            _ = done.Exception;
            job.Stop.Dispose();
        }, CancellationToken.None, TaskContinuationOptions.ExecuteSynchronously, TaskScheduler.Default);
    }

    private void DrawExport(Capture run)
    {
        if (export == null)
        {
            if (ImGui.Button("拍角色技能"))
            {
                uint viewport = ImGui.GetMainViewport().ID;
                FromPanel(() => StartExport(run, viewport, playerSkill: true));
            }
            if (ImGui.IsItemHovered())
                ImGui.SetTooltip("等黑棚就绪后正常施放一次技能，自动截取黑棚原帧，生成适合「添加」混合的透明发光层。");
            ImGui.SameLine();
            if (ImGui.Button("拍地面预览"))
            {
                uint viewport = ImGui.GetMainViewport().ID;
                FromPanel(() => StartExport(run, viewport));
            }
            if (ImGui.IsItemHovered())
                ImGui.SetTooltip("暂停一个 VFXEditor 地面预览，隐藏本人和游戏界面，自动拍黑白绿棚。拍完后原先播放中的预览可能从头播放。");
        }
        else if (ImGui.Button("取消导出")) FromPanel(() => StopExport("已取消透明导出。"));
        if (exportDirectory is { } path)
        {
            ImGui.SameLine();
            if (ImGui.Button("打开结果文件夹")) FromPanel(() =>
                Process.Start(new ProcessStartInfo(path) { UseShellExecute = true }));
        }
        if (exportNotice.Length != 0) ImGui.TextWrapped(exportNotice);
    }
}
