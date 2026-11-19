using System.Buffers.Binary;
using System.IO.Compression;
using System.Text.Json;

namespace RoundTableStudio;

internal sealed record StudioMatteResult(string DirectoryPath, bool Passed, long ForegroundPixels,
    long? GreenMismatchPixels, long? BlackWhiteMismatchPixels, long? RepeatBlackMismatchPixels,
    long? RepeatWhiteMismatchPixels, string Summary);

internal static class StudioMatte
{
    private const int Tolerance = 8;
    private static readonly double[] Linear = Enumerable.Range(0, 256).Select(i =>
    {
        double s = i / 255.0;
        return s <= 0.04045 ? s / 12.92 : Math.Pow((s + 0.055) / 1.055, 2.4);
    }).ToArray();
    private static readonly byte[] Srgb = Enumerable.Range(0, 65536).Select(i =>
    {
        double l = i / 65535.0;
        return (byte)Math.Round(255 * (l <= 0.0031308 ? 12.92 * l : 1.055 * Math.Pow(l, 1 / 2.4) - 0.055));
    }).ToArray();

    // CPU/IO only. All input buffers belong to the caller and must remain immutable until this returns.
    internal static StudioMatteResult Export(string parentDirectory, int width, int height,
        byte[] blackRgba, byte[] whiteRgba, byte[] greenRgba, byte[] repeatBlackRgba, byte[] repeatWhiteRgba,
        CancellationToken cancellation, byte[]? blackHold = null, byte[]? whiteHold = null, string? effectPath = null)
    {
        cancellation.ThrowIfCancellationRequested();
        if (width <= 0 || height <= 0) throw new ArgumentOutOfRangeException(nameof(width), "画面尺寸无效。");
        int bytes = checked(width * height * 4);
        if (blackRgba.Length != bytes || whiteRgba.Length != bytes || greenRgba.Length != bytes
            || repeatBlackRgba.Length != bytes || repeatWhiteRgba.Length != bytes
            || (blackHold != null && blackHold.Length != bytes) || (whiteHold != null && whiteHold.Length != bytes))
            throw new ArgumentException("截图的分辨率或像素格式不一致。");

        var earlyBlack = blackHold ?? blackRgba;
        var earlyWhite = whiteHold ?? whiteRgba;
        var effect = new byte[bytes];
        var reconstructed = new byte[bytes];
        long foreground = 0, blackWhiteErrors = 0, greenErrors = 0;
        long visibleBlack = 0, visibleWhite = 0, visibleGreen = 0, visibleRepeatBlack = 0, visibleRepeatWhite = 0;
        long repeatBlackErrors = 0, repeatWhiteErrors = 0;
        long blackHoldErrors = 0, whiteHoldErrors = 0;
        int worstGreen = 0, worstRepeatBlack = 0, worstRepeatWhite = 0;
        for (int i = 0; i < bytes; i += 4)
        {
            if ((i & 0x3ffff) == 0) cancellation.ThrowIfCancellationRequested();
            // The union includes particles seen only in an earlier same-color sample or a later repeat.
            int blackSignal = 0, whiteSignal = 0, greenSignal = 0, repeatBlackSignal = 0, repeatWhiteSignal = 0;
            int repeatBlackError = 0, repeatWhiteError = 0;
            int holdSignal = 0, blackHoldError = 0, whiteHoldError = 0;
            for (int c = 0; c < 3; c++)
            {
                int p = i + c;
                blackSignal = Math.Max(blackSignal, blackRgba[p]);
                whiteSignal = Math.Max(whiteSignal, 255 - whiteRgba[p]);
                greenSignal = Math.Max(greenSignal, Math.Abs(greenRgba[p] - (c == 1 ? 255 : 0)));
                repeatBlackSignal = Math.Max(repeatBlackSignal, repeatBlackRgba[p]);
                repeatWhiteSignal = Math.Max(repeatWhiteSignal, 255 - repeatWhiteRgba[p]);
                repeatBlackError = Math.Max(repeatBlackError, Math.Abs(repeatBlackRgba[p] - blackRgba[p]));
                repeatWhiteError = Math.Max(repeatWhiteError, Math.Abs(repeatWhiteRgba[p] - whiteRgba[p]));
                holdSignal = Math.Max(holdSignal, Math.Max(earlyBlack[p], 255 - earlyWhite[p]));
                blackHoldError = Math.Max(blackHoldError, Math.Abs(earlyBlack[p] - blackRgba[p]));
                whiteHoldError = Math.Max(whiteHoldError, Math.Abs(earlyWhite[p] - whiteRgba[p]));
            }
            int signal = Math.Max(Math.Max(blackSignal, whiteSignal), Math.Max(greenSignal, Math.Max(repeatBlackSignal, repeatWhiteSignal)));
            signal = Math.Max(signal, holdSignal);
            if (signal == 0)
            {
                reconstructed[i + 1] = 255;
                continue;
            }

            // Same least-squares estimate and sRGB transfer as tools/export-alpha.ps1, LinearSRGB mode.
            double transmission = Math.Clamp(((Linear[whiteRgba[i]] - Linear[blackRgba[i]])
                + (Linear[whiteRgba[i+1]] - Linear[blackRgba[i+1]])
                + (Linear[whiteRgba[i+2]] - Linear[blackRgba[i+2]])) / 3, 0, 1);
            double alpha = 1 - transmission;
            byte storedAlpha = (byte)Math.Round(255 * alpha);
            effect[i+3] = storedAlpha;
            double coverage = storedAlpha / 255.0;
            bool badPair = false;
            int greenError = 0;
            for (int c = 0; c < 3; c++)
            {
                int p = i + c;
                double premultiplied = (Linear[blackRgba[p]] + Linear[whiteRgba[p]] - transmission) / 2;
                effect[p] = storedAlpha == 0 ? (byte)0 : Encode(premultiplied / alpha);
                double foregroundLight = Linear[effect[p]] * coverage;
                byte onBlack = Encode(foregroundLight);
                byte onWhite = Encode(foregroundLight + 1 - coverage);
                // Predict green without consulting greenRgba during the solve.
                reconstructed[p] = c == 1 ? onWhite : onBlack;
                badPair |= Math.Abs(onBlack - blackRgba[p]) > Tolerance || Math.Abs(onWhite - whiteRgba[p]) > Tolerance;
                greenError = Math.Max(greenError, Math.Abs(reconstructed[p] - greenRgba[p]));
            }
            if (signal <= Tolerance) continue;
            foreground++;
            if (blackSignal > Tolerance) visibleBlack++;
            if (whiteSignal > Tolerance) visibleWhite++;
            if (greenSignal > Tolerance) visibleGreen++;
            if (repeatBlackSignal > Tolerance) visibleRepeatBlack++;
            if (repeatWhiteSignal > Tolerance) visibleRepeatWhite++;
            if (badPair) blackWhiteErrors++;
            if (greenError > Tolerance) greenErrors++;
            if (repeatBlackError > Tolerance) repeatBlackErrors++;
            if (repeatWhiteError > Tolerance) repeatWhiteErrors++;
            if (blackHoldError > Tolerance) blackHoldErrors++;
            if (whiteHoldError > Tolerance) whiteHoldErrors++;
            worstGreen = Math.Max(worstGreen, greenError);
            worstRepeatBlack = Math.Max(worstRepeatBlack, repeatBlackError);
            worstRepeatWhite = Math.Max(worstRepeatWhite, repeatWhiteError);
        }

        bool hasBackground = foreground < (long)width * height;
        bool passed = foreground > 0 && hasBackground && blackWhiteErrors == 0 && greenErrors == 0
            && repeatBlackErrors == 0 && repeatWhiteErrors == 0 && blackHoldErrors == 0 && whiteHoldErrors == 0;
        string summary = foreground == 0 ? "各张截图中都未检出明显前景，本次只保存诊断。"
            : !hasBackground ? "未找到各张截图共同的空棚区域，请让特效周围留出底色。"
            : passed ? "透明 PNG 已导出，黑白还原、绿底对照、黑白复拍均通过。"
            : $"已保存待复核样片。黑白还原超差 {blackWhiteErrors:N0}，绿底超差 {greenErrors:N0}。黑底复拍变化 {repeatBlackErrors:N0}，白底复拍变化 {repeatWhiteErrors:N0} 像素。";
        if (!passed && foreground > 0)
        {
            if (blackHoldErrors > 0 || whiteHoldErrors > 0)
            {
                summary += " 在同一棚色下画面已经变化。";
                if (blackHoldErrors > 0) summary += $" 黑棚有 {blackHoldErrors:N0} 个像素变化。";
                if (whiteHoldErrors > 0) summary += $" 白棚有 {whiteHoldErrors:N0} 个像素变化。";
            }
            // This label only describes a visibility imbalance. It never changes the acceptance threshold.
            if ((blackWhiteErrors > 0 || greenErrors > 0) && visibleWhite >= 64
                && visibleBlack * 20 <= visibleWhite && visibleGreen * 20 <= visibleWhite)
                summary += " 特效在白棚明显，在黑棚和绿棚很淡，本次透明还原未通过。";
            summary += $" 黑、白、绿棚分别检出 {visibleBlack:N0}、{visibleWhite:N0}、{visibleGreen:N0} 个可见像素。";
        }
        var additive = !passed && foreground > 0 && hasBackground
            ? CheckAdditive(blackRgba, whiteRgba, greenRgba, foreground, greenErrors,
                visibleBlack > 0 && visibleGreen > 0 && visibleWhite == 0, cancellation)
            : null;
        if (additive?.Pixels != null)
            summary += " 另存了单张黑棚提取的发光素材 glow-add.png，请在线性光空间使用「添加」叠加。";

        string destination = WriteDirectory(parentDirectory, passed ? "frame-" : "review-", cancellation, staging =>
        {
            void WritePng(string name, byte[] pixels, bool withAlpha) =>
                SavePng(Path.Combine(staging, name), width, height, pixels, withAlpha, cancellation);

            WritePng("black.png", blackRgba, false);
            WritePng("white.png", whiteRgba, false);
            WritePng("green.png", greenRgba, false);
            WritePng("black-repeat.png", repeatBlackRgba, false);
            WritePng("white-repeat.png", repeatWhiteRgba, false);
            if (blackHold != null) WritePng("black-hold.png", blackHold, false);
            if (whiteHold != null) WritePng("white-hold.png", whiteHold, false);
            WritePng(passed ? "effect.png" : "candidate.png", effect, true);
            WritePng("green-recomposed.png", reconstructed, false);
            if (additive?.Pixels is { } glow) WritePng("glow-add.png", glow, true);
            File.WriteAllText(Path.Combine(staging, "report.json"), JsonSerializer.Serialize(new
            {
                passed, width, height, effectPath, foregroundPixels = foreground, commonBackgroundPixels = (long)width * height - foreground,
                blackWhiteMismatchPixels = blackWhiteErrors, greenMismatchPixels = greenErrors,
                blackVisiblePixels = visibleBlack, whiteVisiblePixels = visibleWhite, greenVisiblePixels = visibleGreen,
                repeatBlackVisiblePixels = visibleRepeatBlack, repeatWhiteVisiblePixels = visibleRepeatWhite,
                repeatBlackMismatchPixels = repeatBlackErrors, worstRepeatBlackChannelError = worstRepeatBlack,
                repeatWhiteMismatchPixels = repeatWhiteErrors, worstRepeatWhiteChannelError = worstRepeatWhite,
                blackHoldMismatchPixels = blackHold == null ? (long?)null : blackHoldErrors,
                whiteHoldMismatchPixels = whiteHold == null ? (long?)null : whiteHoldErrors,
                worstGreenChannelError = worstGreen, toleranceInSrgb8 = Tolerance, blendSpace = "LinearSRGB",
                additive = additive == null ? null : new
                {
                    file = additive.Pixels == null ? null : "glow-add.png",
                    blendMode = "Add (Linear Dodge)", blendSpace = "LinearSRGB", source = "black.png",
                    blackMismatchPixels = additive.BlackMismatch, whiteMismatchPixels = additive.WhiteMismatch,
                    greenMismatchPixels = additive.GreenMismatch, worstBlackChannelError = additive.WorstBlack,
                    worstWhiteChannelError = additive.WorstWhite, worstGreenChannelError = additive.WorstGreen,
                    scope = "Single-exposure emission only. Quantized straight RGB times alpha is added to linear-light background."
                },
                foregroundDefinition = "Union of pixels differing from their expected backdrop by more than the tolerance in any RGB channel.",
                scope = "Recomposition against black, white and green, agreement between repeats, and comparison with earlier same-color samples when available."
            }, new JsonSerializerOptions { WriteIndented = true }));
        });
        return new(destination, passed, foreground, greenErrors, blackWhiteErrors, repeatBlackErrors, repeatWhiteErrors, summary);
    }

