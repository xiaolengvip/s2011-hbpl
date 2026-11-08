# 图像编码管线

页面图像 = **转置后的 1-bit 位图**，用 **JBIG1（ITU-T T.82）** 压缩，再切成 255 字节的块塞进容器。

```
PDF/PS ─gs(pbmraw,600dpi)─> P4 4961×7016 ─pbmtrans─> P4 7040×4961 ─pbmtojbg─> JBIG1 ─切块─> 52 A4 B1 A1 …
```

## 1. 光栅化

```bash
gs -q -dNOPAUSE -dBATCH -dSAFER \
   -sDEVICE=pbmraw -r600 \
   -dFIXEDMEDIA -dPDFFitPage -sPAPERSIZE=a4 -dAutoRotatePages=/None \
   -dMaxBitmap=80000000 -dBufferSpace=80000000 \
   -dFirstPage=N -dLastPage=N -sOutputFile=p%05d.pbm input.pdf
```

- `-dFIXEDMEDIA` + `-dPDFFitPage`：强制缩放到 A4，否则不同页面尺寸会让位图大小漂移（必然错版）。
- `-dAutoRotatePages=/None`：禁止 gs 自作主张旋转，方向由我们自己控制。
- `-dMaxBitmap/-dBufferSpace`：见 [performance.md](performance.md)，对含图页面是 4 倍加速。

产物：A4@600dpi = **4961 × 7016** 像素；可打印区约 **4767 × 6822**，四边留白约 97 px ≈ 4.1 mm。

## 2. 转置（关键一步）

`pbmtrans.c` 把 4961×7016 的页面位图重排成 7040×4961：

```
dst_row[r]  = 页面的第 r 列   (r = 页面 x)
dst_bit[c]  = 页面 (x=r, y=(7016-1)-c)
```

即 `JBIG 行 = 页面 x`、`JBIG 列 = 7015 − 页面 y`。

这条映射来自驱动捕获的实测：在 GDI 坐标系里写入 `dst[row = 97 + x_gdi][col = 6918 − y_gdi]`，
而 GDI 可打印区原点在纸面 (97, 97)，代入即得上式。`tools/view.py` 做了逆变换
（`col = 6918 − y_paper`，其中 6918 = 7015 − 97），把码流还原成「纸面视图」PNG，
用来目视确认方向与位置。

**为什么必须转置**：JBIG1 是逐扫描线编码、行内取上下文的算法。把页面一列当作一条扫描线，
行内相邻像素正好是纸面上的纵向相邻像素——大段空白与连续笔画都能被上下文预测吃掉。
这也解释了早期观察到的现象：整页纯白时数据量随「页高」而非「页宽」增长，
横向每跳一行要跨 7040 位。

## 3. JBIG 编码

```bash
pbmtojbg -q -p 0 -o 0 -s 4961 in.pbm out.jbg
```

- `-s 4961`：整幅作为**一个 stripe**（等于图像高度），不分条。
- `-p 0`：不用 typical prediction。
- `-o 0`：默认 bit 排列顺序。

**然后必须改一个字节**：

```python
js[16] = 0x00     # jbigkit 写 0x08，官方驱动写 0x00
```

这 1 字节落在 BIH 的 `L0` 字段内。不改的话打印机仍会按 0x08 解释，出图异常。
这是「与驱动逐字节等价」的最后一道坎。

## 4. 切块

JBIG 码流按 **255 字节**切片，每片前缀 `52 A4 B1 A1 len`：

```python
for k in range(0, len(js), 255):
    blk = js[k:k+255]
    out += b"\x52\xA4\xB1\xA1" + bytes([len(blk)]) + blk
```

长度字段只有 1 字节，所以 255 是硬上限。

## 5. 等价的验证方法

1. 用驱动打一份，抓到作业（`corpus/` 里就是这种样本）。
2. 从容器里剥出单页 JBIG（页设置组后、`53 44 C1 A1 00` 前，去掉每块 5 字节头）。
3. `jbigtopbm` 解出 7040×4961 位图，`pbmtrans` 的逆变换（`tools/view.py`）看方向。
4. 用本项目把同一张位图重新编码，与原始页数据 **`cmp`**。

结果：内容全等；只有 BIH 第 16 字节需要按驱动写 `0x00`。这条闭环是整条管线可信的依据——
不是「打出来看着像」，而是字节级一致。
