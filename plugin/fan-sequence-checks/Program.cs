using System.Diagnostics;
using System.Security.Cryptography;
using System.Text.Json;
using RoundTableStudio;

if (args.Length != 0)
    return await RunExporterFixture(args);

var workspace = Directory.CreateTempSubdirectory("fan-sequence-check-");
try
{
    string folder = Directory.CreateDirectory(Path.Combine(workspace.FullName, "报告 路径")).FullName;
    var session = new FanProbeSession(12000, DateTime.UtcNow.AddMinutes(-2), folder);
    string profile = Path.Combine(workspace.FullName, "GreenScreenDrawProbe.lab.ini");
    File.WriteAllText(profile, $"\uFEFF[Other]\nReportDirectory=ignored\n[Capture]\nFanCast=1\n  ReportDirectory = {folder}\n");
    Require(FanSequenceBridge.ReadReportDirectory(profile) == folder, "Parsed the capture report folder.");
    File.AppendAllText(profile, $"ReportDirectory={folder}\n");
    await RejectAsync<InvalidDataException>(() => Task.FromResult(FanSequenceBridge.ReadReportDirectory(profile)), "重复");

    var retired = WriteReady(session, 1);
    var invalidName = retired with { Report = Path.Combine(folder, session.Stem + "-shot-0038.json") };
    File.Copy(retired.Report + ".ready.json", invalidName.Report + ".ready.json");
    WriteReady(session with { StartedUtc = session.StartedUtc.AddHours(-1) }, 19);
    WriteReady(session with { ProcessId = 12001 }, 99);
    Require(FanSequenceBridge.FindReady(session) == retired, "Ignored previous process and malformed shot filenames.");

    var fresh = WriteReady(session, 2);
    await RejectAsync<InvalidOperationException>(() => Publish(retired), "切换镜头");
    Require(!File.Exists(retired.ArmPath), "Never armed a retired readiness file.");
    File.SetLastWriteTimeUtc(fresh.Report + ".ready.json", session.StartedUtc.AddSeconds(-10));
    await RejectAsync<InvalidDataException>(() => Task.FromResult(FanSequenceBridge.FindReady(session)), "早于");
    WriteReady(session, 2, bindsToken: false);
    await RejectAsync<InvalidOperationException>(() => Task.FromResult(FanSequenceBridge.FindReady(session)), "不支持");
    WriteReady(session, 2);

    int claimed = 0, busy = 0;
    await Task.WhenAll(Enumerable.Range(0, 2).Select(_ => Task.Run(async () =>
    {
        try { await Publish(fresh); Interlocked.Increment(ref claimed); }
        catch (InvalidOperationException error) when (error.Message.Contains("已有采集")) { Interlocked.Increment(ref busy); }
    })));
    Require(claimed == 1 && busy == 1, "Exactly one contender owns the shot.");
    Require(File.ReadAllText(fresh.ArmPath) == fresh.Token + ":fan-sequence:240", "Armed the correct request with no BOM.");
    Require(Directory.GetFiles(folder, "*.tmp").Length == 0, "No temporary request files left.");
    await RejectAsync<InvalidOperationException>(() => Task.FromResult(FanSequenceBridge.FindReady(session)), "已有采集");

    await RejectAsync<TimeoutException>(() => FanSequenceBridge.AwaitReportAsync(fresh, TimeSpan.FromMilliseconds(40), default), "超时");
    using (var canceled = new CancellationTokenSource())
    {
        canceled.Cancel();
        await RejectAsync<OperationCanceledException>(() => FanSequenceBridge.AwaitReportAsync(fresh, TimeSpan.FromSeconds(5), canceled.Token));
    }
    var next = WriteReady(session, 3);
    await RejectAsync<InvalidOperationException>(() => FanSequenceBridge.AwaitReportAsync(fresh, TimeSpan.FromSeconds(1), default), "切换镜头");
    Require(!File.Exists(next.ArmPath), "Did not arm a different shot while waiting.");

    WriteCompleted(fresh with { Token = retired.Token }, "complete", 240);
    await RejectAsync<InvalidDataException>(() => Task.FromResult(FanSequenceBridge.ReadCompleted(fresh)), "不符");
    WriteCompleted(fresh with { Shot = 1 }, "complete", 240);
    await RejectAsync<InvalidDataException>(() => Task.FromResult(FanSequenceBridge.ReadCompleted(fresh)), "不符");
    WriteCompleted(fresh with { Session = session with { ProcessId = 1234 } }, "complete", 240);
    await RejectAsync<InvalidDataException>(() => Task.FromResult(FanSequenceBridge.ReadCompleted(fresh)), "不符");
    WriteCompleted(fresh, "complete", 239);
    await RejectAsync<InvalidDataException>(() => Task.FromResult(FanSequenceBridge.ReadCompleted(fresh)), "帧数");
    WriteCompleted(fresh, "staging_queue_exhausted", 13);
    Require(!FanSequenceBridge.ReadCompleted(fresh).CanExport, "Unknown native fault stays native-only.");
    WriteCompleted(fresh, "fan_draw_budget_exhausted", 105);
    Require(FanSequenceBridge.ReadCompleted(fresh).CanExport, "Only the supported prefix is eligible for tool validation.");
    WriteCompleted(fresh, "complete", 240);
    var recording = await FanSequenceBridge.AwaitReportAsync(fresh, TimeSpan.FromSeconds(1), default);
    Require(recording.CanExport && recording.Request == fresh, "Used only the matching publication when a newer shot is ready.");

    using (var deadline = new CancellationTokenSource(TimeSpan.FromSeconds(5)))
    {
        var cue = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        Task<FanSequenceRecording> active = FanSequenceBridge.RecordAsync(session, () => cue.SetResult(), deadline.Token);
        await cue.Task.WaitAsync(deadline.Token);
        Require(File.Exists(next.ArmPath) && !active.IsCompleted, "Cast cue follows request publication and precedes completion.");
        WriteCompleted(next, "complete", 240);
        Require((await active).Request == next, "Recorder returns the intended native shot.");
    }

    string missingExe = FanSequenceExporter.ToolPath(Path.Combine(workspace.FullName, "RoundTableStudio.dll"));
    await RejectAsync<FileNotFoundException>(() => FanSequenceExporter.ExportAsync(missingExe, recording, default), "缺少");
    Require(File.Exists(recording.Request.Report), "Original capture survives missing export tools.");

    string fixtureExe = Environment.ProcessPath!;
    Require(Path.GetFileName(fixtureExe) == "FanSequenceChecks.exe", "Run via test apphost, so it can stand in for the fixed exporter executable.");
    string output = FanSequenceExporter.OutputPath(recording);
    Environment.SetEnvironmentVariable("FAN_SEQUENCE_CHECK_EXPORTER", "error");
    await RejectAsync<InvalidOperationException>(() => FanSequenceExporter.ExportAsync(fixtureExe, recording, default), "模拟导出错误");
    Require(!Directory.Exists(output), "Tool error did not create a success result.");

    Environment.SetEnvironmentVariable("FAN_SEQUENCE_CHECK_EXPORTER", "hang");
    using (var stop = new CancellationTokenSource(TimeSpan.FromSeconds(8)))
    {
        var exporting = FanSequenceExporter.ExportAsync(fixtureExe, recording, stop.Token);
        string pidFile = output + ".fixture.pid";
        while (!File.Exists(pidFile)) await Task.Delay(40, stop.Token);
        int pid = int.Parse(File.ReadAllText(pidFile));
        using var process = Process.GetProcessById(pid);
        stop.Cancel();
        await RejectAsync<OperationCanceledException>(() => exporting);
        Require(process.HasExited, "Cancel waited for exporter exit.");
    }

    Environment.SetEnvironmentVariable("FAN_SEQUENCE_CHECK_EXPORTER", "ok");
    Require(await FanSequenceExporter.ExportAsync(fixtureExe, recording, default) == output,
        "Launch and validate a complete export in a path with spaces and Chinese characters.");
    Require(await FanSequenceExporter.ExportAsync(missingExe, recording, default) == output,
        "Recovered exact pre-existing publication without starting another process.");
    string importer = Path.Combine(output, FanSequenceExporter.Importer);
    File.Delete(importer);
    await RejectAsync<InvalidDataException>(() => FanSequenceExporter.ExportAsync(fixtureExe, recording, default), "缺少文件");
    Require(!File.Exists(importer), "Invalid existing exports are never silently overwritten.");
    WriteCompleted(fresh, "fan_draw_budget_exhausted", 105);
    await RejectAsync<InvalidDataException>(() => FanSequenceExporter.ExportAsync(fixtureExe, recording, default), "改变");
    Console.WriteLine("Fan sequence bridge and exporter checks passed.");
    return 0;
}
finally { workspace.Delete(recursive: true); }

