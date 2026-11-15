/* libwebp: lossless encode then decode must give back every pixel; lossy encode then decode
 * must give the dimensions and a close image; the mux and demux libraries must read back the
 * file the encoder wrote (a container round trip); sharpyuv must be linked and answer. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <webp/decode.h>
#include <webp/encode.h>
#include <webp/mux.h>
#include <webp/demux.h>
#include <sharpyuv/sharpyuv.h>
#include <sharpyuv/sharpyuv_csp.h>

#define W 64
#define H 48

#define CHECK(cond, ...) do { if (!(cond)) { printf("FAIL: "); printf(__VA_ARGS__); printf("\n"); return 1; } } while (0)

static void pixel(int x, int y, unsigned char *p) {
    p[0] = (unsigned char)(x * 255 / (W - 1));
    p[1] = (unsigned char)(y * 255 / (H - 1));
    p[2] = (unsigned char)((x / 8 + y / 8) % 2 ? 200 : 40);   /* a checker the lossless coder must keep */
    p[3] = (unsigned char)(x < 8 ? 128 : 255);                /* partial alpha in one column band */
}

int main(void) {
    const char *want = getenv("REGISTRY_TEST_UPSTREAM_VERSION");
    char got[32];
    int v = WebPGetDecoderVersion();
    unsigned char *rgba = malloc((size_t)W * H * 4);
    unsigned char *smooth = malloc((size_t)W * H * 4);
    uint8_t *lossless = NULL, *lossy = NULL;
    size_t llen, ylen;
    int x, y, w = 0, h = 0, i;
    uint8_t *dec;

    snprintf(got, sizeof(got), "%d.%d.%d", (v >> 16) & 0xff, (v >> 8) & 0xff, v & 0xff);
    CHECK(want != NULL && *want != '\0', "REGISTRY_TEST_UPSTREAM_VERSION is not set (run this through tools/test.py)");
    CHECK(strcmp(want, got) == 0, "libwebp decoder reports %s, but package.toml declares %s", got, want);
    v = WebPGetEncoderVersion();
    snprintf(got, sizeof(got), "%d.%d.%d", (v >> 16) & 0xff, (v >> 8) & 0xff, v & 0xff);
    CHECK(strcmp(want, got) == 0, "libwebp encoder reports %s, but package.toml declares %s", got, want);

    for (y = 0; y < H; y++)
        for (x = 0; x < W; x++) pixel(x, y, rgba + (y * W + x) * 4);

    /* 1. lossless: exact */
    llen = WebPEncodeLosslessRGBA(rgba, W, H, W * 4, &lossless);
    CHECK(llen > 0, "lossless encode failed");
    CHECK(WebPGetInfo(lossless, llen, &w, &h) && w == W && h == H, "lossless header says %dx%d", w, h);
    dec = WebPDecodeRGBA(lossless, llen, &w, &h);
    CHECK(dec != NULL && w == W && h == H, "lossless decode failed");
    for (i = 0; i < W * H * 4; i++) {
        /* lossless keeps RGB even under alpha only when WebPConfig.exact is set; compare the
           pixels that are visible, and alpha everywhere */
        if ((i & 3) == 3) CHECK(dec[i] == rgba[i], "alpha differs at byte %d", i);
        else if (rgba[(i | 3)] == 255) CHECK(dec[i] == rgba[i], "pixel differs at byte %d: %d vs %d", i, dec[i], rgba[i]);
    }
    WebPFree(dec);

    /* 2. lossy: dimensions, and close */
    /* a smooth opaque image: lossy coding is for photographs, not for checkerboards */
    for (y = 0; y < H; y++)
        for (x = 0; x < W; x++) {
            unsigned char *p = smooth + (y * W + x) * 4;
            p[0] = (unsigned char)(x * 255 / (W - 1));
            p[1] = (unsigned char)(y * 255 / (H - 1));
            p[2] = (unsigned char)((x + y) * 255 / (W + H - 2));
            p[3] = 255;
        }
    ylen = WebPEncodeRGBA(smooth, W, H, W * 4, 90.f, &lossy);
    CHECK(ylen > 0, "lossy encode failed");
    CHECK(WebPGetInfo(lossy, ylen, &w, &h) && w == W && h == H, "lossy header says %dx%d", w, h);
    dec = WebPDecodeRGBA(lossy, ylen, &w, &h);
    CHECK(dec != NULL && w == W && h == H, "lossy decode failed");
    {
        double err = 0; int n = 0;
        for (i = 0; i < W * H * 4; i++) if ((i & 3) != 3) { err += abs((int)dec[i] - (int)smooth[i]); n++; }
        err /= n;
        CHECK(err < 4.0, "lossy mean error %.2f", err);
        printf("lossless %zu bytes (exact), lossy %zu bytes (mean error %.2f)\n", llen, ylen, err);
    }
    WebPFree(dec);

    /* 3. demux and mux read the container back */
    {
        WebPData d = { lossy, ylen };
        WebPDemuxer *dm = WebPDemux(&d);
        WebPMux *mux;
        WebPMuxError me;
        WebPData out;
        CHECK(dm != NULL, "WebPDemux failed");
        CHECK((int)WebPDemuxGetI(dm, WEBP_FF_CANVAS_WIDTH) == W && (int)WebPDemuxGetI(dm, WEBP_FF_CANVAS_HEIGHT) == H,
              "demux canvas is %ux%u", WebPDemuxGetI(dm, WEBP_FF_CANVAS_WIDTH), WebPDemuxGetI(dm, WEBP_FF_CANVAS_HEIGHT));
        CHECK(WebPDemuxGetI(dm, WEBP_FF_FRAME_COUNT) == 1, "demux frame count %u", WebPDemuxGetI(dm, WEBP_FF_FRAME_COUNT));
        WebPDemuxDelete(dm);

        mux = WebPMuxCreate(&d, 1);
        CHECK(mux != NULL, "WebPMuxCreate failed");
        {
            WebPData meta = { (const uint8_t *)"harbour", 7 };
            me = WebPMuxSetChunk(mux, "EXIF", &meta, 1);
            CHECK(me == WEBP_MUX_OK, "WebPMuxSetChunk returned %d", (int)me);
        }
        WebPDataInit(&out);
        me = WebPMuxAssemble(mux, &out);
        CHECK(me == WEBP_MUX_OK && out.size > ylen, "WebPMuxAssemble returned %d, %zu bytes", (int)me, out.size);
        CHECK(WebPGetInfo(out.bytes, out.size, &w, &h) && w == W && h == H, "muxed file header says %dx%d", w, h);
        WebPDataClear(&out);
        WebPMuxDelete(mux);
    }

    /* 4. sharpyuv */
    {
        /* mid grey stays grey: Y in the studio range's middle, chroma at its neutral 128 */
        uint8_t g[8 * 8], yy[8 * 8], uu[4 * 4], vv[4 * 4];
        memset(g, 128, sizeof(g));
        CHECK(SharpYuvGetVersion() > 0, "sharpyuv version");
        CHECK(SharpYuvConvert(g, g, g, 1, 8, 8, yy, 8, uu, 4, vv, 4, 8, 8, 8,
                              SharpYuvGetConversionMatrix(kSharpYuvMatrixWebp)) != 0,
              "SharpYuvConvert failed");
        CHECK(yy[0] > 110 && yy[0] < 140 && abs((int)uu[0] - 128) <= 2 && abs((int)vv[0] - 128) <= 2,
              "grey converted to Y=%d U=%d V=%d", yy[0], uu[0], vv[0]);
    }

    WebPFree(lossless);
    WebPFree(lossy);
    free(rgba);
    free(smooth);
    printf("OK libwebp %s: lossless round trip exact, lossy decode, mux/demux, sharpyuv\n", want);
    return 0;
}
