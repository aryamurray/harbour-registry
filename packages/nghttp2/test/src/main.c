/* Exercise nghttp2 without a network.
 *
 *  - the library reports the version package.toml declares;
 *  - HPACK: the RFC 7541 C.4.1 request header block is produced byte for byte (static table
 *    indices plus a Huffman-coded literal), and inflating it gives the headers back; a larger
 *    block with repeated headers shrinks via the dynamic table and still round-trips;
 *  - a client session serialises the HTTP/2 connection preface followed by a SETTINGS frame, whose
 *    bytes are checked, and a server session fed those bytes reports the same two settings. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <nghttp2/nghttp2.h>

static int failures = 0;

#define CHECK(cond, what)                                    \
    do {                                                     \
        if (!(cond)) {                                       \
            printf("FAIL: %s (line %d)\n", what, __LINE__);  \
            failures++;                                      \
        }                                                    \
    } while (0)

#define NV(n, v) { (uint8_t *) (n), (uint8_t *) (v), sizeof(n) - 1, sizeof(v) - 1, NGHTTP2_NV_FLAG_NONE }

static void test_version(void) {
    const char *want = getenv("REGISTRY_TEST_UPSTREAM_VERSION");
    nghttp2_info *info = nghttp2_version(0);
    CHECK(info != NULL, "nghttp2_version");
    if (info == NULL) return;
    if (want == NULL || *want == '\0') {
        printf("FAIL: REGISTRY_TEST_UPSTREAM_VERSION is not set (run this through tools/test.py)\n");
        failures++;
    } else if (strcmp(want, info->version_str) != 0 || strcmp(want, NGHTTP2_VERSION) != 0) {
        printf("FAIL: library reports %s (header %s), but package.toml declares %s\n", info->version_str, NGHTTP2_VERSION, want);
        failures++;
    }
    CHECK(info->version_num == NGHTTP2_VERSION_NUM, "runtime and header version numbers agree");
    CHECK(strcmp(NGHTTP2_PROTO_VERSION_ID, "h2") == 0, "ALPN id");
}

/* Inflates `buf` and compares the result with `want`. */
static int inflate_equals(const uint8_t *buf, size_t len, const nghttp2_nv *want, size_t nwant) {
    nghttp2_hd_inflater *inf = NULL;
    size_t got = 0;
    int ok = 1;

    if (nghttp2_hd_inflate_new(&inf) != 0) return 0;
    for (;;) {
        nghttp2_nv nv;
        int flags = 0;
        nghttp2_ssize rv = nghttp2_hd_inflate_hd3(inf, &nv, &flags, buf, len, 1);
        if (rv < 0) { ok = 0; break; }
        buf += rv;
        len -= (size_t) rv;
        if (flags & NGHTTP2_HD_INFLATE_EMIT) {
            if (got >= nwant || nv.namelen != want[got].namelen || nv.valuelen != want[got].valuelen ||
                memcmp(nv.name, want[got].name, nv.namelen) != 0 || memcmp(nv.value, want[got].value, nv.valuelen) != 0) {
                ok = 0;
            }
            got++;
        }
        if (flags & NGHTTP2_HD_INFLATE_FINAL) {
            nghttp2_hd_inflate_end_headers(inf);
            break;
        }
        if (len == 0 && !(flags & NGHTTP2_HD_INFLATE_EMIT)) break;
    }
    nghttp2_hd_inflate_del(inf);
    return ok && got == nwant;
}

