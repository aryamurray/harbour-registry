// Consumer test for jsoncpp: parse -> modify -> serialise -> compare, and the error paths (a parse
// failure reports its message, and Json::LogicError is a thrown C++ exception).
#include <json/json.h>

#include <cstdio>
#include <cstdlib>
#include <memory>
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

static std::string compact(const Json::Value &v) {
    Json::StreamWriterBuilder wb;
    wb["indentation"] = "";
    return Json::writeString(wb, v);
}

int main() {
    const char *want = std::getenv("REGISTRY_TEST_UPSTREAM_VERSION");
    if (!want || !*want) {
        std::printf("FAIL: REGISTRY_TEST_UPSTREAM_VERSION is not set (run this through tools/test.py)\n");
        return 1;
    }
    if (std::string(JSONCPP_VERSION_STRING) != want) {
        std::printf("FAIL: jsoncpp reports version %s, but package.toml declares %s\n", JSONCPP_VERSION_STRING, want);
        return 1;
    }

    Json::Value root;
    std::string errs;
    Json::CharReaderBuilder rb;
    std::istringstream in(
        R"({"name":"city","books":[{"id":1,"title":"Dune","year":1965},)"
        R"({"id":2,"title":"Emma & Co","year":1815}],"open":true,"score":2.5,"none":null})");
    CHECK(Json::parseFromStream(rb, in, &root, &errs));
    CHECK(errs.empty());
    CHECK(root["name"].asString() == "city");
    CHECK(root["books"].size() == 2);
    CHECK(root["books"][1]["title"].asString() == "Emma & Co");
    CHECK(root["score"].asDouble() == 2.5);
    CHECK(root["none"].isNull());

    // Modify.
    root["name"] = "town";
    Json::Value nb;
    nb["id"] = 3;
    nb["title"] = "Ubik";
    nb["year"] = 1969;
    root["books"].append(nb);
    Json::Value removed;
    root["books"].removeIndex(0, &removed);
    CHECK(removed["title"].asString() == "Dune");
    root.removeMember("none");
    root["open"] = false;

    // Serialise (object members are sorted by key) and compare byte for byte.
    const std::string expected =
        R"({"books":[{"id":2,"title":"Emma & Co","year":1815},{"id":3,"title":"Ubik","year":1969}],)"
        R"("name":"town","open":false,"score":2.5})";
    CHECK(compact(root) == expected);

    // Round trip.
    Json::Value again;
    std::unique_ptr<Json::CharReader> reader(rb.newCharReader());
    CHECK(reader->parse(expected.data(), expected.data() + expected.size(), &again, &errs));
    CHECK(again == root);
    int years = 0;
    for (const Json::Value &b : again["books"]) years += b["year"].asInt();
    CHECK(years == 1815 + 1969);

    // Error path 1: malformed input is reported, not thrown, and says where.
    Json::Value bad;
    const std::string broken = "{\"a\": [1, 2,";
    CHECK(!reader->parse(broken.data(), broken.data() + broken.size(), &bad, &errs));
    CHECK(errs.find("Syntax error") != std::string::npos || errs.find("Missing") != std::string::npos);
    CHECK(errs.find("Line 1") != std::string::npos);

    // Error path 2: misuse throws Json::LogicError (exceptions are on: JSON_USE_EXCEPTION is 1).
    bool logic_error = false;
    try {
        (void)root["name"].asInt();
    } catch (const Json::LogicError &e) {
        logic_error = std::string(e.what()).find("not") != std::string::npos;
    }
    CHECK(logic_error);
    bool range_error = false;
    try {
        (void)Json::Value(Json::Int64(1) << 40).asInt();
    } catch (const Json::LogicError &) {
        range_error = true;
    }
    CHECK(range_error);

    if (failures) {
        std::printf("%d check(s) failed\n", failures);
        return 1;
    }
    std::printf("OK jsoncpp %s: parse, modify, serialise, round trip, parse error report, LogicError\n",
                JSONCPP_VERSION_STRING);
    return 0;
}
