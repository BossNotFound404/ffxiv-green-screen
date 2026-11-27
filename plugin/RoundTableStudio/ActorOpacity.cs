using Dalamud.Plugin.Services;
using Character = FFXIVClientStructs.FFXIV.Client.Game.Character.Character;

namespace RoundTableStudio;

internal sealed unsafe class ActorOpacity(IObjectTable objects)
{
    internal const float RecordingAlpha = 0.01f;
    private ulong ownerContent;
    private uint ownerEntity;
    private nint ownerAddress; // Compared only against the current local player, never dereferenced.
    private float previousAlpha;
    private float appliedAlpha;
    private bool restorePending;

    internal bool Active => ownerContent != 0;

    internal float? Read(ulong currentContentId)
    {
        var actor = LocalActor(currentContentId);
        return actor != null && ValidAlpha(actor->Alpha) ? actor->Alpha : null;
    }

    internal void Apply(ulong currentContentId, float value = RecordingAlpha)
    {
        if (!float.IsFinite(value) || value is < RecordingAlpha or > 1)
            throw new ArgumentOutOfRangeException(nameof(value), "透明度须介于 0.01 和 1.00 之间。");
        Set(currentContentId, value);
    }

    // A separate short-lived instance owns this temporary change during a ground VFX exposure.
    internal void HideForExport(ulong currentContentId) => Set(currentContentId, 0);

    private void Set(ulong currentContentId, float value)
    {
        var actor = LocalActor(currentContentId);
        if (actor == null || !ValidAlpha(actor->Alpha))
            throw new InvalidOperationException("本机角色的透明度尚未就绪。");

        if (!SameActor(actor) || actor->Alpha != appliedAlpha)
            previousAlpha = actor->Alpha;
        ownerContent = currentContentId;
        ownerEntity = actor->EntityId;
        ownerAddress = (nint)actor;
        appliedAlpha = value;
        restorePending = false;
        // Anamnesis's actor Transparency is Character.Alpha. This field belongs to the
        // character and survives a draw-object rebuild. No VFX or visibility flag is changed.
        actor->Alpha = value;
    }

    internal void Observe(ulong currentContentId)
    {
        if (!Active)
            return;
        if (currentContentId != ownerContent)
        {
            Clear();
            return;
        }
        var actor = LocalActor(currentContentId);
        if (actor == null)
            return;
        if (!SameActor(actor) || actor->Alpha != appliedAlpha)
        {
            Clear();
            return;
        }
        if (restorePending)
        {
            if (previousAlpha != appliedAlpha)
                actor->Alpha = previousAlpha;
            Clear();
        }
    }

    internal void Restore(ulong currentContentId)
    {
        if (!Active)
            return;
        restorePending = true;
        Observe(currentContentId);
    }

    private Character* LocalActor(ulong currentContentId)
    {
        if (currentContentId == 0 || objects.LocalPlayer is not { Address: not 0 } player)
            return null;
        var actor = (Character*)player.Address;
        return actor->ContentId == currentContentId ? actor : null;
    }

    private bool SameActor(Character* actor) =>
        ownerContent == actor->ContentId && ownerEntity == actor->EntityId && ownerAddress == (nint)actor;

    private static bool ValidAlpha(float value) => float.IsFinite(value) && value is >= 0 and <= 1;

    internal void Forget() => Clear();

    private void Clear()
    {
        ownerContent = 0;
        ownerEntity = 0;
        ownerAddress = 0;
        restorePending = false;
    }
}
