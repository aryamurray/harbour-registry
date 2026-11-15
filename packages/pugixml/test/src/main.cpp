// Consumer test for pugixml: parse -> query (XPath) -> modify -> serialise -> compare, plus the
// error path (a malformed document reports status and offset instead of crashing) and the
// exception path (pugi::xpath_exception for a bad query).
#include <pugixml.hpp>

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <sstream>
#include <string>

static int failures = 0;
#define CHECK(cond)                                                              \
    do {                                                                         \
        if (!(cond)) {                                                           \
            std::printf("FAIL line %d: %s\n", __LINE__, #cond);                  \
            failures++;                                                          \
        }                                                                        \
    } while (0)

// PUGIXML_VERSION is major * 1000 + minor * 10 + patch (1160 is 1.16).
static std::string version_string() {
    int v = PUGIXML_VERSION;
    return std::to_string(v / 1000) + "." + std::to_string((v % 1000) / 10) +
           (v % 10 ? "." + std::to_string(v % 10) : "");
}

int main() {
    const char *want = std::getenv("REGISTRY_TEST_UPSTREAM_VERSION");
    if (!want || !*want) {
        std::printf("FAIL: REGISTRY_TEST_UPSTREAM_VERSION is not set (run this through tools/test.py)\n");
        return 1;
    }
    if (version_string() != want) {
        std::printf("FAIL: pugixml reports version %s, but package.toml declares %s\n",
                    version_string().c_str(), want);
        return 1;
    }

    pugi::xml_document doc;
    pugi::xml_parse_result res = doc.load_string(
        "<?xml version=\"1.0\"?>"
        "<library name=\"city\">"
        "<book id=\"1\"><title>Dune</title><year>1965</year></book>"
        "<book id=\"2\"><title>Emma &amp; Co</title><year>1815</year></book>"
        "</library>");
    CHECK(res);
    CHECK(res.status == pugi::status_ok);

    // Traversal and entity decoding.
    pugi::xml_node lib = doc.child("library");
    CHECK(std::strcmp(lib.attribute("name").value(), "city") == 0);
    int books = 0;
    for (pugi::xml_node b : lib.children("book")) books++;
    CHECK(books == 2);
    CHECK(std::string(lib.last_child().child_value("title")) == "Emma & Co");

    // XPath.
    pugi::xpath_node_set old = doc.select_nodes("/library/book[year < 1900]");
    CHECK(old.size() == 1);
    CHECK(old.first().node().attribute("id").as_int() == 2);
    pugi::xpath_query total("sum(//year)");
    CHECK(total.evaluate_number(doc) == 1965 + 1815);

    // Modify: add a book, change an attribute, remove one.
    pugi::xml_node nb = lib.append_child("book");
    nb.append_attribute("id") = 3;
    nb.append_child("title").text().set("Ubik");
    nb.append_child("year").text().set(1969);
    lib.attribute("name").set_value("town");
    lib.remove_child(lib.find_child_by_attribute("book", "id", "1"));

    // Serialise (compact, no declaration) and compare byte for byte.
    std::ostringstream out;
    doc.save(out, "", pugi::format_raw | pugi::format_no_declaration);
    const std::string expected =
        "<library name=\"town\">"
        "<book id=\"2\"><title>Emma &amp; Co</title><year>1815</year></book>"
        "<book id=\"3\"><title>Ubik</title><year>1969</year></book>"
        "</library>";
    CHECK(out.str() == expected);

    // Round trip: the serialised text parses back to the same serialisation.
    pugi::xml_document again;
    CHECK(again.load_string(out.str().c_str()));
    std::ostringstream out2;
    again.save(out2, "", pugi::format_raw | pugi::format_no_declaration);
    CHECK(out2.str() == expected);

    // Error path: a mismatched tag is reported with a status and an offset; the document is empty.
    pugi::xml_document bad;
    pugi::xml_parse_result err = bad.load_string("<a><b></a>");
    CHECK(!err);
    CHECK(err.status == pugi::status_end_element_mismatch);
    CHECK(err.offset > 0);
    CHECK(std::strlen(err.description()) > 0);

    // Exception path: an invalid XPath expression throws (exceptions are on by default and the
    // consumer is built with them, so this exercises the C++ runtime end to end).
    bool threw = false;
    try {
        pugi::xpath_query q("//book[");
        (void)q;
    } catch (const pugi::xpath_exception &e) {
        threw = std::strlen(e.what()) > 0;
    }
    CHECK(threw);

    // C++17 surface: string_view overloads exist only when pugixml itself was compiled as C++17
    // (the consumer's cpp_std = 17 raises the whole graph).
#ifdef PUGIXML_HAS_STRING_VIEW
    CHECK(lib.child(std::string_view("book")));
#else
    std::printf("FAIL: PUGIXML_HAS_STRING_VIEW is not defined: the C++ standard did not reach C++17\n");
    failures++;
#endif

    if (failures) {
        std::printf("%d check(s) failed\n", failures);
        return 1;
    }
    std::printf("OK pugixml %s: parse, XPath, modify, serialise, round trip, parse error, xpath_exception\n",
                version_string().c_str());
    return 0;
}
