using HarmonyLib;
using Il2CppFormulaBase;
using Il2CppAssets.Scripts.PeroTools.Managers;
using Il2CppPeroPeroGames.GlobalDefines;

namespace MDRoleRecord;

[HarmonyPatch(typeof(InputManager), "GetButtonDown")]
internal static class Patch_InputManager_GetButtonDown
{
    private static bool Prefix(MDButtonType buttonName,
        ref Il2CppSystem.Collections.Generic.List<int> __result)
    {
        if (!VirtualInputReplay.Handles(buttonName)) return true;
        __result = VirtualInputReplay.GetButtonDown(buttonName);
        return false;
    }
}

[HarmonyPatch(typeof(InputManager), "GetButton")]
internal static class Patch_InputManager_GetButton
{
    private static bool Prefix(MDButtonType buttonName,
        ref Il2CppSystem.Collections.Generic.List<int> __result)
    {
        if (!VirtualInputReplay.Handles(buttonName)) return true;
        __result = VirtualInputReplay.GetButton(buttonName);
        return false;
    }
}

[HarmonyPatch(typeof(InputManager), "GetButtonUp")]
internal static class Patch_InputManager_GetButtonUp
{
    private static bool Prefix(MDButtonType buttonName,
        ref Il2CppSystem.Collections.Generic.List<int> __result)
    {
        if (!VirtualInputReplay.Handles(buttonName)) return true;
        __result = VirtualInputReplay.GetButtonUp(buttonName);
        return false;
    }
}

[HarmonyPatch(typeof(StageBattleComponent), nameof(StageBattleComponent.End))]
internal static class Patch_StageBattleComponent_End
{
    private static void Postfix(StageBattleComponent __instance)
    {
        if (__instance != null && __instance.isSucceed && !__instance.isDead)
            VirtualInputReplay.OnSuccessfulEnd();
    }
}
