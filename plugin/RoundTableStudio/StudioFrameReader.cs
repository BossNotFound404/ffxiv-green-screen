using Dalamud.Interface.Textures;
using Dalamud.Plugin.Services;
using FFXIVClientStructs.FFXIV.Client.UI;

namespace RoundTableStudio;

internal sealed record StudioFrame(int Width, int Height, byte[] Rgba)
{
    // Check each region separately: the sky, the floor and even individual walls can finish at different times.
    internal bool HasStudioBackground(byte weather)
    {
        if (weather is not (46 or 47 or 48) || Width < 64 || Height < 64
            || (long)Width * Height * 4 != Rgba.Length)
            return false;

        var rb = weather == 46 ? 255 : 0;
        var green = weather == 47 ? 0 : 255;
        var totalMatches = 0;
        // Four strips on each side cover the whole rim and leave the middle free for the effect.
        for (var side = 0; side < 4; side++)
        for (var section = 0; section < 4; section++)
        {
            var matches = 0;
            for (var sample = 0; sample < 64; sample++)
            {
                var band = sample / 8;
                var along = (section * 64 + (sample % 8) * 8 + band + 0.5) / 256;
                var inset = (band + 0.5) / 100;
                var horizontal = side < 2;
                var cross = side % 2 == 0 ? inset : 1 - inset;
                var x = (int)((horizontal ? along : cross) * Width);
                var y = (int)((horizontal ? cross : along) * Height);
                // Shift the pixel parity to avoid aliasing a 1px checkerboard dissolve.
                if (((x + y + sample) & 1) != 0)
                {
                    if (horizontal)
                        x += x == Width - 1 ? -1 : 1;
                    else
                        y += y == Height - 1 ? -1 : 1;
                }
                var offset = (y * Width + x) * 4;
                if (Math.Abs(Rgba[offset] - rb) <= 3 && Math.Abs(Rgba[offset + 1] - green) <= 3
                    && Math.Abs(Rgba[offset + 2] - rb) <= 3)
                    matches++;
            }
            if (matches < 55)
                return false;
            totalMatches += matches;
        }
        return totalMatches >= 1000; // At least 97.65% of all 1,024 samples.
    }
}

internal sealed class StudioFrameReader(ITextureProvider textures, ITextureReadbackProvider readback)
{
    private const int Rgba8Unorm = 28; // DXGI_FORMAT_R8G8B8A8_UNORM

    internal async Task<StudioFrame> CaptureAsync(uint viewportId, CancellationToken token)
    {
        token.ThrowIfCancellationRequested();
        if (viewportId == 0)
            throw new InvalidOperationException("游戏主画面尚未就绪。");

        // The main swap chain contains the game, including its native HUD, before Dalamud draws any windows.
        using var texture = await textures.CreateFromImGuiViewportAsync(new ImGuiViewportTextureArgs
        {
            ViewportId = viewportId,
            TakeBeforeImGuiRender = true,
            AutoUpdate = false,
        }, "Green Screen capture", token).ConfigureAwait(false);
        var (spec, data) = await readback.GetRawImageAsync(texture, new TextureModificationArgs
        {
            DxgiFormat = Rgba8Unorm,
            MakeOpaque = true,
        }, true, token).ConfigureAwait(false);
        token.ThrowIfCancellationRequested();

        if (spec.DxgiFormat != Rgba8Unorm || spec.Width <= 0 || spec.Height <= 0)
            throw new InvalidDataException("读取的游戏画面格式无效。");
        var stride = checked(spec.Width * 4);
        if (spec.Pitch < stride || (long)spec.Pitch * (spec.Height - 1) + stride > data.Length)
            throw new InvalidDataException("读取的游戏画面不完整。");
        if (spec.Pitch == stride && data.Length == (long)stride * spec.Height)
            return new StudioFrame(spec.Width, spec.Height, data);

        var packed = new byte[checked(stride * spec.Height)];
        for (var y = 0; y < spec.Height; y++)
            Buffer.BlockCopy(data, y * spec.Pitch, packed, y * stride, stride);
        return new StudioFrame(spec.Width, spec.Height, packed);
    }
}

// Create, inspect, and release on the game thread. The native visibility action is the same one observed by Dalamud.
internal sealed unsafe class StudioHudScope : IDisposable
{
    private readonly nint moduleAddress;
    private bool restore;

    private StudioHudScope(RaptureAtkModule* module)
    {
        moduleAddress = (nint)module;
        if (module->IsUiVisible)
        {
            module->SetUiVisibility(false);
            restore = true;
        }
    }

    internal static StudioHudScope Hide()
    {
        var module = RaptureAtkModule.Instance();
        if (module == null)
            throw new InvalidOperationException("游戏界面尚未就绪。");
        return new StudioHudScope(module);
    }

    internal bool IsHidden
    {
        get
        {
            var module = RaptureAtkModule.Instance();
            return (nint)module == moduleAddress && !module->IsUiVisible;
        }
    }

    public void Dispose()
    {
        if (restore && IsHidden)
            ((RaptureAtkModule*)moduleAddress)->SetUiVisibility(true);
        restore = false;
    }
}
