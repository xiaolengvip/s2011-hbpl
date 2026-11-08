# S2011-HBPL

**Fuji Xerox DocuCentre S2011 打印驱动逆向：协议解析、编码器与 Linux 直打实现**

Fuji官方针对s2011这个机型没有任何 Linux 支持。本项目从驱动捕获与反汇编出发，
完整还原了它的 **HBPL**（Host Based Printer Language）页面描述格式，并用纯开源工具链在 ARM 单板上
重新实现了「PDF → HBPL → 打印机 9100 端口」的整条链路，**编码结果与官方驱动逐字节等价**。

```
PDF/PS ──gs──> PBM 600dpi ──pbmtrans──> 转置位图 ──pbmtojbg──> JBIG1 ──分块──> HBPL 作业 ──TCP 9100──> S2011
```

## 结论速览

| 项目 | 状态 | 说明 |
|---|---|---|
| 容器格式（PJL / 页记录 / 分块 / 作业收尾） | ✅ 完全还原 | 含 `FXJOBINFO` 全部字段 |
| 页面图像编码 | ✅ 逐字节等价 | 解码驱动数据再重编码，`cmp` 全等 |
| 多页单作业 | ✅ | 页间仅 5 字节分隔，`0x42` 是**流**结束符 |
| 双面（长边/短边翻转） | ✅ 实机验证 | 35 页 → 18 张，计数精确 |
| 性能 | ✅ 1.4 s/页编码 | 瓶颈已是打印机引擎（4.4 s/面） |
| PCL / PostScript 模式 | ❌ | 机型本身不支持，只能逆向 HBPL |

## 三个关键发现

1. **页面数据不是自研位流，而是 JBIG1（ITU-T T.82）压缩的转置位图。**
   每条 20 字节的「页设置记录」就是 JBIG 的 BIH 头：`XD=7040`（页高像素）、`YD=4961`（页宽像素）。
   线索来自 `foo2zjs/foo2hbpl2.c` 里对 jbigkit 的调用。

2. **JBIG 的一行 = 页面的一列。** 图像先转置再压缩，所以纵向条带的编码成本远低于横向跳行。
   坐标映射为 `JBIG 行 = 页面 x`、`JBIG 列 = 7015 − 页面 y`（GDI 可打印区原点在纸面 97,97）。

3. **`0x42` 是整条数据流的结束符，不是页结束符。** 页与页之间只发 5 字节 `53 44 C1 A1 00`。
   早期把每页都补一个 `0x42`，导致两页合并的作业只出第一张纸。

详细推导见 [`docs/protocol.md`](docs/protocol.md)、[`docs/encoding.md`](docs/encoding.md)，
逆向过程与踩坑记录见 [`docs/findings.md`](docs/findings.md)。

## 快速开始

依赖：`ghostscript`、`jbigkit-bin`（提供 `pbmtojbg`）、`netpbm`、`gcc`。

```bash
git clone <this-repo> && cd s2011-hbpl

# 1) 从 hex 还原 PJL 骨架（骨架源自一次真实捕获，含日期等可变字段，故以 hex 保存）
python3 tools/hex2bin.py src/skel/pre.bin.hex src/skel/pre.bin
python3 tools/hex2bin.py src/skel/suf.bin.hex src/skel/suf.bin

# 2) 编译转置工具
gcc -O2 -o src/pbmtrans src/pbmtrans.c

# 3) 打印
python3 src/hbprint.py 文件.pdf                 # 单面，整本一个作业
python3 src/hbprint.py 文件.pdf --duplex long    # 双面，长边翻转
python3 src/hbprint.py 文件.pdf --range 1-4 --dry-run   # 只编码不发送
python3 src/hbprint.py 文件.pdf -H 192.168.1.50 -p 9100
```

`hbprint.py` 默认**整本一个作业、单连接流式发送**：编码完一页就立刻写 socket，页间没有作业收尾、
没有 PJL 重初始化，打印机引擎全程不停。`--group N` 可退回「每 N 页一个作业」的旧路径，便于对比排查。

## 仓库结构

```
src/
  hbp.py          编码核心：光栅化 → 转置 → JBIG → 分块 → 骨架拼装 → 发送
  hbprint.py      命令行入口（范围、单双面、并发、dry-run）
  pbmtrans.c      PBM 转置为 JBIG 扫描线布局（C，几百行）
  skel/*.hex      PJL 头与 FXJOBINFO 尾的骨架（hex 形式，见上）
tools/
  hex2bin.py      hex → 二进制
  view.py         把解码出的位图逆变换回「纸面视图」PNG，用于验证方向
assets/
  sample_1char.prn.hex   一份真实作业样本（单字符页），可直接解析对照
examples/
  duplex_probe.ps        双面自证页：四角标记 + FRONT/BACK 大字
docs/
  protocol.md     容器与命令结构
  encoding.md     图像编码管线与坐标映射
  performance.md  性能：分带陷阱、并行、流式发送
  findings.md     逆向过程、错误判断与修正
```

## 验证方法

- **编码等价性**：从驱动捕获的作业里取出单页数据 → 用 `pbmtojbg` 反向解出位图 → 用本项目重新编码 →
  与原页数据 `cmp` 比较，**内容全等**（仅 BIH 第 16 字节需按驱动写 `0x00`，jbigkit 默认写 `0x08`）。
- **方向正确性**：`tools/view.py` 做逆变换，把位图还原成纸面视图 PNG，文字方向与位置可直接目视核对。
- **多页/双面**：用 `examples/duplex_probe.ps` 实打，看物理张数与正反面标记；配合打印机的
  `prtMarkerLifeCount`（面数计数）核对增量。

## 限制

- 只针对 **DocuCentre S2011**（HBPL 语言）。同系列机型可能共用，但未验证。
- 纸张固定 A4@600dpi；其他幅面需要重新标定可打印区与 BIH 尺寸。
- 仓库**不包含任何厂商二进制**（驱动 DLL、固件）。骨架是从一次真实打印作业中提取的 PJL 片段，
  样本文件仅用于格式说明。
- 本项目为互操作用途的逆向工程，与富士施乐 / 富士胶片无任何关联。

## License

代码部分 MIT，见 [`LICENSE`](LICENSE)。
