using Dalamud.Game.Gui.NamePlate;
using Dalamud.Plugin.Services;

namespace RoundTableStudio;

internal sealed class NameplateVisibility(INamePlateGui plates, IObjectTable objects, IPlayerState player) : IDisposable
{
    // NamePlateObjectIntArrayData.VisibilityFlags, FFXIVClientStructs.
    private const int DrawNameText = 1;
    private ulong owner;
    private int maskedEntry = -1;
    private ulong maskedObject;
    private int originalNameFlag;
    private int originalIcon;

    internal bool Hidden => owner != 0;

    internal void Hide(ulong contentId)
    {
        if (contentId == 0 || !player.IsLoaded || player.ContentId != contentId
            || objects.LocalPlayer is not { GameObjectId: not (0 or 0xE0000000) })
            throw new InvalidOperationException("请等本机角色载入后再隐藏头顶名牌。");
        if (owner == contentId)
            return;

        if (!Hidden)
        {
            plates.OnDataUpdate += OnDataUpdate;
            plates.OnPostDataUpdate += OnPostDataUpdate;
        }
        owner = contentId;
        plates.RequestRedraw();
    }

    // Only the current draw pass is filtered. Redrawing after detaching uses the
    // game's current settings, including changes made while the plate was hidden.
    internal void Restore(bool redraw = true)
    {
        if (!Hidden)
            return;
        owner = 0;
        plates.OnDataUpdate -= OnDataUpdate;
        plates.OnPostDataUpdate -= OnPostDataUpdate;
        maskedEntry = -1;
        if (redraw && player.IsLoaded)
            plates.RequestRedraw();
    }

    private void OnDataUpdate(INamePlateUpdateContext _, IReadOnlyList<INamePlateUpdateHandler> entries)
    {
        maskedEntry = -1;
        if (owner == 0 || !player.IsLoaded || player.ContentId != owner
            || objects.LocalPlayer is not { GameObjectId: not (0 or 0xE0000000) } local)
            return;
        var id = local.GameObjectId;
        for (var i = 0; i < entries.Count; i++)
        {
            var plate = entries[i];
            if (plate.GameObjectId != id)
                continue;
            // The baked line includes the name, title, FC tag and inline status icons.
            // NameIconId is the separate small icon to the left of the name.
            maskedEntry = i;
            maskedObject = id;
            originalNameFlag = plate.VisibilityFlags & DrawNameText;
            originalIcon = plate.NameIconId;
            plate.VisibilityFlags &= ~DrawNameText;
            plate.NameIconId = -1;
            break;
        }
    }

    // The game has consumed this draw pass. Return the two input values now, since
    // the next pass can reuse the same array, including for a different character.
    private void OnPostDataUpdate(INamePlateUpdateContext _, IReadOnlyList<INamePlateUpdateHandler> entries)
    {
        var index = maskedEntry;
        maskedEntry = -1;
        if ((uint)index >= (uint)entries.Count)
            return;
        var plate = entries[index];
        if (plate.GameObjectId != maskedObject)
            return;
        if (originalNameFlag != 0)
            plate.VisibilityFlags |= DrawNameText;
        if (plate.NameIconId == -1)
            plate.NameIconId = originalIcon;
    }

    public void Dispose() => Restore();
}