static void Require(bool condition, string message)
{
    if (!condition) throw new InvalidOperationException(message);
}

static async Task RejectAsync<T>(Func<Task> action, string? contains = null) where T : Exception
{
    try { await action(); }
    catch (T error) when (contains == null || error.Message.Contains(contains)) { return; }
    throw new InvalidOperationException($"Expected {typeof(T).Name} with message containing '{contains}'.");
}

static Task Publish(FanSequenceRequest request)
{
    FanSequenceBridge.PublishRequest(request);
    return Task.CompletedTask;
}

static FanSequenceRequest WriteReady(FanProbeSession session, int shot, bool bindsToken = true)
{
    string token = shot.ToString("x32");
    string report = Path.Combine(session.Directory, session.Stem + (shot == 1 ? "" : $"-shot-{shot}") + ".json");
    File.WriteAllText(report + ".ready.json", JsonSerializer.Serialize(new
    {
        hostExecutable = FanProbeSession.Host,
        processId = session.ProcessId,
        sessionShot = shot,
        configurationSource = "sidecar",
        fanCastSupported = true,
        fanSequenceSupported = true,
        fanSequenceMaxFrames = 360,
        fanSequenceRequestTokenBound = bindsToken,
        token,
    }));
    return new(session, shot, token, report);
}

