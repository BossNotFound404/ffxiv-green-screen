using RoundTableStudio;

string root = Path.Combine(Path.GetTempPath(), "GreenScreenFlowChecks-" + Guid.NewGuid().ToString("N"));
try
{
    Plugin.CheckComplete(Path.Combine(root, "complete"));
    Plugin.CheckPlayerSkill(Path.Combine(root, "natural-skill"), cancel: false);
    Plugin.CheckPlayerSkill(Path.Combine(root, "waiting-skill"), cancel: true);
    Plugin.CheckCancel(Path.Combine(root, "cancel"));
    Plugin.CheckDelayedRestore(Path.Combine(root, "gpose"));
    Plugin.CheckSavedBeforeCancel(Path.Combine(root, "saved"));
    Plugin.CheckFailure(Path.Combine(root, "read-failure"), "read");
    Plugin.CheckFailure(Path.Combine(root, "lost-freeze"), "freeze");
    Plugin.CheckFailure(Path.Combine(root, "timeout"), "timeout");
    Plugin.CheckFailure(Path.Combine(root, "missing-preview"), "unavailable");
    Console.WriteLine("PASS: five exposures, consecutive stable frames, restore, cancellation, timeout, preview loss, GPU readback failure.");
    return 0;
}
catch (Exception e) { Console.Error.WriteLine(e); return 1; }
finally { if (Directory.Exists(root)) Directory.Delete(root, true); }

namespace RoundTableStudio
{
    public sealed partial class Plugin
    {
        private readonly TestInterface pi;
        private readonly TestConfig config = new();
        private readonly TestClient client = new();
        private readonly TestModel targetModel = new();
        private readonly List<byte> switches = [];
        private readonly string root;
        private bool positionOnEntry => false;
        private bool applyFirstColor => false;
        private bool disposed => false;
        private bool InDuty => false;
        private bool ChangingArea => false;
        private uint Layout => 448;
        private ulong Character => config.Pending.Character;

        private Plugin(string path, bool hudVisible)
        {
            root = path;
            pi = new TestInterface(path);
            frameReader = new StudioFrameReader();
            previewFreeze = new PreviewFreeze();
            exportOpacity = new ActorOpacity();
            Stage.Color = 45;
            StudioHudScope.Visible = hudVisible;
            pi.UiBuilder.DisableUserUiHide = !hudVisible;
        }

        private void RequireStudio(Capture studio) => Check(studio.CanShoot(Layout, Character) && !client.IsGPosing, "ordinary studio view");
        private void FromPanel(Action action) => action();
        private void Color(Capture studio, byte color, bool temporary)
        {
            Check(temporary && ReferenceEquals(studio, config.Pending), "temporary color within current session");
            switches.Add(color);
            frameReader.ReadsInExposure = 0;
            Stage.Color = color;
        }

        private void Start() => StartExport(config.Pending, 71);
        // Advance past the sampling delay, keeping the real deadline, async transitions and stability counters.
        private void Tick() { if (export != null) export.NextSample = 0; TickExport(); }
        private void RunUntil(Func<bool> until)
        {
            long deadline = Environment.TickCount64 + 5000;
            while (!until() && Environment.TickCount64 < deadline) { Tick(); Thread.Sleep(1); }
            Check(until(), "workflow made progress. Notice was " + exportNotice);
        }

        internal static void CheckComplete(string path)
        {
            var plugin = new Plugin(path, hudVisible: true);
            plugin.Start();
            Check(!StudioHudScope.Visible && plugin.pi.UiBuilder.DisableUserUiHide && plugin.exportOpacity.Value == 0
                && plugin.previewFreeze.Frozen && plugin.previewFreeze.Speed == 0, "freeze and hide");
            plugin.Start(); // A double-click must not start a second job or overwrite the snapshot.
            plugin.RunUntil(() => plugin.export == null);
            Check(plugin.switches.SequenceEqual(new byte[] { 47, 46, 48, 47, 46, 45 }), "five exposures and original red restored");
            Check(plugin.frameReader.Reads == 20, "transition resets count and each exposure requires another stable pair");
            plugin.CheckRestored(true, 1.25);
            Check(plugin.exportDirectory != null && Directory.GetDirectories(Path.Combine(path, "Exports")).Length == 1,
                "exactly one export from the button");
            string[] files = ["black.png","white.png","green.png","black-repeat.png","white-repeat.png","black-hold.png","white-hold.png","effect.png","green-recomposed.png","report.json"];
            foreach (string file in files) Check(File.Exists(Path.Combine(plugin.exportDirectory!, file)), file + " present");
            using var report = System.Text.Json.JsonDocument.Parse(File.ReadAllText(Path.Combine(plugin.exportDirectory!, "report.json")));
            Check(report.RootElement.GetProperty("effectPath").GetString() == "vfx/test/example.avfx"
                && report.RootElement.GetProperty("blackHoldMismatchPixels").GetInt32() == 0
                && report.RootElement.GetProperty("whiteHoldMismatchPixels").GetInt32() == 0, "frozen preview identity and consecutive stable samples");
        }

