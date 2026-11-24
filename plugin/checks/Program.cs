using System.Text.Json;
using RoundTableStudio;

var run = new Capture { Inn = 177, Character = 123, Collection = Guid.NewGuid(), Mod = "test" };
byte target = 44;
int writes = 0;
string journal = "";
void Save() => journal = JsonSerializer.Serialize(run);
bool Replace(byte from, byte to)
{
    if (target != from) return false;
    target = to;
    writes++;
    return true;
}
void Check(bool value, string scenario)
{
    if (!value) throw new Exception(scenario);
    Console.WriteLine("PASS " + scenario);
}

foreach (bool gradient in new[] { false, true })
{
    var newest = gradient ? StagePackage.Gradient : StagePackage.Stable;
    var original = gradient ? StagePackage.LegacyGradient : StagePackage.LegacyStable;
    foreach (var package in new[] { newest, original })
        Check(package.MatchesHash(package.Hash) && package.MatchesHash(package.AlternateHash)
            && !package.MatchesHash(new string('0', 64)), "only the two known archive variants are accepted for " + package.FileName);
    Check(StagePackage.Select(gradient, [original.Name], f => f == newest.FileName) == newest,
        "a bundled current package takes precedence over an installed legacy package");
    Check(StagePackage.Select(gradient, [newest.Name], _ => false) == newest,
        "an installed current package works without a copy beside the DLL");
    Check(StagePackage.Select(gradient, [original.Name], _ => false) == original &&
        StagePackage.Select(gradient, [], f => f == original.FileName) == original,
        "both installed and bundled legacy packages are supported");
}
var legacyRun = JsonSerializer.Deserialize<Capture>("{}")!;
Check(!legacyRun.SupportsColor(44) && legacyRun.SupportsColor(46), "old recording journals retain five-color mode");
try { legacyRun.SetColor(47, 44, () => {}, (_, _) => true); throw new Exception("legacy pack accepted grey"); }
catch (ArgumentOutOfRangeException) { }
var grayRun = new Capture { SupportsMidGray = true };
grayRun.SetColor(46, 44, () => {}, (old, next) => old == 46 && next == 44);
var resumedGray = JsonSerializer.Deserialize<Capture>(JsonSerializer.Serialize(grayRun))!;
Check(resumedGray.SupportsColor(44) && resumedGray.Last == 44, "mid-gray selection is persisted with package capability");
bool restoredWhite = false;
resumedGray.Restore(44, (old, next) => restoredWhite = old == 44 && next == 46);
Check(restoredWhite && resumedGray.Last == null, "leave mid-gray and restore the original weather after recovery");

Check(!run.CanShoot(448, 123) && !run.ShouldRelease(0, 123) && !run.ShouldRelease(177, 123),
    "waiting for local studio; loading screen is not a return");
run.EntryRequestedAt = 1000;
var inTransit = JsonSerializer.Deserialize<Capture>(JsonSerializer.Serialize(run))!;
Check(inTransit.AwaitingArrival(0, 1050) && inTransit.AwaitingArrival(177, 1050)
    && !inTransit.AwaitingArrival(448, 1050) && !inTransit.AwaitingArrival(178, 1050)
    && !inTransit.AwaitingArrival(177, 31000), "reload preserves an in-flight entry only until arrival or timeout");
run.SeenStudio = true;
Check(!run.AwaitingArrival(177, 1060), "a verified arrival makes the next inn reading a return");
Check(run.CanShoot(448, 123) && !run.CanShoot(437, 123) && !run.CanShoot(448, 456), "character and studio gating");
foreach (byte color in new byte[] { 47, 45, 48, 56, 46, 47 })
    run.SetColor(target, color, Save, Replace);
Check(target == 47 && writes == 6 && run.Original == 44, "all five colors retain the original weather");
run.SetColor(target, 47, Save, Replace);
Check(writes == 6, "repeated selection is idempotent");
int refreshes = 0;
run.SetColor(target, 47, Save, (from, to) => { refreshes++; return from == to; }, refresh: true);
Check(refreshes == 1 && run.Original == 44, "reselecting a lagging renderer preserves the original weather");
try { run.SetColor(target, 45, Save, (_, _) => false); } catch (InvalidOperationException) { }
Check(run.Last == 47 && run.Original == 44 && JsonSerializer.Deserialize<Capture>(journal)!.Last == 47,
    "failed change restores the previous ownership journal");
