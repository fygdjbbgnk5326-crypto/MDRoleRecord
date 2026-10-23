# MDRoleRecord 谱面播放器项目解析

本项目解析 Muse Dash 官包或 MDM 谱面，生成供 `MDRoleRecord` 使用的虚拟触点动作表。当前 Mod 在选曲界面调用 `action_cache_generator.py`，进入对局时读取已校验的动作表，通过游戏的 `InputManager.GetButtonDown`、`GetButton`、`GetButtonUp` 提供输入。生成器本身不向游戏注入按键，也不启用官机 AutoPlay。

独立运行、手动分析和 Win32 `SendInput` 的旧脚本保存在原 `muse_dash_chart_only/unused_review_20261023/`，供人工检查后删除；Mod 当前只调用此目录的生成器。

## 当前流程

```text
游戏选曲
  → MDRoleRecord/BattleClock.cs 写出曲目 UID、map 序号、精确 stage_map
  → MDRoleRecord/ChartActionCache.cs 等待选曲稳定并启动 Python 生成器
  → 官包 + notedata → ChartIR → 齿轮/长条处理 → EventScheduler
  → virtual_actions.txt + manifest.json
  → 进入对局时校验身份、官包时间戳、动作表哈希和触点配对
  → MDRoleRecord/VirtualInputReplay.cs 按游戏歌曲时钟送出虚拟触点
```

Mod 内置了原 MDChartClock 的时钟采集逻辑，写入 `%LOCALAPPDATA%\MuseDashChart\battle_clock.json`。独立的 `MDChartClock.dll` 不需要同时安装。游戏菜单里的官机 AutoPlay 应保持关闭；若 Mod 无法确认其状态或动作表身份，虚拟输入不会启动。

### 谱面身份与 `_default`

仅凭 `曲目 UID + map 序号` 不足以识别某些官包。同一官包可能同时包含 `echo_over_you_map2` 和 `echo_over_you_default_map2`，两者在游戏里都是 map2，但节点不同。Mod 现在把游戏的 `selectedStageMapName` 作为 `stage_map` 传给生成器：

| 游戏选中的对象 | 缓存位置，相对于 `MDRoleRecord/action_cache` |
| --- | --- |
| `echo_over_you_map2` | `echo_over_you/map2/` |
| `echo_over_you_default_map2` | `echo_over_you/map2/variants/echo_over_you_default_map2/` |

生成器按精确对象名解析谱面，并在动作表头和 `manifest.json` 中写入 `stage_map`。Mod 加载时再次核对该字段，防止原版与 `_default` 互相使用动作表。未标记 `stage_map` 的旧手工表只可匹配普通谱面，不能用于变体。

map4–6 里谱由选曲时的实际谱面名识别；生成器会检查官包中存在的里谱对象，不会假定每首歌都有 map4、map5 或 map6。

## 项目结构

| 路径 | 作用 |
| --- | --- |
| `action_cache_generator.py` | Mod 调用的缓存入口；选择官包、解析、导出、校验并发布 manifest |
| `chart/bundle.py` | 读取 `music_*.bundle` 和 notedata，将官包对象转为谱面事件 |
| `chart/parser.py` | 统一 `ChartIR`；解析 MDM、目录和 BMS；按 `stage_map` 选择官包对象 |
| `chart/library.py` | 根据 UID、曲名和可选词典查找官包 |
| `chart/dodge.py` | 根据齿轮和邻近物件规划躲避事件 |
| `chart/hold_hits.py` | 处理长条中的打击和收集物 |
| `scheduler/scheduler.py` | 将谱面事件转换为带时间、轨道和事件 ID 的成对触点边沿 |
| `config.py`、`config.example.json` | 生成器的配置和默认值 |
| `requirements.txt` | 生成器的 Python 依赖 |

Mod 的主要文件位于上一级目录：`BattleClock.cs` 发布选曲及歌曲时钟，`ChartActionCache.cs` 管理缓存，`VirtualInputReplay.cs` 加载并回放虚拟触点。更细的游戏端说明见 [VIRTUAL_INPUT.md](../VIRTUAL_INPUT.md)。

## 谱面解析规则

### 输入格式

- 官包：`music_<uid>_assets_all.bundle`；同目录需要 `config_others_assets_notedata_*.bundle`。使用 UnityPy 读取谱面对象，并以 notedata 补全物件类型。
- MDM：包含 `info.json` 与 `mapN.bms` 的 `.mdm`/`.zip` 或解压目录。BMS 通道经 `BMSParser` 转换为统一事件。
- `ChartIR` 保留事件时间、结束时间、轨道、`ibms_id`、物件类型和 map 序号；`raw_gears` 保留未规划前的齿轮事件，供后续时序规则使用。

官包解析遵循项目中的物件转换规则：保留可玩物件的原始 `NoteConfigData.ibms_id`；`0G`、`16`、`17` 连打类型分别处理；`0F` 长度为零的中间刻度不作为独立输入，短点长按位置处理；长条上的可玩物件仍保留。齿轮、长条、收集物和同刻节点会进入后续规划及调度。具体条件以 `chart/bundle.py`、`chart/dodge.py`、`chart/hold_hits.py` 和 `scheduler/scheduler.py` 为准。

齿轮与单击时序包含根据实测结果形成的规则；生成成功只表示格式和缓存校验通过，不能保证任意曲目都取得 Perfect 或零受击。新曲目应结合游戏判定日志验证。

