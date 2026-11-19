using System.Collections;
using System.Reflection;
using System.Runtime.InteropServices;
using Dalamud.Plugin;
using FFXIVClientStructs.FFXIV.Client.Graphics.Scene;

namespace RoundTableStudio;

// Only call on the framework thread. Match Brio's world-object Pause and Resume
// controls on the individual VFXEditor preview; Resume can restart playback.
internal sealed unsafe class PreviewFreeze(IDalamudPluginInterface pi)
{
    private static readonly Guid EditorBuild = new("ab6d4acd-dc0a-4803-a94b-ca0d300cd3d3");
    private static readonly Guid BrioBuild = new("9421a4d9-35bc-4867-b3c9-a22d00d7a86f");
    private const string Changed = "地面预览已结束或被替换，请重新生成特效后导出。";
    private Lease? held;
    public string? EffectPath => held?.EffectPath;

    public bool TryAcquire(out string reason)
    {
        if (held != null)
        {
            reason = "正在使用已定格的特效。";
            return false;
        }
        try
        {
            if (Stage.LoadedTerritory() != Stage.Territory)
                throw new InvalidOperationException("请先进摄影棚再导出。");
            var editor = Find("VFXEditor", EditorBuild, "1.9.4.6");
            var spawn = editor.GetType("VfxEditor.Spawn.VfxSpawn", true)!;
            var previews = (IDictionary)spawn.GetField("Vfxs")!.GetValue(null)!;
            if (previews.Count != 1)
                throw new InvalidOperationException("请在 VFXEditor 的 Spawn 中只保留一个地面预览（On Ground）。");
            var preview = previews.Keys.Cast<object>().Single();
            if (preview.GetType() != editor.GetType("VfxEditor.Structs.Vfx.StaticVfx", true))
                throw new InvalidOperationException("当前只支持地面预览，请在 VFXEditor 的 Spawn 中选择 On Ground。");
            var path = preview.GetType().GetField("Path")?.GetValue(preview) as string;
            if (path == null || !path.StartsWith("vfx/", StringComparison.OrdinalIgnoreCase) ||
                !path.EndsWith(".avfx", StringComparison.OrdinalIgnoreCase) || path.Length > 260 ||
                path.Any(c => !(char.IsAsciiLetterOrDigit(c) || c is '/' or '_' or '-' or '.')) ||
                path.Split('/').Any(part => part is "" or "." or ".."))
                throw new InvalidOperationException("无法识别所选预览的游戏特效路径，请选择 vfx 目录内的 AVFX。");

            var brio = Find("Brio", BrioBuild, "0.8.0.12");
            var serviceType = brio.GetType("Brio.Game.Core.VFXService", true)!;
            var resourceType = brio.GetType("Brio.Game.VFX.Intertop.VfxResourceInstance", true)!;
            var getService = brio.GetType("Brio.Brio", true)!.GetMethod("TryGetService")!.MakeGenericMethod(serviceType);
            object?[] args = [null];
            if (getService.Invoke(null, args) is not true || args[0] == null)
                throw new InvalidOperationException("Brio 的特效播放服务尚未就绪。");

            var vfxField = preview.GetType().GetField("Vfx")!;
            if (vfxField.FieldType != typeof(VfxObject*) || resourceType.GetField("Speed")?.FieldType != typeof(float))
                throw new InvalidOperationException("特效播放接口与当前游戏版本不匹配。");
            var setter = serviceType.GetMethod("SetVFXSpeed", [resourceType.MakePointerType(), typeof(float)])!;
            var pause = serviceType.GetMethod("PauseVFX", [resourceType.MakePointerType()])!;
            var play = serviceType.GetMethod("PlayStaticVFX", [typeof(VfxObject*)])!;
            var lease = new Lease(editor, brio, previews, preview, vfxField, args[0]!, setter, pause, play,
                resourceType.MakePointerType(), Marshal.OffsetOf(resourceType, "Speed").ToInt32());
            lease.EffectPath = path;
            if (!lease.TryLocate(out lease.Scene, out lease.Resource))
                throw new InvalidOperationException(Changed);
            lease.OriginalSpeed = lease.ReadSpeed();
            if (!float.IsFinite(lease.OriginalSpeed))
                throw new InvalidOperationException("无法读取地面预览的播放速度。");

            // Retain the lease before the first mutation, including on partial failure.
            Hold(lease);
            if (Check(out reason)) return true;
        }
        catch (Exception ex)
        {
            reason = ex.GetBaseException().Message;
        }
        try { Release(rollingBack: true); }
        catch (Exception ex) { reason += " " + ex.GetBaseException().Message; }
        return false;
    }

