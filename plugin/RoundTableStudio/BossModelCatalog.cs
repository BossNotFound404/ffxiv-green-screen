using Dalamud.Plugin.Services;
using Lumina.Excel.Exceptions;
using Lumina.Excel.Sheets;

namespace RoundTableStudio;

internal static class BossModelCatalog
{
    // BNpcBase and BNpcName row pairs from Anamnesis/Data/NpcNames.json, blob 744ab8f43e0adf4708a69ec630279ea0705a7328.
    // Checked against the CN game sheets. Use the original variants when later encounters reuse the same name.
    private static readonly (uint Base, uint Name)[] Bosses =
    [
        (207, 1185),  // Ifrit
        (239, 1644),  // Garuda
        (246, 1801),  // Titan
        (2663, 2505), // Leviathan
        (3820, 3649), // Bismarck
        (5346, 4776), // Sephirot
        (6026, 5199), // Sophia
        (6449, 5567), // Zurvan
    ];

    internal static IReadOnlyList<(string Name, int ModelId)> Read(IDataManager data)
    {
        try { return ReadRows(data); }
        catch (Exception error) when (error is SheetNotFoundException or MismatchedColumnHashException
            or UnsupportedLanguageException or IOException)
        {
            return [];
        }
    }

    private static IReadOnlyList<(string Name, int ModelId)> ReadRows(IDataManager data)
    {
        var bases = data.GetExcelSheet<BNpcBase>();
        var names = data.GetExcelSheet<BNpcName>();
        var models = data.GetExcelSheet<ModelChara>();
        if (bases == null || names == null || models == null) return [];

        var choices = new List<(string Name, int ModelId)>();
        foreach (var boss in Bosses)
        {
            if (!bases.TryGetRow(boss.Base, out var appearance) || !names.TryGetRow(boss.Name, out var name)
                || !models.TryGetRow(appearance.ModelChara.RowId, out var model) || model.Type != 3 || model.Model == 0)
                continue;

            string label = name.Singular.ExtractText().Trim();
            if (label.Length != 0) choices.Add((label, checked((int)model.RowId)));
        }
        return choices;
    }
}
