# 任务交接：Minecraft 本地启动器 + 下载器 + 内容中心

- **更新时间**：2026-10-31 20:30（Asia/Shanghai）
- **交接版本**：0.6.3 发布包 + 0.7.0 候选（微软登录 / CI）；0.7.0 尚未打包，待用户完成微软实网登录验收
- **交接对象**：用户验收 / 下一阶段
- **工作区**：`<workspace>`
- **测试游戏目录**：`D:\game\.minecraft`（用户授权修改）；`_download_test\game`、`game-1.6.4`、`game-1.5.2`、`game-26.3`、`java-test`；临时 `build\content-test-game`、`build\cli-test`
- **当前结论**：0.7.0 候选在 0.6.3 基础上加入 **微软 OAuth 设备代码登录**（`core/msauth.py`、`core/secure_store.py`、GUI/CLI、DPAPI 令牌存储）与 **CI**。**224 个单元/安全测试通过**；`compileall` 通过；用提供的 Azure client ID `<client-id>` 实测 `devicecode` 请求成功，用户完成浏览器登录后 Microsoft/Xbox/XSTS 链通过，但 `login_with_xbox` 返回 **HTTP 403 `Invalid app registration`**：Azure 应用尚未获得 Minecraft Services allowlist 批准，**不是账号没有 Java 版**（用户截图确认拥有 Java）。未保存令牌。需要提交 AppID 审核后再复验；**0.6.3 发布包与 SHA-256 未变**。Java lzma 经实测评估后本轮不启用（见 §6.13）。
- **目标**：Windows 本地 Minecraft 启动器；本地启动 + 官方/镜像下载 + 加载器安装 + 文件校验 + Java 管理 + 内容下载（Mod/整合包/数据包/资源包/光影包）+ 下载队列，不做远程管理。

> **新对话接续提示**：先读 §0 和 [README.md](README.md)。当前发布包 `0.6.3`；源码包含 0.7.0 候选的 **微软登录**（设备代码 + DPAPI）与 **CI**，224 个测试通过；真实登录在 `login_with_xbox` 返回 `403 Invalid app registration`，原因是 Azure 应用未获 Minecraft Services allowlist 批准（账号本身有 Java 版），需先提交 AppID 审核；Java lzma 本轮评估后暂不启用，Linux/macOS、CurseForge `manifest.json` 仍属后续版本。

---

## 0. 交接约定（固定保留）

### 0.1 上下文与阶段结束规则（原文保留）

> 项目要求上下文不到50%时阶段性结束该项目，同时写出下一步交接。占用不到50%时若完成项目也可结束。

执行口径：

- 新阶段开始前先评估上下文，不要在一轮里无限扩展功能。
- 上下文占用接近 50% 时停止新功能开发，优先保证“代码可运行 + 测试通过 + README/process/发布包一致”，并立即更新本文件的新一轮交接。
- 若 0.5.0 在 50% 上下文内完成（实现、测试、打包、非沙箱复验），可直接结束项目并写交接。
- 任务列表按 §0.5 管理：单步任务不建、完成即标、阶段收尾清空。
- 每次交接必须包含：当前版本/状态、已完成、未完成、下一步计划、关键文件、验证命令、测试目录、已知限制。

### 0.2 新对话启动步骤

1. 读本文件 §0、§1、§2、§6 和 README。
2. 确认环境（§0.3）；运行 `py -m unittest discover -v`，基线应为 **224 个通过**（0.7.0 候选）；发布包仍为 0.6.3。
3. 跑一次 `py .\launcher.py --game-dir "D:\game\.minecraft" --verify 1.20.1` 作为下载/启动基线。
4. 跑一次 `py .\launcher.py --java-info --game-dir "D:\game\.minecraft" --game-version 1.20.1` 与 `--verify-java java-runtime-gamma --java-dir "D:\game\_download_test\java-test"`，确认 0.4.0 功能可用。
5. 内容下载基线：`py .\launcher.py --content-search jei --content-type mod --content-game-version 1.20.1 --content-loader fabric --content-limit 3`，以及 `--content-install jei ... --dry-run`。
6. 整合包基线：`py .\launcher.py --game-dir "D:\game\.minecraft" --modpack-archive build\live-cache\fabulously-optimized.mrpack --dry-run`。
7. 微软登录基线：`py .\launcher.py --account-info`；未登录时按 README 用 `--login-microsoft --ms-client-id <id>` 完成浏览器登录。完整登录后跑 `--dry-run`，确认 `account: microsoft` 且 `--accessToken` 为 `<redacted>`。
8. 下一阶段建议优先级：微软登录实网验收 → 0.7.0 打包 → Java lzma（可选）→ Linux/macOS → CurseForge `manifest.json` → Forge 旧版本/Java 8 实网 → NeoForge/Quilt 整合包实网安装 → 队列持久化；0.6.3 的剩余限制见 §5.16，0.7.0 候选记录见 §6.13。
9. 结束前按 §0.1 写交接；若打包发布，更新 README、process、release zip 与 SHA-256。
10. 收尾时按 §0.5 清空任务列表，确认 GUI 中没有残留的“待办/进行中”任务。

### 0.3 环境事实（2026-10-31）

- Python：`py` 3.13.9；打包用 `.venv\Scripts\python.exe`（PyInstaller 6.22.3）。
- Java：`C:\Program Files\Java\jdk-17\bin\java.exe`（1.20.1 / Fabric / Forge / NeoForge legacy 实测）；1.21.11 + NeoForge 21.11.48 实测使用 `C:\Program Files\Java\jdk-22\bin\java.exe`（目标建议 21）；本机另有 Java 8/11/23/27 可用。
- 启动器下载的 Java 17.0.15：`D:\game\_download_test\java-test\java-runtime-gamma\bin\java.exe`（403 文件 / 86.9 MB）。
- 网络：Mojang 官方源与 BMCLAPI 均可用；Modrinth API/CDN 可用（搜索、版本、文件下载均实测）；CurseForge 无 API Key，未做完整实网验收；`maven.neoforged.net` 在本机被 DNS 污染/不可达，BMCLAPI `/maven/net/neoforged/...` 可用但无 `.sha1`；Quilt Meta/Maven 官方可用且无 BMCLAPI 镜像；`login.microsoftonline.com` 可访问，Azure client ID `<client-id>` 的 `devicecode` 请求成功，但 `login_with_xbox` 返回 `403 Invalid app registration`（Azure 应用未获 Minecraft Services allowlist；账号本身有 Java）。
- 稳定版发布包：`release\MinecraftLauncher-0.6.3-win64.zip`，13,623,033 字节 / 1007 个条目，SHA-256 `FBFE9A85CA3B697A883F8468923D8004892BBB9B9B3EC7161601378EAC0F54EC`；0.6.2/0.6.1/0.6.0/0.5.4 包保留。**0.7.0 候选未打包。**
- Git：本机原无 git；已通过 TUNA 镜像安装 PortableGit 2.56.0.4 到 `%LOCALAPPDATA%\Programs\PortableGit`，并把 `cmd` 加入用户 PATH。工作区已 `git init -b main`，提交 `dc4cadf9720d580d01159f4b0a8ae98dfa47741c`（70 个文件）已推送到 `https://github.com/canmengG/cmmcluncher.git` 的 `main`；提交身份 `canmengG <canmengG@users.noreply.github.com>`。**HTTPS 推送需要 VPN/代理；关闭 VPN 后本次推送成功。**
- 0.7.0 候选已实现微软登录与 CI，但微软登录被 **Minecraft AppID allowlist** 阻塞（403 `Invalid app registration`）；仍未实现：Java lzma（评估后暂缓）、Linux/macOS、CurseForge `manifest.json`、Forge 旧版本/Java 8 实网验证、队列持久化。不得在文档中把未实现或未实网验收项标为已完成。

### 0.4 关键文件

- Java 运行时：`mc_launcher/core/java_runtime.py`、`mc_launcher/app.py`（Java 管理对话框/提示）、`mc_launcher/launcher.py`（Java CLI）
- 版本管理：`mc_launcher/core/version_admin.py`、`mc_launcher/app.py`（版本管理对话框）、`mc_launcher/launcher.py`
- 缓存清理：`mc_launcher/core/cache.py`、`mc_launcher/app.py`（缓存清理对话框）、`mc_launcher/launcher.py`
- 多线程下载：`mc_launcher/core/net.py`（单流/分段/合并/回退）、`mc_launcher/core/install.py`（文件并发调度）
- 下载/校验/续传：`mc_launcher/core/net.py`、`mc_launcher/core/install.py`
- 清单/缓存/源：`mc_launcher/core/manifest.py`、`mc_launcher/core/mirrors.py`、`mc_launcher/core/storage.py`
- 启动/加载器：`mc_launcher/core/launch.py`、`mc_launcher/core/loaders.py`（Fabric/Forge/NeoForge/Quilt）、`mc_launcher/core/version.py`
- 下载队列（0.6.0）：`mc_launcher/core/taskqueue.py`、`mc_launcher/app.py`（队列窗口/入队）、`mc_launcher/content_tab.py`（内容入队）、`mc_launcher/core/net.py`（目标文件锁）
- 微软登录（0.7.0 候选）：`mc_launcher/core/msauth.py`（设备代码/XBL/XSTS/MC/profile/refresh）、`mc_launcher/core/secure_store.py`（DPAPI）、`mc_launcher/core/settings.py`（client ID）、`mc_launcher/core/models.py`（`MicrosoftAccount`/`LaunchOptions.account`）、`mc_launcher/core/launch.py`、`mc_launcher/app.py`（账户行/登录对话框）、`mc_launcher/launcher.py`（`--login-microsoft` 等）
- 内容下载（0.5.0）：`mc_launcher/core/content.py`（Modrinth/CurseForge、计划、下载、ZIP/mrpack、sidecar）、`mc_launcher/content_tab.py`（内容页）、`mc_launcher/launcher.py`（内容 CLI）
- 整合包可启动（0.5.1）：`mc_launcher/core/modpack.py`（依赖解析、实例计划、游戏+加载器编排、版本 JSON）、`mc_launcher/core/content.py`（`ModpackPlan`/marker）、`mc_launcher/content_tab.py`（本地压缩包 UI）、`mc_launcher/launcher.py`（`--modpack-archive`）
- UI/CLI：`mc_launcher/app.py`、`mc_launcher/launcher.py`
- 测试：`tests/test_loaders.py`、`tests/test_taskqueue.py`、`tests/test_content.py`、`tests/test_modpack.py`、`tests/test_java_runtime.py`、`tests/test_download.py`、`tests/test_segments.py`、`tests/test_cache.py`、`tests/test_version_admin.py`、`tests/test_core.py`、`tests/fixtures/content/`、`tests/manual_download_check.py`
- 打包/文档：`build.ps1`、`README.md`、`process.md`、`screenshots/`

### 0.5 任务列表（todo）使用约定（固定保留）

背景：DSH Web GUI 的任务列表是**持久面板**。`todo_write` 写入后不会随某条回复结束自动消失；只要列表里还有 `pending` / `in_progress` 项，界面就会持续显示该任务列表。0.3.0 收尾时曾因未关闭列表而让面板长期停留，因此把以下要求固定为项目约定：

1. **单步任务不创建任务列表**；只有需要多步跟踪的阶段性开发才使用。
2. 创建后**每完成一步立即标记 `completed`**，不得让已完成的工作长期停留在 `pending` / `in_progress`。
3. **阶段收尾前必须清空任务列表**（`todo_write` 传入空数组）或全部标记 `completed`；写交接、打包、最终回复前确认 GUI 无残留任务。
4. 新对话接手时，若发现上一轮遗留的任务列表，**先清空/重置再开始**；遗留列表不得当作项目进度来源，进度一律以本文件 §1 为准。
5. 任务列表是工作过程提示，**不是项目交付物**：不写入 README/release，不计入测试、验收或版本号。

---

## 1. 进度

### 1.1 已完成

| 阶段 | 状态 | 说明 |
|---|---|---|
| 技术选型 | 完成 | Python + Tkinter，仅标准库；无 requests/httpx 运行时依赖 |
| 本地启动 | 完成 | 版本扫描、继承合并、参数、natives、Java、隔离 |
| 0.1.1 验收 | 通过 | 用户确认非沙箱环境可进入游戏 |
| 0.2.0 下载器 | 完成 | 安全 HTTP、清单/缓存、原版下载、校验/补齐、Fabric/Forge |
| 安全 HTTP 层 | 完成 | HTTPS-only、主机允许列表、重定向校验、公网 IP 检查、重试/断点 |
| 版本清单/缓存 | 完成 | Mojang/BMCLAPI、正式/快照/远古、10 分钟缓存、离线回退 |
| 原版下载 | 完成 | 客户端 jar、libraries、natives、asset index、assets、log config |
| 完整性检查 | 完成 | 存在/大小/SHA-1；快速/深度；远程优先、本地回退 |
| 补齐 | 完成 | 只下载缺失/损坏文件；原子替换；失败回退 |
| 旧版资源 | 完成 | `assets/virtual/<id>` 与 `resources/` 整理；启动时自动使用 |
| Fabric | 完成 | 稳定/最新/全部；游戏版本对应；profile JSON + Maven 库 |
| Forge | 完成 | 推荐/最新/全部；官方 installer + SHA-1 + 镜像预取依赖 |
| 0.2.1 下载加固 | 完成 | 续传分片保留、进度绝对计数、基础文件优先、版本 JSON/缓存自愈 |
| 0.2.2 Forge 启动修复 | 完成 | 自动把父版本客户端 jar 加入 Forge `-DignoreList` |
| 0.3.0 版本管理 | 完成 | 详情列表、打开文件夹、删除 `versions/<folder>`、路径/运行/链接检查 |
| 0.3.0 缓存清理 | 完成 | 四类缓存统计与分类清理、越界保护、UI/CLI |
| 0.3.0 多线程下载 | 完成 | 文件并发可调；≥8 MB 文件 4 段 Range 并发、合并校验、200/失败回退单流、取消清分段 |
| **0.4.0 Java 清单/组件** | **完成** | Mojang `all.json`、6h 缓存、离线回退、windows-x64 7 个组件、大版本映射 |
| **0.4.0 Java 安装器** | **完成** | 确认后才下载、可选组件/路径/源、逐文件 SHA-1、原子替换、取消保留分片、安装记录 |
| **0.4.0 Java 校验/修复** | **完成** | 离线按安装记录校验 SHA-1；只补缺失/损坏文件；扫描已安装运行时 |
| **0.4.0 Java UI** | **完成** | “获取 Java…”对话框；未匹配提醒；启动前拦截；手动下载网址；导入已有 Java |
| **0.4.0 Java CLI** | **完成** | `--java-info`、`--list-java`、`--install-java`、`--verify-java`、`--java-urls`、`--java-dir`、`--required-java` |
| **0.4.0 零字节修复** | **完成** | 空文件（`trusted.libraries`）正确创建；`jre-legacy` 192 文件全通过 |
| **0.4.0 Fabric 元数据修复** | **完成** | 官方/BMCLAPI 并发获取加载器列表；官方超时不再长时间卡住 |
| **0.5.0 内容源** | **完成** | Modrinth 公共 API（搜索/分类/游戏版本/加载器/排序/版本）；CurseForge 可选 Key 适配 |
| **0.5.0 内容安装** | **完成** | Mod/资源包/光影/数据包目标目录、文件名/路径/URL/SHA-1 校验、确认后下载、sidecar |
| **0.5.0 数据包** | **完成** | 世界选择（level.dat 校验）+ `<world>/datapacks/` 安装 |
| **0.5.0 整合包（旧）** | **完成（实例，不可启动）** | `.mrpack` 下载、ZIP 安全、`modrinth.index.json`、files/overrides、实例记录；0.5.1 已升级为可启动 |
| **0.5.0 内容页 UI** | **完成** | “内容 / 资源”标签页：筛选/搜索/结果/版本/依赖/目标/确认/进度/取消/日志/错误 |
| **0.5.0 内容 CLI** | **完成** | `--content-search`、`--content-versions`、`--content-install`、`--content-*`、`--curseforge-key` |
| **0.5.1 整合包可启动** | **完成** | 依赖解析、自动安装 Minecraft+Fabric/Forge、`versions/<实例>` 版本 JSON、隔离 `.minecraft`、UI 自动选中 |
| **0.5.1 本地压缩包** | **完成** | UI“从压缩包安装…”；CLI `--modpack-archive`/`--modpack-name`/`--modpack-no-launch`；ZIP/mrpack 同套安全校验 |
| **0.5.1 Maven 修复** | **完成** | BMCLAPI `+` 编码 404，Fabric 0.14.22 `sponge-mixin` 可下载 |
| **0.5.2 直接实例布局** | **完成** | 新安装 `versions/<实例>/` 即游戏目录；0.5.1 嵌套 `.minecraft` 旧实例仍可识别/修复 |
| **0.5.3 全下载分段** | **完成** | 游戏/加载器/Java/内容/整合包统一使用配置的分段数，移除 `segments=0` 限制 |
| **0.5.3 暂停/继续** | **完成** | 下载页/内容页/Java 窗口暂停保留 `.part`/`.part.segN`，继续从断点恢复 |
| **0.5.3 取消清理** | **完成** | 取消删除本次下载的完整文件、`.part` 与分段；暂停不删除 |
| **0.5.4 Fabric Maven 修复** | **完成** | Fabric profile 库优先 BMCLAPI Maven，官方回退；解决 `intermediary` 官方超时 |
| **0.6.0 NeoForge 自动安装** | **完成** | legacy 1.20.1 + 现代 1.20.2+；BMCLAPI Maven 镜像重写、安装器预取与结构校验、版本 ID/游戏版本一致性 |
| **0.6.0 Quilt 自动安装** | **完成** | Quilt Meta loader 列表/profile、稳定通道、库下载、`quilt-loader-<v>-<game>` |
| **0.6.0 下载队列** | **完成** | 进程内队列、默认并发 1（上限 3）、任务暂停/继续/取消/移除、进度快照、目标文件锁 |
| **0.6.0 队列 UI/内容入队** | **完成** | 下载页“加入队列/下载队列…”、内容页“加入队列”、本地整合包队列确认 |
| **0.6.1 内容页版本默认值** | **完成** | 沿 `inheritsFrom` 取真正 Minecraft 版本；修复实例 ID（如 `fabulously-optimized-14.1.1`）出现在游戏版本筛选 |
| **0.6.2 加载器镜像允许列表** | **完成** | Mojang 源下 NeoForge/加载器客户端允许 BMCLAPI Maven；元数据/安装器/依赖回退不再被主机列表拒绝 |
| **0.6.3 加载器默认隔离** | **完成** | 带 `inheritsFrom` 的版本默认使用 `versions/<版本>/`；NeoForge/Quilt/Forge/Fabric 不再读取共享 `.minecraft/mods` |
| **0.7.0 微软登录（候选）** | **完成（待实网验收）** | `core/msauth.py` 设备代码/XBL/XSTS/MC/profile/refresh；`core/secure_store.py` DPAPI；GUI 登录对话框、CLI `--login-microsoft`/`--account-info`/`--logout-microsoft`；启动使用正版 UUID/token/`userType=msa`。实网登录在 `login_with_xbox` 返回 `403 Invalid app registration`：Azure 应用未获 Mojang allowlist，账号本身有 Java 版；待提交 AppID 审核后复验 |
| **0.7.0 CI（候选）** | **完成（待推送验证）** | `.github/workflows/ci.yml`（Windows 3.11/3.13 + 测试/compileall + PyInstaller package job）；`ci.ps1` 本地复现；`build.ps1` 支持无 `.venv` 时回退 `python` |
| **Java lzma 评估** | **暂不启用** | 实测 Java 17 省 23%、jre-legacy 省 70%；因仅覆盖部分文件且新增解压/校验路径，本轮只记录决策 |
| 单元/安全测试 | 完成 | **224 个测试通过**（新增 22 个微软登录/网络安全/启动参数测试） |
| 真实环境验证 | 完成 | NeoForge 1.20.1-47.1.106（3675 文件）、NeoForge 21.11.48（4695 文件）、Quilt 0.27.0（3653 文件）均校验通过；Fabulously Optimized 5.2.8 实网安装保留；Azure client ID 的 `devicecode` 请求实测成功 |
| 文档/截图 | 完成 | README/process 已更新 0.7.0 候选说明；截图沿用 0.5.0，登录对话框待实网截图 |
| 打包 | 完成（0.6.3） | `release\MinecraftLauncher-0.6.3-win64.zip`（13,623,033 字节 / 1007 条目；SHA-256 `FBFE9A85CA3B697A883F8468923D8004892BBB9B9B3EC7161601378EAC0F54EC`）；0.7.0 未打包 |
| exe 冒烟测试 | 通过（0.6.3） | 窗口标题 `Minecraft Launcher 0.6.3`，进程正常响应 |
| **非沙箱实机验证** | **通过（0.6.3）** | 用户确认 0.6.3 沙箱外可正常启动；0.7.0 微软登录待用户实网复验 |

