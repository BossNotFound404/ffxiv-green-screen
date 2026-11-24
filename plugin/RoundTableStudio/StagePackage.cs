namespace RoundTableStudio;

internal sealed record StagePackage(string Name, string FileName, string Hash, string AlternateHash, bool SupportsMidGray)
{
    internal static readonly StagePackage Stable = new("Round Table Studio - Stable Stage v0.18",
        "RoundTable_StableStage_v0.18.pmp", "8130631105fd92b96474d44e4f44bea0e90525bb501a50498b960079a0133340",
        "f2aae46b95b445cf6c1fc7688dd6b6248dd18787d16b6d2a41a2204d17ef3fb4", true);
    internal static readonly StagePackage Gradient = new("Round Table Studio - Capture Stage v0.19",
        "RoundTable_CaptureStage_v0.19.pmp", "13248e955989957c3b6282fc561951eb444f038be59b3ae23b17a96e97ac3b0b",
        "24b47b21dbaac6dcffed0fce5648c103acbb50451fb7e87ce2f38ffddfd7054f", true);
    internal static readonly StagePackage LegacyStable = new("Round Table Studio - Stable Stage v0.16",
        "RoundTable_StableStage_v0.16.pmp", "6556cbdc8cff5793aef80e900ce53a273b77ec5252fafd746d71066120388bfe",
        "3933e64834b5610f60dcf16faa65033324c99b9ac92c5c68efeefe7843b40c96", false);
    internal static readonly StagePackage LegacyGradient = new("Round Table Studio - Capture Stage v0.17",
        "RoundTable_CaptureStage_v0.17.pmp", "5824fe4a956ae8de59ddc3df65950686c4573e6ab3716c1d7c6f9351d452903b",
        "d5ccb529137d8e2490fceec01b0aec98bccc202ca08daa4f4589544699d701c2", false);

    // Both approved archives contain identical files compressed by different zlib builds.
    internal bool MatchesHash(string hash) => hash == Hash || hash == AlternateHash;

    internal static StagePackage Select(bool gradient, IEnumerable<string> installedNames, Func<string, bool> bundled)
    {
        var current = gradient ? Gradient : Stable;
        var legacy = gradient ? LegacyGradient : LegacyStable;
        if (installedNames.Contains(current.Name) || bundled(current.FileName)) return current;
        if (installedNames.Contains(legacy.Name) || bundled(legacy.FileName)) return legacy;
        return current;
    }
}
