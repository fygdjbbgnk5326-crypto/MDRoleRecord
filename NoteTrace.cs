using System.Globalization;
using System.Reflection;
using System.Text;
using Il2Cpp;
using Il2CppAssets.Scripts.GameCore.HostComponent;
using Il2CppFormulaBase;
using UnityEngine;

namespace MDRoleRecord;

/// <summary>
/// 音符判定 / 撞齿轮事件记录（比 RoleTrace 只看角色动画更精确的玩法事件）。
///
/// 轮询判定管理器 TaskStageTarget(单例) 的计数器，变化即写一行到 note_events_*.csv。
/// 列：t_song, d_perfect, d_great, d_miss, d_hit_enemy, d_hurt_miss,
///      perfect, great, miss, hit_enemy, hurt_miss, note_ibms, note_uid, note_pathway
///
/// d_* 是上一帧以来的增量，后面的 total_* 是累计值。
/// d_miss > 0        = 漏接音符（combo break）
/// d_hit_enemy > 0   = 撞敌人/齿轮
/// d_hurt_miss > 0   = 漏并受击（撞齿轮）
/// </summary>
internal static class NoteTrace
{
    private const float FlushSec = 0.4f;
    private static readonly CultureInfo Inv = CultureInfo.InvariantCulture;
    private static readonly string Dir = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
        "MuseDashChart", "role_traces");

    private static readonly string Header =
        "t_song,d_perfect,d_great,d_miss,d_hit_enemy,d_hurt_miss," +
        "perfect,great,miss,hit_enemy,hurt_miss,note_ibms,note_uid,note_pathway,"
        + "d_music,d_long_press,d_long_press_hit,music,long_press,long_press_hit,"
        + "d_score,score,d_miss_combo,miss_combo,air_pressing,ground_pressing";

    private static StreamWriter _writer;
    private static string _path = "";
    private static float _lastFlush = -1f;
    private static bool _wasInGame;

    private static int _lastPerfect = -1, _lastGreat = -1, _lastMiss = -1;
    private static int _lastHitEnemy = -1, _lastHurtMiss = -1;
    private static int _lastMusic = -1, _lastLongPress = -1, _lastLongPressHit = -1;
    private static int _lastScore = -1, _lastMissCombo = -1;

    internal static void Tick()
    {
        try
        {
            var sb = StageBattleComponent.instance;
            bool inGame = sb != null && sb.isInGame;
            if (!inGame)
            {
                // 退出对局：收文件。连打多把时每把都独立成文件，计数器不残留。
                if (_wasInGame)
                    Close();
                _wasInGame = false;
                return;
            }
            if (!_wasInGame)
            {
                // 新一局开始：重置上一局残留的计数器，避免第一帧 delta 错乱。
                _lastPerfect = _lastGreat = _lastMiss = _lastHitEnemy = _lastHurtMiss = -1;
                _lastMusic = _lastLongPress = _lastLongPressHit = -1;
                _lastScore = _lastMissCombo = -1;
            }
            _wasInGame = true;

            float tSong = 0f;
            try { tSong = sb.timeFromMusicStart; } catch { }

            // 强类型直接访问单例，反射在 Il2Cpp 对象上读不到属性（RoleTrace 的 hp 一直 -1 也是这个原因）
            var target = TaskStageTarget.instance;
            if (target == null)
                return;

            int perfect = target.m_PerfectResult;
            int great = target.m_GreatResult;
            int miss = target.m_MissResult;
            int hitEnemy = target.m_HitEnemy;
            int hurtMiss = target.m_HurtMiss;
            int music = target.m_MusicCount;
            int longPress = target.m_LongPressCount;
            int longPressHit = target.m_LongPressHitCount;
            int score = target.m_Score;
            int missCombo = target.m_MissCombo;
            var battleEnemy = BattleEnemyManager.instance;
            int airPressing = battleEnemy != null && battleEnemy.isAirPressing ? 1 : 0;
            int groundPressing = battleEnemy != null && battleEnemy.isGroundPressing ? 1 : 0;

            int dPerfect = Delta(perfect, ref _lastPerfect);
            int dGreat = Delta(great, ref _lastGreat);
            int dMiss = Delta(miss, ref _lastMiss);
            int dHit = Delta(hitEnemy, ref _lastHitEnemy);
            int dHurt = Delta(hurtMiss, ref _lastHurtMiss);
            int dMusic = Delta(music, ref _lastMusic);
            int dLongPress = Delta(longPress, ref _lastLongPress);
            int dLongPressHit = Delta(longPressHit, ref _lastLongPressHit);
            int dScore = Delta(score, ref _lastScore);
            int dMissCombo = Delta(missCombo, ref _lastMissCombo);

            if (dPerfect == 0 && dGreat == 0 && dMiss == 0 && dHit == 0 && dHurt == 0
                && dMusic == 0 && dLongPress == 0 && dLongPressHit == 0
                && dScore == 0 && dMissCombo == 0)
                return;

            string ibms = "", uid = "", pathway = "";
            ReadCurNote(sb, ref ibms, ref uid, ref pathway);

            EnsureOpen();
            if (_writer == null)
                return;

            WriteRow(tSong, dPerfect, dGreat, dMiss, dHit, dHurt,
                perfect, great, miss, hitEnemy, hurtMiss, ibms, uid, pathway,
                dMusic, dLongPress, dLongPressHit, music, longPress, longPressHit,
                dScore, score, dMissCombo, missCombo, airPressing, groundPressing);

            if (Time.unscaledTime - _lastFlush >= FlushSec)
            {
                try { _writer.Flush(); } catch { }
                _lastFlush = Time.unscaledTime;
            }
        }
        catch { }
    }

    internal static void Close()
    {
        try { _writer?.Flush(); _writer?.Dispose(); } catch { }
        _writer = null;
        _lastPerfect = _lastGreat = _lastMiss = _lastHitEnemy = _lastHurtMiss = -1;
        _lastMusic = _lastLongPress = _lastLongPressHit = -1;
        _lastScore = _lastMissCombo = -1;
        if (!string.IsNullOrEmpty(_path))
        {
            try { File.WriteAllText(Path.Combine(Dir, "note_latest.txt"), _path, new UTF8Encoding(false)); } catch { }
        }
    }

    private static int Delta(int cur, ref int last)
    {
        if (cur < 0)
            return 0;
        int d = last < 0 ? 0 : cur - last;
        last = cur;
        return d;
    }

    /// <summary>尽量读当前被判定音符的 ibms_id / uid / 轨道。</summary>
    private static void ReadCurNote(StageBattleComponent sb, ref string ibms, ref string uid, ref string pathway)
    {
        try
        {
            var m = sb.GetType().GetMethod("GetCurMusicData",
                BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);
            if (m == null)
                return;
            for (int air = 0; air <= 1; air++)
            {
                object md = null;
                try { md = m.Invoke(sb, new object[] { air == 1 }); } catch { }
                if (md == null) continue;

                var noteData = ReadProp(md, "noteData");
                if (noteData == null) continue;

                string s = ReadStr(noteData, "ibms_id", "ibmsId");
                if (!string.IsNullOrEmpty(s)) ibms = s;
                s = ReadStr(noteData, "uid", "m_BmsUid", "noteUid", "bmsUid");
                if (!string.IsNullOrEmpty(s)) uid = s;
                int pw = ReadInt(noteData, "pathway");
                if (pw >= 0) pathway = pw == 1 ? "air" : "road";
                if (!string.IsNullOrEmpty(ibms) || !string.IsNullOrEmpty(uid))
                    break;
            }
        }
        catch { }
    }

    private static void EnsureOpen()
    {
        if (_writer != null)
            return;
        try
        {
            Directory.CreateDirectory(Dir);
            _path = Path.Combine(Dir, "note_events_" + DateTime.Now.ToString("yyyyMMdd_HHmmss", Inv) + ".csv");
            _writer = new StreamWriter(_path, false, new UTF8Encoding(false), 1 << 15);
            _writer.WriteLine(Header);
            _lastFlush = Time.unscaledTime;
        }
        catch { _writer = null; }
    }

    private static void WriteRow(float t, int dp, int dg, int dm, int dh, int dhu,
        int p, int g, int m, int h, int hu, string ibms, string uid, string pathway,
        int dmusic, int dlong, int dlongHit, int music, int longPress, int longPressHit,
        int dScore, int score, int dMissCombo, int missCombo, int airPressing, int groundPressing)
    {
        var w = _writer;
        w.Write(t.ToString("F4", Inv)); w.Write(',');
        w.Write(dp); w.Write(','); w.Write(dg); w.Write(','); w.Write(dm); w.Write(',');
        w.Write(dh); w.Write(','); w.Write(dhu); w.Write(',');
        w.Write(p); w.Write(','); w.Write(g); w.Write(','); w.Write(m); w.Write(',');
        w.Write(h); w.Write(','); w.Write(hu); w.Write(',');
        w.Write(ibms ?? ""); w.Write(',');
        w.Write(uid ?? ""); w.Write(',');
        w.Write(pathway ?? ""); w.Write(',');
        w.Write(dmusic); w.Write(','); w.Write(dlong); w.Write(','); w.Write(dlongHit); w.Write(',');
        w.Write(music); w.Write(','); w.Write(longPress); w.Write(','); w.Write(longPressHit); w.Write(',');
        w.Write(dScore); w.Write(','); w.Write(score); w.Write(',');
        w.Write(dMissCombo); w.Write(','); w.Write(missCombo); w.Write(',');
        w.Write(airPressing); w.Write(','); w.WriteLine(groundPressing);
    }

    private static int ReadInt(object obj, params string[] names)
    {
        if (obj == null) return -1;
        var t = obj.GetType();
        foreach (var name in names)
        {
            try
            {
                var p = t.GetProperty(name, BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);
                if (p != null)
                {
                    var v = p.GetValue(obj);
                    if (v is int i) return i;
                    if (v is short s) return s;
                    if (v is byte b) return b;
                    if (v != null && int.TryParse(v.ToString(), out int n)) return n;
                }
            }
            catch { }
            try
            {
                var f = t.GetField(name, BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);
                if (f != null)
                {
                    var v = f.GetValue(obj);
                    if (v is int i) return i;
                    if (v is short s) return s;
                    if (v is byte b) return b;
                    if (v != null && int.TryParse(v.ToString(), out int n)) return n;
                }
            }
            catch { }
        }
        return -1;
    }

    private static object ReadProp(object obj, string name)
    {
        if (obj == null) return null;
        var t = obj.GetType();
        try
        {
            var p = t.GetProperty(name, BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);
            if (p != null) return p.GetValue(obj);
        }
        catch { }
        try
        {
            var f = t.GetField(name, BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);
            if (f != null) return f.GetValue(obj);
        }
        catch { }
        return null;
    }

    private static string ReadStr(object obj, params string[] names)
    {
        if (obj == null) return "";
        var t = obj.GetType();
        foreach (var name in names)
        {
            try
            {
                var p = t.GetProperty(name, BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);
                if (p != null)
                {
                    var v = p.GetValue(obj);
                    if (v != null) return v.ToString();
                }
            }
            catch { }
            try
            {
                var f = t.GetField(name, BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);
                if (f != null)
                {
                    var v = f.GetValue(obj);
                    if (v != null) return v.ToString();
                }
            }
            catch { }
        }
        return "";
    }
}