### 1.2 关键里程碑

1. 0.1.1 基础启动链路验收通过。
2. 0.2.0 完成安全 HTTP 层与下载计划抽象（`net.py` / `install.py`）。
3. 用 BMCLAPI 修复 `.minecraft` 的 1.20.1，3642 个文件全部通过 SHA-1。
4. 空目录全新安装 1.0：479 个文件 / 48.7 MB，`resources/` 自动生成。
5. Fabric 0.16.9 + 1.20.1、Forge 47.4.0 + 1.20.1 均通过校验。
6. 0.2.1 修复进度重复计数、续传分片丢失、下载顺序和版本 JSON 损坏自愈。
7. 2026-10-30 1.6.4 全新安装（BMCLAPI）：616 文件 / 117.7 MB / 190.5 s，`assets/virtual/legacy` 生成 1120 个文件。
8. 1.6.4 修复：只重下 4 个文件 / 4.7 MB，612 个完整文件跳过；26.3 中断续传完成 5224 文件深度校验。
9. 2026-10-30 用户非沙箱双击 0.2.2 exe：1.20.1 原版、Fabric、Forge、26.3 均进入游戏。
10. 2026-10-31 0.3.0 完成三项功能；26.3 客户端 jar（41,483,720 字节）单线程 53.8 s、4 段 15.7 s（3.43×），SHA-1 通过且无残留。
11. 2026-10-31 0.3.0 全新安装 1.0（`--threads 4 --segments 4`）479 文件全通过；删除测试版本后共享库/资源保留；`--clean-cache partial` 精确删除 2 个分片。
12. 2026-10-31 删除 26.3 客户端 jar 后用 CLI `--install 26.3 --segments 4` 补全：只下载 1 个文件 / 39.6 MB，SHA-1 与备份一致，5224 文件深度校验全部通过，无残留分片；`.minecraft` 1.20.1 深度校验 3642 文件通过。
13. 2026-10-31 0.4.0 Java 17.0.15（`java-runtime-gamma`，Mojang 官方源）：403 个文件 / 86.9 MB，全部 SHA-1 通过，`java -version` 输出 `openjdk version "17.0.15"`。
14. 2026-10-31 0.4.0 修复：删除 `lib/jvm.cfg` 并改坏 `release` 后重新安装，只下载 2 个文件 / 1.1 KB，跳过 401 个；`--verify-java` 403 文件全部通过（0.46 s）。
15. 2026-10-31 0.4.0 未确认下载验证：非交互 `--install-java 17` 不下载、退出码 130、目标目录未创建；`--dry-run` 只打印 403 文件 / 86.9 MB 计划。
16. 2026-10-31 0.4.0 修复：Java `lib/security/trusted.libraries`（0 字节）不再因缺少 `.part` 失败；`jre-legacy` 192 文件全部通过。
17. 2026-10-31 0.4.0 修复：Fabric 加载器列表改为官方 Meta + BMCLAPI 并发请求；官方 `meta.fabricmc.net` 超时场景从约 45 s 卡住变为约 1 s 返回 255 个 build。
18. 2026-10-31 用户确认 0.4.0 测试完成、两个问题修复完毕、功能确认完成。
19. 2026-10-31 0.5.0 Modrinth 实测：五类搜索（Mod 17,855 / 整合包 4,324 / 数据包 2,133 / 资源包 17,981 / 光影 693）；分类 19、游戏版本 921；JEI 1.20.1+Fabric 212 个版本，release `15.56.0.205`。
20. 2026-10-31 0.5.0 实网安装并 SHA-1 通过：JEI（1.7 MB）、Translations for Sodium（287 KB）、BSL 10.1.8（1.1 MB）、Terralith 2.5.4（3.0 MB，临时世界）。
21. 2026-10-31 0.5.0 整合包：Fabulously Optimized 5.2.8 `.mrpack`（61 KB）下载/SHA-1/解析通过（52 文件 / 25.8 MB / Fabric 0.14.22 / overrides）；完整安装由 fixture 测试覆盖。
22. 2026-10-31 0.5.0 UI：内容页实网搜索 JEI、212 版本、release 选择、目标 `mods/`、安装按钮可用；安装处理函数实机执行到“安装完成”。
23. 2026-10-31 0.5.0 未确认下载：`--content-install ... --dry-run` 只打印计划，退出 0，不写文件。
24. 2026-10-31 0.5.1 整合包启动集成：Fabulously Optimized 5.2.8（Fabric 0.14.22 / 1.20.1）实网完整安装，52 文件 / 25.8 MB + 27 overrides，生成 `versions/Fabulously-Optimized-5.2.8/.minecraft`，marker `launchable=true`。
25. 2026-10-31 0.5.1 `--list-versions --details` 显示 `Fabric 0.14.22`、25.8 MB、隔离；`--dry-run` 的 `--gameDir` 指向实例隔离目录。
26. 2026-10-31 0.5.1 修复 BMCLAPI Maven `+` 编码 404，Fabric 0.14.22 `sponge-mixin-0.12.5+mixin.0.8.5` 可下载；本地 `.mrpack` CLI dry-run 不创建游戏目录。
27. 2026-10-31 0.5.2 布局修正：新整合包直接以 `versions/<实例>` 为游戏目录；已把 `Fabulously-Optimized-5.2.8` 从 `.minecraft` 迁移到版本目录，`--gameDir` 验证指向版本目录，启动到 Fabric 加载阶段。
28. 2026-10-31 0.5.3 全下载分段、暂停/继续、取消清理；171 个测试通过，发布包与 exe 冒烟通过。
29. 2026-10-31 0.5.4 Fabric profile 库优先 BMCLAPI Maven；1.21.11 的 `intermediary` 不再官方超时，172 个测试通过。
30. 2026-10-31 0.6.0 NeoForge 1.20.1-47.1.106 实网安装：3675 文件全部通过；`--verify --fast` 通过；启动 dry-run 正确使用 Java 17 与父版本客户端 jar。
31. 2026-10-31 0.6.0 NeoForge 21.11.48 + 1.21.11 实网安装：4695 文件全部通过；启动 dry-run 使用 Java 22，主类 `net.neoforged.fml.startup.Client`。
32. 2026-10-31 0.6.0 Quilt 0.27.0 + 1.20.1 实网安装：3653 文件全部通过；版本详情显示 `Quilt`。
33. 2026-10-31 0.6.0 下载队列（顺序/并发/暂停/取消/失败隔离/目标文件锁）与 UI 冒烟通过；总计 200 个测试通过。
34. 2026-10-31 0.6.1 修复内容页游戏版本默认值：选中 `fabulously-optimized-14.1.1` 时显示 `26.2`，原版/NeoForge/Quilt 版本分别显示 `1.20.1`、`1.21.11`、`1.20.1`；201 个测试通过。
35. 2026-10-31 0.6.2 修复 Mojang 源下 NeoForge 元数据被允许列表拒绝：加载器客户端允许 BMCLAPI Maven，`fetch_neoforge_builds("1.21.11")` 返回 46 个版本；202 个测试通过。
36. 2026-10-31 0.6.3 修复加载器版本默认读取共享 `.minecraft/mods`：带 `inheritsFrom` 的版本默认隔离；NeoForge/Quilt `--dry-run` 的 `--gameDir` 指向各自 `versions/<版本>`。

---

## 2. 现状

### 2.1 交付物

| 文件 | 说明 |
|---|---|
| `release\MinecraftLauncher-0.6.3-win64.zip` | 0.6.3 发布包（加载器版本默认隔离），13,623,033 字节 / 1007 个条目；SHA-256 `FBFE9A85CA3B697A883F8468923D8004892BBB9B9B3EC7161601378EAC0F54EC` |
| `release\MinecraftLauncher-0.6.2-win64.zip` | 上一版 0.6.2 包（加载器镜像允许列表修复），13,621,178 字节 / 1007 个条目；SHA-256 `684E5E2945863670A9A3FE1387F828F08B18243ECBB89CE7FB0C040982606F20` |
| `release\MinecraftLauncher-0.6.1-win64.zip` | 上一版 0.6.1 包（内容页游戏版本默认值修复），13,620,896 字节 / 1007 个条目；SHA-256 `91972BA4739EE661F5AFC43B47F93DA31E367FF979AF4D095397E0E8F00D3008` |
| `release\MinecraftLauncher-0.6.0-win64.zip` | 上一版 0.6.0 包（NeoForge/Quilt + 下载队列），13,619,670 字节 / 1007 个条目；SHA-256 `780C146E9BA7AFD3C139A334A9B6FC7B5DFD2CBB0E1526770D901D9D71744E1E` |
| `release\MinecraftLauncher-0.5.4-win64.zip` | 上一版 0.5.4 包，13,585,530 字节 / 1007 个条目；SHA-256 `42B9265BFBB4360B140207BB73B6B85B2018D67E94BC5302C97D8F434E3D3AEC` |
| `release\MinecraftLauncher-0.5.3-win64.zip` | 上一版 0.5.3 包，13,583,536 字节 / 1007 个条目；SHA-256 `EF9F8EB8F94258FBD2238FF197BC3D3E2C6EED1DB726866E5848E9449ABAE2F2` |
| `release\MinecraftLauncher-0.5.2-win64.zip` | 上一版 0.5.2 包，13,578,203 字节 / 1007 个条目；SHA-256 `2189356657E25B24EEAF2BF9E8D4DA029D71051EB4C4BEC666B392191C7634D0` |
| `release\MinecraftLauncher-0.5.1-win64.zip` | 上一版 0.5.1 包，13,578,431 字节 / 1007 个条目；SHA-256 `304186B0D53AEBECADFE59B6BEC9DCC8E5E028495DC5A2AE81DF40B6DAC2D7F9` |
| `release\MinecraftLauncher-0.5.0-win64.zip` | 更早的 0.5.0 包，13,556,619 字节 / 1007 个条目；SHA-256 `FE824BA317FFF9C102AEE1F940E10DE6E69647A98853EDE658DD3331076160AF` |
| `dist\MinecraftLauncher\MinecraftLauncher.exe` | PyInstaller onedir 主程序（0.6.3，窗口标题冒烟通过） |
| `dist\MinecraftLauncher\_internal\` | 必须与 exe 同目录 |
| `README.md` | 使用、微软登录、内容下载、整合包实例、Java 管理、版本管理、缓存清理、并发/分段、安全、CLI、构建、CI |
| `ci.ps1` | 本地 CI 复现（测试 + compileall） |
| `.github/workflows/ci.yml` | GitHub Actions：Windows 3.11/3.13 测试 + PyInstaller package job |
| `tests\test_msauth.py` | 微软设备代码/令牌链/DPAPI/启动参数测试（22 个） |
| `screenshots\ui.png` | 启动页截图（0.5.0；0.5.2 功能未变） |
| `screenshots\download-tab.png` | 下载页截图（0.5.0；0.5.2 功能未变） |
| `screenshots\content-tab.png` | 内容页截图（0.5.0，Mod 搜索 + 版本选择） |
| `screenshots\content-modpack.png` | 整合包截图（0.5.0；0.5.2 新增“从压缩包安装…”按钮） |
| `screenshots\content-datapack.png` | 数据包截图（0.5.0，世界选择） |
| `screenshots\java-runtime.png` | Java 运行时管理截图（0.4.0，功能未变） |
| `screenshots\java-links.png` | 手动下载 Java 网址截图（0.4.0，功能未变） |
| `assets\icon.ico` | 图标 |
| `build.ps1` | 从 `APP_VERSION` 读取版本并打包 |
| `tests\manual_download_check.py` | 联网手工集成检查（install / verify / repair / resume） |

> 发布包是 **onedir**，不是单文件 exe；必须保留 `_internal`。

### 2.2 代码结构

```text
launcher.py                       # PyInstaller 入口
mc_launcher/
├─ __init__.py                    # APP_VERSION = 0.6.3
├─ app.py                         # Tkinter：启动页 + 下载页 + 内容页 + 下载队列 + 各管理对话框
├─ content_tab.py                 # “内容 / 资源”标签页
├─ launcher.py                    # CLI
└─ core/
   ├─ auth.py
   ├─ cache.py                    # 缓存统计/分类清理/越界保护
   ├─ content.py                  # Modrinth/CurseForge、内容计划/下载/ZIP/mrpack/sidecar
   ├─ errors.py
   ├─ install.py                  # 计划/校验/补齐/续传调度/JSON 修复/虚拟资源
   ├─ java.py                     # Java 探测、版本评分、所需大版本推断
   ├─ java_runtime.py             # Mojang Java 清单/组件、确认后下载、校验、手动网址
   ├─ launch.py
   ├─ loaders.py                  # Fabric/Forge/NeoForge/Quilt、安装器预取
   ├─ manifest.py                 # 清单/版本 JSON/缓存/并发取元数据
   ├─ mirrors.py                  # Mojang/BMCLAPI 源与 URL 重写（含 NeoForge /releases）
   ├─ models.py
   ├─ modpack.py                  # 整合包依赖/隔离实例/游戏+加载器/版本 JSON
   ├─ msauth.py                   # 微软设备代码/Xbox/Minecraft 令牌链/刷新
   ├─ net.py                      # HTTPS/允许列表/重定向/SSRF/单流+分段下载/目标锁
   ├─ paths.py
   ├─ safety.py
   ├─ secure_store.py             # Windows DPAPI 加密令牌存储
   ├─ settings.py                 # config.json / Azure client ID
   ├─ storage.py
   ├─ taskqueue.py                # 下载任务队列（排队/并发/暂停/取消/进度）
   ├─ version.py                  # 版本扫描/合并/库/资源
   └─ version_admin.py            # 版本详情/加载器识别/安全删除
