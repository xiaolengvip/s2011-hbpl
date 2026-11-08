#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hbp.py - S2011 HBPL 编码核心（OEC 本地库）

PDF/PS -> HBPL 作业字节流，可直接发打印机 9100。整条链路纯本机完成，不依赖 Windows。

    import hbp
    for n, data in hbp.jobs_for_pdf("/tmp/a.pdf"):
        hbp.send_job(data, "10.10.10.15", 9100)

编码参数与富士施乐官方驱动逐字节等价（已用驱动捕获做过闭环验证）：
    gs pbmraw 600dpi  ->  pbmtrans  ->  pbmtojbg -q -p 0 -o 0 -s 4961  ->  BIH[16]=0
    -> 52 A4 B1 A1 分块（≤255）包裹到 PJL 骨架里
页面几何：A4@600dpi 可打印区 4767x6822，JBIG 阵列 7040x4961
    JBIG 行 = 页面 x，JBIG 列 = 6918 - 页面 y
"""
import os
import shutil
import socket
import subprocess
import tempfile
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
TAG = bytes([0x52, 0xA4, 0xB1, 0xA1])
OUTW = 7040
OUTH = 4961
STRIPE = 4961
DEFAULT_HOST = "10.10.10.15"
DEFAULT_PORT = 9100

# 光栅化参数。MaxBitmap/BufferSpace 是关键加速项：默认 gs 按 1MB 分带渲染，
# 复杂页面（含大图/渐变）每个 band 都要重跑整份显示列表，实测页 3 从 21.9s
# 降到 5.5s（4 倍）。整页位图仅 4.35MB，80MB 足够放下整页 + 工作缓冲。
GS_MEM = 80000000
GS_WORKERS = int(os.environ.get("HBPL_WORKERS") or min(4, os.cpu_count() or 2))

_PRE = None
_SUF = None


def _skeleton():
    global _PRE, _SUF
    if _PRE is None:
        _PRE = open(os.path.join(HERE, "skel", "pre.bin"), "rb").read()
        _SUF = open(os.path.join(HERE, "skel", "suf.bin"), "rb").read()
    return _PRE, _SUF


def available():
    """依赖是否齐备（gs / pbmtojbg / pbmtrans / 骨架）"""
    ok = True
    for tool in ("gs", "pbmtojbg"):
        if not shutil.which(tool):
            ok = False
    if not os.access(os.path.join(HERE, "pbmtrans"), os.X_OK):
        ok = False
    if not os.path.exists(os.path.join(HERE, "skel", "pre.bin")):
        ok = False
    return ok


def workroot():
    """中间文件目录：优先内存盘（快、不磨 SD 卡），不够就退回磁盘

    每页中间物是 pbm + 转置后的 pbm，各 4.35MB；并发 N 个时占用 N×8.7MB，
    内存盘只剩 200MB 以上才用。
    """
    env = os.environ.get("HBPL_TMP")
    if env and os.path.isdir(env):
        return env
    shm = "/dev/shm"
    try:
        st = os.statvfs(shm)
        if st.f_bavail * st.f_frsize > 200 * 1024 * 1024:
            return shm
    except (OSError, AttributeError):
        # AttributeError：非 POSIX 平台没有 statvfs（本地调试用）
        pass
    d = "/var/tmp"
    return d if os.path.isdir(d) and os.access(d, os.W_OK) else None


def gs_cmd(dpi, paper, first=None, last=None, outpat=None, src=None):
    cmd = ["gs", "-q", "-dNOPAUSE", "-dBATCH", "-dSAFER",
           "-sDEVICE=pbmraw", "-r%d" % dpi,
           "-dFIXEDMEDIA", "-dPDFFitPage", "-sPAPERSIZE=" + paper,
           "-dAutoRotatePages=/None",
           "-dMaxBitmap=%d" % GS_MEM, "-dBufferSpace=%d" % GS_MEM]
    if first:
        cmd.append("-dFirstPage=%d" % first)
    if last:
        cmd.append("-dLastPage=%d" % last)
    if outpat:
        cmd.append("-sOutputFile=" + outpat)
    if src:
        cmd.append(src)
    return cmd


def rasterize(src, dpi, outdir, paper="a4", first=None, last=None):
    """光栅化到指定目录，返回 PBM 路径列表。

    注意：一次只渲染「一页」时最快（多页 bundle 在 4 核上无法并行，反而更慢）。
    这里保留区间能力给调试用，生产路径走 rasterize_page()。
    """
    pat = os.path.join(outdir, "pg-%04d.pbm")
    cmd = gs_cmd(dpi, paper, first, last, pat, src)
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       timeout=900)
    pages = sorted(f for f in os.listdir(outdir) if f.startswith("pg-"))
    if p.returncode != 0 and not pages:
        raise RuntimeError("光栅化失败：" +
                           (p.stderr or b"").decode("utf-8", "replace")[:200])
    return [os.path.join(outdir, f) for f in pages]


def rasterize_page(src, page, dpi, outdir, paper="a4"):
    """单页 -> PBM 路径（并行调用的安全入口）"""
    out = os.path.join(outdir, "p%05d.pbm" % page)
    cmd = gs_cmd(dpi, paper, page, page, out, src)
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       timeout=900)
    if not os.path.exists(out):
        raise RuntimeError("第 %d 页光栅化失败：%s"
                           % (page, (p.stderr or b"").decode("utf-8", "replace")[:200]))
    return out


def doc_pages(pdf):
    """文档页数：优先 pdfinfo，没有就退回 ghostscript"""
    exe = shutil.which("pdfinfo")
    if exe:
        try:
            p = subprocess.run([exe, pdf], stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, timeout=60)
            for line in p.stdout.decode("utf-8", "replace").splitlines():
                if line.startswith("Pages:"):
                    return int(line.split()[1])
        except Exception:
            pass
    p = subprocess.run(["gs", "-q", "-dNODISPLAY", "-dNOSAFER",
                        "-dBATCH", "-dNOPAUSE",
                        "-c", "(%s) (r) file runpdfbegin pdfpagecount = quit"
                        % pdf.replace("(", r"\(").replace(")", r"\)")],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
    try:
        return int((p.stdout or b"0").split()[0])
    except Exception:
        raise RuntimeError("无法读取页数")



PJL_MARK = b"ENTER LANGUAGE=HBPL"
PAGE_END = b"SD\xc1\xa1\x00B"      # 最后一页收尾 = 页结束 + 流结束符
PAGE_END_MID = b"SD\xc1\xa1\x00"   # 页与页之间只发 5 字节，0x42 是流结束符


def skeleton_parts():
    """把骨架拆成：PJL头 / 页设置组 / 页结束 / 流结束 / FXJOBINFO 尾

    PJL头      ...  \n@PJL ENTER LANGUAGE=HBPL\n
    页设置组   41... 43(纸张A4, 像素7040x4961) 51
    页结束     SD C1 A1 00      每页一份
    流结束     42               整条流只有一份（parse1 遇 0x42 即返回）
    FXJOBINFO  ESC%-12345X@PJL COMMENT FXJOBINFO ... @PJL EOJ
    """
    pre, suf = _skeleton()
    i = pre.find(PJL_MARK) + len(PJL_MARK) + 1
    if suf[:6] != PAGE_END:
        raise RuntimeError("骨架页结束记录异常：%r" % suf[:6])
    return pre[:i], pre[i:], suf[:5], suf[5:6], suf[6:]


def _rewrite_jobinfo(tail, pages, duplex="simplex"):
    """重写 FXJOBINFO 的页数与装订方式，PAGEINFO 按页数复制"""
    t = tail.decode("latin1")
    if duplex == "duplex":
        dt, pj = "DUPLEX", "ON"
    elif duplex == "tumble":
        dt, pj = "TUMBLE", "ON"
    else:
        dt, pj = "SIMPLEX", "OFF"
    sheets = (pages + 1) // 2 if duplex != "simplex" else pages

    t = t.replace("DUPLEXTYPE=SIMPLEX", "DUPLEXTYPE=" + dt)
    t = t.replace("DUPLEX[SIMPLEX]", "DUPLEX[%s]" % dt)
    t = t.replace("PHYSICALPAGES=1", "PHYSICALPAGES=%d" % pages)
    t = t.replace("LOGICALPAGES=1", "LOGICALPAGES=%d" % pages)
    t = t.replace("ESTIMATEDSHEETS=1", "ESTIMATEDSHEETS=%d" % sheets)

    b0 = t.index("PAGEINFOBEGIN")
    b1 = t.index("PAGEINFOEND")
    seg = t[b0:b1]
    line = [x for x in seg.splitlines() if "PAGEINFO=" in x]
    if line and pages > 1:
        head_of_end = t.rindex("\n", 0, b1) + 1
        t = (t[:head_of_end] + (line[0] + "\n") * (pages - 1)
             + t[head_of_end:])
    return t.encode("latin1")


def _with_duplex_pjl(head, duplex):
    on = b"OFF" if duplex == "simplex" else b"ON"
    return head.replace(b"@PJL SET DUPLEX=OFF", b"@PJL SET DUPLEX=" + on)


def page_bytes(js, pset=None, pend=None):
    """单页 -> 页设置组 + 分块数据 + 页结束（不含流结束符）

    拆出来是为了流式发送：一页编码完立刻写 socket，不必先把整本拼进内存。
    """
    if pset is None or pend is None:
        _, pset, pend, _, _ = skeleton_parts()
    out = bytearray(pset)
    for k in range(0, len(js), 255):
        blk = js[k:k + 255]
        out += TAG + bytes([len(blk)]) + blk
    out += pend
    return bytes(out)


def multi_job(jbigs, duplex="simplex", copies=1):
    """多页合成一个作业：PJL头 + [页设置+页数据+页结束] xN + 流结束 + FXJOBINFO"""
    head, pset, pend, endb, tail = skeleton_parts()
    out = bytearray(_with_duplex_pjl(head, duplex))
    n = 0
    for js in jbigs:
        for _ in range(max(1, copies)):
            out += page_bytes(js, pset, pend)
            n += 1
    out += endb
    out += _rewrite_jobinfo(tail, n, duplex)
    return bytes(out)


def page_jbig(pbm, workdir):
    """单页 PBM -> JBIG 字节流（不含容器）

    中间文件按源文件名派生，并行调用不会互相覆盖。
    """
    base = os.path.splitext(os.path.basename(pbm))[0]
    tp = os.path.join(workdir, base + ".t.pbm")
    jp = os.path.join(workdir, base + ".jbg")
    try:
        with open(pbm, "rb") as fin, open(tp, "wb") as fout:
            p = subprocess.run([os.path.join(HERE, "pbmtrans"),
                                "-W", str(OUTW), "-H", str(OUTH)],
                               stdin=fin, stdout=subprocess.PIPE, timeout=300)
            if p.returncode != 0:
                raise RuntimeError("转置失败")
            fout.write(p.stdout)
        p = subprocess.run(["pbmtojbg", "-q", "-p", "0", "-o", "0",
                            "-s", str(STRIPE), tp, jp],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300)
        if p.returncode != 0 or not os.path.exists(jp):
            raise RuntimeError("JBIG 编码失败")
        js = bytearray(open(jp, "rb").read())
        if len(js) > 16:
            js[16] = 0x00        # 驱动该字节为 0；jbigkit 写 0x08
        return bytes(js)
    finally:
        for f in (tp, jp):
            try:
                os.unlink(f)
            except OSError:
                pass


def _render_encode(src, page, dpi, paper, outdir):
    """单页完整流水线：光栅化 -> 转置 -> JBIG。供线程池并发调用。"""
    pbm = rasterize_page(src, page, dpi, outdir, paper)
    try:
        return page, page_jbig(pbm, outdir)
    finally:
        try:
            os.unlink(pbm)
        except OSError:
            pass


def page_job(pbm, workdir):
    """单页 PBM -> 完整 HBPL 作业字节流"""
    return multi_job([page_jbig(pbm, workdir)])


def _parallel_pages(src, pages, dpi, paper, outdir, workers):
    """并行渲染+编码，按页序产出 (页号, jbig)。

    滑动窗口只保留 workers 个任务在飞，避免中间文件堆积。
    结果按页码顺序吐出，保证打印机收到的顺序正确。
    """
    if workers <= 1:
        for p in pages:
            yield _render_encode(src, p, dpi, paper, outdir)
        return
    it = iter(pages)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        win = deque()
        for _ in range(workers):
            p = next(it, None)
            if p is None:
                break
            win.append((p, ex.submit(_render_encode, src, p, dpi, paper, outdir)))
        while win:
            _, fut = win.popleft()
            nxt = next(it, None)
            if nxt is not None:
                win.append((nxt, ex.submit(_render_encode, src, nxt, dpi,
                                           paper, outdir)))
            yield fut.result()


def jobs_for_pdf(pdf, dpi=600, workdir=None, paper="a4", chunk=4,
                 first=1, last=None, duplex="simplex", group=None,
                 workers=None):
    """生成器：产出 (组末页码, 作业字节流)

    并行策略：每页一条独立 gs 流水线，N 路并发，按页序汇总后按 group 组装作业。
    首组一凑齐就 yield，消费端（发送）与后续编码天然重叠，打印机不停等。
    """
    if group is None:
        group = 1 if duplex == "simplex" else 4
    if workers is None:
        workers = GS_WORKERS
    root = workdir or tempfile.mkdtemp(prefix="hbp.", dir=workroot())
    own = workdir is None
    buf = []          # [(页号, jbig)]
    try:
        total = doc_pages(pdf) if last is None else last
        if total <= 0:
            raise RuntimeError("页数为 0")
        stop = min(last or 10 ** 6, total)
        a = max(1, first)
        if a > stop:
            return
        pages = list(range(a, stop + 1))
        sub = tempfile.mkdtemp(prefix="c.", dir=root)
        try:
            for p, jb in _parallel_pages(pdf, pages, int(dpi), paper, sub, workers):
                buf.append((p, jb))
                if len(buf) >= group:
                    job = multi_job([j for _, j in buf], duplex=duplex)
                    n = buf[-1][0]
                    buf.clear()
                    yield n, job
            if buf:
                job = multi_job([j for _, j in buf], duplex=duplex)
                n = buf[-1][0]
                buf.clear()
                yield n, job
        finally:
            shutil.rmtree(sub, ignore_errors=True)
    finally:
        if own:
            shutil.rmtree(root, ignore_errors=True)


def stream_send(pdf, dpi=600, workdir=None, paper="a4", first=1, last=None,
                duplex="simplex", workers=None, host=None, port=None,
                on_page=None, timeout=1800):
    """整本一个作业：单 TCP 连接、边编码边发送。

    与 jobs_for_pdf 的分组发送相比，页与页之间不再有作业收尾（0x42 / FXJOBINFO
    / @PJL EOJ），也没有重复的 PJL 初始化，打印机内部状态机连续运行不中断。
    首张纸延迟仍只有「前 workers 页编码完」的时间。

    发送端的阻塞形成天然背压：打印机缓冲满 -> sendall 停 -> 编码窗口不再往前
    推，中间文件不会堆积。

    返回 (已发送页数, 总字节数, 耗时秒)。
    """
    head, pset, pend, endb, tail = skeleton_parts()
    if workers is None:
        workers = GS_WORKERS
    total = doc_pages(pdf) if last is None else last
    stop = min(last or 10 ** 6, total)
    a = max(1, first)
    if a > stop:
        return 0, 0, 0.0
    pages = list(range(a, stop + 1))
    root = workdir or tempfile.mkdtemp(prefix="hbp.", dir=workroot())
    own = workdir is None
    sub = tempfile.mkdtemp(prefix="c.", dir=root)
    t0 = time.time()
    n = nbytes = 0
    s = None
    try:
        s = socket.create_connection((host or DEFAULT_HOST, port or DEFAULT_PORT),
                                     timeout=15)
        s.settimeout(timeout)
        hdr = _with_duplex_pjl(head, duplex)
        s.sendall(hdr)
        nbytes += len(hdr)
        try:
            for p, js in _parallel_pages(pdf, pages, int(dpi), paper, sub,
                                         workers):
                buf = page_bytes(js, pset, pend)
                s.sendall(buf)
                nbytes += len(buf)
                n += 1
                if on_page:
                    try:
                        on_page(p, n)
                    except Exception:
                        pass
        finally:
            # 成功或异常都补收尾，别把打印机挂在半截作业上
            try:
                tailb = endb + _rewrite_jobinfo(tail, max(1, n), duplex)
                s.sendall(tailb)
                nbytes += len(tailb)
                s.shutdown(socket.SHUT_WR)
            except OSError:
                pass
    finally:
        if s is not None:
            try:
                s.close()
            except OSError:
                pass
        shutil.rmtree(sub, ignore_errors=True)
        if own:
            shutil.rmtree(root, ignore_errors=True)
    return n, nbytes, time.time() - t0


def page_count(pdf, dpi=600, paper="a4"):
    return doc_pages(pdf)


def send_job(data, host=DEFAULT_HOST, port=DEFAULT_PORT, timeout=60):
    t0 = time.time()
    s = socket.create_connection((host, port), timeout=15)
    s.settimeout(timeout)
    try:
        s.sendall(data)
        try:
            s.shutdown(socket.SHUT_WR)
        except OSError:
            pass
    finally:
        s.close()
    return time.time() - t0
