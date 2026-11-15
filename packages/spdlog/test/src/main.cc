// Consumer for spdlog (compiled-library mode, external fmt). Prints OK only after every check ran.
#include <spdlog/spdlog.h>
#include <spdlog/sinks/ostream_sink.h>
#include <spdlog/sinks/null_sink.h>

#include <cstdio>
#include <cstdlib>
#include <memory>
#include <sstream>
#include <string>

static int failures = 0;

#define CHECK(cond)                                         \
  do {                                                      \
    if (!(cond)) {                                          \
      std::printf("FAIL line %d: %s\n", __LINE__, #cond);   \
      failures++;                                           \
    }                                                       \
  } while (0)

static bool version_is_declared() {
  const char* want = std::getenv("REGISTRY_TEST_UPSTREAM_VERSION");
  if (want == nullptr || *want == '\0') {
    std::printf("FAIL: REGISTRY_TEST_UPSTREAM_VERSION is not set (run this through tools/test.py)\n");
    return false;
  }
  std::string got = std::to_string(SPDLOG_VER_MAJOR) + "." + std::to_string(SPDLOG_VER_MINOR) + "." +
                    std::to_string(SPDLOG_VER_PATCH);
  if (got != want) {
    std::printf("FAIL: spdlog reports version %s, but package.toml declares %s\n", got.c_str(), want);
    return false;
  }
  return true;
}

int main() {
  CHECK(version_is_declared());
  CHECK(SPDLOG_VERSION == SPDLOG_VER_MAJOR * 10000 + SPDLOG_VER_MINOR * 100 + SPDLOG_VER_PATCH);

#ifndef SPDLOG_COMPILED_LIB
  std::printf("FAIL: SPDLOG_COMPILED_LIB is not defined for the consumer\n");
  return 1;
#endif
#ifndef SPDLOG_FMT_EXTERNAL
  std::printf("FAIL: SPDLOG_FMT_EXTERNAL is not defined for the consumer\n");
  return 1;
#endif

  std::ostringstream out;
  auto sink = std::make_shared<spdlog::sinks::ostream_sink_mt>(out);
  spdlog::logger logger("t", sink);

  // Format, and the default level is info.
  logger.set_pattern("[%n] [%l] %v");
  logger.info("hello {} {:>5}|{:.2f}", "world", 42, 3.14159);
  CHECK(out.str() == "[t] [info] hello world    42|3.14\n");

  // Level filtering: debug is below the default, warn is above.
  out.str("");
  logger.debug("dropped");
  CHECK(out.str().empty());
  logger.warn("kept {}", 1);
  CHECK(out.str() == "[t] [warning] kept 1\n");

  logger.set_level(spdlog::level::debug);
  out.str("");
  logger.debug("now visible");
  CHECK(out.str() == "[t] [debug] now visible\n");

  logger.set_level(spdlog::level::err);
  out.str("");
  logger.warn("below err");
  logger.error("at err");
  logger.critical("above err");
  CHECK(out.str() == "[t] [error] at err\n[t] [critical] above err\n");
  CHECK(logger.should_log(spdlog::level::critical));
  CHECK(!logger.should_log(spdlog::level::info));

  // A custom pattern: literal text, the level in short form, and the message.
  logger.set_level(spdlog::level::trace);
  logger.set_pattern("<%L|%v>");
  out.str("");
  logger.trace("a");
  logger.info("b");
  logger.error("c");
  CHECK(out.str() == "<T|a>\n<I|b>\n<E|c>\n");

  // Level names round trip.
  CHECK(std::string(spdlog::level::to_string_view(spdlog::level::warn).data(),
                    spdlog::level::to_string_view(spdlog::level::warn).size()) == "warning");
  CHECK(spdlog::level::from_str("error") == spdlog::level::err);

  // The registry-level API through the compiled library: a named logger registered globally.
  std::ostringstream out2;
  auto sink2 = std::make_shared<spdlog::sinks::ostream_sink_st>(out2);
  auto named = std::make_shared<spdlog::logger>("global", sink2);
  named->set_pattern("%v");
  spdlog::register_logger(named);
  CHECK(spdlog::get("global") == named);
  spdlog::get("global")->info("via registry {}", 7);
  CHECK(out2.str() == "via registry 7\n");
  spdlog::drop("global");
  CHECK(spdlog::get("global") == nullptr);

  // The default logger (stdout, lives in the compiled library) is usable and has a name.
  CHECK(spdlog::default_logger() != nullptr);

  if (failures) return 1;
  std::printf("OK spdlog %d.%d.%d\n", SPDLOG_VER_MAJOR, SPDLOG_VER_MINOR, SPDLOG_VER_PATCH);
  return 0;
}
