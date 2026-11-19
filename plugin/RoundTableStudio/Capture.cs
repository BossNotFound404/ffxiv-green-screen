namespace RoundTableStudio;

public readonly record struct LampColor(float R, float G, float B)
{
    // Reconstruct only the color written by the v0.3 runtime override.
    public LampColor LegacyNeutral(uint id)
    {
        if (id is 5912930 or 5909895) return default;
        if (R == G && G == B) return this;
        float gray = 0.2126f * R + 0.7152f * G + 0.0722f * B;
        return new(gray, gray, gray);
    }
}

public sealed class Capture
{
    public Guid Collection { get; set; }
    public string Mod { get; set; } = "";
    public uint Inn { get; set; }
    public ulong Character { get; set; }
    public long ProcessStart { get; set; }
    public bool SeenStudio { get; set; }
    public long EntryRequestedAt { get; set; }
    public int? HyperOwner { get; set; }
    public int? HyperOrigin { get; set; }
    public bool ShelfReleased { get; set; }
    public bool Closing { get; set; }
    public byte? Original { get; set; }
    public byte? Last { get; set; }
    public float? PriorFade { get; set; }
    public bool MutedAmbient { get; set; }
    public uint? PriorLight { get; set; }
    public uint? AppliedLight { get; set; }
    // Populated only when recovering a v0.3 recording in the same game process.
    public Dictionary<uint, LampColor> OriginalLamps { get; set; } = new();

    public void RestoreLegacyLamps(IEnumerable<uint> knownIds, Func<uint, LampColor?> read,
        Action<uint, LampColor> write, Action save)
    {
        if (OriginalLamps.Count == 0) return;
        var originals = OriginalLamps;
        foreach (var id in knownIds)
        {
            if (originals.TryGetValue(id, out var original) && read(id) == original.LegacyNeutral(id))
                write(id, original);
        }
        OriginalLamps = new();
        try { save(); }
        catch { OriginalLamps = originals; throw; }
    }

    public bool CanShoot(uint layout, ulong character) =>
        !Closing && SeenStudio && layout == 448 && Character != 0 && Character == character;

    public bool AwaitingArrival(uint layout, long now) => !SeenStudio && EntryRequestedAt > 0
        && (layout == 0 || layout == Inn) && now - EntryRequestedAt is >= 0 and < 30000;

    public bool ShouldRelease(uint layout, ulong character)
    {
        if (character != 0 && character != Character) return true;
        if (layout == 0 || layout == 448 || character == 0) return false;
        return Closing || SeenStudio || layout != Inn;
    }

    public void SetColor(byte target, byte color, Action persist, Func<byte, byte, bool> replace, bool refresh = false)
    {
        if (color is not (45 or 46 or 47 or 48 or 56)) throw new ArgumentOutOfRangeException(nameof(color));
        if (target == color && !refresh) return;
        var previous = (Original, Last);
        if (Last != target) Original = target;
        Last = color;
        try { persist(); }
        catch { (Original, Last) = previous; throw; }
        if (!replace(target, color))
        {
            (Original, Last) = previous;
            persist();
            throw new InvalidOperationException("场景或天气正在变化，请稍后再点一次色卡。");
        }
    }

    public void Restore(byte? target, Func<byte, byte, bool> replace)
    {
        if (Original.HasValue && Last.HasValue && target == Last && !replace(Last.Value, Original.Value))
            throw new InvalidOperationException("天气正在变化，等待恢复本次切色。");
        Original = Last = null;
    }

    public void SetLight(uint current, uint value, Action persist, Action<uint> apply)
    {
        if (value > 100) throw new ArgumentOutOfRangeException(nameof(value));
        if (value == current) return;
        var previous = (PriorLight, AppliedLight);
        if (AppliedLight != current) PriorLight = current;
        AppliedLight = value;
        try { persist(); }
        catch { (PriorLight, AppliedLight) = previous; throw; }
        apply(value);
    }

    public void RestoreLight(uint current, Action<uint> apply)
    {
        if (PriorLight.HasValue && AppliedLight == current) apply(PriorLight.Value);
        PriorLight = AppliedLight = null;
    }
}