tests/
├─ test_core.py                   # 11 个原有测试
├─ test_download.py               # 下载/安全/续传/修复/目标文件锁测试
├─ test_segments.py               # 7 个分段下载测试
├─ test_cache.py                  # 7 个缓存统计/清理测试
├─ test_version_admin.py          # 版本详情/删除安全/NeoForge 与 Quilt 识别
├─ test_java_runtime.py           # 19 个 Java 清单/确认/安全/校验/修复测试
├─ test_content.py                # 61 个内容 facets/解析/版本/路径/ZIP/mrpack/sidecar/CurseForge 测试
├─ test_modpack.py                # 整合包依赖/隔离实例/版本 JSON/压缩包/可启动测试
├─ test_loaders.py                # NeoForge 映射/镜像、Quilt profile、加载器识别
├─ test_taskqueue.py              # 队列顺序/并发/暂停/取消/失败隔离
├─ test_msauth.py                 # 微软设备代码/令牌链/刷新/DPAPI/启动参数/POST
├─ fixtures/content/              # Modrinth/CurseForge 固定 JSON fixture
└─ manual_download_check.py       # 手工联网集成检查（不被 unittest 收集）
```

### 2.3 下载源

| id | 名称 | 用途 | 重定向策略 |
|---|---|---|---|
| `bmclapi` | BMCLAPI 镜像（国内加速） | 默认；清单/版本 JSON/客户端/库/加载器/Java 清单与文件，失败回退官方 | 允许跳转到其他公网 HTTPS（拒绝 IP/私网） |
| `mojang` | Mojang 官方源 | 官方端点；更严格 | 只允许允许列表内主机 |

关键 URL 重写：

- `piston-meta/launchermeta/launcher.mojang.com` → `bmclapi2.bangbang93.com`
- `piston-data.mojang.com` → `bmclapi2.bangbang93.com`（Java 文件走 `/v1/objects/...`）
- `libraries.minecraft.net` → `bmclapi2.bangbang93.com/maven`
- `resources.download.minecraft.net` → `bmclapi2.bangbang93.com/assets`
- `meta.fabricmc.net` → `bmclapi2.bangbang93.com/fabric-meta`
- `maven.fabricmc.net` / `maven.minecraftforge.net` → `bmclapi2.bangbang93.com/maven`
- `maven.neoforged.net/releases/...` → `bmclapi2.bangbang93.com/maven/...`（去除 `/releases`）
- Java 清单 `launchermeta.mojang.com/v1/products/java-runtime/.../all.json` → BMCLAPI 同路径
- Java 组件清单 `piston-meta.mojang.com/v1/packages/<sha1>/manifest.json` → BMCLAPI `/v1/packages/...`

资源对象在 BMCLAPI 模式下 **官方 CDN 优先**，镜像作为回退，避免数千个小文件触发镜像限流。

### 2.4 本轮已验证范围（2026-10-31）

0.7.0 候选单元/静态检查：

- `py -m unittest discover -v`：**224 个测试全部通过**；`py -m compileall -q mc_launcher tests` 通过。
- 新增 `tests/test_msauth.py`（22 个）：设备代码轮询（pending/slow_down/expired）、Xbox/XSTS/Minecraft/profile 链、refresh、XSTS 无 Xbox 账户、profile 404、`Invalid app registration` 403、DPAPI 往返/损坏文件、`config.json` client ID 优先级、启动参数使用正版 UUID/token/`userType=msa`、POST/HTTP 错误体、认证主机与下载主机隔离。
- 真实网络：使用 `<client-id>` 请求 `devicecode` 成功，返回 `verification_uri=https://www.microsoft.com/link`、`user_code`、`expires_in=900`、`interval=5`；用户完成浏览器登录后，Microsoft/Xbox/XSTS 链通过，但 `login_with_xbox` 返回 **HTTP 403 `Invalid app registration`**。账号截图确认拥有 Minecraft: Java，因此原因是 **Azure 应用未获 Minecraft Services allowlist 批准**，不是账号所有权；未保存令牌。
- UI 冒烟：主窗口创建，账户行显示“离线账户”，登录/退出按钮状态正确；登录对话框尚未实网点击。
- CLI 冒烟：`--dry-run` 在测试目录输出 `account: offline`；使用临时 DPAPI 账户运行时输出 `account: microsoft`，`--accessToken` 已替换为 `<redacted>`。
- CI：`.github/workflows/ci.yml` + `ci.ps1` 已加入；本地 test job 命令已执行，GitHub Actions 尚未运行（工作区无 git 远程）。
- 打包：**未打包 0.7.0**；`release\MinecraftLauncher-0.6.3-win64.zip` 未变。

0.6.3 单元/静态检查：

- `py -m unittest discover -v`：**202 个测试全部通过**。
- `py -m compileall -q mc_launcher tests`：通过。
- `has_isolated_game_data()` 增加“版本 JSON 含 `inheritsFrom`”判定；NeoForge/Quilt/Forge/Fabric 版本即使没有 `mods/`、`logs/` 也默认隔离。
- 真实目录验证：`neoforge-21.11.48`、`quilt-loader-0.27.0-1.20.1`、`1.20.1-forge-47.4.0`、`fabric-loader-0.19.3-1.21.11` 均返回 True。
- `--dry-run` 验证：NeoForge `--gameDir` = `versions\neoforge-21.11.48`，Quilt = `versions\quilt-loader-0.27.0-1.20.1`，`isolated: true`。

0.6.2 单元/静态检查：

- `py -m unittest discover -v`：**202 个测试全部通过**。
- `py -m compileall -q mc_launcher tests`：通过。
- 新增回归测试：`_fabric_http()` 在 Mojang 源下仍允许 `bmclapi2.bangbang93.com`；`loader_allowed_hosts(mojang)` 包含镜像，但 `mojang.allowed_hosts` 本身保持严格。
- 真实网络：`get_source("mojang")` + 严格客户端调用 `fetch_neoforge_builds("1.21.11")`，成功返回 46 个版本，最新/稳定 `21.11.48`，不再出现主机列表拒绝。
- 修复范围：NeoForge 元数据、NeoForge/Forge/Quilt/Fabric 安装客户端。

0.6.1 单元/静态检查：

- `py -m unittest discover -v`：**201 个测试全部通过**。
- `py -m compileall -q mc_launcher tests`：通过。
- 新增 `version.base_game_version()`：沿 `inheritsFrom` 找到真正的 Minecraft 版本；`test-pack-1.0 → fabric-loader-0.19.5-1.20.1 → 1.20.1` 回归测试通过。
- 真实目录 UI 冒烟：`fabulously-optimized-14.1.1` → 内容页默认 `26.2`；`1.20.1` → `1.20.1`；`neoforge-21.11.48` → `1.21.11`；`quilt-loader-0.27.0-1.20.1` → `1.20.1`。

0.6.0 单元/静态检查：

- `py -m unittest discover -v`：**200 个测试全部通过**（约 10.3 s）。
- `py -m compileall -q mc_launcher tests`：通过。
- 新增 NeoForge/Quilt/队列测试（28 个）：版本映射、镜像重写、install_profile 一致性、Quilt 列表/profile、加载器识别、队列顺序/并发/暂停/取消/失败隔离、目标文件锁。
- 0.6.0 实网：
  - NeoForge 1.20.1-47.1.106（legacy）：3675 文件全部通过；`--verify --fast` 全通过；`--dry-run` 使用 Java 17。
  - NeoForge 21.11.48 + 1.21.11：4695 文件全部通过；`--dry-run` 使用 Java 22，主类 `net.neoforged.fml.startup.Client`。
  - Quilt 0.27.0 + 1.20.1：3653 文件全部通过；版本详情显示 `Quilt`。
  - NeoForge 安装器 SHA-1 缺失走结构校验；`de.siegmar:fastcsv` 首次预取失败后自动重试成功。
- UI/队列冒烟：窗口创建、任务入队、暂停/继续、完成刷新正常。

0.5.4 单元/静态检查（历史）：

- `py -m unittest discover -v`：**172 个测试全部通过**（约 5.2 s）。
- `py -m compileall -q mc_launcher tests`：通过。
- 新增 Fabric Maven 回归测试：即使来源选 Mojang，Fabric profile 库 URL 仍为 BMCLAPI 第一、官方第二。
- 1.21.11 + Fabric 0.19.3 规划验证：`libraries/net/fabricmc/intermediary/1.21.11/intermediary-1.21.11.jar` 的 URL 顺序为 BMCLAPI → `maven.fabricmc.net`。

0.5.3 历史单元/静态检查：

- `py -m unittest discover -v`：**171 个测试全部通过**（约 5.2 s）。
- `py -m compileall -q mc_launcher tests`：通过。
- UI 冒烟：下载页、内容页、Java 管理窗口均有“暂停”按钮，初始禁用；窗口标题 `Minecraft Launcher 0.5.3`。
- 下载策略：内容/整合包/Java 代码路径不再出现 `segments=0`；`get_content_source(segments=...)` 与 UI/CLI 配置一致。
- 暂停测试：暂停时下载线程阻塞且不落盘，继续后完整写入；暂停中取消抛出 `CancelledError` 并删除 `.part`。

0.5.2 历史单元/静态检查：

- `py -m unittest discover -v`：**169 个测试全部通过**（约 4.3 s）。
- `py -m compileall -q mc_launcher tests`：通过。
- 源码扫描：无 `shell=True`、`verify=False`、未验证 TLS 等模式。
- CLI 冒烟：`--modpack-archive ... --dry-run` 打印依赖/版本目录/文件数，退出 0，且不创建游戏目录。
- 启动 dry-run：`--game-version Fabulously-Optimized-5.2.8 --dry-run` 的 `--gameDir` 为 `versions\Fabulously-Optimized-5.2.8`（不再嵌套 `.minecraft`）。
- 版本详情：`--list-versions --details` 显示 `Fabric 0.14.22`、约 63.7 MB、隔离。

0.5.2 真实网络（整合包）：

| 场景 | 结果 |
|---|---|
| Fabulously Optimized 5.2.8 | `.mrpack` 61 KB；Fabric 0.14.22 / 1.20.1；52 文件 / 25.8 MB + 27 overrides |
| 加载器安装 | Fabric 0.14.22 profile + 库；修复 BMCLAPI `sponge-mixin` `%2B` 404 |
| 实例 | `versions/Fabulously-Optimized-5.2.8/`；版本 JSON、49 mod、26 config、marker 同级；无 `.minecraft` 子目录 |
| 版本 JSON | `inheritsFrom=fabric-loader-0.14.22-1.20.1`；`mcLauncher.loaderLabel=Fabric 0.14.22` |
| 隔离 | 启动 dry-run 的 `--gameDir` 指向版本目录；`has_isolated_game_data=true` |
| 实际启动 | 进程运行 25 s 未退出；`logs/latest.log` 出现 `Loading Minecraft 1.20.1 with Fabric Loader 0.14.22`、`Fabric is preparing JARs`；未等待到标题界面 |

> 0.5.2 已完成实网 52 文件下载、直接布局迁移与启动到 Fabric 加载阶段；标题界面/进入世界的最终视觉确认需用户非沙箱复验。0.5.1 的嵌套 `.minecraft` 实例仍可修复（代码保留兼容），但不作为新安装布局。

0.5.0 历史单元/静态检查：

- `py -m unittest discover -v`：**151 个测试全部通过**（约 4 s）。
- `py -m compileall -q mc_launcher`：通过。
- 源码扫描：无 `shell=True`、`verify=False`、未验证 TLS 等模式。
- UI 冒烟：主窗口 + 内容页；内容页实网搜索、版本加载、目标/按钮状态、安装处理函数均正常；窗口标题 `Minecraft Launcher 0.5.0`。

0.5.0 真实网络（Modrinth）：

| 场景 | 结果 |
|---|---|
| 五类搜索 | Mod 17,855 / 整合包 4,324 / 数据包 2,133 / 资源包 17,981 / 光影 693 |
| 标签 | 分类 19（mod `categories`）；加载器 29；游戏版本 921 |
| JEI 1.20.1 + Fabric | 212 个版本；默认 release `15.56.0.205`；文件 1.7 MB |
| Mod 安装 | JEI 下载 + SHA-1 通过；sidecar 记录；重复安装 0 下载 |
| 资源包 | Translations for Sodium `v8.21.0`（287 KB）SHA-1 通过 |
| 光影 | BSL Shaders `10.1.8`（1.1 MB）SHA-1 通过 |
| 数据包 | Terralith `2.5.4`（3.0 MB）安装到临时世界 `saves/TestWorld/datapacks` |
| 整合包 | Fabulously Optimized `5.2.8` `.mrpack` 61 KB / SHA-1 / 解析 52 文件 / 25.8 MB / Fabric 0.14.22 / overrides |
| `--content-install ... --dry-run` | 只打印项目/版本/文件/SHA-1/目标；退出 0；不创建文件 |
| 未确认下载 | UI 必须点确认框；CLI 非交互无 `--yes` 返回 2，不写文件 |

> 0.5.0 时整合包只验证到解析/实例逻辑；0.5.1 已补齐完整下载与可启动版本。

0.4.0 历史验证（保留）：

- `py -m unittest discover -v`：90 个通过；`py -m compileall -q mc_launcher` 通过。
- Java 17.0.15：403 文件 / 86.9 MB / 全部 SHA-1 通过 / `java -version` 正常。
- 未确认下载：非交互无 `--yes` 退出 130、未创建目录；`--dry-run` 只打印计划。
- 修复：只补 2 文件 / 1.1 KB；`--verify-java` 403 通过；`jre-legacy` 192 文件（含 0 字节）通过。
- Fabric 加载器列表官方/镜像并发，官方超时不再卡住。
- 用户于 2026-10-31 确认 0.4.0 测试完成、两个问题修复、功能确认完成。

尚未确认（需用户复验）：

- 用户在非沙箱环境双击 0.5.3 exe，并从整合包实例实际进入游戏。
- CurseForge 有 Key 的真实搜索/下载（无可用 Key）。
- Forge 整合包（本轮只实网验证 Fabric）与 Forge 1.12.2 等旧版本 + Java 8；微软登录、NeoForge/Quilt、下载队列。

### 2.5 运行方式

```powershell
py .\launcher.py

py .\launcher.py --list-remote --source bmclapi --remote-type release

py .\launcher.py --game-dir "D:\game\_download_test\game-1.6.4" `
  --install 1.6.4 --source bmclapi --threads 6 --segments 4

py .\launcher.py --game-dir "D:\game\.minecraft" `
  --install 1.20.1 --loader fabric --loader-version 0.16.9 --source bmclapi

py .\launcher.py --game-dir "D:\game\.minecraft" `
  --install 1.20.1 --loader forge --loader-version 47.4.0 --source bmclapi

py .\launcher.py --game-dir "D:\game\.minecraft" --verify 1.20.1

py .\launcher.py --game-dir "D:\game\.minecraft" --list-versions --details

py .\launcher.py --game-dir "D:\game\.minecraft" `
  --remove-version 1.19.3 --yes

py .\launcher.py --game-dir "D:\game\.minecraft" --cache-info
py .\launcher.py --game-dir "D:\game\.minecraft" `
  --clean-cache partial --yes

# Java 运行时（0.4.0）
py .\launcher.py --java-info --game-dir "D:\game\.minecraft" `
  --game-version 1.20.1
py .\launcher.py --list-java --source mojang --required-java 17
py .\launcher.py --install-java 17 --java-dir "F:\Java\mc" --dry-run
py .\launcher.py --install-java 17 --java-dir "F:\Java\mc" --source bmclapi --yes
py .\launcher.py --verify-java java-runtime-gamma --java-dir "F:\Java\mc"
py .\launcher.py --java-urls --required-java 17

# 内容下载（0.5.0；默认 Modrinth）
py .\launcher.py --content-search jei --content-type mod `
  --content-game-version 1.20.1 --content-loader fabric --content-limit 5
