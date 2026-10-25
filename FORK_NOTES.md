# Fork 说明（by0212zy）

本仓库 fork 自 [1812z/HyperIsland](https://github.com/1812z/HyperIsland)，用途是在**澎湃 OS 2 / Android 15** 上定位「常驻显示岛轮廓」「外圈光效」静默失效的原因，并提供可持续同步上游的构建环境。

新增内容只有两处，**不改动任何既有 Hook 逻辑**，因此后续合并上游更新基本不会冲突：

| 文件 | 作用 |
| --- | --- |
| `android/app/src/main/kotlin/io/github/hyperisland/xposed/diag/IslandCompatDiag.kt` | 一次性兼容性自检，把静默失败写成 logcat 日志 |
| `.github/workflows/compat-build.yml` | 手动构建 APK 并上传 Actions 工件（不创建 Release、不推 TG） |
| `.github/workflows/sync-upstream.yml` | 手动或每周拉取上游更新，冲突时自动开 PR |
| `tools/superisland_scan.py` | 扫描 ROM 里的超级岛实现类在哪个 APK / dex |
| `tools/dexdump_members.py` | 免 apktool/jadx 列出类成员签名，用于核对 hook 目标是否漂移 |

---

## 分支

- `main`：基于上游 `v3.2.2`（最新）
- `compat-v3.0.3`：基于上游 `v3.0.3`，配置键按该版本的 `pref_always_show_*` 读取

---

## 怎么构建

1. 打开仓库 **Actions** → **Build (fork, keep local patches)** → **Run workflow**
2. 右上角 *Use workflow from* 选择分支（`main` 或 `compat-v3.0.3`）
3. `version_suffix` 默认 `-diag`（便于和官方包区分），`flavor` 选 `release`
4. 跑完后在该次运行的 **Artifacts** 里下载 APK

签名使用本 fork 自己的 keystore（已存入 Actions secrets），版本之间保持一致，可直接覆盖安装。

---

## 怎么同步上游

- 自动：每周一 UTC 18:00 运行 **Sync Upstream**，能自动合并的直接推到 `main`
- 手动：Actions → **Sync Upstream** → Run workflow，填 `upstream_ref`（如 `main` 或 `v3.3.0`）
- 有冲突时工作流会 `git merge --abort` 并自动开一个 PR，人工解决冲突后合并即可

---

## 诊断怎么用

安装带 `-diag` 后缀的 APK，重启作用域后执行：

```bash
adb logcat -v brief | grep -E "HyperIsland\[CompatDiag\]|HyperIsland\[ConfigManager\]|HyperIsland\[LoaderDiscovery\]"
```

会得到一行 `HyperIsland[CompatDiag]`，形如：

```
sdk=35 release=15 | remotePrefs=ok | island_outline_mode=show |
focus_outline_mode=system | default_island_outer_glow=on |
hostLoader: contentView=missing backgroundView=missing ... |
note: 超级岛类通常来自 miui.systemui.plugin 插件 dex ...
```

### 判读

| 现象 | 结论与处理 |
| --- | --- |
| `remotePrefs=UNAVAILABLE`，并跟随一条 `无法读取模块配置` 的 ERROR | 当前 LSPosed 不支持 `XposedModule.getRemotePreferences`（libxposed api 102）。App 里的开关传不进 SystemUI 进程，全部回退默认值（而这两个开关默认就是「关」）。升级 LSPosed 到支持 libxposed 102 的版本即可，不需要改代码 |
| `remotePrefs=ok`，但开关值仍显示 `system` / `unset` / `off` | 配置确实没写进去。打开 HyperIsland App 重新开关一次，再重启作用域 |
| `hostLoader` 全 `missing` 且日志里没有 `background=found` | 插件 ClassLoader 没被发现，hook 未安装（需进一步加固 `HookUtils`） |
| `background=found` 且出现 `hooked updateDarkLightMode` / `hooked background setDrawable`（需开 App 内「调试日志」） | hook 已安装正常，属于运行时流程差异，需要结合场景日志继续查 |

---

## 已完成的静态核对（针对 `2.0.212 MiuiSystemUI` + `MIUISystemUIPlugin`）

- 超级岛的 8 个 hook 目标类**全部存在于 `MIUISystemUIPlugin.apk`**，宿主 SystemUI 里没有（正常，它在插件 dex 里）
- 方法签名与 HyperIsland 预期完全一致：`isExpanded()`、`backgroundView`/`state` 字段、`updateMedianLuma(float)`、`updateDarkLightMode(State,String,boolean,boolean)`、`setDrawable(Drawable)`、`onStateChange(ContentView)`、`setUpDynamicIslandDataBundle(SBN)→Bundle`、`updateViewForAvoidingScreenBurnIn(ContentView,int)`
- 字节码层面确认：系统在 `updateDarkLightMode()` / `updateMedianLuma()` 里对 `Resources.getDrawable()` 得到的 **GradientDrawable** 调用 `setStroke()`，与 `IslandOutlineHook` 的前提完全一致
- `DynamicIslandBigIslandView extends DynamicGlowEffectView`，与「大岛 View 即光效 View」的假设一致

结论：**不需要做类名适配**，问题在运行时链路（配置传递或 loader 发现）。
