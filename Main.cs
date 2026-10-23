namespace MDRoleRecord;

public sealed class Main : MelonLoader.MelonMod
{
    public override void OnInitializeMelon()
    {
        HarmonyInstance.PatchAll();
        LoggerInstance.Msg("role_traces：选曲时预生成虚拟触点动作表，开局加载匹配缓存；手工 virtual_actions.txt 优先");
    }

    public override void OnUpdate()
    {
        // 先发布本帧选曲/对局时钟，预生成与开局校验读取同一份状态。
        BattleClock.Write();
        // 在游戏本帧判定前送出虚拟触点。
        VirtualSession.Tick();
    }

    public override void OnLateUpdate()
    {
        RoleTrace.Tick();
        NoteTrace.Tick();
    }

    public override void OnApplicationQuit()
    {
        VirtualInputReplay.ClearPendingCompletion();
        VirtualInputReplay.End();
        RoleTrace.Close();
        NoteTrace.Close();
        ChartActionCache.Close();
    }
}