py .\launcher.py --content-versions jei --content-type mod `
  --content-game-version 1.20.1 --content-loader fabric
py .\launcher.py --content-install jei --content-type mod `
  --content-game-version 1.20.1 --content-loader fabric `
  --game-dir "D:\game\.minecraft" --dry-run
py .\launcher.py --content-install terralith --content-type datapack `
  --content-game-version 1.20.1 `
  --content-world "D:\game\.minecraft\saves\World" `
  --game-dir "D:\game\.minecraft" --yes
py .\launcher.py --content-install fabulously-optimized --content-type modpack `
  --content-game-version 1.20.1 --content-loader fabric `
  --game-dir "D:\game\.minecraft" --dry-run

# 本地整合包（0.5.2）：默认可启动；--modpack-no-launch 只整理内容
py .\launcher.py --game-dir "D:\game\.minecraft" `
  --modpack-archive "D:\packs\pack.mrpack" --dry-run
py .\launcher.py --game-dir "D:\game\.minecraft" `
  --modpack-archive "D:\packs\pack.mrpack" --yes
```

联网集成检查（会真实下载/删除测试目录）：

```powershell
py .\tests\manual_download_check.py install --game-dir "F:\tmp\mc" --version 1.6.4 --source bmclapi
py .\tests\manual_download_check.py repair  --game-dir "F:\tmp\mc" --version 1.6.4
py .\tests\manual_download_check.py resume  --game-dir "F:\tmp\mc" --version 26.3 --cancel-mb 20
py .\tests\manual_download_check.py verify  --game-dir "D:\game\.minecraft" --version 1.20.1
```

重新打包：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\build.ps1
```

---

## 3. 实现说明

### 3.0 0.4.0 实现说明（保留）

### 3.1 Java 运行时清单与组件

- 模块 `core/java_runtime.py`：
  - 清单地址 `https://launchermeta.mojang.com/v1/products/java-runtime/2ec0cc96c44e5a76b9c8b7c39df7210883d12871/all.json`；BMCLAPI 同路径。
  - 6 小时缓存（`%APPDATA%\MinecraftLauncher\cache\<源>\java-runtime\all.json`），缓存损坏会重拉；网络失败时使用缓存并提示。
  - 只接受 `java-runtime*` / `jre-*` 组件，忽略 `minecraft-java-exe`；按版本号识别 Java 大版本，快照标记为实验。
  - `windows-x64` 当前 7 个组件；`default_platform()` 在 Windows ARM64 上返回 `windows-arm64`。
  - `COMPONENT_FOR_MAJOR`：8→`jre-legacy`、16→`java-runtime-alpha`、17→`java-runtime-gamma`、21→`java-runtime-delta`、25→`java-runtime-epsilon`；实际以清单为准。
  - `choose_component()`：优先精确大版本且稳定、版本最高；否则最近的更高版本；再否则更低版本。
  - 组件清单按 SHA-1 内容寻址缓存到 `.../java-runtime/components/<sha1>.json`，校验 SHA-1 和可选大小。
- 组件清单解析只接受：
  - `type=file` 且 `downloads.raw`（不使用 lzma）；
  - `type=directory`（显式创建）；
  - 拒绝 `type=link`、未知类型、缺少 raw、缺少 SHA-1、非法大小；
  - 路径经过 `safe_relative_path`（拒绝 `..`、绝对路径、驱动器、Windows 保留名），拒绝大小写重复路径、文件/目录冲突、层级过深；
  - 文件数 ≤ 6000，总大小 ≤ 4 GB。

### 3.2 用户可控的下载流程

- `install_java_runtime(plan, *, client, confirmed, ...)` **强制要求** `confirmed=True`；为假时直接抛错，不创建目录、不发请求。测试覆盖。
- UI：先勾选“我确认下载并安装/校验并补齐 … 到 …”，按钮才从禁用变可用；点击后再弹出一次汇总确认（组件、文件数、大小、来源、目标）。
- CLI：先打印组件、文件数、大小、来源、目标；交互询问 `y/N`；非交互必须 `--yes`，否则退出 2；`--dry-run` 只打印计划。
- 可选内容：
  - 组件（Java 8/16/17/21/25）；
  - 安装位置（默认 `<游戏目录>\runtime`；可浏览任意有写权限目录）；
  - 下载源（Mojang/BMCLAPI）；
  - 是否在完成后切换为该 Java；
  - 随时取消；
  - 手动下载网址（Mojang 清单 + Adoptium/Microsoft/Azul/Corretto/BellSoft/Oracle）；
  - 导入已有 `java.exe`。
- 安装布局：`<安装位置>\<组件名>\`，例如 `...\runtime\java-runtime-gamma\bin\java.exe`；与现有 `discover_java()` 的 `runtime/**/bin/java.exe` 扫描兼容。
- 安装前检查写入权限、磁盘剩余空间（需要下载量 + 64 MB）。
- 下载调度：默认 4-6 个文件并发；单文件按配置分段（0.5.3 起所有下载都支持，≥8 MB 且服务器支持 Range 时多段）；进度按“已落盘绝对字节”聚合，跳过已完整文件。
- 每文件 `.part`/`.part.segN` + SHA-1 + 原子替换；网络失败保留分片；暂停保留分片，取消删除本次文件与分片。
- 零字节文件（Mojang Java 清单中的 `lib/security/trusted.libraries` 等）会创建空的 `.part` 并完成 SHA-1 校验，不再触发“系统找不到指定的文件”。
- 成功后写 `.mc-launcher-runtime.json`：schema、组件、版本、平台、源、清单 SHA-1、安装时间、总大小、文件列表（相对路径/SHA-1/大小/可执行）；最后运行 `java -version` 确认可用。
- 安装记录损坏/缺失时 `verify_java_runtime()` 报告“不是由本启动器安装”；仍可检查 `bin\java.exe`。修复通过重新执行安装计划，只下载缺失/损坏文件。
- `scan_installed_runtimes()` 扫描安装根目录的直接子目录，识别 `bin\java.exe` 和安装记录；UI 表格显示“已安装/未安装”，启动器安装的组件可校验/补齐。

### 3.3 UI 行为

- 启动页 Java 行新增 **“获取 Java…”**；Java 提示行按匹配/过低/过高/未检测显示颜色和下一步建议。
- 未检测到 Java 时，日志和提示行明确给出“获取 Java…”入口；点“自动检测”没有 Java 时会询问是否打开 Java 管理。
- Fabric 加载器列表与安装 profile 由 `fetch_fabric_loaders()` / `fetch_fabric_profile()` 同时向官方 Meta 与 BMCLAPI 镜像发起请求；官方 `meta.fabricmc.net` 超时时镜像结果可直接使用，UI 不再长时间停在“正在获取版本列表”，安装 Fabric 也不会卡在 profile 获取。
- 启动前：
  - 未选择/找不到/无法运行 Java → 三选一对话框：打开 Java 管理 / 手动选择 java.exe / 取消；
  - Java 低于要求 → 三选一：打开 Java 管理 / 仍然尝试 / 取消；
  - Java 高于要求 → 日志警告，仍可启动。
- Java 管理窗口：
  - 顶部显示当前版本所需 Java、已检测 Java、当前使用；
  - 组件表（组件/Java/版本/平台/本机状态）；
  - 安装位置输入 + 浏览 + 默认位置；
  - 下载源 + 手动下载网址 + 导入已有 Java + 打开安装目录；
  - 复选框确认 + 开始下载/校验并补齐 + 校验选中安装；
  - 进度条、速度、当前文件、取消；
  - 操作日志；
  - 下载/校验完成后按用户选择切换 Java，并保存 `java_runtime_dir` / `java_component`。
- 手动下载网址窗口：Mojang 清单、所选组件清单、6 个发行版页面；支持打开、复制网址、复制全部；双击打开。

### 3.4 CLI

- `--java-info`：显示所需 Java、系统检测到的 Java、启动器安装的运行时；缺失时给出 `--install-java` / `--java-urls` 建议。
- `--list-java`：列出 Mojang 组件（不下载）；标注匹配当前所需大版本。
- `--install-java <组件|大版本>`：先显示计划；确认后下载；失败/取消返回非零。
- `--verify-java <组件>`：按安装记录校验；返回 0/1。
- `--java-urls`：只打印网址。
- `--java-dir`：安装根目录；`--required-java`：覆盖所需大版本；`--source`：官方/镜像。
- `--yes` 同样适用于 `--install-java`；`--dry-run` 优先于下载。

### 3.5 历史版本（保留）

- 0.3.0：版本管理/删除、缓存清理、大文件分段多线程下载。
- 0.2.1 / 0.2.2：进度绝对计数、续传分片保留、版本 JSON 自愈、下载顺序、Forge `-DignoreList` 修复。
- 0.2.0：安全 HTTP 层、清单/缓存、原版下载、校验/补齐、Fabric/Forge。

### 3.6 0.5.0 内容下载中心

数据源（`mc_launcher/core/content.py`）：

- `ContentSource` 接口：`search`、`categories`、`loaders`、`game_versions`、`versions`、`project`；`get_content_source()` 工厂。
- `ModrinthSource`（`https://api.modrinth.com/v2`，无需 Key）：
  - `/search`：`query/limit/offset/index/facets`；返回 `hits/total_hits/offset/limit`；结果解析 `project_id/slug/title/author/categories/display_categories/downloads/follows/date_modified/icon_url` 等，兼容 `id/followers/updated`。
  - `/tag/category`、`/tag/loader`、`/tag/game_version`：6 小时文件缓存 + 会话缓存；网络失败回退旧缓存；分类只取 `header=categories`。
  - `/project/{id}/version?game_versions=[...]&loaders=[...]`：服务端筛选 + 本地二次过滤/排序；最多解析 2000 个版本。
  - `/project/{id}`、`/projects?ids=[...]`：项目详情 / 依赖标题批量解析。
  - `build_facets()` 纯函数：datapack 使用 `project_type:mod` + `loaders:datapack`；mod/modpack 的 loader 用 `categories:<loader>`；资源包/光影不传 loader；游戏版本用 `versions:<gv>`。
  - `select_best_version()`：优先 release、匹配 loader/game version、最新发布日期；无 release 时回退 beta/alpha；`channel="latest"` 按日期。
- `CurseForgeSource`（`https://api.curseforge.com/v1`，`x-api-key`）：
  - classId：mod 6 / modpack 4471 / resourcepack 12 / datapack 6945 / shader 6552；loader：Forge 1 / Fabric 4 / Quilt 5 / NeoForge 6。
  - `/mods/search`、`/categories`、`/mods/{id}/files`、`/minecraft/version`。
  - `downloadUrl=null` 时保留空 URL，计划阶段给出“手动下载”说明；无 SHA-1 同样拒绝自动下载。
  - 无 Key 时 `enabled=False`、`missing_key_message()` 明确提示；403/429 映射为可操作错误。
- 版本/文件/项目都是 frozen dataclass；所有 id、URL、SHA-1、大小、日期、依赖都做类型/长度/格式清洗。

安装计划与执行：

- `build_content_plan(project, version, file, game_root, content_type, target_dir, world_dir, overwrite)`：
  - 文件名只取最后一段 + `safe_component` + 允许扩展名（mod `.jar`；资源包/光影/数据包 `.zip`；整合包 `.mrpack/.zip`）；
  - URL 必须是 HTTPS + `CONTENT_HOSTS`；必须 SHA-1；目标必须位于游戏目录/所选世界；拒绝磁盘根目录和 `%SystemRoot%`；`is_within` 再校验。
  - 目标目录：`mods`、`resourcepacks`、`shaderpacks`、`<world>/datapacks`、`modpacks`。
- `install_content()`：强制 `confirmed=True`；目标已存在且 SHA-1 一致时直接返回“已安装”；内容不同且未确认覆盖时拒绝；下载复用 `HttpClient.download_file`（分段、`.part`、SHA-1、原子替换、暂停/取消清理）。
- sidecar `.mc-launcher-content.json`：schema 1、相对路径为 key、最多 2000 条；损坏/缺字段时忽略，不阻塞安装；`is_content_installed()` 供 UI 标记。
- 数据包：`list_worlds()` 扫描 `<game>/saves/*/level.dat`；`validate_world_dir()` 必须存在 `level.dat`，否则拒绝。

整合包（`.mrpack`）：

- `validate_zip_archive()`：条目数 ≤ 20000、解压后总大小 ≤ 4 GB、单文件 ≤ 1 GB；拒绝加密、符号链接、`..`、绝对路径、驱动器、Windows 保留名。
- `read_mrpack()`：读取 `modrinth.index.json`（≤ 4 MB），校验 `formatVersion=1`、`name/versionId`、`dependencies`（minecraft/fabric-loader/forge/neoforge/quilt-loader）、`files[]`（path/hashes/downloads/fileSize/env）；文件路径不得在 `overrides/` 下；每个文件必须有 SHA-1 和至少一个允许主机的下载 URL。
- `build_modpack_plan()`：0.5.0 兼容函数，实例目录 `<game>/modpacks/<slug>-<versionId>`；新 UI/CLI 不再使用，改用 `core/modpack.py` 的 `build_instance_plan()`。
- `install_modpack()`：低层内容安装；先解压 `overrides/`、`client-overrides/`（可跳过已有文件），再按 `files[]` 下载/校验；`env.client=unsupported` 跳过；已存在且 SHA-1 一致跳过；未确认覆盖的差异文件记为失败；成功后写 `.mc-launcher-modpack.json`（`complete`、`launchable`、版本目录等）。失败聚合前 8 条后抛错。
- `download_and_install_modpack()`：0.5.0 兼容的“仅内容”下载入口；新入口为 `core/modpack.py` 的 `download_and_install_launchable_modpack()`，先下载 `.mrpack` 到缓存，再安装游戏/加载器并写版本 JSON。

UI（`mc_launcher/content_tab.py`）：

- 独立标签页“内容 / 资源”，不挤压现有下载页；顶部筛选（类型/来源/关键词/分类/游戏版本/加载器/排序），结果表（名称/作者/类型/分类/下载量/更新/已安装），详情（简介/项目链接/版本下拉/依赖/目标/进度/日志）。
- 异步：搜索/标签/分类/版本/依赖标题/安装全部在 daemon 线程，结果经 `queue` + `after(80)` 回 UI；`_search_gen` / `_version_gen` / `_install_gen` 丢弃过期结果。
- 搜索防抖 350 ms；Enter 立即搜索；数据包切换类型后强制显示“世界”下拉并可浏览选择；资源包/光影隐藏加载器/世界。
- 安装按钮只在有项目/版本/文件/URL/SHA-1 且非忙碌时可用；点击后弹出包含项目、版本、类型、文件、大小、目标的确认框；同名文件再确认覆盖；数据包无世界时不可安装。
- CurseForge Key 对话框：`show="*"`、只保存到本机 `config.json`；保存后清理旧 source 缓存并重新加载标签。
- 进度条与“内容日志”；取消按钮设置 `threading.Event`；安装完成刷新 sidecar/“已安装”；错误弹窗 + 日志给出“切换来源/清空筛选/检查网络/手动下载”等下一步。

CLI（`mc_launcher/launcher.py`）：

- `--content-search QUERY`（空字符串列出热门）、`--content-type`、`--content-source`、`--content-game-version`、`--content-loader`、`--content-category`、`--content-sort`、`--content-limit`。
- `--content-versions PROJECT`：只读列出并打印默认选择。
- `--content-install PROJECT`：`--content-version`、`--content-channel release|latest`、`--content-dir`、`--content-world`、`--yes`、`--dry-run`；打印计划后交互确认；非交互无 `--yes` 返回 2。
- `--curseforge-key` 或本机 `config.json` 的 `curseforge_api_key`；Key 不打印、不写日志。

模块边界：内容源不执行任何下载代码；只写游戏目录/世界/整合包实例；0.5.2 的 `core/modpack.py` 会注册可启动版本（Fabric/Forge/原版），NeoForge/Quilt 仍标记 `launchable=false`。

---

## 4. 网络安全设计

### 4.1 威胁模型

主要风险：

1. 恶意/被篡改的版本 JSON 让启动器访问任意 URL（SSRF）；
2. 下载到错误/损坏文件；
3. 远程路径穿越写出游戏目录；
4. 镜像/CDN 被劫持或返回错误内容；
5. Forge 安装器执行下载代码；
6. 启动器被误认为可远程管理；
7. 本地删除/清理功能误删用户数据；
8. 恶意 Java 清单/组件清单写入任意路径或诱导下载。

### 4.2 已实施控制

