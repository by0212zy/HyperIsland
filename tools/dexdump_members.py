#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dexdump_members.py —— 直接从 DEX 里列出某个类的字段与方法签名（含参数类型）。

用来验证 Xposed Hook 的目标方法在新 ROM 上有没有变：
 Hook 代码里写的是 getMethod("xxx", A::class.java, B::class.java)，
 只要参数列表或方法名漂移，Class.forName 能找到类但 hook 依旧不生效。

用法
----
  python3 dexdump_members.py <apk|dex|目录> <类名...> [--all-methods]
  python3 dexdump_members.py MIUISystemUIPlugin.apk miui.systemui.dynamicisland.DynamicIslandBackgroundView
  python3 dexdump_members.py MIUISystemUIPlugin.apk miui.systemui.dynamicisland --prefix
"""

from __future__ import annotations

import os
import struct
import sys
import zipfile

import io


# ------------------------------------------------------------------ 基础解析

def _uleb(data: bytes, off: int) -> tuple[int, int]:
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


class Dex:
    def __init__(self, data: bytes, label: str):
        if data[:4] != b"dex\n":
            raise ValueError("not a dex")
        self.d = data
        self.label = label

        (self.str_size, self.str_off) = struct.unpack("<II", data[0x38:0x40])
        (self.type_size, self.type_off) = struct.unpack("<II", data[0x40:0x48])
        (self.proto_size, self.proto_off) = struct.unpack("<II", data[0x48:0x50])
        (self.field_size, self.field_off) = struct.unpack("<II", data[0x50:0x58])
        (self.method_size, self.method_off) = struct.unpack("<II", data[0x58:0x60])
        (self.class_size, self.class_off) = struct.unpack("<II", data[0x60:0x68])

        self.strings = []
        for i in range(self.str_size):
            (o,) = struct.unpack("<I", data[self.str_off + i * 4:self.str_off + i * 4 + 4])
            size, p = _uleb(data, o)
            try:
                self.strings.append(data[p:p + size].decode("utf-8", "replace"))
            except Exception:
                self.strings.append("")

    # --- 索引表读取 ---
    def type(self, idx: int) -> str:
        if idx == 0xFFFFFFFF or idx >= self.type_size:
            return "?"
        (d,) = struct.unpack("<I", self.d[self.type_off + idx * 4:self.type_off + idx * 4 + 4])
        return self.strings[d] if d < len(self.strings) else "?"

    def proto_params(self, idx: int) -> list[str]:
        base = self.proto_off + idx * 12
        _shorty, ret, params_off = struct.unpack("<III", self.d[base:base + 12])
        if params_off == 0:
            return []
        (n,) = struct.unpack("<I", self.d[params_off:params_off + 4])
        out = []
        for i in range(n):
            (t,) = struct.unpack("<H", self.d[params_off + 4 + i * 2:params_off + 6 + i * 2])
            out.append(self.type(t))
        return out

    def proto_ret(self, idx: int) -> str:
        base = self.proto_off + idx * 12
        _shorty, ret, _p = struct.unpack("<III", self.d[base:base + 12])
        return self.type(ret)

    def field(self, idx: int) -> tuple[str, str, str]:
        base = self.field_off + idx * 8
        cls, typ, name = struct.unpack("<HHI", self.d[base:base + 8])
        return (self.type(cls), self.type(typ), self.strings[name] if name < len(self.strings) else "?")

    def method(self, idx: int) -> tuple[str, str, str, list[str]]:
        base = self.method_off + idx * 8
        cls, proto, name = struct.unpack("<HHI", self.d[base:base + 8])
        return (
            self.type(cls),
            self.strings[name] if name < len(self.strings) else "?",
            self.proto_ret(proto),
            self.proto_params(proto),
        )

    def classes(self):
        for i in range(self.class_size):
            base = self.class_off + i * 32
            (cls_idx, _flags, super_idx, _if, _src, _anno, data_off, _sv) = struct.unpack(
                "<IIIIIIII", self.d[base:base + 32])
            yield i, self.type(cls_idx), self.type(super_idx), data_off

    def members(self, class_idx: int):
        """返回 (static_fields, instance_fields, direct_methods, virtual_methods)"""
        base = self.class_off + class_idx * 32
        data_off = struct.unpack("<I", self.d[base + 24:base + 28])[0]
        if data_off == 0:
            return [], [], [], []
        p = data_off
        sf, p = _uleb(self.d, p)
        inf, p = _uleb(self.d, p)
        dm, p = _uleb(self.d, p)
        vm, p = _uleb(self.d, p)

        def read_fields(p, n):
            out = []
            fidx = 0
            for _ in range(n):
                diff, p = _uleb(self.d, p)
                _flags, p = _uleb(self.d, p)
                fidx += diff
                out.append(self.field(fidx))
            return out, p

        def read_methods(p, n):
            out = []
            midx = 0
            for _ in range(n):
                diff, p = _uleb(self.d, p)
                _flags, p = _uleb(self.d, p)
                _code, p = _uleb(self.d, p)
                midx += diff
                out.append(self.method(midx))
            return out, p

        sfields, p = read_fields(p, sf)
        ifields, p = read_fields(p, inf)
        dmethods, p = read_methods(p, dm)
        vmethods, p = read_methods(p, vm)
        return sfields, ifields, dmethods, vmethods


# ------------------------------------------------------------------ 可读化

def human(t: str) -> str:
    if t.startswith("L") and t.endswith(";"):
        return t[1:-1].replace("/", ".")
    return {
        "Z": "boolean", "B": "byte", "S": "short", "C": "char",
        "I": "int", "J": "long", "F": "float", "D": "double", "V": "void",
    }.get(t, t)


def sig(name, params, ret) -> str:
    return f"{human(ret)} {name}({', '.join(human(x) for x in params)})"


# ------------------------------------------------------------------ 载入

def load_dexes(path: str):
    if os.path.isdir(path):
        for root, _d, files in os.walk(path):
            for f in files:
                if f.endswith((".apk", ".dex", ".jar", ".zip")):
                    for x in load_dexes(os.path.join(root, f)):
                        yield x
        return
    if path.endswith(".dex"):
        with open(path, "rb") as f:
            yield Dex(f.read(), os.path.basename(path))
        return
    with open(path, "rb") as f:
        blob = f.read()
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            for n in zf.namelist():
                if n.endswith(".dex"):
                    try:
                        yield Dex(zf.read(n), f"{os.path.basename(path)}:{n}")
                    except Exception:
                        pass
    except Exception:
        pass


def main(argv):
    if len(argv) < 3:
        print(__doc__)
        return 2
    path = argv[1]
    args = argv[2:]
    prefix_mode = "--prefix" in args
    args = [a for a in args if not a.startswith("--")]

    dexes = list(load_dexes(path))
    wanted = [a.replace(".", "/") for a in args]

    for w in wanted if not prefix_mode else [None]:
        for dex in dexes:
            for ci, cls, sup, _ in dex.classes():
                cls_n = cls[1:-1] if cls.startswith("L") else cls
                match = cls_n.startswith(w) if prefix_mode else cls_n == w
                if not match:
                    continue
                sf, inf, dm, vm = dex.members(ci)
                print("=" * 78)
                print(f"class {cls_n}")
                print(f"  extends {human(sup) if sup.startswith('L') else sup}   [{dex.label}]")
                if inf:
                    print("  instance fields:")
                    for owner, typ, name in inf:
                        print(f"      {human(typ)} {name}")
                if sf:
                    print("  static fields:")
                    for owner, typ, name in sf:
                        print(f"      {human(typ)} {name}")
                allm = [("direct", m) for m in dm] + [("virtual", m) for m in vm]
                if allm:
                    print(f"  methods ({len(allm)}):")
                    for kind, (owner, name, ret, params) in allm:
                        print(f"      [{kind}] {sig(name, params, ret)}")
                break
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
