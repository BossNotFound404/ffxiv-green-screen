using System.Diagnostics;
using System.Text;
using System.Text.Json;

namespace RoundTableStudio;

internal static class FanSequenceExporter
{
    internal const string Importer = "导入蓝扇序列.jsx";
    private const string ExeName = "GreenScreenFanExport.exe";
    private const int LogLimit = 4096;
    private static readonly TimeSpan ExportTimeout = TimeSpan.FromMinutes(15);

    internal static string ToolPath(string pluginAssemblyPath) =>
        Path.Combine(Path.GetDirectoryName(pluginAssemblyPath)!, "FanExport", ExeName);

    internal static string OutputPath(FanSequenceRecording recording) =>
        Path.Combine(recording.Request.Session.Directory, Path.GetFileNameWithoutExtension(recording.Request.Report) + "-AE");

    internal static async Task<string> ExportAsync(string exe, FanSequenceRecording recording, CancellationToken cancellation)
    {
        if (!recording.CanExport || FanSequenceBridge.ReadCompleted(recording.Request) != recording)
            throw new InvalidDataException("蓝扇原始序列已改变或不可整理。");

        string output = OutputPath(recording);
        if (Directory.Exists(output)) return ConfirmOutput(recording, output);
        if (!File.Exists(exe))
            throw new FileNotFoundException($"插件目录缺少蓝扇 AE 整理工具。{exe}");

        var start = new ProcessStartInfo(exe)
        {
            WorkingDirectory = Path.GetDirectoryName(exe)!,
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            StandardOutputEncoding = Encoding.UTF8,
            StandardErrorEncoding = Encoding.UTF8,
        };
        start.ArgumentList.Add("--index");
        start.ArgumentList.Add(recording.Request.Report);
        start.ArgumentList.Add("--out");
        start.ArgumentList.Add(output);

        using var deadline = CancellationTokenSource.CreateLinkedTokenSource(cancellation);
        deadline.CancelAfter(ExportTimeout);
        deadline.Token.ThrowIfCancellationRequested();
        using var process = Process.Start(start) ?? throw new InvalidOperationException("无法启动蓝扇 AE 整理工具。");
        Task<string> standardOutput = ReadDiagnosticsAsync(process.StandardOutput, deadline.Token);
        Task<string> standardError = ReadDiagnosticsAsync(process.StandardError, deadline.Token);
        try
        {
            await Task.WhenAll(process.WaitForExitAsync(deadline.Token), standardOutput, standardError).ConfigureAwait(false);
        }
        catch (OperationCanceledException)
        {
            if (!process.HasExited)
            {
                try { process.Kill(entireProcessTree: true); }
                catch (InvalidOperationException) when (process.HasExited) { }
            }
            using var exitWait = new CancellationTokenSource(TimeSpan.FromSeconds(3));
            await process.WaitForExitAsync(exitWait.Token).ConfigureAwait(false);
            cancellation.ThrowIfCancellationRequested();
            throw new TimeoutException("整理蓝扇 AE 序列超过 15 分钟。");
        }
        if (process.ExitCode != 0)
        {
            string diagnostic = standardError.Result;
            if (string.IsNullOrWhiteSpace(diagnostic)) diagnostic = standardOutput.Result;
            throw new InvalidOperationException(string.IsNullOrWhiteSpace(diagnostic)
                ? $"蓝扇 AE 整理工具退出，代码 {process.ExitCode}。" : diagnostic.Trim());
        }
        return ConfirmOutput(recording, output);
    }

    internal static string ConfirmOutput(FanSequenceRecording recording, string output)
    {
        var directory = new DirectoryInfo(output);
        if (!directory.Exists || (directory.Attributes & FileAttributes.ReparsePoint) != 0)
            throw new InvalidDataException($"找不到本次录制的 AE 整理目录。{output}");
        using var json = JsonDocument.Parse(FanSequenceBridge.ReadFile(Path.Combine(output, "sequence.json"), 16 * 1024 * 1024));
        JsonElement manifest = json.RootElement;
        if (FanSequenceBridge.ReadString(manifest, "schema") != "fan-ae-sequence-v1" ||
            !FanSequenceBridge.MatchesSession(manifest, recording.Request.Session, recording.Request.Shot) ||
            FanSequenceBridge.ReadString(manifest, "nativeIndexFile") != Path.GetFileName(recording.Request.Report) ||
            !string.Equals(FanSequenceBridge.ReadString(manifest, "nativeIndexSha256"), recording.IndexSha256, StringComparison.OrdinalIgnoreCase) ||
            !HasArtifact(Path.Combine(output, Importer)) || !HasArtifact(Path.Combine(output, "clean-plate.png")))
            throw new InvalidDataException($"AE 整理结果与本次蓝扇录制不符或缺少文件。{output}");
        return output;
    }

    private static bool HasArtifact(string path)
    {
        var file = new FileInfo(path);
        return file.Exists && (file.Attributes & FileAttributes.ReparsePoint) == 0 && file.Length > 0;
    }

    private static async Task<string> ReadDiagnosticsAsync(StreamReader reader, CancellationToken cancellation)
    {
        var output = new StringBuilder();
        var buffer = new char[1024];
        int count;
        while ((count = await reader.ReadAsync(buffer.AsMemory(), cancellation).ConfigureAwait(false)) > 0)
        {
            output.Append(buffer, 0, count);
            if (output.Length > LogLimit) output.Remove(0, output.Length - LogLimit);
        }
        return output.ToString();
    }
}