    private static byte Encode(double light) => Srgb[(int)Math.Round(65535 * Math.Clamp(light, 0, 1))];

    internal static StudioMatteResult ExportBlackSkill(string parentDirectory, int width, int height,
        byte[] black, int offsetMs, CancellationToken cancellation)
    {
        cancellation.ThrowIfCancellationRequested();
        if (width <= 0 || height <= 0 || black.Length != checked(width * height * 4))
            throw new ArgumentException("黑棚截图的尺寸或像素格式无效。");

        byte[] glow = new byte[black.Length];
        long visible = 0, blackErrors = 0;
        int worstBlack = 0;
        for (int i = 0; i < black.Length; i += 4)
        {
            if ((i & 0x3ffff) == 0) cancellation.ThrowIfCancellationRequested();
            int peak = AdditiveEncoding.WritePixel(black, i, glow);
            if (peak > Tolerance) visible++;
            int error = 0;
            for (int c = 0; c < 3; c++)
            {
                int value = black[i + c];
                error = Math.Max(error, Math.Abs(AdditiveEncoding.OnBlack[peak * 256 + value] - value));
            }
            if (error > Tolerance) blackErrors++;
            worstBlack = Math.Max(worstBlack, error);
        }
        bool hasGlow = visible > 0 && blackErrors == 0;
        string summary = hasGlow
            ? "已保存单次施放的黑棚原帧 black.png 和发光素材 glow-add.png。请在线性光空间使用「添加」叠加。"
            : visible == 0 ? "已保存单次施放的黑棚原帧 black.png，画面中未检出明显特效。"
            : "已保存单次施放的黑棚原帧 black.png，发光素材叠回黑底时超出容差。";
        summary += " 本次只拍了黑棚，未做白棚、绿棚对照或普通透明度验证。";
        string destination = WriteDirectory(parentDirectory, "black-skill-", cancellation, staging =>
        {
            SavePng(Path.Combine(staging, "black.png"), width, height, black, false, cancellation);
            if (hasGlow) SavePng(Path.Combine(staging, "glow-add.png"), width, height, glow, true, cancellation);
            File.WriteAllText(Path.Combine(staging, "report.json"), JsonSerializer.Serialize(new
            {
                sourceMode = "技能单次施放", width, height, offsetMs, source = "black.png", blackVisiblePixels = visible,
                passed = false, ordinaryAlphaTested = false, threeBackdropComparison = false, toleranceInSrgb8 = Tolerance,
                additive = new
                {
                    file = hasGlow ? "glow-add.png" : null, blendMode = "Add (Linear Dodge)", blendSpace = "LinearSRGB",
                    blackMismatchPixels = blackErrors, worstBlackChannelError = worstBlack,
                    whiteMismatchPixels = (long?)null, greenMismatchPixels = (long?)null
                },
                scope = "Single black exposure of one cast. Straight-alpha emission derived from and recomposed onto black only."
            }, new JsonSerializerOptions { WriteIndented = true }));
        });
        return new(destination, false, visible, null, null, null, null, summary);
    }