    private void Hold(Lease lease)
    {
        held = lease;
        if (lease.OriginalSpeed == 0) return;
        lease.Pause();
        lease.PausedByStudio = true;
        if (!Current(lease)) throw new InvalidOperationException(Changed);
        var speedAfterPause = lease.ReadSpeed();
        if (speedAfterPause != 0 && speedAfterPause != lease.OriginalSpeed)
            throw new InvalidOperationException("地面预览的速度被其他设置更改。");
        lease.SetSpeed(0);
    }

    public bool Check(out string reason)
    {
        try
        {
            var lease = held;
            if (lease == null || !Current(lease)) throw new InvalidOperationException(Changed);
            if (lease.Previews.Count != 1)
                throw new InvalidOperationException("拍摄期间出现了其他预览，请只保留要导出的特效。");
            if (lease.ReadSpeed() != 0)
                throw new InvalidOperationException("特效播放速度发生了变化，无法继续同帧拍摄。");
            reason = "";
            return true;
        }
        catch (Exception ex) { reason = ex.GetBaseException().Message; return false; }
    }

    public void Release() => Release(rollingBack: false);

    private void Release(bool rollingBack)
    {
        var lease = held;
        if (lease == null) return;
        try
        {
            // Leave a newly spawned preview and any playback change made elsewhere alone.
            if (!lease.PausedByStudio || !OwnsPreview(lease)) return;
            if (pi.GetPlugin(lease.Brio) is not { InternalName: "Brio", IsLoaded: true })
                throw new InvalidOperationException("Brio 已卸载，请在 VFXEditor 重新生成地面预览以恢复播放。");
            var speed = lease.ReadSpeed();
            if (speed != 0 && !(rollingBack && speed == lease.OriginalSpeed)) return;
            lease.Play();
            // Resume may reconstruct a resource synchronously. Never reuse the previous pointer.
            if (!Current(lease))
                throw new InvalidOperationException("重新播放时特效实例已变更，无法确认原速。");
            lease.SetSpeed(lease.OriginalSpeed);
            if (!Current(lease) || lease.ReadSpeed() != lease.OriginalSpeed)
                throw new InvalidOperationException("未能恢复特效原速，请在 VFXEditor 重新生成地面预览。");
        }
        finally { held = null; }
    }

    private bool Current(Lease lease) => pi.GetPlugin(lease.Brio) is { InternalName: "Brio", IsLoaded: true } &&
        OwnsPreview(lease);

    private bool OwnsPreview(Lease lease) =>
        Stage.LoadedTerritory() == Stage.Territory &&
        pi.GetPlugin(lease.Editor) is { InternalName: "VFXEditor", IsLoaded: true } &&
        lease.TryLocate(out var scene, out var resource) && scene == lease.Scene && resource == lease.Resource;

    private Assembly Find(string name, Guid build, string version)
    {
        foreach (var assembly in AppDomain.CurrentDomain.GetAssemblies())
            if (assembly.GetName().Name == name && assembly.ManifestModule.ModuleVersionId == build &&
                pi.GetPlugin(assembly) is { IsLoaded: true } owner && owner.InternalName == name)
                return assembly;
        throw new InvalidOperationException($"透明导出需要已加载的 {name} {version}。");
    }

    private sealed class Lease(Assembly editor, Assembly brio, IDictionary previews, object preview,
        FieldInfo vfxField, object service, MethodInfo setter, MethodInfo pause, MethodInfo play,
        Type resourcePointer, int speedOffset)
    {
        public readonly Assembly Editor = editor;
        public readonly Assembly Brio = brio;
        public readonly IDictionary Previews = previews;
        public string? EffectPath;
        public nint Scene, Resource;
        public float OriginalSpeed;
        public bool PausedByStudio;

        public bool TryLocate(out nint scene, out nint resource)
        {
            scene = resource = 0;
            if (!Previews.Contains(preview) || vfxField.GetValue(preview) is not Pointer pointer)
                return false;
            var vfx = (VfxObject*)Pointer.Unbox(pointer);
            if (vfx == null || (Scene != 0 && (nint)vfx != Scene) || vfx->VfxResourceInstance == null) return false;
            scene = (nint)vfx;
            resource = (nint)vfx->VfxResourceInstance;
            return true;
        }

        // Read the named field from the same loaded resource type Brio uses in get_Speed.
        public float ReadSpeed() => BitConverter.Int32BitsToSingle(Marshal.ReadInt32(Resource, speedOffset));
        public void SetSpeed(float speed) => setter.Invoke(service, [Pointer.Box((void*)Resource, resourcePointer), speed]);
        public void Pause() => pause.Invoke(service, [Pointer.Box((void*)Resource, resourcePointer)]);
        public void Play() => play.Invoke(service, [Pointer.Box((void*)Scene, typeof(VfxObject*))]);
    }
}
