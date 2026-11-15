/* Load a font from memory and rasterise a glyph, with no font file on disk.
 *
 * FreeType carries no fonts, but it reads BDF, a plain-text bitmap font
 * format, and a one-glyph BDF fits in a string. So this exercises the real
 * pipeline end to end: library init, the module registry (which fails if
 * ftinit.c was dropped), the BDF driver and its parser, the Unicode charmap,
 * glyph loading and bitmap rendering. The rendered pixels are compared
 * row by row with the bitmap written into the font.
 */
#include <ft2build.h>
#include FT_FREETYPE_H
#include FT_MODULE_H

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

/* An 8x8 "A", the bitmap rows written as hex bytes exactly as BDF stores them. */
static const unsigned char ROWS[8] = {0x18, 0x24, 0x42, 0x42, 0x7E, 0x42, 0x42, 0x42};

static const char BDF[] =
    "STARTFONT 2.1\n"
    "FONT -harbour-test-medium-r-normal--8-80-75-75-c-80-iso10646-1\n"
    "SIZE 8 75 75\n"
    "FONTBOUNDINGBOX 8 8 0 0\n"
    "STARTPROPERTIES 4\n"
    "FONT_ASCENT 8\n"
    "FONT_DESCENT 0\n"
    "CHARSET_REGISTRY \"ISO10646\"\n"
    "CHARSET_ENCODING \"1\"\n"
    "ENDPROPERTIES\n"
    "CHARS 1\n"
    "STARTCHAR A\n"
    "ENCODING 65\n"
    "SWIDTH 1000 0\n"
    "DWIDTH 8 0\n"
    "BBX 8 8 0 0\n"
    "BITMAP\n"
    "18\n24\n42\n42\n7E\n42\n42\n42\n"
    "ENDCHAR\n"
    "ENDFONT\n";

int main(void) {
    FT_Library lib;
    CHECK(FT_Init_FreeType(&lib) == 0);
    if (failures) return 1;

    FT_Int major, minor, patch;
    FT_Library_Version(lib, &major, &minor, &patch);
    char version[32];
    snprintf(version, sizeof version, "%d.%d.%d", (int)major, (int)minor, (int)patch);
    if (!version_is_declared("FT_Library_Version()", version)) return 1;

    /* The drivers ftinit.c registers: a build that lost it has none. */
    CHECK(FT_Get_Module(lib, "bdf") != NULL);
    CHECK(FT_Get_Module(lib, "truetype") != NULL);
    CHECK(FT_Get_Module(lib, "cff") != NULL);
    CHECK(FT_Get_Module(lib, "autofitter") != NULL);

    FT_Face face;
    CHECK(FT_New_Memory_Face(lib, (const FT_Byte *)BDF, (FT_Long)(sizeof BDF - 1), 0, &face) == 0);
    if (failures) return 1;

    CHECK(face->num_glyphs >= 1);
    CHECK(FT_Select_Charmap(face, FT_ENCODING_UNICODE) == 0);
    FT_UInt gi = FT_Get_Char_Index(face, 'A');
    CHECK(gi != 0);
    CHECK(FT_Get_Char_Index(face, 'B') == 0);   /* not in the font */
    CHECK(FT_Load_Glyph(face, gi, FT_LOAD_RENDER | FT_LOAD_TARGET_MONO) == 0);

    FT_GlyphSlot slot = face->glyph;
    CHECK(slot->bitmap.rows == 8 && slot->bitmap.width == 8);
    CHECK(slot->bitmap.pixel_mode == FT_PIXEL_MODE_MONO);
    CHECK(slot->advance.x >> 6 == 8);
    if (slot->bitmap.rows == 8 && slot->bitmap.buffer) {
        int pitch = slot->bitmap.pitch < 0 ? -slot->bitmap.pitch : slot->bitmap.pitch;
        for (int row = 0; row < 8; row++) {
            unsigned char got = slot->bitmap.buffer[row * pitch];
            if (got != ROWS[row]) {
                printf("FAIL: bitmap row %d is 0x%02X, the font says 0x%02X\n", row, got, ROWS[row]);
                failures++;
            }
        }
    }

    /* Garbage must be rejected, not accepted as a font. */
    FT_Face bad;
    CHECK(FT_New_Memory_Face(lib, (const FT_Byte *)"not a font at all", 17, 0, &bad) != 0);

    FT_Done_Face(face);
    FT_Done_FreeType(lib);
    if (failures) {
        printf("%d check(s) failed\n", failures);
        return 1;
    }
    printf("OK freetype %s: BDF font loaded from memory, 'A' rasterised and matched row by row, drivers registered\n",
           version);
    return 0;
}
