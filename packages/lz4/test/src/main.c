/* Exercise lz4 at runtime: the block API, the high-compression codec, the frame
 * format (which is what pulls in lz4frame.c and the bundled xxhash.c for the
 * content checksum) and the FILE* helpers in lz4file.c (by symbol: a source list
 * that dropped lib/lz4file.c would fail to link). */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <lz4.h>
#include <lz4hc.h>
#include <lz4frame.h>
#include <lz4file.h>

/* lib/xxhash.h is lz4's private copy (namespaced LZ4_XXH*, no XXH3). Exposed, it
 * shadowed the registry's xxhash package for a consumer listing lz4 first. */
#if defined(__has_include)
#  if __has_include(<xxhash.h>)
#    error "lz4 exposes its private xxhash.h to consumers"
#  endif
#endif

/* tools/test.py exports the upstream version that package.toml declares. The
 * library under test must report exactly that. */
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

static int failures = 0;

#define CHECK(cond)                                                            \
    do {                                                                       \
        if (!(cond)) {                                                         \
            printf("FAIL line %d: %s\n", __LINE__, #cond);                     \
            failures++;                                                        \
        }                                                                      \
    } while (0)

#define RAW_SIZE (200u * 1024u)

/* Compressible (LZ77 finds matches), deterministic, and not periodic: a pseudo-random
 * choice among a few words. */
static void fill(unsigned char *buf, size_t n) {
    static const char *words[8] = {"harbour ", "registry", "compress", "lz4 data", "frame   ", "block   ",
                                   "checksum", "payload "};
    unsigned state = 0x9e3779b9u;
    for (size_t i = 0; i < n; i += 8) {
        state = state * 1103515245u + 12345u;
        const char *w = words[(state >> 16) & 7];
        for (size_t j = 0; j < 8 && i + j < n; j++) buf[i + j] = (unsigned char) w[j];
    }
}

int main(void) {
    if (!version_is_declared("LZ4_versionString()", LZ4_versionString())) return 1;
    CHECK(LZ4_versionNumber() == LZ4_VERSION_NUMBER);

    unsigned char *raw = malloc(RAW_SIZE);
    unsigned char *back = malloc(RAW_SIZE);
    int bound = LZ4_compressBound((int) RAW_SIZE);
    char *comp = malloc((size_t) bound);
    if (raw == NULL || back == NULL || comp == NULL) {
        printf("FAIL: out of memory\n");
        return 1;
    }
    fill(raw, RAW_SIZE);

    /* Block API, fast mode. */
    int csize = LZ4_compress_default((const char *) raw, comp, (int) RAW_SIZE, bound);
    CHECK(csize > 0 && csize < (int) RAW_SIZE);
    memset(back, 0, RAW_SIZE);
    int dsize = LZ4_decompress_safe(comp, (char *) back, csize, (int) RAW_SIZE);
    CHECK(dsize == (int) RAW_SIZE);
    CHECK(memcmp(raw, back, RAW_SIZE) == 0);

    /* lz4hc.c: the high-compression codec must do at least as well. */
    int hsize = LZ4_compress_HC((const char *) raw, comp, (int) RAW_SIZE, bound, LZ4HC_CLEVEL_MAX);
    CHECK(hsize > 0 && hsize <= csize);
    memset(back, 0, RAW_SIZE);
    dsize = LZ4_decompress_safe(comp, (char *) back, hsize, (int) RAW_SIZE);
    CHECK(dsize == (int) RAW_SIZE);
    CHECK(memcmp(raw, back, RAW_SIZE) == 0);

    /* lz4frame.c (+ xxhash.c): frame round trip with the content checksum on. */
    LZ4F_preferences_t prefs;
    memset(&prefs, 0, sizeof(prefs));
    prefs.frameInfo.contentChecksumFlag = LZ4F_contentChecksumEnabled;
    prefs.frameInfo.contentSize = RAW_SIZE;
    size_t fbound = LZ4F_compressFrameBound(RAW_SIZE, &prefs);
    char *frame = malloc(fbound);
    CHECK(frame != NULL);
    if (frame != NULL) {
        size_t fsize = LZ4F_compressFrame(frame, fbound, raw, RAW_SIZE, &prefs);
        CHECK(!LZ4F_isError(fsize));

        LZ4F_dctx *dctx = NULL;
        CHECK(!LZ4F_isError(LZ4F_createDecompressionContext(&dctx, LZ4F_VERSION)));
        memset(back, 0, RAW_SIZE);
        size_t src_pos = 0, dst_pos = 0;
        size_t rc = 1;
        while (rc != 0 && !LZ4F_isError(rc) && src_pos < fsize) {
            size_t src_n = fsize - src_pos;
            size_t dst_n = RAW_SIZE - dst_pos;
            rc = LZ4F_decompress(dctx, back + dst_pos, &dst_n, frame + src_pos, &src_n, NULL);
            src_pos += src_n;
            dst_pos += dst_n;
        }
        CHECK(rc == 0);           /* the frame ended, so the checksum matched */
        CHECK(dst_pos == RAW_SIZE);
        CHECK(memcmp(raw, back, RAW_SIZE) == 0);

        /* A flipped payload bit must be caught by the xxHash content checksum. */
        frame[fsize / 2] ^= 0x40;
        LZ4F_resetDecompressionContext(dctx);
        src_pos = 0; dst_pos = 0; rc = 1;
        while (rc != 0 && !LZ4F_isError(rc) && src_pos < fsize) {
            size_t src_n = fsize - src_pos;
            size_t dst_n = RAW_SIZE - dst_pos;
            rc = LZ4F_decompress(dctx, back + dst_pos, &dst_n, frame + src_pos, &src_n, NULL);
            src_pos += src_n;
            dst_pos += dst_n;
        }
        CHECK(LZ4F_isError(rc) || memcmp(raw, back, RAW_SIZE) != 0);
        LZ4F_freeDecompressionContext(dctx);
        free(frame);
    }

    /* lz4file.c: write a frame to a FILE*, read it back through the same API. */
    {
        const char *path = "lz4-test.tmp";
        FILE *fp = fopen(path, "w+b");
        CHECK(fp != NULL);
        if (fp != NULL) {
            LZ4_writeFile_t *wf = NULL;
            CHECK(!LZ4F_isError(LZ4F_writeOpen(&wf, fp, &prefs)));
            CHECK(!LZ4F_isError(LZ4F_write(wf, raw, RAW_SIZE)));
            CHECK(!LZ4F_isError(LZ4F_writeClose(wf)));
            rewind(fp);
            LZ4_readFile_t *rf = NULL;
            CHECK(!LZ4F_isError(LZ4F_readOpen(&rf, fp)));
            memset(back, 0, RAW_SIZE);
            LZ4F_errorCode_t got = LZ4F_read(rf, back, RAW_SIZE);
            CHECK(!LZ4F_isError(got) && got == RAW_SIZE);
            CHECK(memcmp(raw, back, RAW_SIZE) == 0);
            CHECK(!LZ4F_isError(LZ4F_readClose(rf)));
            fclose(fp);
            remove(path);
        }
    }

    free(raw);
    free(back);
    free(comp);

    if (failures != 0) {
        printf("%d check(s) failed\n", failures);
        return 1;
    }
    printf("OK lz4 %s: block, HC, frame and FILE* round trips\n", LZ4_versionString());
    return 0;
}