- HTTPS-only；初始 443；拒绝 userinfo/fragment/非法端口。
- 初始 URL 主机允许列表（Mojang/BMCLAPI/Fabric/Forge）。
- 重定向处理器逐个校验 Location；官方严格、镜像仅允许公网 HTTPS。
- DNS 解析检查，拒绝私网/回环/链路本地/IP 字面量重定向。
- SHA-1 + 大小校验；`.part` + 原子替换；失败删除/重试。
- 续传使用 `Range` 并校验 `Content-Range` 起点；校验失败的文件不会被续传复用。
- 分段下载：每段同样校验 URL/重定向/公网地址；只接受 `206` 与匹配的 `Content-Range`；合并后仍做完整大小 + SHA-1 校验；失败不替换正式文件。
- 路径穿越/Windows 保留名/绝对路径/驱动器检查。
- JSON 大小与条目数上限；索引对象数上限。
- Forge installer SHA-1 + UI 二次确认 + 固定参数列表，无 shell。
- Fabric 不执行下载 jar。
- 版本删除：只允许 `versions/` 直接子目录；拒绝链接/联接、隐藏目录、越界路径；运行中版本拒绝；先改名再删除。
- 缓存清理：只处理已知四类目标；每个路径 `resolve()` 后必须位于允许根目录内；不跟随链接；只删链接本身。
- Java 运行时：
  - 清单/组件/文件 URL 都走同一 HTTPS/允许列表/重定向策略；
  - 组件清单和每个文件必须有 SHA-1；组件清单还校验官方给出的大小；
  - 只接受 `raw`，拒绝 `link`；拒绝路径穿越、绝对路径、驱动器、保留名、重复路径、文件/目录冲突；
  - 文件数/总大小/单文件大小上限；安装记录中的路径同样校验；
  - `confirmed` 必须为真才下载；UI 复选框 + 二次确认；CLI 交互或 `--yes`；
  - 安装路径拒绝磁盘根目录和 Windows 系统目录；检查写入权限与磁盘空间；
  - 每个文件 `.part` + SHA-1 + 原子替换；取消/失败不写正式文件。
- 内容下载（0.5.0）：
  - 独立 `CONTENT_HOSTS`（Modrinth/CurseForge API + CDN），不并入游戏下载允许列表，版本 JSON 无法把游戏文件指向内容 CDN；
  - 内容 URL 在计划阶段和下载阶段都重新校验 HTTPS/主机/重定向公网地址；
  - 必须 SHA-1；文件名清洗、扩展名白名单、目标必须在游戏目录/世界内、拒绝盘符根和 Windows 目录；
  - ZIP/mrpack：条目数/总大小/单文件上限，拒绝加密、符号链接、`..`、绝对路径、驱动器、保留名；解压后再次 `is_within`；
  - 安装函数强制 `confirmed=True`；UI 二次确认、CLI 交互或 `--yes`；`--dry-run` 不写文件；
  - CurseForge Key 只存本机 `config.json`，不写日志/发布包。
- 无监听端口、无远程 API、无遥测；配置仅本机。
- TLS 默认上下文，最低 TLS 1.2，不提供跳过校验选项。

### 4.3 仍存在的信任边界

- BMCLAPI/CDN 是第三方；SHA-1 来自同一元数据，不能证明“发布者签名”。需要更强保证时用官方源。
- Forge 官方 installer 是执行代码；只能验证下载完整性和 SHA-1，不能审计其内部行为。
- Mojang Java 清单/组件同样来自 Mojang 元数据；SHA-1 不提供发布者签名，但比手工下载更难被中间人篡改。
- 本机缓存/配置可被本机攻击者修改；这不是远程攻击面。
- `.part`/`.part.segN` 续传文件会暂存未校验数据；只有最终 SHA-1 通过才会替换正式文件。
- 版本删除是永久删除，没有回收站/撤销；隔离存档会随版本文件夹一起删除。
- Java 下载会写入用户选择的目录；用户若选择共享目录，启动器不会清理其他 Java/文件，但同名文件会被替换（提示框已说明）。
- Modrinth/CurseForge 是第三方；项目/版本/文件元数据和 SHA-1 都来自同一 API，不能证明作者签名。CurseForge `downloadUrl` 允许作者禁止第三方分发，此时只能手动下载。
- `.mrpack` 的 `overrides` 会写入实例目录，可能包含任意脚本/配置；启动器只校验路径和大小，不审计内容。用户应只安装可信整合包。
- 已安装内容 sidecar 可被本机修改；它能提示“已安装”，但不能证明文件来源。
- 内容安装目标目录由用户选择（游戏目录内/世界），同名文件可能被覆盖；覆盖前 UI/CLI 会确认。

---

## 5. 问题与限制

### 5.1 P0（历史，已缓解）：沙箱写入权限

0.1.1 已加入写入预检；0.2.x/0.3.0/0.4.0 保留。请从资源管理器双击 exe，不要从 DSH 工作区/受限沙箱启动。

### 5.2 P1：Java 下载是“按需确认”，不是后台自动

0.4.0 **不会**在后台自动下载 Java，这是刻意设计：无匹配 Java 时提醒，用户打开 Java 管理、选择组件/路径/源并勾选确认后才下载。若希望“一键补齐”，仍需用户点击一次；这是可控性要求，不是缺陷。

### 5.3 P2：NeoForge / Quilt（0.6.0 已完成，边界仍存在）

已实现自动安装：NeoForge 1.20.1 走 legacy `net.neoforged:forge`，1.20.2+ 走现代 `neoforge`；Quilt 走 Meta profile。边界：NeoForge 镜像无官方 `.sha1` 时只做结构校验；Quilt 没有 BMCLAPI 镜像；版本前缀依赖 Minecraft 版本映射，安装器内部 `minecraft` 会二次校验。

### 5.4 P3：Forge 旧版本 / Java 8

Forge 1.12.2 等旧版本需要 Java 8 和旧 installer；当前逻辑会按版本 `javaVersion` 选 Java，但未实测。安装器参数 `--installClient` 对旧版本可能不同，需补测试。`jre-legacy` 是 Java 8u51，较旧；旧版游戏/模组可能需要自行下载新版 Java 8。

### 5.5 P4：Fabric 部分库没有发布 SHA-1

`intermediary` / `fabric-loader` 在 profile 中没有 sha1/size；下载后计算并记录实际哈希。首次下载依赖 HTTPS + Maven 坐标，后续检查可发现损坏。

### 5.6 P5：UI 仍以本地单实例为主

- 下载页、内容页与启动页共用游戏目录；
- 下载中关闭窗口会等待最多 5 秒；
- 0.6.0 已有进程内下载队列（默认并发 1、上限 3），但队列不跨重启保留；
- 即时安装与队列是两条路径，同一时间仍应避免对同一版本重复操作；
- 深度校验大版本需要几秒到几十秒；
- 内容页的结果/版本请求没有单独取消按钮，切换项目时旧结果按 generation 丢弃。

### 5.7 P6：测试目录

- `D:\game\.minecraft`：1.20.1 + Fabric + Forge + NeoForge 1.20.1-47.1.106 + NeoForge 21.11.48 + Quilt 0.27.0 + 其他版本；本轮新增 NeoForge/Quilt 测试版本，其余版本只读/深度校验。
- `_download_test\game`：1.0 全新安装（约 96 MB）。
- `_download_test\game-1.6.4`：1.6.4 + `virtual/legacy`（约 264 MB）。
- `_download_test\game-1.5.2`：1.5.2 + `resources/`（约 99 MB）。
- `_download_test\game-26.3`：26.3 中断续传测试（约 587 MB）。
- `_download_test\java-test`：0.4.0 下载的 Java 17.0.15（约 87 MB，403 文件）。
- `build\content-test-game`：0.5.0 临时游戏目录，含 JEI、Translations for Sodium、BSL、Terralith、`saves/TestWorld`；可整体删除。
- `build\live-cache`：0.5.0 实网测试缓存（标签、Fabulously Optimized `.mrpack`）；可整体删除。
- 如需回收磁盘，可删除整个 `_download_test`，不影响 `.minecraft`。

### 5.8 P7：续传的适用范围

- 续传依赖服务器支持 `Range`；服务器返回 200 时会从头覆盖，进度会归零重算（不会算错总量）。
- 分片按目标文件路径保留；如果版本/源内容变化（哈希变化），旧分片会在校验失败后删除并重下。
- 临时失败保留分片会占用磁盘；安装成功/校验失败/目标已完整时自动清理。

### 5.9 P8：分段下载限制

- 依赖服务器支持 `Range` 且返回 206；`200`、416、`Content-Range` 不匹配都会回退单线程。
- 分段只在目标 `.part` 不存在时启用；单流断点续传仍按原逻辑工作。
- 取消会删除分段，无法跨“取消”继续；进程被强制结束时留下的分段可在下次继续，但要求服务器内容未变化。
- 段数/并发过高可能被镜像限速；默认 6 文件 × 4 段，可在 UI/CLI 调低。
- 进度只显示聚合字节，不显示每段详情。
- **Java 运行时下载从 0.5.3 起使用配置的分段数**（≥8 MB 且服务器支持 Range），不再固定单流。
- 0.5.3 起暂停保留分片；取消删除本次下载文件与分片。

### 5.10 P9：版本删除限制

- 没有回收站/撤销；删除版本文件夹内的隔离存档会一并删除（确认框已提示）。
- 运行中的版本通过“UI 选中版本 + 进程存在”以及 Windows 文件占用拒绝；若游戏从其他启动器启动，改名会因占用失败并报错。
- 目标为符号链接/目录联接时直接拒绝，需用户手动处理。
- 删除是同步操作，大版本可能需要几秒；期间 UI 不显示进度条。

### 5.11 P10：缓存清理限制

- 只识别四类已知缓存；不清理未知的 `cache/<目录>`、`.tmp` 或第三方启动器缓存。
- `*.part` 清理按文件名匹配，不解析内容；用户自己命名为 `*.part` 的文件也会被删除。
- 大目录的 `rglob` 统计在机械硬盘上可能需要几秒；统计过程在 UI 线程同步执行。

### 5.12 P11：Java 运行时限制

- 只支持 Mojang 官方清单中的 Windows 组件（x64/ARM64）；Linux/macOS、其他发行版不自动安装。
- 只下载 `raw` 文件，不使用 lzma；Java 17 约 87 MB / 403 文件，Java 8 `jre-legacy` 约 143 MB / 192 文件；国内官方源可能较慢。
- 组件清单和文件都依赖 Mojang/BMCLAPI；BMCLAPI Java 清单首次请求可能较慢或超时，可切换官方源或稍后重试。
- 不支持“只下载不安装”“选择单个文件”“自定义 JVM 参数”；不支持断点续传之外的分段加速。
- 安装目录可以是任意有写权限的文件夹，但启动器重启后只自动扫描 `<游戏目录>/runtime` 和配置中记录的位置；其他目录需在启动页“浏览…”选择 `java.exe` 或再次打开 Java 管理。
- 安装记录文件可被本机修改；校验能发现安装后损坏，但不能证明来源。
- Java 下载支持分段与暂停；取消会删除本次文件和分片。再次安装会续传（暂停时）或重新下载（取消后），官方文件更新（SHA-1 变化）时旧分片会被删除重下。

### 5.13 P12：Java 版本推断

- 优先使用版本 JSON 的 `javaVersion.majorVersion`；缺失时按版本号推断（1.17→16、1.18-1.20.4→17、1.20.5+/1.21→21、26.x→21）。26.3 的 `javaVersion` 以实际 JSON 为准；若与 README 表格冲突以 JSON 为准。
- Java 8 组件只有 Mojang `jre-legacy`（8u51）；如游戏/模组要求更高 Java 8 小版本，请手动下载并导入。

### 5.14 P13：内容下载限制（0.5.3）

- 只支持 Modrinth / CurseForge 两个来源，不做聚合；CurseForge 需要 Key，且没有可用 Key 的完整实网验收。
- 整合包自动启动支持 **原版 / Fabric / Forge / NeoForge / Quilt**；NeoForge 1.20.1 的版本目录名为 `1.20.1-forge-<版本>`，1.20.2+ 为 `neoforge-<版本>`，Quilt 为 `quilt-loader-<版本>-<游戏版本>`。
- 整合包实例固定在 `versions/<实例>/`：版本 JSON 为 `versions/<实例>/<实例>.json`，`mods/config/saves` 与 `.mc-launcher-modpack.json` 直接放在版本目录；新安装不再创建嵌套 `.minecraft`。0.5.1 的嵌套旧实例仍可识别/修复。
- 加载器版本必须能在 Fabric Meta / Forge Maven 中找到；找不到时安装失败并提示，不会静默降级到其他版本。
- CurseForge 整合包 `manifest.json` 未适配；本地压缩包只接受 `.mrpack` 或包含 `modrinth.index.json` 的 ZIP；`downloadUrl=null` 的文件拒绝自动下载。
- 依赖只展示，不递归下载；版本冲突需用户自行处理。
- 数据包只能安装到已有世界（含 `level.dat`）；不能创建世界，不能自动启用资源包/光影。
- 内容文件按单个文件下载（复用 `.part`/SHA-1/取消/续传）；整合包 `files[]` 逐个串行下载。
- 0.5.3 起内容/整合包/Java 下载也使用配置的分段数（≥8 MB 且服务器支持 Range）；服务器忽略 Range 时自动回退单流。
- 0.5.3 暂停保留 `.part`/`.part.segN` 并可从断点继续；取消会删除本次下载的完整文件、`.part` 和分段。Forge 官方安装器执行阶段不能暂停，只能取消等待。
- 搜索/标签/版本列表有长度与大小上限；游戏版本下拉默认来自远程标签（约 900+ 项），首次加载需要网络。
- 同名文件覆盖前必须确认；sidecar 只记录最近 2000 条，超出后旧记录会被淘汰。
- 没有“已安装内容管理/卸载/更新”页面；删除仍需用户手动操作文件。

### 5.15 P14：0.5.x 实施结果（整合包可启动，已完成）

- 读取 `.mrpack` 的 `dependencies.minecraft` + `fabric-loader` / `forge`；无加载器按原版处理；
- 复用 `install_selected()` 安装对应游戏版本与加载器，再调用 `install_modpack()` 把内容写入隔离实例；
- 注册 `versions/<实例>/<实例>.json`，`inheritsFrom` 指向实际加载器 profile；`mcLauncher` 元数据记录整合包名、版本、加载器；
- 0.5.2 起版本目录本身就是游戏目录；`version_game_dir()` 在无 `.minecraft` 时回退到版本目录，版本隔离仍由 `mods/`、`.mc-launcher-modpack.json` 等标记识别；
- UI 安装完成自动刷新版本列表并选中新实例；CLI 打印启动命令；
- 新增“从压缩包安装…”，支持 `.mrpack` / 含 `modrinth.index.json` 的 ZIP；CLI `--modpack-archive`、`--modpack-name`、`--modpack-no-launch`；
- 已实网安装 Fabulously Optimized 5.2.8（Fabric 0.14.22 / 1.20.1 / 52 文件）到测试游戏目录，迁移到直接布局，修复 BMCLAPI Maven `+` 编码 404；
- 剩余：CurseForge `manifest.json`、实际进入标题界面由用户复验；NeoForge/Quilt 已在 0.6.0 补齐。

### 5.16 P15：0.6.0 限制（NeoForge / Quilt / 队列）

- **NeoForge 安装器 SHA-1**：BMCLAPI 的 NeoForge Maven 不提供 `.sha1`，官方 `maven.neoforged.net` 在本机 DNS 污染/不可达；启动器会警告并校验 ZIP 结构、`install_profile.json.minecraft` 与 `version.json.id`。这不是官方哈希校验，信任边界弱于 Forge。
- **NeoForge 版本映射**：`1.20.2+` 的版本前缀由 Minecraft 版本推导（`1.21.1 → 21.1.*`、`26.3 → 26.3.0.*`）；安装前会用安装器内部 `minecraft` 字段二次校验，但未知/未来版本仍可能没有匹配项。
- **Quilt 无镜像**：`meta.quiltmc.org` / `maven.quiltmc.org` 只能走官方，国内网络可能慢或失败；没有像 Fabric 那样的 BMCLAPI Meta 镜像。
- **队列不跨重启**：内存队列，关闭启动器即清空；暂停/取消按任务生效；Forge/NeoForge 安装器执行阶段不能暂停。
- **队列并发**：默认 1、UI 上限 3；同一目标文件有进程内锁，但不同任务仍共享带宽/磁盘。
- **即时安装与队列并存**：两者互不阻塞；同一游戏目录/版本同时操作仍可能产生重复校验或版本 JSON 竞争，建议一次只用一个入口。

---

## 6. 后续进展

### 6.1 立即待办