static void test_hpack(void) {
    /* RFC 7541, C.4.1: first request, with Huffman coding. */
    static const uint8_t rfc[] = {0x82, 0x86, 0x84, 0x41, 0x8c, 0xf1, 0xe3, 0xc2, 0xe5, 0xf2, 0x3a, 0x6b, 0xa0, 0xab, 0x90, 0xf4, 0xff};
    nghttp2_nv req[] = {NV(":method", "GET"), NV(":scheme", "http"), NV(":path", "/"), NV(":authority", "www.example.com")};
    nghttp2_hd_deflater *def = NULL;
    uint8_t buf[512];
    nghttp2_ssize n;

    CHECK(nghttp2_hd_deflate_new(&def, 4096) == 0 && def != NULL, "deflater");
    if (def == NULL) return;
    CHECK(nghttp2_hd_deflate_bound(def, req, 4) >= sizeof rfc, "deflate bound covers the block");
    n = nghttp2_hd_deflate_hd2(def, buf, sizeof buf, req, 4);
    CHECK(n == (nghttp2_ssize) sizeof rfc && memcmp(buf, rfc, sizeof rfc) == 0, "RFC 7541 C.4.1 bytes");
    CHECK(inflate_equals(rfc, sizeof rfc, req, 4), "inflate the RFC 7541 block");

    /* Second request of the same connection (C.4.2): :authority now comes from the dynamic table
     * and cache-control is added. */
    {
        static const uint8_t rfc2[] = {0x82, 0x86, 0x84, 0xbe, 0x58, 0x86, 0xa8, 0xeb, 0x10, 0x64, 0x9c, 0xbf};
        nghttp2_nv req2[] = {NV(":method", "GET"), NV(":scheme", "http"), NV(":path", "/"), NV(":authority", "www.example.com"),
                             NV("cache-control", "no-cache")};
        n = nghttp2_hd_deflate_hd2(def, buf, sizeof buf, req2, 5);
        CHECK(n == (nghttp2_ssize) sizeof rfc2 && memcmp(buf, rfc2, sizeof rfc2) == 0, "RFC 7541 C.4.2 bytes (dynamic table hit)");
    }
    nghttp2_hd_deflate_del(def);

    /* A bigger block: repeating a long header the second time costs one byte, and the decoder agrees. */
    {
        nghttp2_nv hdrs[] = {NV(":method", "POST"), NV(":scheme", "https"), NV(":path", "/api/v1/things?x=1"),
                             NV(":authority", "registry.example"), NV("user-agent", "harbour-registry-test/1.0"),
                             NV("x-binary", "\x01\x02\xff"), NV("user-agent", "harbour-registry-test/1.0")};
        nghttp2_ssize n1, n2;
        uint8_t b1[512], b2[512];
        nghttp2_nv single[] = {NV("user-agent", "harbour-registry-test/1.0")};

        CHECK(nghttp2_hd_deflate_new(&def, 4096) == 0, "second deflater");
        n1 = nghttp2_hd_deflate_hd2(def, b1, sizeof b1, hdrs, 7);
        n2 = nghttp2_hd_deflate_hd2(def, b2, sizeof b2, single, 1);
        CHECK(n1 > 0 && n2 == 1, "repeated header is a single indexed byte");
        CHECK(n1 > 0 && inflate_equals(b1, (size_t) n1, hdrs, 7), "large block round trip");
        nghttp2_hd_deflate_del(def);
    }
}

/* ---- sessions ------------------------------------------------------------------------------ */

typedef struct {
    int settings_frames;
    int saw_max_streams, saw_window;
    uint32_t max_streams, window;
} server_state;

static int on_frame_recv(nghttp2_session *session, const nghttp2_frame *frame, void *user_data) {
    server_state *st = user_data;
    size_t i;
    (void) session;
    if (frame->hd.type == NGHTTP2_SETTINGS && !(frame->hd.flags & NGHTTP2_FLAG_ACK)) {
        st->settings_frames++;
        for (i = 0; i < frame->settings.niv; i++) {
            if (frame->settings.iv[i].settings_id == NGHTTP2_SETTINGS_MAX_CONCURRENT_STREAMS) {
                st->saw_max_streams = 1;
                st->max_streams = frame->settings.iv[i].value;
            } else if (frame->settings.iv[i].settings_id == NGHTTP2_SETTINGS_INITIAL_WINDOW_SIZE) {
                st->saw_window = 1;
                st->window = frame->settings.iv[i].value;
            }
        }
    }
    return 0;
}

