#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
superisland_scan.py —— 判断一台小米/澎湃系统的 ROM 里，「小米超级岛」代码到底在哪个 APK、类名是什么。

它是为了回答这类问题而写的：
  HyperIsland 的「常驻显示岛轮廓」「外圈光效」为什么在某台机器上完全没反应？

原理
----
HyperIsland 的这两个功能必须 hook 到下面这些 SystemUI 内部类（缺一不可）：

  miui.systemui.dynamicisland.window.content.DynamicIslandBaseContentView
  miui.systemui.dynamicisland.DynamicIslandBackgroundView
  miui.systemui.dynamicisland.DynamicFeatureConfig
  miui.systemui.dynamicisland.anim.DynamicIslandAnimationController
  miui.systemui.dynamicisland.view.DynamicGlowEffectView
  miui.systemui.dynamicisland.display.AvoidScreenBurnInHelper
  miui.systemui.notification.focus.FocusNotificationController
  com.mi.widget.core.AbsShader

HookUtils.isIslandLoaderReady() 会先去找前两个类，找不到就直接不下发 hook，
所以「没有任何报错、功能只是静默失效」== 这些类在当前进程里不存在。

本脚本直接扫 DEX 的字符串池（不需要 apktool/jadx/java），列出是不是真的不存在、
如果换了包名/换了名字的新实现，也能把它找出来。

用法
----
  python3 superisland_scan.py MiuiSystemUI.apk
  python3 superisland_scan.py MiuiSystemUIPlugin.apk MiuiSystemUI.apk
  python3 superisland_scan.py /path/to/rom_zip_dir/          # 递归找 *.apk / *.dex
  python3 superisland_scan.py 2.0.212MiuiSystemUI.7z         # 支持 7z（需要 py7zr）

退出码
------
  0 = 找到超级岛实现（兼容）
  1 = 没找到（这台系统上 HyperIsland 的这两个功能不可能生效）
  2 = 用法/解析错误