- [x] 0.4.0 Java 清单/组件与缓存
- [x] 0.4.0 确认后才下载（UI/CLI）
- [x] 0.4.0 选择组件/安装路径/下载源
- [x] 0.4.0 校验/补齐/安装记录
- [x] 0.4.0 导入已有 Java
- [x] 0.4.0 手动下载网址（Mojang + 发行版）
- [x] 0.4.0 无匹配 Java 提醒与启动前拦截
- [x] 19 个 Java 运行时测试 + 2 个回归测试（总计 90）
- [x] 真实网络 Java 17 安装、修复、校验
- [x] 零字节 `trusted.libraries` 修复与 `jre-legacy` 192 文件校验
- [x] Fabric 加载器列表官方/镜像并发修复
- [x] 更新 README/process/截图
- [x] 生成 `release\MinecraftLauncher-0.4.0-win64.zip`
- [x] 打包 exe 冒烟测试（标题 `Minecraft Launcher 0.4.0`）
- [x] 用户确认 0.4.0 测试完成、两个问题修复、功能确认完成
- [x] 0.5.0 `core/content.py`：Modrinth 搜索/分类/游戏版本/加载器/排序、版本选择、facets
- [x] 0.5.0 Mod/资源包/光影/数据包安装（确认、SHA-1、sidecar、路径安全）
- [x] 0.5.0 `.mrpack` 下载/解析/ZIP 安全/overrides/实例记录
- [x] 0.5.0 内容页 UI（筛选/结果/版本/依赖/目标/进度/取消/日志）
- [x] 0.5.0 CurseForge 可选 Key 适配与错误提示
- [x] 0.5.0 内容 CLI（搜索/版本/安装/dry-run/yes）
- [x] 61 个内容测试（总计 151）
- [x] Modrinth 实网五类搜索；JEI/资源包/光影/数据包下载与 SHA-1
- [x] Fabulously Optimized `.mrpack` 实网下载/解析
- [x] 更新 README/process/截图（content-tab/content-modpack/content-datapack；ui/download 更新）
- [x] 生成 `release\MinecraftLauncher-0.5.0-win64.zip`（13,556,619 字节 / 1007 条目）
- [x] exe 冒烟测试（标题 `Minecraft Launcher 0.5.0`，正常响应）
- [x] 0.5.1 `core/modpack.py`：`.mrpack` 依赖解析、隔离实例计划、版本 JSON、游戏+加载器编排
- [x] 0.5.1 整合包自动安装 Fabric/Forge/原版
- [x] 0.5.1 UI 安装后刷新并选中新版本；“从压缩包安装…”（.mrpack / ZIP）
- [x] 0.5.1 CLI `--modpack-archive`、`--modpack-name`、`--modpack-no-launch`
- [x] 0.5.1 修复 BMCLAPI Maven `+` 编码导致 Fabric 0.14.22 `sponge-mixin` 404
- [x] 0.5.1 新增 16 个测试（15 个整合包 + 1 个 Maven `+` 回归）
- [x] 0.5.1 实网完整安装 Fabulously Optimized 5.2.8 并启动到 Fabric 加载阶段
- [x] 0.5.1 发布 zip 与 exe 冒烟
- [x] 0.5.2 新安装直接以 `versions/<实例>` 为游戏目录（不再嵌套 `.minecraft`）
- [x] 0.5.2 0.5.1 嵌套旧实例兼容/修复；真实实例已迁移到直接布局
- [x] 0.5.2 新增 2 个布局测试（直接目录 + 旧布局复用；总计 169）
- [ ] 用户非沙箱复验 0.5.2 整合包实际进入标题界面
- [x] 0.5.2 发布 zip 与 exe 冒烟
- [x] 0.5.3 全下载移除 `segments=0`，内容/整合包/Java 使用配置分段数
- [x] 0.5.3 下载页/内容页/Java 窗口“暂停/继续”，保留 `.part`/分段
- [x] 0.5.3 取消删除本次下载文件与分片；暂停不删除
- [x] 0.5.3 新增 2 个暂停/取消测试（总计 171）
- [x] 0.5.3 发布 zip 与 exe 冒烟
- [x] 0.6.0 NeoForge legacy/现代版本映射、安装器 URL、BMCLAPI `/releases` 重写
- [x] 0.6.0 NeoForge installer 预取、结构校验、用户确认后执行
- [x] 0.6.0 Quilt Meta 列表/profile、库下载、`quilt-loader-<v>-<game>` 注册
- [x] 0.6.0 整合包依赖解析允许 NeoForge/Quilt 并注册可启动版本
- [x] 0.6.0 下载队列 core（顺序/并发/暂停/取消/失败隔离）与目标文件锁
- [x] 0.6.0 队列窗口、下载页/内容页/本地整合包入队
- [x] 0.6.0 新增 28 个测试（队列/加载器/锁/识别）；总计 200
- [x] 0.6.0 实网安装 NeoForge 1.20.1-47.1.106、NeoForge 21.11.48、Quilt 0.27.0 并深度/快速校验
- [x] 0.6.0 更新 README/process、版本 0.6.0
- [x] 0.6.0 发布 zip 与 exe 冒烟
- [x] 0.6.1 修复内容页游戏版本默认值（`base_game_version`）
- [x] 0.6.1 新增 1 个回归测试（总计 201）
- [x] 0.6.1 更新 README/process、重新打包与 exe 冒烟
- [x] 0.6.2 修复 Mojang 源下加载器客户端拒绝 BMCLAPI 镜像
- [x] 0.6.2 新增 1 个镜像允许列表回归测试（总计 202）
- [x] 0.6.2 更新 README/process、重新打包与 exe 冒烟
- [x] 0.6.3 加载器/整合包版本默认隔离（`inheritsFrom` 判定）
- [x] 0.6.3 更新隔离相关测试断言与 README/process
- [x] 0.6.3 重新打包与 exe 冒烟
- [ ] 用户非沙箱复验 NeoForge/Quilt 实际进入游戏与队列 UI
- [x] 0.7.0 `core/msauth.py`：设备代码、Xbox/XSTS/Minecraft 令牌链、刷新、错误映射
- [x] 0.7.0 `core/secure_store.py`：DPAPI 加密令牌、原子写、损坏检测、无明文回退
- [x] 0.7.0 GUI 账户行/微软登录对话框/自动刷新/退出登录
- [x] 0.7.0 CLI `--login-microsoft`/`--account-info`/`--logout-microsoft`/`--ms-client-id`/`--offline`
- [x] 0.7.0 启动参数使用正版 UUID/token/`userType=msa`；日志/CLI 输出对 `--accessToken` 打码
- [x] 0.7.0 新增 22 个测试（总计 224）；`compileall` 通过
- [x] 0.7.0 `.github/workflows/ci.yml`、`ci.ps1`、`build.ps1` Python 回退
- [x] 0.7.0 README/process 更新
- [ ] 用户完成微软浏览器登录，并以正版账户启动 1.20.1
- [ ] 用户确认后打包 0.7.0、更新 SHA-256/exe 冒烟

### 6.2 0.5.x 实施结果（0.5.0 → 0.5.3）

> 本节取代 0.5.0 的原始设计稿。旧稿中的“整合包只整理、不可启动”“内容只能单流下载”“暂停留到 0.6.0”等表述已经过时，统一以本节和 §6.4/§6.5/§6.6 的实施记录为准。

**用户需求**：增加 Mod、整合包、数据包、资源包、光影包下载；必须支持版本选择、搜索、分类和来源选择；后续追加整合包可启动、本地 `.mrpack`/ZIP 安装、所有下载多线程、暂停/继续、取消删除。

**实际完成**：

- **0.5.0 内容中心**：Modrinth 搜索/分类/游戏版本/加载器/排序/版本选择；CurseForge 可选 Key；Mod/资源包/光影/数据包安全安装；内容页与 CLI；SHA-1、sidecar、ZIP 安全。
- **0.5.1 整合包可启动**：解析 `.mrpack` 的 `dependencies`，自动安装 Minecraft + Fabric/Forge/原版，注册版本 JSON 并自动选中；本地 `.mrpack`/含 `modrinth.index.json` 的 ZIP 安装；NeoForge/Quilt 仅整理内容并标记 `launchable=false`。
- **0.5.2 直接版本目录**：新实例直接使用 `versions/<实例>/` 作为游戏目录，版本 JSON、`mods/`、`config/`、`saves/`、`.mc-launcher-modpack.json` 同级；0.5.1 的嵌套 `.minecraft` 旧实例仍可修复。
- **0.5.3 下载控制**：游戏文件、加载器依赖、Java、内容、整合包归档/内容全部使用配置的分段数；下载页/内容页/Java 窗口支持暂停/继续；取消删除本次下载文件、`.part` 和 `.part.segN`，暂停保留。
- **0.5.4 Fabric Maven 修复**：Fabric profile 库的 `maven.fabricmc.net` URL 优先替换为 BMCLAPI Maven，官方作为回退；修复 `intermediary` 等官方超时导致 Fabric 安装失败。

**安装布局**：

```text
versions/<实例>/
  <实例>.json              # inheritsFrom = 实际加载器 profile
  mods/ config/ saves/ ...  # 版本目录本身就是隔离游戏目录
  .mc-launcher-modpack.json
```

- 旧 `<game>/modpacks/` 仅保留给 0.5.0 兼容函数，不再作为新 UI/CLI 安装目标。
- 删除 `versions/<实例>` 会同时删除该整合包的 mods/config/saves，不影响共享 `libraries`/`assets` 或其他版本。

**下载控制语义**：

- 分段条件：文件 ≥ 8 MB 且服务器支持 `Range`；否则自动回退单流。分段失败也会回退，不会留下未校验的正式文件。
- 暂停：停止读取，保留 `.part`/`.part.segN`，继续时从断点恢复。
- 取消：停止所有并发任务，删除本次下载完成的文件、当前 `.part` 和分段；操作前已存在且校验通过的文件不在清理集合内。
- Forge 官方安装器执行阶段无法暂停，只能取消等待；这是安装器子进程的限制。

**测试与实网验证**：

- `py -m unittest discover -v`：**171 个测试通过**；`compileall` 通过。
- Fabulously Optimized 5.2.8（Fabric 0.14.22 / 1.20.1）实网完整安装：52 文件 / 25.8 MB + 27 overrides；迁移到直接版本目录；启动到 Fabric 加载阶段（日志 `Loading Minecraft 1.20.1 with Fabric Loader 0.14.22`）。
- 暂停/取消单元测试覆盖：暂停时线程阻塞且不落盘、继续后完成；暂停中取消删除 `.part`。
- 0.5.3 发布包与 exe 冒烟通过；实际进入标题界面/非沙箱复验仍待用户。

**0.6.0 已完成（原“仍未完成”项）**：

- NeoForge、Quilt 自动安装（见 §6.8）；
- 多任务下载队列（排队/并发上限/每任务暂停取消，见 §6.8）。

**仍未完成（后续版本）**：

- CurseForge `manifest.json` 整合包适配；
- Java lzma、微软登录、CI、Linux/macOS；
- Forge/NeoForge/Quilt 整合包的真实网络安装/启动验证（各加载器安装链路已分别实测，整合包编排由 fixture 覆盖）。

### 6.3 0.5.x 回归检查清单

#### 0.5.0

- [x] `py -m unittest discover -v`：151 个通过
- [x] `py -m compileall -q mc_launcher`
- [x] `--content-search jei --content-type mod --content-game-version 1.20.1 --content-loader fabric`（Modrinth 38 个结果）
- [x] `--content-versions jei ...`（212 个版本，默认 release `15.56.0.205`）
- [x] `--content-install jei ... --dry-run`（只打印计划，退出 0，不写文件）
- [x] 实网安装 JEI/Translations for Sodium/BSL/Terralith，SHA-1 全部通过
- [x] 数据包安装到临时世界 `saves/TestWorld/datapacks`
- [x] Fabulously Optimized `.mrpack` 下载/解析（52 文件 / 25.8 MB）
- [x] UI：内容页实网搜索、版本下拉、目标、安装按钮、安装完成
- [x] 截图 `content-tab.png`、`content-modpack.png`、`content-datapack.png`；`ui.png`、`download-tab.png` 更新为 0.5.0
- [x] 打包 zip 包含 README/screenshots，排除 debug.log/.venv
- [x] 打包 exe 冒烟：窗口标题 `Minecraft Launcher 0.5.0`
- [ ] 用户非沙箱复验内容下载/整合包

#### 0.5.1

- [x] `py -m unittest discover -v`：167 个通过
- [x] `py -m compileall -q mc_launcher tests`
- [x] Fabulously Optimized 5.2.8 实网完整安装（Fabric 0.14.22 / 52 文件 / 25.8 MB / 27 overrides）
- [x] `versions/Fabulously-Optimized-5.2.8/.minecraft` 隔离目录；marker `launchable=true`；49 mod / 26 config
- [x] `--list-versions --details` 显示 `Fabric 0.14.22` / 25.8 MB / 隔离
- [x] `--game-version Fabulously-Optimized-5.2.8 --dry-run` 使用隔离 `--gameDir`
- [x] 本地 `.mrpack` `--dry-run` 不创建游戏目录；UI“从压缩包安装…”按钮存在
- [x] 修复 BMCLAPI Maven `+` 编码 404 并加回归测试
- [x] 生成 `release\MinecraftLauncher-0.5.1-win64.zip`（13,578,431 字节 / 1007 条目；SHA-256 `304186B0D53AEBECADFE59B6BEC9DCC8E5E028495DC5A2AE81DF40B6DAC2D7F9`）
- [x] exe 冒烟：窗口标题 `Minecraft Launcher 0.5.1`，进程正常响应
- [ ] 用户非沙箱实际进入整合包游戏/复验本地压缩包安装

#### 0.5.2

- [x] `py -m unittest discover -v`：169 个通过
- [x] `py -m compileall -q mc_launcher tests`
- [x] 新建实例 `game_dir == version_dir`（`versions/<实例>`），不再创建 `.minecraft`
- [x] 0.5.1 嵌套 `.minecraft` 实例仍可识别并复用其游戏目录
- [x] 真实 `Fabulously-Optimized-5.2.8` 已从 `.minecraft` 迁移到版本目录；版本 JSON/marker/mods 同级
- [x] `--game-version Fabulously-Optimized-5.2.8 --dry-run` 的 `--gameDir` 指向版本目录
- [x] `--list-versions --details` 显示 `Fabric 0.14.22` 与隔离状态
- [x] 启动到 Fabric 加载阶段（`Loading Minecraft 1.20.1 with Fabric Loader 0.14.22`）
- [x] 生成 `release\MinecraftLauncher-0.5.2-win64.zip`（13,578,203 字节 / 1007 条目；SHA-256 `2189356657E25B24EEAF2BF9E8D4DA029D71051EB4C4BEC666B392191C7634D0`）
- [x] exe 冒烟：窗口标题 `Minecraft Launcher 0.5.2`，进程正常响应
- [ ] 用户非沙箱实际进入标题界面

#### 0.5.3

- [x] `py -m unittest discover -v`：171 个通过
- [x] `py -m compileall -q mc_launcher tests`
- [x] 内容/整合包/Java 下载路径不再出现 `segments=0`
- [x] 下载页、内容页、Java 窗口均有“暂停/继续”按钮
- [x] 暂停保留 `.part`/`.part.segN`，继续后完成
- [x] 暂停中取消删除 `.part`；取消删除本次下载文件与分片
- [x] 生成 `release\MinecraftLauncher-0.5.3-win64.zip`（13,583,536 字节 / 1007 条目；SHA-256 `EF9F8EB8F94258FBD2238FF197BC3D3E2C6EED1DB726866E5848E9449ABAE2F2`）
- [x] exe 冒烟：窗口标题 `Minecraft Launcher 0.5.3`
- [ ] 用户非沙箱复验暂停/取消与整合包进入标题界面

#### 0.5.4

- [x] `py -m unittest discover -v`：172 个通过
- [x] `py -m compileall -q mc_launcher tests`
- [x] Fabric profile 库 URL 优先 BMCLAPI Maven，官方回退
- [x] 1.21.11 + Fabric 0.19.3 的 `intermediary` URL 顺序验证
- [x] 生成 `release\MinecraftLauncher-0.5.4-win64.zip`（13,585,530 字节 / 1007 条目；SHA-256 `42B9265BFBB4360B140207BB73B6B85B2018D67E94BC5302C97D8F434E3D3AEC`）
- [x] exe 冒烟：窗口标题 `Minecraft Launcher 0.5.4`
- [ ] 用户复验 Fabric 1.21.11 安装

#### 0.6.0

