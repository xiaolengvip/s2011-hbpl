# HBPL 容器格式

S2011 的打印数据走 **raw TCP 9100**，整条流是「PJL 包裹 + HBPL 二进制段」的混合体。

## 整体结构

```
ESC%-12345X@PJL SET DUPLEX=OFF          ┐
@PJL COMMENT DATE=…  TIME=…             │ PJL 头（ASCII，可读）
@PJL ENTER LANGUAGE=HBPL                ┘
┌─────────────────────────────────────────────────────────┐
│ 页设置组（55 字节，含 20 字节 JBIG BIH）                    │  ← 每页一份
│ 52 A4 B1 A1 | len | data …            （≤255 字节/块，可多块）│
│ 53 44 C1 A1 00                        （页结束，5 字节）     │
└─────────────────────────────────────────────────────────┘
                        × N 页，页间无任何其它字节
42                                     ← 流结束符（整条流仅一次）
ESC%-12345X@PJL COMMENT FXJOBINFO …    ┐ 作业信息（ASCII）
@PJL EOJ                               ┘
```

## 各段说明

### PJL 头

普通 PJL。关键一行是 `@PJL ENTER LANGUAGE=HBPL`，它之后解释器切到 HBPL 二进制模式。
双面由 `@PJL SET DUPLEX=OFF|ON` 控制（见下）。

### 页设置组（55 字节）

固定长度、固定位置：

| 偏移 | 内容 |
|---|---|
| 0 | `0x41` 记录起始 |
| 中间 | **20 字节 JBIG BIH 头**：`DL(4) D(1) P(1) XD(4) YD(4) L0(4) MX(1) MY(1)` |
| — | `XD = 7040`（页高像素）、`YD = 4961`（页宽像素）、单 stripe |
| — | `0x43` 纸张/分辨率标记（A4 @600dpi） |
| 54 | `0x51` 组结束 |

把这 20 字节当 BIH 看，是整件事的转折点：它意味着页面数据就是标准 JBIG1 码流，
不需要自己写算术解码器。

### 页数据块

```
52 A4 B1 A1   len    payload
└── 4 字节标记 ─┘  1B    最多 255 字节
```

一条 JBIG 码流按 255 字节切片，每片加 5 字节头依次写入。`len` 是实际长度，最后一片可以不足 255。

### 页结束与流结束

- `53 44 C1 A1 00`：**页**结束，5 字节，每页一份。
- `0x42`：**流**结束符。`hbpldecode.c` 的 `parse1()` 遇到 `0x42` 直接返回，
  之后的字节交给 PJL 解析器。整条流只能出现一次。

这条区别踩过坑：早期每页都补 `SD C1 A1 00 42`，两页的作业只出第一张纸——第二个 `0x42`
被当成流结束，后续页被丢弃。

### FXJOBINFO

```
ESC%-12345X@PJL COMMENT FXJOBINFO
…DUPLEXTYPE=SIMPLEX…
…PAGEINFO=…DUPLEX[SIMPLEX]…
…PHYSICALPAGES=1  LOGICALPAGES=1  ESTIMATEDSHEETS=1…
PAGEINFOBEGIN
PAGEINFO=…
PAGEINFOEND
@PJL EOJ
```

打印机用这段做计费与排纸调度，字段必须与前面实际页数一致，否则会出现「打完了但面板显示异常」
或双面翻转时序错乱。

| 字段 | 单面 | 双面 N 页 |
|---|---|---|
| `DUPLEXTYPE=` | `SIMPLEX` | `DUPLEX` / `TUMBLE` |
| `PAGEINFO=…DUPLEX[…]` | `SIMPLEX` | 同上 |
| `PHYSICALPAGES=` / `LOGICALPAGES=` | 页数 | 页数 |
| `ESTIMATEDSHEETS=` | 页数 | `ceil(N/2)` |
| `PAGEINFOBEGIN/END` 之间 | 1 条 | **N 条**（按页复制） |

### 双面

三处必须同时改，缺一处就会出现「只翻一面」「翻错边」或「按单面计张」：

1. PJL 头：`@PJL SET DUPLEX=ON`
2. FXJOBINFO：`DUPLEXTYPE=DUPLEX`（长边）/ `TUMBLE`（短边）
3. FXJOBINFO：`PAGEINFO=…DUPLEX[DUPLEX|TUMBLE]`

另外 `PHYSICALPAGES` / `LOGICALPAGES` 要写成页数、`ESTIMATEDSHEETS` 写成 `ceil(页数/2)`，
`PAGEINFO` 行按页数复制。

> 注意：打印机的 `prtMarkerLifeCount`（`1.3.6.1.2.1.43.10.2.1.4.1.1`）统计的是**面数**不是张数。
> 双面打 35 页，计数 +35，纸只有 18 张。

## 对照样本

`assets/sample_1char.prn.hex` 是一份真实作业（单页、一个字符）的 hex dump，可以
`xxd -r -p` 还原后逐段对照上面的结构。骨架 `src/skel/pre.bin`、`suf.bin` 就是从中提取的。
