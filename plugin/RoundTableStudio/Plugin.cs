using Dalamud.Bindings.ImGui;
using Dalamud.Configuration;
using Dalamud.Game.ClientState.Conditions;
using Dalamud.Game.Command;
using Dalamud.Game.Config;
using Dalamud.Plugin;
using Dalamud.Plugin.Services;
using System.Numerics;
using TerritoryType = Lumina.Excel.Sheets.TerritoryType;

namespace RoundTableStudio;

[Serializable]
public sealed class StudioConfig : IPluginConfiguration
{
    public int Version { get; set; } = 1;
    public Capture? Pending { get; set; }
    public byte LastColor { get; set; } = 47;
    public bool UseSpawn { get; set; }
    public float SpawnX { get; set; }
    public float SpawnY { get; set; }
    public float SpawnZ { get; set; }
}

public sealed partial class Plugin : IDalamudPlugin
{
    private readonly IDalamudPluginInterface pi;
    private readonly ICommandManager commands;
    private readonly IClientState client;
    private readonly IPlayerState player;
    private readonly IObjectTable objects;
    private readonly ICondition condition;
    private readonly IDataManager data;
    private readonly IFramework framework;
    private readonly IPluginLog log;
    private readonly IGameConfig gameConfig;
    private readonly Shelf shelf;
    private readonly StudioConfig config;
    private readonly ActorOpacity opacity;
    private readonly NameplateVisibility nameplate;
    private readonly StudioFreecam freecam;
    private readonly StudioTargetModel targetModel;
    private IReadOnlyList<(string Name, int ModelId)>? bossModels;
    private string? shownTarget;
    private int requestedModel;
    private Capture? returnAfterModelRestore;
    private readonly HyperboreaStudio travel;
    private readonly CameraDistance camera = new();
    private CameraRange? shownCamera;
    private bool? shownFreecam;
    private float requestedDistance = 30;
    private float requestedOpacity = 0.01f;
    private bool applyFirstColor;
    private bool resumeExtendedRange;
    private bool setPitchOnEntry;
    private bool positionOnEntry;
    private long positionDeadline;
    private float? shownOpacity;
    private Vector3 requestedPosition;
    private Vector3 entryPosition;
    private Vector3? shownPosition;
    private enum Journey { Ready, AwaitEnable, Entering, EntryHelp, Returning, ReturnHelp, RestoreHelp }
    private Journey journey;
    private bool? switchRequested;
    private int switchFrame = -1;
    private long switchDeadline;
    private long travelDeadline;
    private bool open;
    private bool neutralToplights;
    private bool verified;
    private long verifyUntil;
    private long nextTick;
    private bool showColors;
    private bool showShooting;
    private byte? selectedColor;
    private string cameraNotice = "";
    private uint? shownLight;
    private bool disposed;
    private (uint Inn, ulong Character, long Deadline)? import;
    private long importDeadline;
    private static readonly long ProcessStart = System.Diagnostics.Process.GetCurrentProcess().StartTime.ToUniversalTime().Ticks;
    private string status = "在旅馆点击「进入摄影棚」。";
    private uint Layout => client.IsLoggedIn ? Stage.LoadedTerritory() : 0;
    private ulong Character => player.IsLoaded ? player.ContentId : 0;
    private bool InDuty => condition[ConditionFlag.BoundByDuty] || condition[ConditionFlag.BoundByDuty56]
        || condition[ConditionFlag.BoundByDuty95];

    public Plugin(IDalamudPluginInterface pi, ICommandManager commands, IClientState client,
        IPlayerState player, IObjectTable objects, ICondition condition, IDataManager data, IFramework framework, IPluginLog log, IGameConfig gameConfig, INamePlateGui plates,
        ITextureProvider textures, ITextureReadbackProvider readback, ITargetManager targets)
    {
        this.pi = pi;
        this.commands = commands;
        this.client = client;
        this.player = player;
        this.objects = objects;
        opacity = new ActorOpacity(objects);
        exportOpacity = new ActorOpacity(objects);
        frameReader = new StudioFrameReader(textures, readback);
        previewFreeze = new PreviewFreeze(pi);
        targetModel = new StudioTargetModel(pi, targets, objects, data);
        nameplate = new NameplateVisibility(plates, objects, player);
        freecam = new StudioFreecam(pi, commands, CanControlCamera);
        travel = new HyperboreaStudio(pi, objects, player, client, condition);
        this.condition = condition;
        this.data = data;
        this.framework = framework;
        this.log = log;
        this.gameConfig = gameConfig;
        shelf = new Shelf(pi);
        config = (StudioConfig?)pi.GetPluginConfig() ?? new();
        entryPosition = new(config.SpawnX, config.SpawnY, config.SpawnZ);
        requestedPosition = entryPosition;
        if (config.Pending is { } recovery)
        {
            recovery.Closing = true;
            if (recovery.ProcessStart != ProcessStart)
            {
                recovery.EntryRequestedAt = 0;
                recovery.HyperOwner = recovery.HyperOrigin = null;
                recovery.StudioLayout = 0;
                recovery.Original = recovery.Last = null;
                recovery.PriorFade = null;
                recovery.MutedAmbient = false;
                recovery.OriginalLamps.Clear();
            }
            open = true;
            status = "正在恢复上次录制。如果仍在棚内，将尝试返回原旅馆。";
        }
        commands.AddHandler("/rtstudio", new CommandInfo((_, argument) => StudioCommand(argument.Trim()))
            { HelpMessage = "打开 Green Screen / 绿幕。可加 进棚、退棚 或 黑、红、绿、蓝、白、中灰。" });
        recorderSceneUsage = pi.GetIpcSubscriber<bool>("VfxRecorder.IsUsingScene");
        studioTransition = pi.GetIpcProvider<bool>("RoundTableStudio.IsTransitioning");
        studioTransition.RegisterFunc(IsStudioTransitioning);
        pi.UiBuilder.Draw += Draw;
        pi.UiBuilder.OpenMainUi += Show;
        framework.Update += Tick;
    }

