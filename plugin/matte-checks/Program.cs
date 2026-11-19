using System.Buffers.Binary;
using System.IO.Compression;
using System.Text.Json;
using RoundTableStudio;

string root = Path.Combine(Path.GetTempPath(), "GreenScreenMatteChecks-" + Guid.NewGuid().ToString("N"));
try
{
    const int width = 3, height = 2;
    byte[] knownAlpha = [0, 64, 128, 192, 255, 0];
    byte[] dark = Capture([0, 0, 0]), white = Capture([255, 255, 255]), green = Capture([0, 255, 0]);
    var pass = Export(green);
    Check(pass.Passed && pass.ForegroundPixels == 4 && pass.GreenMismatchPixels == 0, "independent green reconstruction");
    var effect = ReadPng(Path.Combine(pass.DirectoryPath, "effect.png"), 4);
    for (int i = 0; i < knownAlpha.Length; i++)
        Check(Math.Abs(effect[4*i+3] - knownAlpha[i]) <= 1, "straight alpha for pixel " + i);
    Check(effect[0] == 0 && effect[1] == 0 && effect[2] == 0, "transparent RGB");
    Check(File.ReadAllBytes(Path.Combine(pass.DirectoryPath, "green.png"))
        .Length > 50 && ReadPng(Path.Combine(pass.DirectoryPath, "green.png"), 3)[2] == 0, "valid RGB sources");
    var predicted = ReadPng(Path.Combine(pass.DirectoryPath, "green-recomposed.png"), 3);
    for (int i = 0; i < knownAlpha.Length; i++)
        for (int c = 0; c < 3; c++)
            Check(Math.Abs(predicted[3*i+c] - green[4*i+c]) <= 2, "predicted green sample");

    byte[] lateGreen = (byte[])green.Clone();
    lateGreen[0] = 255; // on empty black and white only
    var review = Export(lateGreen);
    Check(!review.Passed && review.GreenMismatchPixels == 1 && review.ForegroundPixels == 5,
        "green-only particle included in foreground");
    Check(File.Exists(Path.Combine(review.DirectoryPath, "candidate.png"))
        && !File.Exists(Path.Combine(review.DirectoryPath, "effect.png")), "review files are distinct");
    var report = JsonDocument.Parse(File.ReadAllText(Path.Combine(review.DirectoryPath, "report.json"))).RootElement;
    Check(report.GetProperty("commonBackgroundPixels").GetInt32() == 1, "foreground denominator");
    byte[] moved = (byte[])dark.Clone();
    moved[0] = 255;
    var movement = StudioMatte.Export(root, width, height, dark, white, green, moved, white, default);
    Check(!movement.Passed && movement.RepeatBlackMismatchPixels == 1 && movement.ForegroundPixels == 5
        && ReadPng(Path.Combine(movement.DirectoryPath, "black-repeat.png"), 3)[0] == 255, "last black detects motion");
    var whiteMotion = (byte[])white.Clone();
    whiteMotion[2] = 180;
    var lateWhite = StudioMatte.Export(root, width, height, dark, white, green, dark, whiteMotion, default);
    Check(!lateWhite.Passed && lateWhite.RepeatWhiteMismatchPixels == 1 && lateWhite.ForegroundPixels == 5
        && ReadPng(Path.Combine(lateWhite.DirectoryPath, "white-repeat.png"), 3)[2] == 180, "repeat white detects motion on black-invisible effect");
    using var lateReport = JsonDocument.Parse(File.ReadAllText(Path.Combine(lateWhite.DirectoryPath, "report.json")));
    Check(lateReport.RootElement.GetProperty("whiteVisiblePixels").GetInt32() == 4 &&
        lateReport.RootElement.GetProperty("repeatWhiteVisiblePixels").GetInt32() == 5, "original and repeat visibility counted separately");
    Check(lateReport.RootElement.GetProperty("whiteHoldMismatchPixels").ValueKind == JsonValueKind.Null
        && !File.Exists(Path.Combine(lateWhite.DirectoryPath, "white-hold.png")), "an absent early sample is recorded as unavailable");
    const string testedPath = "vfx/test/example.avfx";
    var earlyMotion = StudioMatte.Export(root, width, height, dark, white, green, dark, white, default, moved, whiteMotion, testedPath);
    using var earlyReport = JsonDocument.Parse(File.ReadAllText(Path.Combine(earlyMotion.DirectoryPath, "report.json")));
    Check(!earlyMotion.Passed && earlyMotion.Summary.Contains("在同一棚色下画面已经变化") && earlyMotion.ForegroundPixels == 5
        && earlyReport.RootElement.GetProperty("blackHoldMismatchPixels").GetInt32() == 1
        && earlyReport.RootElement.GetProperty("whiteHoldMismatchPixels").GetInt32() == 1
        && earlyReport.RootElement.GetProperty("effectPath").GetString() == testedPath, "movement before changing backdrop");
    Check(ReadPng(Path.Combine(earlyMotion.DirectoryPath, "black-hold.png"), 3)[0] == 255
        && ReadPng(Path.Combine(earlyMotion.DirectoryPath, "white-hold.png"), 3)[2] == 180, "unmodified early exposures");
    var blank = StudioMatte.Export(root, 1, 1, [0,0,0,0], [255,255,255,0], [0,255,0,0], [0,0,0,0], [255,255,255,0], default);
    Check(!blank.Passed && blank.ForegroundPixels == 0 && !File.Exists(Path.Combine(blank.DirectoryPath, "glow-add.png")), "blank shelf rejected");
    var opaque = StudioMatte.Export(root, 1, 1, [80,80,80,0], [80,80,80,0], [80,80,80,0], [80,80,80,0], [80,80,80,0], default);
    Check(!opaque.Passed, "missing common backdrop rejected");
    var clipped = StudioMatte.Export(root, 2, 1, [0,0,0,0, 120,50,80,0],
        [255,255,255,0, 255,90,251,0], [0,255,0,0, 120,255,80,0], [0,0,0,0, 120,50,80,0],
        [255,255,255,0, 255,90,251,0], default);
    Check(!clipped.Passed && clipped.BlackWhiteMismatchPixels == 1, "inconsistent black and white");
    byte[] darkField = new byte[65 * 4];
    byte[] greenField = new byte[darkField.Length];
    byte[] whiteField = Enumerable.Repeat((byte)255, darkField.Length).ToArray();
    for (int i = 0; i < 65; i++)
    {
        greenField[i * 4 + 1] = 255;
        if (i > 0) whiteField[i * 4 + 2] = 194;
    }
    var whiteOnly = StudioMatte.Export(root, 65, 1, darkField, whiteField, greenField, darkField, whiteField, default);
    Check(!whiteOnly.Passed && whiteOnly.Summary.Contains("在白棚明显") && whiteOnly.Summary.Contains("在黑棚和绿棚很淡"),
        "white-only color response is explained without guessing the material");
    CheckSyntheticGlow(root);
    var emptySkill = StudioMatte.ExportBlackSkill(root, 1, 1, [0,0,0,0], 0, default);
    Check(!emptySkill.Passed && emptySkill.ForegroundPixels == 0 && File.Exists(Path.Combine(emptySkill.DirectoryPath, "black.png"))
        && !File.Exists(Path.Combine(emptySkill.DirectoryPath, "glow-add.png")), "blank skill exposure preserves only the actual image and report");
    if (args.Length == 2 && args[0] == "--reference-0.8.0") CheckRecordedSample(root, args[1], fireball: false);
    else if (args.Length == 2 && args[0] == "--fire-0.8.0") CheckRecordedSample(root, args[1], fireball: true);
    else if (args.Length > 0) throw new ArgumentException("Usage: MatteChecks [--reference-0.8.0 or --fire-0.8.0 directory]");
    int existing = Directory.GetDirectories(root).Length;
    try { StudioMatte.Export(root, width, height, [], white, green, dark, white, default); throw new Exception("invalid size accepted"); }
    catch (ArgumentException) { }
    try { StudioMatte.Export(root, width, height, dark, white, green, dark, white, new CancellationToken(true)); throw new Exception("cancellation ignored"); }
    catch (OperationCanceledException) { }
    Check(Directory.GetDirectories(root).Length == existing && !Directory.EnumerateDirectories(root, ".pending-*").Any(),
        "invalid or cancelled export leaves no partial results");
    Console.WriteLine("PASS: alpha/PNG, held-out green, motion in repeat black and white, visibility counts, bad pair, cancellation and diagnostic outputs.");
    return 0;

    StudioMatteResult Export(byte[] g) => StudioMatte.Export(root, width, height, dark, white, g, dark, white, default);
    byte[] Capture(byte[] background)
    {
        byte[] pixels = new byte[width*height*4];
        for (int i = 0; i < knownAlpha.Length; i++)
        {
            double coverage = knownAlpha[i] / 255.0;
            for (int c = 0; c < 3; c++)
            {
                double foreground = (i + c + 1) / 10.0;
                double light = coverage * foreground + (1 - coverage) * background[c] / 255.0;
                double srgb = light <= 0.0031308 ? 12.92 * light : 1.055 * Math.Pow(light, 1 / 2.4) - 0.055;
                pixels[4*i+c] = (byte)Math.Round(255 * srgb);
            }
            pixels[4*i+3] = 11; // GPU targets can have unused alpha.
        }
        return pixels;
    }
}
catch (Exception e) { Console.Error.WriteLine(e); return 1; }
finally { if (Directory.Exists(root)) Directory.Delete(root, true); }

