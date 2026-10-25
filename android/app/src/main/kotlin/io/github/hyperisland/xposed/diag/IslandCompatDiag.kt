package io.github.hyperisland.xposed.diag

import android.os.Build
import android.util.Log
import io.github.hyperisland.xposed.ConfigManager
import io.github.libxposed.api.XposedModule
import io.github.libxposed.api.XposedModuleInterface.PackageLoadedParam

/**
 * 一次性兼容性自检：把「Hook 为什么看起来没生效」的常见静默原因显式写进 logcat。
 *
 * 常驻轮廓 / 外圈光效失效通常只有三个原因，且在不开日志时都表现为「开关没反应、也不报错」：
 *   1. 读不到 App 侧配置：XposedModule.getRemotePreferences 不可用时，ConfigManager 只打一条
 *      WARN 就继续运行，而相关开关的默认值恰好都是「关」，于是现象像功能坏了；
 *   2. 超级岛类不在宿主的 ClassLoader 里（它们来自 miui.systemui.plugin 插件 dex），
 *      HookUtils 的 loader 发现失败会导致 hook 根本没安装，失败路径是
 *      ClassNotFoundException 被 runCatching 静默吞掉；
 *   3. 运行时流程差异（extras 桥接 / state 判定 / 材质替换）。
 *
 * 只读诊断，不修改任何既有行为。
 */
object IslandCompatDiag {

    private const val TAG = "HyperIsland[CompatDiag]"
    private const val MISSING = "__unreadable__"

    private const val CONTENT_VIEW =
        "miui.systemui.dynamicisland.window.content.DynamicIslandBaseContentView"
    private const val BACKGROUND_VIEW =
        "miui.systemui.dynamicisland.DynamicIslandBackgroundView"
    private const val GLOW_VIEW =
        "miui.systemui.dynamicisland.view.DynamicGlowEffectView"
    private const val FOCUS_CONTROLLER =
        "miui.systemui.notification.focus.FocusNotificationController"
    private const val ABS_SHADER =
        "com.mi.widget.core.AbsShader"

    @Volatile private var reported = false

    fun report(module: XposedModule, param: PackageLoadedParam) {
        if (param.packageName != "com.android.systemui") return
        if (reported) return
        reported = true

        val readable = prefsReadable()
        val lines = listOf(
            "sdk=${Build.VERSION.SDK_INT} release=${Build.VERSION.RELEASE}",
            "remotePrefs=" + if (readable) "ok" else "UNAVAILABLE(开关全部回退默认值)",
            "island_outline_mode=" + read("pref_island_outline_mode"),
            "focus_outline_mode=" + read("pref_focus_outline_mode"),
            "legacy_always_show_island_outline=${ConfigManager.getBoolean("pref_always_show_island_outline", false)}",
            "default_island_outer_glow=" + read("pref_default_island_outer_glow"),
            "hostLoader: contentView=${probe(param.defaultClassLoader, CONTENT_VIEW)}" +
                " backgroundView=${probe(param.defaultClassLoader, BACKGROUND_VIEW)}" +
                " glowView=${probe(param.defaultClassLoader, GLOW_VIEW)}" +
                " focusController=${probe(param.defaultClassLoader, FOCUS_CONTROLLER)}" +
                " absShader=${probe(param.defaultClassLoader, ABS_SHADER)}",
            "note: 超级岛类通常来自 miui.systemui.plugin 插件 dex，" +
                "hostLoader 全为 missing 属正常，实际结果请配合 HyperIsland[LoaderDiscovery] 判断",
        )
        val text = lines.joinToString(" | ")
        module.log(Log.WARN, TAG, text)

        if (!readable) {
            module.log(Log.ERROR, 
                TAG,
                "无法读取模块配置：当前框架不支持 RemotePreferences(libxposed api 102)。" +
                    "请升级 LSPosed 到支持 libxposed 102 的版本，否则 App 内的开关无法传递到 SystemUI 进程，" +
                    "默认值全部为关闭。ConfigManager 的详细原因见上方 HyperIsland[ConfigManager] 日志。",
            )
        }
    }

    /** 远程配置不可用时，所有读取都会返回调用方传入的默认值。 */
    private fun prefsReadable(): Boolean =
        ConfigManager.getString("pref_default_island_outer_glow", MISSING) != MISSING

    private fun read(key: String): String {
        val value = ConfigManager.getString(key, MISSING)
        return if (value == MISSING) "unset" else value
    }

    private fun probe(loader: ClassLoader, className: String): String = runCatching {
        Class.forName(className, false, loader)
        "found"
    }.getOrDefault("missing")
}