    private void Show() => open = true;
    private void Save() => pi.SavePluginConfig(config);

    private void StudioCommand(string argument)
    {
        if (argument.Length == 0) { Show(); return; }
        if (argument == "取消导出") { FromPanel(() => StopExport("已取消透明导出。")); return; }
        if (LocalMapsEnabled && argument == "截帧")
        {
            FromPanel(() =>
            {
                if (localMap is not { } capture)
                    throw new InvalidOperationException("请先在 Hyperborea 中载入本地地图。");
                TakeLocalMapFrame(capture);
            });
            return;
        }
        if (argument == "进棚") { Show(); FromPanel(Begin); return; }
        if (argument == "退棚")
        {
            Show();
            FromPanel(() =>
            {
                if (localMap is { } capture) ReturnFromLocalMap(capture);
                else if (config.Pending is { } run) End(run);
                else if (import.HasValue) { import = null; status = "已取消本次进棚。"; }
            });
            return;
        }
        foreach (var (name, id) in Stage.Colors)
        {
            if (argument != name) continue;
            FromPanel(() =>
            {
                if (localMap != null) throw new InvalidOperationException(LocalMapColorsUnavailable);
                if (config.Pending is not { } run)
                    throw new InvalidOperationException("请先在旅馆准备进棚。");
                Color(run, id);
            });
            return;
        }
        Show();
        status = "可用命令包括 /rtstudio 进棚、/rtstudio 退棚 和 /rtstudio 白。";
    }

    private bool IsInn(uint id)
    {
        var sheet = data.GetExcelSheet<TerritoryType>();
        return id != 0 && sheet.TryGetRow(id, out var row)
            && row.TerritoryIntendedUse.RowId == sheet.GetRow(177).TerritoryIntendedUse.RowId;
    }

    private void Begin()
    {
        RequireSceneAvailable();
        var inn = Layout;
        var character = Character;
        if (config.Pending != null || import.HasValue) return;
        if (LocalMapsEnabled && travel.DetectLocalMap(inn) != null)
            throw new InvalidOperationException("请在 Hyperborea 中点 Revert 回到原旅馆，再进入 448 摄影棚。");
        if (client.IsGPosing || InDuty || ChangingArea || character == 0 || client.TerritoryType != inn || !IsInn(inn))
            throw new InvalidOperationException("请先返回实际旅馆，等载入完成后再进棚。");
        var installed = shelf.Find();
        if (installed == null)
        {
            if (Environment.TickCount64 >= importDeadline)
            {
                shelf.Import();
                importDeadline = Environment.TickCount64 + 60000;
            }
            import = (inn, character, importDeadline);
            status = "正在首次导入棚包，完成后会自动继续。";
            nextTick = 0;
            return;
        }
        Prepare(inn, character, installed.Value);
    }

    private void WaitForImport()
    {
        if (import is not { } request) return;
        if (Layout != request.Inn || client.TerritoryType != request.Inn || Character != request.Character
            || InDuty || !client.IsLoggedIn || client.IsGPosing || Environment.TickCount64 >= request.Deadline)
        {
            import = null;
            throw new InvalidOperationException("本次进棚等待已结束。回到原旅馆再点「进入摄影棚」即可复用已导入的棚包。");
        }
        var installed = shelf.Find();
        if (installed == null) return;
        import = null;
        Prepare(request.Inn, request.Character, installed.Value);
    }

    private void Prepare(uint inn, ulong character, (Guid, string, bool) installed)
    {
        var (collection, mod, supportsMidGray) = installed;
        shelf.CheckAvailable(collection, mod);
        config.Pending = new Capture { Collection = collection, Mod = mod, SupportsMidGray = supportsMidGray,
            Inn = inn, Character = character, ProcessStart = ProcessStart };
        verified = false;
        applyFirstColor = true;
        exportNotice = "";
        exportDirectory = null;
        resumeExtendedRange = false;
        setPitchOnEntry = true;
        if (config.UseSpawn) requestedPosition = entryPosition;
        journey = Journey.Ready;
        showColors = false;
        cameraNotice = "";
        try { Save(); } catch { config.Pending = null; throw; }
        try { shelf.Enable(collection, mod, neutralToplights); }
        catch
        {
            config.Pending.Closing = true;
            Save();
            throw;
        }
        verifyUntil = Environment.TickCount64 + 5000;
        status = "正在准备摄影棚…";
    }

    private void VerifyResources(Capture run)
    {
        try { shelf.Verify(run.Mod, run.SupportsMidGray); }
        catch (Exception) when (Environment.TickCount64 < verifyUntil) { return; }
        catch
        {
            run.Closing = true;
            Save();
            throw;
        }
        verified = true;
        if (Layout == run.Inn) EnterStudio(run);
    }

