using System.Diagnostics;
using System.Globalization;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace RoundTableStudio;

internal sealed record FanProbeSession(int ProcessId, DateTime StartedUtc, string Directory)
{
    internal const string Host = "ffxiv_dx11.exe";
    internal string Stem => FormattableString.Invariant($"draws-{ProcessId}-{StartedUtc.ToFileTimeUtc()}");
}

internal sealed record FanSequenceRequest(FanProbeSession Session, int Shot, string Token, string Report)
{
    internal string ArmPath => Report + ".arm";
}

internal sealed record FanSequenceRecording(FanSequenceRequest Request, string Status, string IndexSha256)
{
    internal bool CanExport => Status is "complete" or "fan_draw_budget_exhausted";
}

internal static class FanSequenceBridge
{
    internal const int FrameCount = 240;
    private const string Addon = "FFxivGreenScreenDrawProbe.addon64";
    private const string Profile = "GreenScreenDrawProbe.lab.ini";
    private const string ReadySuffix = ".json.ready.json";
    private const string NativeSchema = "fan-native-sequence-v1";
    private static readonly TimeSpan ReadyTimeout = TimeSpan.FromSeconds(3);
    private static readonly TimeSpan CaptureTimeout = TimeSpan.FromSeconds(90);
    private static readonly TimeSpan PollDelay = TimeSpan.FromMilliseconds(120);

    internal static FanProbeSession CurrentSession()
    {
        using var process = Process.GetCurrentProcess();
        if (!string.Equals(Path.GetFileName(Environment.ProcessPath), FanProbeSession.Host, StringComparison.OrdinalIgnoreCase))
            throw new InvalidOperationException("蓝扇采集需要在 FFXIV 的 DX11 客户端中使用。");
        var modules = process.Modules.Cast<ProcessModule>().ToArray();
        var probe = Array.FindAll(modules, module => module.ModuleName.Equals(Addon, StringComparison.OrdinalIgnoreCase));
        if (probe.Length != 1 || !Array.Exists(modules, module => module.ModuleName.Equals("d3d11.dll", StringComparison.OrdinalIgnoreCase)))
            throw new InvalidOperationException("本次游戏没有载入可用的 ReShade 蓝扇采集扩展。请配置扩展后重新启动游戏。");
        string profile = Path.Combine(Path.GetDirectoryName(probe[0].FileName)!, Profile);
        if (!File.Exists(profile)) throw new InvalidOperationException($"找不到采集扩展的设置文件。{profile}");
        return new(process.Id, process.StartTime.ToUniversalTime(), ReadReportDirectory(profile));
    }

    internal static string ReadReportDirectory(string profile)
    {
        string? directory = null;
        bool capture = false;
        var text = new UTF8Encoding(false, true).GetString(ReadFile(profile, 128 * 1024));
        using var reader = new StringReader(text.TrimStart('\uFEFF'));
        while (reader.ReadLine() is { } line)
        {
            line = line.Trim(' ', '\t', '\r');
            if (line.Length == 0 || line[0] is ';' or '#') continue;
            if (line[0] == '[')
            {
                if (line[^1] != ']') throw new InvalidDataException("采集扩展设置文件的分节无效。");
                capture = line[1..^1].Trim(' ', '\t').Equals("capture", StringComparison.OrdinalIgnoreCase);
                continue;
            }
            if (!capture) continue;
            int equals = line.IndexOf('=');
            if (equals < 0) throw new InvalidDataException("采集扩展设置文件有无效的设置项。");
            if (!line[..equals].Trim(' ', '\t').Equals("ReportDirectory", StringComparison.OrdinalIgnoreCase)) continue;
            if (directory != null) throw new InvalidDataException("采集扩展设置文件重复填写了报告目录。");
            directory = line[(equals + 1)..].Trim(' ', '\t');
        }
        if (directory == null || !Path.IsPathFullyQualified(directory))
            throw new InvalidDataException("请在采集扩展设置中填写已存在的本机报告目录。");

        return ResolveReportDirectory(Path.GetFullPath(directory));
    }

