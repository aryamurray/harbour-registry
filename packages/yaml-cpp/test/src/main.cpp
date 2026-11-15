// Consumer test for yaml-cpp: parse -> modify -> emit -> compare, a multi-document stream, the emitter,
// and the error paths (YAML::ParserException, YAML::BadConversion, YAML::InvalidNode are C++
// exceptions). yaml-cpp 0.9.0 defines no version macro or function, so unlike the other packages
// this test cannot compare the library's own version with REGISTRY_TEST_UPSTREAM_VERSION; it
// still requires the variable (a run outside tools/test.py is refused) and reports it.
#include <yaml-cpp/yaml.h>

#include <cstdio>
#include <cstdlib>
#include <sstream>
#include <string>
#include <vector>

static int failures = 0;
#define CHECK(cond)                                                              \
    do {                                                                         \
        if (!(cond)) {                                                           \
            std::printf("FAIL line %d: %s\n", __LINE__, #cond);                  \
            failures++;                                                          \
        }                                                                        \
    } while (0)

int main() {
    const char *version = std::getenv("REGISTRY_TEST_UPSTREAM_VERSION");
    if (!version || !*version) {
        std::printf("FAIL: REGISTRY_TEST_UPSTREAM_VERSION is not set (run this through tools/test.py)\n");
        return 1;
    }

    // The consumer asked for C++20: yaml-cpp must have been compiled with the same standard.
    CHECK(__cplusplus >= 202002L);

    YAML::Node doc = YAML::Load(
        "name: city\n"
        "open: true\n"
        "score: 2.5\n"
        "books:\n"
        "  - {id: 1, title: Dune, year: 1965}\n"
        "  - {id: 2, title: \"Emma & Co\", year: 1815}\n");
    CHECK(doc.IsMap());
    CHECK(doc["name"].as<std::string>() == "city");
    CHECK(doc["open"].as<bool>());
    CHECK(doc["score"].as<double>() == 2.5);
    CHECK(doc["books"].size() == 2);
    CHECK(doc["books"][1]["title"].as<std::string>() == "Emma & Co");
    CHECK(!doc["missing"]);  // a missing key is a false node, not an error, until it is converted
    CHECK(doc["missing"].as<int>(7) == 7);

    // Modify.
    doc["name"] = "town";
    YAML::Node nb;
    nb["id"] = 3;
    nb["title"] = "Ubik";
    nb["year"] = 1969;
    doc["books"].push_back(nb);
    doc["open"] = false;
    int years = 0;
    for (const auto &b : doc["books"]) years += b["year"].as<int>();
    CHECK(years == 1965 + 1815 + 1969);

    // Emit a block-style document and compare byte for byte; the round trip parses back to the same data.
    YAML::Emitter out;
    out.SetIndent(2);
    out << YAML::BeginMap;
    out << YAML::Key << "name" << YAML::Value << doc["name"].as<std::string>();
    out << YAML::Key << "open" << YAML::Value << doc["open"].as<bool>();
    out << YAML::Key << "books" << YAML::Value << YAML::BeginSeq;
    for (const auto &b : doc["books"]) {
        out << YAML::BeginMap;
        out << YAML::Key << "id" << YAML::Value << b["id"].as<int>();
        out << YAML::Key << "title" << YAML::Value << b["title"].as<std::string>();
        out << YAML::EndMap;
    }
    out << YAML::EndSeq << YAML::EndMap;
    CHECK(out.good());
    const std::string expected =
        "name: town\n"
        "open: false\n"
        "books:\n"
        "  - id: 1\n"
        "    title: Dune\n"
        "  - id: 2\n"
        "    title: \"Emma & Co\"\n"
        "  - id: 3\n"
        "    title: Ubik";
    CHECK(std::string(out.c_str()) == expected);
    YAML::Node back = YAML::Load(out.c_str());
    CHECK(back["books"].size() == 3 && back["books"][2]["title"].as<std::string>() == "Ubik");

    // Node -> text -> Node with the stream operator, and a sequence of scalars.
    std::stringstream ss;
    ss << YAML::Load("[1, 2, 3]");
    CHECK(ss.str() == "[1, 2, 3]");
    std::vector<int> v = YAML::Load("[4, 5, 6]").as<std::vector<int>>();
    CHECK(v.size() == 3 && v[0] == 4 && v[2] == 6);

    // A stream of documents (the contrib-free part of the parser).
    std::vector<YAML::Node> docs = YAML::LoadAll("--- a\n--- b\n--- [c, d]\n");
    CHECK(docs.size() == 3 && docs[0].as<std::string>() == "a" && docs[2].size() == 2);

    // Error paths: all are C++ exceptions.
    bool parse_error = false;
    try {
        YAML::Load("key: [unterminated\n  other: value\n");
    } catch (const YAML::ParserException &e) {
        parse_error = e.mark.line >= 0 && std::string(e.what()).find("line") != std::string::npos;
    }
    CHECK(parse_error);

    bool bad_conversion = false;
    try {
        (void)YAML::Load("word")["x"].as<int>();
    } catch (const YAML::Exception &) {
        bad_conversion = true;
    }
    CHECK(bad_conversion);

    bool bad_cast = false;
    try {
        (void)YAML::Load("hello").as<int>();
    } catch (const YAML::BadConversion &e) {
        bad_cast = std::string(e.what()).find("bad conversion") != std::string::npos;
    }
    CHECK(bad_cast);

    if (failures) {
        std::printf("%d check(s) failed\n", failures);
        return 1;
    }
    std::printf("OK yaml-cpp %s: parse, modify, emit, round trip, multi-document stream, "
                "ParserException/BadConversion\n", version);
    return 0;
}
