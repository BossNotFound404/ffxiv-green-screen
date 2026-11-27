using Dalamud.Game.ClientState.Objects.Enums;
using Dalamud.Game.ClientState.Objects.Types;
using Dalamud.Plugin;
using Dalamud.Plugin.Ipc;
using Dalamud.Plugin.Services;
using Lumina.Excel.Sheets;
using GameCharacter = FFXIVClientStructs.FFXIV.Client.Game.Character.Character;

namespace RoundTableStudio;

// Framework-thread operations. Resolve the original table slot and identity again before
// restoring; the selected target can change while the studio is open.
internal sealed unsafe class StudioTargetModel : IDisposable
{
    private readonly ITargetManager targets;
    private readonly IObjectTable objects;
    private readonly IDataManager data;
    private readonly ICallGateSubscriber<int, int, object> redraw;
    private readonly ICallGateSubscriber<nint, int, object> redrawn;
    private Change? active;
    private Pending? pending;
    private bool detached;
    public bool IsActive => active != null;
    public bool IsRedrawing => pending != null;
    public bool HasReleasedPlayerModel => active is { Released: true } && pending is not { Restoring: true };
    public string? OriginalTargetName => active?.Name;
    public string? CurrentTargetName => targets.Target is { } actor && actor.IsValid() ? actor.Name.TextValue : null;

    public StudioTargetModel(IDalamudPluginInterface pi, ITargetManager targets, IObjectTable objects, IDataManager data)
    {
        this.targets = targets;
        this.objects = objects;
        this.data = data;
        redraw = pi.GetIpcSubscriber<int, int, object>("Penumbra.RedrawObject.V5");
        redrawn = pi.GetIpcSubscriber<nint, int, object>("Penumbra.GameObjectRedrawn");
        redrawn.Subscribe(OnRedrawn);
    }

    public void Apply(int modelId, bool localMap = false)
    {
        var layout = Stage.LoadedLayout();
        if (layout.Territory == 0 || !localMap && layout.Territory != Stage.Territory)
            throw new InvalidOperationException("请在本地拍摄场景中更换目标模型。");
        if (detached || pending != null) throw new InvalidOperationException("请等目标模型重绘完成。");
        if (targets.Target is not ICharacter selected || !selected.IsValid() ||
            selected.ObjectKind is not (ObjectKind.Pc or ObjectKind.BattleNpc or ObjectKind.EventNpc))
            throw new InvalidOperationException("请选中要替换模型的角色或 NPC，也可以选中自己。");
        var localPlayer = objects.LocalPlayer;
        if (selected.EntityId is 0 or 0xE0000000 || selected.GameObjectId == 0 || localPlayer == null)
            throw new InvalidOperationException("这个目标缺少可用于还原的角色编号，请选中自己或有独立编号的 NPC。");

        ValidateModel(modelId);
        var key = Identity.Of(selected);
        if (active != null && active.Key != key)
            throw new InvalidOperationException($"请先还原「{active.Name}」，再为其他目标更换模型。");
        var actor = Resolve(key);
        if (actor == null || actor->GameObject.DrawObject == null)
            throw new InvalidOperationException("目标模型正在载入，请等它显示后再应用。");
        if (actor->ModelContainer.ModelCharaId_2 != -1)
            throw new InvalidOperationException("目标已有变身模型覆盖，请先解除变身。");
        if (active != null && actor->ModelContainer.ModelCharaId != active.Applied)
        {
            active = null;
            throw new InvalidOperationException("原目标的模型已被其他设置更改。再次应用即可按当前外观重新记录。");
        }
        var before = actor->ModelContainer.ModelCharaId;
        if (before == modelId) return;
        // Capture once for repeated changes on the same target. Zero is a valid original.
        var prior = active;
        active = new(key, selected.Name.TextValue, prior?.Original ?? before, modelId, localPlayer.GameObjectId,
            localMap ? layout : null);
        try
        {
            Submit(actor, modelId, modelId == active.Original);
        }
        catch
        {
            if (Resolve(key) == actor && actor->ModelContainer.ModelCharaId == modelId)
                actor->ModelContainer.ModelCharaId = before;
            active = prior;
            throw;
        }
    }

    public void Restore()
    {
        var change = active;
        if (change == null || pending is { Restoring: true }) return;
        var actor = OriginalActor(change);
        if (actor == null || actor->ModelContainer.ModelCharaId != change.Applied)
        {
            Clear();
            return;
        }
        Submit(actor, change.Original, true);
    }

    public string? Tick()
    {
        if (active is not { } change) return null;
        var actor = OriginalActor(change);
        if (actor == null)
        {
            Clear();
            return "原目标已离开，已结束模型替换。";
        }
        var draw = pending;
        if (actor->ModelContainer.ModelCharaId != (draw?.ModelId ?? change.Applied))
        {
            Clear();
            return "目标模型已由其他设置更改。";
        }
        if (draw == null) return null;
        if (draw.Notified && actor->GameObject.IsReadyToDraw() &&
            actor->GameObject.DrawObject != null && actor->GameObject.DrawObject->IsVisible)
        {
            pending = null;
            if (draw.Restoring) active = null;
            return draw.Restoring ? $"「{change.Name}」的原模型已还原。" : $"「{change.Name}」已重绘为模型 {draw.ModelId}。";
        }
        if (Environment.TickCount64 < draw.Deadline) return null;
        pending = null;
        if (draw.Restoring)
        {
            active = null;
            return "已还原目标的模型编号，尚未确认重绘完成。";
        }
        Restore();
        return "未收到目标模型的重绘确认，正在还原原模型。";
    }