    private static string ResolveReportDirectory(string path)
    {
        var root = Path.GetPathRoot(path)!;
        RequireFixedDrive(root);
        string resolved = root;
        foreach (string part in path[root.Length..].Split(Path.DirectorySeparatorChar, StringSplitOptions.RemoveEmptyEntries))
        {
            var directory = new DirectoryInfo(Path.Combine(resolved, part));
            FileAttributes attributes = directory.Attributes;
            if (attributes == (FileAttributes)(-1) || (attributes & FileAttributes.Directory) == 0)
                throw new InvalidDataException($"采集扩展的报告目录不存在。{path}");
            resolved = directory.FullName;
            if ((attributes & FileAttributes.ReparsePoint) != 0)
            {
                var target = directory.ResolveLinkTarget(returnFinalTarget: true)
                    ?? throw new InvalidDataException("无法找到报告目录链接指向的位置。");
                resolved = Path.GetFullPath(target.FullName);
                RequireFixedDrive(Path.GetPathRoot(resolved)!);
            }
        }
        return resolved;
    }

    private static void RequireFixedDrive(string root)
    {
        if (root.Length != 3 || root[1] != ':' || new DriveInfo(root).DriveType != DriveType.Fixed)
            throw new InvalidDataException("蓝扇采集报告目录须位于本机固定磁盘。");
    }

    internal static async Task<FanSequenceRecording> RecordAsync(FanProbeSession session, Action requested, CancellationToken cancellation)
    {
        var readyWait = Stopwatch.StartNew();
        FanSequenceRequest? request;
        do
        {
            cancellation.ThrowIfCancellationRequested();
            request = FindReady(session);
            if (request != null) break;
            await Task.Delay(PollDelay, cancellation).ConfigureAwait(false);
        } while (readyWait.Elapsed < ReadyTimeout);
        if (request == null)
            throw new InvalidOperationException($"采集扩展没有发布可用的新镜头。请确认 ReShade 中已启用蓝扇采集。{session.Directory}");

        cancellation.ThrowIfCancellationRequested();
        PublishRequest(request);
        requested();
        return await AwaitReportAsync(request, CaptureTimeout, cancellation).ConfigureAwait(false);
    }

    internal static FanSequenceRequest? FindReady(FanProbeSession session)
    {
        var candidate = NewestReadyFile(session);
        if (candidate == null) return null;
        string report = candidate.Value.Ready[..^".ready.json".Length];
        if (File.Exists(report)) return null;
        if (File.Exists(report + ".arm"))
            throw new InvalidOperationException("采集扩展已有采集请求，请等当前录制结束。");

        var file = new FileInfo(candidate.Value.Ready);
        if (file.LastWriteTimeUtc < session.StartedUtc)
            throw new InvalidDataException("采集扩展的准备信号早于本次游戏启动。");
        using var ready = JsonDocument.Parse(ReadFile(file.FullName, 8192));
        JsonElement notice = ready.RootElement;
        if (!MatchesSession(notice, session, candidate.Value.Shot) || ReadString(notice, "configurationSource") != "sidecar")
            throw new InvalidDataException("采集扩展的准备信号与本次游戏或镜头不符。");
        string? token = ReadString(notice, "token");
        if (token == null || token.Length != 32 || !token.All(char.IsAsciiHexDigit))
            throw new InvalidDataException("采集扩展的本次令牌无效。");
        if (!ReadFlag(notice, "fanCastSupported") || !ReadFlag(notice, "fanSequenceSupported") ||
            ReadInt(notice, "fanSequenceMaxFrames") < FrameCount || !ReadFlag(notice, "fanSequenceRequestTokenBound"))
            throw new InvalidOperationException("当前采集扩展不支持这版蓝扇序列。请更新扩展并启用 FanCast。");
        return new(session, candidate.Value.Shot, token, report);
    }

    internal static void PublishRequest(FanSequenceRequest request)
    {
        if (FindReady(request.Session) != request)
            throw new InvalidOperationException("采集扩展已切换镜头，请重新点击录制。");
        string temporary = $"{request.ArmPath}.{Guid.NewGuid():N}.tmp";
        bool created = false;
        try
        {
            using (var stream = new FileStream(temporary, FileMode.CreateNew, FileAccess.Write, FileShare.None))
            {
                created = true;
                byte[] payload = Encoding.ASCII.GetBytes(FormattableString.Invariant($"{request.Token}:fan-sequence:{FrameCount}"));
                stream.Write(payload);
            }
            File.Move(temporary, request.ArmPath);
        }
        catch (IOException) when (File.Exists(request.ArmPath))
        {
            throw new InvalidOperationException("采集扩展已有采集请求，请等当前录制结束。");
        }
        finally { if (created) File.Delete(temporary); }
    }