static void test_sessions(void) {
    static const char preface[] = "PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n";
    static const uint8_t settings[] = {
        0x00, 0x00, 0x0c, 0x04, 0x00, 0x00, 0x00, 0x00, 0x00,  /* SETTINGS, 12 bytes, stream 0 */
        0x00, 0x03, 0x00, 0x00, 0x00, 0x64,                    /* MAX_CONCURRENT_STREAMS = 100 */
        0x00, 0x04, 0x00, 0x00, 0xff, 0xff,                    /* INITIAL_WINDOW_SIZE = 65535 */
    };
    nghttp2_session_callbacks *cbs = NULL;
    nghttp2_session *client = NULL, *server = NULL;
    nghttp2_settings_entry iv[2];
    uint8_t out[256];
    size_t outlen = 0;
    server_state st;
    nghttp2_ssize rv;

    memset(&st, 0, sizeof st);
    CHECK(nghttp2_session_callbacks_new(&cbs) == 0, "callbacks");
    CHECK(nghttp2_session_client_new(&client, cbs, NULL) == 0 && client != NULL, "client session");
    if (client == NULL) { nghttp2_session_callbacks_del(cbs); return; }

    iv[0].settings_id = NGHTTP2_SETTINGS_MAX_CONCURRENT_STREAMS;
    iv[0].value = 100;
    iv[1].settings_id = NGHTTP2_SETTINGS_INITIAL_WINDOW_SIZE;
    iv[1].value = 65535;
    CHECK(nghttp2_submit_settings(client, NGHTTP2_FLAG_NONE, iv, 2) == 0, "submit SETTINGS");
    CHECK(nghttp2_session_want_write(client) != 0, "client wants to write");

    for (;;) {
        const uint8_t *data = NULL;
        rv = nghttp2_session_mem_send2(client, &data);
        if (rv <= 0) { CHECK(rv == 0, "mem_send2 did not fail"); break; }
        if (outlen + (size_t) rv > sizeof out) { CHECK(0, "output fits"); break; }
        memcpy(out + outlen, data, (size_t) rv);
        outlen += (size_t) rv;
    }
    CHECK(outlen == sizeof preface - 1 + sizeof settings, "preface + SETTINGS length");
    CHECK(outlen >= sizeof preface - 1 && memcmp(out, preface, sizeof preface - 1) == 0, "connection preface bytes");
    CHECK(outlen == sizeof preface - 1 + sizeof settings && memcmp(out + sizeof preface - 1, settings, sizeof settings) == 0,
          "SETTINGS frame bytes");
    CHECK(nghttp2_session_want_write(client) == 0, "nothing more to write");

    /* The same bytes, received by a server, are a valid preface and SETTINGS frame. */
    nghttp2_session_callbacks_set_on_frame_recv_callback(cbs, on_frame_recv);
    CHECK(nghttp2_session_server_new(&server, cbs, &st) == 0 && server != NULL, "server session");
    if (server != NULL) {
        rv = nghttp2_session_mem_recv2(server, out, outlen);
        CHECK(rv == (nghttp2_ssize) outlen, "server consumed every byte");
        CHECK(st.settings_frames == 1, "server saw one SETTINGS frame");
        CHECK(st.saw_max_streams && st.max_streams == 100, "MAX_CONCURRENT_STREAMS survived");
        CHECK(st.saw_window && st.window == 65535, "INITIAL_WINDOW_SIZE survived");
        /* ...and it owes the client a SETTINGS ACK. */
        CHECK(nghttp2_session_want_write(server) != 0, "server owes a SETTINGS ACK");
        nghttp2_session_del(server);
    }
    /* A wrong preface is a protocol error, not a crash. */
    {
        nghttp2_session *s2 = NULL;
        static const char junk[] = "GET / HTTP/1.1\r\nHost: x\r\n\r\n";
        CHECK(nghttp2_session_server_new(&s2, cbs, &st) == 0, "second server session");
        if (s2 != NULL) {
            CHECK(nghttp2_session_mem_recv2(s2, (const uint8_t *) junk, sizeof junk - 1) < 0, "HTTP/1.1 bytes are rejected");
            nghttp2_session_del(s2);
        }
    }
    nghttp2_session_del(client);
    nghttp2_session_callbacks_del(cbs);
}

int main(void) {
    test_version();
    test_hpack();
    test_sessions();
    if (failures) {
        printf("%d check(s) failed\n", failures);
        return 1;
    }
    printf("OK nghttp2 %s: HPACK vectors, client preface + SETTINGS bytes, server round trip\n", NGHTTP2_VERSION);
    return 0;
}
