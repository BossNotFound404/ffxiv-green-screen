using Dalamud.Plugin;
using System.Security.Cryptography;
using Options = System.Collections.Generic.IReadOnlyDictionary<string, System.Collections.Generic.IReadOnlyList<string>>;
using SavedOptions = System.Collections.Generic.Dictionary<string, System.Collections.Generic.List<string>>;

namespace RoundTableStudio;

internal sealed class Shelf(IDalamudPluginInterface pi)
{
    private const string StableStage = "Round Table Studio - Stable Stage v0.16";
    private const string SinglePassStage = "Round Table Studio - Capture Stage v0.17";
    internal bool UseSinglePassSky { get; set; }
    private (string Name, string FileName, string Hash) Package => UseSinglePassSky
        ? (SinglePassStage, "RoundTable_CaptureStage_v0.17.pmp", "5824fe4a956ae8de59ddc3df65950686c4573e6ab3716c1d7c6f9351d452903b")
        : (StableStage, "RoundTable_StableStage_v0.16.pmp", "6556cbdc8cff5793aef80e900ce53a273b77ec5252fafd746d71066120388bfe");
    internal const string NeutralToplights = "四色中性顶灯（试验）";
    private const string Source = "RoundTableStudio";
    private const int Key = -1381258068;
    private const byte DefaultCollection = 224; // Penumbra.Api.Enums.ApiCollectionType.Default
    private static readonly HashSet<string> OtherStageNames = new(StringComparer.Ordinal)
    {
        "Round Table Studio - Floor Probe v0.1", "Round Table Studio - Environment Probe v0.2",
        "Round Table Studio - Stage Light Probe v0.3", "Round Table Studio - Main Group Probe v0.4",
        "Round Table Studio - Center Light Probe v0.5", "Round Table Studio - Light Off Floors v0.6",
        "Round Table Studio - RGB Floor Probe v0.7", "Round Table Studio - Red Depth Probe v0.8",
        "Round Table Studio - Material Pair v0.9", "Round Table Studio - Five Weather Scene Layer v0.10",
        "Round Table Studio - Solid Red Floor v0.11", "Round Table Studio - Projected Floors v0.12",
        "Round Table Studio - Sky and Light v0.13", "Round Table Studio - Quiet Stage v0.14",
        "Round Table Studio - Matched Stage v0.15", StableStage, SinglePassStage,
    };

    public void Import()
    {
        var package = Package;
        var path = Path.Combine(pi.AssemblyLocation.DirectoryName!, package.FileName);
        if (!File.Exists(path)) throw new InvalidOperationException($"缺少 {package.FileName}，请将随附棚包留在插件 DLL 旁。");
        using (var stream = File.OpenRead(path))
        {
            if (Convert.ToHexStringLower(SHA256.HashData(stream)) != package.Hash)
                throw new InvalidOperationException("随附棚包与本次测试版本的校验值不符。");
        }
        Check(pi.GetIpcSubscriber<string, int>("Penumbra.InstallMod.V5").InvokeFunc(path), "提交棚包导入");
    }

    public (Guid Collection, string Directory, string CollectionName)? Find()
    {
        var list = pi.GetIpcSubscriber<Dictionary<string, string>>("Penumbra.GetModList").InvokeFunc();
        var matches = list.Where(kv => kv.Value == Package.Name).Select(kv => kv.Key).ToArray();
        if (matches.Length == 0) return null;
        if (matches.Length != 1)
            throw new InvalidOperationException($"在 Penumbra 中找到了多份「{Package.Name}」，请检查重复条目。");
        var collection = pi.GetIpcSubscriber<byte, (Guid Id, string Name)?>("Penumbra.GetCollection").InvokeFunc(DefaultCollection);
        if (collection == null || collection.Value.Id == Guid.Empty)
            throw new InvalidOperationException("Penumbra 的默认场景集合不可用。");
        return (collection.Value.Id, matches[0], collection.Value.Name);
    }

    private (int Code, (bool, bool, int, SavedOptions)? Settings, string Source) Query(Guid id, string mod) =>
        pi.GetIpcSubscriber<Guid, string, string, int, (int, (bool, bool, int, SavedOptions)?, string)>(
            "Penumbra.QueryTemporaryModSettings.V5").InvokeFunc(id, mod, "", Key);

    public void CheckAvailable(Guid id, string mod)
    {
        var current = Query(id, mod);
        Check(current.Code, "读取临时设置");
        if (current.Settings != null) throw new InvalidOperationException("该棚包已有临时设置。请先在 Penumbra 核对其来源。");
    }

