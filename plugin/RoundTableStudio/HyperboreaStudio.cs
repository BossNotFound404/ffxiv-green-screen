using System.Numerics;
using System.Reflection;
using System.Runtime.CompilerServices;
using Dalamud.Bindings.ImGui;
using Dalamud.Game.ClientState.Conditions;
using Dalamud.Plugin;
using Dalamud.Plugin.Services;
using StudioActor = FFXIVClientStructs.FFXIV.Client.Game.Object.GameObject;

namespace RoundTableStudio;

// Public managed members used by the 1.0.1.2 Load Zone and Revert
// controls. Resolve the loaded plugin per operation so a reload cannot leave
// us holding a delegate, configuration, or instance from its old context.
internal sealed class HyperboreaStudio(IDalamudPluginInterface pi, IObjectTable objects,
    IPlayerState player, IClientState client, ICondition condition)
{
    private static readonly Guid SupportedModule = new("224fefb0-3833-4c36-a9a8-82c3a374062b");
    private const string Incompatible = "当前 Hyperborea 版本未适配，请使用它的 Load Zone 或 Revert。";

    private (uint Inn, ulong Character)? entered;
    private WeakReference<object>? originRecord;
    private Vector3? arrivalPosition;

    internal sealed class EnableRequired() : InvalidOperationException("请在旅馆勾选 Enable Hyperborea，录制棚会继续进棚。") { }

    public bool IsReady()
    {
        try { Resolve(); return true; }
        catch (EnableRequired) { return false; }
    }

    // ImGui queues a named widget for the following frame. Only draw the host's
    // control if the same session still needs the change when that frame arrives.
    public bool DrawSwitch(Capture run, bool enable, ref int queuedFrame, Action save)
    {
        RequireOrdinaryView();
        if (player.ContentId != run.Character || client.TerritoryType != run.Inn || Stage.LoadedTerritory() != run.Inn)
            throw new InvalidOperationException("正在等待本角色回到原旅馆。");
        var host = FindHost();
        bool before = host.Enabled;
        if (before == enable || !enable && (run.HyperOwner != host.Identity
            || run.HyperOrigin.HasValue && run.HyperOrigin != host.OriginIdentity))
            return true;
        if (!host.Call<bool>("IsInInnInternal"))
            throw new InvalidOperationException("Hyperborea 未确认已在实际旅馆。");
        object?[] args = [null];
        var allowed = (bool)Method(host.Utils, "CanEnablePlugin", typeof(List<string>).MakeByRefType()).Invoke(null, args)!;
        if (!allowed || condition[ConditionFlag.Mounted])
            throw new InvalidOperationException("Hyperborea 暂不允许切换，请下坐骑并等旅馆恢复普通状态。");

        var flags = ImGuiWindowFlags.NoMouseInputs | ImGuiWindowFlags.NoSavedSettings | ImGuiWindowFlags.NoScrollbar
            | ImGuiWindowFlags.NoCollapse | ImGuiWindowFlags.NoFocusOnAppearing | ImGuiWindowFlags.NoDocking;
        ImGui.SetNextWindowSize(new Vector2(310, 84));
        var visible = ImGui.Begin(enable ? "正在启用 Hyperborea###RoundTableHyperOn" : "正在还原 Hyperborea###RoundTableHyperOff", flags);
        try
        {
            if (!visible) return false;
            var frame = ImGui.GetFrameCount();
            if (queuedFrame != frame)
            {
                ImGui.TextUnformatted(enable ? "正在准备摄影棚…" : "正在还原旅馆设置…");
                ImGuiP.ActivateItem(ImGui.GetID("Enable Hyperborea"));
                queuedFrame = frame + 1;
                return false;
            }
            if (enable)
            {
                run.HyperOwner = host.Identity;
                try { save(); } catch { run.HyperOwner = null; throw; }
            }
            try
            {
                Method(host.Ui, "DrawNeo").Invoke(null, null);
                if (host.Enabled != enable)
                    throw new InvalidOperationException("Hyperborea 开关未生效，请检查其原面板状态。");
            }
            catch
            {
                if (enable && !host.Enabled) { run.HyperOwner = null; save(); }
                throw;
            }
            return true;
        }
        finally { ImGui.End(); }
    }

    public void Enter(uint inn, Vector3 currentPosition, Vector3? spawn = null)
    {
        if (entered.HasValue) throw new InvalidOperationException("本次进棚已经提交。");
        RequireOrdinaryView();
        if (inn == 0 || client.TerritoryType != inn || Stage.LoadedTerritory() != inn || !Finite(currentPosition))
            throw new InvalidOperationException("请在原旅馆内站稳后再进棚。");
        if (spawn.HasValue && !Finite(spawn.Value)) throw InvalidPosition();
        var host = Resolve();
        if (!host.Call<bool>("IsInInnInternal"))
            throw new InvalidOperationException("Hyperborea 未确认当前位于实际旅馆。");
        var origin = host.SavedZone.GetValue(null);
        if (origin != null && (uint)Field(origin.GetType(), "ZoneId").GetValue(origin)! != inn)
            throw new InvalidOperationException("Hyperborea 已保存另一处返回地点，请先在其面板完成返回。");
        if (Field(host.Ui, "SavedPos").GetValue(null) is not Vector3 savedPosition || !Finite(savedPosition))
            throw new InvalidOperationException("Hyperborea 尚未保存旅馆站位，请在旅馆重新启用后再进棚。");

        var loader = Method(host.Utils, "LoadZone", typeof(uint), typeof(bool), typeof(bool),
            typeof(int), typeof(int), typeof(int), typeof(int), typeof(int));
        // Match the native button's SavedZoneState ??=, also on a partial
        // load: Revert needs the original inn to recover.
        if (origin == null)
        {
            origin = Activator.CreateInstance(host.SavedZone.FieldType, inn, currentPosition)!;
            host.SavedZone.SetValue(null, origin);
        }
        var character = player.ContentId;
        // The host samples its optional default spawn during loading.
        // A studio-selected position is applied only after the layout is ready.
        // The host's optional phase switch would overwrite the studio weather.
        loader.Invoke(null, [Stage.Territory, !spawn.HasValue, false, 0, 0, 1, 1, 0]);
        entered = (inn, character);
        originRecord = new(origin);
        arrivalPosition = spawn;
    }

    public int? OriginIdentity => originRecord != null && originRecord.TryGetTarget(out var origin)
        ? RuntimeHelpers.GetHashCode(origin) : null;

    public void CompleteArrival(uint inn, ulong character)
    {
        if (arrivalPosition is not { } target) return;
        MoveWithinStudio(inn, character, target);
        arrivalPosition = null;
    }

    public unsafe void MoveWithinStudio(uint inn, ulong character, Vector3 target)
    {
        if (!Finite(target)) throw InvalidPosition();
        if (entered != (inn, character) || !player.IsLoaded || player.ContentId != character)
            throw new InvalidOperationException("原地调整站位需要由录制棚从本角色的旅馆进棚。");
        InStudio(inn);
        var actor = objects.LocalPlayer;
        if (actor == null || actor.Address == 0) throw new InvalidOperationException("棚内角色尚未载入。");
        // The same actor operation as the Hyperborea Compass position button.
        ((StudioActor*)actor.Address)->SetPosition(target.X, target.Y, target.Z);
    }

    public void Return(uint inn)
    {
        try
        {
            var host = InStudio(inn);
            Method(host.Utils, "Revert").Invoke(null, null);
        }
        catch (Exception e)
        {
            throw new InvalidOperationException($"{e.GetBaseException().Message} 请打开 Hyperborea，点 Revert 返回原旅馆。", e);
        }
        Forget();
    }

    public void Forget() { entered = null; originRecord = null; arrivalPosition = null; }

    private Host InStudio(uint inn)
    {
        RequireOrdinaryView();
        if (inn is 0 or Stage.Territory || Stage.LoadedTerritory() != Stage.Territory)
            throw new InvalidOperationException("请等本地 448 摄影棚载入完成。");
        var host = Resolve();
        var origin = host.SavedZone.GetValue(null);
        if (origin == null || (uint)Field(origin.GetType(), "ZoneId").GetValue(origin)! != inn)
            throw new InvalidOperationException("Hyperborea 的返程记录与本次旅馆不符，请使用原面板确认。");
        if (originRecord != null && (!originRecord.TryGetTarget(out var owned) || !ReferenceEquals(origin, owned)))
            throw new InvalidOperationException("Hyperborea 的会话已更换，请使用原面板确认返程地点。");
        return host;
    }

    private void RequireOrdinaryView()
    {
        if (!client.IsLoggedIn || !player.IsLoaded || player.ContentId == 0 || client.IsGPosing
            || condition[ConditionFlag.BoundByDuty] || condition[ConditionFlag.BoundByDuty56]
            || condition[ConditionFlag.BoundByDuty95] || condition[ConditionFlag.BetweenAreas]
            || condition[ConditionFlag.BetweenAreas51])
            throw new InvalidOperationException("请在本角色的普通视角下，等过图结束后再操作。");
    }

    private Host Resolve()
    {
        var host = FindHost();
        if (!host.Enabled) throw new EnableRequired();
        if (!host.Call<bool>("CanUse"))
            throw new InvalidOperationException("Hyperborea 尚未允许载入，请检查原面板状态。");
        return host;
    }

    private Host FindHost()
    {
        foreach (var assembly in AppDomain.CurrentDomain.GetAssemblies())
        {
            if (assembly.GetName().Name != "Hyperborea") continue;
            var owner = pi.GetPlugin(assembly);
            if (owner is not { InternalName: "Hyperborea", IsLoaded: true }) continue;
            if (assembly.ManifestModule.ModuleVersionId != SupportedModule) continue;
            var main = assembly.GetType("Hyperborea.Hyperborea", true)!;
            var plugin = Field(main, "P").GetValue(null);
            if (plugin == null || !(bool)Field(main, "AllowedOperation").GetValue(plugin)!)
                throw new InvalidOperationException("Hyperborea 尚未就绪，请检查其原面板状态。");
            if ((bool)Field(main, "Bypass").GetValue(plugin)!)
                throw new InvalidOperationException("请在 Hyperborea 关闭调试限制绕过选项，再使用录制棚。");
            return new Host(assembly, plugin);
        }
        throw new InvalidOperationException("未找到已加载的适配版 Hyperborea 1.0.1.2，请检查插件安装或使用原面板。");
    }

    private static bool Finite(Vector3 v) => float.IsFinite(v.X) && float.IsFinite(v.Y) && float.IsFinite(v.Z);
    private static InvalidOperationException InvalidPosition() => new("摄影站位的 X、Y、Z 均须为有限数值。");
    private static MethodInfo Method(Type type, string name, params Type[] parameters)
        => type.GetMethod(name, BindingFlags.Public | BindingFlags.Static, parameters)
           ?? throw new InvalidOperationException(Incompatible);
    private static FieldInfo Field(Type type, string name)
        => type.GetField(name, BindingFlags.Public | BindingFlags.Static | BindingFlags.Instance)
           ?? throw new InvalidOperationException(Incompatible);

    private sealed class Host(Assembly assembly, object plugin)
    {
        public bool Enabled => (bool)Field(plugin.GetType(), "Enabled").GetValue(plugin)!;
        public int Identity => RuntimeHelpers.GetHashCode(plugin);
        public int? OriginIdentity => SavedZone.GetValue(null) is { } origin ? RuntimeHelpers.GetHashCode(origin) : null;
        public Type Utils { get; } = assembly.GetType("Hyperborea.Utils", true)!;
        public Type Ui { get; } = assembly.GetType("Hyperborea.Gui.UI", true)!;
        public FieldInfo SavedZone => Field(Ui, "SavedZoneState");

        public T Call<T>(string name) => (T)Method(Utils, name).Invoke(null, null)!;
    }
}
