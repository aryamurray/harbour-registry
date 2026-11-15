/* libjpeg-turbo: compress an in-memory image, decompress it, and compare.
 *
 * Four independent paths, so a source list that silently lost a file fails to link or fails here:
 *  1. libjpeg API, 8-bit baseline (the wrapper `-8.c` files);
 *  2. libjpeg API, 8-bit arithmetic coding (jcarith.c/jdarith.c);
 *  3. libjpeg API, 12-bit samples (the wrapper `-12.c` files, a second compile of the same code);
 *  4. TurboJPEG API (turbojpeg.c), whose header read must report the image's dimensions, then a
 *     round trip through a PNG-free path.
 * JPEG is lossy: the comparison is a mean absolute error bound on a smooth image.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <setjmp.h>
#include <jpeglib.h>
#include <turbojpeg.h>

#define W 64
#define H 48

#define STR_(x) #x
#define STR(x) STR_(x)

static int version_is_declared(const char *what, const char *got) {
    const char *want = getenv("REGISTRY_TEST_UPSTREAM_VERSION");
    if (want == NULL || *want == '\0') {
        printf("FAIL: REGISTRY_TEST_UPSTREAM_VERSION is not set (run this through tools/test.py)\n");
        return 0;
    }
    if (strcmp(want, got) != 0) {
        printf("FAIL: %s reports version %s, but package.toml declares %s\n", what, got, want);
        return 0;
    }
    return 1;
}

struct errmgr {
    struct jpeg_error_mgr pub;
    jmp_buf jb;
};

static void error_exit(j_common_ptr cinfo) {
    struct errmgr *e = (struct errmgr *)cinfo->err;
    longjmp(e->jb, 1);
}

/* A smooth colour image: gradients JPEG can reproduce within a small error. */
static void pixel(int x, int y, unsigned *r, unsigned *g, unsigned *b) {
    *r = (unsigned)(x * 255 / (W - 1));
    *g = (unsigned)(y * 255 / (H - 1));
    *b = (unsigned)((x + y) * 255 / (W + H - 2));
}

/* depth 8 or 12; arith selects arithmetic entropy coding. Returns 0 on success. */
static int roundtrip(int depth, int arith, const char *name) {
    int maxv = (1 << depth) - 1;
    struct jpeg_compress_struct c;
    struct jpeg_decompress_struct d;
    struct errmgr ec, ed;
    unsigned char *jpg = NULL;
    unsigned long jpglen = 0;
    unsigned short *src = malloc((size_t)W * H * 3 * sizeof(unsigned short));
    unsigned short *dst = malloc((size_t)W * H * 3 * sizeof(unsigned short));
    int x, y, i, rc = 1;
    double err = 0;

    for (y = 0; y < H; y++)
        for (x = 0; x < W; x++) {
            unsigned r, g, b;
            pixel(x, y, &r, &g, &b);
            src[(y * W + x) * 3 + 0] = (unsigned short)(r * (unsigned)maxv / 255);
            src[(y * W + x) * 3 + 1] = (unsigned short)(g * (unsigned)maxv / 255);
            src[(y * W + x) * 3 + 2] = (unsigned short)(b * (unsigned)maxv / 255);
        }

    c.err = jpeg_std_error(&ec.pub);
    ec.pub.error_exit = error_exit;
    if (setjmp(ec.jb)) { jpeg_destroy_compress(&c); free(jpg); goto out; }
    jpeg_create_compress(&c);
    jpeg_mem_dest(&c, &jpg, &jpglen);
    c.image_width = W; c.image_height = H;
    c.input_components = 3; c.in_color_space = JCS_RGB;
    jpeg_set_defaults(&c);
    c.data_precision = depth;
    jpeg_set_quality(&c, 95, TRUE);
    if (arith) c.arith_code = TRUE;
    jpeg_start_compress(&c, TRUE);
    for (y = 0; y < H; y++) {
        if (depth == 8) {
            unsigned char row8[W * 3];
            JSAMPROW rp = row8;
            for (i = 0; i < W * 3; i++) row8[i] = (unsigned char)src[y * W * 3 + i];
            jpeg_write_scanlines(&c, &rp, 1);
        } else {
            J12SAMPLE row12[W * 3];
            J12SAMPROW rp = row12;
            for (i = 0; i < W * 3; i++) row12[i] = (J12SAMPLE)src[y * W * 3 + i];
            jpeg12_write_scanlines(&c, &rp, 1);
        }
    }
    jpeg_finish_compress(&c);
    jpeg_destroy_compress(&c);

    if (jpglen < 200 || jpglen > (unsigned long)W * H * 3) {
        printf("FAIL: %s: implausible compressed size %lu\n", name, jpglen);
        free(jpg); goto out;
    }

    d.err = jpeg_std_error(&ed.pub);
    ed.pub.error_exit = error_exit;
    if (setjmp(ed.jb)) { jpeg_destroy_decompress(&d); free(jpg); printf("FAIL: %s: decode error\n", name); goto out; }
    jpeg_create_decompress(&d);
    jpeg_mem_src(&d, jpg, jpglen);
    jpeg_read_header(&d, TRUE);
    if ((int)d.image_width != W || (int)d.image_height != H || d.num_components != 3 ||
        d.data_precision != depth) {
        printf("FAIL: %s: header says %ux%u, %d components, %d bits\n", name, d.image_width,
               d.image_height, d.num_components, d.data_precision);
        jpeg_destroy_decompress(&d); free(jpg); goto out;
    }
    d.out_color_space = JCS_RGB;
    jpeg_start_decompress(&d);
    for (y = 0; y < H; y++) {
        if (depth == 8) {
            unsigned char row8[W * 3];
            JSAMPROW rp = row8;
            jpeg_read_scanlines(&d, &rp, 1);
            for (i = 0; i < W * 3; i++) dst[y * W * 3 + i] = row8[i];
        } else {
            J12SAMPLE row12[W * 3];
            J12SAMPROW rp = row12;
            jpeg12_read_scanlines(&d, &rp, 1);
            for (i = 0; i < W * 3; i++) dst[y * W * 3 + i] = (unsigned short)row12[i];
        }
    }
    jpeg_finish_decompress(&d);
    jpeg_destroy_decompress(&d);
    free(jpg);

    for (i = 0; i < W * H * 3; i++)
        err += abs((int)src[i] - (int)dst[i]);
    err /= (double)(W * H * 3) * (double)maxv / 255.0;  /* in 8-bit units */
    if (err > 2.0) {
        printf("FAIL: %s: mean absolute error %.2f (limit 2.0, in 8-bit units)\n", name, err);
        goto out;
    }
    printf("%s: %lu bytes, mean error %.3f\n", name, jpglen, err);
    rc = 0;
out:
    free(src); free(dst);
    return rc;
}