static void Check(bool ok, string name)
{
    if (!ok) throw new Exception("Failed: " + name);
}

// Optional read-only regression for the original 2560x1440 four-exposure session.
// There was no fifth image in 0.8.0. Reuse white solely to exercise the original, recorded color statistics.
static void CheckRecordedSample(string temporaryOutput, string recording, bool fireball)
{
    byte[] Load(string name)
    {
        byte[] rgb = ReadPng(Path.Combine(recording, name + ".png"), 3);
        Check(rgb.Length == 2560 * 1440 * 3, "recorded dimensions");
        byte[] rgba = new byte[2560 * 1440 * 4];
        for (int src = 0, dst = 0; src < rgb.Length; src += 3, dst += 4)
            rgb.AsSpan(src, 3).CopyTo(rgba.AsSpan(dst, 3));
        return rgba;
    }
    byte[] black = Load("black"), white = Load("white");
    var result = StudioMatte.Export(temporaryOutput, 2560, 1440, black, white, Load("green"), Load("black-repeat"), white, default);
    using var report = JsonDocument.Parse(File.ReadAllText(Path.Combine(result.DirectoryPath, "report.json")));
    int Field(string key) => report.RootElement.GetProperty(key).GetInt32();
    if (fireball)
    {
        Check(!result.Passed && result.ForegroundPixels == 59462 && result.GreenMismatchPixels == 51151 &&
            result.RepeatBlackMismatchPixels == 2521, "ordinary fireball matte still requires review");
        var additive = report.RootElement.GetProperty("additive");
        Check(additive.GetProperty("greenMismatchPixels").GetInt32() == 3729 && additive.GetProperty("worstGreenChannelError").GetInt32() == 113
            && additive.GetProperty("worstBlackChannelError").GetInt32() == 0 && additive.GetProperty("worstWhiteChannelError").GetInt32() == 0,
            "real quantized additive recomposition measurements");
        Check(File.Exists(Path.Combine(result.DirectoryPath, "glow-add.png")) && !File.Exists(Path.Combine(result.DirectoryPath, "effect.png")),
            "only single-exposure additive candidate exported");
        var skill = StudioMatte.ExportBlackSkill(temporaryOutput, 2560, 1440, black, 40, default);
        Check(!skill.Passed && File.ReadAllBytes(Path.Combine(skill.DirectoryPath, "glow-add.png"))
            .SequenceEqual(File.ReadAllBytes(Path.Combine(result.DirectoryPath, "glow-add.png"))), "real black plate yields the same emission with no second exposure");
        Console.WriteLine("PASS: real fireball stays in review; quantized additive errors on black/white 0, green 3729/59462, worst green error 113.");
        return;
    }
    Check(!result.Passed && result.Summary.Contains("在白棚明显") && result.ForegroundPixels == 251025, "historical diagnostic classification");
    Check(Field("blackVisiblePixels") == 183 && Field("whiteVisiblePixels") == 250914 && Field("greenVisiblePixels") == 0,
        "visibility of the three original exposures");
    Check(Field("repeatBlackVisiblePixels") == 1 && Field("repeatBlackMismatchPixels") == 0 && Field("worstRepeatBlackChannelError") == 7,
        "motion and visibility in the real repeated black");
    Check(result.BlackWhiteMismatchPixels == 251025 && result.GreenMismatchPixels == 250914,
        "independent green and black-white residuals match the original diagnostic");
    Check(!File.Exists(Path.Combine(result.DirectoryPath, "glow-add.png")), "no emission layer for the recorded white-only ring");
    Console.WriteLine("PASS: real 0.8.0 foreground 251025; black 183, white 250914, green 0; repeat black max difference 7. Repeated white was not recorded.");
}

