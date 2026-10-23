using Il2CppFormulaBase;
using UnityEngine;

namespace MDRoleRecord;

/// <summary>Start virtual input only when the matching action table is ready.</summary>
internal static class VirtualSession
{
    private static bool _inGame;
    private static bool _started;
    private static float _nextAttempt;

    internal static void Tick()
    {
        var battle = StageBattleComponent.instance;
        bool inGame = battle != null && battle.isInGame;
        if (!inGame)
        {
            if (_inGame)
            {
                // isInGame becomes false several seconds before End sets isSucceed.
                // Keep the chart in memory until that callback confirms a clear.
                VirtualInputReplay.HoldPendingCompletion();
                if (battle != null && battle.isSucceed && !battle.isDead)
                    VirtualInputReplay.OnSuccessfulEnd();
                VirtualInputReplay.End();
                VirtualInputReplay.ForgetStartError();
            }
            _inGame = false;
            _started = false;
            ChartActionCache.Tick();
            return;
        }

        if (!_inGame)
        {
            _inGame = true;
            _started = VirtualInputReplay.Begin();
            _nextAttempt = Time.unscaledTime + 0.15f;
        }

        // The selection clock may still describe the previous chart on entry.
        // Retry only before the first note; failed validation leaves real input alone.
        if (!_started && battle.timeFromMusicStart < 0.8f
            && Time.unscaledTime >= _nextAttempt)
        {
            _started = VirtualInputReplay.Begin();
            _nextAttempt = Time.unscaledTime + 0.15f;
        }
        VirtualInputReplay.Tick();
    }
}