- [x] `py -m unittest discover -v`：200 个通过
- [x] `py -m compileall -q mc_launcher tests`
- [x] NeoForge legacy（1.20.1-47.1.106）实网安装 + 3675 文件校验 + `--verify --fast`
- [x] NeoForge 现代（21.11.48 + 1.21.11）实网安装 + 4695 文件校验 + 启动 dry-run
- [x] Quilt（0.27.0 + 1.20.1）实网安装 + 3653 文件校验 + 版本详情
- [x] 队列核心测试与 UI 冒烟（入队/暂停/继续/完成刷新）
- [x] 生成 `release\MinecraftLauncher-0.6.0-win64.zip`（13,619,670 字节 / 1007 条目；SHA-256 `780C146E9BA7AFD3C139A334A9B6FC7B5DFD2CBB0E1526770D901D9D71744E1E`）
- [x] exe 冒烟：窗口标题 `Minecraft Launcher 0.6.0`
- [ ] 用户非沙箱复验 NeoForge/Quilt 进入游戏、队列实际下载

#### 0.6.1

- [x] `py -m unittest discover -v`：201 个通过
- [x] `py -m compileall -q mc_launcher tests`
- [x] 新增 `base_game_version()` 与回归测试（modpack → loader → vanilla）
- [x] 真实目录 UI 冒烟：内容页默认 `26.2`（选中 `fabulously-optimized-14.1.1`）
- [x] 生成 `release\MinecraftLauncher-0.6.1-win64.zip`（13,620,896 字节 / 1007 条目；SHA-256 `91972BA4739EE661F5AFC43B47F93DA31E367FF979AF4D095397E0E8F00D3008`）
- [x] exe 冒烟：窗口标题 `Minecraft Launcher 0.6.1`
- [ ] 用户复验内容页默认游戏版本与实际下载

#### 0.6.2

- [x] `py -m unittest discover -v`：202 个通过
- [x] `py -m compileall -q mc_launcher tests`
- [x] 新增加载器镜像允许列表回归测试
- [x] 真实网络验证 Mojang 源 + NeoForge 元数据成功
- [x] 生成 `release\MinecraftLauncher-0.6.2-win64.zip`（13,621,178 字节 / 1007 条目；SHA-256 `684E5E2945863670A9A3FE1387F828F08B18243ECBB89CE7FB0C040982606F20`）
- [x] exe 冒烟：窗口标题 `Minecraft Launcher 0.6.2`
- [ ] 用户复验 Mojang 源下 NeoForge 实际下载安装

#### 0.6.3

- [x] `py -m unittest discover -v`：202 个通过
- [x] `py -m compileall -q mc_launcher tests`
- [x] `has_isolated_game_data()` 把 `inheritsFrom` 版本视为默认隔离
- [x] 真实目录验证 NeoForge/Quilt/Forge/Fabric 均为隔离
- [x] `--dry-run` 验证 NeoForge/Quilt 的 `--gameDir` 指向版本目录
- [x] 生成 `release\MinecraftLauncher-0.6.3-win64.zip`（13,623,033 字节 / 1007 条目；SHA-256 `FBFE9A85CA3B697A883F8468923D8004892BBB9B9B3EC7161601378EAC0F54EC`）
- [x] exe 冒烟：窗口标题 `Minecraft Launcher 0.6.3`
- [ ] 用户复验 NeoForge/Quilt 实际启动不再读取共享 mods

#### 0.4.0 基线（保留，已于 2026-10-31 由用户确认）

- [x] 90 个测试通过；Java 17.0.15 403 文件 / 86.9 MB；`--verify-java` 403 通过
- [x] 未确认下载退出 130；`--dry-run` 只打印计划
- [x] `jre-legacy` 192 文件（含 0 字节）通过；Fabric 元数据并发修复
- [x] Java 管理 UI/CLI、截图、0.4.0 打包、exe 标题冒烟、用户确认

### 6.4 0.5.1 历史记录（嵌套 `.minecraft`，已被 0.5.2 取代）

**设计**：整合包不再放进 `<game>/modpacks/`，而是成为普通版本：

```text
versions/<实例>/
  <实例>.json              # inheritsFrom = 实际加载器 profile
  .minecraft/              # 隔离游戏目录
    mods/ config/ saves/ resourcepacks/ ...
    .mc-launcher-modpack.json
```

- `core/modpack.py` 负责依赖解析、实例计划、游戏+加载器安装、版本 JSON、内容-only 分支；
- `core/content.py` 的 `ModpackPlan`/`ModpackOutcome` 增加 `version_dir`/`version_id`/`launchable`/`loader_label`，marker 由 `write_modpack_marker()` 统一写入；
- `content_tab.py` 安装完成设置 `app._preferred_version` 并刷新版本列表；新增“从压缩包安装…”，读取索引后显示预览，确认才安装；
- `launcher.py` 新增 `--modpack-archive`、`--modpack-name`、`--modpack-no-launch`；`--content-install ... --content-type modpack` 也走可启动路径；
- 版本隔离完全复用 `version_game_dir()`/`has_isolated_game_data()`：看到 `.minecraft` 即使用隔离目录，删除版本会连同整合包内容一起删除。

**兼容**：旧 `build_modpack_plan()` 的 `<game>/modpacks` 路径与 `install_modpack()` 保留，避免破坏 0.5.0 测试/调用；新 UI/CLI 一律走 `modpack.py` 的隔离实例。

**验证命令**：

```powershell
py -m unittest discover -v                    # 167 passed
py -m compileall -q mc_launcher tests
py .\launcher.py --game-dir "D:\game\.minecraft" `
  --modpack-archive build\live-cache\fabulously-optimized.mrpack --dry-run
py .\launcher.py --game-dir "D:\game\.minecraft" `
  --game-version Fabulously-Optimized-5.2.8 --dry-run
py .\launcher.py --game-dir "D:\game\.minecraft" --list-versions --details
```

**实网结果**：Fabulously Optimized 5.2.8（Fabric 0.14.22 / 1.20.1）安装成功；52 个文件 / 25.8 MB + 27 个 overrides；`versions/Fabulously-Optimized-5.2.8/.minecraft` 含 49 个 mod、26 个 config 文件；marker `launchable=true`、`complete=true`；版本详情显示 `Fabric 0.14.22`；启动 dry-run 的 `--gameDir` 指向隔离目录；实际启动 25 s 进程保持运行，`logs/latest.log` 显示 `Loading Minecraft 1.20.1 with Fabric Loader 0.14.22`。

**修复**：Fabric 0.14.22 的 `sponge-mixin-0.12.5+mixin.0.8.5` 在 BMCLAPI Maven 上只有字面 `+` 路径，原 `_maven_url()` 把 `+` 编码成 `%2B` 导致 404；改为 `safe="/+"` 并加回归测试。

**剩余**：NeoForge/Quilt 自动安装、CurseForge `manifest.json`、实际进入游戏与非沙箱 exe 复验。

### 6.5 0.5.2 实施记录（直接版本目录）

**用户反馈**：安装时可以直接以 `versions/<实例>` 为游戏主要目录，不需要再添加 `.minecraft` 文件夹。

**改动**：

- `build_instance_plan()` 新实例的 `game_dir` 从 `version_dir / ".minecraft"` 改为 `version_dir`；
- 版本 JSON、`mods/`、`config/`、`saves/`、`.mc-launcher-modpack.json` 全部位于 `versions/<实例>/`；
- `ISOLATION_MARKERS` 增加 `.mc-launcher-modpack.json`，即使整合包为空也能识别为隔离版本；
- `_existing_instance_game_dir()` 先查版本目录、再查旧 `.minecraft`；0.5.1 实例修复时继续使用旧目录；
- UI 目标/确认文案和 CLI 目标改为版本目录本身；
- 测试更新：新布局 `game_dir == version_dir`、无 `.minecraft`；新增旧布局复用测试。

**验证**：

- `py -m unittest discover -v`：169 个通过；
- 真实 `Fabulously-Optimized-5.2.8` 已迁移：`.minecraft` 内容上移、删除空目录、marker `game_dir` 更新；
- `--dry-run` 的 `--gameDir` 为 `versions\Fabulously-Optimized-5.2.8`；
- 实际启动 25 s，日志仍为 `Loading Minecraft 1.20.1 with Fabric Loader 0.14.22`。

**兼容**：0.5.1 旧实例保留 `.minecraft` 布局，可继续修复/启动；新安装一律直接布局。

### 6.6 0.5.3 实施记录（多线程 / 暂停 / 取消清理）

**需求**：所有下载都能多线程；增加暂停；取消时删除已下载文件。

**实现**：

- `net.py` 新增 `PauseToken`，`download_file(pause=...)` 在打开连接前、单流读取每个 chunk、分段任务、分段合并时检查；暂停时阻塞并保留 `.part`/`.part.segN`，取消时抛出 `CancelledError` 并删除分片。
- 移除内容/Java 路径上的 `segments=0`；`get_content_source(segments=...)`、Java UI/CLI 都使用配置的分段数。
- `install_plan`/`install_selected`/`install_fabric`/`install_forge`/`prefetch_forge_libraries`/`install_java_runtime`/`install_content`/`install_modpack`/`modpack.py` 全链路增加 `pause` 参数。
- 取消清理：`download_file` 删除 `.part`/分段；`_download_tasks`、Java 安装、内容/整合包安装记录本次成功下载的路径并在取消时删除；已有且校验通过的文件不在清理集合内。
- UI：下载页、内容页、Java 管理窗口新增“暂停/继续”按钮；取消文案改为“本次下载的文件和未完成分片已删除”。
- 测试：新增暂停时线程阻塞、继续完成、暂停中取消删除 `.part` 的测试；总计 171。

**验证**：`py -m unittest discover -v` 171 通过；`py -m compileall -q mc_launcher tests` 通过；0.5.3 exe 窗口标题 `Minecraft Launcher 0.5.3` 冒烟通过。

**限制**：暂停/取消是按当前安装/下载操作生效；已经完成的旧文件不会因为后续取消而回滚。Forge 安装器执行阶段本身无法暂停，只能取消等待。

### 6.7 0.5.4 修复记录（Fabric Maven 超时）

**用户日志**：Fabric 1.21.11 + Loader 0.19.3 安装时，`intermediary-1.21.11.jar` 只尝试 `https://maven.fabricmc.net/...`，两次读取超时，导致安装失败。

**原因**：Fabric profile 的库条目使用 `library.url = https://maven.fabricmc.net/`；`_check_urls()` 只在所选源提供重写时生成镜像 URL。选择 Mojang 官方源时列表只有官方地址，BMCLAPI 没有进入回退链。

**修复**：`install._fabric_library_urls()` 对 `maven.fabricmc.net` 的库 URL 始终生成 BMCLAPI Maven 地址并排在第一位，官方地址保留为回退；SHA-1 仍会校验。验证 1.21.11 的 `intermediary` URL 顺序为 BMCLAPI → 官方；实际重跑安装成功，最终 4677 个文件校验通过。

**测试**：新增 SourcePolicy 回归测试，总计 172 个通过。

### 6.8 0.6.0 实施记录（NeoForge / Quilt + 下载队列）

**需求**：NeoForge / Quilt 自动安装；多任务下载队列（用户确认：队列覆盖内容下载，默认并发 1，NeoForge 无官方 SHA-1 时使用结构校验）。

**NeoForge 实现**：

- `mirrors.py` 重写表改为 `(host, base, strip_prefix)`，新增 `maven.neoforged.net/releases/...` → `bmclapi2.bangbang93.com/maven/...`。
- `loaders.py`：`neoforge_artifact()`（1.20.1 legacy / 其他现代）、`neoforge_version_prefixes()`（`1.21.1 → 21.1.`、`26.3 → 26.3.0.`）、`fetch_neoforge_builds()`、`neoforge_installer_official_url()`、`install_neoforge()`。
- 抽取 `_install_with_installer()`：原版前置 → 下载安装器（Forge 强制 SHA-1，NeoForge 可选）→ 读取 `install_profile.json` / `version.json` 校验目标与 ID → Java 选择 → 预取依赖 → 执行 `--installClient` → 本地补齐/校验。
- `prefetch_forge_libraries()` 支持 `fallback_maven` / `url_resolver` / `label`；NeoForge 依赖固定 BMCLAPI Maven 优先。首次预取失败（实测 `de.siegmar:fastcsv:2.0.0`）会再重试一轮。
- `_installer_sha1_optional()` 在镜像/官方都没有 `.sha1` 时返回 `None` 并写入警告；随后做 ZIP/JSON 结构校验。

**Quilt 实现**：

- `fetch_quilt_loaders()`（Meta v3，按版本排序，稳定通道取最新非 `-beta`）、`fetch_quilt_profile()`、`build_quilt_plan()`、`install_quilt()`。
- 复用 `_build_profile_plan()`；`maven.fabricmc.net` 库仍由 `_fabric_library_urls()` 镜像优先，`org.quiltmc:*` 走官方 Quilt Maven。

**整合包**：`LAUNCHABLE_LOADERS` 加入 `neoforge`/`quilt`，`resolve_modpack_dependencies()` 不再返回 `launchable=false`；`install_instance()` 通过 `install_selected()` 安装对应加载器。

**队列实现**：

- 新增 `core/taskqueue.py`：`TaskQueue` 调度线程、任务状态（queued/running/paused/cancelling/succeeded/failed/cancelled）、并发上限、每任务 `cancel`/`PauseToken`、进度快照、`stop()`。
- `net.py`：`destination_lock()` 按目标路径串行化 `download_file()`，避免同路径 `.part` 并发写；引用计数回收锁。
- UI：下载页“加入队列/下载队列…”；内容页“加入队列”；本地整合包确认框可加入队列；`_DownloadQueueDialog` 表格与暂停/继续/取消/移除/清空/并发控件；关闭窗口不停止队列；应用退出时取消并等待最多 5 秒。
- 队列覆盖：原版/加载器安装、Mod/资源包/光影/数据包、在线整合包、本地整合包。

**验证**：

- `py -m unittest discover -v`：**200 通过**；`py -m compileall -q mc_launcher tests` 通过。
- NeoForge 1.20.1-47.1.106：3675 文件全部通过；`--verify --fast` 通过；启动 dry-run 使用 Java 17。
- NeoForge 21.11.48 + 1.21.11：4695 文件全部通过；启动 dry-run 使用 Java 22。
- Quilt 0.27.0 + 1.20.1：3653 文件全部通过；`--list-versions --details` 显示 `Quilt`。
- 队列 UI 冒烟：窗口/表格/入队/暂停/继续/完成刷新正常。

**限制**：见 §5.16。

### 6.9 0.6.1 修复记录（内容页游戏版本默认值）

**问题**：打开“内容 / 资源”页时，游戏版本筛选默认显示所选实例 ID（例如 `fabulously-optimized-14.1.1`），而不是真正的 Minecraft 版本。

**原因**：`content_tab.refresh_context()` 直接使用 `app.selected_version.version_id` 初始化筛选值；对整合包/加载器实例，这个值是实例目录名，不是游戏版本。

**修复**：

- `core/version.py` 新增 `base_game_version(root, summary)`：沿 `inheritsFrom` 链取最后一个父版本（无父版本时返回自身 ID）。
- `content_tab` 使用 `base_game_version()`，并对 `root_info` 为空的情况回退到版本 ID。
- 新增回归测试：`test-pack-1.0 → fabric-loader-0.19.5-1.20.1 → 1.20.1`。

**验证**：真实目录 `fabulously-optimized-14.1.1` 内容页默认 `26.2`；`neoforge-21.11.48` → `1.21.11`；`quilt-loader-0.27.0-1.20.1` → `1.20.1`；201 个测试通过。

### 6.10 0.6.2 修复记录（Mojang 源下 NeoForge 元数据被允许列表拒绝）

**用户日志**：

```text
无法下载元数据。
  - https://bmclapi2.bangbang93.com/maven/net/neoforged/neoforge/maven-metadata.xml：
    下载地址不在允许的主机列表中：bmclapi2.bangbang93.com
  - https://maven.neoforged.net/releases/.../maven-metadata.xml：网络错误：timed out
```

**原因**：下载源选择“Mojang 官方源”时，`DownloadSource.allowed_hosts` 不包含 `bmclapi2.bangbang93.com`；但 NeoForge/加载器代码为保证国内可用，始终把 BMCLAPI Maven 放在第一顺位。`fetch_neoforge_builds()` 使用了调用方传入的严格客户端（只允许 Mojang 主机），于是镜像 URL 被 `validate_url` 拒绝，随后官方主机又超时。

**修复**：

- `mirrors.loader_allowed_hosts(source)`：加载器客户端允许“所选源 + BMCLAPI 镜像”，`mojang.allowed_hosts` 本身保持严格。
- `loaders._client()` 使用该集合并关闭严格重定向（BMCLAPI 会 302 到公网 CDN；重定向目标仍必须是公网 HTTPS）。
- `loaders._fabric_http()` 始终构造允许官方 Meta + BMCLAPI 的元数据客户端，不再复用调用方的严格客户端；NeoForge/Quilt 元数据与 profile 也改用它。
- UI 下载页/队列与 CLI 的安装客户端改用 `loader_allowed_hosts()`，确保 NeoForge 安装器和预取依赖能使用镜像。

