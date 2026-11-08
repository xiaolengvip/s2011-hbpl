/* pbmtrans.c - PBM 位图转置为 HBPL 所需的 JBIG 扫描线布局
 *
 * 输入：P4 (PBM) 图像 w x h，表示页面（左上角为 0,0，x 向右，y 向下）
 * 输出：P4 图像 OUTW x OUTH，其中
 *          out_row = x           (页面 x 轴 -> JBIG 扫描线号)
 *          out_col = (h-1) - y   (页面 y 轴反向 -> 扫描线内位置)
 *
 * 这是从富士施乐 S2011 驱动捕获逐字节反推得到的等价变换：
 *   dst[row = 97 + x_gdi][col = 6918 - y_gdi]，而 GDI 可打印区原点在纸面 (97,97)，
 *   故 dst[row = x_page][col = 7015 - y_page]。
 *
 * 用法: pbmtrans [-W outw] [-H outh] < in.pbm > out.pbm
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int next_int(FILE *f)
{
    int c, v = 0, got = 0;
    /* skip whitespace and comments */
    for (;;) {
        c = fgetc(f);
        if (c == '#') {
            while (c != EOF && c != '\n') c = fgetc(f);
            continue;
        }
        if (c == EOF) return -1;
        if (c == ' ' || c == '\t' || c == '\r' || c == '\n') continue;
        break;
    }
    while (c >= '0' && c <= '9') {
        v = v * 10 + (c - '0');
        got = 1;
        c = fgetc(f);
    }
    if (!got) return -1;
    return v;
}

#define BIT(buf, idx) (((buf)[(idx) >> 3] >> (7 - ((idx) & 7))) & 1)

int main(int argc, char **argv)
{
    int outw = 7040, outh = 4961, i;
    for (i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "-W") && i + 1 < argc) outw = atoi(argv[++i]);
        else if (!strcmp(argv[i], "-H") && i + 1 < argc) outh = atoi(argv[++i]);
    }

    int c = fgetc(stdin);
    if (c != 'P') { fprintf(stderr, "not a PBM\n"); return 1; }
    c = fgetc(stdin);
    if (c != '4') { fprintf(stderr, "only P4 supported\n"); return 1; }

    int w = next_int(stdin), h = next_int(stdin);
    if (w <= 0 || h <= 0) { fprintf(stderr, "bad pbm size\n"); return 1; }
    /* next_int 已把分隔空白字符吃掉，不能再多读一个字节 */

    size_t rb = (size_t)((w + 7) / 8);
    unsigned char *src = malloc(rb * (size_t)h);
    if (!src) return 1;
    if (fread(src, 1, rb * (size_t)h, stdin) != rb * (size_t)h) {
        fprintf(stderr, "short read\n");
        return 1;
    }

    size_t orb = (size_t)((outw + 7) / 8);
    unsigned char *row = calloc(orb, 1);
    if (!row) return 1;

    fprintf(stdout, "P4\n%d %d\n", outw, outh);

    for (int r = 0; r < outh; r++) {
        memset(row, 0, orb);
        if (r < w) {
            /* 页面第 r 列（x=r）整列为一条扫描线；col c 取自 y = (h-1)-c */
            size_t bits_per_line = rb * 8;
            for (int cc = 0; cc < h && cc < outw; cc++) {
                int y = (h - 1) - cc;
                if (BIT(src, (size_t)y * bits_per_line + (size_t)r))
                    row[cc >> 3] |= (unsigned char)(0x80 >> (cc & 7));
            }
        }
        if (fwrite(row, 1, orb, stdout) != orb) return 1;
    }
    free(src);
    free(row);
    return 0;
}
