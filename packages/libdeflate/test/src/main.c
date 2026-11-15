/* Exercise libdeflate at runtime: raw DEFLATE, zlib and gzip round trips at several levels, the
 * checksums (against published values), and a gzip member that this build did not produce.
 * libdeflate has no runtime version function, so the version check compares the
 * LIBDEFLATE_VERSION_STRING of the header the consumer actually compiled against, which comes
 * from the fetched source tree. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <libdeflate.h>

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

#define RAW_SIZE (300u * 1024u)

/* Compressible, deterministic, and not periodic. */
static void fill(unsigned char *buf, size_t n) {
    static const char *words[8] = {"harbour ", "registry", "compress", "deflate ", "huffman ", "inflate ",
                                   "checksum", "payload "};
    unsigned state = 0x9e3779b9u;
    for (size_t i = 0; i < n; i += 8) {
        state = state * 1103515245u + 12345u;
        const char *w = words[(state >> 16) & 7];
        for (size_t j = 0; j < 8 && i + j < n; j++) buf[i + j] = (unsigned char) w[j];
    }
}

int main(void) {
    if (!version_is_declared("LIBDEFLATE_VERSION_STRING", LIBDEFLATE_VERSION_STRING)) return 1;

    /* Published check values for the nine ASCII bytes "123456789". */
    CHECK(libdeflate_crc32(0, "123456789", 9) == 0xCBF43926u);   /* CRC-32/ISO-HDLC check value */
    CHECK(libdeflate_adler32(1, "Wikipedia", 9) == 0x11E60398u); /* the Adler-32 example on Wikipedia */

    /* A gzip member written by gzip(1): `printf hello | gzip -n`. */
    {
        static const unsigned char gz[] = {0x1f, 0x8b, 0x08, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x03, 0xcb,
                                           0x48, 0xcd, 0xc9, 0xc9, 0x07, 0x00, 0x86, 0xa6, 0x10, 0x36, 0x05,
                                           0x00, 0x00, 0x00};
        struct libdeflate_decompressor *d = libdeflate_alloc_decompressor();
        CHECK(d != NULL);
        if (d != NULL) {
            char out[16];
            size_t got = 0;
            CHECK(libdeflate_gzip_decompress(d, gz, sizeof(gz), out, sizeof(out), &got) == LIBDEFLATE_SUCCESS);
            CHECK(got == 5 && memcmp(out, "hello", 5) == 0);
            libdeflate_free_decompressor(d);
        }
    }

    unsigned char *raw = malloc(RAW_SIZE);
    unsigned char *back = malloc(RAW_SIZE);
    if (raw == NULL || back == NULL) {
        printf("FAIL: out of memory\n");
        return 1;
    }
    fill(raw, RAW_SIZE);

    static const int levels[4] = {1, 6, 9, 12};
    size_t smallest = (size_t) -1, largest = 0;
    for (int l = 0; l < 4; l++) {
        struct libdeflate_compressor *c = libdeflate_alloc_compressor(levels[l]);
        struct libdeflate_decompressor *d = libdeflate_alloc_decompressor();
        CHECK(c != NULL && d != NULL);
        if (c == NULL || d == NULL) continue;

        size_t bound = libdeflate_gzip_compress_bound(c, RAW_SIZE);
        char *comp = malloc(bound);
        CHECK(comp != NULL);
        if (comp == NULL) continue;

        /* gzip */
        size_t csize = libdeflate_gzip_compress(c, raw, RAW_SIZE, comp, bound);
        CHECK(csize > 18 && csize < RAW_SIZE);
        size_t actual = 0;
        memset(back, 0, RAW_SIZE);
        CHECK(libdeflate_gzip_decompress(d, comp, csize, back, RAW_SIZE, &actual) == LIBDEFLATE_SUCCESS);
        CHECK(actual == RAW_SIZE && memcmp(raw, back, RAW_SIZE) == 0);
        if (csize < smallest) smallest = csize;
        if (csize > largest) largest = csize;

        /* zlib */
        csize = libdeflate_zlib_compress(c, raw, RAW_SIZE, comp, bound);
        CHECK(csize > 6 && csize < RAW_SIZE);
        memset(back, 0, RAW_SIZE);
        CHECK(libdeflate_zlib_decompress(d, comp, csize, back, RAW_SIZE, &actual) == LIBDEFLATE_SUCCESS);
        CHECK(actual == RAW_SIZE && memcmp(raw, back, RAW_SIZE) == 0);

        /* raw deflate, and a corrupted copy of it */
        csize = libdeflate_deflate_compress(c, raw, RAW_SIZE, comp, bound);
        CHECK(csize > 0 && csize < RAW_SIZE);
        memset(back, 0, RAW_SIZE);
        CHECK(libdeflate_deflate_decompress(d, comp, csize, back, RAW_SIZE, &actual) == LIBDEFLATE_SUCCESS);
        CHECK(actual == RAW_SIZE && memcmp(raw, back, RAW_SIZE) == 0);

        csize = libdeflate_zlib_compress(c, raw, RAW_SIZE, comp, bound);
        comp[csize / 2] ^= 0x55;
        CHECK(libdeflate_zlib_decompress(d, comp, csize, back, RAW_SIZE, &actual) != LIBDEFLATE_SUCCESS ||
              memcmp(raw, back, RAW_SIZE) != 0);

        free(comp);
        libdeflate_free_compressor(c);
        libdeflate_free_decompressor(d);
    }
    CHECK(smallest < largest);   /* the levels really differ */

    free(raw);
    free(back);

    if (failures != 0) {
        printf("%d check(s) failed\n", failures);
        return 1;
    }
    printf("OK libdeflate %s: gzip, zlib and deflate round trips at four levels, known gzip member and checksums\n",
           LIBDEFLATE_VERSION_STRING);
    return 0;
}
