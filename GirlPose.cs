using Il2Cpp;
using Il2CppAssets.Scripts.GameCore.HostComponent;
using Il2CppAssets.Scripts.GameCore.Managers;
using UnityEngine;
using Object = UnityEngine.Object;

namespace MDRoleRecord;

/// <summary>当帧角色姿势，给注入器按动画分流用。</summary>
internal static class GirlPose
{
    private static BaseEnemyObjectController _girl;
    private static float _lastScan = -1f;

    internal static string Anim = "";
    internal static bool JumpingAction;
    internal static bool Air;
    internal static bool AirPressing;

    /// <summary>连跳／空中斩，这时按住地键会落地。</summary>
    internal static bool HopAnim
    {
        get
        {
            if (string.IsNullOrEmpty(Anim))
                return JumpingAction && !AirPressing && !PressAnim;
            return Anim.IndexOf("jump", StringComparison.OrdinalIgnoreCase) >= 0
                || Anim.IndexOf("down_hit", StringComparison.OrdinalIgnoreCase) >= 0;
        }
    }

    internal static bool PressAnim
    {
        get
        {
            if (AirPressing)
                return true;
            return !string.IsNullOrEmpty(Anim)
                && Anim.IndexOf("press", StringComparison.OrdinalIgnoreCase) >= 0;
        }
    }

    internal static void Sample()
    {
        Anim = "";
        JumpingAction = false;
        Air = false;
        AirPressing = false;
        try
        {
            var gm = GlobalManagers.s_GirlManager;
            if (gm != null)
            {
                Air = gm.m_IsAir;
                JumpingAction = gm.m_IsJumpingAction;
            }
        }
        catch { }
        try
        {
            var bm = BattleEnemyManager.instance;
            if (bm != null)
                AirPressing = bm.isAirPressing;
        }
        catch { }

        var girl = FindGirl();
        if (girl == null)
            return;
        try
        {
            var te0 = girl.m_SkeletonAnimation?.AnimationState?.GetCurrent(0);
            if (te0 != null)
                Anim = te0.Animation?.Name ?? "";
        }
        catch { }
    }

    private static BaseEnemyObjectController FindGirl()
    {
        if (Time.unscaledTime - _lastScan < 0.25f && _girl != null)
            return _girl;
        _lastScan = Time.unscaledTime;
        _girl = null;
        try
        {
            foreach (var ctrl in Object.FindObjectsOfType<BaseEnemyObjectController>())
            {
                if (ctrl == null)
                    continue;
                string name = ctrl.gameObject != null ? ctrl.gameObject.name : "";
                if (name.IndexOf("sleepy_girl", StringComparison.OrdinalIgnoreCase) < 0)
                    continue;
                if (name.IndexOf("ghost", StringComparison.OrdinalIgnoreCase) >= 0)
                    continue;
                _girl = ctrl;
                break;
            }
        }
        catch { }
        return _girl;
    }
}
