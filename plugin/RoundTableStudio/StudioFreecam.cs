using System.Reflection;
using Dalamud.Game.Command;
using Dalamud.Plugin;
using Dalamud.Plugin.Services;

namespace RoundTableStudio;

internal sealed class StudioFreecam(IDalamudPluginInterface pi, ICommandManager commands, Func<bool> canControl)
{
    private static readonly Guid SupportedModule = new("c5e1ec35-d159-42f4-b569-4be32745f145");
    private IReadOnlyCommandInfo? registration;
    private Func<bool>? enabled;
    public bool Owned { get; private set; }

    // Called from the framework thread, including when the UI window is closed.
    // The registered command identifies its actual loaded owner. Discard old
    // ownership if that registration disappears or is replaced by a reload.
    public bool? Read()
    {
        if (!commands.Commands.TryGetValue("/cammy", out var active))
        {
            Forget();
            return null;
        }
        if (!ReferenceEquals(active, registration))
        {
            Forget();
            var assembly = active.Handler.Method.Module.Assembly;
            if (assembly.GetName().Name == "Cammy" && assembly.ManifestModule.ModuleVersionId == SupportedModule
                && pi.GetPlugin(assembly) is { InternalName: "Cammy", IsLoaded: true })
            {
                enabled = assembly.GetType("Cammy.FreeCam")?.GetProperty("Enabled", BindingFlags.Public | BindingFlags.Static)
                    ?.GetMethod?.CreateDelegate<Func<bool>>();
                if (enabled != null) registration = active;
            }
        }
        bool? value;
        try { value = enabled?.Invoke(); }
        catch (TypeInitializationException) { Forget(); return null; }
        if (value != true) Owned = false;
        return value;
    }

    public void Set(bool requested)
    {
        if (!canControl()) throw new InvalidOperationException("请在本次 448 录制棚的普通视角下使用自由镜头。");
        var current = Read() ?? throw new InvalidOperationException("未找到可读取状态的 Cammy 2.1.1.2。");
        if (current == requested) return;
        var before = registration;
        if (!commands.ProcessCommand("/cammy freecam") || Read() != requested || !ReferenceEquals(registration, before))
            throw new InvalidOperationException("Cammy 没有确认切换，自由镜头状态请以面板读数为准。");
        Owned = requested;
    }

    public void Observe()
    {
        if (!Owned) return;
        if (canControl()) Read();
        else Forget();
    }

    public void Restore()
    {
        if (Owned && canControl())
        {
            var current = Read();
            // A newly registered Cammy may already have its own camera.
            if (Owned && current == true) Set(false);
        }
        Forget();
    }

    public void Forget()
    {
        Owned = false;
        registration = null;
        enabled = null;
    }
}