try { run.SetColor(target, 45, () => throw new IOException(), Replace); } catch (IOException) { }
Check(target == 47 && run.Last == 47 && writes == 6, "journal failure leaves live weather alone");
var recovered = JsonSerializer.Deserialize<Capture>(journal)!;
recovered.Restore(target, Replace);
Check(target == 44 && recovered.Last == null && recovered.Collection == run.Collection, "recover prior weather and collection after reload");
run.SetColor(target, 45, Save, Replace);
target = 56;
run.Restore(target, Replace);
Check(target == 56 && run.Last == null, "keep another tool's newer selection");
run.SetColor(target, 46, Save, Replace);
target = 48;
run.SetColor(target, 47, Save, Replace);
run.Restore(target, Replace);
Check(target == 48, "reacquiring control remembers the external value");
run.Closing = true;
Check(!run.CanShoot(448, 123) && !run.ShouldRelease(448, 123) && !run.ShouldRelease(0, 123) && run.ShouldRelease(177, 123),
    "revert waits for a loaded destination before releasing");
Check(run.ShouldRelease(448, 456) && run.ShouldRelease(437, 123), "release on different character or unexpected scene");
try { run.SetColor(target, 44, Save, Replace); throw new Exception("unsupported weather accepted"); }
catch (ArgumentOutOfRangeException) { Check(target == 48, "only the five studio weathers are writable"); }

uint light = 12;
run.SetLight(light, 60, Save, value => light = value);
run.SetLight(light, 100, Save, value => light = value);
var restored = JsonSerializer.Deserialize<Capture>(journal)!;
restored.RestoreLight(light, value => light = value);
Check(light == 12 && restored.AppliedLight == null, "light presets restore the pre-studio setting across reload");
run.RestoreLight(77, _ => throw new Exception("external light was overwritten"));
Check(run.PriorLight == null, "keep a manual lighting change");
try { run.SetLight(light, 60, () => throw new IOException(), _ => throw new Exception("light changed")); }
catch (IOException) { Check(run.AppliedLight == null, "cannot change lighting without a saved original"); }
run.SetLight(12, 60, Save, _ => { });
run.SetLight(35, 100, Save, _ => { });
run.RestoreLight(100, value => light = value);
Check(light == 35, "reacquiring the light remembers the latest manual value");

uint[] knownIds = [5912930, 5909895, 5912939, 5939535];
void NoLampWork(Capture capture) => capture.RestoreLegacyLamps(knownIds,
    _ => throw new Exception("unexpected lamp read"), (_, _) => throw new Exception("unexpected lamp write"),
    () => throw new Exception("unexpected journal save"));
NoLampWork(new Capture());
Check(true, "a new capture leaves all scene lamps to the resource pack");

// Old saves stored original renderer RGBs and an optional neutral-light toggle.
var recoveredLights = JsonSerializer.Deserialize<Capture>("""
    {"NeutralLamps":true,"OriginalLamps":{
        "5912930":{"R":0.2,"G":0.3,"B":1.0},
        "5909895":{"R":0.4,"G":1.0,"B":0.6},
        "5912939":{"R":0.2,"G":0.3,"B":1.0},
        "5939535":{"R":1.0,"G":0.0,"B":0.0},
        "1234":{"R":1.0,"G":0.0,"B":0.0}
    }}
    """)!;
var externalColor = new LampColor(0.1f, 0.2f, 0.3f);
var legacyGray = new LampColor(0.2126f, 0.2126f, 0.2126f);
var lamps = new Dictionary<uint, LampColor> {
    [5912930] = default, [5909895] = externalColor, [5939535] = legacyGray, [1234] = legacyGray,
};
LampColor? ReadLegacy(uint id) => lamps.TryGetValue(id, out var color) ? color : null;
int lampWrites = 0;
void RestoreLegacy(uint id, LampColor color) { lamps[id] = color; lampWrites++; }
try
{
    recoveredLights.RestoreLegacyLamps(knownIds, ReadLegacy, RestoreLegacy, () => throw new IOException());
    throw new Exception("journal save failure was ignored");
}
catch (IOException) { Check(recoveredLights.OriginalLamps.Count == 5, "failed cleanup save remains recoverable"); }
Check(lamps[5912930] == new LampColor(0.2f, 0.3f, 1) && lamps[5939535] == new LampColor(1, 0, 0),
    "restore both a blacked-out uplight and a desaturated overhead light from a v0.3 journal");
Check(lampWrites == 2 && lamps[5909895] == externalColor && lamps[1234] == legacyGray && !lamps.ContainsKey(5912939),
    "leave edited, unknown and unloaded lamps alone");
recoveredLights.RestoreLegacyLamps(knownIds, ReadLegacy, RestoreLegacy, () => journal = JsonSerializer.Serialize(recoveredLights));
NoLampWork(JsonSerializer.Deserialize<Capture>(journal)!);
Check(lampWrites == 2, "cleanup retries and subsequent reloads never fight renderer updates");