    private void EnterStudio(Capture run)
    {
        if (!ReferenceEquals(config.Pending, run) || !verified || run.Closing || journey == Journey.Entering) return;
        journey = Journey.EntryHelp;
        if (Layout != run.Inn || client.TerritoryType != run.Inn || Character != run.Character
            || !IsInn(run.Inn) || ChangingArea || InDuty || client.IsGPosing || objects.LocalPlayer is not { } actor)
            throw new InvalidOperationException("请等原旅馆的角色和场景载入，再点「继续进棚」。");
        Vector3? spawn = config.UseSpawn ? entryPosition : null;
        run.EntryRequestedAt = Environment.TickCount64;
        Save();
        try { travel.Enter(run.Inn, actor.Position, spawn); }
        catch (HyperboreaStudio.EnableRequired)
        {
            run.EntryRequestedAt = 0;
            Save();
            journey = Journey.AwaitEnable;
            RequestSwitch(true);
            status = "正在启用 Hyperborea，随后自动进棚。";
            return;
        }
        catch (InvalidOperationException)
        {
            // A readiness check rejected the operation before the host loader ran.
            run.EntryRequestedAt = 0;
            Save();
            throw;
        }
        journey = Journey.Entering;
        if (LocalMapsEnabled || run.HyperOwner.HasValue)
        {
            run.HyperOrigin = travel.OriginIdentity;
            Save();
        }
        travelDeadline = Environment.TickCount64 + 30000;
        status = "正在进入摄影棚…";
    }

    private void ReturnToInn(Capture run)
    {
        if (!OwnsStudioLayout(run) || InDuty || client.IsGPosing || ChangingArea) return;
        journey = Journey.ReturnHelp;
        travel.Return(run.Inn, LocalMapsEnabled ? run.HyperOrigin : null);
        journey = Journey.Returning;
        travelDeadline = Environment.TickCount64 + 30000;
        run.SeenStudio = true;
        run.EntryRequestedAt = 0;
        Save();
        status = "正在返回进棚前的旅馆…";
    }

    private void RetryEntry(Capture run)
    {
        RequireSceneAvailable();
        if (journey != Journey.EntryHelp || !ReferenceEquals(config.Pending, run) || run.SeenStudio || run.Closing
            || Layout != run.Inn || client.TerritoryType != run.Inn || Character != run.Character
            || ChangingArea || InDuty || client.IsGPosing) return;
        travel.Forget();
        EnterStudio(run);
    }

    private bool Release(Capture run)
    {
        if (localMap == null) RestoreShot();
        RestoreLight(run);
        if (!run.ShelfReleased)
        {
            shelf.Release(run.Collection, run.Mod);
            run.ShelfReleased = true;
            run.Closing = true;
            Save();
        }
        if (run.HyperOwner.HasValue && run.ProcessStart == ProcessStart && client.IsLoggedIn
            && (Character == run.Character || Character == 0))
        {
            if (disposed) return false;
            if (Layout != run.Inn || client.TerritoryType != run.Inn || Character == 0 || InDuty || ChangingArea || client.IsGPosing)
            {
                if (localMap == null) status = "棚包已还原。回到原旅馆的普通视角后会恢复 Hyperborea 开关。";
                return false;
            }
            if (journey == Journey.RestoreHelp)
            {
                // The user may have finished recovery from the original panel.
                try { if (travel.IsReady()) return false; }
                catch (InvalidOperationException) { return false; }
                run.HyperOwner = run.HyperOrigin = null;
            }
            else
            {
                if (switchRequested != false)
                {
                    RequestSwitch(false);
                    status = "正在还原进棚前的 Hyperborea 开关。";
                }
                return false;
            }
        }
        switchRequested = null;
        config.Pending = null;
        try { Save(); }
        catch { config.Pending = run; throw; }
        travel.Forget();
        verified = false;
        showColors = false;
        journey = Journey.Ready;
        return true;
    }

    private void RequestSwitch(bool enabled)
    {
        switchRequested = enabled;
        switchFrame = -1;
        switchDeadline = Environment.TickCount64 + 8000;
    }

    private void SwitchFailed(bool enable, Exception error)
    {
        switchRequested = null;
        journey = enable ? Journey.EntryHelp : Journey.RestoreHelp;
        status = error.GetBaseException().Message + (enable ? " 可重试进棚或打开 Hyperborea。" : " 可点「重试还原」或在原面板关闭。");
        log.Warning(error, "Hyperborea switch failed");
    }

    private void DrawSwitch()
    {
        if (switchRequested is not bool enable || config.Pending is not { } run || disposed) return;
        if (run.Closing == enable) { switchRequested = null; return; }
        try
        {
            if (!travel.DrawSwitch(run, enable, ref switchFrame, Save)) return;
            switchRequested = null;
            if (!enable)
            {
                run.HyperOwner = run.HyperOrigin = null;
                Save();
            }
            journey = Journey.Ready;
            nextTick = 0;
        }
        catch (Exception e) { SwitchFailed(enable, e); }
    }

    private void RestoreInStudio(Capture run)
    {
        if (InDuty || !OwnsStudioLayout(run)) return;
        if (LocalMapsEnabled && !CanRestoreStudioScene(run)) return;
        if (run.OriginalLamps.Count > 0)
            run.RestoreLegacyLamps(Stage.LegacyLamps, Stage.ReadLegacyLamp, Stage.RestoreLegacyLamp, Save);
        if (run.MutedAmbient)
        {
            Stage.ToggleAmbient(true);
            run.MutedAmbient = false;
            Save();
        }
        if (run.Last.HasValue)
        {
            if (client.IsGPosing) throw new InvalidOperationException("请退出合影模式，恢复普通视角的天气。");
            var current = Stage.Target() ?? throw new InvalidOperationException("天气尚未就绪，稍后重试。");
            if (current == run.Last) Stage.RestoreFade(run.PriorFade);
            run.Restore(current, Stage.TryReplace);
            run.PriorFade = null;
            Save();
        }
    }

    private bool OwnsStudioLayout(Capture run)
    {
        if (!client.IsLoggedIn || Character != run.Character || Layout != Stage.Territory) return false;
        return !LocalMapsEnabled || !run.ShelfReleased && run.ProcessStart == ProcessStart &&
            (run.StudioLayout == 0 || run.StudioLayout == (long)Stage.LoadedLayout().Address);
    }

