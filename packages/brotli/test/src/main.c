/* Exercise all three brotli libraries at runtime.
 *
 * The consumer selects them with `targets = ["brotlienc", "brotlidec", "brotlicommon"]`, and this
 * program references a symbol of each, so a library missing from the link fails to link:
 *   brotlienc    BrotliEncoderCompress, BrotliEncoderVersion
 *   brotlidec    BrotliDecoderDecompress, BrotliDecoderVersion
 *   brotlicommon BrotliSharedDictionaryCreateInstance
 * The decoder is also checked against a stream from upstream's own test data
 * (tests/testdata/10x10y.compressed), which this build did not produce. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <brotli/decode.h>
#include <brotli/encode.h>
#include <brotli/shared_dictionary.h>

/* tools/test.py exports the upstream version that package.toml declares. */
static int version_is_declared(const char *what, uint32_t v) {
    char got[32];
    snprintf(got, sizeof(got), "%u.%u.%u", (unsigned) (v >> 24), (unsigned) ((v >> 12) & 0xfff),
             (unsigned) (v & 0xfff));
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

/* Compressible, deterministic, and not periodic. */
static void fill(uint8_t *buf, size_t n) {
    static const char *words[8] = {"harbour ", "registry", "compress", "brotli  ", "window  ", "quality ",
                                   "metablck", "payload "};
    unsigned state = 0x9e3779b9u;
    for (size_t i = 0; i < n; i += 8) {
        state = state * 1103515245u + 12345u;
        const char *w = words[(state >> 16) & 7];
        for (size_t j = 0; j < 8 && i + j < n; j++) buf[i + j] = (uint8_t) w[j];
    }
}

int main(void) {
    if (!version_is_declared("BrotliEncoderVersion()", BrotliEncoderVersion())) return 1;
    if (!version_is_declared("BrotliDecoderVersion()", BrotliDecoderVersion())) return 1;

    /* brotlicommon, directly. */
    BrotliSharedDictionary *sd = BrotliSharedDictionaryCreateInstance(NULL, NULL, NULL);
    CHECK(sd != NULL);
    if (sd != NULL) BrotliSharedDictionaryDestroyInstance(sd);

    /* Known answer: a stream produced by upstream, not by this build. */
    {
        static const uint8_t stream[] = {0x1b, 0x13, 0x00, 0x00, 0xa4, 0xb0, 0xb2, 0xea, 0x81, 0x47, 0x02, 0x8a};
        uint8_t out[64];
        size_t out_size = sizeof(out);
        CHECK(BrotliDecoderDecompress(sizeof(stream), stream, &out_size, out) == BROTLI_DECODER_RESULT_SUCCESS);
        CHECK(out_size == 20 && memcmp(out, "XXXXXXXXXXYYYYYYYYYY", 20) == 0);
    }

    uint8_t *raw = malloc(RAW_SIZE);
    uint8_t *back = malloc(RAW_SIZE);
    size_t bound = BrotliEncoderMaxCompressedSize(RAW_SIZE);
    uint8_t *comp = malloc(bound);
    if (raw == NULL || back == NULL || comp == NULL) {
        printf("FAIL: out of memory\n");
        return 1;
    }
    fill(raw, RAW_SIZE);

    /* One-shot round trip at a fast and at the best quality. */
    static const int qualities[2] = {1, 11};
    size_t sizes[2] = {0, 0};
    for (int q = 0; q < 2; q++) {
        size_t csize = bound;
        CHECK(BrotliEncoderCompress(qualities[q], BROTLI_DEFAULT_WINDOW, BROTLI_MODE_GENERIC, RAW_SIZE, raw,
                                    &csize, comp) == BROTLI_TRUE);
        CHECK(csize > 0 && csize < RAW_SIZE);
        sizes[q] = csize;
        size_t dsize = RAW_SIZE;
        memset(back, 0, RAW_SIZE);
        CHECK(BrotliDecoderDecompress(csize, comp, &dsize, back) == BROTLI_DECODER_RESULT_SUCCESS);
        CHECK(dsize == RAW_SIZE);
        CHECK(memcmp(raw, back, RAW_SIZE) == 0);
    }
    CHECK(sizes[1] <= sizes[0]);

    /* Streaming: encode with the instance API, decode through small output windows. */
    {
        BrotliEncoderState *enc = BrotliEncoderCreateInstance(NULL, NULL, NULL);
        CHECK(enc != NULL);
        size_t csize = 0;
        if (enc != NULL) {
            BrotliEncoderSetParameter(enc, BROTLI_PARAM_QUALITY, 5);
            const uint8_t *in = raw;
            size_t in_left = RAW_SIZE;
            while (!BrotliEncoderIsFinished(enc)) {
                size_t out_left = bound - csize;
                uint8_t *out = comp + csize;
                CHECK(BrotliEncoderCompressStream(enc, BROTLI_OPERATION_FINISH, &in_left, &in, &out_left,
                                                  &out, NULL) == BROTLI_TRUE);
                csize = (size_t) (out - comp);
            }
            BrotliEncoderDestroyInstance(enc);
        }
        BrotliDecoderState *dec = BrotliDecoderCreateInstance(NULL, NULL, NULL);
        CHECK(dec != NULL);
        if (dec != NULL) {
            const uint8_t *in = comp;
            size_t in_left = csize;
            size_t done = 0;
            BrotliDecoderResult r = BROTLI_DECODER_RESULT_NEEDS_MORE_OUTPUT;
            memset(back, 0, RAW_SIZE);
            while (r == BROTLI_DECODER_RESULT_NEEDS_MORE_OUTPUT) {
                size_t chunk = RAW_SIZE - done < 4096 ? RAW_SIZE - done : 4096;
                uint8_t scratch[4096];
                uint8_t *out = scratch;
                size_t out_left = chunk;
                r = BrotliDecoderDecompressStream(dec, &in_left, &in, &out_left, &out, NULL);
                size_t produced = (size_t) (out - scratch);
                memcpy(back + done, scratch, produced);
                done += produced;
            }
            CHECK(r == BROTLI_DECODER_RESULT_SUCCESS);
            CHECK(done == RAW_SIZE);
            CHECK(memcmp(raw, back, RAW_SIZE) == 0);
            BrotliDecoderDestroyInstance(dec);
        }
    }

    free(raw);
    free(back);
    free(comp);

    if (failures != 0) {
        printf("%d check(s) failed\n", failures);
        return 1;
    }
    printf("OK brotli %u.%u.%u: encoder, decoder and common linked; one-shot, streaming and known-stream decode\n",
           (unsigned) (BrotliEncoderVersion() >> 24), (unsigned) ((BrotliEncoderVersion() >> 12) & 0xfff),
           (unsigned) (BrotliEncoderVersion() & 0xfff));
    return 0;
}