    private static string WriteDirectory(string parent, string prefix, CancellationToken token, Action<string> write)
    {
        token.ThrowIfCancellationRequested();
        string root = Path.GetFullPath(parent);
        string suffix = $"{DateTime.Now:yyyyMMdd-HHmmss}-{Guid.NewGuid():N}";
        string staging = Path.Combine(root, ".pending-" + suffix);
        string destination = Path.Combine(root, prefix + suffix);
        Directory.CreateDirectory(staging);
        try
        {
            write(staging);
            token.ThrowIfCancellationRequested();
            Directory.Move(staging, destination);
            return destination;
        }
        finally
        {
            if (Directory.Exists(staging)) Directory.Delete(staging, recursive: true);
        }
    }

    private sealed record AdditiveResult(byte[]? Pixels, long BlackMismatch, long WhiteMismatch, long GreenMismatch,
        int WorstBlack, int WorstWhite, int WorstGreen);

    private static AdditiveResult CheckAdditive(byte[] black, byte[] white, byte[] green, long foreground, long ordinaryGreenErrors,
        bool candidatePossible, CancellationToken cancellation)
    {
        byte[]? pixels = candidatePossible ? new byte[black.Length] : null;
        long blackErrors = 0, whiteErrors = 0, greenErrors = 0;
        int worstBlack = 0, worstWhite = 0, worstGreen = 0;
        for (int i = 0; i < black.Length; i += 4)
        {
            if ((i & 0x3ffff) == 0) cancellation.ThrowIfCancellationRequested();
            int peak = AdditiveEncoding.WritePixel(black, i, pixels);
            int row = peak * 256;
            int blackError = 0, whiteError = 0, greenError = 0;
            for (int c = 0; c < 3; c++)
            {
                int p = i + c;
                int index = row + black[p];
                byte onBlack = AdditiveEncoding.OnBlack[index];
                // On a saturated primary, addition clips to 255 in the lit channel and reproduces the source in the others.
                blackError = Math.Max(blackError, Math.Abs(onBlack - black[p]));
                whiteError = Math.Max(whiteError, 255 - white[p]);
                greenError = Math.Max(greenError, Math.Abs((c == 1 ? 255 : onBlack) - green[p]));
            }
            if (blackError > Tolerance) blackErrors++;
            if (whiteError > Tolerance) whiteErrors++;
            if (greenError > Tolerance) greenErrors++;
            worstBlack = Math.Max(worstBlack, blackError);
            worstWhite = Math.Max(worstWhite, whiteError);
            worstGreen = Math.Max(worstGreen, greenError);
        }
        // These checks select an optional single-exposure file; they never change the standard matte verdict.
        if (blackErrors > 0 || whiteErrors > 0 || greenErrors >= ordinaryGreenErrors || greenErrors * 10 >= foreground)
            pixels = null;
        return new(pixels, blackErrors, whiteErrors, greenErrors, worstBlack, worstWhite, worstGreen);
    }