    private bool LostStudioLayout(Capture run, uint layout) => LocalMapsEnabled && !ChangingArea && Character != 0 && layout == Stage.Territory &&
        (!OwnsStudioLayout(run) || !travel.MatchesStudio(run.Inn, run.HyperOrigin));

    private void End(Capture run)
    {
        if (!ReferenceEquals(run, config.Pending) || localMap != null) return;
        if (journey == Journey.Returning || journey == Journey.Entering) return;
        RequireSceneAvailable();
        StopExport("已结束本次透明导出。");
        if (targetModel.IsActive || targetModel.IsRedrawing)
        {
            targetModel.Restore();
            if (targetModel.IsRedrawing)
            {
                returnAfterModelRestore = run;
                status = "正在还原目标模型，完成后自动退棚。";
                return;
            }
        }
        returnAfterModelRestore = null;
        if (switchRequested == true) switchRequested = null;
        if (journey == Journey.RestoreHelp) journey = Journey.Ready;
        RestoreShot();
        run.Closing = true;
        Save();
        RestoreLight(run);
        RestoreInStudio(run);
        if (run.ShouldRelease(Layout, Character) || LostStudioLayout(run, Layout))
        {
            if (Release(run)) status = "录制已结束，棚包已恢复到进棚前的设置。";
        }
        else if (Layout == Stage.Territory) ReturnToInn(run);
    }

    private void Tick(IFramework _)
    {
        if (disposed || framework.IsFrameworkUnloading) return;
        if (LocalMapsEnabled)
        {
            Act(ObserveLocalMap);
            PollLocalMapFrame();
        }
        if (!targetModel.HasReleasedPlayerModel && (targetModel.IsActive || targetModel.IsRedrawing)) Act(() =>
        {
            var notice = targetModel.Tick();
            if (notice != null) status = notice;
        });
        if (returnAfterModelRestore is { } exiting && !targetModel.IsRedrawing)
        {
            returnAfterModelRestore = null;
            if (ReferenceEquals(exiting, config.Pending)) Act(() => End(exiting));
        }
        TickExport();
        if (export?.SceneReleased != false && exportOpacity.Active) exportOpacity.Observe(Character);
        if (switchRequested is bool switching && Environment.TickCount64 >= switchDeadline)
            SwitchFailed(switching, new InvalidOperationException("Hyperborea 开关操作超时。"));
        if (opacity.Active || camera.HasChanges || nameplate.Hidden || freecam.Owned || targetModel.IsActive) Act(ObserveShot);
        if ((config.Pending == null && import == null && localMap == null) || Environment.TickCount64 < nextTick) return;
        nextTick = Environment.TickCount64 + (showShooting && open ? 80 : 350);
        if (import.HasValue) { Act(WaitForImport); return; }
        Act(() =>
        {
            var layout = Layout;
            var character = Character;
            var run = config.Pending;
            showColors = run != null && CanShootInStudio(run);
            showShooting = showColors || localMap != null && !LocalMapReturning && !client.IsGPosing;
            selectedColor = showColors ? Stage.Target() : null;
            shownLight = showShooting && gameConfig.TryGet(SystemConfigOption.CharaLight, out uint value) ? value : null;
            shownFreecam = showShooting ? freecam.Read() : null;
            shownCamera = showShooting && shownFreecam != true ? CameraDistance.Read() : null;
            shownOpacity = showShooting ? opacity.Read(character) : null;
            shownPosition = showShooting ? objects.LocalPlayer?.Position : null;
            shownTarget = showShooting ? targetModel.CurrentTargetName : null;
            if (showShooting && localMap != null) ApplyCameraSettings();
            if (run == null) return;
            if (journey == Journey.Entering && layout == Stage.Territory) journey = Journey.Ready;
            if (journey is Journey.Entering or Journey.Returning && Environment.TickCount64 >= travelDeadline)
            {
                journey = journey == Journey.Entering ? Journey.EntryHelp : Journey.ReturnHelp;
                status = "暂未读到目标场景。载入完成后可重试，或打开 Hyperborea。";
            }
            if (!client.IsLoggedIn || InDuty || run.ShouldRelease(layout, character) || LostStudioLayout(run, layout))
            {
                if (client.IsLoggedIn && !InDuty && run.Closing && character == run.Character
                    && run.AwaitingArrival(layout, Environment.TickCount64)) return;
                bool returned = run.SeenStudio && layout == run.Inn && character == run.Character && !InDuty;
                if (!Release(run)) return;
                if (localMap != null) return;
                if (returned) status = "已回到原旅馆，本次拍摄设置已还原。";
                else if (!status.Contains("失败") && !status.Contains("未指向"))
                    status = "本次进棚已结束，已撤销录制棚的临时设置。";
                return;
            }
            if (run.Closing)
            {
                RestoreInStudio(run);
                if (journey == Journey.Ready) ReturnToInn(run);
                return;
            }
            if (!verified) { VerifyResources(run); return; }
            if (!run.SeenStudio && layout == run.Inn && !ChangingArea && !client.IsGPosing)
            {
                if (journey == Journey.AwaitEnable)
                {
                    try { if (!travel.IsReady()) return; }
                    catch { switchRequested = null; journey = Journey.EntryHelp; throw; }
                    switchRequested = null;
                    EnterStudio(run);
                    return;
                }
                if (journey == Journey.Ready) { EnterStudio(run); return; }
            }
            if (!run.SeenStudio && layout == Stage.Territory && character == run.Character &&
                (!LocalMapsEnabled || !ChangingArea && !client.IsGPosing && travel.MatchesStudio(run.Inn, run.HyperOrigin)))
            {
                shelf.Verify(run.Mod, run.SupportsMidGray);
                run.SeenStudio = true;
                if (LocalMapsEnabled) run.StudioLayout = (long)Stage.LoadedLayout().Address;
                journey = Journey.Ready;
                positionOnEntry = config.UseSpawn;
                positionDeadline = Environment.TickCount64 + 15000;
                if (!config.UseSpawn && objects.LocalPlayer is { } actor) requestedPosition = actor.Position;
                Save();
                status = "";
            }
            if (showColors) ApplyEntrySettings(run, character);
            if (showColors && Stage.AmbientActive() == true)
            {
                if (!run.MutedAmbient)
                {
                    run.MutedAmbient = true;
                    Save();
                }
                Stage.ToggleAmbient(false);
            }
        });
    }

