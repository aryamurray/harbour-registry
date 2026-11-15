/* Exercise Mbed TLS without touching the network.
 *
 * Each of the three libraries must be needed, so a target missing from the link surface fails:
 *  - libmbedcrypto: SHA-256 against the FIPS 180 vectors, AES-256-GCM round trip (and tag
 *    failure on a flipped bit);
 *  - libmbedx509: parse an embedded certificate, read its subject, verify its self-signature;
 *  - libmbedtls: build a client configuration and a context with mbedtls_ssl_config_defaults and
 *    mbedtls_ssl_setup, which pulls in the TLS 1.2/1.3 state machines. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "mbedtls/build_info.h"
#include "mbedtls/ctr_drbg.h"
#include "mbedtls/entropy.h"
#include "mbedtls/gcm.h"
#include "mbedtls/sha256.h"
#include "mbedtls/ssl.h"
#include "mbedtls/version.h"
#include "mbedtls/x509_crt.h"

static int failures = 0;

#define CHECK(cond, what)                                    \
    do {                                                     \
        if (!(cond)) {                                       \
            printf("FAIL: %s (line %d)\n", what, __LINE__);  \
            failures++;                                      \
        }                                                    \
    } while (0)

/* Self-signed ECDSA P-256 certificate generated for this test (CN=harbour-test.example),
 * valid 2026-11-15 .. 2126-10-22. */
static const char test_cert[] =
    "-----BEGIN CERTIFICATE-----\n"
    "MIIB5jCCAYugAwIBAgIUCywZAYJo9swmjrdyeqS6BIlxG2UwCgYIKoZIzj0EAwIw\n"
    "RzELMAkGA1UEBhMCTkwxGTAXBgNVBAoMEEhhcmJvdXIgUmVnaXN0cnkxHTAbBgNV\n"
    "BAMMFGhhcmJvdXItdGVzdC5leGFtcGxlMCAXDTI2MTExNTAzNTQ0NVoYDzIxMjYx\n"
    "MDIyMDM1NDQ1WjBHMQswCQYDVQQGEwJOTDEZMBcGA1UECgwQSGFyYm91ciBSZWdp\n"
    "c3RyeTEdMBsGA1UEAwwUaGFyYm91ci10ZXN0LmV4YW1wbGUwWTATBgcqhkjOPQIB\n"
    "BggqhkjOPQMBBwNCAAR2Fwm8hRz4yMaXn7pvDMwv8oepemfvkloSR+HNG3z6ISEj\n"
    "3l78yl91pKscPdiKwqjiRcFF5EIJiVQq8RHbC2nso1MwUTAdBgNVHQ4EFgQUcZAx\n"
    "WN/a66Ba6YE21UKjom6vYgswHwYDVR0jBBgwFoAUcZAxWN/a66Ba6YE21UKjom6v\n"
    "YgswDwYDVR0TAQH/BAUwAwEB/zAKBggqhkjOPQQDAgNJADBGAiEAkPeoaS+KLGIb\n"
    "ED85qPIf5bDGgO4KepyxT6VYXmORIRcCIQCQo/WZkPk/IoweWMS+vLIErqRHzE42\n"
    "35PdBlCq0PEl8A==\n"
    "-----END CERTIFICATE-----\n";

static int ignore_time(void *ctx, mbedtls_x509_crt *crt, int depth, uint32_t *flags) {
    (void) ctx; (void) crt; (void) depth;
    /* The signature is what is being checked here; the clock is not. */
    *flags &= ~(uint32_t) (MBEDTLS_X509_BADCERT_EXPIRED | MBEDTLS_X509_BADCERT_FUTURE);
    return 0;
}

static void hex(const unsigned char *in, size_t n, char *out) {
    static const char d[] = "0123456789abcdef";
    for (size_t i = 0; i < n; i++) {
        out[2 * i] = d[in[i] >> 4];
        out[2 * i + 1] = d[in[i] & 15];
    }
    out[2 * n] = '\0';
}

static void test_version(void) {
    const char *want = getenv("REGISTRY_TEST_UPSTREAM_VERSION");
    char got[18];
    mbedtls_version_get_string(got);
    if (want == NULL || *want == '\0') {
        printf("FAIL: REGISTRY_TEST_UPSTREAM_VERSION is not set (run this through tools/test.py)\n");
        failures++;
    } else if (strcmp(want, got) != 0 || strcmp(want, MBEDTLS_VERSION_STRING) != 0) {
        printf("FAIL: library reports %s (header %s), but package.toml declares %s\n",
               got, MBEDTLS_VERSION_STRING, want);
        failures++;
    }
    CHECK(mbedtls_version_get_number() == MBEDTLS_VERSION_NUMBER, "runtime and header version numbers agree");
}

