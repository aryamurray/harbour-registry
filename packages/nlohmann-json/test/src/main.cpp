// Consumer test for nlohmann/json: parse -> modify -> serialise -> compare, binary round trip, and
// the error paths (parse_error, type_error, out_of_range are thrown C++ exceptions).
#include <nlohmann/json.hpp>

#include <cstdio>
#include <cstdlib>
#include <string>
#include <vector>

using nlohmann::json;

static int failures = 0;
#define CHECK(cond)                                                              \
    do {                                                                         \
        if (!(cond)) {                                                           \
            std::printf("FAIL line %d: %s\n", __LINE__, #cond);                  \
            failures++;                                                          \
        }                                                                        \
    } while (0)

int main() {
    const std::string version = std::to_string(NLOHMANN_JSON_VERSION_MAJOR) + "." +
                                std::to_string(NLOHMANN_JSON_VERSION_MINOR) + "." +
                                std::to_string(NLOHMANN_JSON_VERSION_PATCH);
    const char *want = std::getenv("REGISTRY_TEST_UPSTREAM_VERSION");
    if (!want || !*want) {
        std::printf("FAIL: REGISTRY_TEST_UPSTREAM_VERSION is not set (run this through tools/test.py)\n");
        return 1;
    }
    if (version != want) {
        std::printf("FAIL: nlohmann/json reports version %s, but package.toml declares %s\n", version.c_str(), want);
        return 1;
    }
    CHECK(json::meta()["version"]["string"] == version);

    // Parse, read, modify.
    json doc = json::parse(R"({"name":"city","books":[{"id":1,"title":"Dune","year":1965},
                               {"id":2,"title":"Emma & Co","year":1815}],"open":true,"score":2.5,"none":null})");
    CHECK(doc["name"] == "city");
    CHECK(doc["books"].size() == 2);
    CHECK(doc["books"][1]["title"].get<std::string>() == "Emma & Co");
    CHECK(doc["score"].get<double>() == 2.5);
    CHECK(doc["none"].is_null());

    doc["name"] = "town";
    doc["books"].push_back({{"id", 3}, {"title", "Ubik"}, {"year", 1969}});
    doc["books"].erase(doc["books"].begin());
    doc.erase("none");
    doc["open"] = false;

    // Serialise (object keys are ordered) and compare byte for byte.
    const std::string expected =
        R"({"books":[{"id":2,"title":"Emma & Co","year":1815},{"id":3,"title":"Ubik","year":1969}],)"
        R"("name":"town","open":false,"score":2.5})";
    CHECK(doc.dump() == expected);
    CHECK(json::parse(doc.dump()) == doc);
    CHECK(doc.dump(2).find("\n  \"books\": [") != std::string::npos);

    // Typed conversion and range-for over the array.
    int years = 0;
    for (const auto &b : doc["books"]) years += b.at("year").get<int>();
    CHECK(years == 1815 + 1969);

    // JSON Pointer and patch.
    CHECK(doc["/books/1/title"_json_pointer] == "Ubik");
    json patch = json::parse(R"([{"op":"replace","path":"/books/1/title","value":"Ubik!"}])");
    CHECK(doc.patch(patch)["books"][1]["title"] == "Ubik!");

    // Binary round trip (MessagePack and CBOR).
    CHECK(json::from_msgpack(json::to_msgpack(doc)) == doc);
    CHECK(json::from_cbor(json::to_cbor(doc)) == doc);

    // Unicode escapes survive a round trip.
    CHECK(json::parse("\"caf\\u00e9\"").get<std::string>() == "caf\xC3\xA9");
    CHECK(json("caf\xC3\xA9").dump(-1, ' ', true) == "\"caf\\u00e9\"");

    // Error paths: all are C++ exceptions, so this runs the consumer's exception support end to end.
    bool parse_error = false;
    try {
        json::parse("{\"a\": [1, 2,");
    } catch (const json::parse_error &e) {
        parse_error = e.id == 101 && std::string(e.what()).find("parse error") != std::string::npos;
    }
    CHECK(parse_error);

    bool type_error = false;
    try {
        (void)doc["name"].get<int>();
    } catch (const json::type_error &e) {
        type_error = e.id == 302;
    }
    CHECK(type_error);

    bool out_of_range = false;
    try {
        (void)doc.at("missing");
    } catch (const json::out_of_range &e) {
        out_of_range = e.id == 403;
    }
    CHECK(out_of_range);
    CHECK(json::parse("{\"a\": [1, 2,", nullptr, false).is_discarded());  // the non-throwing mode

    if (failures) {
        std::printf("%d check(s) failed\n", failures);
        return 1;
    }
    std::printf("OK nlohmann-json %s: parse, modify, serialise, JSON pointer/patch, msgpack/cbor round trip, "
                "parse_error/type_error/out_of_range\n", version.c_str());
    return 0;
}