static void CheckSyntheticGlow(string root)
{
    byte[] black = [0,0,0,0, 200,90,15,0, 3,1,0,0], white = Enumerable.Repeat((byte)255, 12).ToArray();
    byte[] green = [0,255,0,0, 200,255,15,0, 3,255,0,0];
    var result = StudioMatte.Export(root, 3, 1, black, white, green, black, white, default);
    Check(!result.Passed && result.Summary.Contains("添加") && !File.Exists(Path.Combine(result.DirectoryPath, "effect.png")),
        "single exposure does not approve standard alpha");
    var rgba = ReadPng(Path.Combine(result.DirectoryPath, "glow-add.png"), 4);
    var skill = StudioMatte.ExportBlackSkill(root, 3, 1, black, 87, default);
    Check(!skill.Passed && skill.GreenMismatchPixels == null && skill.RepeatWhiteMismatchPixels == null
        && skill.Summary.Contains("本次只拍了黑棚") && skill.ForegroundPixels == 1, "one cast results cannot be reported as verified RGB or alpha");
    Check(Directory.GetFiles(skill.DirectoryPath).Select(Path.GetFileName).OrderBy(x=>x)
        .SequenceEqual(new[] { "black.png", "glow-add.png", "report.json" }), "single shot writes exactly its source, emission and report");
    using var skillReport = JsonDocument.Parse(File.ReadAllText(Path.Combine(skill.DirectoryPath, "report.json")));
    var meta = skillReport.RootElement;
    Check(!meta.GetProperty("ordinaryAlphaTested").GetBoolean() && !meta.GetProperty("threeBackdropComparison").GetBoolean()
        && meta.GetProperty("offsetMs").GetInt32() == 87 && meta.GetProperty("sourceMode").GetString() == "技能单次施放",
        "single-shot validation scope and observed timing");
    Check(ReadPng(Path.Combine(skill.DirectoryPath, "glow-add.png"), 4).SequenceEqual(rgba), "single-shot uses the tested linear emission encoder");
    Check(rgba[3] == 0 && rgba[7] is > 0 and < 255 && rgba[11] == 1, "transparent surround, fractional alpha and retained dim light");
    static double ToLinear(byte value)
    {
        double x = value / 255.0;
        return x <= 0.04045 ? x / 12.92 : Math.Pow((x + 0.055) / 1.055, 2.4);
    }
    static int ToSrgb(double light) => (int)Math.Round(255 * (light <= 0.0031308
        ? 12.92 * light : 1.055 * Math.Pow(light, 1 / 2.4) - 0.055));
    foreach (var (background, observed) in new (byte[] Base, byte[] Image)[] { ([0,0,0], black), ([255,255,255], white), ([0,255,0], green) })
        for (int i = 0; i < rgba.Length; i += 4)
            for (int c = 0; c < 3; c++)
            {
                double emission = ToLinear(rgba[i + c]) * rgba[i + 3] / 255.0;
                int display = ToSrgb(Math.Min(1, ToLinear(background[c]) + emission));
                Check(Math.Abs(display - observed[i + c]) <= 1, "independent linear plus-blend from decoded PNG");
            }
}