static void test_sha256(void) {
    unsigned char out[32];
    char h[65];
    static const char *abc = "abc";
    static const char *two = "abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq";

    CHECK(mbedtls_sha256((const unsigned char *) abc, 3, out, 0) == 0, "sha256(abc) ran");
    hex(out, 32, h);
    CHECK(strcmp(h, "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad") == 0, "SHA-256 of \"abc\"");

    CHECK(mbedtls_sha256((const unsigned char *) two, strlen(two), out, 0) == 0, "sha256(448 bits) ran");
    hex(out, 32, h);
    CHECK(strcmp(h, "248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1") == 0, "SHA-256 of the 448-bit message");

    CHECK(mbedtls_sha256((const unsigned char *) "", 0, out, 0) == 0, "sha256(empty) ran");
    hex(out, 32, h);
    CHECK(strcmp(h, "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855") == 0, "SHA-256 of the empty string");
}

static void test_gcm(void) {
    unsigned char key[32], iv[12], tag[16], tag2[16];
    unsigned char plain[61], enc[61], dec[61];
    const unsigned char aad[] = "harbour-registry";
    mbedtls_gcm_context ctx;
    size_t i;

    for (i = 0; i < sizeof key; i++) key[i] = (unsigned char) (i * 7 + 1);
    for (i = 0; i < sizeof iv; i++) iv[i] = (unsigned char) (0xA0 + i);
    for (i = 0; i < sizeof plain; i++) plain[i] = (unsigned char) (i * 13 + 5);

    mbedtls_gcm_init(&ctx);
    CHECK(mbedtls_gcm_setkey(&ctx, MBEDTLS_CIPHER_ID_AES, key, 256) == 0, "gcm setkey");
    CHECK(mbedtls_gcm_crypt_and_tag(&ctx, MBEDTLS_GCM_ENCRYPT, sizeof plain, iv, sizeof iv,
                                    aad, sizeof aad - 1, plain, enc, sizeof tag, tag) == 0, "gcm encrypt");
    CHECK(memcmp(enc, plain, sizeof plain) != 0, "ciphertext differs from plaintext");
    CHECK(mbedtls_gcm_auth_decrypt(&ctx, sizeof enc, iv, sizeof iv, aad, sizeof aad - 1,
                                   tag, sizeof tag, enc, dec) == 0, "gcm decrypt with the right tag");
    CHECK(memcmp(dec, plain, sizeof plain) == 0, "AES-GCM round trip restores the plaintext");

    memcpy(tag2, tag, sizeof tag);
    tag2[3] ^= 1;
    CHECK(mbedtls_gcm_auth_decrypt(&ctx, sizeof enc, iv, sizeof iv, aad, sizeof aad - 1,
                                   tag2, sizeof tag2, enc, dec) == MBEDTLS_ERR_GCM_AUTH_FAILED,
          "a flipped tag bit is rejected");
    mbedtls_gcm_free(&ctx);

    /* NIST GCM test case 14 (AES-256, zero key, zero IV, 16 zero bytes). */
    {
        unsigned char zk[32] = {0}, ziv[12] = {0}, zp[16] = {0}, zc[16], zt[16];
        char h[33];
        mbedtls_gcm_init(&ctx);
        CHECK(mbedtls_gcm_setkey(&ctx, MBEDTLS_CIPHER_ID_AES, zk, 256) == 0, "gcm setkey (zero key)");
        CHECK(mbedtls_gcm_crypt_and_tag(&ctx, MBEDTLS_GCM_ENCRYPT, 16, ziv, 12, NULL, 0, zp, zc, 16, zt) == 0,
              "gcm encrypt (zero key)");
        hex(zc, 16, h);
        CHECK(strcmp(h, "cea7403d4d606b6e074ec5d3baf39d18") == 0, "GCM test case 14 ciphertext");
        hex(zt, 16, h);
        CHECK(strcmp(h, "d0d1c8a799996bf0265b98b5d48ab919") == 0, "GCM test case 14 tag");
        mbedtls_gcm_free(&ctx);
    }
}