    private void ApplyEntrySettings(Capture run, ulong character)
    {
        RestoreExportColor(run);
        if (applyFirstColor && Stage.Target().HasValue && Stage.Fade().HasValue)
        {
            applyFirstColor = false;
            var color = run.SupportsColor(config.LastColor) ? config.LastColor : (byte)47;
            Act(() => Color(run, color, temporary: config.LastColor == 44 && !run.SupportsMidGray));
        }
        ApplyCameraSettings();
        if (positionOnEntry && (shownPosition.HasValue || Environment.TickCount64 >= positionDeadline))
        {
            positionOnEntry = false;
            Act(() => travel.CompleteArrival(run.Inn, character));
        }
    }

    private void ApplyCameraSettings()
    {
        if (resumeExtendedRange && shownCamera.HasValue && shownFreecam != true)
        {
            resumeExtendedRange = false;
            Act(camera.Enable);
        }
        if (setPitchOnEntry && shownCamera.HasValue && shownFreecam != true)
        {
            setPitchOnEntry = false;
            Act(camera.EnablePitch);
        }
    }

    private bool CanShootInStudio(Capture run) => !InDuty && !client.IsGPosing && !ChangingArea && verified
        && run.CanShoot(Layout, Character) && (!LocalMapsEnabled ||
            OwnsStudioLayout(run) && travel.MatchesStudio(run.Inn, run.HyperOrigin));

    private void RequireStudio(Capture run)
    {
        RequireSceneAvailable();
        if (!ReferenceEquals(run, config.Pending) || !CanShootInStudio(run))
            throw new InvalidOperationException(localMap != null ? LocalMapColorsUnavailable : "请在摄影棚的普通视角下操作。");
    }

    private bool CanShootInLocalMap(Capture run) => !client.IsGPosing && !LocalMapReturning && localMap is { } capture
        && ReferenceEquals(capture.Run, run) && SameLocalMap(capture);

    private void RequireShooting(Capture run)
    {
        if (ReferenceEquals(run, config.Pending)) RequireStudio(run);
        else
        {
            RequireSceneAvailable();
            if (!CanShootInLocalMap(run))
                throw new InvalidOperationException("请等 Hyperborea 的本地地图和角色加载完成。");
        }
    }

    private bool ChangingArea => condition[ConditionFlag.BetweenAreas] || condition[ConditionFlag.BetweenAreas51];

    private bool CanControlCamera() => localMap is { } capture ? CanShootInLocalMap(capture.Run)
        : config.Pending is { SeenStudio: true } run && client.IsLoggedIn
            && !InDuty && !ChangingArea && !client.IsGPosing && Character == run.Character
            && (OwnsStudioLayout(run) && (!LocalMapsEnabled || travel.MatchesStudio(run.Inn, run.HyperOrigin))
                || freecam.Owned && Layout == run.Inn && client.TerritoryType == run.Inn);

    private void ObserveShot()
    {
        var character = Character;
        if (client.IsGPosing && config.Pending is { Closing: false, SeenStudio: true } session
            && character == session.Character && Layout == Stage.Territory)
            setPitchOnEntry = true;
        if (localMap is { } capture ? !CanShootInLocalMap(capture.Run)
            : config.Pending is not { } run || !CanShootInStudio(run))
        {
            RestoreShot();
            return;
        }
        if (export?.SceneReleased != false && !exportOpacity.Active) opacity.Observe(character);
        camera.Observe();
        freecam.Observe();
    }

    private void RestoreShot()
    {
        if (LocalMapsEnabled)
        {
            var owner = localMap is { } capture && !SameLocalMap(capture) ? capture.Run
                : localMap == null && config.Pending is { SeenStudio: true } studio && !CanRestoreStudioScene(studio) ? studio : null;
            if (owner != null)
            {
                ReleaseShotOwnership(owner.Character);
                return;
            }
            if (releasedShotOwner != 0)
            {
                RestoreReleasedShot();
                return;
            }
        }
        if (targetModel.IsActive) targetModel.Restore();
        nameplate.Restore();
        // Cammy may still hold the world camera while the player is loading or in GPose.
        // Wait for the same character's normal view before releasing an owned camera.
        if (!freecam.Owned || (localMap?.Run ?? config.Pending) is not { } run || !client.IsLoggedIn || InDuty
            || Character != 0 && Character != run.Character
            || !client.IsGPosing && !ChangingArea && Layout != 0 && Character != 0)
            freecam.Restore();
        opacity.Restore(Character);
        camera.Restore();
    }

    private bool CanRestoreStudioScene(Capture run) => !ChangingArea && !InDuty
        && OwnsStudioLayout(run) && travel.MatchesStudio(run.Inn, run.HyperOrigin);

