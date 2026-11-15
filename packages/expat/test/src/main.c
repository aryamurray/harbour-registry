/* Exercise expat at runtime: parse a small document (elements, attributes, character data, a
 * DTD-declared internal entity, a namespace-aware pass), reject a malformed one at the right
 * line, and check the compiled-in feature list against the configuration the manifest declares
 * (XML_DTD, XML_GE, XML_NS, XML_CONTEXT_BYTES=1024). */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <expat.h>

static int version_is_declared(const char *got) {
    const char *want = getenv("REGISTRY_TEST_UPSTREAM_VERSION");
    if (want == NULL || *want == '\0') {
        printf("FAIL: REGISTRY_TEST_UPSTREAM_VERSION is not set (run this through tools/test.py)\n");
        return 0;
    }
    char expected[64];
    snprintf(expected, sizeof(expected), "expat_%s", want);
    if (strcmp(expected, got) != 0) {
        printf("FAIL: XML_ExpatVersion() reports %s, but package.toml declares %s\n", got, want);
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

typedef struct {
    int elements;
    int depth;
    int max_depth;
    char title[64];
    char id[16];
    char text[128];
    int in_title;
} state_t;

static void XMLCALL on_start(void *ud, const XML_Char *name, const XML_Char **atts) {
    state_t *s = ud;
    s->elements++;
    if (++s->depth > s->max_depth) s->max_depth = s->depth;
    s->in_title = strcmp(name, "title") == 0;
    for (int i = 0; atts[i] != NULL; i += 2) {
        if (strcmp(atts[i], "id") == 0) {
            strncpy(s->id, atts[i + 1], sizeof(s->id) - 1);
        }
    }
}

static void XMLCALL on_end(void *ud, const XML_Char *name) {
    state_t *s = ud;
    (void) name;
    s->depth--;
    s->in_title = 0;
}

static void XMLCALL on_text(void *ud, const XML_Char *data, int len) {
    state_t *s = ud;
    size_t used = strlen(s->text);
    if (used + (size_t) len < sizeof(s->text)) memcpy(s->text + used, data, (size_t) len);
    if (s->in_title) {
        size_t t = strlen(s->title);
        if (t + (size_t) len < sizeof(s->title)) memcpy(s->title + t, data, (size_t) len);
    }
}

static const char doc[] =
    "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
    "<!DOCTYPE book [<!ENTITY pub \"Harbour Press\">]>\n"
    "<book id=\"b42\">\n"
    "  <title>Registry &amp; Co</title>\n"
    "  <publisher>&pub;</publisher>\n"
    "  <!-- a comment -->\n"
    "</book>\n";

static const char nsdoc[] =
    "<a:root xmlns:a=\"urn:example:a\"><a:leaf/></a:root>";

static void XMLCALL on_ns_start(void *ud, const XML_Char *name, const XML_Char **atts) {
    int *n = ud;
    (void) atts;
    /* With namespace processing, names arrive as "<uri><sep><local>". */
    if (strcmp(name, "urn:example:a|leaf") == 0) (*n)++;
}

int main(void) {
    if (!version_is_declared(XML_ExpatVersion())) return 1;
    XML_Expat_Version v = XML_ExpatVersionInfo();
    CHECK(v.major == XML_MAJOR_VERSION && v.minor == XML_MINOR_VERSION && v.micro == XML_MICRO_VERSION);

    /* The configuration the generated header was supposed to carry. */
    int has_dtd = 0, has_ns = 0, has_ge = 0;
    long context_bytes = -1;
    for (const XML_Feature *f = XML_GetFeatureList(); f->feature != XML_FEATURE_END; f++) {
        if (f->feature == XML_FEATURE_DTD) has_dtd = 1;
        if (f->feature == XML_FEATURE_NS) has_ns = 1;
        if (f->feature == XML_FEATURE_GE) has_ge = 1;
        if (f->feature == XML_FEATURE_CONTEXT_BYTES) context_bytes = f->value;
    }
    CHECK(has_dtd && has_ns && has_ge && context_bytes == 1024);

    /* A well-formed document with an internal entity. */
    {
        state_t s;
        memset(&s, 0, sizeof(s));
        XML_Parser p = XML_ParserCreate("UTF-8");
        CHECK(p != NULL);
        if (p != NULL) {
            XML_SetUserData(p, &s);
            XML_SetElementHandler(p, on_start, on_end);
            XML_SetCharacterDataHandler(p, on_text);
            /* Feed it in two pieces, to exercise the incremental interface. */
            size_t half = sizeof(doc) / 2;
            CHECK(XML_Parse(p, doc, (int) half, 0) == XML_STATUS_OK);
            CHECK(XML_Parse(p, doc + half, (int) (sizeof(doc) - 1 - half), 1) == XML_STATUS_OK);
            CHECK(s.elements == 3 && s.depth == 0 && s.max_depth == 2);
            CHECK(strcmp(s.id, "b42") == 0);
            CHECK(strcmp(s.title, "Registry & Co") == 0);
            CHECK(strstr(s.text, "Harbour Press") != NULL); /* &pub; was expanded */
            XML_ParserFree(p);
        }
    }

    /* Namespace-aware parsing. */
    {
        int leaves = 0;
        XML_Parser p = XML_ParserCreateNS("UTF-8", '|');
        CHECK(p != NULL);
        if (p != NULL) {
            XML_SetUserData(p, &leaves);
            XML_SetStartElementHandler(p, on_ns_start);
            CHECK(XML_Parse(p, nsdoc, (int) strlen(nsdoc), 1) == XML_STATUS_OK);
            CHECK(leaves == 1);
            XML_ParserFree(p);
        }
    }

    /* A malformed document is rejected, and reported at the right place. */
    {
        static const char bad[] = "<a>\n<b>\n</a>\n";
        XML_Parser p = XML_ParserCreate(NULL);
        CHECK(p != NULL);
        if (p != NULL) {
            CHECK(XML_Parse(p, bad, (int) strlen(bad), 1) == XML_STATUS_ERROR);
            CHECK(XML_GetErrorCode(p) == XML_ERROR_TAG_MISMATCH);
            CHECK(XML_GetCurrentLineNumber(p) == 3);
            CHECK(strlen(XML_ErrorString(XML_GetErrorCode(p))) > 0);
            XML_ParserFree(p);
        }
    }

    if (failures != 0) {
        printf("%d check(s) failed\n", failures);
        return 1;
    }
    printf("OK expat %s: incremental parse, internal entity, namespaces, error position, feature list\n",
           XML_ExpatVersion());
    return 0;
}
