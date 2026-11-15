/* Encode a PNG into memory, then decode it again and compare every pixel.
 *
 * A link-only test would pass with a libpng whose read or write path was
 * silently dropped from the source list, or whose zlib dependency resolved to
 * nothing useful. This one needs both directions, the CRC checks, the filters
 * and a working deflate: the image is mostly flat, so a real encoder must
 * produce far fewer bytes than the raw pixels, and the decoder must give back
 * exactly what went in.
 */
#include <png.h>

#include <setjmp.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define W 64
#define H 48

/* tools/test.py exports the upstream version that package.toml declares. The
 * library under test must report exactly that: it is what catches a version
 * bump whose url, tag or sha256 points at the wrong source. */
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

typedef struct {
    unsigned char *data;
    size_t len, cap;
    size_t pos; /* read cursor */
} membuf;

static void mem_write(png_structp png, png_bytep bytes, size_t n) {
    membuf *b = (membuf *)png_get_io_ptr(png);
    if (b->len + n > b->cap) {
        b->cap = (b->len + n) * 2;
        b->data = (unsigned char *)realloc(b->data, b->cap);
        if (!b->data) png_error(png, "out of memory");
    }
    memcpy(b->data + b->len, bytes, n);
    b->len += n;
}

static void mem_flush(png_structp png) { (void)png; }

static void mem_read(png_structp png, png_bytep out, size_t n) {
    membuf *b = (membuf *)png_get_io_ptr(png);
    if (b->pos + n > b->len) png_error(png, "read past end of buffer");
    memcpy(out, b->data + b->pos, n);
    b->pos += n;
}

/* The pixel at (x, y): a flat field with a diagonal stripe and a gradient
 * corner, so filtering and compression both have something to do. */
static void pixel(int x, int y, unsigned char rgba[4]) {
    rgba[0] = (unsigned char)(x < 8 && y < 8 ? x * 32 : 10);
    rgba[1] = (unsigned char)((x + y) % 16 == 0 ? 255 : 20);
    rgba[2] = (unsigned char)(y * 5);
    rgba[3] = (unsigned char)(x == y ? 128 : 255);
}

static int encode(membuf *out) {
    png_structp png = png_create_write_struct(PNG_LIBPNG_VER_STRING, NULL, NULL, NULL);
    png_infop info = png ? png_create_info_struct(png) : NULL;
    if (!png || !info) return 0;
    if (setjmp(png_jmpbuf(png))) {
        png_destroy_write_struct(&png, &info);
        return 0;
    }
    png_set_write_fn(png, out, mem_write, mem_flush);
    png_set_IHDR(png, info, W, H, 8, PNG_COLOR_TYPE_RGBA, PNG_INTERLACE_NONE,
                 PNG_COMPRESSION_TYPE_DEFAULT, PNG_FILTER_TYPE_DEFAULT);
    png_set_compression_level(png, 9);
    png_write_info(png, info);
    unsigned char row[W * 4];
    for (int y = 0; y < H; y++) {
        for (int x = 0; x < W; x++) pixel(x, y, row + x * 4);
        png_write_row(png, row);
    }
    png_write_end(png, info);
    png_destroy_write_struct(&png, &info);
    return 1;
}

static int decode(membuf *in) {
    png_structp png = png_create_read_struct(PNG_LIBPNG_VER_STRING, NULL, NULL, NULL);
    png_infop info = png ? png_create_info_struct(png) : NULL;
    if (!png || !info) return 0;
    if (setjmp(png_jmpbuf(png))) {
        png_destroy_read_struct(&png, &info, NULL);
        printf("FAIL: libpng raised an error while decoding\n");
        return 0;
    }
    in->pos = 0;
    png_set_read_fn(png, in, mem_read);
    png_read_info(png, info);
    if (png_get_image_width(png, info) != W || png_get_image_height(png, info) != H ||
        png_get_bit_depth(png, info) != 8 ||
        png_get_color_type(png, info) != PNG_COLOR_TYPE_RGBA) {
        printf("FAIL: IHDR came back as %ux%u depth %d type %d\n", (unsigned)png_get_image_width(png, info),
               (unsigned)png_get_image_height(png, info), png_get_bit_depth(png, info),
               png_get_color_type(png, info));
        png_destroy_read_struct(&png, &info, NULL);
        return 0;
    }
    unsigned char row[W * 4];
    for (int y = 0; y < H; y++) {
        png_read_row(png, row, NULL);
        for (int x = 0; x < W; x++) {
            unsigned char want[4];
            pixel(x, y, want);
            if (memcmp(row + x * 4, want, 4) != 0) {
                printf("FAIL: pixel (%d,%d) is %u,%u,%u,%u, expected %u,%u,%u,%u\n", x, y, row[x * 4],
                       row[x * 4 + 1], row[x * 4 + 2], row[x * 4 + 3], want[0], want[1], want[2], want[3]);
                png_destroy_read_struct(&png, &info, NULL);
                return 0;
            }
        }
    }
    png_read_end(png, NULL);
    png_destroy_read_struct(&png, &info, NULL);
    return 1;
}

int main(void) {
    if (!version_is_declared("png_get_libpng_ver()", png_get_libpng_ver(NULL))) return 1;

    membuf buf = {0};
    if (!encode(&buf)) {
        printf("FAIL: encoding failed\n");
        return 1;
    }
    if (buf.len < 8 || png_sig_cmp(buf.data, 0, 8) != 0) {
        printf("FAIL: output does not start with the PNG signature\n");
        return 1;
    }
    /* Raw pixels are W*H*4 = 12288 bytes. A working deflate (zlib) gets this
     * image far below that; a store-only or broken one would not. */
    if (buf.len >= (size_t)W * H * 4 / 4) {
        printf("FAIL: %zu bytes is not compressed (raw is %d)\n", buf.len, W * H * 4);
        return 1;
    }
    if (!decode(&buf)) return 1;

    /* A corrupted stream must be rejected, not decoded: flip a byte inside
     * the IDAT data and the CRC check has to notice. */
    buf.data[buf.len / 2] ^= 0x55;
    png_structp png = png_create_read_struct(PNG_LIBPNG_VER_STRING, NULL, NULL, NULL);
    png_infop info = png_create_info_struct(png);
    int rejected = 0;
    if (setjmp(png_jmpbuf(png))) {
        rejected = 1;
    } else {
        buf.pos = 0;
        png_set_read_fn(png, &buf, mem_read);
        png_read_info(png, info);
        unsigned char row[W * 4];
        for (int y = 0; y < H; y++) png_read_row(png, row, NULL);
        png_read_end(png, NULL);
    }
    png_destroy_read_struct(&png, &info, NULL);
    if (!rejected) {
        printf("FAIL: a corrupted PNG was accepted\n");
        return 1;
    }

    printf("OK libpng %s: %dx%d RGBA encoded to %zu bytes, decoded pixel-exact, corruption rejected\n",
           png_get_libpng_ver(NULL), W, H, buf.len);
    free(buf.data);
    return 0;
}
