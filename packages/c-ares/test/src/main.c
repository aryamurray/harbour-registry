/* Exercise c-ares without touching the network.
 *
 *  - the library reports the version package.toml declares (string and packed number);
 *  - a channel is created and destroyed, its server list set and read back;
 *  - ares_inet_pton / ares_inet_ntop round trip for IPv4 and IPv6;
 *  - a hand-built DNS reply is parsed by ares_dns_parse (name compression, TTL, address, truncation),
 *    and a query built by ares_dns_record_create round-trips through ares_dns_write. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "ares.h"
#include "ares_dns_record.h"

static int failures = 0;

#define CHECK(cond, what)                                    \
    do {                                                     \
        if (!(cond)) {                                       \
            printf("FAIL: %s (line %d)\n", what, __LINE__);  \
            failures++;                                      \
        }                                                    \
    } while (0)

/* A response for `example.com. IN A`: id 0x1234, QR|RD|RA, one question, one answer whose name is a
 * compression pointer to the question (offset 12), TTL 300, 93.184.216.34. */
static const unsigned char reply[] = {
    0x12, 0x34, 0x81, 0x80, 0x00, 0x01, 0x00, 0x01, 0x00, 0x00, 0x00, 0x00,
    7, 'e', 'x', 'a', 'm', 'p', 'l', 'e', 3, 'c', 'o', 'm', 0, 0x00, 0x01, 0x00, 0x01,
    0xc0, 0x0c, 0x00, 0x01, 0x00, 0x01, 0x00, 0x00, 0x01, 0x2c, 0x00, 0x04, 93, 184, 216, 34,
};

static void test_version(void) {
    const char *want = getenv("REGISTRY_TEST_UPSTREAM_VERSION");
    int packed = 0;
    const char *got = ares_version(&packed);
    if (want == NULL || *want == '\0') {
        printf("FAIL: REGISTRY_TEST_UPSTREAM_VERSION is not set (run this through tools/test.py)\n");
        failures++;
    } else if (strcmp(want, got) != 0 || strcmp(want, ARES_VERSION_STR) != 0) {
        printf("FAIL: library reports %s (header %s), but package.toml declares %s\n", got, ARES_VERSION_STR, want);
        failures++;
    }
    CHECK(packed == ARES_VERSION, "packed version number matches the header");
    CHECK(strcmp(ares_strerror(ARES_ENOTFOUND), "Domain name not found") == 0, "ares_strerror(ARES_ENOTFOUND)");
}

static void test_channel(void) {
    ares_channel_t *channel = NULL;
    struct ares_options opts;
    char *csv;
    int ret;

    CHECK(ares_library_init(ARES_LIB_INIT_ALL) == ARES_SUCCESS, "ares_library_init");
    memset(&opts, 0, sizeof opts);
    opts.flags = ARES_FLAG_NORECURSE;
    opts.tries = 2;
    ret = ares_init_options(&channel, &opts, ARES_OPT_FLAGS | ARES_OPT_TRIES);
    CHECK(ret == ARES_SUCCESS && channel != NULL, "ares_init_options creates a channel");
    if (channel != NULL) {
        /* Servers are only stored: nothing is sent. 192.0.2.0/24 and 2001:db8::/32 are documentation ranges. */
        CHECK(ares_set_servers_csv(channel, "192.0.2.53,[2001:db8::35]:5353") == ARES_SUCCESS, "set servers");
        csv = ares_get_servers_csv(channel);
        CHECK(csv != NULL, "get servers");
        if (csv != NULL) {
            CHECK(strstr(csv, "192.0.2.53") != NULL, "first server read back");
            CHECK(strstr(csv, "2001:db8::35") != NULL && strstr(csv, "5353") != NULL, "second server and its port read back");
            ares_free_string(csv);
        }
        ares_destroy(channel);
    }
    ares_library_cleanup();
}

static void test_inet(void) {
    unsigned char a4[4], a6[16];
    char buf[64];
    static const unsigned char want6[16] = {0x20, 0x01, 0x0d, 0xb8, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1};

    CHECK(ares_inet_pton(AF_INET, "192.0.2.33", a4) == 1, "pton v4");
    CHECK(a4[0] == 192 && a4[1] == 0 && a4[2] == 2 && a4[3] == 33, "v4 bytes");
    CHECK(ares_inet_ntop(AF_INET, a4, buf, sizeof buf) != NULL && strcmp(buf, "192.0.2.33") == 0, "ntop v4");

    CHECK(ares_inet_pton(AF_INET6, "2001:db8::1", a6) == 1, "pton v6");
    CHECK(memcmp(a6, want6, 16) == 0, "v6 bytes");
    CHECK(ares_inet_ntop(AF_INET6, a6, buf, sizeof buf) != NULL && strcmp(buf, "2001:db8::1") == 0, "ntop v6 compresses the zero run");

    CHECK(ares_inet_pton(AF_INET, "300.1.1.1", a4) != 1, "pton rejects 300.1.1.1");
    CHECK(ares_inet_pton(AF_INET6, "2001:db8::g", a6) != 1, "pton rejects a bad v6 literal");
}

