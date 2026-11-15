// Consumer test for tinyxml2: parse -> query -> modify -> serialise -> compare, plus the error
// path (a malformed document returns an XMLError with line information).
#include <tinyxml2.h>

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>

using namespace tinyxml2;

static int failures = 0;
#define CHECK(cond)                                                              \
    do {                                                                         \
        if (!(cond)) {                                                           \
            std::printf("FAIL line %d: %s\n", __LINE__, #cond);                  \
            failures++;                                                          \
        }                                                                        \
    } while (0)

int main() {
    std::string version = std::to_string(TIXML2_MAJOR_VERSION) + "." + std::to_string(TIXML2_MINOR_VERSION) +
                          "." + std::to_string(TIXML2_PATCH_VERSION);
    const char *want = std::getenv("REGISTRY_TEST_UPSTREAM_VERSION");
    if (!want || !*want) {
        std::printf("FAIL: REGISTRY_TEST_UPSTREAM_VERSION is not set (run this through tools/test.py)\n");
        return 1;
    }
    if (version != want) {
        std::printf("FAIL: tinyxml2 reports version %s, but package.toml declares %s\n", version.c_str(), want);
        return 1;
    }

    XMLDocument doc;
    XMLError rc = doc.Parse(
        "<?xml version=\"1.0\"?>"
        "<library name=\"city\">"
        "<book id=\"1\"><title>Dune</title><year>1965</year></book>"
        "<book id=\"2\"><title>Emma &amp; Co</title><year>1815</year></book>"
        "</library>");
    CHECK(rc == XML_SUCCESS);

    XMLElement *lib = doc.FirstChildElement("library");
    CHECK(lib != nullptr);
    CHECK(std::strcmp(lib->Attribute("name"), "city") == 0);
    int books = 0, total = 0;
    for (XMLElement *b = lib->FirstChildElement("book"); b; b = b->NextSiblingElement("book")) {
        books++;
        total += b->FirstChildElement("year")->IntText();
    }
    CHECK(books == 2);
    CHECK(total == 1965 + 1815);
    CHECK(std::string(lib->LastChildElement("book")->FirstChildElement("title")->GetText()) == "Emma & Co");

    // Modify: add a book, change an attribute, remove one.
    XMLElement *nb = doc.NewElement("book");
    nb->SetAttribute("id", 3);
    XMLElement *t = doc.NewElement("title");
    t->SetText("Ubik");
    nb->InsertEndChild(t);
    XMLElement *y = doc.NewElement("year");
    y->SetText(1969);
    nb->InsertEndChild(y);
    lib->InsertEndChild(nb);
    lib->SetAttribute("name", "town");
    lib->DeleteChild(lib->FirstChildElement("book"));

    // Serialise compactly and compare byte for byte.
    XMLPrinter printer(nullptr, true);
    doc.Print(&printer);
    const std::string expected =
        "<?xml version=\"1.0\"?>"
        "<library name=\"town\">"
        "<book id=\"2\"><title>Emma &amp; Co</title><year>1815</year></book>"
        "<book id=\"3\"><title>Ubik</title><year>1969</year></book>"
        "</library>";
    CHECK(std::string(printer.CStr()) == expected);

    // Round trip.
    XMLDocument again;
    CHECK(again.Parse(printer.CStr()) == XML_SUCCESS);
    XMLPrinter printer2(nullptr, true);
    again.Print(&printer2);
    CHECK(std::string(printer2.CStr()) == expected);

    // Error path: a mismatched tag yields an error code, a name for it and the line number.
    XMLDocument bad;
    XMLError err = bad.Parse("<a>\n<b>\n</a>");
    CHECK(err == XML_ERROR_MISMATCHED_ELEMENT);
    CHECK(bad.Error());
    CHECK(bad.ErrorLineNum() == 2);
    CHECK(std::strlen(bad.ErrorName()) > 0);

    // Error path: a missing file.
    XMLDocument missing;
    CHECK(missing.LoadFile("/nonexistent/registry-test.xml") == XML_ERROR_FILE_NOT_FOUND);

    if (failures) {
        std::printf("%d check(s) failed\n", failures);
        return 1;
    }
    std::printf("OK tinyxml2 %s: parse, query, modify, serialise, round trip, mismatched-tag and missing-file errors\n",
                version.c_str());
    return 0;
}