    internal static async Task<FanSequenceRecording> AwaitReportAsync(FanSequenceRequest request, TimeSpan timeout, CancellationToken cancellation)
    {
        var wait = Stopwatch.StartNew();
        do
        {
            cancellation.ThrowIfCancellationRequested();
            if (File.Exists(request.Report)) return ReadCompleted(request);
            if (NewestReadyFile(request.Session) is { } newer && newer.Shot > request.Shot)
            {
                if (File.Exists(request.Report)) return ReadCompleted(request);
                throw new InvalidOperationException("采集扩展已切换镜头，本次未发布原始序列。");
            }
            await Task.Delay(PollDelay, cancellation).ConfigureAwait(false);
        } while (wait.Elapsed < timeout);
        throw new TimeoutException($"等候蓝扇原始序列超时，尚未收到本次录制报告。{request.Report}");
    }

    internal static FanSequenceRecording ReadCompleted(FanSequenceRequest request)
    {
        byte[] bytes = ReadFile(request.Report, 512 * 1024);
        using var json = JsonDocument.Parse(bytes);
        JsonElement report = json.RootElement;
        if (ReadString(report, "schema") != NativeSchema || !MatchesSession(report, request.Session, request.Shot) ||
            ReadString(report, "requestToken") != request.Token || ReadInt(report, "requestedFrames") != FrameCount)
            throw new InvalidDataException("收到的蓝扇报告与本次采集请求不符。");

        string? status = ReadString(report, "status");
        int observed = ReadInt(report, "observedFrames");
        if (string.IsNullOrWhiteSpace(status) || status.Length > 128 || observed < 0 || observed > FrameCount ||
            !report.TryGetProperty("frames", out var frames) || frames.ValueKind != JsonValueKind.Array ||
            frames.GetArrayLength() > observed || (status == "complete" && (observed != FrameCount || frames.GetArrayLength() != FrameCount)))
            throw new InvalidDataException("收到的蓝扇报告帧数或状态无效。");
        return new(request, status, Convert.ToHexString(SHA256.HashData(bytes)));
    }

    private static (int Shot, string Ready)? NewestReadyFile(FanProbeSession session)
    {
        string stem = session.Stem;
        string shotPrefix = stem + "-shot-";
        (int Shot, string Ready)? latest = null;
        foreach (string ready in Directory.EnumerateFiles(session.Directory, $"{stem}*{ReadySuffix}"))
        {
            string name = Path.GetFileName(ready);
            if (!name.EndsWith(ReadySuffix, StringComparison.Ordinal)) continue;
            string header = name[..^ReadySuffix.Length];
            int shot = 1;
            if (header != stem)
            {
                if (!header.StartsWith(shotPrefix, StringComparison.Ordinal) ||
                    !int.TryParse(header.AsSpan(shotPrefix.Length), NumberStyles.None, CultureInfo.InvariantCulture, out shot) ||
                    shot <= 1 || header != shotPrefix + shot.ToString(CultureInfo.InvariantCulture)) continue;
            }
            if (latest == null || shot > latest.Value.Shot) latest = (shot, ready);
        }
        return latest;
    }

    internal static byte[] ReadFile(string path, int maxBytes)
    {
        var info = new FileInfo(path);
        if ((info.Attributes & (FileAttributes.ReparsePoint | FileAttributes.Directory)) != 0)
            throw new InvalidDataException($"无法读取采集文件。{info.Name}");
        using var stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read);
        if (stream.Length <= 0 || stream.Length > maxBytes)
            throw new InvalidDataException($"采集文件大小无效。{info.Name}");
        var bytes = new byte[(int)stream.Length];
        stream.ReadExactly(bytes);
        return bytes;
    }

    internal static bool MatchesSession(JsonElement item, FanProbeSession session, int shot) =>
        string.Equals(ReadString(item, "hostExecutable"), FanProbeSession.Host, StringComparison.OrdinalIgnoreCase) &&
        ReadInt(item, "processId") == session.ProcessId && ReadInt(item, "sessionShot") == shot;

    internal static string? ReadString(JsonElement item, string key) =>
        item.ValueKind == JsonValueKind.Object && item.TryGetProperty(key, out var value) && value.ValueKind == JsonValueKind.String
            ? value.GetString() : null;

    private static int ReadInt(JsonElement item, string key) =>
        item.ValueKind == JsonValueKind.Object && item.TryGetProperty(key, out var value) && value.ValueKind == JsonValueKind.Number &&
        value.TryGetInt32(out int number) ? number : -1;

    private static bool ReadFlag(JsonElement item, string key) =>
        item.ValueKind == JsonValueKind.Object && item.TryGetProperty(key, out var value) && value.ValueKind == JsonValueKind.True;
}
