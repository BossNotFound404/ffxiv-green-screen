using FFXIVClientStructs.FFXIV.Client.Game;
using FFXIVClientStructs.FFXIV.Client.Graphics.Environment;
using FFXIVClientStructs.FFXIV.Client.LayoutEngine;
using FFXIVClientStructs.FFXIV.Client.LayoutEngine.Layer;
using RenderLight = FFXIVClientStructs.FFXIV.Client.Graphics.Render.Light;

namespace RoundTableStudio;

internal static unsafe class Stage
{
    public const uint Territory = 448;
    public static readonly (string Name, byte Id)[] Colors =
        [("黑", 47), ("红", 45), ("绿", 48), ("蓝", 56), ("白", 46)];
    private static readonly (byte Weather, uint Instance)[] Backdrops =
        [(47, 5837159), (45, 5837160), (48, 5837161), (56, 6051359)];
    public static readonly uint[] LegacyLamps =
        [5912930, 5909895, 5759859, 5912939, 5912803, 5939535, 5939539, 6068901];

    public static uint LoadedTerritory()
    {
        var world = LayoutWorld.Instance();
        var layout = world == null ? null : world->ActiveLayout;
        return layout != null && layout->InitState == 7 ? layout->TerritoryTypeId : 0;
    }

    // Anamnesis uses the third local ServerWeather record in normal camera mode.
    // Read the renderer separately: a changed target does not prove a changed scene.
    public static byte? Target()
    {
        var weather = WeatherManager.Instance();
        return weather == null ? null : weather->Weathers[2].NextWeatherId;
    }

    public static byte? Displayed()
    {
        var env = EnvManager.Instance();
        return env == null ? null : env->ActiveWeather;
    }

    public static bool TryReplace(byte expected, byte next)
    {
        if (LoadedTerritory() != Territory) return false;
        var weather = WeatherManager.Instance();
        if (weather == null || weather->Weathers[2].NextWeatherId != expected) return false;
        weather->Weathers[2].NextWeatherId = next;
        return weather->Weathers[2].NextWeatherId == next;
    }

    public const float QuickFade = 0.05f;

    public static float? Fade()
    {
        var env = EnvManager.Instance();
        return env != null && env->EnvScene != null && float.IsFinite(env->TransitionTime)
            ? env->TransitionTime : null;
    }

    public static bool TryQuickReplace(byte expected, byte next)
    {
        if (LoadedTerritory() != Territory) return false;
        var weather = WeatherManager.Instance();
        var env = EnvManager.Instance();
        if (weather == null || env == null || env->EnvScene == null) return false;
        ref var slot = ref weather->Weathers[2];
        if (slot.NextWeatherId != expected || slot.VirtualTable == null) return false;
        slot.SetNextWeather(next, QuickFade, disablesOverride: false);
        if (slot.NextWeatherId != next) return false;
        // Match the renderer-facing pair used by Brio when changing the weather.
        env->ActiveWeather = next;
        env->TransitionTime = QuickFade;
        return true;
    }

    public static void RestoreFade(float? prior)
    {
        var env = EnvManager.Instance();
        if (prior.HasValue && env != null && env->TransitionTime == QuickFade)
            env->TransitionTime = prior.Value;
    }

    public static void SwitchBackdrops(byte weather)
    {
        if (LoadedTerritory() != Territory) return;
        var world = LayoutWorld.Instance();
        // Resolve the whole set before changing any placement. During loading,
        // the ordinary weather-driven object groups handle the transition.
        ILayoutInstance** hosts = stackalloc ILayoutInstance*[Backdrops.Length];
        int selected = -1;
        for (int i = 0; i < Backdrops.Length; i++)
        {
            hosts[i] = world->GetLayoutInstance(InstanceType.Vfx, Backdrops[i].Instance);
            if (hosts[i] == null || hosts[i]->Layout != world->ActiveLayout) return;
            if (Backdrops[i].Weather == weather) selected = i;
        }
        // White uses the weather-controlled stage model for both floor and sky.
        // Its former VFX placement belongs to the same object set as the blue uplight.
        if (selected < 0 && weather != 46) return;
        if (selected >= 0 && !hosts[selected]->IsActive) hosts[selected]->SetActive(true);
        for (int i = 0; i < Backdrops.Length; i++)
            if (i != selected && hosts[i]->IsActive) hosts[i]->SetActive(false);
    }

    // v0.3 recorded renderer colors in its session journal. These accessors
    // only restore those records. New recordings get their lights from the pack.
    private static RenderLight* FindLegacyLamp(uint id)
    {
        if (LoadedTerritory() != Territory) return null;
        var world = LayoutWorld.Instance();
        var lamp = (LightLayoutInstance*)world->GetLayoutInstance(InstanceType.Light, id);
        if (lamp == null || lamp->Layout != world->ActiveLayout || lamp->GraphicsObject == null) return null;
        return lamp->GraphicsObject->RenderLight;
    }

    public static LampColor? ReadLegacyLamp(uint id)
    {
        var lamp = FindLegacyLamp(id);
        if (lamp == null) return null;
        var c = lamp->Color;
        return float.IsFinite(c.X) && float.IsFinite(c.Y) && float.IsFinite(c.Z)
            ? new LampColor(c.X, c.Y, c.Z) : null;
    }

    public static void RestoreLegacyLamp(uint id, LampColor color)
    {
        var lamp = FindLegacyLamp(id);
        if (lamp != null) lamp->Color = new(color.R, color.G, color.B);
    }

    // This one placed ambient emitter shares its SCD with other maps. Only
    // touch the instance belonging to the loaded studio, never the shared file.
    private static ILayoutInstance* AmbientEmitter()
    {
        if (LoadedTerritory() != Territory) return null;
        var world = LayoutWorld.Instance();
        var instance = world->GetLayoutInstance(InstanceType.Sound, 5941210);
        return instance != null && instance->Layout == world->ActiveLayout ? instance : null;
    }

    public static bool? AmbientActive()
    {
        var instance = AmbientEmitter();
        return instance == null ? null : instance->IsActive;
    }

    public static void ToggleAmbient(bool enabled)
    {
        var instance = AmbientEmitter();
        if (instance != null && instance->IsActive != enabled) instance->SetActive(enabled);
    }
}
