using System.Diagnostics;
using System.Globalization;
using System.Security.Cryptography;
using System.Text.Json;
using System.Text.RegularExpressions;
using UnityEngine;

namespace MDRoleRecord;

/// <summary>Prepares chart actions off the game thread while a song is selected.</summary>
internal static class ChartActionCache
{
    private const int CacheVersion = 1; // Keep in sync with action_cache_generator.py.
    private static readonly string Root = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "MuseDashChart");
    private static readonly string TraceDir = Path.Combine(Root, "role_traces");
    private static readonly string ClockFile = Path.Combine(Root, "battle_clock.json");
    private static readonly string SettingsFile = Path.Combine(TraceDir, "generator_settings.json");
    internal static readonly string ManualFile = Path.Combine(TraceDir, "virtual_actions.txt");

    internal readonly record struct Selection(string Uid, int Map, string StageMap, string Name, string Level, bool InGame)
    {
        internal string CanonicalMap => Uid + "_map" + Map.ToString(CultureInfo.InvariantCulture);
        internal bool IsVariant => !string.Equals(StageMap, CanonicalMap, StringComparison.Ordinal);
        internal string Key => Uid + "/map" + Map.ToString(CultureInfo.InvariantCulture)
            + (IsVariant ? "/variants/" + StageMap : "");
    }

    private static Process _worker;
    private static string _workerKey = "";
    private static string _workerUid = "";
    private static string _selectedKey = "";
    private static string _lastAttemptKey = "";
    private static string _retiredKey = "";
    private static string _statusFile = "";
    private static readonly HashSet<string> VerifiedThisRun = new();
    private static float _selectedAt;
    private static float _lastAttemptAt;
    private static float _nextPoll;
    private static bool _settingsWarningShown;

    internal static bool TryReadSelection(out Selection selection)
    {
        selection = default;
        try
        {
            if (!File.Exists(ClockFile)
                || DateTime.UtcNow - File.GetLastWriteTimeUtc(ClockFile) > TimeSpan.FromSeconds(2))
                return false;
            using var document = JsonDocument.Parse(File.ReadAllText(ClockFile));
            var root = document.RootElement;
            string uid = root.GetProperty("music_uid").GetString()?.Trim().ToLowerInvariant() ?? "";
            int map = root.GetProperty("difficulty").GetInt32();
            if (!Regex.IsMatch(uid, "^[a-z0-9_-]{1,100}$") || map < 1 || map > 6)
                return false;
            string name = root.TryGetProperty("music_name", out var n) ? n.GetString() ?? "" : "";
            string level = root.TryGetProperty("level", out var l) ? l.GetString() ?? "" : "";
            bool inGame = root.TryGetProperty("in_game", out var g) && g.GetBoolean();
            string stageMap = root.TryGetProperty("stage_map", out var s)
                ? s.GetString()?.Trim().ToLowerInvariant() ?? "" : "";
            if (stageMap.Length == 0) return false;
            if (!Regex.IsMatch(stageMap, "^[a-z0-9_-]{1,120}_map[1-6]$")
                || !stageMap.EndsWith("_map" + map.ToString(CultureInfo.InvariantCulture), StringComparison.Ordinal)
                || (stageMap != uid + "_map" + map.ToString(CultureInfo.InvariantCulture)
                    && !stageMap.StartsWith(uid + "_", StringComparison.Ordinal)))
                return false;
            selection = new Selection(uid, map, stageMap, name, level, inGame);
            return true;
        }
        catch { return false; }
    }

    internal static void Tick()
    {
        if (Time.unscaledTime < _nextPoll) return;
        _nextPoll = Time.unscaledTime + 0.25f;
        FinishWorker();
        if (!TryReadSelection(out var selection) || selection.InGame)
            return;
        if (selection.Key != _selectedKey)
        {
            if (_retiredKey.Length > 0 && _retiredKey != selection.Key)
            {
                _retiredKey = "";
                VirtualInputReplay.ForgetRetained();
            }
            _selectedKey = selection.Key;
            _selectedAt = Time.unscaledTime;
            return;
        }
        // A completed chart is held in memory for an immediate replay. Do not
        // recreate its just-deleted disk cache while the same song stays selected.
        if (_retiredKey == selection.Key) return;
        if (Time.unscaledTime - _selectedAt < 1.0f || _worker != null)
            return;
        // 同曲可以同时有 map4/map5/map6。即使当前 map 有手工表，仍准备其它里谱。
        if (HasMatchingManual(selection) && !IsHidden(selection)) return;
        if (VerifiedThisRun.Contains(selection.Key)
            && TryGetCachedFile(selection, out _, out _)) return;
        if (_lastAttemptKey == selection.Key && Time.unscaledTime - _lastAttemptAt < 30f)
            return;
        StartWorker(selection);
    }

    internal static bool TrySelectFile(Selection selection, out string path, out string error)
    {
        if (HasMatchingManual(selection))
        {
            path = ManualFile;
            error = "";
            return true;
        }
        FinishWorker();
        if (!VerifiedThisRun.Contains(selection.Key))
        {
            path = "";
            error = "当前曲目的缓存尚未在本次启动中验证；请在选曲界面等待生成完成";
            return false;
        }
        return TryGetCachedFile(selection, out path, out error);
    }

    private static bool HasMatchingManual(Selection selection)
    {
        try
        {
            if (!File.Exists(ManualFile)) return false;
            string uid = "";
            string map = "";
            string stageMap = "";
            foreach (string line in File.ReadLines(ManualFile).Take(8))
            {
                if (line.StartsWith("# chart_uid=", StringComparison.OrdinalIgnoreCase))
                    uid = line.Substring("# chart_uid=".Length).Trim();
                else if (line.StartsWith("# map_index=", StringComparison.OrdinalIgnoreCase))
                    map = line.Substring("# map_index=".Length).Trim();
                else if (line.StartsWith("# stage_map=", StringComparison.OrdinalIgnoreCase))
                    stageMap = line.Substring("# stage_map=".Length).Trim();
            }
            return string.Equals(uid, selection.Uid, StringComparison.OrdinalIgnoreCase)
                && map == selection.Map.ToString(CultureInfo.InvariantCulture)
                && (stageMap.Length == 0 ? !selection.IsVariant
                    : string.Equals(stageMap, selection.StageMap, StringComparison.OrdinalIgnoreCase));
        }
        catch { return false; }
    }

    internal static bool IsRetired(Selection selection) => _retiredKey == selection.Key;

    internal static bool RetireCompleted(Selection selection)
    {
        if (_retiredKey == selection.Key) return true;
        if (HasMatchingManual(selection)) return false;
        FinishWorker();
        if (_worker != null && _workerKey == selection.Key) return false;
        if (!TryGetCachedFile(selection, out string path, out _)) return false;
        string manifest = Path.Combine(Path.GetDirectoryName(path)!, "manifest.json");
        try
        {
            using (var document = JsonDocument.Parse(File.ReadAllText(manifest)))
            {
                if (document.RootElement.TryGetProperty("manual_override", out var pinned)
                    && pinned.ValueKind == JsonValueKind.True)
                    return false;
            }
            File.Delete(path);
            if (File.Exists(path))
                return false;
            try { File.Delete(manifest); }
            catch (Exception ex)
            {
                // The action file is gone; retain the in-memory copy for replay.
                MelonLoader.MelonLogger.Warning("[ChartCache] 动作表已移除，清理 manifest 失败: " + ex.Message);
            }
            VerifiedThisRun.Remove(selection.Key);
            _retiredKey = selection.Key;
            MelonLoader.MelonLogger.Msg("[ChartCache] 曲目结算后已移除自动动作表: " + selection.Key);
            return true;
        }
        catch (Exception ex)
        {
            MelonLoader.MelonLogger.Warning("[ChartCache] 结算后清理动作表失败: " + ex.Message);
            return false;
        }
    }

    private static bool IsHidden(Selection selection)
    {
        string level = selection.Level.Trim();
        return selection.Map >= 4 || level is "?" or "？" or "¿" or "‽" or "里";
    }

    private static bool TryGetCachedFile(Selection selection, out string path, out string error)
    {
        path = "";
        if (!TryGetCacheDir(out string cacheDir, out error)) return false;
        string directory = Path.Combine(cacheDir, selection.Uid, "map" + selection.Map);
        if (selection.IsVariant) directory = Path.Combine(directory, "variants", selection.StageMap);
        path = Path.Combine(directory, "virtual_actions.txt");
        string manifest = Path.Combine(Path.GetDirectoryName(path)!, "manifest.json");
        error = "尚未生成当前曲目的动作表";
        if (!File.Exists(path) || !File.Exists(manifest)) return false;
        try
        {
            using var document = JsonDocument.Parse(File.ReadAllText(manifest));
            var root = document.RootElement;
            if (root.GetProperty("cache_version").GetInt32() != CacheVersion
                || !string.Equals(root.GetProperty("chart_uid").GetString(), selection.Uid, StringComparison.Ordinal)
                || root.GetProperty("map_index").GetInt32() != selection.Map
                || (root.TryGetProperty("stage_map", out var stage)
                    ? !string.Equals(stage.GetString(), selection.StageMap, StringComparison.OrdinalIgnoreCase)
                    : selection.IsVariant)
                || root.GetProperty("action_count").GetInt32() <= 0
                || string.IsNullOrWhiteSpace(root.GetProperty("rule_hash").GetString()))
                throw new InvalidDataException("缓存版本或谱面身份不匹配");
            foreach (var source in root.GetProperty("sources").EnumerateArray())
            {
                string sourcePath = source.GetProperty("path").GetString() ?? "";
                var info = new FileInfo(sourcePath);
                long mtime = new DateTimeOffset(info.LastWriteTimeUtc).ToUnixTimeMilliseconds();
                if (!info.Exists || info.Length != source.GetProperty("length").GetInt64()
                    || mtime != source.GetProperty("mtime_ms").GetInt64())
                    throw new InvalidDataException("官包或 notedata 已更新");
            }
            string actualHash = Convert.ToHexString(SHA256.HashData(File.ReadAllBytes(path)));
            if (!string.Equals(actualHash, root.GetProperty("sha256").GetString(), StringComparison.OrdinalIgnoreCase))
                throw new InvalidDataException("动作表与缓存校验值不符");
            error = "";
            return true;
        }
        catch (Exception ex)
        {
            error = "动作表缓存无效: " + ex.Message;
            return false;
        }
    }

    private static bool TryGetCacheDir(out string cacheDir, out string error)
    {
        cacheDir = "";
        error = "";
        try
        {
            using var document = JsonDocument.Parse(File.ReadAllText(SettingsFile));
            string configured = document.RootElement.GetProperty("cache_root").GetString() ?? "";
            if (!Path.IsPathFullyQualified(configured))
                throw new InvalidDataException("cache_root 必须是绝对路径");
            cacheDir = Path.GetFullPath(configured);
            return true;
        }
        catch (Exception ex)
        {
            error = "无法读取动作表缓存目录: " + ex.Message;
            return false;
        }
    }

    private static void StartWorker(Selection selection)
    {
        _lastAttemptKey = selection.Key;
        _lastAttemptAt = Time.unscaledTime;
        try
        {
            if (!File.Exists(SettingsFile))
            {
                if (!_settingsWarningShown)
                {
                    MelonLoader.MelonLogger.Warning("[ChartCache] 缺少 generator_settings.json，仍可使用手工动作表");
                    _settingsWarningShown = true;
                }
                return;
            }
            using var document = JsonDocument.Parse(File.ReadAllText(SettingsFile));
            var root = document.RootElement;
            if (root.TryGetProperty("enabled", out var enabled) && !enabled.GetBoolean()) return;
            string python = root.GetProperty("python_executable").GetString() ?? "";
            string script = root.GetProperty("generator_script").GetString() ?? "";
            string config = root.TryGetProperty("config_path", out var c) ? c.GetString() ?? "" : "";
            if (!TryGetCacheDir(out string cacheDir, out string cacheError))
                throw new InvalidDataException(cacheError);
            if (!File.Exists(python) || !File.Exists(script) || (config.Length > 0 && !File.Exists(config)))
                throw new FileNotFoundException("Python、生成脚本或配置文件不存在");
            Directory.CreateDirectory(cacheDir);
            _statusFile = Path.Combine(cacheDir, "generation_status.json");
            File.Delete(_statusFile);
            var start = new ProcessStartInfo(python) { UseShellExecute = false, CreateNoWindow = true };
            start.ArgumentList.Add(script);
            start.ArgumentList.Add("--uid"); start.ArgumentList.Add(selection.Uid);
            start.ArgumentList.Add("--map"); start.ArgumentList.Add(selection.Map.ToString(CultureInfo.InvariantCulture));
            start.ArgumentList.Add("--stage-map"); start.ArgumentList.Add(selection.StageMap);
            start.ArgumentList.Add("--name"); start.ArgumentList.Add(selection.Name);
            start.ArgumentList.Add("--level"); start.ArgumentList.Add(selection.Level);
            if (IsHidden(selection)) start.ArgumentList.Add("--all-hidden");
            start.ArgumentList.Add("--cache-root"); start.ArgumentList.Add(cacheDir);
            start.ArgumentList.Add("--status"); start.ArgumentList.Add(_statusFile);
            if (config.Length > 0)
            {
                start.ArgumentList.Add("--config"); start.ArgumentList.Add(config);
            }
            _worker = Process.Start(start) ?? throw new InvalidOperationException("Python 进程未启动");
            _workerKey = selection.Key;
            _workerUid = selection.Uid;
            MelonLoader.MelonLogger.Msg("[ChartCache] 选曲预生成: " + _workerKey);
        }
        catch (Exception ex)
        {
            MelonLoader.MelonLogger.Warning("[ChartCache] 无法启动生成器: " + ex.Message);
        }
    }

    private static void FinishWorker()
    {
        if (_worker == null || !_worker.HasExited) return;
        try
        {
            if (_worker.ExitCode == 0)
            {
                VerifiedThisRun.Add(_workerKey);
                if (File.Exists(_statusFile))
                {
                    using var document = JsonDocument.Parse(File.ReadAllText(_statusFile));
                    if (document.RootElement.TryGetProperty("keys", out var keys)
                        && keys.ValueKind == JsonValueKind.Array)
                    {
                        foreach (var key in keys.EnumerateArray())
                        {
                            string value = key.GetString() ?? "";
                            if (value.StartsWith(_workerUid + "/map", StringComparison.Ordinal)
                                && value.Length < 250)
                                VerifiedThisRun.Add(value);
                        }
                    }
                }
                MelonLoader.MelonLogger.Msg("[ChartCache] 动作表已生成: " + _workerKey);
            }
            else
            {
                string detail = "Python 退出码 " + _worker.ExitCode;
                if (File.Exists(_statusFile))
                {
                    using var document = JsonDocument.Parse(File.ReadAllText(_statusFile));
                    detail = document.RootElement.GetProperty("error").GetString() ?? detail;
                }
                MelonLoader.MelonLogger.Warning("[ChartCache] 生成失败 " + _workerKey + ": " + detail);
            }
        }
        catch (Exception ex)
        {
            MelonLoader.MelonLogger.Warning("[ChartCache] 读取生成结果失败: " + ex.Message);
        }
        finally
        {
            _worker.Dispose();
            _worker = null;
        }
    }

    internal static void Close()
    {
        try
        {
            if (_worker != null && !_worker.HasExited) _worker.Kill(entireProcessTree: true);
            _worker?.Dispose();
        }
        catch { }
        _worker = null;
    }
}
