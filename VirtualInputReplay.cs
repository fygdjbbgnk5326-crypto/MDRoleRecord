using System.Globalization;
using System.Reflection;
using System.Text;
using Il2Cpp;
using Il2CppAssets.Scripts.GameCore.HostComponent;
using Il2CppAssets.Scripts.GameCore.Managers;
using Il2CppFormulaBase;
using Il2CppPeroPeroGames.GlobalDefines;
using UnityEngine;

namespace MDRoleRecord;

/// <summary>
/// Replays paired input edges through InputManager only. Each chart event owns a
/// finger until its up edge, so a collect pulse can coexist with a long hold.
/// This class never calls GameManager.AutoPlay or DBSkill.SetAutoPlay.
/// </summary>
internal static class VirtualInputReplay
{
    private readonly struct Edge
    {
        internal readonly float Time;
        internal readonly bool Down;
        internal readonly bool Air;
        internal readonly string Kind;
        internal readonly int EventId;

        internal Edge(float time, bool down, bool air, string kind, int eventId)
        {
            Time = time;
            Down = down;
            Air = air;
            Kind = kind;
            EventId = eventId;
        }
    }

    private sealed class Finger
    {
        internal int Id;
        internal int DownFrame;
        internal Edge DownEdge;
    }

    private sealed class DodgeAttempt
    {
        internal float DownSong;
        internal bool LastAir;
        internal bool LastJumping;
        internal bool Activated;
        internal bool Retried;
    }