static void test_parse(void) {
    ares_dns_record_t *rec = NULL;
    const ares_dns_rr_t *rr;
    const struct in_addr *addr;
    char ip[32];

    CHECK(ares_dns_parse(reply, sizeof reply, 0, &rec) == ARES_SUCCESS && rec != NULL, "ares_dns_parse the reply");
    if (rec != NULL) {
        CHECK(ares_dns_record_get_id(rec) == 0x1234, "reply id");
        CHECK(ares_dns_record_get_rcode(rec) == ARES_RCODE_NOERROR, "rcode NOERROR");
        CHECK((ares_dns_record_get_flags(rec) & ARES_FLAG_QR) != 0, "QR flag set");
        CHECK(ares_dns_record_query_cnt(rec) == 1, "one question");
        CHECK(ares_dns_record_rr_cnt(rec, ARES_SECTION_ANSWER) == 1, "one answer");
        rr = ares_dns_record_rr_get_const(rec, ARES_SECTION_ANSWER, 0);
        CHECK(rr != NULL && ares_dns_rr_get_type(rr) == ARES_REC_TYPE_A, "answer is an A record");
        if (rr != NULL) {
            CHECK(strcmp(ares_dns_rr_get_name(rr), "example.com") == 0, "answer name, through the compression pointer");
            CHECK(ares_dns_rr_get_ttl(rr) == 300, "ttl");
            addr = ares_dns_rr_get_addr(rr, ARES_RR_A_ADDR);
            CHECK(addr != NULL && ares_inet_ntop(AF_INET, addr, ip, sizeof ip) != NULL && strcmp(ip, "93.184.216.34") == 0,
                  "A record address");
        }
        ares_dns_record_destroy(rec);
    }

    /* A truncated reply must be refused, not read past its end. */
    rec = NULL;
    CHECK(ares_dns_parse(reply, sizeof reply - 3, 0, &rec) != ARES_SUCCESS, "truncated reply is rejected");
    if (rec != NULL) ares_dns_record_destroy(rec);
}

static void test_query_roundtrip(void) {
    ares_dns_record_t *q = NULL, *back = NULL;
    unsigned char *wire = NULL;
    size_t wlen = 0;
    const char *name = NULL;
    ares_dns_rec_type_t type;
    ares_dns_class_t cls;

    CHECK(ares_dns_record_create(&q, 0xBEEF, ARES_FLAG_RD, ARES_OPCODE_QUERY, ARES_RCODE_NOERROR) == ARES_SUCCESS, "create a query record");
    CHECK(ares_dns_record_query_add(q, "harbour.example", ARES_REC_TYPE_AAAA, ARES_CLASS_IN) == ARES_SUCCESS, "add a question");
    CHECK(ares_dns_write(q, &wire, &wlen) == ARES_SUCCESS && wire != NULL, "serialise the query");
    /* header (12) + 1+7 + 1+7 + 1 (root) + type + class */
    CHECK(wlen == 12 + 8 + 8 + 1 + 4, "wire length");
    CHECK(wire != NULL && wire[0] == 0xBE && wire[1] == 0xEF, "wire id");
    CHECK(ares_dns_parse(wire, wlen, 0, &back) == ARES_SUCCESS && back != NULL, "parse it back");
    if (back != NULL) {
        CHECK(ares_dns_record_query_get(back, 0, &name, &type, &cls) == ARES_SUCCESS, "read the question");
        CHECK(name != NULL && strcmp(name, "harbour.example") == 0 && type == ARES_REC_TYPE_AAAA && cls == ARES_CLASS_IN,
              "question survives the round trip");
        ares_dns_record_destroy(back);
    }
    ares_free_string(wire);
    ares_dns_record_destroy(q);
}

int main(void) {
    test_version();
    test_channel();
    test_inet();
    test_parse();
    test_query_roundtrip();
    if (failures) {
        printf("%d check(s) failed\n", failures);
        return 1;
    }
    printf("OK c-ares %s: version, channel, inet_pton/ntop, DNS reply parse, query round trip\n", ARES_VERSION_STR);
    return 0;
}
