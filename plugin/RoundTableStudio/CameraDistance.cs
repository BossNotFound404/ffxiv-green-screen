using FFXIVClientStructs.FFXIV.Client.Game;
using FFXIVClientStructs.FFXIV.Client.Game.Control;

namespace RoundTableStudio;

internal readonly record struct CameraRange(float Current, float Minimum, float Maximum);

internal sealed unsafe class CameraDistance
{
    internal const float ExtendedMaximum = 100f;
    private const float PitchLimit = 1.569f;

    // Only compare this identity to the live world camera. Never follow a saved pointer.
    private nint owner;
    private float? originalMaximum;
    private float appliedMaximum;
    private float? originalDistance;
    private float appliedDistance;
    private float? originalPitchMinimum;
    private float? originalPitchMaximum;

    internal bool Enabled { get; private set; }
    internal bool PitchEnabled { get; private set; }
    internal bool HasChanges => Enabled || originalDistance.HasValue || PitchEnabled
        || originalPitchMinimum.HasValue || originalPitchMaximum.HasValue;

    internal static CameraRange? Read()
    {
        var camera = WorldCamera(activeOnly: true);
        return HasRange(camera) ? new(camera->Distance, camera->MinDistance, camera->MaxDistance) : null;
    }

    internal void Enable()
    {
        var camera = ForChange();
        if (Enabled) return;
        appliedMaximum = Math.Max(ExtendedMaximum, camera->MaxDistance);
        if (camera->MaxDistance < appliedMaximum)
        {
            originalMaximum = camera->MaxDistance;
            camera->MaxDistance = appliedMaximum;
        }
        owner = (nint)camera;
        Enabled = true;
    }

    internal void SetDistance(float value)
    {
        var camera = ForChange();
        if (!float.IsFinite(value) || value < camera->MinDistance || value > camera->MaxDistance)
            throw new InvalidOperationException($"当前可用的视距为 {camera->MinDistance:0.##} 至 {camera->MaxDistance:0.##}。");
        if (camera->Distance == value) return;
        owner = (nint)camera;
        originalDistance ??= camera->Distance;
        appliedDistance = value;
        camera->Distance = value;
    }

    internal void EnablePitch()
    {
        var camera = ForChange();
        var minimum = camera->DirVMin;
        var maximum = camera->DirVMax;
        if (!float.IsFinite(minimum) || !float.IsFinite(maximum) || minimum > maximum)
            throw new InvalidOperationException("当前镜头的俯仰范围尚未就绪。");
        owner = (nint)camera;
        if (minimum != -PitchLimit)
        {
            originalPitchMinimum ??= minimum;
            camera->DirVMin = -PitchLimit;
        }
        if (maximum != PitchLimit)
        {
            originalPitchMaximum ??= maximum;
            camera->DirVMax = PitchLimit;
        }
        PitchEnabled = true;
    }

    internal void Observe() => Reconcile(WorldCamera(activeOnly: false));

    internal void Restore()
    {
        RestoreDistance();
        RestorePitch();
    }

    internal void RestoreDistance()
    {
        if (!Enabled && !originalDistance.HasValue) return;
        var camera = WorldCamera(activeOnly: false);
        Reconcile(camera);
        if (!Enabled && !originalDistance.HasValue) return;
        bool restoredLimit = originalMaximum is float maximum && maximum > camera->MinDistance;
        if (restoredLimit)
            camera->MaxDistance = originalMaximum!.Value;
        if (camera->ZoomMode == CameraZoomMode.ThirdPerson && HasRange(camera))
        {
            // Keep an independently scrolled distance wherever the restored range permits.
            if (originalDistance.HasValue || restoredLimit && camera->Distance > camera->MaxDistance)
                camera->Distance = Math.Clamp(originalDistance ?? camera->Distance, camera->MinDistance, camera->MaxDistance);
        }
        Enabled = false;
        originalMaximum = null;
        originalDistance = null;
    }

    internal void RestorePitch()
    {
        if (!PitchEnabled && !originalPitchMinimum.HasValue && !originalPitchMaximum.HasValue) return;
        var camera = WorldCamera(activeOnly: false);
        Reconcile(camera);
        if (originalPitchMinimum.HasValue || originalPitchMaximum.HasValue)
        {
            // An independently changed opposite endpoint can make the original range impossible.
            var minimum = originalPitchMinimum ?? camera->DirVMin;
            var maximum = originalPitchMaximum ?? camera->DirVMax;
            if (float.IsFinite(minimum) && float.IsFinite(maximum) && minimum <= maximum)
            {
                if (originalPitchMinimum.HasValue) camera->DirVMin = minimum;
                if (originalPitchMaximum.HasValue) camera->DirVMax = maximum;
            }
        }
        PitchEnabled = false;
        originalPitchMinimum = originalPitchMaximum = null;
    }

    private Camera* ForChange()
    {
        var camera = WorldCamera(activeOnly: true);
        if (!HasRange(camera))
            throw new InvalidOperationException("请切回可缩放的普通第三人称镜头。");
        Reconcile(camera);
        return camera;
    }

    private void Reconcile(Camera* camera)
    {
        if (!HasChanges) return;
        if (!HasRange(camera) || owner != (nint)camera)
        {
            Forget();
            return;
        }
        if (Enabled && camera->MaxDistance != appliedMaximum)
        {
            Enabled = false;
            originalMaximum = null;
        }
        if (originalDistance.HasValue && (camera->Distance != appliedDistance || camera->ZoomMode != CameraZoomMode.ThirdPerson))
            originalDistance = null;
        if (originalPitchMinimum.HasValue && camera->DirVMin != -PitchLimit)
            originalPitchMinimum = null;
        if (originalPitchMaximum.HasValue && camera->DirVMax != PitchLimit)
            originalPitchMaximum = null;
        if (PitchEnabled && (camera->DirVMin != -PitchLimit || camera->DirVMax != PitchLimit))
            PitchEnabled = false;
    }

    private void Forget()
    {
        owner = 0;
        Enabled = false;
        originalMaximum = null;
        originalDistance = null;
        PitchEnabled = false;
        originalPitchMinimum = originalPitchMaximum = null;
    }

    private static bool HasRange(Camera* camera) =>
        camera != null && float.IsFinite(camera->Distance) && float.IsFinite(camera->MinDistance)
        && float.IsFinite(camera->MaxDistance) && camera->MinDistance >= 0 && camera->MaxDistance > 0
        // A fixed-zoom preset owns the world camera even if it reports third person.
        && camera->MinDistance < camera->MaxDistance;

    private static Camera* WorldCamera(bool activeOnly)
    {
        if (Control.Addresses.Instance.Value == 0) return null;
        var manager = CameraManager.Instance();
        if (manager == null || manager->Camera == null) return null;
        if (activeOnly && (manager->ActiveCameraIndex != 0 || manager->Camera->ZoomMode != CameraZoomMode.ThirdPerson))
            return null;
        return manager->Camera;
    }
}