    private void Submit(GameCharacter* actor, int modelId, bool restoring)
    {
        if (!redraw.HasAction) throw new InvalidOperationException("Penumbra 的模型重绘接口尚未就绪。");
        var before = actor->ModelContainer.ModelCharaId;
        var previous = pending;
        var key = active!.Key;
        pending = new(key, modelId, restoring);
        actor->ModelContainer.ModelCharaId = modelId;
        try { redraw.InvokeAction(key.Index, 0); }
        catch
        {
            if (OriginalActor(active) == actor && actor->ModelContainer.ModelCharaId == modelId)
                actor->ModelContainer.ModelCharaId = before;
            pending = previous;
            throw;
        }
    }

    private void OnRedrawn(nint address, int index)
    {
        if (pending is { } draw && draw.Key.Index == index && draw.Key.Address == address) draw.Notified = true;
    }

    private GameCharacter* OriginalActor(Change change)
    {
        if (change.Released)
            return objects.LocalPlayer is ICharacter local && Identity.Of(local) == change.Key ? Resolve(change.Key) : null;
        if (change.Layout.HasValue && Stage.LoadedLayout() != change.Layout) return null;
        return objects.LocalPlayer?.GameObjectId == change.Player ? Resolve(change.Key) : null;
    }

    internal void ForgetChange() => Clear();

    internal void ReleaseLayout()
    {
        if (active is not { } change || change.Key.GameId != change.Player || pending is { Restoring: true })
        {
            Clear();
            return;
        }
        // Only our own character can be kept across a layout reload. The actor's
        // full identity is checked again before the saved model can be restored.
        active = change with { Released = true };
        pending = null;
    }

    internal void RestoreReleasedPlayerModel()
    {
        if (!HasReleasedPlayerModel || active is not { } change) return;
        try { Restore(); }
        catch
        {
            var actor = OriginalActor(change);
            if (actor != null && actor->ModelContainer.ModelCharaId == change.Applied)
                actor->ModelContainer.ModelCharaId = change.Original;
            Clear();
            throw;
        }
    }

    private void Clear() { pending = null; active = null; }

    public void Dispose()
    {
        if (detached) return;
        try { Restore(); }
        catch
        {
            if (active is { } change)
            {
                var actor = OriginalActor(change);
                if (actor != null && actor->ModelContainer.ModelCharaId == change.Applied)
                    actor->ModelContainer.ModelCharaId = change.Original;
            }
            throw;
        }
        finally { Detach(); }
    }

    public void Detach()
    {
        if (detached) return;
        detached = true;
        try { redrawn.Unsubscribe(OnRedrawn); }
        finally { Clear(); }
    }

    private void ValidateModel(int modelId)
    {
        if (modelId <= 0 || !data.GetExcelSheet<ModelChara>().TryGetRow((uint)modelId, out var row) ||
            row.Model == 0 || row.Base == 0 || row.Type is not (2 or 3))
            throw new InvalidOperationException("请输入有效的怪物或半人型 Boss 的 ModelChara 行号。恢复原模型请点还原。");
        var monster = row.Type == 3;
        var model = (monster ? "m" : "d") + row.Model.ToString("D4");
        var root = $"chara/{(monster ? "monster" : "demihuman")}/{model}";
        var body = (monster ? "b" : "e") + row.Base.ToString("D4");
        var stem = $"{root}/obj/{(monster ? "body" : "equipment")}/{body}/model/{model}{body}";
        var hasMesh = monster ? data.FileExists(stem + ".mdl") :
            new[] { "_top", "_met", "_glv", "_dwn", "_sho" }.Any(slot => data.FileExists(stem + slot + ".mdl"));
        if (!hasMesh || !data.FileExists($"{root}/skeleton/base/b0001/skl_{model}b0001.sklb"))
            throw new InvalidOperationException("本机游戏数据中缺少这个模型的网格或基础骨骼，请换一个模型编号。");
    }

    private GameCharacter* Resolve(Identity key) =>
        objects[key.Index] is ICharacter actor && actor.IsValid() && Identity.Of(actor) == key
            ? (GameCharacter*)actor.Address : null;

    private sealed class Pending(Identity key, int modelId, bool restoring)
    {
        public readonly Identity Key = key;
        public readonly int ModelId = modelId;
        public readonly bool Restoring = restoring;
        public readonly long Deadline = Environment.TickCount64 + 8000;
        public volatile bool Notified;
    }

    private sealed record Change(Identity Key, string Name, int Original, int Applied, ulong Player, (uint Territory, nint Address)? Layout,
        bool Released = false);
    private readonly record struct Identity(ushort Index, ulong GameId, uint Entity, uint Base, ObjectKind Kind, nint Address)
    {
        public static Identity Of(ICharacter actor) =>
            new(actor.ObjectIndex, actor.GameObjectId, actor.EntityId, actor.BaseId, actor.ObjectKind, actor.Address);
    }
}
