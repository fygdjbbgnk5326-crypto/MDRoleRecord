using System.Globalization;
using System.Reflection;
using System.Text;
using Il2Cpp;
using Il2CppAssets.Scripts.GameCore.HostComponent;
using Il2CppAssets.Scripts.GameCore.Managers;
using Il2CppFormulaBase;
using Il2CppSpine;
using UnityEngine;
using Object = UnityEngine.Object;

namespace MDRoleRecord;

/// <summary>
/// 官机梦游少女动作。进对局自动记，出对局收文件。
/// 前三列仍是 t_song,anim_changed,anim，旧解析脚本不用改。
/// </summary>
internal static class RoleTrace
{
    private const float HeartbeatSec = 0.02f;
    private const float FlushSec = 0.4f;
    private static readonly CultureInfo Inv = CultureInfo.InvariantCulture;
    private static readonly string Dir = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
        "MuseDashChart", "role_traces");

    private static readonly string Header =
        "t_song,anim_changed,anim,track_time,anim_time,mix,alpha,loop,complete," +
        "anim1,t1,mix1,pos_x,pos_y,body,body_x,body_y,foot,foot_y,model_y,skel_x,skel_y,scale_x,scale_y," +
        "hp,hp_delta,combo,combo_delta,fever,is_air,is_jumping,is_air_pressing,girl_pos_y,event";

    private static float _lastHeartbeat = -1f;
    private static float _lastFlush = -1f;
    private static float _lastScan = -1f;
    private static BaseEnemyObjectController _girl;
    private static Bone _body;
    private static string _bodyName = "";
    private static Bone _foot;
    private static string _footName = "";
    private static bool _bonesDumped = false;
    private static bool _childrenDumped = false;
    private static StreamWriter _writer;
    private static string _path = "";
    private static string _lastAnim = "";
    private static string _lastAnim1 = "";
    private static int _lastHp = int.MinValue;
    private static int _lastCombo = int.MinValue;
    private static int _lastFever = int.MinValue;
    private static bool _wasInGame;

    internal static void Tick()
    {
        bool inGame = false;
        float tSong = 0f;
        object battle = null;
        try
        {
            var sb = StageBattleComponent.instance;
            if (sb != null)
            {
                inGame = sb.isInGame;
                battle = sb;
                try { tSong = sb.timeFromMusicStart; } catch { }
            }
        }
        catch { }

        if (!inGame)
        {
            if (_wasInGame) Close();
            _wasInGame = false;
            return;
        }
        _wasInGame = true;

        var girl = FindGirl();
        if (girl == null)
            return;

        DumpBones(girl);
        DumpChildren(girl);

        string anim = "";
        float trackTime = float.NaN, animTime = float.NaN, mix = float.NaN, alpha = float.NaN;
        int loop = -1, complete = -1;
        string anim1 = "";
        float t1 = float.NaN, mix1 = float.NaN;
        ReadTracks(girl, ref anim, ref trackTime, ref animTime, ref mix, ref alpha, ref loop, ref complete,
            ref anim1, ref t1, ref mix1);
        if (string.IsNullOrEmpty(anim))
            return;

        Vector3 pos = default;
        Vector3 scale = Vector3.one;
        try
        {
            var tr = girl.transform;
            pos = tr.position;
            scale = tr.localScale;
        }
        catch { }

        // 视觉模型的 transform（跳跃是挪模型这层，不是控制器的 transform）
        float modelY = float.NaN;
        try
        {
            var sa = girl.m_SkeletonAnimation;
            if (sa != null)
                modelY = sa.transform.position.y;
        }
        catch { }

        // spine 骨架的根偏移（小写 x/y）——跳跃很可能就是挪这个，而不是 transform/骨骼 WorldY
        float skelX = float.NaN, skelY = float.NaN;
        try
        {
            var sk = girl.m_SkeletonAnimation?.Skeleton;
            if (sk != null)
            {
                skelX = sk.x;
                skelY = sk.y;
            }
        }
        catch { }

        // 逻辑轨道（Air/Road）+ 跳跃动作状态 + 空中键按住状态。字段读避免闪退
        float isAir = float.NaN, isJumping = float.NaN, isAirPressing = float.NaN;
        try
        {
            var gm = GlobalManagers.s_GirlManager;
            if (gm != null)
            {
                isAir = gm.m_IsAir ? 1f : 0f;
                isJumping = gm.m_IsJumpingAction ? 1f : 0f;
            }
        }
        catch { }
        try
        {
            var bm = BattleEnemyManager.instance;
            if (bm != null)
                isAirPressing = bm.isAirPressing ? 1f : 0f;
        }
        catch { }

        // 角色实际世界坐标（可能是碰撞体位置，跳跃时上下动）
        float girlPosY = float.NaN;
        try
        {
            var gm2 = GlobalManagers.s_GirlManager;
            if (gm2 != null)
                girlPosY = gm2.GetCurrentGirlPositon().y;
        }
        catch { }

        float bodyX = float.NaN, bodyY = float.NaN;
        ReadBody(girl, ref bodyX, ref bodyY);

        float footY = float.NaN;
        ReadFoot(girl, ref footY);

        int hp = ReadInt(battle, "hp", "HP", "curHp", "currentHp", "playerHp", "blood");
        int combo = ReadInt(battle, "combo", "Combo", "currentCombo", "curCombo");
        int fever = ReadInt(battle, "fever", "Fever", "feverValue", "isFever");
        if (fever < 0)
            fever = ReadBool(battle, "isFever", "IsFever", "inFever") ? 1 : 0;

        bool animChanged = anim != _lastAnim;
        bool overlayChanged = anim1 != _lastAnim1;
        int hpDelta = 0;
        int comboDelta = 0;
        if (_lastHp != int.MinValue && hp >= 0 && hp != _lastHp)
            hpDelta = hp - _lastHp;
        if (_lastCombo != int.MinValue && combo >= 0 && combo != _lastCombo)
            comboDelta = combo - _lastCombo;
        bool hpChanged = hpDelta != 0;
        bool comboChanged = comboDelta != 0;
        bool feverChanged = fever >= 0 && fever != _lastFever && _lastFever != int.MinValue;

        float now = Time.unscaledTime;
        bool heartbeat = now - _lastHeartbeat >= HeartbeatSec;
        if (!animChanged && !overlayChanged && !hpChanged && !comboChanged && !feverChanged && !heartbeat)
            return;
        _lastHeartbeat = now;

        EnsureOpen();
        if (_writer == null)
            return;

        string ev = "sample";
        if (animChanged) ev = "anim";
        else if (hpChanged) ev = hpDelta < 0 ? "hurt" : "heal";
        else if (comboChanged) ev = comboDelta < 0 ? "combo_break" : "combo";
        else if (overlayChanged) ev = "overlay";
        else if (feverChanged) ev = "fever";

        _lastAnim = anim;
        _lastAnim1 = anim1;
        if (hp >= 0) _lastHp = hp;
        if (combo >= 0) _lastCombo = combo;
        if (fever >= 0) _lastFever = fever;

        WriteRow(tSong, animChanged, anim, trackTime, animTime, mix, alpha, loop, complete,
            anim1, t1, mix1, pos.x, pos.y, _bodyName, bodyX, bodyY, _footName, footY, modelY, skelX, skelY, scale.x, scale.y,
            hp, hpDelta, combo, comboDelta, fever, isAir, isJumping, isAirPressing, girlPosY, ev);

        if (now - _lastFlush >= FlushSec)
        {
            try { _writer.Flush(); } catch { }
            _lastFlush = now;
        }
    }

    internal static void Close()
    {
        try
        {
            _writer?.Flush();
            _writer?.Dispose();
        }
        catch { }
        _writer = null;
        _girl = null;
        _body = null;
        _bodyName = "";
        _foot = null;
        _footName = "";
        _lastAnim = "";
        _lastAnim1 = "";
        _lastHp = int.MinValue;
        _lastCombo = int.MinValue;
        _lastFever = int.MinValue;
        if (!string.IsNullOrEmpty(_path))
        {
            try
            {
                File.WriteAllText(Path.Combine(Dir, "latest.txt"), _path, new UTF8Encoding(false));
            }
            catch { }
        }
    }

    private static void EnsureOpen()
    {
        if (_writer != null)
            return;
        try
        {
            Directory.CreateDirectory(Dir);
            _path = Path.Combine(Dir, "sleepy_girl_" + DateTime.Now.ToString("yyyyMMdd_HHmmss", Inv) + ".csv");
            _writer = new StreamWriter(_path, false, new UTF8Encoding(false), 1 << 15);
            _writer.WriteLine(Header);
            _lastFlush = Time.unscaledTime;
        }
        catch
        {
            _writer = null;
        }
    }

    private static void WriteRow(
        float tSong, bool changed, string anim,
        float trackTime, float animTime, float mix, float alpha, int loop, int complete,
        string anim1, float t1, float mix1,
        float posX, float posY, string body, float bodyX, float bodyY, string foot, float footY, float modelY, float skelX, float skelY, float sx, float sy,
        int hp, int hpDelta, int combo, int comboDelta, int fever, float isAir, float isJumping, float isAirPressing, float girlPosY, string ev)
    {
        var w = _writer;
        w.Write(F(tSong, 4));
        w.Write(',');
        w.Write(changed ? "1" : "0");
        w.Write(',');
        w.Write(anim);
        w.Write(',');
        w.Write(F(trackTime, 4));
        w.Write(',');
        w.Write(F(animTime, 4));
        w.Write(',');
        w.Write(F(mix, 4));
        w.Write(',');
        w.Write(F(alpha, 3));
        w.Write(',');
        w.Write(loop);
        w.Write(',');
        w.Write(complete);
        w.Write(',');
        w.Write(anim1 ?? "");
        w.Write(',');
        w.Write(F(t1, 4));
        w.Write(',');
        w.Write(F(mix1, 4));
        w.Write(',');
        w.Write(F(posX, 4));
        w.Write(',');
        w.Write(F(posY, 4));
        w.Write(',');
        w.Write(body ?? "");
        w.Write(',');
        w.Write(F(bodyX, 4));
        w.Write(',');
        w.Write(F(bodyY, 4));
        w.Write(',');
        w.Write(foot ?? "");
        w.Write(',');
        w.Write(F(footY, 4));
        w.Write(',');
        w.Write(F(modelY, 4));
        w.Write(',');
        w.Write(F(skelX, 4));
        w.Write(',');
        w.Write(F(skelY, 4));
        w.Write(',');
        w.Write(F(sx, 4));
        w.Write(',');
        w.Write(F(sy, 4));
        w.Write(',');
        w.Write(hp);
        w.Write(',');
        w.Write(hpDelta);
        w.Write(',');
        w.Write(combo);
        w.Write(',');
        w.Write(comboDelta);
        w.Write(',');
        w.Write(fever);
        w.Write(',');
        w.Write(F(isAir, 0));
        w.Write(',');
        w.Write(F(isJumping, 0));
        w.Write(',');
        w.Write(F(isAirPressing, 0));
        w.Write(',');
        w.Write(F(girlPosY, 4));
        w.Write(',');
        w.WriteLine(ev);
    }

    private static string F(float v, int d)
    {
        if (float.IsNaN(v) || float.IsInfinity(v))
            return "";
        return d == 3 ? v.ToString("F3", Inv) : v.ToString("F4", Inv);
    }

    private static BaseEnemyObjectController FindGirl()
    {
        if (Time.unscaledTime - _lastScan < 0.25f && _girl != null)
            return _girl;
        _lastScan = Time.unscaledTime;
        _girl = null;
        _body = null;
        _bodyName = "";
        _foot = null;
        _footName = "";
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

    private static void ReadTracks(
        BaseEnemyObjectController girl,
        ref string anim, ref float trackTime, ref float animTime, ref float mix, ref float alpha,
        ref int loop, ref int complete,
        ref string anim1, ref float t1, ref float mix1)
    {
        try
        {
            var sa = girl.m_SkeletonAnimation;
            var state = sa != null ? sa.AnimationState : null;
            if (state == null)
                return;
            var te0 = state.GetCurrent(0);
            if (te0 != null)
            {
                anim = te0.Animation?.Name ?? "";
                trackTime = te0.TrackTime;
                animTime = te0.AnimationTime;
                loop = te0.Loop ? 1 : 0;
                complete = te0.IsComplete ? 1 : 0;
            }
            var te1 = state.GetCurrent(1);
            if (te1 != null && te1.Animation != null)
            {
                anim1 = te1.Animation.Name ?? "";
                t1 = te1.TrackTime;
            }
        }
        catch { }
    }

    private static void ReadBody(BaseEnemyObjectController girl, ref float x, ref float y)
    {
        try
        {
            if (_body == null)
                BindBody(girl);
            if (_body == null)
                return;
            x = _body.WorldX;
            y = _body.WorldY;
        }
        catch
        {
            _body = null;
        }
    }

    private static void ReadFoot(BaseEnemyObjectController girl, ref float y)
    {
        try
        {
            if (_foot == null)
                BindFoot(girl);
            if (_foot == null)
                return;
            y = _foot.WorldY;
        }
        catch
        {
            _foot = null;
        }
    }

    private static void BindBody(BaseEnemyObjectController girl)
    {
        _body = null;
        _bodyName = "";
        try
        {
            var bones = girl.m_SkeletonAnimation?.Skeleton?.Bones;
            if (bones == null || bones.Count == 0)
                return;
            Bone pick = null;
            string pickName = "";
            for (int i = 0; i < bones.Count; i++)
            {
                var b = bones.Items[i];
                if (b == null)
                    continue;
                string n = "";
                try { n = b.Data?.Name ?? ""; } catch { }
                if (n.IndexOf("body", StringComparison.OrdinalIgnoreCase) >= 0 ||
                    n.IndexOf("hip", StringComparison.OrdinalIgnoreCase) >= 0 ||
                    n.IndexOf("torso", StringComparison.OrdinalIgnoreCase) >= 0)
                {
                    pick = b;
                    pickName = n;
                    break;
                }
            }
            if (pick == null)
            {
                pick = bones.Count > 1 ? bones.Items[1] : bones.Items[0];
                try { pickName = pick?.Data?.Name ?? ""; } catch { }
            }
            _body = pick;
            _bodyName = pickName;
        }
        catch { }
    }

    private static void BindFoot(BaseEnemyObjectController girl)
    {
        _foot = null;
        _footName = "";
        try
        {
            var bones = girl.m_SkeletonAnimation?.Skeleton?.Bones;
            if (bones == null || bones.Count == 0)
                return;
            Bone pick = null;
            string pickName = "";
            // sleepy_girl 骨骼名是 root / root_2 / X / shadow。身体是 X（跳跃时它动），
            // 优先找 X / body / hip，找不到就用 WorldY 最高的骨头（身体最高点）。
            for (int i = 0; i < bones.Count; i++)
            {
                var b = bones.Items[i];
                if (b == null)
                    continue;
                string n = "";
                try { n = b.Data?.Name ?? ""; } catch { }
                if (n.Equals("X", StringComparison.OrdinalIgnoreCase) ||
                    n.IndexOf("body", StringComparison.OrdinalIgnoreCase) >= 0 ||
                    n.IndexOf("hip", StringComparison.OrdinalIgnoreCase) >= 0)
                {
                    pick = b;
                    pickName = n;
                    break;
                }
            }
            if (pick == null)
            {
                float highest = float.MinValue;
                for (int i = 0; i < bones.Count; i++)
                {
                    var b = bones.Items[i];
                    if (b == null)
                        continue;
                    float wy;
                    try { wy = b.WorldY; } catch { continue; }
                    if (wy > highest)
                    {
                        highest = wy;
                        pick = b;
                        try { pickName = b.Data?.Name ?? ""; } catch { }
                    }
                }
            }
            _foot = pick;
            _footName = pickName;
        }
        catch { }
    }

    private static void DumpBones(BaseEnemyObjectController girl)
    {
        if (_bonesDumped)
            return;
        _bonesDumped = true;
        try
        {
            var bones = girl.m_SkeletonAnimation?.Skeleton?.Bones;
            if (bones == null || bones.Count == 0)
                return;
            var sb = new StringBuilder();
            sb.AppendLine("bone_count=" + bones.Count);
            for (int i = 0; i < bones.Count; i++)
            {
                var b = bones.Items[i];
                if (b == null)
                    continue;
                string n = "";
                try { n = b.Data?.Name ?? ""; } catch { }
                float wx = float.NaN, wy = float.NaN, x = float.NaN, y = float.NaN;
                try { wx = b.WorldX; wy = b.WorldY; x = b.X; y = b.Y; } catch { }
                sb.AppendLine(i + "|" + n + "|WorldY=" + wy.ToString("F4", Inv) +
                    "|WorldX=" + wx.ToString("F4", Inv) + "|Y=" + y.ToString("F4", Inv) + "|X=" + x.ToString("F4", Inv));
            }
            File.WriteAllText(Path.Combine(Dir, "bones_dump.txt"), sb.ToString(), new UTF8Encoding(false));
        }
        catch { }
    }

    private static void DumpChildren(BaseEnemyObjectController girl)
    {
        if (_childrenDumped)
            return;
        _childrenDumped = true;
        try
        {
            var sb = new StringBuilder();
            DumpTransform(sb, girl.transform, 0, 5);
            var sa = girl.m_SkeletonAnimation;
            if (sa != null && sa.transform != girl.transform)
            {
                sb.AppendLine("--- m_SkeletonAnimation.transform ---");
                DumpTransform(sb, sa.transform, 0, 5);
            }
            File.WriteAllText(Path.Combine(Dir, "children_dump.txt"), sb.ToString(), new UTF8Encoding(false));
        }
        catch { }
    }

    private static void DumpTransform(StringBuilder sb, UnityEngine.Transform t, int depth, int maxDepth)
    {
        if (t == null || depth > maxDepth)
            return;
        var lp = t.localPosition;
        var p = t.position;
        sb.AppendLine(new string(' ', depth * 2) + depth + "|" + t.gameObject.name +
            "|localY=" + lp.y.ToString("F4", Inv) +
            "|localX=" + lp.x.ToString("F4", Inv) +
            "|localZ=" + lp.z.ToString("F4", Inv) +
            "|worldY=" + p.y.ToString("F4", Inv));
        for (int i = 0; i < t.childCount; i++)
        {
            var c = t.GetChild(i);
            DumpTransform(sb, c, depth + 1, maxDepth);
        }
    }

    private static int ReadInt(object obj, params string[] names)
    {
        if (obj == null)
            return -1;
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
                    if (v is float f) return (int)f;
                    if (v is bool b) return b ? 1 : 0;
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
                    if (v is float fl) return (int)fl;
                    if (v != null && int.TryParse(v.ToString(), out int n)) return n;
                }
            }
            catch { }
        }
        return -1;
    }

    private static bool ReadBool(object obj, params string[] names)
    {
        if (obj == null)
            return false;
        var t = obj.GetType();
        foreach (var name in names)
        {
            try
            {
                var p = t.GetProperty(name, BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);
                if (p != null)
                {
                    var v = p.GetValue(obj);
                    if (v is bool b) return b;
                    if (v is int i) return i != 0;
                }
            }
            catch { }
        }
        return false;
    }
}
