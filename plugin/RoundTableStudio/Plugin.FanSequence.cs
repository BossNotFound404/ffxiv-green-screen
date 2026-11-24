using System.Diagnostics;
using Dalamud.Bindings.ImGui;

namespace RoundTableStudio;

public sealed partial class Plugin
{
    private readonly CancellationTokenSource fanSequenceLifetime = new();
    private FanSequenceJob? fanSequence;
    private FanSequenceRecording? fanSequenceRecording;
    private string? fanSequenceOutput;
    private string fanSequenceNotice = "";

    private sealed class FanSequenceJob(string notice, FanSequenceRecording? recording)
    {
        internal volatile string Notice = notice;
        internal volatile FanSequenceRecording? Recording = recording;
        internal Task<string?> Work = null!;
    }

    private bool FanSequenceBusy => fanSequence != null;
    private bool CanRecordFanSequence => client.IsLoggedIn && !ChangingArea && !import.HasValue &&
        journey is not (Journey.Entering or Journey.Returning) && export == null;

    private void DrawFanSequence()
    {
        ImGui.Separator();
        ImGui.BeginDisabled(FanSequenceBusy || !CanRecordFanSequence);
        if (ImGui.Button("录蓝扇 AE 序列")) FromPanel(() => BeginFanSequence());
        ImGui.EndDisabled();
        if (ImGui.IsItemHovered()) ImGui.SetTooltip("实验功能。先取好景，看到施放提示后放蓝扇，录制期间保持镜头不动。");

        if (fanSequence is { } active)
        {
            ImGui.TextWrapped(active.Notice);
            return;
        }
        if (fanSequenceNotice.Length != 0) ImGui.TextWrapped(fanSequenceNotice);
        if (fanSequenceOutput is { } output)
        {
            if (ImGui.Button("打开蓝扇 AE 目录")) FromPanel(() => OpenFanSequenceDirectory(output));
        }
        else if (fanSequenceRecording is { } recording)
        {
            if (ImGui.Button("打开蓝扇原始录制目录")) FromPanel(() => OpenFanSequenceDirectory(recording.Request.Session.Directory));
            if (recording.CanExport)
            {
                ImGui.SameLine();
                ImGui.BeginDisabled(export != null);
                if (ImGui.Button("重试整理蓝扇 AE 序列")) FromPanel(() => BeginFanSequence(recording));
                ImGui.EndDisabled();
            }
        }
    }

    private static void OpenFanSequenceDirectory(string path) =>
        Process.Start(new ProcessStartInfo(path) { UseShellExecute = true });

    private void BeginFanSequence(FanSequenceRecording? retry = null)
    {
        if (FanSequenceBusy || export != null) return;
        if (retry == null && !CanRecordFanSequence)
            throw new InvalidOperationException("请等角色载入且场景切换结束，再开始录制蓝扇。");
        if (retry != null && retry != fanSequenceRecording) return;

        var job = new FanSequenceJob(retry == null ? "正在查找本次游戏的蓝扇采集信号。" : "正在重新整理蓝扇 AE 序列。", retry);
        fanSequenceRecording = null;
        fanSequenceOutput = null;
        fanSequenceNotice = "";
        var cancellation = fanSequenceLifetime.Token;
        job.Work = Task.Run(() => RunFanSequenceAsync(job, cancellation), cancellation);
        fanSequence = job;
    }

    private static async Task<string?> RunFanSequenceAsync(FanSequenceJob job, CancellationToken cancellation)
    {
        if (job.Recording == null)
        {
            var session = FanSequenceBridge.CurrentSession();
            job.Recording = await FanSequenceBridge.RecordAsync(session,
                () => job.Notice = "已发出采集请求。现在施放蓝扇，录完前保持镜头和站位。", cancellation).ConfigureAwait(false);
        }
        var recording = job.Recording;
        if (!recording.CanExport) return null;

        job.Notice = "原始录制已保存，正在整理蓝扇 AE 序列。";
        var exe = FanSequenceExporter.ToolPath(typeof(Plugin).Assembly.Location);
        return await FanSequenceExporter.ExportAsync(exe, recording, cancellation).ConfigureAwait(false);
    }

    private void PollFanSequence()
    {
        if (fanSequence is not { Work.IsCompleted: true } done) return;
        fanSequence = null;
        fanSequenceRecording = done.Recording;
        try
        {
            fanSequenceOutput = done.Work.GetAwaiter().GetResult();
            if (fanSequenceOutput == null)
                fanSequenceNotice = $"原始录制未完成（{done.Recording!.Status}）。{done.Recording.Request.Report}";
            else if (done.Recording!.Status == "fan_draw_budget_exhausted")
                fanSequenceNotice = $"已从录到的有效片段整理 AE 序列。请用 {FanSequenceExporter.Importer} 导入。{fanSequenceOutput}";
            else
                fanSequenceNotice = $"蓝扇 AE 序列已就绪。请用 {FanSequenceExporter.Importer} 导入。{fanSequenceOutput}";
        }
        catch (Exception error)
        {
            fanSequenceNotice = done.Recording is { } recording
                ? $"AE 整理未完成。{error.GetBaseException().Message}\n本次原始报告已保留。{recording.Request.Report}"
                : $"蓝扇录制未完成。{error.GetBaseException().Message}";
            log.Warning(error, "Blue-fan sequence capture or export failed");
        }
    }

    private void DisposeFanSequence()
    {
        fanSequenceLifetime.Cancel();
        if (fanSequence is { } pending)
            _ = pending.Work.ContinueWith(task => { _ = task.Exception; fanSequenceLifetime.Dispose(); },
                CancellationToken.None, TaskContinuationOptions.ExecuteSynchronously, TaskScheduler.Default);
        else fanSequenceLifetime.Dispose();
    }
}
