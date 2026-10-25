package io.github.hyperisland.xposed.diag

import android.os.Build
import io.github.hyperisland.xposed.ConfigManager
import io.github.hyperisland.xposed.logError
import io.github.hyperisland.xposed.logWarn
import io.github.hyperisland.xposed.utils.HookUtils
import io.github.libxposed.api.XposedModule
import io.github.libxposed.api.XposedModuleInterface.PackageLoadedParam

/**
 * 兼容性自检：把「功能为什么没生效」的判定依据一次性打到 logcat。
 *
 * 用 WARN 级别是刻意的：模块里绝大部分诊断走 BaseHook.log / logDebug，
 * 需要先在 App 里打开「调试日志」；而如果 App 侧配置根本读不进来，
 * 这个开关永远拿不到 true，形成死锁。这里绕开开关直接输出。
 *
 * 输出：
 *   1) [env]  环境：SDK、远程配置通道是否可用、两个开关的实际生效值；
 *   2) [api]  超级岛 hook 目标的类/成员逐个探测结果。
 *
 * 只读，不修改任何既有行为。
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

    /** 运行时要逐一确认的 hook 目标：类名 -> 需要存在的成员（方法或字段）名。 */
    private val API_CHECKS: List<Pair<String, List<String>>> = listOf(
        CONTENT_VIEW to listOf(
            "isExpanded", "backgroundView", "state",
            "updateMedianLuma", "updateDarkLightMode", "updateBackgroundBg",
            "getBigIslandView", "getExpandedView",
    ),
        BACKGROUND_VIEW to listOf("setDrawable", "getDrawable", "getStokeWidth"),
        GLOW_VIEW to listOf(
            "startGlowEffect", "stopGlowEffect", "setGlowEffectPosition",
            "initGlowEffect", "setAlphaOfGlowEffect",
    ),
        FOCUS_CONTROLLER to listOf("setUpDynamicIslandDataBundle"),
        ABS_SHADER to listOf("readRawString"),
    )

    @Volatile private var probed = false

    fun report(module: XposedModule, param: PackageLoadedParam) {
        if (param.packageName != "com.android.systemui") return
        reportEnvironment(module, param)
        HookUtils.hookDynamicClassLoaders(module, ClassLoader.getSystemClassLoader()) { loader ->
            probeIslandApi(module, loader)
        }
    }

    private fun reportEnvironment(module: XposedModule, param: PackageLoadedParam) {
        val readable = ConfigManager.getString("pref_default_island_outer_glow", MISSING) != MISSING
        val islandMode = ConfigManager.getString("pref_island_outline_mode", MISSING)
        val focusMode = ConfigManager.getString("pref_focus_outline_mode", MISSING)
        val glow = ConfigManager.getString("pref_default_island_outer_glow", MISSING)
        val debug = ConfigManager.getBoolean("pref_debug_log", false)

        val lines = listOf(
            "sdk=${Build.VERSION.SDK_INT} release=${Build.VERSION.RELEASE}",
            "remotePrefs=" + if (readable) "ok" else "UNAVAILABLE(所有开关回退默认值)",
            "island_outline_mode=" + show(islandMode),
            "focus_outline_mode=" + show(focusMode),
            "default_island_outer_glow=" + show(glow),
            "debug_log=$debug",
            "hostLoader: contentView=${probe(param.defaultClassLoader, CONTENT_VIEW)}" +
                " backgroundView=${probe(param.defaultClassLoader, BACKGROUND_VIEW)}" +
                " glowView=${probe(param.defaultClassLoader, GLOW_VIEW)}" +
                " focusController=${probe(param.defaultClassLoader, FOCUS_CONTROLLER)}" +
                " absShader=${probe(param.defaultClassLoader, ABS_SHADER)}",
        )
        module.logWarn(TAG, "[env] " + lines.joinToString(" | "))
        if (!readable) {
            module.logError(
                TAG,
                "读不到模块配置：当前框架不支持 XposedModule.getRemotePreferences" +
                    "(libxposed api 102)。App 内开关无法传入 SystemUI 进程，全部按默认值处理；" +
                    "而『常驻岛轮廓』『外圈光效』的默认值都是关闭，于是表现为完全无反应。" +
                    "请升级 LSPosed 到支持 libxposed 102 的版本。",
            )
        }
    }

    private fun probeIslandApi(module: XposedModule, loader: ClassLoader) {
        if (probed) return
        if (!HookUtils.isIslandLoaderReady(loader)) return
        probed = true

        val lines = API_CHECKS.map { (className, members) ->
            val short = className.substringAfterLast('.')
            val clazz = runCatching { Class.forName(className, false, loader) }.getOrNull()
            if (clazz == null) {
                "$short=CLASS_MISSING"
            } else {
                val available = mutableSetOf<String>()
                collectMembers(clazz, available)
                val text = members.joinToString(",") { m ->
                    // Kotlin internal 成员带 $<module>_release 后缀，用前缀匹配
                    val hit = available.any { it == m || it.startsWith(m + "$") }
                    "$m=${if (hit) "OK" else "MISSING"}"
                }
                "$short: $text"
            }
        }
        module.logWarn(TAG, "[api] " + lines.joinToString(" | "))
    }

    private fun collectMembers(clazz: Class<*>, out: MutableSet<String>) {
        var current: Class<*>? = clazz
        while (current != null) {
            out.addAll(current.declaredMethods.map { it.name })
            out.addAll(current.declaredFields.map { it.name })
            current = current.superclass
        }
    }

    private fun probe(loader: ClassLoader, className: String): String = runCatching {
        Class.forName(className, false, loader)
        "found"
    }.getOrDefault("missing")

    private fun show(value: String): String = if (value == MISSING) "?" else "'$value'"
}
