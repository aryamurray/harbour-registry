/* PCRE2 8-bit: compile, match, capture groups (numbered and named), UTF-8 with Unicode
 * properties, substitution, and an invalid pattern rejected with the right error and offset.
 * Also checks the build configuration the manifest claims (Unicode on, JIT off) and that the
 * library reports the version package.toml declares. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <pcre2.h>

#define CHECK(cond, ...) do { if (!(cond)) { printf("FAIL: "); printf(__VA_ARGS__); printf("\n"); return 1; } } while (0)

static int version_is_declared(const char *got) {
    const char *want = getenv("REGISTRY_TEST_UPSTREAM_VERSION");
    size_t n;
    if (want == NULL || *want == '\0') {
        printf("FAIL: REGISTRY_TEST_UPSTREAM_VERSION is not set (run this through tools/test.py)\n");
        return 0;
    }
    n = strlen(want);
    /* "10.49 2026-09-28": the version, a space, the release date */
    if (strncmp(want, got, n) != 0 || (got[n] != '\0' && got[n] != ' ')) {
        printf("FAIL: pcre2 reports version \"%s\", but package.toml declares %s\n", got, want);
        return 0;
    }
    return 1;
}

int main(void) {
    char ver[64];
    uint32_t uni = 0, jit = 1;
    int err;
    PCRE2_SIZE eoff;
    pcre2_code *re;
    pcre2_match_data *md;
    PCRE2_SIZE *ov;
    int rc;
    PCRE2_UCHAR msg[256];

    CHECK(pcre2_config(PCRE2_CONFIG_VERSION, ver) > 0, "config version");
    if (!version_is_declared(ver)) return 1;
    pcre2_config(PCRE2_CONFIG_UNICODE, &uni);
    pcre2_config(PCRE2_CONFIG_JIT, &jit);
    CHECK(uni == 1, "Unicode support is off");
    CHECK(jit == 0, "JIT is on, the manifest builds without it");

    /* 1. numbered and named captures */
    re = pcre2_compile((PCRE2_SPTR)"(\\d+)-(?<word>[a-z]+)", PCRE2_ZERO_TERMINATED, 0, &err, &eoff, NULL);
    CHECK(re != NULL, "compile failed (%d at %d)", err, (int)eoff);
    md = pcre2_match_data_create_from_pattern(re, NULL);
    rc = pcre2_match(re, (PCRE2_SPTR)"id 123-abc x", PCRE2_ZERO_TERMINATED, 0, 0, md, NULL);
    CHECK(rc == 3, "match returned %d, want 3 (whole match + 2 groups)", rc);
    ov = pcre2_get_ovector_pointer(md);
    CHECK(ov[0] == 3 && ov[1] == 10, "whole match at [%d,%d), want [3,10)", (int)ov[0], (int)ov[1]);
    CHECK(ov[2] == 3 && ov[3] == 6, "group 1 at [%d,%d), want [3,6)", (int)ov[2], (int)ov[3]);
    CHECK(ov[4] == 7 && ov[5] == 10, "group 2 at [%d,%d), want [7,10)", (int)ov[4], (int)ov[5]);
    {
        PCRE2_UCHAR *w = NULL;
        PCRE2_SIZE wl = 0;
        CHECK(pcre2_substring_get_byname(md, (PCRE2_SPTR)"word", &w, &wl) == 0, "get_byname");
        CHECK(wl == 3 && memcmp(w, "abc", 3) == 0, "named group is not \"abc\"");
        pcre2_substring_free(w);
    }
    CHECK(pcre2_match(re, (PCRE2_SPTR)"no digits here", PCRE2_ZERO_TERMINATED, 0, 0, md, NULL)
          == PCRE2_ERROR_NOMATCH, "expected no match");
    pcre2_match_data_free(md);
    pcre2_code_free(re);

    /* 2. UTF-8 and Unicode properties: Greek letters are two bytes each */
    re = pcre2_compile((PCRE2_SPTR)"^\\p{Greek}+$", PCRE2_ZERO_TERMINATED, PCRE2_UTF, &err, &eoff, NULL);
    CHECK(re != NULL, "UTF compile failed (%d at %d)", err, (int)eoff);
    md = pcre2_match_data_create_from_pattern(re, NULL);
    CHECK(pcre2_match(re, (PCRE2_SPTR)"\xce\xb1\xce\xb2\xce\xb3", 6, 0, 0, md, NULL) >= 0, "Greek string did not match");
    CHECK(pcre2_match(re, (PCRE2_SPTR)"abc", 3, 0, 0, md, NULL) == PCRE2_ERROR_NOMATCH, "Latin matched \\p{Greek}");
    /* an invalid UTF-8 subject is refused, with the offset of the bad byte */
    rc = pcre2_match(re, (PCRE2_SPTR)"\xce\xb1\xff", 3, 0, 0, md, NULL);
    CHECK(rc == PCRE2_ERROR_UTF8_ERR1 || rc < 0, "invalid UTF-8 matched (%d)", rc);
    pcre2_match_data_free(md);
    pcre2_code_free(re);
    re = pcre2_compile((PCRE2_SPTR)"^.{3}$", PCRE2_ZERO_TERMINATED, PCRE2_UTF, &err, &eoff, NULL);
    CHECK(re != NULL, "compile ^.{3}$");
    md = pcre2_match_data_create_from_pattern(re, NULL);
    CHECK(pcre2_match(re, (PCRE2_SPTR)"\xce\xb1\xce\xb2\xce\xb3", 6, 0, 0, md, NULL) >= 0, ". counts characters, not bytes");
    pcre2_match_data_free(md);
    pcre2_code_free(re);

    /* 3. substitution */
    re = pcre2_compile((PCRE2_SPTR)"(\\w+)@(\\w+)", PCRE2_ZERO_TERMINATED, 0, &err, &eoff, NULL);
    CHECK(re != NULL, "compile substitution pattern");
    {
        PCRE2_UCHAR out[64];
        PCRE2_SIZE outlen = sizeof(out);
        rc = pcre2_substitute(re, (PCRE2_SPTR)"mail bob@host now", PCRE2_ZERO_TERMINATED, 0,
                              PCRE2_SUBSTITUTE_GLOBAL, NULL, NULL, (PCRE2_SPTR)"$2:$1", PCRE2_ZERO_TERMINATED,
                              out, &outlen);
        CHECK(rc == 1, "substitute returned %d", rc);
        CHECK(outlen == strlen("mail host:bob now") && memcmp(out, "mail host:bob now", outlen) == 0,
              "substitute result \"%.*s\"", (int)outlen, out);
    }
    pcre2_code_free(re);

    /* 4. an invalid pattern: unmatched "(" is reported at the end of the pattern */
    re = pcre2_compile((PCRE2_SPTR)"ab(cd", PCRE2_ZERO_TERMINATED, 0, &err, &eoff, NULL);
    CHECK(re == NULL, "invalid pattern compiled");
    CHECK(err == PCRE2_ERROR_MISSING_CLOSING_PARENTHESIS, "error %d, want missing closing parenthesis", err);
    CHECK(eoff == 5, "error offset %d, want 5", (int)eoff);
    CHECK(pcre2_get_error_message(err, msg, sizeof(msg)) > 0, "error message");
    printf("invalid pattern: %s at offset %d\n", (const char *)msg, (int)eoff);

    printf("OK pcre2 %s: captures, UTF/Unicode, substitute, error offsets\n", ver);
    return 0;
}