static int turbo(void) {
    tjhandle h = tj3Init(TJINIT_COMPRESS);
    tjhandle dh = tj3Init(TJINIT_DECOMPRESS);
    unsigned char *img = malloc((size_t)W * H * 3), *out = malloc((size_t)W * H * 3);
    unsigned char *jpg = NULL;
    size_t jpglen = 0;
    int x, y, i, rc = 1;
    double err = 0;

    if (!h || !dh) { printf("FAIL: tj3Init\n"); goto out; }
    for (y = 0; y < H; y++)
        for (x = 0; x < W; x++) {
            unsigned r, g, b;
            pixel(x, y, &r, &g, &b);
            img[(y * W + x) * 3 + 0] = (unsigned char)r;
            img[(y * W + x) * 3 + 1] = (unsigned char)g;
            img[(y * W + x) * 3 + 2] = (unsigned char)b;
        }
    tj3Set(h, TJPARAM_QUALITY, 90);
    tj3Set(h, TJPARAM_SUBSAMP, TJSAMP_420);
    if (tj3Compress8(h, img, W, 0, H, TJPF_RGB, &jpg, &jpglen) != 0) {
        printf("FAIL: tj3Compress8: %s\n", tj3GetErrorStr(h)); goto out;
    }
    if (tj3DecompressHeader(dh, jpg, jpglen) != 0) {
        printf("FAIL: tj3DecompressHeader: %s\n", tj3GetErrorStr(dh)); goto out;
    }
    if (tj3Get(dh, TJPARAM_JPEGWIDTH) != W || tj3Get(dh, TJPARAM_JPEGHEIGHT) != H ||
        tj3Get(dh, TJPARAM_SUBSAMP) != TJSAMP_420) {
        printf("FAIL: turbojpeg header %dx%d subsamp %d\n", tj3Get(dh, TJPARAM_JPEGWIDTH),
               tj3Get(dh, TJPARAM_JPEGHEIGHT), tj3Get(dh, TJPARAM_SUBSAMP));
        goto out;
    }
    if (tj3Decompress8(dh, jpg, jpglen, out, 0, TJPF_RGB) != 0) {
        printf("FAIL: tj3Decompress8: %s\n", tj3GetErrorStr(dh)); goto out;
    }
    for (i = 0; i < W * H * 3; i++) err += abs((int)img[i] - (int)out[i]);
    err /= (double)(W * H * 3);
    if (err > 4.0) { printf("FAIL: turbojpeg mean error %.2f\n", err); goto out; }
    printf("turbojpeg: %zu bytes, mean error %.3f\n", jpglen, err);
    rc = 0;
out:
    tj3Free(jpg);
    if (h) tj3Destroy(h);
    if (dh) tj3Destroy(dh);
    free(img); free(out);
    return rc;
}

int main(void) {
    if (!version_is_declared("libjpeg-turbo", STR(LIBJPEG_TURBO_VERSION))) return 1;
    if (roundtrip(8, 0, "8-bit huffman")) return 1;
    if (roundtrip(8, 1, "8-bit arithmetic")) return 1;
    if (roundtrip(12, 0, "12-bit huffman")) return 1;
    if (turbo()) return 1;
    printf("OK libjpeg-turbo %s: 8/12-bit, arithmetic and TurboJPEG round trips\n", STR(LIBJPEG_TURBO_VERSION));
    return 0;
}
