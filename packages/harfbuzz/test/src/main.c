/* Drive HarfBuzz's Unicode machinery without any font file.
 *
 * Shaping needs a font, and a font is a binary asset this registry does not
 * carry, so this stays on the part of HarfBuzz that works from the text alone:
 * decoding UTF-8 into a buffer, and guessing the script and direction from the
 * code points. That is still real behaviour, and it goes through the
 * Unicode-database tables compiled into the library's single translation unit:
 * a build that dropped them, or one that miscompiled the UTF-8 decoder, gets
 * these answers wrong.
 *
 * It also queries the Unicode tables (general category, combining class,
 * mirroring, composition) directly. (hb_shape against the empty face does not
 * shape: it reports failure, so shaping is out of scope without a font file.)
 */
#include <hb.h>

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

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

static int failures = 0;
#define CHECK(cond)                                                            \
    do {                                                                       \
        if (!(cond)) {                                                         \
            printf("FAIL line %d: %s\n", __LINE__, #cond);                     \
            failures++;                                                        \
        }                                                                      \
    } while (0)

/* Segment `text` and report what HarfBuzz decided. */
static hb_buffer_t *segment(const char *text, hb_script_t *script, hb_direction_t *dir) {
    hb_buffer_t *buf = hb_buffer_create();
    hb_buffer_add_utf8(buf, text, -1, 0, -1);
    hb_buffer_guess_segment_properties(buf);
    *script = hb_buffer_get_script(buf);
    *dir = hb_buffer_get_direction(buf);
    return buf;
}

int main(void) {
    if (!version_is_declared("hb_version_string()", hb_version_string())) return 1;

    hb_script_t script;
    hb_direction_t dir;
    hb_buffer_t *buf;

    /* Latin: left to right. 5 ASCII code points. */
    buf = segment("Hello", &script, &dir);
    CHECK(hb_buffer_get_length(buf) == 5);
    CHECK(script == HB_SCRIPT_LATIN);
    CHECK(dir == HB_DIRECTION_LTR);
    hb_buffer_destroy(buf);

    /* Arabic: right to left. 5 code points encoded in 10 UTF-8 bytes, so a
     * decoder that counted bytes would report 10. */
    buf = segment("\xD9\x85\xD8\xB1\xD8\xAD\xD8\xA8\xD8\xA7", &script, &dir);
    CHECK(hb_buffer_get_length(buf) == 5);
    CHECK(script == HB_SCRIPT_ARABIC);
    CHECK(dir == HB_DIRECTION_RTL);
    hb_buffer_destroy(buf);

    /* Hebrew: right to left, and a different script from Arabic. */
    buf = segment("\xD7\xA9\xD7\x9C\xD7\x95\xD7\x9D", &script, &dir);
    CHECK(hb_buffer_get_length(buf) == 4);
    CHECK(script == HB_SCRIPT_HEBREW);
    CHECK(dir == HB_DIRECTION_RTL);
    hb_buffer_destroy(buf);

    /* Devanagari and Han: left to right, scripts told apart. */
    buf = segment("\xE0\xA4\xA8\xE0\xA4\xAE\xE0\xA4\xB8\xE0\xA5\x8D\xE0\xA4\xA4\xE0\xA5\x87", &script, &dir);
    CHECK(script == HB_SCRIPT_DEVANAGARI);
    CHECK(dir == HB_DIRECTION_LTR);
    hb_buffer_destroy(buf);
    buf = segment("\xE6\xBC\xA2\xE5\xAD\x97", &script, &dir);
    CHECK(hb_buffer_get_length(buf) == 2);
    CHECK(script == HB_SCRIPT_HAN);
    hb_buffer_destroy(buf);

    /* Invalid UTF-8 must be replaced, not crash or truncate: 0xFF is never valid. */
    buf = hb_buffer_create();
    hb_buffer_add_utf8(buf, "a\xFF" "b", -1, 0, -1);
    CHECK(hb_buffer_get_length(buf) == 3);
    hb_buffer_destroy(buf);

    /* Clusters of a freshly added buffer are the UTF-8 byte offsets of each
     * code point: four 2-byte Hebrew letters start at 0, 2, 4, 6. */
    buf = segment("\xD7\xA9\xD7\x9C\xD7\x95\xD7\x9D", &script, &dir);
    unsigned count = 0;
    hb_glyph_info_t *info = hb_buffer_get_glyph_infos(buf, &count);
    CHECK(count == 4);
    if (count == 4) {
        CHECK(info[0].codepoint == 0x05E9 && info[3].codepoint == 0x05DD);
        CHECK(info[0].cluster == 0 && info[1].cluster == 2 && info[3].cluster == 6);
    }
    /* Reversing (what RTL shaping does to reach visual order) flips them. */
    hb_buffer_reverse(buf);
    info = hb_buffer_get_glyph_infos(buf, &count);
    CHECK(info[0].codepoint == 0x05DD && info[3].codepoint == 0x05E9);
    hb_buffer_destroy(buf);

    /* The Unicode database compiled into the library (hb-ucd): these are table
     * lookups, so a missing or corrupted table gives wrong answers. */
    hb_unicode_funcs_t *uf = hb_unicode_funcs_get_default();
    CHECK(hb_unicode_general_category(uf, 'A') == HB_UNICODE_GENERAL_CATEGORY_UPPERCASE_LETTER);
    CHECK(hb_unicode_general_category(uf, '7') == HB_UNICODE_GENERAL_CATEGORY_DECIMAL_NUMBER);
    CHECK(hb_unicode_combining_class(uf, 0x0301) == 230);   /* COMBINING ACUTE ACCENT */
    CHECK(hb_unicode_mirroring(uf, '(') == ')');
    CHECK(hb_unicode_script(uf, 0x05D0) == HB_SCRIPT_HEBREW);
    hb_codepoint_t composed = 0;
    CHECK(hb_unicode_compose(uf, 'e', 0x0301, &composed) && composed == 0x00E9);
    hb_codepoint_t a1 = 0, b1 = 0;
    CHECK(hb_unicode_decompose(uf, 0x00E9, &a1, &b1) && a1 == 'e' && b1 == 0x0301);

    /* Script tags round-trip through their ISO 15924 spelling. */
    CHECK(hb_script_to_iso15924_tag(HB_SCRIPT_ARABIC) == HB_TAG('A', 'r', 'a', 'b'));
    CHECK(hb_script_from_string("Latn", -1) == HB_SCRIPT_LATIN);

    if (failures) {
        printf("%d check(s) failed\n", failures);
        return 1;
    }
    printf("OK harfbuzz %s: UTF-8 decoding, script and direction detection (Latin, Arabic, Hebrew, "
           "Devanagari, Han), Unicode tables\n", hb_version_string());
    return 0;
}
