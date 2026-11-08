#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""view.py - 把 JBIG 解码出的 7040x4961 位图还原成"纸面视图" PNG

用法: view.py <in.pbm> <out.png> [降采样倍数]
变换（与 pbmtrans 互逆）：page(x,y) = bmp(row=x, col=6918-y)
"""
import struct
import sys
import zlib


def load_pbm(p):
    d = open(p, "rb").read()
    assert d[:2] == b"P4", "only P4"
    i = 2
    toks = []
    while len(toks) < 2:
        while d[i:i + 1].isspace():
            i += 1
        if d[i:i + 1] == b"#":
            while d[i:i + 1] != b"\n":
                i += 1
            continue
        j = i
        while not d[j:j + 1].isspace():
            j += 1
        toks.append(int(d[i:j]))
        i = j
    while d[i:i + 1].isspace():
        i += 1
    return toks[0], toks[1], d[i:]


def write_png(path, rows, w, h):
    raw = b"".join(b"\x00" + bytes(r) for r in rows)

    def chunk(t, data):
        return (struct.pack(">I", len(data)) + t + data +
                struct.pack(">I", zlib.crc32(t + data) & 0xFFFFFFFF))

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw, 6))
    png += chunk(b"IEND", b"")
    open(path, "wb").write(png)


def main():
    src, dst = sys.argv[1], sys.argv[2]
    step = int(sys.argv[3]) if len(sys.argv) > 3 else 4
    W, H, data = load_pbm(src)
    rb = (W + 7) // 8
    PW, PH = 4961, 7016
    ow, oh = PW // step, PH // step
    rows = []
    for oy in range(oh):
        r = bytearray()
        y = oy * step
        col = 6918 - y
        good = 0 <= col < W
        for ox in range(ow):
            x = ox * step
            if good and x < H:
                bit = (data[x * rb + (col >> 3)] >> (7 - (col & 7))) & 1
            else:
                bit = 0
            r.append(0 if bit else 255)
        rows.append(r)
    write_png(dst, rows, ow, oh)
    print("%s  %dx%d (step %d)" % (dst, ow, oh, step))


if __name__ == "__main__":
    main()