        internal static void CheckPlayerSkill(string path, bool cancel)
        {
            var plugin = new Plugin(path, hudVisible: true);
            plugin.previewFreeze.Available = false;
            Dalamud.Bindings.ImGui.ImGui.NextButton = "拍角色技能";
            plugin.DrawExport(plugin.config.Pending);
            var sampler = plugin.export?.Skill ?? throw new Exception("skill button did not arm optical sampling");
            Check(Stage.Color == 47 && !StudioHudScope.Visible && plugin.exportOpacity.Value == 0 &&
                plugin.previewFreeze.Calls == 0, "natural skill starts on black without a preview or time change");
            plugin.RunUntil(() => sampler.State == SkillExposureState.ReadyForSkill);
            Check(plugin.exportNotice.Contains("请施放技能") && sampler.Shots == 0, "waits for a natural cast");
            if (cancel)
            {
                Dalamud.Bindings.ImGui.ImGui.NextButton = "取消导出";
                plugin.DrawExport(plugin.config.Pending);
                plugin.CheckNoOutput();
            }
            else
            {
                sampler.Cast = true;
                plugin.RunUntil(() => plugin.export == null);
                Check(sampler.Shots == 1 && plugin.exportDirectory != null && plugin.exportNotice.Contains("本次只拍了黑棚"),
                    "exactly one sampled cast published with its actual validation scope");
                Check(Directory.GetFiles(plugin.exportDirectory!).Select(Path.GetFileName).OrderBy(x => x)
                    .SequenceEqual(new[] { "black.png", "glow-add.png", "report.json" }), "single skill does not fabricate plates or effect.png");
                using var report = System.Text.Json.JsonDocument.Parse(File.ReadAllText(Path.Combine(plugin.exportDirectory!, "report.json")));
                Check(report.RootElement.GetProperty("offsetMs").GetInt32() == sampler.ActualOffsetMs
                    && !report.RootElement.GetProperty("ordinaryAlphaTested").GetBoolean(), "measured skill timing reaches the exporter");
            }
            plugin.CheckRestored(true, 1.25);
            Check(plugin.switches.SequenceEqual(new byte[] { 47, 45 }) && plugin.previewFreeze.Calls == 0,
                "one visit to black, restored red, no VFXEditor or Brio freeze calls");
        }

        internal static void CheckCancel(string path)
        {
            var plugin = new Plugin(path, hudVisible: false);
            plugin.previewFreeze.Speed = 0.5;
            plugin.Start();
            plugin.RunUntil(() => Stage.Color == 46);
            plugin.frameReader.WaitForCancel = true;
            plugin.Tick();
            Check(plugin.frameReader.Pending is { IsCompleted: false }, "read in flight");
            plugin.StopExport("cancelled");
            Check(plugin.frameReader.Pending is { IsCanceled: true }, "in-flight capture cancelled");
            plugin.CheckRestored(false, 0.5);
            plugin.CheckNoOutput();
        }

        internal static void CheckFailure(string path, string mode)
        {
            var plugin = new Plugin(path, hudVisible: true);
            if (mode == "unavailable") plugin.previewFreeze.Available = false;
            plugin.Start();
            if (mode != "unavailable")
            {
                plugin.RunUntil(() => Stage.Color == 46);
                if (mode == "read") plugin.frameReader.FailNext = true;
                if (mode == "freeze") plugin.previewFreeze.Available = false;
                if (mode == "timeout") plugin.export!.Deadline = Environment.TickCount64;
                plugin.RunUntil(() => plugin.export == null);
            }
            plugin.CheckRestored(true, 1.25);
            plugin.CheckNoOutput();
            Check(!string.IsNullOrWhiteSpace(plugin.exportNotice), "failure reported");
        }

        internal static void CheckDelayedRestore(string path)
        {
            var plugin = new Plugin(path, hudVisible: true);
            plugin.Start();
            plugin.RunUntil(() => Stage.Color == 46);
            plugin.client.IsGPosing = true;
            plugin.Tick();
            Check(plugin.export == null && Stage.Color == 46 && plugin.deferredExportColor != null,
                "do not switch backgrounds inside GPose");
            Check(StudioHudScope.Visible && plugin.previewFreeze.Speed == 1.25 && plugin.exportOpacity.Value == 0.37f,
                "GPose interruption releases capture-only settings");
            plugin.client.IsGPosing = false;
            plugin.RestoreExportColor(plugin.config.Pending);
            plugin.CheckRestored(true, 1.25);
            Check(plugin.deferredExportColor == null, "ordinary view consumes pending restoration");
            plugin.CheckNoOutput();
        }

        internal static void CheckSavedBeforeCancel(string path)
        {
            var plugin = new Plugin(path, hudVisible: false);
            plugin.Start();
            plugin.RunUntil(() => plugin.export?.Save != null);
            plugin.export!.Save!.GetAwaiter().GetResult();
            plugin.StopExport("cancelled");
            Check(plugin.exportDirectory != null && File.Exists(Path.Combine(plugin.exportDirectory, "effect.png")),
                "late cancellation reports the committed export");
            plugin.CheckRestored(false, 1.25);
        }

        private void CheckRestored(bool previousHud, double speed) =>
            Check(export == null && Stage.Color == 45 && exportOpacity.Value == 0.37f && StudioHudScope.Visible == previousHud
                && pi.UiBuilder.DisableUserUiHide == !previousHud && !previewFreeze.Frozen && previewFreeze.Speed == speed,
                "original scene, model alpha, HUD state, UI hide preference and preview speed restored");

        private void CheckNoOutput() =>
            Check(exportDirectory == null && (!Directory.Exists(root) || !Directory.EnumerateFiles(root, "*", SearchOption.AllDirectories).Any()),
                "no capture or partial result published");

        private static void Check(bool ok, string name)
        {
            if (!ok) throw new Exception("Failed: " + name);
        }
    }
}