static byte[] ReadPng(string path, int channels)
{
    byte[] bytes = File.ReadAllBytes(path);
    int pos = 8, width = 0, height = 0;
    using var idat = new MemoryStream();
    while (pos < bytes.Length)
    {
        int size = BinaryPrimitives.ReadInt32BigEndian(bytes.AsSpan(pos));
        var type = bytes.AsSpan(pos+4, 4);
        var data = bytes.AsSpan(pos+8, size);
        uint crc = uint.MaxValue;
        foreach (byte b in bytes.AsSpan(pos+4, size+4))
        {
            crc ^= b;
            for (int bit = 0; bit < 8; bit++) crc = (crc & 1) != 0 ? (crc >> 1) ^ 0xedb88320u : crc >> 1;
        }
        Check(~crc == BinaryPrimitives.ReadUInt32BigEndian(bytes.AsSpan(pos+8+size)), "PNG chunk CRC");
        if (type.SequenceEqual("IHDR"u8))
        {
            width = BinaryPrimitives.ReadInt32BigEndian(data);
            height = BinaryPrimitives.ReadInt32BigEndian(data[4..]);
            Check(data[8] == 8 && data[9] == (channels == 4 ? 6 : 2), "PNG color format");
        }
        if (type.SequenceEqual("IDAT"u8)) idat.Write(data);
        pos += 12 + size;
    }
    idat.Position = 0;
    using var zip = new ZLibStream(idat, CompressionMode.Decompress);
    var pixels = new byte[width*height*channels];
    for (int y = 0; y < height; y++)
    {
        Check(zip.ReadByte() == 1, "PNG filter");
        var row = pixels.AsSpan(y*width*channels, width*channels);
        zip.ReadExactly(row);
        for (int j = channels; j < row.Length; j++) row[j] = unchecked((byte)(row[j] + row[j-channels]));
    }
    Check(zip.ReadByte() == -1, "PNG raster length");
    return pixels;
}
