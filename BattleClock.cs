using System.Text.RegularExpressions;
using Il2CppAssets.Scripts.Database;
using Il2CppFormulaBase;
using UnityEngine;
using UnityEngine.SceneManagement;

namespace MDRoleRecord;

internal static class BattleClock
{
    private static float _lastWrite = -1f;
    private static float _lastScan = -1f;
    private static bool _hitpoint;
    private static bool _sceneLayer;
    private static string _unityScene = "";
    private static string _gameScene = "";
    private static string _musicUid = "";
    private static string _musicName = "";
    private static string _stageMap = "";
    private static int _difficulty;
    private static string _level = "";
    private static string _lastChartReport = "";

    private static readonly string Dir = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
        "MuseDashChart");
    private static readonly string FilePath = Path.Combine(Dir, "battle_clock.json");

    internal static void Write()
    {
        if (Time.unscaledTime - _lastWrite < 0.03f)
            return;
        _lastWrite = Time.unscaledTime;
        try
        {
            bool inGame = false;
            float tSong = 0f;
            var sb = StageBattleComponent.instance;
            if (sb != null)
            {
                inGame = sb.isInGame;
                try { tSong = sb.timeFromMusicStart; } catch { }
                try
                {
                    var n = sb.GetSceneName();
                    if (!string.IsNullOrWhiteSpace(n)) _gameScene = n;
                }
                catch { }
            }
            if (Time.unscaledTime - _lastScan > 0.12f)
            {
                _lastScan = Time.unscaledTime;
                ScanScene();
            }

            bool gameMain = string.Equals(_unityScene, "GameMain", StringComparison.Ordinal);
            bool sceneReady = inGame && gameMain && (_hitpoint || _sceneLayer);
            ReadSong(inGame && gameMain);

            Directory.CreateDirectory(Dir);
            var tmp = FilePath + ".tmp";
            string Esc(string s) => (s ?? "").Replace("\\", "\\\\").Replace("\"", "\\\"");
            File.WriteAllText(tmp,
                "{\"in_game\":" + (inGame ? "true" : "false") +
                ",\"t_song\":" + tSong.ToString("G9", System.Globalization.CultureInfo.InvariantCulture) +
                ",\"scene_ready\":" + (sceneReady ? "true" : "false") +
                ",\"unity_scene\":\"" + Esc(_unityScene) + "\"" +
                ",\"game_scene\":\"" + Esc(_gameScene) + "\"" +
                ",\"hitpoint\":" + (_hitpoint ? "true" : "false") +
                ",\"scene_objs\":" + (_sceneLayer ? "true" : "false") +
                ",\"music_uid\":\"" + Esc(_musicUid) + "\"" +
                ",\"music_name\":\"" + Esc(_musicName) + "\"" +
                ",\"stage_map\":\"" + Esc(_stageMap) + "\"" +
                ",\"difficulty\":" + _difficulty.ToString(System.Globalization.CultureInfo.InvariantCulture) +
                ",\"level\":\"" + Esc(_level) + "\"" +
                "}");
            File.Copy(tmp, FilePath, true);
            File.Delete(tmp);
        }
        catch { }
    }

    private static void ReadSong(bool chartLoaded)
    {
        try
        {
            var battle = GlobalDataBase.dbBattleStage;
            if (battle == null)
                return;
            string stageMap = Clean(battle.selectedStageMapName);
            string uid = stageMap;
            if (string.IsNullOrEmpty(uid))
                uid = Clean(battle.musicUid);
            string title = Clean(battle.selectedMusicName);
            string level = Clean(battle.selectMusicLevelStr);
            var music = battle.selectedMusicInfo;
            if (music != null)
            {
                string map = Clean(music.mapName);
                if (!string.IsNullOrEmpty(map))
                    uid = map;
                if (string.IsNullOrEmpty(title))
                    title = Clean(music.name);
            }

            int selected = battle.selectedDifficulty;
            int diff = selected >= 1 && selected <= 6 ? selected : 0;
            int byLevel = LevelIndex(music, level);
            if (byLevel >= 1)
                diff = byLevel;
            // 倒问号是里谱。按钮序号常停在 3，先用难度文案推断。
            int hidden = HiddenIndex(music, level, selected);
            if (hidden >= 4)
                diff = hidden;
            else if (IsHiddenMark(level) && diff < 4)
                diff = 4; // map6-only 等情况先作为里谱候选，开局再由谱面名校正。

            string key = SongKey(uid);
            int selectedMap = MapNumber(stageMap);
            // 选曲时 selectedStageMapName 已给出实际谱面时，以它区分同曲的
            // map4/map5/map6；重复的「¿」文案本身无法区分这些谱面。
            if (selectedMap >= 4 && SongKey(stageMap) == key
                && (IsHiddenMark(level) || selected >= 4))
                diff = selectedMap;

            string infoMap = "";
            try
            {
                var stage = GlobalDataBase.dbStageInfo;
                if (stage != null)
                    infoMap = Clean(stage.mapName);
            }
            catch { }
            int named = SongKey(infoMap) == key ? MapNumber(infoMap) : 0;
            if (named == 0)
                named = SongKey(stageMap) == key ? selectedMap : 0;
            bool sameSong = string.IsNullOrEmpty(key)
                || SongKey(infoMap) == key
                || SongKey(stageMap) == key;
            // 谱面载入后，文件名里的 mapN 才是正在打的那一张。选曲界面上的旧序号不用。
            // 里谱的 StageInfo 常把 difficulty 写成 2 或 3，不能把 ¿ 改回 map2/map3。
            if (chartLoaded && sameSong && named >= 1 && !(IsHiddenMark(level) && diff >= 4 && named < 4))
                diff = named;

            if (chartLoaded)
            {
                string report = key + "|" + selected + "|" + stageMap + "|" + infoMap + "|" + diff;
                if (report != _lastChartReport)
                {
                    _lastChartReport = report;
                    MelonLoader.MelonLogger.Msg("[BattleClock] 对局谱面: uid=" + key
                        + " selected=" + selected + " level=" + level
                        + " selectedStage=" + stageMap + " stageInfo=" + infoMap
                        + " musicInfo=" + Clean(music?.mapName) + " resolved=map" + diff);
                }
            }
            else
                _lastChartReport = "";

            if (string.IsNullOrEmpty(key))
                key = SongKey(Clean(battle.musicUid));
            if (!string.IsNullOrEmpty(key))
                _musicUid = key;
            _stageMap = !string.IsNullOrEmpty(stageMap) && SongKey(stageMap) == key
                ? stageMap.ToLowerInvariant() : "";
            if (!string.IsNullOrEmpty(title))
                _musicName = title;
            if (diff >= 1 && diff <= 6)
                _difficulty = diff;
            if (!string.IsNullOrEmpty(level))
                _level = level;
        }
        catch { }
    }

    private static int MapNumber(string value)
    {
        var match = Regex.Match(value ?? "", @"_map(\d)$", RegexOptions.IgnoreCase);
        if (!match.Success)
            return 0;
        if (!int.TryParse(match.Groups[1].Value, out int n))
            return 0;
        return n >= 1 && n <= 6 ? n : 0;
    }

    private static string SongKey(string value)
    {
        value = Clean(value);
        if (string.IsNullOrEmpty(value))
            return "";
        string key = Regex.Replace(value, @"_map\d*$", "", RegexOptions.IgnoreCase);
        // 某些官谱对象名是 *_default_mapN，官包却是 music_<uid>.bundle。
        // 仅在 _default 官包不存在且去尾缀的官包存在时合并，避免误合并真正的曲目。
        if (key.EndsWith("_default", StringComparison.OrdinalIgnoreCase))
        {
            string plain = key.Substring(0, key.Length - "_default".Length);
            string bundles = Path.Combine(Application.dataPath, "StreamingAssets", "aa", "StandaloneWindows64");
            if (!File.Exists(Path.Combine(bundles, "music_" + key + "_assets_all.bundle"))
                && File.Exists(Path.Combine(bundles, "music_" + plain + "_assets_all.bundle")))
                return plain;
        }
        return key;
    }

    private static bool IsHiddenMark(string level)
    {
        level = Clean(level);
        return level is "?" or "？" or "¿" or "‽" or "里";
    }

    private static string CanonLevel(string level)
    {
        level = Clean(level);
        return IsHiddenMark(level) ? "?" : level;
    }

    /// <summary>
    /// 按钮上的等级文案对上 MusicInfo 提供的 difficulty1–5。
    /// 「〇」是特殊大触；「¿ / ?」是里谱，map6 由谱面名或按钮序号识别。
    /// </summary>
    private static int LevelIndex(MusicInfo music, string level)
    {
        if (music == null || string.IsNullOrEmpty(level) || level == "0")
            return 0;
        string want = CanonLevel(level);
        string[] slots =
        {
            Clean(music.difficulty1),
            Clean(music.difficulty2),
            Clean(music.difficulty3),
            Clean(music.difficulty4),
            Clean(music.difficulty5),
        };
        int hit = 0;
        int count = 0;
        for (int i = 0; i < slots.Length; i++)
        {
            if (string.IsNullOrEmpty(slots[i]) || slots[i] == "0")
                continue;
            if (CanonLevel(slots[i]) != want)
                continue;
            hit = i + 1;
            count++;
        }
        if (count == 1)
            return hit;
        if (count > 1 && want == "?")
        {
            for (int i = slots.Length - 1; i >= 3; i--)
            {
                if (CanonLevel(slots[i]) == "?")
                    return i + 1;
            }
        }
        return 0;
    }

    /// <summary>
    /// 里谱等级通常在 difficulty4/5。选中文案对得上时，selectedDifficulty 可能仍停在 3。
    /// </summary>
    private static int HiddenIndex(MusicInfo music, string level, int selected)
    {
        if (selected >= 4 && selected <= 6)
            return selected;
        if (music == null || string.IsNullOrEmpty(level) || level == "0")
            return 0;
        string d1 = Clean(music.difficulty1);
        string d2 = Clean(music.difficulty2);
        string d3 = Clean(music.difficulty3);
        string d4 = Clean(music.difficulty4);
        string d5 = Clean(music.difficulty5);
        bool live(string s) => !string.IsNullOrEmpty(s) && s != "0";
        string own = selected switch
        {
            1 => d1,
            2 => d2,
            3 => d3,
            _ => ""
        };
        // 按钮上写着萌新/高手/大触，等级对上 difficulty1–3，就留在这一档。
        if (selected >= 1 && selected <= 3 && live(own) && CanonLevel(level) == CanonLevel(own) && !IsHiddenMark(level))
            return 0;
        if (IsHiddenMark(level))
        {
            if (live(d5) && !live(d4))
                return 5;
            if (live(d4))
                return 4;
            if (live(d5))
                return 5;
        }
        if (live(d5) && level == d5 && level != d4 && level != d3 && level != d2 && level != d1)
            return 5;
        if (live(d4) && level == d4 && level != d3 && level != d2 && level != d1)
            return 4;
        return 0;
    }

    private static string Clean(string value) => value?.Trim() ?? "";

    private static void ScanScene()
    {
        _hitpoint = false;
        _sceneLayer = false;
        _unityScene = "";
        try
        {
            var scene = SceneManager.GetActiveScene();
            _unityScene = scene.name ?? "";
            int n = SceneManager.sceneCount;
            for (int s = 0; s < n; s++)
            {
                var sc = SceneManager.GetSceneAt(s);
                if (!sc.isLoaded) continue;
                var roots = sc.GetRootGameObjects();
                if (roots == null) continue;
                foreach (var go in roots)
                {
                    if (go == null) continue;
                    Walk(go.transform, 0);
                    if (_hitpoint && _sceneLayer) return;
                }
            }
        }
        catch { }
    }

    private static void Walk(Transform t, int depth)
    {
        if (t == null || depth > 12) return;
        string name = t.name ?? "";
        if (name.StartsWith("HitPoint", StringComparison.Ordinal))
            _hitpoint = true;
        if (name == "Near" || name == "Mid" || name == "Far" || name == "Bg"
            || name.StartsWith("scene_", StringComparison.OrdinalIgnoreCase)
            || name.StartsWith("Near", StringComparison.Ordinal)
            || name.StartsWith("Mid_", StringComparison.Ordinal))
            _sceneLayer = true;
        if (_hitpoint && _sceneLayer) return;
        for (int i = 0; i < t.childCount; i++)
            Walk(t.GetChild(i), depth + 1);
    }
}