    private static class AdditiveEncoding
    {
        internal static readonly byte[] Alpha = new byte[256];
        internal static readonly byte[] Color = new byte[256 * 256];
        internal static readonly byte[] OnBlack = new byte[256 * 256];

        internal static int WritePixel(byte[] source, int i, byte[]? output)
        {
            int peak = Math.Max(source[i], Math.Max(source[i + 1], source[i + 2]));
            if (output != null)
            {
                int row = peak * 256;
                for (int c = 0; c < 3; c++) output[i + c] = Color[row + source[i + c]];
                output[i + 3] = Alpha[peak];
            }
            return peak;
        }

        static AdditiveEncoding()
        {
            for (int peak = 1; peak <= 255; peak++)
            {
                // Round upward so faint nonzero emission survives and no straight channel must exceed one.
                Alpha[peak] = (byte)Math.Ceiling(255 * Linear[peak]);
                double coverage = Alpha[peak] / 255.0;
                for (int channel = 0; channel <= peak; channel++)
                {
                    int index = peak * 256 + channel;
                    Color[index] = Quantize(Linear[channel] / coverage);
                    OnBlack[index] = Quantize(Linear[Color[index]] * coverage);
                }
            }
        }

        private static byte Quantize(double light) => (byte)Math.Clamp(Math.Round(255 * (light <= 0.0031308
            ? 12.92 * light : 1.055 * Math.Pow(light, 1 / 2.4) - 0.055)), 0, 255);
    }