static void WriteCompleted(FanSequenceRequest request, string status, int observed)
{
    string staging = request.Report + ".fixture.part";
    File.WriteAllText(staging, JsonSerializer.Serialize(new
    {
        schema = "fan-native-sequence-v1",
        hostExecutable = FanProbeSession.Host,
        processId = request.Session.ProcessId,
        sessionShot = request.Shot,
        requestToken = request.Token,
        requestedFrames = 240,
        status,
        observedFrames = observed,
        frames = Enumerable.Range(0, observed),
    }));
    File.Move(staging, request.Report, overwrite: true);
}

static async Task<int> RunExporterFixture(string[] fixtureArgs)
{
    Require(fixtureArgs is ["--index", _, "--out", _], "Expected two fixed named arguments.");
    string index = fixtureArgs[1], output = fixtureArgs[3];
    string mode = Environment.GetEnvironmentVariable("FAN_SEQUENCE_CHECK_EXPORTER") ?? "";
    if (mode == "error")
    {
        Console.OutputEncoding = System.Text.Encoding.UTF8;
        Console.Error.WriteLine(new string('x', 100_000));
        Console.Error.WriteLine("模拟导出错误");
        return 1;
    }
    if (mode == "hang")
    {
        string marker = output + ".fixture.pid";
        File.WriteAllText(marker + ".tmp", Environment.ProcessId.ToString());
        File.Move(marker + ".tmp", marker);
        await Task.Delay(TimeSpan.FromMinutes(1));
        return 1;
    }
    Require(mode == "ok", "Unknown fixture mode.");
    Directory.CreateDirectory(output);
    var bytes = File.ReadAllBytes(index);
    using var json = JsonDocument.Parse(bytes);
    File.WriteAllText(Path.Combine(output, "sequence.json"), JsonSerializer.Serialize(new
    {
        schema = "fan-ae-sequence-v1",
        hostExecutable = FanProbeSession.Host,
        processId = json.RootElement.GetProperty("processId").GetInt32(),
        sessionShot = json.RootElement.GetProperty("sessionShot").GetInt32(),
        nativeIndexFile = Path.GetFileName(index),
        nativeIndexSha256 = Convert.ToHexString(SHA256.HashData(bytes)).ToLowerInvariant(),
    }));
    File.WriteAllText(Path.Combine(output, FanSequenceExporter.Importer), "Fixture importer");
    File.WriteAllText(Path.Combine(output, "clean-plate.png"), "Fixture plate");
    return 0;
}
