#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hex2bin.py - 把 hex dump 还原成二进制

仓库里的二进制资源（PJL 骨架、作业样本）以 hex 形式保存：它们是文本、可 diff、
不会被 Git 当二进制处理，也避免把厂商数据以二进制形式散布。

用法:
    hex2bin.py <in.hex> <out.bin>
"""
import sys


def main(argv):
    if len(argv) < 3:
        raise SystemExit(__doc__)
    src, dst = argv[1], argv[2]
    with open(src, "r", encoding="utf-8") as f:
        hexstr = "".join(f.read().split())
    if len(hexstr) % 2:
        raise SystemExit("hex 长度是奇数，文件可能被截断：%s" % src)

    data = bytes.fromhex(hexstr)
    with open(dst, "wb") as f:
        f.write(data)
    print("%s -> %s  %d 字节" % (src, dst, len(data)))


if __name__ == "__main__":
    main(sys.argv)