    private void Color(Capture run, byte color, bool temporary = false)
    {
        RequireStudio(run);
        if (!run.SupportsColor(color))
            throw new InvalidOperationException("当前棚包不支持该颜色。中灰棚需要 v0.18 稳定棚包或 v0.19 渐变棚包。");
        if (!temporary && export is { SceneReleased: false })
            throw new InvalidOperationException("透明导出正在换棚，请先取消拍摄。");
        applyFirstColor = false;
        var target = Stage.Target() ?? throw new InvalidOperationException("布景正在载入，请稍后再选颜色。");
        var refresh = Stage.Displayed() != color;
        if (target == color && !refresh)
        {
            if (!temporary && config.LastColor != color) { config.LastColor = color; Save(); }
            status = "";
            return;
        }
        var fade = Stage.Fade() ?? throw new InvalidOperationException("场景环境仍在加载。");
        var previous = run.PriorFade;
        if (fade != Stage.QuickFade || previous == null) run.PriorFade = fade;
        try { run.SetColor(target, color, Save, Stage.TryQuickReplace, refresh); }
        catch { run.PriorFade = previous; Save(); throw; }
        Stage.SwitchBackdrops(color);
        if (!temporary) { config.LastColor = color; Save(); }
        nextTick = 0;
        status = "";
    }

    private void SetLight(Capture run, uint? desired)
    {
        RequireShooting(run);
        if (desired.HasValue)
        {
            if (!gameConfig.TryGet(SystemConfigOption.CharaLight, out uint current))
                throw new InvalidOperationException("当前客户端无法读取角色照明。");
            run.SetLight(current, desired.Value, ReferenceEquals(run, config.Pending) ? Save : () => { }, ApplyLight);
        }
        else RestoreLight(run);
        nextTick = 0;
    }

    private void ApplyLight(uint level) => gameConfig.Set(SystemConfigOption.CharaLight, level);

    private void RestoreLight(Capture run)
    {
        if (!run.AppliedLight.HasValue) return;
        if (!gameConfig.TryGet(SystemConfigOption.CharaLight, out uint current))
            throw new InvalidOperationException("正在等待恢复角色照明。");
        run.RestoreLight(current, ApplyLight);
        if (ReferenceEquals(run, config.Pending)) Save();
    }

    private void Act(Action action)
    {
        try { action(); }
        catch (Exception e)
        {
            status = e is InvalidOperationException ? e.Message : $"操作未完成：{e.Message}";
            log.Warning(e, "Studio action: {Status}", status);
            nextTick = Environment.TickCount64 + 3000;
        }
    }

    private void FromPanel(Action action)
    {
        if (framework.IsInFrameworkUpdateThread) Act(action);
        else _ = framework.RunOnFrameworkThread(() => { if (!disposed) Act(action); });
    }

    private void FromCameraPanel(Capture run, Action change)
    {
        FromPanel(() =>
        {
            RequireShooting(run);
            cameraNotice = "";
            if (freecam.Read() == true || !CameraDistance.Read().HasValue) return;
            try { change(); }
            catch (InvalidOperationException e) { cameraNotice = e.Message; }
            nextTick = 0;
        });
    }

    private void Hyper()
    {
        if (!commands.ProcessCommand("/hyper"))
            status = "未找到 /hyper，请在卫月启用 Hyperborea。";
    }

    private void DrawEntrySettings()
    {
        if (!ImGui.TreeNode("进棚设置")) return;
        bool spawn = config.UseSpawn;
        if (ImGui.Checkbox("指定进棚坐标", ref spawn)) { config.UseSpawn = spawn; Save(); }
        if (spawn)
        {
            ImGui.InputFloat3("出生点 X / Y / Z", ref entryPosition);
            if (ImGui.IsItemDeactivatedAfterEdit()) FromPanel(SavePosition);
        }
        ImGui.Checkbox(Shelf.NeutralToplights, ref neutralToplights);
        var singlePassSky = shelf.UseSinglePassSky;
        if (ImGui.Checkbox("试用天幕渐变修正", ref singlePassSky)) FromPanel(() =>
        {
            if (config.Pending != null || import.HasValue) return;
            var inn = Layout;
            if (Character == 0 || ChangingArea || InDuty || client.IsGPosing || client.TerritoryType != inn || !IsInn(inn))
                throw new InvalidOperationException("请回到旅馆后再切换布景选项。");
            shelf.UseSinglePassSky = singlePassSky;
            importDeadline = 0;
        });
        ImGui.TreePop();
    }

    private void SavePosition()
    {
        if (!float.IsFinite(entryPosition.X) || !float.IsFinite(entryPosition.Y) || !float.IsFinite(entryPosition.Z))
            throw new InvalidOperationException("请输入有效的 X、Y、Z 坐标。");
        (config.SpawnX, config.SpawnY, config.SpawnZ) = (entryPosition.X, entryPosition.Y, entryPosition.Z);
        Save();
    }