## 安装与配置

### MelonLoader 与 Mod

先为 Muse Dash 安装适配该游戏 IL2CPP 版本的 MelonLoader，并至少启动游戏一次，让它生成 `MelonLoader/Il2CppAssemblies/`。将编译后的 `MDRoleRecord.dll` 放到游戏的 `Mods/` 目录。项目的 `MDRoleRecord.csproj` 需要游戏目录下的 `MelonLoader/net6/MelonLoader.dll`、`0Harmony.dll`、`Il2CppInterop.Runtime.dll`，以及 `MelonLoader/Il2CppAssemblies/` 中的游戏代理程序集；本地编译还需要 .NET 6 SDK。若游戏安装路径不同，构建时设置 `MuseDashPath`。

Mod 已包含时钟采集功能，游戏的 `Mods/` 中不要再同时放置 `MDChartClock.dll`。MelonLoader 负责加载 C# Mod；下面的 Python 依赖只供选曲时的谱面生成器使用，不能代替 MelonLoader。

### Python 生成器

建议使用 Python 3.14，并在此目录安装依赖：

```powershell
py -3.14 -m pip install -r .\requirements.txt
```

官包解析需要 `UnityPy`；`mutagen` 用于读取可用的音频时长。`action_cache_generator.py` 默认从 `MUSE_DASH_BUNDLE_DIR`、`MUSE_DASH_PATH` 或常见 Steam 安装位置找游戏官包；若游戏不在这些位置，可复制 `config.example.json` 为 `config.json`，修改 `bundle_dir`，再通过生成器的 `--config` 指定它。按 UID 直接找到官包时，不依赖外部曲名词典或额外 JSON 工具。

在上一级 `MDRoleRecord/` 目录运行以下命令，为 Mod 写入 Python 和缓存路径：

```powershell
powershell -ExecutionPolicy Bypass -File .\configure_generator.ps1
```

该脚本把 `generator_settings.json` 写到 `%LOCALAPPDATA%\MuseDashChart\role_traces\`，默认缓存目录是 `MDRoleRecord/action_cache/`。游戏中选曲稳定约 1 秒后，日志出现 `[ChartCache] 动作表已生成`，再进入对局。首次解析、规则变化或官包更新时，需要重新生成。

## 手动生成及检查

下例在本项目目录手动生成《Echo over you...》原版 map2。`--stage-map` 必须与官包内对象名一致；测试 `_default` 时改为 `echo_over_you_default_map2`。两个命令会写入不同缓存目录。

```powershell
$cache = (Resolve-Path ..\action_cache).Path
py -3.14 .\action_cache_generator.py `
  --uid echo_over_you --map 2 --stage-map echo_over_you_map2 `
  --name 'Echo over you...' --level 8 `
  --cache-root $cache --status (Join-Path $cache 'generation_status.json')
```

`--map` 支持 1–6。选中里谱时，Mod 还会传 `--all-hidden`，为官包中存在的 map4–6 生成缓存。生成器返回的状态 JSON 包含 `ok`、`path`、`maps` 和 `keys`；失败时写入 `error`。对 `_default` 等变体，`keys` 包含完整变体路径。

动作表为 UTF-8 文本，头部包含 `mdr_virtual_format=1`、`chart_uid`、`map_index`、`stage_map`、`action_count`。数据行格式为：

```text
歌曲秒数  down|up  air|ground  tap|hold|dodge|collect|mash|bar_star  event_id
```

每个 `(轨道, event_id)` 都要有配对的 `down` 和 `up`。独立触点使收集物可与同轨长条重叠。`manifest.json` 记录官包与 notedata 的大小、修改时间、规则哈希、动作数量及动作表 SHA-256；缓存校验失败时 Mod 不使用该文件。

## 对局验证与排错

1. 关闭游戏内官机 AutoPlay，在选曲界面等待 `[ChartCache] 动作表已生成`。
2. 进入对应曲目，查看 MelonLoader 日志中 `[BattleClock] 对局谱面` 的 `selectedStage`、`stageInfo`、`resolved`，以及 `[VirtualInput] 加载动作表` 的路径。
3. 原版和 `_default` 应分别指向普通目录与 `variants/<stage_map>/`。若显示“缓存尚未验证”或“谱面对象不匹配”，返回选曲界面等待重新生成，并检查 `generator_settings.json` 和生成状态 JSON。
4. 判定问题可按歌曲时间对齐 `%LOCALAPPDATA%\MuseDashChart\role_traces\` 中的 `virtual_input_*.csv`、`virtual_state_*.csv` 和 `note_events_*.csv`。动作表加载成功并不代表 Great、Miss、长条或齿轮判定已经正确。

成功结算后，Mod 会删除本曲自动生成的 `virtual_actions.txt` 和 `manifest.json`，减少磁盘缓存。中途退出、失败、手工动作表以及 manifest 中标记 `manual_override: true` 的校准缓存不会删除。同一首歌立即重开可暂用内存中保留的边沿；切换选曲后再回来则会重新生成。此前积累的旧缓存不在本次结算清理范围内。

旧的 `main.py` 已归档于 `muse_dash_chart_only/unused_review_20261023/`，不在 Mod 的生成链路中。需要 map6 或区分同序号的 `_default` 官包对象时，请使用此目录的缓存生成器。
