/* Exercise libbz2 at runtime: one-shot and streaming round trips, and a known stream that this
 * build did not produce (the 14-byte .bz2 of an empty file, which every bzip2 writes). */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <bzlib.h>

/* tools/test.py exports the upstream version that package.toml declares. BZ2_bzlibVersion()
 * returns "1.0.8, 13-Jul-2019": the version, then a comma and the release date. */
static int version_is_declared(const char *what, const char *got) {
    const char *want = getenv("REGISTRY_TEST_UPSTREAM_VERSION");
    if (want == NULL || *want == '\0') {
        printf("FAIL: REGISTRY_TEST_UPSTREAM_VERSION is not set (run this through tools/test.py)\n");
        return 0;
    }
    size_t n = strlen(want);
    if (strncmp(want, got, n) != 0 || (got[n] != ',' && got[n] != '\0')) {
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

#define RAW_SIZE (300u * 1024u)

/* Compressible, deterministic, and not periodic. */
static void fill(unsigned char *buf, size_t n) {
    static const char *words[8] = {"harbour ", "registry", "compress", "bzip2   ", "burrows ", "wheeler ",
                                   "huffman ", "payload "};
    unsigned state = 0x9e3779b9u;
    for (size_t i = 0; i < n; i += 8) {
        state = state * 1103515245u + 12345u;
        const char *w = words[(state >> 16) & 7];
        for (size_t j = 0; j < 8 && i + j < n; j++) buf[i + j] = (unsigned char) w[j];
    }
}

int main(void) {
    if (!version_is_declared("BZ2_bzlibVersion()", BZ2_bzlibVersion())) return 1;

    /* The well-known empty stream: "BZh9", the end-of-stream magic and a zero CRC. */
    {
        static const unsigned char empty[] = {0x42, 0x5a, 0x68, 0x39, 0x17, 0x72, 0x45,
                                              0x38, 0x50, 0x90, 0x00, 0x00, 0x00, 0x00};
        char out[16];
        unsigned int out_len = sizeof(out);
        CHECK(BZ2_bzBuffToBuffDecompress(out, &out_len, (char *) empty, sizeof(empty), 0, 0) == BZ_OK);
        CHECK(out_len == 0);
    }

    unsigned char *raw = malloc(RAW_SIZE);
    unsigned char *back = malloc(RAW_SIZE);
    unsigned int bound = RAW_SIZE + RAW_SIZE / 100 + 600; /* bzip2's documented worst case */
    char *comp = malloc(bound);
    if (raw == NULL || back == NULL || comp == NULL) {
        printf("FAIL: out of memory\n");
        return 1;
    }
    fill(raw, RAW_SIZE);

    /* One-shot. */
    unsigned int csize = bound;
    CHECK(BZ2_bzBuffToBuffCompress(comp, &csize, (char *) raw, RAW_SIZE, 9, 0, 0) == BZ_OK);
    CHECK(csize > 4 && csize < RAW_SIZE && memcmp(comp, "BZh9", 4) == 0);
    unsigned int dsize = RAW_SIZE;
    memset(back, 0, RAW_SIZE);
    CHECK(BZ2_bzBuffToBuffDecompress((char *) back, &dsize, comp, csize, 0, 0) == BZ_OK);
    CHECK(dsize == RAW_SIZE);
    CHECK(memcmp(raw, back, RAW_SIZE) == 0);

    /* A flipped bit must be detected by the block CRC. */
    comp[csize / 2] ^= 0x10;
    dsize = RAW_SIZE;
    CHECK(BZ2_bzBuffToBuffDecompress((char *) back, &dsize, comp, csize, 0, 0) != BZ_OK);
    comp[csize / 2] ^= 0x10;

    /* Streaming: compress in 7000-byte pieces, decompress through a small output window. */
    {
        bz_stream s;
        memset(&s, 0, sizeof(s));
        CHECK(BZ2_bzCompressInit(&s, 1, 0, 0) == BZ_OK);
        s.next_out = comp;
        s.avail_out = bound;
        size_t pos = 0;
        while (pos < RAW_SIZE) {
            size_t n = RAW_SIZE - pos < 7000 ? RAW_SIZE - pos : 7000;
            s.next_in = (char *) raw + pos;
            s.avail_in = (unsigned int) n;
            CHECK(BZ2_bzCompress(&s, BZ_RUN) == BZ_RUN_OK);
            pos += n;
        }
        int rc = BZ_FINISH_OK;
        while (rc == BZ_FINISH_OK) rc = BZ2_bzCompress(&s, BZ_FINISH);
        CHECK(rc == BZ_STREAM_END);
        unsigned int streamed = bound - s.avail_out;
        BZ2_bzCompressEnd(&s);

        memset(&s, 0, sizeof(s));
        CHECK(BZ2_bzDecompressInit(&s, 0, 0) == BZ_OK);
        s.next_in = comp;
        s.avail_in = streamed;
        size_t done = 0;
        memset(back, 0, RAW_SIZE);
        rc = BZ_OK;
        while (rc == BZ_OK) {
            char window[4096];
            s.next_out = window;
            s.avail_out = sizeof(window);
            rc = BZ2_bzDecompress(&s);
            size_t produced = sizeof(window) - s.avail_out;
            if (done + produced > RAW_SIZE) { rc = BZ_DATA_ERROR; break; }
            memcpy(back + done, window, produced);
            done += produced;
        }
        CHECK(rc == BZ_STREAM_END);
        CHECK(done == RAW_SIZE);
        CHECK(memcmp(raw, back, RAW_SIZE) == 0);
        BZ2_bzDecompressEnd(&s);
    }

    free(raw);
    free(back);
    free(comp);

    if (failures != 0) {
        printf("%d check(s) failed\n", failures);
        return 1;
    }
    printf("OK bzip2 %s: one-shot and streaming round trips, known empty stream, corruption detected\n",
           BZ2_bzlibVersion());
    return 0;
}
