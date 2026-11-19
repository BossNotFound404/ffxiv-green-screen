using RoundTableStudio;

static StudioFrame Plate(int size = 0, byte intensity = 0)
{
    const int w = 160, h = 100;
    var pixels = new byte[w * h * 4];
    for (int y = h / 2 - size / 2; y < h / 2 - size / 2 + size; y++)
    for (int x = w / 2 - size / 2; x < w / 2 - size / 2 + size; x++)
        pixels[(y * w + x) * 4] = intensity;
    return new(w, h, pixels);
}

static void Check(bool value, string name)
{
    if (!value) throw new InvalidOperationException(name);
    Console.WriteLine("PASS " + name);
}

var empty = Plate();
var soft = Plate(8, 32);
var bright = Plate(12, 200);
var weak = Plate(1, 255);

SkillExposure Armed(int window = 360)
{
    var e = new SkillExposure(47, window);
    foreach (var ms in new[] { 0, 60, 160, 320 }) e.Observe(empty, ms);
    Check(e.State == SkillExposureState.ReadyForSkill && e.Frame == null, "clean black plate arms the shot");
    return e;
}

var pulse = Armed();
pulse.Observe(bright, 350);
pulse.Observe(empty, 480);
pulse.Observe(empty, 711);
Check(pulse.State == SkillExposureState.Captured && ReferenceEquals(bright, pulse.Frame) && pulse.ActualOffsetMs == 0,
    "single observed flash survives after it disappears");

var burst = Armed();
burst.Observe(soft, 360);
burst.Observe(bright, 445);
burst.Observe(empty, 605);
burst.Observe(Plate(20, 255), 800);
Check(ReferenceEquals(bright, burst.Frame) && burst.ActualOffsetMs == 85, "selects a real frame inside the window");

var timeout = Armed();
timeout.Observe(weak, 620);
timeout.Observe(empty, 46000);
Check(timeout.State == SkillExposureState.Failed && timeout.Frame == null, "tiny signal or no cast never creates an empty frame");

var blocked = new SkillExposure(47, 360);
blocked.Observe(bright, 0);
blocked.Observe(empty, 200);
blocked.Observe(bright, 260);
blocked.Observe(bright, 15500);
Check(blocked.State == SkillExposureState.Failed && blocked.Frame == null, "old effect cannot become a new cast before a clean plate");

var resize = Armed();
resize.Observe(new StudioFrame(100, 100, new byte[40000]), 355);
Check(resize.State == SkillExposureState.Failed && resize.Frame == null, "rejects a resized viewport");

var noisy = Armed();
noisy.Observe(new StudioFrame(160, 100, Enumerable.Repeat((byte)255, 64000).ToArray()), 400);
Check(noisy.State == SkillExposureState.Failed && noisy.Frame == null, "fullscreen loss of the black backdrop is rejected");

var instant = Armed(0);
instant.Observe(soft, 330);
Check(ReferenceEquals(instant.Frame, soft), "zero window captures the first eligible observation");
