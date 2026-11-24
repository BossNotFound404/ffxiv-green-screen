namespace RoundTableStudio
{
    internal static class Stage
    {
        internal static byte Color;
        internal static byte? Target() => Color;
    }

    internal sealed class TestConfig { internal Capture Pending { get; } = new() { Character = 5, SeenStudio = true }; }
    internal sealed class TestUi { internal bool DisableUserUiHide { get; set; } }
    internal sealed class TestClient { internal bool IsGPosing { get; set; } }
    internal sealed class TestModel { internal bool IsRedrawing { get; set; } }
    internal sealed class TestInterface(string root)
    {
        internal TestUi UiBuilder { get; } = new();
        internal string GetPluginConfigDirectory() => root;
    }

    internal sealed class PreviewFreeze
    {
        internal string? EffectPath => Frozen ? "vfx/test/example.avfx" : null;
        internal double Speed = 1.25;
        internal bool Available = true;
        internal int Calls;
        internal bool Frozen { get; private set; }
        private double saved;
        internal bool TryAcquire(out string reason)
        {
            Calls++;
            reason = "preview missing";
            if (!Available) return false;
            saved = Speed;
            Speed = 0;
            Frozen = true;
            return true;
        }
        internal bool Check(out string reason) { Calls++; reason = "preview changed"; return Frozen && Available; }
        internal void Release() { Calls++; Frozen = false; Speed = saved; }
    }

    internal sealed class ActorOpacity
    {
        internal float Value = 0.37f;
        private float? original;
        internal void HideForExport(ulong actor) { original = Value; Value = 0; }
        internal float? Read(ulong actor) => Value;
        internal void Restore(ulong actor)
        {
            if (original is float saved && Value == 0) Value = saved;
            original = null;
        }
    }

    internal sealed class StudioHudScope : IDisposable
    {
        internal static bool Visible;
        private readonly bool wasVisible = Visible;
        internal static StudioHudScope Hide() { var scope = new StudioHudScope(); Visible = false; return scope; }
        internal bool IsHidden => !Visible;
        public void Dispose() => Visible = wasVisible;
    }

    // The frame-reader boundary is programmable: the real reader's sampling/GPU path is tested separately.
    internal sealed record StudioFrame(int Width, int Height, byte[] Rgba, byte Backdrop)
    {
        internal bool HasStudioBackground(byte color) => color == Backdrop;
    }

    internal enum SkillExposureState { WaitingForBackground, ReadyForSkill, Captured, Failed }

    // The real optical detector has its own pixel/timing tests. Here the user controls when one cast becomes visible.
    internal sealed class SkillExposure(byte color, int desiredOffsetMs)
    {
        internal bool Cast;
        internal int Shots;
        internal SkillExposureState State { get; private set; }
        internal StudioFrame? Frame { get; private set; }
        internal int ActualOffsetMs => desiredOffsetMs - 13;
        internal string Message => State == SkillExposureState.ReadyForSkill ? "请施放技能" : "等待画面";
        internal void Observe(StudioFrame frame, long receivedAt)
        {
            if (!frame.HasStudioBackground(color)) return;
            if (State == SkillExposureState.WaitingForBackground) State = SkillExposureState.ReadyForSkill;
            else if (Cast && State == SkillExposureState.ReadyForSkill)
            {
                Shots++;
                Frame = frame;
                State = SkillExposureState.Captured;
            }
        }
    }

    internal sealed class StudioFrameReader
    {
        internal int Reads;
        internal int ReadsInExposure;
        internal bool FailNext, WaitForCancel;
        internal Task<StudioFrame>? Pending;
        internal Task<StudioFrame> CaptureAsync(uint viewport, CancellationToken token)
        {
            if (viewport != 71) throw new Exception("Wrong viewport.");
            token.ThrowIfCancellationRequested();
            Reads++;
            ReadsInExposure++;
            if (FailNext) return Task.FromException<StudioFrame>(new IOException("readback failed"));
            if (WaitForCancel)
            {
                var pending = new TaskCompletionSource<StudioFrame>(TaskCreationOptions.RunContinuationsAsynchronously);
                token.Register(() => pending.TrySetCanceled(token));
                return Pending = pending.Task;
            }
            byte color = Stage.Color;
            byte rb = color == 46 ? (byte)255 : (byte)0;
            byte g = color == 47 ? (byte)0 : (byte)255;
            byte[] pixels = [rb, g, rb, 9, 98, 112, 24, 9, rb, g, rb, 9];
            // A clear frame, a transition, then two stable frames.
            return Task.FromResult(new StudioFrame(3, 1, pixels, ReadsInExposure == 2 ? (byte)0 : color));
        }
    }
}

namespace Dalamud.Bindings.ImGui
{
    internal static class ImGui
    {
        internal readonly record struct Viewport(uint ID);
        internal static string? NextButton;
        internal static bool Button(string text)
        {
            if (text != NextButton) return false;
            NextButton = null;
            return true;
        }
        internal static Viewport GetMainViewport() => new(71);
        internal static bool IsItemHovered() => false;
        internal static void SetTooltip(string text) { }
        internal static void SameLine() { }
        internal static void TextWrapped(string text) { }
    }
}