    private static void SavePng(string path, int width, int height, byte[] rgba, bool withAlpha, CancellationToken token)
    {
        using var file = new FileStream(path, FileMode.CreateNew, FileAccess.Write, FileShare.None);
        file.Write(new byte[] { 137, 80, 78, 71, 13, 10, 26, 10 });
        Span<byte> header = stackalloc byte[13];
        header.Clear();
        BinaryPrimitives.WriteInt32BigEndian(header, width);
        BinaryPrimitives.WriteInt32BigEndian(header[4..], height);
        header[8] = 8;
        header[9] = withAlpha ? (byte)6 : (byte)2;
        WriteChunk(file, "IHDR"u8, header);
        WriteChunk(file, "sRGB"u8, [0]);

        using var compressed = new MemoryStream();
        int channels = withAlpha ? 4 : 3;
        var scanline = new byte[1 + width * channels];
        using (var zip = new ZLibStream(compressed, CompressionLevel.Fastest, leaveOpen: true))
        {
            scanline[0] = 1; // PNG Sub filter
            for (int y = 0; y < height; y++)
            {
                token.ThrowIfCancellationRequested();
                int source = y * width * 4;
                int output = 1;
                for (int x = 0; x < width; x++, source += 4)
                    for (int c = 0; c < channels; c++)
                        scanline[output++] = unchecked((byte)(rgba[source+c] - (x == 0 ? 0 : rgba[source+c-4])));
                zip.Write(scanline);
            }
        }
        WriteChunk(file, "IDAT"u8, compressed.GetBuffer().AsSpan(0, checked((int)compressed.Length)));
        WriteChunk(file, "IEND"u8, []);
    }

    private static readonly uint[] CrcTable = Enumerable.Range(0, 256).Select(i =>
    {
        uint crc = (uint)i;
        for (int bit = 0; bit < 8; bit++) crc = (crc >> 1) ^ ((crc & 1) == 1 ? 0xedb88320u : 0u);
        return crc;
    }).ToArray();

    private static void WriteChunk(Stream stream, ReadOnlySpan<byte> type, ReadOnlySpan<byte> data)
    {
        Span<byte> word = stackalloc byte[4];
        BinaryPrimitives.WriteInt32BigEndian(word, data.Length);
        stream.Write(word);
        stream.Write(type);
        stream.Write(data);
        uint crc = uint.MaxValue;
        foreach (byte b in type) crc = CrcTable[(byte)(crc ^ b)] ^ (crc >> 8);
        foreach (byte b in data) crc = CrcTable[(byte)(crc ^ b)] ^ (crc >> 8);
        BinaryPrimitives.WriteUInt32BigEndian(word, ~crc);
        stream.Write(word);
    }
}