"""

from __future__ import annotations

import os
import re
import struct
import sys
import tempfile
import zipfile

# ---------------------------------------------------------------- 关心的类

OFFICIAL_TARGETS = [
    "miui.systemui.dynamicisland.window.content.DynamicIslandBaseContentView",
    "miui.systemui.dynamicisland.DynamicIslandBackgroundView",
    "miui.systemui.dynamicisland.DynamicFeatureConfig",
    "miui.systemui.dynamicisland.anim.DynamicIslandAnimationController",
    "miui.systemui.dynamicisland.view.DynamicGlowEffectView",
    "miui.systemui.dynamicisland.display.AvoidScreenBurnInHelper",
    "miui.systemui.notification.focus.FocusNotificationController",
    "com.mi.widget.core.AbsShader",
]

# 判定超级岛是否存在，至少要有这两个（与 HookUtils 保持一致）
REQUIRED_CORE = [
    "miui.systemui.dynamicisland.window.content.DynamicIslandBaseContentView",
    "miui.systemui.dynamicisland.DynamicIslandBackgroundView",
]

# 换包名时会遗留下来的关键词，用来找「候选新实现」
FALLBACK_HINTS = re.compile(
    r"dynamicisland|DynamicIsland|dynamic_island|super_?island|SuperIsland|island_?container",
    re.IGNORECASE,
)


# ---------------------------------------------------------------- DEX 解析

def _read_uleb128(data: bytes, off: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        b = data[off]
        off += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            break
        shift += 7
    return result, off


def dex_strings(data: bytes) -> list[str]:
    """读出 dex 字符串池。只读 string_ids，不解析指令，够快也够稳。"""
    if data[:4] != b"dex\n":
        raise ValueError("not a dex file")
    string_ids_size, string_ids_off = struct.unpack("<II", data[0x38:0x40])
    out: list[str] = []
    for i in range(string_ids_size):
        pos = string_ids_off + i * 4
        if pos + 4 > len(data):
            break
        (str_off,) = struct.unpack("<I", data[pos:pos + 4])
        try:
            size, s_pos = _read_uleb128(data, str_off)
            out.append(data[s_pos:s_pos + size].decode("utf-8", "replace"))
        except Exception:
            out.append("")
    return out


def _classes_from_strings(strings) -> set[str]:
    out = set()
    for s in strings:
        if len(s) > 2 and s.startswith("L") and s.endswith(";") and "/" in s:
            out.add(s[1:-1].replace("/", "."))
    return out


# ---------------------------------------------------------------- 输入展开

def iter_dex_blobs(path: str):
    """yield (来源描述, dex bytes)"""
    import io

    ext = os.path.splitext(path)[1].lower()

    if os.path.isdir(path):
        for root, _dirs, files in os.walk(path):
            for f in files:
                if os.path.splitext(f)[1].lower() in (".apk", ".dex", ".zip", ".jar"):
                    yield from iter_dex_blobs(os.path.join(root, f))
        return

    if ext == ".7z":
        with tempfile.TemporaryDirectory() as tmp:
            try:
                import py7zr
            except ImportError:
                print("  ! 需要 py7zr：pip3 install --break-system-packages py7zr")
                return
            with py7zr.SevenZipFile(path, "r") as zf:
                zf.extractall(tmp)
            for root, _dirs, files in os.walk(tmp):
                for f in files:
                    if os.path.splitext(f)[1].lower() in (".dex", ".apk", ".zip", ".jar"):
                        yield from iter_dex_blobs(os.path.join(root, f))
        return

    if ext == ".dex":
        with open(path, "rb") as f:
            yield os.path.basename(path), f.read()
        return

    with open(path, "rb") as f:
        blob = f.read()
    yield from _nested(os.path.basename(path), blob)


def _nested(label: str, data: bytes):
    """apk/zip -> 内部 dex"""
    try:
        with zipfile.ZipFile(__import__("io").BytesIO(data)) as zf:
            names = [n for n in zf.namelist() if n.endswith(".dex")]
            # 某些 ROM 会把附加代码放在 assets / lib 里的二级包，顺带找一层
            extra = [n for n in zf.namelist() if n.endswith((".apk", ".jar", ".zip"))][:20]
            for n in names:
                yield f"{label}!{n}", zf.read(n)
            for n in extra:
                try:
                    yield from _nested(f"{label}!{n}", zf.read(n))
                except Exception:
                    pass
    except Exception:
        return


# ---------------------------------------------------------------- 主流程

def scan(paths) -> int:
    found_map: dict[str, set[str]] = {}   # target -> {source}
    all_classes: dict[str, set[str]] = {}  # source -> {class}
    sources: list[str] = []

    for p in paths:
        if not os.path.exists(p):
            print(f"  ! 路径不存在：{p}")
            continue
        for source, data in iter_dex_blobs(p):
            try:
                strings = dex_strings(data)
            except Exception as e:
                print(f"  ! 跳过 {source}（{e}）")
                continue
            classes = _classes_from_strings(strings)
            if not classes:
                continue
            if source not in all_classes:
                all_classes[source] = set()
                sources.append(source)
            all_classes[source] |= classes
            for t in OFFICIAL_TARGETS:
                if t in classes:
                    found_map.setdefault(t, set()).add(source)

    print("=" * 72)
    print("HyperIsland 超级岛 Hook 目标扫描")
    print("=" * 72)
    for s in sources:
        print(f"  已解析: {s}  (类名 {len(all_classes[s])} 条)")
    if not sources:
        print("  ! 没有解析到任何 dex/apk")
        return 2

    print("\n[1] HyperIsland 需要 hook 的类是否命中")
    print("-" * 72)
    for t in OFFICIAL_TARGETS:
        srcs = found_map.get(t)
        if srcs:
            print(f"  [OK]      {t}\n            位于 {', '.join(sorted(srcs))}")
        else:
            mark = "必需" if t in REQUIRED_CORE else "可选"
            print(f"  [缺失/{mark}] {t}")

    core_ok = all(t in found_map for t in REQUIRED_CORE)
    print("\n[2] 结论")
    print("-" * 72)
    if core_ok:
        print("  这台系统的超级岛 View 层存在，类名与 HyperIsland 一致。")
        print("  若功能仍不生效，问题在别处（ClassLoader 时机 / 方法签名漂移 / 配置 key），")
        print("  请导出 LSPosed 日志，过滤 HyperIsland[LoaderDiscovery] 与 HyperIsland[IslandOutline]。")
        return 0

    print("  未找到 miui.systemui.dynamicisland.* —— 超级岛 View 层不在这份代码里。")
    print("  HookUtils.isIslandLoaderReady() 会判定 loader 未就绪，")
    print("  「常驻显示岛轮廓」「外圈光效」的 hook 根本不会安装，所以表现为完全无反应。")

    print("\n[3] 是否换名/换包名了？扫一遍疑似候选")
    print("-" * 72)
    cand = {}
    for src, classes in all_classes.items():
        hits = sorted(c for c in classes if FALLBACK_HINTS.search(c))
        if hits:
            cand[src] = hits
    if cand:
        for src, hits in cand.items():
            print(f"  {src}")
            for h in hits[:40]:
                print(f"      {h}")
            if len(hits) > 40:
                print(f"      ... 其余 {len(hits) - 40} 条")
        print("\n  → 存在疑似候选，说明超级岛代码换过头，可以据此做适配（把这些类发给我即可）。")
    else:
        print("  连疑似候选都没有：这份代码里不存在任何超级岛相关实现。")
        print("  → 该 ROM 版本没有系统级超级岛，HyperIsland 的这两个功能无法在此生效。")
        print("     请把 /system_ext/priv-app/MiuiSystemUIPlugin/MiuiSystemUIPlugin.apk")
        print("     （或 abspath 由 `adb shell pm path com.android.systemui` 得到的插件包）")
        print("     也丢进来再扫一次；若同样没有，就只能升级到澎湃 OS3/4 的超级岛版本。")
    return 1


def main(argv) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    return scan(argv[1:])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
