# 仅虚拟按键模式

此模式只通过 `InputManager.GetButtonDown/GetButton/GetButtonUp` 向空轨和地轨提供虚拟触点。它不调用游戏的 `AutoPlay`，也不设置 `dbSkill.isAutoPlay`。每个谱面事件有独立触点，长条按住时可以同时输入其它节点。

## 使用

Mod 只保留虚拟触点回放，无需 `input_mode.txt`。在游戏菜单中关闭官机 AutoPlay，重新进入对局。当前曲目的动作表未准备好或校验失败时，Mod 不注入输入。

首次使用自动生成前，在本目录执行 `powershell -ExecutionPolicy Bypass -File .\configure_generator.ps1`。该脚本设置 Python 3.14 与 `python_runtime/action_cache_generator.py` 的绝对路径，并检查 UnityPy；依赖文件在 `python_runtime/requirements.txt`。MDRoleRecord 已内置 MDChartClock 的时钟写入逻辑；游戏 `Mods` 中不要同时保留 `MDChartClock.dll`。

选曲稳定约 1 秒后，Mod 启动独立 Python 进程解析对应 `music_<uid>_assets_all.bundle`，在 `MDRoleRecord\action_cache\<uid>\mapN\` 中生成动作表和校验清单。进谱时只加载本次启动已验证、官包未更新且 SHA-256 一致的缓存。缓存尚未备好或生成失败时，虚拟注入不会启动；需返回选曲界面等待日志出现 `[ChartCache] 动作表已生成` 后再进入。`generator_settings.json` 的 `cache_root` 指向项目内缓存目录；AppData 仍保留共享时钟、手工动作表和诊断日志。

选择里谱时会检查该曲官包中的 map4–6，并分别生成存在的谱面。选曲时使用游戏给出的实际 `selectedStageMapName`；进入对局后再用已载入谱面的名称校正。缓存按 UID、map 序号和精确谱面名匹配，`_default` 等变体位于 `mapN/variants/<stage_map>/`。当前游戏资源中可见 map4、map5，代码也支持日后出现的 map6。

`%LOCALAPPDATA%\MuseDashChart\role_traces\virtual_actions.txt` 如与当前 UID/map 匹配，会优先于自动生成缓存；变体谱面还需要匹配 `stage_map`。已校准的 MEGALOVANIA map4 v45 表因此继续生效；新曲目的通用动作表仍需结合判定日志验证 Great、Miss 和受击。

动作表的 `chart_uid`、`map_index` 和 `action_count` 必须正确；无法确认曲目、官机 AutoPlay 状态或触点配对时，虚拟注入不启动。若播放中时钟倒退或官机 AutoPlay 被开启，立即停止注入并释放触点。

手工生成缓存示例（在本目录执行）：

```powershell
$cache = (Resolve-Path .\action_cache).Path
py -3.14 .\python_runtime\action_cache_generator.py --uid echo_over_you --map 2 --stage-map echo_over_you_map2 --cache-root $cache --status (Join-Path $cache 'generation_status.json')
```

每次虚拟回放会写 `virtual_input_*.csv`。此文件记录输入边沿和触点 ID；`note_events_*.csv` 记录实际判定和得分。出现漏判时保留两份记录，用歌曲时间对齐检查。旧节点录制、节点回放和键盘注入源码已移出编译目录，备份在 `dll_backups/virtual_only_20261023/`。

成功结算会清理当前自动生成的动作表与 manifest；中途退出、失败、手工表和 `manual_override: true` 的校准缓存会保留。相同曲目立即重开时使用内存中保留的动作边沿，切换选曲后再次进入则重新生成。