    public void Enable(Guid id, string mod, bool neutralToplights)
    {
        CheckAvailable(id, mod);
        var settings = pi.GetIpcSubscriber<Guid, string, string, bool, bool, int,
            (int, (bool Enabled, int Priority, SavedOptions Settings, bool, bool)?)>(
            "Penumbra.GetCurrentModSettingsWithTemp");
        var (code, current) = settings.InvokeFunc(id, mod, "", false, true, Key);
        Check(code, "读取原有选项");
        var priority = current?.Priority ?? 0;
        var list = pi.GetIpcSubscriber<Dictionary<string, string>>("Penumbra.GetModList").InvokeFunc();
        foreach (var (directory, name) in list)
        {
            if (directory == mod || !OtherStageNames.Contains(name)) continue;
            var (priorCode, prior) = settings.InvokeFunc(id, directory, "", false, true, Key);
            Check(priorCode, "读取旧棚包优先级");
            if (prior is not { Enabled: true } active || active.Priority < priority) continue;
            if (active.Priority == int.MaxValue)
                throw new InvalidOperationException("旧棚包已使用最高优先级，请先停用旧棚包。");
            priority = active.Priority + 1;
        }
        var options = current?.Settings.ToDictionary(k => k.Key, k => (IReadOnlyList<string>)k.Value)
            ?? new Dictionary<string, IReadOnlyList<string>>();
        if (neutralToplights) options["人物灯光"] = new[] { NeutralToplights };
        var result = pi.GetIpcSubscriber<Guid, string, string, (bool, bool, int, Options), string, int, int>(
            "Penumbra.SetTemporaryModSettings.V5").InvokeFunc(id, mod, "", (false, true, priority, options), Source, Key);
        Check(result, "临时启用棚包");
        if (neutralToplights)
        {
            var saved = Query(id, mod);
            Check(saved.Code, "核对本次灯光选项");
            if (saved.Settings == null || !saved.Settings.Value.Item4.TryGetValue("人物灯光", out var selected)
                || !selected.Contains(NeutralToplights))
                throw new InvalidOperationException("四色中性顶灯未生效，请核对棚包是否完整。");
        }
    }

    public void Verify(string mod)
    {
        var root = pi.GetIpcSubscriber<string>("Penumbra.GetModDirectory").InvokeFunc();
        if (!Path.IsPathFullyQualified(root)) throw new InvalidOperationException("无法读取 Penumbra 的模组目录。");
        var directory = Path.GetFullPath(Path.Combine(root, mod)) + Path.DirectorySeparatorChar;
        var resolve = pi.GetIpcSubscriber<string, string>("Penumbra.ResolveDefaultPath");
        const string group = "bgcommon/env/obset/ex1_obset/obset_abr_a2/obset_a2_fld/obset_a2fb_";
        var paths = new List<string> {
            "bgcommon/env/global/ex1_genv/genv_abr_a2/genv_a2_fld/genv_a2fb.envb",
            "bg/ex1/03_abr_a2/fld/a2fb/bgparts/a2fb_a1_boss2.mdl",
            "bg/ex4/05_zon_z5/fld/z5fd/material/z5fd_k6_emv01a.mtrl",
            "bg/ex4/05_zon_z5/fld/z5fd/texture/z5fd_k6_emv01_d.tex",
            "bg/ex1/03_abr_a2/common/vfx/eff/b0673_1cng_y.avfx",
            "bg/ex1/03_abr_a2/common/vfx/eff/b0798_edge_y.avfx",
            "bgcommon/env/sound/abr_a2/a2_fld/sound_a2fb.essb",
            "bgcommon/sound/abr/abr_spot_a2fb_change_start.scd",
            "bgcommon/sound/fst/placednpc_a2fb_circledamagefloor_on.scd"
        };
        foreach (string name in new[] { "default", "ultimate", "ultimate2", "vfx01", "vfx02", "vfx03", "vfx04", "vfx05" })
            paths.Add(group + name + ".obsb");
        for (var i = 1; i <= 5; i++)
            paths.Add($"bg/ex1/03_abr_a2/common/vfx/eff/b0673_{i}lnd_y.avfx");
        foreach (var gamePath in paths)
        {
            var mapped = resolve.InvokeFunc(gamePath);
            if (!Path.IsPathFullyQualified(mapped) || !Path.GetFullPath(mapped).StartsWith(directory, StringComparison.OrdinalIgnoreCase))
                throw new InvalidOperationException($"棚包未生效，请检查 Penumbra 总开关及旧棚包的优先级。（{Path.GetFileName(gamePath)}）");
        }
    }

    public void Release(Guid id, string mod)
    {
        var current = Query(id, mod);
        // Missing collection/mod or a new owner's lock means our lease is gone.
        if (current.Code is 2 or 3 or 19) return;
        Check(current.Code, "核对临时设置");
        if (current.Settings == null || current.Source != Source) return;
        var code = pi.GetIpcSubscriber<Guid, string, string, int, int>(
            "Penumbra.RemoveTemporaryModSettings.V5").InvokeFunc(id, mod, "", Key);
        if (code is not (0 or 1 or 2 or 3)) Check(code, "撤销棚包");
    }

    private static void Check(int result, string action)
    {
        if (result != 0) throw new InvalidOperationException($"{action}失败，Penumbra 返回 {result}。");
    }
}