**验证**：`get_source("mojang")` + 严格 `HttpClient` 调用 `fetch_neoforge_builds("1.21.11")` 返回 46 个版本（最新/稳定 `21.11.48`）；新增回归测试检查镜像 URL 通过允许列表；202 个测试通过。

### 6.11 0.6.3 修复记录（加载器版本读取共享 mods）

**问题**：启动 NeoForge / Quilt 版本时，游戏目录仍是 `D:\game\.minecraft`，因此会直接读取共享的 `.minecraft/mods`，而不是版本自己的 Mod。

**原因**：`has_isolated_game_data()` 只检查版本文件夹里是否已有 `mods/`、`saves/`、`logs/` 等游戏数据。刚安装的 NeoForge/Quilt/Forge/Fabric 版本只有版本 JSON 和安装清单，没有任何标记，于是“版本隔离”自动关闭，游戏回退到共享根目录。

**修复**：

- `paths.has_isolated_game_data()` 增加版本 JSON 判定：只要版本声明了 `inheritsFrom`（加载器/整合包 profile），就默认视为隔离版本。
- 因此 NeoForge、Quilt、Forge、Fabric 等版本即使尚未生成任何游戏数据，也默认使用 `versions/<版本>/` 作为游戏目录。
- 仍可手动取消“版本隔离”复选框，显式恢复共享 `.minecraft` 布局。
- 版本详情中的“隔离”列同步显示为“是”，与默认启动行为一致。

**验证**：

- `neoforge-21.11.48`、`quilt-loader-0.27.0-1.20.1`、`1.20.1-forge-47.4.0`、`fabric-loader-0.19.3-1.21.11` 的 `has_isolated_game_data` 均为 True。
- `--dry-run`：NeoForge 的 `--gameDir` 为 `F:\...\versions\neoforge-21.11.48`，Quilt 为 `F:\...\versions\quilt-loader-0.27.0-1.20.1`，`isolated: true`。
- 202 个测试通过；`compileall` 通过。

### 6.12 0.6.3 用户验收（2026-10-31）

- 用户确认 **0.6.3 在沙箱外可正常启动**。
- 0.6.3 的加载器默认隔离修复生效：NeoForge/Quilt 使用 `versions/<版本>/` 作为游戏目录，不再读取共享 `.minecraft/mods`。
- 当前发布包 `release\MinecraftLauncher-0.6.3-win64.zip`（13,623,033 字节 / SHA-256 `FBFE9A85CA3B697A883F8468923D8004892BBB9B9B3EC7161601378EAC0F54EC`）即为用户复验的版本。
- 下一阶段尚未开始：Java lzma、微软登录、CI、Linux/macOS、CurseForge `manifest.json`、Forge 旧版本/Java 8 实网验证、NeoForge/Quilt 整合包实网安装与队列持久化等。

### 6.13 0.7.0 候选实施记录（微软登录 / CI，2026-10-31）

**用户决策**：按方案 B 推进——CI + 微软 OAuth 全链路（含 UI），用户提供 Azure client ID 并由用户完成实网登录复验；Java lzma 暂不启用。

**Azure client ID**：`<client-id>`（公开标识，不是密钥；登录成功后会写入本机 `config.json` 的 `ms_client_id`，也可用 `--ms-client-id` 或 `MC_LAUNCHER_MS_CLIENT_ID`）。真实 `devicecode` 请求成功，说明公共客户端流和个人账户端点配置正确。

**微软登录实现**：

- `core/msauth.py`：`request_device_code` → `poll_device_code`（`authorization_pending`/`slow_down`/`expired_token`/`authorization_declined`）→ `authenticate_xbox` → `authorize_xsts` → `login_with_xbox` → `fetch_profile`；`refresh_account`/`ensure_account_fresh` 在启动前刷新；XSTS `2148916233`（无 Xbox 账户）、`2148916238`（儿童账户）、`2148916235`（地区）映射为中文提示；profile 404 表示没有 Java 版。
- `core/secure_store.py`：`CryptProtectData`/`CryptUnprotectData`（ctypes，当前 Windows 用户 + 固定 entropy），原子写 `%APPDATA%\MinecraftLauncher\account.dat`；DPAPI 不可用或解密失败时不落明文，只保留内存会话。
- `core/settings.py`：`config.json` 读写与 client ID 优先级（显式参数 > 环境变量 > 配置）。
- `core/models.py`：新增 `MicrosoftAccount`（token 字段 `repr=False`）与 `LaunchOptions.account`。
- `core/launch.py`：MSA 使用 `account.uuid`/`account.access_token`/`userType=msa`/`auth_xuid`/`clientid`；离线仍用 `offline_uuid_hex` 和 `accessToken=0`。
- GUI：账户行“微软登录…/退出登录”、`_MicrosoftLoginDialog`（后台线程轮询、显示/复制代码、打开浏览器、取消）、登录后锁定玩家名、启动前自动刷新、刷新失败提示重新登录。
- CLI：`--login-microsoft`、`--account-info`、`--logout-microsoft`、`--ms-client-id`、`--offline`；`--dry-run` 输出 `account` 字段，`command`/`command_line` 的 `--accessToken` 打码。
- 安全：认证主机独立于下载允许列表（`login.microsoftonline.com`、`user.auth.xboxlive.com`、`xsts.auth.xboxlive.com`、`api.minecraftservices.com`）；POST 仍走 HTTPS/主机/重定向/公网地址检查；token 不写 config/日志；UI 启动命令日志打码。

**CI 实现**：

- `.github/workflows/ci.yml`：Windows runner，Python 3.11/3.13 matrix，`python -m unittest discover -v` + `compileall`；`package` job 在测试通过后 `pip install -r requirements-dev.txt`、`build.ps1 -NoZip`、上传 `dist/MinecraftLauncher`。
- `ci.ps1`：本地等价 test job（`py`）。
- `build.ps1`：新增 `-Python` 参数；无 `.venv` 时回退到 `python`，供 CI 使用。
- 限制：工作区没有 git 远程仓库，Actions 尚未实际执行；需用户推送后验证。

**Java lzma 评估（本轮不添加）**：

| 组件 | raw | lzma 覆盖 | 实际下载 | 节省 |
|---|---:|---:|---:|---:|
| `java-runtime-gamma` | 86.9 MB | 31.8 MB（185/403 文件） | 66.9 MB | 23.0% |
| `jre-legacy` | 143.2 MB | 131.9 MB（158/192 文件） | 43.0 MB | 70.0% |
| `java-runtime-delta` | 94.5 MB | 35.3 MB（187/402 文件） | 71.5 MB | 24.3% |

结论：不是功能必需；Java 17 节省有限，收益主要在 Java 8；会新增压缩下载、流式解压、解压后 raw SHA-1/大小校验、原子替换、暂停/取消/清理和解压炸弹上限。本轮保持 raw 路径，后续若做需默认 raw 回退并在新临时目录实网验证。

**验证**：

- `py -m unittest discover -v`：224 通过；`py -m compileall -q mc_launcher tests` 通过。
- 真实 `devicecode` 请求成功（`https://www.microsoft.com/link`，900 秒有效）；用户完成浏览器登录后，Microsoft/Xbox/XSTS 链通过，`login_with_xbox` 返回 **HTTP 403**，响应体 `{"errorMessage":"Invalid app registration","path":"/authentication/login_with_xbox"}`。账号截图确认拥有 Minecraft: Java，因此原因是 **Azure 应用未获 Minecraft Services allowlist 批准**，不是账号所有权；未保存令牌。代码已把该错误单独映射为可操作提示。
- CLI `--dry-run`：离线/临时 DPAPI 微软账户两种路径均成功，token 已打码。
- UI 冒烟：账户行状态正确；登录对话框已创建；完整成功登录待 allowlist 批准后复验。
- 上传前脱敏：`screenshots/ui.png` 已把玩家名 `<player>` 和 `F:\game...` 替换为 `Steve` / `D:\game\.minecraft`；README/process 的测试路径改为通用路径；真实 client ID 从文档/测试移到 `<client-id>`/假 GUID。
- 未打包 0.7.0；0.6.3 发布包/SHA 未变。

**Minecraft AppID 审核（外部阻塞）**：

- 当前官方入口：[Java Edition Game Service API Review or Application Process](https://help.minecraft.net/hc/en-us/articles/16254801392141-Java-Edition-Game-Service-API-Review-or-Application-Process)。
- Microsoft Q&A 上开发者给出的当前申请表单：<https://forms.cloud.microsoft/Pages/ResponsePage.aspx?id=v4j5cvGGr0GRqy180BHbR-ajEQ1td1ROpz00KtS8Gd5UNVpPTkVLNFVROVQxNkdRMEtXVjNQQjdXVC4u>（来自 2026-08 的 Minelys 案例，提交后由 Mojang Enforcement 邮件确认 allowlist）。
- 需提交应用名称、Azure client ID、用途、分发方式等；批准前同一个 client ID 始终 403。不要复用其他启动器的 client ID。
- 批准后无需改代码，直接用 `<client-id>` 重试即可。

**剩余 / 下一步**：

1. 按上面入口提交 client ID，等待 allowlist 批准；
2. 批准后运行 `py .\launcher.py --login-microsoft --ms-client-id <client-id>`；
3. `py .\launcher.py --account-info` 确认账户；GUI 启动 1.20.1 并确认正版服务器/皮肤；
4. 登录验收通过后升 `APP_VERSION=0.7.0`、打包、更新 SHA-256/exe 冒烟/截图；
5. 之后再做 Java lzma（可选）、Linux/macOS、CurseForge `manifest.json`、Forge 旧版本/Java 8、队列持久化。

---

## 7. 交接检查清单

- [x] 安全 HTTP 层
- [x] Mojang/BMCLAPI 源选择
- [x] 正式版/快照/远古版本
- [x] 原版下载
- [x] 完整性检查/补齐
- [x] 续传/重试/进度加固（0.2.1）
- [x] Fabric 安装
- [x] Forge 安装 + 镜像预取 + 启动修复（0.2.2）
- [x] 旧版 virtual/map_to_resources
- [x] UI 下载页
- [x] 版本管理（详情/打开/删除，0.3.0）
- [x] 缓存清理（统计/分类/保护，0.3.0）
- [x] 多线程分段下载（回退/取消/续传，0.3.0）
- [x] Java 运行时清单/组件（0.4.0）
- [x] Java 用户确认后下载、可选路径/源、取消（0.4.0）
- [x] Java 校验/修复/扫描/导入/手动网址（0.4.0）
- [x] Java UI/CLI/无匹配提醒（0.4.0）
- [x] 内容源 Modrinth 搜索/分类/版本（0.5.0）
- [x] 内容安装 Mod/资源包/光影/数据包/整合包（0.5.0）
- [x] 内容页 UI + CLI（0.5.0）
- [x] CurseForge 可选 Key（0.5.0，未实网验收）
- [x] 整合包可启动：依赖解析 + 游戏/加载器 + 隔离版本（0.5.1/0.5.2）
- [x] 本地 `.mrpack`/ZIP 安装 UI + CLI（0.5.1/0.5.2）
- [x] 新实例直接以 `versions/<实例>` 为游戏目录（0.5.2）
- [x] 0.5.1 嵌套 `.minecraft` 旧实例兼容（0.5.2）
- [x] BMCLAPI Maven `+` 编码修复（0.5.1）
- [x] 全下载使用配置的分段数（0.5.3）
- [x] 暂停/继续：下载页/内容页/Java（0.5.3）
- [x] 取消删除本次文件与分片（0.5.3）
- [x] Fabric Maven 库优先 BMCLAPI（0.5.4）
- [x] NeoForge 自动安装（legacy + 现代，0.6.0）
- [x] Quilt 自动安装（0.6.0）
- [x] 整合包支持 NeoForge/Quilt 可启动（0.6.0）
- [x] 多任务下载队列（core/UI/内容/整合包，0.6.0）
- [x] 目标文件锁（0.6.0）
- [x] 内容页游戏版本默认值修复（0.6.1）
- [x] 加载器镜像允许列表修复（0.6.2）
- [x] 加载器版本默认隔离（0.6.3）
- [x] CLI
- [x] 202 个测试
- [x] README/process.md 更新（截图沿用 0.5.0）
- [x] 0.5.4 发布 zip（13,585,530 字节 / 1007 条目；SHA-256 `42B9265BFBB4360B140207BB73B6B85B2018D67E94BC5302C97D8F434E3D3AEC`）
- [x] 0.5.4 exe 窗口标题冒烟（`Minecraft Launcher 0.5.4`）
- [x] 0.6.0 发布 zip 与 SHA-256（见 §2.1）
- [x] 0.6.0 exe 窗口标题冒烟（`Minecraft Launcher 0.6.0`）
- [x] 0.6.1 发布 zip 与 SHA-256（见 §2.1）
- [x] 0.6.1 exe 窗口标题冒烟（`Minecraft Launcher 0.6.1`）
- [x] 0.6.2 发布 zip 与 SHA-256（见 §2.1）
- [x] 0.6.2 exe 窗口标题冒烟（`Minecraft Launcher 0.6.2`）
- [x] 0.6.3 发布 zip 与 SHA-256（见 §2.1）
- [x] 0.6.3 exe 窗口标题冒烟（`Minecraft Launcher 0.6.3`）
- [x] 0.7.0 候选：微软登录 core/GUI/CLI/DPAPI/22 个测试（总计 224）
- [x] 0.7.0 候选：CI workflow、`ci.ps1`、`build.ps1` Python 回退
- [x] 0.7.0 候选：README/process 更新
- [ ] 0.7.0 候选：用户微软浏览器登录 + 正版 1.20.1 启动复验
- [ ] 0.7.0 打包/SHA-256/exe 冒烟（登录验收后）
- [x] 任务列表已清空（§0.5）
- [x] 非沙箱双击 0.4.0 exe 完整复验（用户确认）
- [x] 非沙箱双击 0.6.3 exe / NeoForge / Quilt 读取自身 mods 复验（用户确认可正常启动）

0.6.3 修复加载器版本默认读取共享 `.minecraft/mods` 的问题，并保留 0.6.2 的加载器镜像修复、0.6.1 的内容页默认版本修复、0.6.0 的 NeoForge/Quilt 自动安装与下载队列。0.7.0 候选新增微软登录与 CI，224 个测试通过；微软登录被 `403 Invalid app registration` 阻塞（Azure 应用未获 allowlist），需提交 AppID 审核后复验并打包。后续未完成项见 §6.2 / §6.13。

> 0.6.3 发布包 SHA-256：`FBFE9A85CA3B697A883F8468923D8004892BBB9B9B3EC7161601378EAC0F54EC`（13,623,033 字节 / 1007 条目）
> 0.6.2 发布包 SHA-256：`684E5E2945863670A9A3FE1387F828F08B18243ECBB89CE7FB0C040982606F20`（13,621,178 字节 / 1007 条目）
> 0.6.1 发布包 SHA-256：`91972BA4739EE661F5AFC43B47F93DA31E367FF979AF4D095397E0E8F00D3008`（13,620,896 字节 / 1007 条目）
> 0.6.0 发布包 SHA-256：`780C146E9BA7AFD3C139A334A9B6FC7B5DFD2CBB0E1526770D901D9D71744E1E`（13,619,670 字节 / 1007 条目）
> 0.5.4 发布包 SHA-256：`42B9265BFBB4360B140207BB73B6B85B2018D67E94BC5302C97D8F434E3D3AEC`（13,585,530 字节 / 1007 条目）
> 0.5.3 发布包 SHA-256：`EF9F8EB8F94258FBD2238FF197BC3D3E2C6EED1DB726866E5848E9449ABAE2F2`（13,583,536 字节 / 1007 条目）
> 0.5.2 发布包 SHA-256：`2189356657E25B24EEAF2BF9E8D4DA029D71051EB4C4BEC666B392191C7634D0`（13,578,203 字节 / 1007 条目）
> 0.5.1 发布包 SHA-256：`304186B0D53AEBECADFE59B6BEC9DCC8E5E028495DC5A2AE81DF40B6DAC2D7F9`（13,578,431 字节 / 1007 条目）
> 0.5.0 发布包 SHA-256：`FE824BA317FFF9C102AEE1F940E10DE6E69647A98853EDE658DD3331076160AF`（13,556,619 字节 / 1007 条目）
> 0.4.0 发布包 SHA-256：`76B9E6C2BD69B6C92B43860B63CDE0C9613D975E3422EBB0B2FFDA2A2EBCD9DD`
