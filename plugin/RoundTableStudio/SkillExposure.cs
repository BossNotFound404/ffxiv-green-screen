namespace RoundTableStudio;

internal enum SkillExposureState { WaitingForBackground, ReadyForSkill, SamplingSkill, Captured, Failed }

// Keep one real frame: a pulse that is visible in just one observation can still be captured.
internal sealed class SkillExposure
{
    private const int MaxGapMs = 220;
    private readonly int captureWindowMs;
    private long? firstSample, previousSample, clearSince;
    private int width, height, clearFrames;
    private long armedAt, onset;
    private StudioFrame? bestFrame;
    private long bestEnergy;
    private int bestOffset;

    internal SkillExposure(byte weather, int captureWindowMs)
    {
        if (weather != 47) throw new ArgumentOutOfRangeException(nameof(weather));
        if (captureWindowMs < 0) throw new ArgumentOutOfRangeException(nameof(captureWindowMs));
        this.captureWindowMs = captureWindowMs;
    }

    internal SkillExposureState State { get; private set; } = SkillExposureState.WaitingForBackground;
    internal string Message { get; private set; } = "等待棚色稳定和上一次技能消失。";
    internal StudioFrame? Frame { get; private set; }
    internal int ActualOffsetMs { get; private set; }

    internal void Observe(StudioFrame frame, long sampleTimeMs)
    {
        if (State is SkillExposureState.Captured or SkillExposureState.Failed) return;
        if (firstSample == null)
        {
            firstSample = sampleTimeMs;
            width = frame.Width;
            height = frame.Height;
        }
        if (frame.Width != width || frame.Height != height || width < 64 || height < 64 ||
            (long)width * height * 4 != frame.Rgba.Length)
        {
            Fail("游戏画面的尺寸发生变化或画面读取不完整，请重新拍摄。");
            return;
        }
        // Several UI updates can refer to the same completed read. It cannot confirm a second observation.
        if (sampleTimeMs == previousSample) return;
        long gap = sampleTimeMs - (previousSample ?? sampleTimeMs);
        if (gap < 0) { Fail("画面采样时间异常，请重新拍摄。"); return; }
        previousSample = sampleTimeMs;

        // Only require all four sides before arming. A large genuine effect can extend into the margins.
        if (State == SkillExposureState.WaitingForBackground)
        {
            if (sampleTimeMs - firstSample.Value >= 15000)
            {
                Fail("未等到干净的棚色。请移走其他模型，等上一轮特效消失后重试。");
                return;
            }
            if (gap > MaxGapMs || !frame.HasStudioBackground(47))
            {
                clearSince = null;
                clearFrames = 0;
                return;
            }
        }

        var (changed, samples, energy) = MeasureLight(frame);
        bool empty = changed <= Math.Max(2, samples / 250000);
        bool visible = changed >= Math.Max(6, samples / 48000);
        if (State == SkillExposureState.WaitingForBackground)
        {
            if (!empty) { clearSince = null; clearFrames = 0; return; }
            clearSince ??= sampleTimeMs;
            if (++clearFrames >= 3 && sampleTimeMs - clearSince.Value >= 320)
            {
                State = SkillExposureState.ReadyForSkill;
                armedAt = sampleTimeMs;
                Message = "棚色已稳定，请在游戏里施放技能。";
            }
            return;
        }
        if (State == SkillExposureState.ReadyForSkill)
        {
            if (sampleTimeMs - armedAt >= 45000)
            {
                Fail("未检测到足够清晰的技能。它在这一棚色上可能太小或接近背景色。");
                return;
            }
            if (!visible) return;
            onset = sampleTimeMs;
            State = SkillExposureState.SamplingSkill;
            Message = "已看到技能，正在选取清晰的一帧。";
        }
        long elapsed = sampleTimeMs - onset;
        if (elapsed <= captureWindowMs && changed > samples * 0.995)
        {
            Fail("技能出现时整幅画面已失去棚色参照，请拉远镜头后重试。");
            return;
        }
        if (elapsed <= captureWindowMs && visible && energy > bestEnergy)
        {
            bestFrame = frame;
            bestOffset = (int)elapsed;
            bestEnergy = energy;
        }
        if (elapsed < captureWindowMs) return;
        Frame = bestFrame;
        bestFrame = null;
        ActualOffsetMs = bestOffset;
        State = SkillExposureState.Captured;
        Message = $"已拍到技能可见后 {ActualOffsetMs} 毫秒的画面。";
    }

    private (int Changed, int Samples, long Energy) MeasureLight(StudioFrame frame)
    {
        int step = Math.Max(1, Math.Min(width / 960, height / 540));
        int changed = 0, samples = 0;
        long energy = 0;
        var rgba = frame.Rgba;
        for (int y = 0; y < height; y += step)
        {
            int startX = (y / step) % step;
            int sampleY = y;
            for (int x = startX; x < width; x += step)
            {
                int i = (sampleY * width + x) * 4;
                int peak = Math.Max(rgba[i], Math.Max(rgba[i + 1], rgba[i + 2]));
                if (peak > 8)
                {
                    changed++;
                    energy += peak - 8;
                }
                samples++;
                if (++sampleY == y + step || sampleY == height) sampleY = y;
            }
        }
        return (changed, samples, energy);
    }

    private void Fail(string reason)
    {
        bestFrame = null;
        State = SkillExposureState.Failed;
        Message = reason;
    }
}