    private void DrawShot(Capture run)
    {
        if (!ImGui.TreeNode("拍摄设置")) return;
        if (shownCamera is { } range)
        {
            ImGui.TextUnformatted($"镜头距离 {range.Current:0.0}　滚轮上限 {range.Maximum:0.0}");
            ImGui.SetNextItemWidth(110);
            if (ImGui.InputFloat("指定距离", ref requestedDistance, 1, 5, "%.1f")) cameraNotice = "";
            if (ImGui.Button("扩展至 100")) FromCameraPanel(run, camera.Enable);
            ImGui.SameLine();
            if (ImGui.Button("应用距离")) FromCameraPanel(run, () => camera.SetDistance(requestedDistance));
            ImGui.SameLine();
            if (ImGui.Button("还原视距")) FromCameraPanel(run, () => { resumeExtendedRange = false; camera.RestoreDistance(); });
            if (cameraNotice.Length != 0) ImGui.TextWrapped(cameraNotice);
        }
        ImGui.BeginDisabled(!shownFreecam.HasValue);
        var enableFreecam = shownFreecam != true;
        if (ImGui.Button(enableFreecam ? "开启 Freecam" : "关闭 Freecam"))
            FromPanel(() =>
            {
                RequireShooting(run);
                if (enableFreecam && freecam.Read() == false)
                {
                    // Cammy installs a separate preset. Return the world camera to
                    // its baseline before it saves that preset's initial values.
                    resumeExtendedRange |= camera.Enabled;
                    camera.Restore();
                }
                setPitchOnEntry = true;
                freecam.Set(enableFreecam);
                cameraNotice = "";
                nextTick = 0;
            });
        ImGui.EndDisabled();
        if (!shownFreecam.HasValue) { ImGui.SameLine(); ImGui.TextDisabled("需要启用 Cammy 2.1.1.2"); }
        if (shownOpacity.HasValue)
        {
            ImGui.TextUnformatted($"当前角色透明度 {shownOpacity:0.00}");
            ImGui.SetNextItemWidth(110);
            ImGui.InputFloat("指定透明度", ref requestedOpacity, 0.01f, 0.1f, "%.2f");
            if (ImGui.Button("设为 0.01")) FromPanel(() => { RequireShooting(run); opacity.Apply(Character); });
            ImGui.SameLine();
            if (ImGui.Button("应用透明度")) FromPanel(() => { RequireShooting(run); opacity.Apply(Character, requestedOpacity); });
            ImGui.SameLine();
            if (ImGui.Button("还原透明度")) FromPanel(() => { RequireShooting(run); opacity.Restore(Character); });
        }
        var hidePlate = nameplate.Hidden;
        if (ImGui.Checkbox("隐藏本人头顶名牌", ref hidePlate))
            FromPanel(() =>
            {
                RequireShooting(run);
                if (hidePlate) nameplate.Hide(Character, localMap != null ? () => CanShootInLocalMap(run) : null);
                else nameplate.Restore();
            });
        if (ImGui.IsItemHovered()) ImGui.SetTooltip("一起隐藏自己的角色名、称号和职业图标。");
        if (ImGui.TreeNode("替换目标模型"))
        {
            ImGui.TextUnformatted("当前目标　" + (shownTarget ?? "请在游戏中选中角色，可选本人"));
            bossModels ??= BossModelCatalog.Read(data);
            if (bossModels.Count != 0 && ImGui.BeginCombo("怪物 / Boss", bossModels.FirstOrDefault(m => m.ModelId == requestedModel).Name ?? "选择模型"))
            {
                foreach (var boss in bossModels)
                    if (ImGui.Selectable($"{boss.Name}###{boss.ModelId}", boss.ModelId == requestedModel)) requestedModel = boss.ModelId;
                ImGui.EndCombo();
            }
            ImGui.SetNextItemWidth(130);
            ImGui.InputInt("模型编号", ref requestedModel);
            if (ImGui.IsItemHovered()) ImGui.SetTooltip("也可直接输入 ModelChara 表中的行号。");
            ImGui.BeginDisabled(shownTarget == null || requestedModel <= 0 || targetModel.IsRedrawing);
            if (ImGui.Button("替换当前目标"))
            {
                int id = requestedModel;
                FromPanel(() =>
                {
                    RequireShooting(run);
                    targetModel.Apply(id, localMap != null);
                    nextTick = 0;
                });
            }
            ImGui.EndDisabled();
            if (targetModel.IsActive)
            {
                ImGui.SameLine();
                if (ImGui.Button("还原目标模型"))
                    FromPanel(() => { RequireShooting(run); targetModel.Restore(); nextTick = 0; });
                ImGui.TextUnformatted("已替换　" + targetModel.OriginalTargetName);
            }
            ImGui.TreePop();
        }
        if (shownPosition is { } position)
        {
            ImGui.TextUnformatted($"当前位置　{position.X:0.00}，{position.Y:0.00}，{position.Z:0.00}");
            ImGui.InputFloat3("目标 X / Y / Z", ref requestedPosition);
            if (ImGui.Button("调整站位")) FromPanel(() =>
            {
                RequireShooting(run);
                positionOnEntry = false;
                if (localMap is { } capture)
                    travel.MoveWithinLocalMap(capture.Host, capture.Territory, capture.Layout, requestedPosition);
                else travel.MoveWithinStudio(run.Inn, run.Character, requestedPosition);
                status = "已应用站位。";
            });
            if (localMap == null)
            {
                ImGui.SameLine();
                if (ImGui.Button("记住当前站位")) FromPanel(() =>
                {
                    RequireStudio(run);
                    if (objects.LocalPlayer is not { } actor) return;
                    entryPosition = requestedPosition = actor.Position;
                    config.UseSpawn = true;
                    SavePosition();
                    status = "下次进棚将回到这个站位。";
                });
            }
        }
        if (shownLight.HasValue && ImGui.TreeNode("游戏角色照明"))
        {
            ImGui.TextUnformatted($"当前值  {shownLight}/100");
            if (ImGui.Button("设为 60")) FromPanel(() => SetLight(run, 60));
            ImGui.SameLine();
            if (ImGui.Button("设为 100")) FromPanel(() => SetLight(run, 100));
            ImGui.SameLine();
            if (ImGui.Button("还原照明")) FromPanel(() => SetLight(run, null));
            ImGui.TreePop();
        }
        ImGui.TreePop();
    }