    private static readonly CultureInfo Inv = CultureInfo.InvariantCulture;
    private static readonly string Dir = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
        "MuseDashChart", "role_traces");
    private static readonly List<Edge> Edges = new();
    private static Edge[] _retainedEdges = Array.Empty<Edge>();
    private static string _retainedKey = "";
    private static Edge[] _pendingCompletionEdges = Array.Empty<Edge>();
    private static ChartActionCache.Selection _pendingCompletionSelection;
    private static ChartActionCache.Selection _loadedSelection;
    private static bool _hasLoadedSelection;
    private static readonly Dictionary<(bool Air, int EventId), Finger> Active = new();
    private static readonly List<Edge> DeferredUps = new();
    private static readonly HashSet<int> ReservedIds = new();
    private static readonly List<int> AirDown = new(), GroundDown = new();
    private static readonly List<int> AirHold = new(), GroundHold = new();
    private static readonly List<int> AirUp = new(), GroundUp = new();
    private static readonly Dictionary<int, DodgeAttempt> DodgeAttempts = new();
    private static readonly Dictionary<int, float> RetryUps = new();
    private static int _nextRetryEventId = 2_000_000;
    private static StreamWriter _audit;
    private static StreamWriter _stateAudit;
    private static int _index, _delivered, _skipped, _faults;
    private static long _downQueries, _holdQueries, _upQueries;
    private static float _lastSong, _lastFlush;
    private static float _lastStateSample;
    private static bool _enabled;
    private static int _releaseFrame = -1;
    private static string _lastStartError = "";
    private static PropertyInfo _dbSkillProperty, _autoStateProperty;

    internal static bool Enabled => _enabled;

    internal static bool Begin()
    {
        ClearPendingCompletion();
        End();
        if (!TryReadOfficialAuto(out bool autoEnabled))
        {
            ReportStartError("无法确认官机 AutoPlay 状态");
            return false;
        }
        if (autoEnabled)
        {
            ReportStartError("请在游戏菜单关闭官机 AutoPlay");
            return false;
        }
        if (!TryLoad(out string error))
        {
            ReportStartError("动作表拒绝加载: " + error);
            return false;
        }
        try
        {
            Directory.CreateDirectory(Dir);
            string path = Path.Combine(Dir, "virtual_input_" + DateTime.Now.ToString("yyyyMMdd_HHmmss", Inv) + ".csv");
            _audit = new StreamWriter(path, false, new UTF8Encoding(false));
            _audit.WriteLine("song_time,scheduled,edge,lane,kind,event_id,finger,frame,result");
            string statePath = Path.Combine(Dir, "virtual_state_" + DateTime.Now.ToString("yyyyMMdd_HHmmss", Inv) + ".csv");
            _stateAudit = new StreamWriter(statePath, false, new UTF8Encoding(false));
            _stateAudit.WriteLine("song_time,frame,is_air,jumping,air_pressing,ground_pressing,air_fingers,ground_fingers");
            _enabled = true;
            _lastStartError = "";
            _lastSong = float.NaN;
            _lastStateSample = -99f;
            MelonLoader.MelonLogger.Msg("[VirtualInput] 仅虚拟输入已启用；" + Edges.Count
                + " 条边沿；官机 AutoPlay=off；" + path + "；姿态=" + statePath);
            return true;
        }
        catch (Exception ex)
        {
            End();
            ReportStartError("无法创建审计文件: " + ex.Message);
            return false;
        }
    }

    private static void ReportStartError(string error)
    {
        if (error == _lastStartError) return;
        _lastStartError = error;
        MelonLoader.MelonLogger.Warning("[VirtualInput] 虚拟注入未启用: " + error);
    }

    internal static void End()
    {
        if (_audit != null)
        {
            try
            {
                _audit.Flush();
                _audit.Dispose();
                MelonLoader.MelonLogger.Msg("[VirtualInput] 结束：delivered=" + _delivered
                    + " skipped=" + _skipped + " faults=" + _faults
                    + " queries(down/hold/up)=" + _downQueries + "/" + _holdQueries + "/" + _upQueries);
            }
            catch { }
            _audit = null;
        }
        try { _stateAudit?.Flush(); _stateAudit?.Dispose(); } catch { }
        _stateAudit = null;
        _enabled = false;
        _releaseFrame = -1;
        Edges.Clear();
        _hasLoadedSelection = false;
        Active.Clear();
        DodgeAttempts.Clear();
        RetryUps.Clear();
        _nextRetryEventId = 2_000_000;
        DeferredUps.Clear();
        ReservedIds.Clear();
        ClearSnapshots();
        _index = _delivered = _skipped = _faults = 0;
        _downQueries = _holdQueries = _upQueries = 0;
        _lastSong = float.NaN;
    }

    internal static void ForgetStartError() => _lastStartError = "";

    internal static void ForgetRetained()
    {
        _retainedEdges = Array.Empty<Edge>();
        _retainedKey = "";
    }

    internal static void HoldPendingCompletion()
    {
        if (!_enabled || !_hasLoadedSelection || Edges.Count == 0) return;
        _pendingCompletionEdges = Edges.ToArray();
        _pendingCompletionSelection = _loadedSelection;
    }

    internal static void ClearPendingCompletion()
    {
        _pendingCompletionEdges = Array.Empty<Edge>();
        _pendingCompletionSelection = default;
    }

    internal static void OnSuccessfulEnd()
    {
        Edge[] snapshot;
        ChartActionCache.Selection selection;
        if (_enabled && _hasLoadedSelection && Edges.Count > 0)
        {
            snapshot = Edges.ToArray();
            selection = _loadedSelection;
        }
        else
        {
            snapshot = _pendingCompletionEdges;
            selection = _pendingCompletionSelection;
        }
        ClearPendingCompletion();
        if (snapshot.Length == 0 || !ChartActionCache.RetireCompleted(selection)) return;
        _retainedEdges = snapshot;
        _retainedKey = selection.Key;
    }

    internal static void Tick()
    {
        if (!_enabled)
        {
            if (_releaseFrame >= 0 && _releaseFrame != Time.frameCount)
            {
                ClearSnapshots();
                _releaseFrame = -1;
            }
            return;
        }
        try
        {
            var battle = StageBattleComponent.instance;
            if (battle == null || !battle.isInGame)
            {
                End();
                return;
            }
            if (!TryReadOfficialAuto(out bool autoEnabled))
            {
                Abort("无法确认官机 AutoPlay 状态");
                return;
            }
            if (autoEnabled)
            {
                Abort("官机 AutoPlay 在对局中开启");
                return;
            }

            float song = battle.timeFromMusicStart;
            if (!float.IsFinite(song))
            {
                Abort("歌曲时间无效");
                return;
            }
            if (float.IsFinite(_lastSong) && song < _lastSong - 0.05f)
            {
                Abort("歌曲时间倒退；本局停止注入，避免遗留触点");
                return;
            }
            _lastSong = song;
            ClearSnapshots();

            // An up that arrived in the same frame as its down must wait one frame.
            for (int i = DeferredUps.Count - 1; i >= 0; i--)
            {
                var edge = DeferredUps[i];
                if (Active.TryGetValue((edge.Air, edge.EventId), out var finger)
                    && finger.DownFrame < Time.frameCount)
                {
                    DeliverUp(edge, song);
                    DeferredUps.RemoveAt(i);
                }
            }

            foreach (var retry in RetryUps.ToArray())
            {
                if (song < retry.Value) continue;
                DeliverUp(new Edge(retry.Value, false, true, "dodge_retry", retry.Key), song);
                RetryUps.Remove(retry.Key);
            }

            float horizon = song + Mathf.Clamp(Time.deltaTime, 0f, 0.034f);
            while (_index < Edges.Count && Edges[_index].Time <= horizon)
            {
                var edge = Edges[_index++];
                if (edge.Down)
                {
                    if (edge.Time < song - 0.08f)
                    {
                        _skipped++;
                        Audit(edge, song, -1, "late_down_skipped");
                    }
                    else
                        DeliverDown(edge, song);
                }
                else if (Active.TryGetValue((edge.Air, edge.EventId), out var finger))
                {
                    if (finger.DownFrame == Time.frameCount)
                        DeferredUps.Add(edge);
                    else
                        DeliverUp(edge, song);
                }
                else
                {
                    _skipped++;
                    Audit(edge, song, -1, "up_without_down");
                }
            }

            RetryFailedDodges(song);

            foreach (var pair in Active)
            {
                if (pair.Key.Air) AirHold.Add(pair.Value.Id);
                else GroundHold.Add(pair.Value.Id);
            }
            AirHold.Sort();
            GroundHold.Sort();
            SampleState(song);
            if (Time.unscaledTime - _lastFlush >= 0.5f)
            {
                _audit?.Flush();
                _stateAudit?.Flush();
                _lastFlush = Time.unscaledTime;
            }
        }
        catch (Exception ex)
        {
            Abort(ex.GetType().Name + ": " + ex.Message);
        }
    }

    private static void DeliverDown(Edge edge, float song)
    {
        var key = (edge.Air, edge.EventId);
        if (Active.ContainsKey(key))
        {
            _faults++;
            Audit(edge, song, -1, "duplicate_down");
            return;
        }
        int id = 0;
        while (ReservedIds.Contains(id)) id++;
        // Keep IDs in a plausible touch range. A malformed dense chart fails closed.
        if (id >= 32)
        {
            Abort("同时触点超过 32 个");
            return;
        }
        ReservedIds.Add(id);
        Active.Add(key, new Finger { Id = id, DownFrame = Time.frameCount, DownEdge = edge });
        if (edge.Air && edge.Kind == "dodge")
        {
            try
            {
                var girl = GlobalManagers.s_GirlManager;
                if (girl != null)
                    DodgeAttempts[edge.EventId] = new DodgeAttempt
                    {
                        DownSong = song,
                        LastAir = girl.m_IsAir,
                        LastJumping = girl.m_IsJumpingAction,
                    };
            }
            catch { }
        }
        if (edge.Air) AirDown.Add(id);
        else GroundDown.Add(id);
        _delivered++;
        Audit(edge, song, id, "delivered");
    }

    private static void DeliverUp(Edge edge, float song)
    {
        var key = (edge.Air, edge.EventId);
        if (!Active.TryGetValue(key, out var finger))
            return;
        Active.Remove(key);
        if (edge.Air && edge.Kind == "dodge")
            DodgeAttempts.Remove(edge.EventId);
        // Do not reuse this ID until the following frame; GetButtonUp can be queried later.
        if (edge.Air) AirUp.Add(finger.Id);
        else GroundUp.Add(finger.Id);
        _delivered++;
        Audit(edge, song, finger.Id, "delivered");
    }

    private static void RetryFailedDodges(float song)
    {
        bool air, jumping;
        try
        {
            var girl = GlobalManagers.s_GirlManager;
            if (girl == null) return;
            air = girl.m_IsAir;
            jumping = girl.m_IsJumpingAction;
        }
        catch { return; }

        foreach (var pair in DodgeAttempts)
        {
            var attempt = pair.Value;
            if (!Active.ContainsKey((true, pair.Key))) continue;
            if ((!attempt.LastAir && air) || (!attempt.LastJumping && jumping))
                attempt.Activated = true;
            attempt.LastAir = air;
            attempt.LastJumping = jumping;
            if (attempt.Activated || attempt.Retried || air || jumping
                || song - attempt.DownSong < 0.045f)
                continue;

            // A down can be consumed while the previous jump action is still
            // ending. Retry once only if no new rise was observed afterward.
            int id = 0;
            while (ReservedIds.Contains(id)) id++;
            if (id >= 32) continue;
            int retryId = _nextRetryEventId++;
            attempt.Retried = true;
            DeliverDown(new Edge(song, true, true, "dodge_retry", retryId), song);
            RetryUps[retryId] = song + 0.040f;
            break;
        }
    }

    private static void ClearSnapshots()
    {
        foreach (int id in AirUp) ReservedIds.Remove(id);
        foreach (int id in GroundUp) ReservedIds.Remove(id);
        AirDown.Clear(); GroundDown.Clear();
        AirHold.Clear(); GroundHold.Clear();
        AirUp.Clear(); GroundUp.Clear();
    }

    private static void Abort(string reason)
    {
        MelonLoader.MelonLogger.Warning("[VirtualInput] 注入停止: " + reason);
        var air = new List<int>();
        var ground = new List<int>();
        foreach (var pair in Active)
        {
            if (pair.Key.Air) air.Add(pair.Value.Id);
            else ground.Add(pair.Value.Id);
        }
        End();
        AirUp.AddRange(air);
        GroundUp.AddRange(ground);
        _releaseFrame = Time.frameCount;
    }

    internal static bool Handles(MDButtonType button)
        => (Enabled || _releaseFrame == Time.frameCount)
            && (button == MDButtonType.BATTLE_AIR || button == MDButtonType.BATTLE_GROUND);

    internal static Il2CppSystem.Collections.Generic.List<int> GetButtonDown(MDButtonType button)
    {
        _downQueries++;
        return Copy(button == MDButtonType.BATTLE_AIR ? AirDown : GroundDown);
    }

    internal static Il2CppSystem.Collections.Generic.List<int> GetButton(MDButtonType button)
    {
        _holdQueries++;
        return Copy(button == MDButtonType.BATTLE_AIR ? AirHold : GroundHold);
    }

    internal static Il2CppSystem.Collections.Generic.List<int> GetButtonUp(MDButtonType button)
    {
        _upQueries++;
        return Copy(button == MDButtonType.BATTLE_AIR ? AirUp : GroundUp);
    }

    private static Il2CppSystem.Collections.Generic.List<int> Copy(List<int> ids)
    {
        // The game can mutate a returned touch list; never expose stored snapshots.
        var result = new Il2CppSystem.Collections.Generic.List<int>();
        foreach (int id in ids) result.Add(id);
        return result;
    }

    private static void Audit(Edge edge, float song, int finger, string result)
    {
        if (_audit == null) return;
        _audit.Write(song.ToString("F4", Inv)); _audit.Write(',');
        _audit.Write(edge.Time.ToString("F4", Inv)); _audit.Write(',');
        _audit.Write(edge.Down ? "down" : "up"); _audit.Write(',');
        _audit.Write(edge.Air ? "air" : "ground"); _audit.Write(',');
        _audit.Write(edge.Kind); _audit.Write(',');
        _audit.Write(edge.EventId); _audit.Write(',');
        _audit.Write(finger); _audit.Write(',');
        _audit.Write(Time.frameCount); _audit.Write(',');
        _audit.WriteLine(result);
    }

    private static void SampleState(float song)
    {
        if (_stateAudit == null || song - _lastStateSample < 0.01f) return;
        _lastStateSample = song;
        int air = -1, jumping = -1, airPress = -1, groundPress = -1;
        try
        {
            var girl = GlobalManagers.s_GirlManager;
            if (girl != null)
            {
                air = girl.m_IsAir ? 1 : 0;
                jumping = girl.m_IsJumpingAction ? 1 : 0;
            }
            var enemy = BattleEnemyManager.instance;
            if (enemy != null)
            {
                airPress = enemy.isAirPressing ? 1 : 0;
                groundPress = enemy.isGroundPressing ? 1 : 0;
            }
        }
        catch { }
        _stateAudit.Write(song.ToString("F4", Inv)); _stateAudit.Write(',');
        _stateAudit.Write(Time.frameCount); _stateAudit.Write(',');
        _stateAudit.Write(air); _stateAudit.Write(',');
        _stateAudit.Write(jumping); _stateAudit.Write(',');
        _stateAudit.Write(airPress); _stateAudit.Write(',');
        _stateAudit.Write(groundPress); _stateAudit.Write(',');
        _stateAudit.Write(AirHold.Count); _stateAudit.Write(',');
        _stateAudit.WriteLine(GroundHold.Count);
    }


    private static bool TryLoad(out string error)
    {
        error = "";
        if (!ChartActionCache.TryReadSelection(out var selection))
        {
            error = "无法确认当前曲目和难度";
            return false;
        }
        if (ChartActionCache.IsRetired(selection) && _retainedKey == selection.Key
            && _retainedEdges.Length > 0)
        {
            Edges.AddRange(_retainedEdges);
            _loadedSelection = selection;
            _hasLoadedSelection = true;
            MelonLoader.MelonLogger.Msg("[VirtualInput] 从内存重放已结算曲目: " + selection.Key);
            return true;
        }
        if (!ChartActionCache.TrySelectFile(selection, out string actionsFile, out error))
            return false;
        string[] lines;
        try { lines = File.ReadAllLines(actionsFile); }
        catch (Exception ex) { error = ex.Message; return false; }
        var header = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        var parsed = new List<Edge>();
        var opened = new HashSet<(bool Air, int EventId)>();
        int count = 0;
        foreach (string source in lines)
        {
            string line = source.Trim();
            if (line.Length == 0) continue;
            if (line.StartsWith("#"))
            {
                int split = line.IndexOf('=');
                if (split > 1)
                    header[line.Substring(1, split - 1).Trim()] = line.Substring(split + 1).Trim();
                continue;
            }
            count++;
            var p = line.Split(' ', StringSplitOptions.RemoveEmptyEntries);
            if (p.Length != 5 || !float.TryParse(p[0], NumberStyles.Float, Inv, out float t)
                || !float.IsFinite(t) || t < 0f || (p[1] != "down" && p[1] != "up")
                || (p[2] != "air" && p[2] != "ground")
                || !IsKind(p[3])
                || !int.TryParse(p[4], NumberStyles.Integer, Inv, out int eventId) || eventId <= 0)
            { error = "无效数据行 " + count; return false; }
            parsed.Add(new Edge(t, p[1] == "down", p[2] == "air", p[3], eventId));
        }
        if (!header.TryGetValue("mdr_virtual_format", out string format) || format != "1"
            || !header.TryGetValue("chart_uid", out string chartUid) || string.IsNullOrWhiteSpace(chartUid)
            || !header.TryGetValue("map_index", out string mapText)
            || !int.TryParse(mapText, out int map) || map < 1 || map > 6
            || !header.TryGetValue("action_count", out string countText)
            || !int.TryParse(countText, out int expected) || expected != count || count == 0)
        { error = "v1 元数据不完整或动作数量不符"; return false; }
        if (!string.Equals(selection.Uid, chartUid, StringComparison.OrdinalIgnoreCase)
            || selection.Map != map)
        {
            error = "曲目/难度不匹配：动作=" + chartUid + " map" + map
                + "，对局=" + selection.Uid + " map" + selection.Map;
            return false;
        }
        if (header.TryGetValue("stage_map", out string stageMap)
            ? !string.Equals(stageMap, selection.StageMap, StringComparison.OrdinalIgnoreCase)
            : selection.IsVariant)
        {
            error = "谱面对象不匹配：动作=" + (stageMap ?? "(未标记)")
                + "，对局=" + selection.StageMap;
            return false;
        }
        float previous = -1f;
        foreach (var edge in parsed)
        {
            if (edge.Time < previous) { error = "动作顺序错误"; return false; }
            previous = edge.Time;
            var key = (edge.Air, edge.EventId);
            if (edge.Down)
            {
                if (!opened.Add(key)) { error = "重复 down: " + edge.EventId; return false; }
            }
            else if (!opened.Remove(key)) { error = "没有配对 down 的 up: " + edge.EventId; return false; }
        }
        if (opened.Count != 0) { error = "存在未抬起的触点"; return false; }
        Edges.AddRange(parsed);
        _loadedSelection = selection;
        _hasLoadedSelection = true;
        MelonLoader.MelonLogger.Msg("[VirtualInput] 加载动作表: " + actionsFile);
        return true;
    }

    private static bool IsKind(string kind)
        => kind == "tap" || kind == "hold" || kind == "dodge"
            || kind == "collect" || kind == "mash" || kind == "bar_star";

    private static bool TryReadOfficialAuto(out bool enabled)
    {
        enabled = false;
        try
        {
            if (_dbSkillProperty == null || _autoStateProperty == null)
            {
                var asm = typeof(StageBattleComponent).Assembly;
                var database = asm.GetType("Il2CppAssets.Scripts.Database.GlobalDataBase", false);
                var type = asm.GetType("Il2CppAssets.Scripts.Database.DBSkill", false);
                _dbSkillProperty = database?.GetProperty("dbSkill", BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Static);
                _autoStateProperty = type?.GetProperty("isAutoPlay", BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance);
            }
            var skill = _dbSkillProperty?.GetValue(null);
            if (skill == null || _autoStateProperty?.GetValue(skill) is not bool value) return false;
            enabled = value;
            return true;
        }
        catch { return false; }
    }
}