static void test_x509(void) {
    mbedtls_x509_crt crt;
    char buf[256];
    uint32_t flags = 0;
    int ret;

    mbedtls_x509_crt_init(&crt);
    ret = mbedtls_x509_crt_parse(&crt, (const unsigned char *) test_cert, sizeof test_cert);
    CHECK(ret == 0, "parse the embedded certificate");
    if (ret == 0) {
        ret = mbedtls_x509_dn_gets(buf, sizeof buf, &crt.subject);
        CHECK(ret > 0 && strstr(buf, "CN=harbour-test.example") != NULL, "subject carries the common name");
        CHECK(ret > 0 && strstr(buf, "O=Harbour Registry") != NULL, "subject carries the organisation");
        CHECK(crt.version == 3, "certificate is X.509 v3");
        CHECK(mbedtls_x509_crt_get_ca_istrue(&crt) > 0, "basicConstraints CA:TRUE was read");
        CHECK(crt.valid_from.year == 2026 && crt.valid_to.year == 2126, "validity years");
        CHECK(mbedtls_pk_get_type(&crt.pk) == MBEDTLS_PK_ECKEY, "public key is an EC key");
        CHECK(mbedtls_pk_get_bitlen(&crt.pk) == 256, "public key is 256 bits");
        ret = mbedtls_x509_crt_verify(&crt, &crt, NULL, NULL, &flags, ignore_time, NULL);
        CHECK(ret == 0 && flags == 0, "self-signature verifies against itself");
    }
    mbedtls_x509_crt_free(&crt);

    mbedtls_x509_crt_init(&crt);
    CHECK(mbedtls_x509_crt_parse(&crt, (const unsigned char *) "-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n",
                                 sizeof "-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n") != 0,
          "garbage is not a certificate");
    mbedtls_x509_crt_free(&crt);
}

static void test_ssl(void) {
    mbedtls_ssl_config conf;
    mbedtls_ssl_context ssl;
    mbedtls_entropy_context entropy;
    mbedtls_ctr_drbg_context drbg;
    unsigned char r1[16], r2[16];
    int ret;

    mbedtls_ssl_config_init(&conf);
    mbedtls_ssl_init(&ssl);
    mbedtls_entropy_init(&entropy);
    mbedtls_ctr_drbg_init(&drbg);
    /* The TLS layer needs a random generator before it will set up a context. */
    CHECK(mbedtls_ctr_drbg_seed(&drbg, mbedtls_entropy_func, &entropy,
                                (const unsigned char *) "harbour", 7) == 0, "seed CTR_DRBG from the entropy source");
    CHECK(mbedtls_ctr_drbg_random(&drbg, r1, sizeof r1) == 0 && mbedtls_ctr_drbg_random(&drbg, r2, sizeof r2) == 0 &&
          memcmp(r1, r2, sizeof r1) != 0, "CTR_DRBG output differs between calls");
    CHECK(mbedtls_ssl_config_defaults(&conf, MBEDTLS_SSL_IS_CLIENT, MBEDTLS_SSL_TRANSPORT_STREAM,
                                      MBEDTLS_SSL_PRESET_DEFAULT) == 0, "client config defaults");
    mbedtls_ssl_conf_rng(&conf, mbedtls_ctr_drbg_random, &drbg);
    CHECK(mbedtls_ssl_ciphersuite_from_id(MBEDTLS_TLS_ECDHE_ECDSA_WITH_AES_128_GCM_SHA256) != NULL,
          "TLS_ECDHE_ECDSA_WITH_AES_128_GCM_SHA256 is a known ciphersuite");
    ret = mbedtls_ssl_setup(&ssl, &conf);
    if (ret != 0) printf("mbedtls_ssl_setup returned -0x%04x\n", (unsigned) -ret);
    CHECK(ret == 0, "ssl context setup");
    mbedtls_ssl_free(&ssl);
    mbedtls_ssl_config_free(&conf);
    mbedtls_ctr_drbg_free(&drbg);
    mbedtls_entropy_free(&entropy);
}

int main(void) {
    test_version();
    test_sha256();
    test_gcm();
    test_x509();
    test_ssl();
    if (failures) {
        printf("%d check(s) failed\n", failures);
        return 1;
    }
    printf("OK mbedtls %s: SHA-256 vectors, AES-GCM, X.509 parse+verify, TLS client setup\n", MBEDTLS_VERSION_STRING);
    return 0;
}