    private void Draw()
    {
        if (LocalMapsEnabled) localMapViewport = ImGui.GetMainViewport().ID;
        DrawSwitch();
        if (!open) return;
        ImGui.SetNextWindowSize(new System.Numerics.Vector2(475, 0), ImGuiCond.FirstUseEver);
        if (ImGui.Begin("Green Screen / 绿幕###RoundTableStudio", ref open, ImGuiWindowFlags.AlwaysAutoResize))
        {
            ImGui.PushTextWrapPos(450);
            DrawVfxEditorShortcut();
            DrawRecorderShortcut();
            if (status.Length != 0)
            {
                ImGui.TextWrapped(status);
                ImGui.Separator();
            }
            if (localMap is { } capture)
            {
                DrawLocalMap(capture);
            }
            else if (config.Pending is not { } run)
            {
                if (import.HasValue)
                {
                    if (ImGui.Button("取消进棚")) FromPanel(() => { import = null; status = "已取消本次进棚。"; });
                }
                else
                {
                    ImGui.BeginDisabled(RecorderUsingScene);
                    if (ImGui.Button("进入摄影棚")) FromPanel(Begin);
                    ImGui.EndDisabled();
                    DrawEntrySettings();
                }
            }
            else
            {
                var ready = showColors && !run.Closing && run.SeenStudio && returnAfterModelRestore == null;
                var recorderUsingScene = RecorderUsingScene;
                ImGui.BeginDisabled(!ready || recorderUsingScene || export is { SceneReleased: false });
                foreach (var (label, id) in Stage.Colors)
                {
                    if (!run.SupportsColor(id)) continue;
                    if (id != Stage.Colors[0].Id) ImGui.SameLine();
                    if (ImGui.RadioButton(label, selectedColor == id)) FromPanel(() => Color(run, id));
                }
                ImGui.EndDisabled();
                if (ready)
                {
                    ImGui.BeginDisabled(recorderUsingScene);
                    DrawExport(run);
                    ImGui.EndDisabled();
                    ImGui.BeginDisabled(recorderUsingScene || export is { SceneReleased: false });
                    DrawShot(run);
                    ImGui.EndDisabled();
                }
                if (journey is Journey.EntryHelp or Journey.ReturnHelp or Journey.RestoreHelp)
                {
                    if (journey == Journey.EntryHelp && !run.Closing)
                    {
                        ImGui.BeginDisabled(recorderUsingScene);
                        if (ImGui.Button("继续进棚")) FromPanel(() => RetryEntry(run));
                        ImGui.EndDisabled();
                        ImGui.SameLine();
                    }
                    if (ImGui.Button("打开 Hyperborea")) FromPanel(Hyper);
                    ImGui.SameLine();
                }
                ImGui.BeginDisabled(recorderUsingScene || journey is Journey.Entering or Journey.Returning || switchRequested == false);
                if (ImGui.Button(journey == Journey.RestoreHelp ? "重试还原" : run.Closing ? "重试退棚" : run.SeenStudio ? "退棚" : "取消进棚")) FromPanel(() => End(run));
                ImGui.EndDisabled();
            }
            ImGui.PopTextWrapPos();
        }
        ImGui.End();
    }

    public void Dispose()
    {
        if (disposed) return;
        disposed = true;
        if (localMapFrame is { } pendingFrame) CancelLocalMapFrame(pendingFrame);
        studioTransition.UnregisterFunc();
        framework.Update -= Tick;
        pi.UiBuilder.Draw -= Draw;
        pi.UiBuilder.OpenMainUi -= Show;
        commands.RemoveHandler("/rtstudio");
        if (config.Pending is { } closing)
        {
            closing.Closing = true;
            try { Save(); }
            catch (Exception e) { log.Warning(e, "Could not save the closing studio session"); }
        }
        if (framework.IsFrameworkUnloading)
        {
            if (export is { } stopping)
            {
                stopping.Stop.Cancel();
                FinishExport(stopping);
            }
            nameplate.Restore(false);
            targetModel.Detach();
            freecam.Forget();
            return;
        }
        try
        {
            if (framework.IsInFrameworkUpdateThread) CleanupOnUnload();
            else framework.RunOnFrameworkThread(CleanupOnUnload).GetAwaiter().GetResult();
        }
        catch (Exception e) { log.Warning(e, "Studio cleanup pending on next load"); }
        finally { nameplate.Restore(false); targetModel.Detach(); }
    }

    private void CleanupOnUnload()
    {
        if (framework.IsFrameworkUnloading || !framework.IsInFrameworkUpdateThread) return;
        StopExport("已结束本次透明导出。");
        if (localMap is { } capture) CloseLocalMap(capture, SameLocalMap(capture));
        targetModel.Dispose();
        RestoreShot();
        if (config.Pending is not { } run) return;
        RestoreLight(run);
        try
        {
            if (run.AwaitingArrival(Layout, Environment.TickCount64) || journey == Journey.Returning && Layout == 0)
            {
                shelf.Release(run.Collection, run.Mod);
                return;
            }
            RestoreInStudio(run);
            if (OwnsStudioLayout(run) && !InDuty)
            {
                if (client.IsGPosing || ChangingArea)
                    throw new InvalidOperationException("等待返回原旅馆。");
                if (journey != Journey.Returning && (!LocalMapsEnabled || travel.MatchesStudio(run.Inn, run.HyperOrigin)))
                    travel.Return(run.Inn, LocalMapsEnabled ? run.HyperOrigin : null);
                // There will be no further framework ticks after unload. The saved
                // journal lets the next load verify the actual return destination.
                shelf.Release(run.Collection, run.Mod);
                return;
            }
        }
        catch
        {
            shelf.Release(run.Collection, run.Mod);
            throw;
        }
        Release(run);
    }
}
