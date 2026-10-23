# MDRoleRecord

Muse Dash 的 MelonLoader Mod：选曲时解析谱面并生成虚拟触点动作表，开局按游戏歌曲时钟回放。它通过 `InputManager.GetButtonDown`、`GetButton`、`GetButtonUp` 输入，不开启游戏内 AutoPlay。通关后清理当前自动生成的磁盘动作表；中途退出和手工校准表保留。

## 依赖

- Windows 版 Muse Dash 与适配其 IL2CPP 版本的 MelonLoader。先运行一次游戏，让 MelonLoader 生成 `MelonLoader/Il2CppAssemblies/`。
- .NET 6 SDK，用于编译 Mod。
- Python 3.14 与 [生成器依赖](python_runtime/requirements.txt)，用于选曲时解析官包。

项目引用游戏和 MelonLoader 生成的程序集；仓库不包含游戏资源、DLL、个人缓存或游玩日志。

## 安装

1. 安装 Python 依赖：`py -3.14 -m pip install -r python_runtime/requirements.txt`。
2. 运行 `powershell -ExecutionPolicy Bypass -File .\configure_generator.ps1`，为 Mod 写入本机生成器路径。
3. 编译：`dotnet build MDRoleRecord.csproj -c Release -p:MuseDashPath="你的 Muse Dash 安装目录"`。
4. 将 `out/MDRoleRecord.dll` 复制到游戏的 `Mods/` 目录。游戏内官机 AutoPlay 保持关闭。

生成器会尝试从 `MUSE_DASH_BUNDLE_DIR`、`MUSE_DASH_PATH` 或常见 Steam 安装位置寻找官包。其它位置可设置环境变量，或以 [config.example.json](python_runtime/config.example.json) 为模板创建本机配置，并在 `%LOCALAPPDATA%\MuseDashChart\role_traces\generator_settings.json` 的 `config_path` 中填写其绝对路径。仓库忽略本机配置、`action_cache/`、构建产物和备份。

使用方法、缓存身份校验和排错见 [虚拟输入说明](VIRTUAL_INPUT.md)；谱面解析规则见 [Python 项目解析](python_runtime/README.md)。
