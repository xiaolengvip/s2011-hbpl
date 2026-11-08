#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hbprint.py - 在 OEC 上把 PDF/PS 编码成 HBPL 并直接发打印机（不经 Windows）

用法：
    hbprint.py <文件.pdf|.ps> [-r dpi] [-H 打印机IP] [-p 端口]
               [--range 1-5] [--duplex long|short] [--jobs N]
               [--group N] [--keep 目录] [--dry-run]

默认整本一个作业、单连接流式发送（页间不停顿）。
--group N 退回「每 N 页一个独立作业」的旧路径，用于排查或特殊场景。

示例：
    hbprint.py /tmp/a.pdf
    hbprint.py /tmp/a.pdf --range 1-3 -H 10.10.10.15
    hbprint.py /tmp/a.pdf --duplex long          # 双面长边
    hbprint.py /tmp/a.pdf --dry-run --keep /tmp/prn   # 只编码不发送
"""
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hbp  # noqa: E402


def parse_range(spec, total):
    if not spec:
        return list(range(1, total + 1))
    out = []
    for part in spec.split(","):
        part = part.strip()
        m = re.match(r"^(\d+)-(\d+)$", part)
        if m:
            out += list(range(int(m.group(1)), int(m.group(2)) + 1))
        elif part.isdigit():
            out.append(int(part))
    return [p for p in out if 1 <= p <= total]


def main(argv):
    if len(argv) < 2:
        raise SystemExit(__doc__)
    src = argv[1]
    dpi, host, port = 600, hbp.DEFAULT_HOST, hbp.DEFAULT_PORT
    rng, keep, dry = None, None, False
    duplex, group = "simplex", None
    jobs = None
    i = 2
    while i < len(argv):
        a = argv[i]
        if a == "-r":
            dpi = int(argv[i + 1]); i += 2
        elif a == "-H":
            host = argv[i + 1]; i += 2
        elif a == "-p":
            port = int(argv[i + 1]); i += 2
        elif a == "--range":
            rng = argv[i + 1]; i += 2
        elif a == "--keep":
            keep = argv[i + 1]; i += 2
        elif a == "--duplex":
            v = argv[i + 1].lower() if i + 1 < len(argv) else ""
            if v in ("long", "duplex"):
                duplex = "duplex"; i += 2
            elif v in ("short", "tumble"):
                duplex = "tumble"; i += 2
            else:
                duplex = "duplex"; i += 1
        elif a == "--group":
            group = int(argv[i + 1]); i += 2
        elif a == "--jobs":
            jobs = max(1, int(argv[i + 1])); i += 2
        elif a == "--dry-run":
            dry = True; i += 1
        else:
            raise SystemExit("unknown option: " + a)

    if not hbp.available():
        raise SystemExit("依赖缺失：需要 gs、pbmtojbg、pbmtrans 与 skel/")

    try:
        total = hbp.page_count(src, dpi)
    except Exception:
        if not rng:
            raise
        total = 0          # PostScript 等无法预读页数，按给定范围走

    sel = parse_range(rng, total) or []
    if not sel and not total:
        # total=0 时 parse_range 会全部过滤掉，这里改成信任用户给的范围
        for part in (rng or "").split(","):
            m = re.match(r"^(\d+)-(\d+)$", part.strip())
            if m:
                sel += list(range(int(m.group(1)), int(m.group(2)) + 1))
            elif part.strip().isdigit():
                sel.append(int(part.strip()))
    if not sel:
        raise SystemExit("页码范围无效（共 %d 页）" % total)
    lo, hi = min(sel), max(sel)
    if len(sel) != hi - lo + 1:
        raise SystemExit("页码范围必须连续（双面要按纸张配对），收到 %s" % rng)
    print("[raster] %s，本次处理 %d-%d（%d 页）%s，%d 路并行"
          % ("共 %d 页" % total if total else "页数未知",
             lo, hi, len(sel),
             "，双面 " + ("短边" if duplex == "tumble" else "长边")
             if duplex != "simplex" else "，单面",
             jobs or hbp.GS_WORKERS))

    sent = 0
    t0 = time.time()

    # 默认：整本一个作业、单连接流式发送。
    # 页与页之间只有 5 字节分隔符，没有作业收尾和 PJL 重初始化，
    # 打印机引擎连续跑不中断；首张纸仍只需等前 workers 页编码完。
    if not dry and not keep and not group:
        def cb(_p, k):
            print("  已发 %d 页  %.1fs" % (k, time.time() - t0))
        n, nbytes, dt = hbp.stream_send(src, dpi, first=lo, last=hi,
                                        duplex=duplex, workers=jobs,
                                        host=host, port=port, on_page=cb)
        print("[done] 已发送 %d 页，%d KB，耗时 %.1fs（%.2f s/页）"
              % (n, nbytes // 1024, dt, dt / max(1, n)))
        return

    for n, data in hbp.jobs_for_pdf(src, dpi, first=lo, last=hi,
                                    duplex=duplex, group=group,
                                    workers=jobs):
        if keep:
            os.makedirs(keep, exist_ok=True)
            open(os.path.join(keep, "upto%03d.prn" % n), "wb").write(data)
        if dry:
            print("  到第 %d 页  job=%d 字节 (dry-run)  %.1fs"
                  % (n, len(data), time.time() - t0))
            sent = n
            continue
        dt = hbp.send_job(data, host, port)
        print("  到第 %d/%d 页  job=%d 字节 -> %s:%d  %.2fs  [累计 %.1fs]"
              % (n, total, len(data), host, port, dt, time.time() - t0))
        sent = n
    print("[done] 已处理至第 %d 页%s，耗时 %.1fs（%.2f s/页）"
          % (sent, "（未发送）" if dry else "",
             time.time() - t0, (time.time() - t0) / max(1, sent - lo + 1)))


if __name__ == "__main__":
    main(sys.argv)
