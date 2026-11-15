// Consumer for Abseil. Prints OK only after every check ran.
#include <cstdio>
#include <cstdlib>
#include <string>
#include <thread>
#include <vector>

#include "absl/base/config.h"
#include "absl/container/flat_hash_map.h"
#include "absl/hash/hash.h"
#include "absl/log/check.h"
#include "absl/random/random.h"
#include "absl/status/status.h"
#include "absl/status/statusor.h"
#include "absl/strings/numbers.h"
#include "absl/strings/str_cat.h"
#include "absl/strings/str_format.h"
#include "absl/strings/str_join.h"
#include "absl/strings/str_split.h"
#include "absl/strings/string_view.h"
#include "absl/synchronization/mutex.h"
#include "absl/time/civil_time.h"
#include "absl/time/time.h"

static int failures = 0;

#define CHECK_T(cond)                                       \
  do {                                                      \
    if (!(cond)) {                                          \
      std::printf("FAIL line %d: %s\n", __LINE__, #cond);   \
      failures++;                                           \
    }                                                       \
  } while (0)

static absl::StatusOr<int> ParsePositive(const std::string& s) {
  int v;
  if (!absl::SimpleAtoi(s, &v)) return absl::InvalidArgumentError(absl::StrCat("not a number: ", s));
  if (v <= 0) return absl::OutOfRangeError("not positive");
  return v;
}

int main() {
  // The release declared in package.toml: ABSL_LTS_RELEASE_VERSION is the date, PATCH_LEVEL the `.N`.
  const char* want = std::getenv("REGISTRY_TEST_UPSTREAM_VERSION");
  if (want == nullptr || *want == '\0') {
    std::printf("FAIL: REGISTRY_TEST_UPSTREAM_VERSION is not set (run this through tools/test.py)\n");
    return 1;
  }
  std::string got = absl::StrCat(ABSL_LTS_RELEASE_VERSION, ".", ABSL_LTS_RELEASE_PATCH_LEVEL);
  if (got != want) {
    std::printf("FAIL: abseil reports version %s, but package.toml declares %s\n", got.c_str(), want);
    return 1;
  }

  // Strings.
  CHECK_T(absl::StrCat("a", 1, "b", 2.5, "c") == "a1b2.5c");
  std::vector<std::string> parts = absl::StrSplit("a,b,,c", ',');
  CHECK_T(parts.size() == 4 && parts[2].empty() && parts[3] == "c");
  CHECK_T(absl::StrJoin(parts, "-") == "a-b--c");
  std::vector<std::string> nonempty = absl::StrSplit("a,b,,c", ',', absl::SkipEmpty());
  CHECK_T(nonempty.size() == 3);
  CHECK_T(absl::StrFormat("%05d|%-4s|%.3f", 42, "ab", 3.14159) == "00042|ab  |3.142");

  // flat_hash_map.
  absl::flat_hash_map<std::string, int> m;
  for (int i = 0; i < 1000; i++) m[absl::StrCat("k", i)] = i;
  CHECK_T(m.size() == 1000);
  CHECK_T(m.at("k999") == 999);
  CHECK_T(m.find("nope") == m.end());
  m.erase("k5");
  CHECK_T(m.size() == 999 && !m.contains("k5"));

  // Time formatting with a fixed zone (no tz database needed).
  absl::TimeZone utc = absl::UTCTimeZone();
  absl::Time t = absl::FromUnixSeconds(1700000000);
  CHECK_T(absl::FormatTime("%Y-%m-%d %H:%M:%S %z", t, utc) == "2023-11-14 22:13:20 +0000");
  absl::TimeZone fixed = absl::FixedTimeZone(5 * 3600 + 30 * 60);
  CHECK_T(absl::FormatTime("%Y-%m-%d %H:%M:%S %z", t, fixed) == "2023-11-15 03:43:20 +0530");
  absl::Time parsed;
  std::string err;
  CHECK_T(absl::ParseTime("%Y-%m-%d %H:%M:%S", "2023-11-14 22:13:20", utc, &parsed, &err));
  CHECK_T(parsed == t);
  CHECK_T(absl::FormatDuration(absl::Seconds(90) + absl::Milliseconds(5)) == "1m30.005s");
  absl::CivilDay d = absl::ToCivilDay(t, utc);
  CHECK_T(d.year() == 2023 && d.month() == 11 && d.day() == 14);

  // Status and StatusOr.
  absl::Status ok;
  CHECK_T(ok.ok());
  absl::Status bad = absl::NotFoundError("missing");
  CHECK_T(!bad.ok() && bad.code() == absl::StatusCode::kNotFound && bad.message() == "missing");
  CHECK_T(bad.ToString() == "NOT_FOUND: missing");
  auto r1 = ParsePositive("17");
  CHECK_T(r1.ok() && *r1 == 17);
  auto r2 = ParsePositive("x");
  CHECK_T(!r2.ok() && r2.status().code() == absl::StatusCode::kInvalidArgument);
  CHECK_T(ParsePositive("-3").status().code() == absl::StatusCode::kOutOfRange);

  // Mutex across two threads.
  absl::Mutex mu;
  long counter = 0;
  const int kIters = 100000;
  auto work = [&] {
    for (int i = 0; i < kIters; i++) {
      absl::MutexLock l(mu);
      ++counter;
    }
  };
  std::thread a(work), b(work);
  a.join();
  b.join();
  CHECK_T(counter == 2L * kIters);

  // Hash: stable within a run, equal for equal values, and discriminating.
  CHECK_T(absl::Hash<std::string>()("hello") == absl::Hash<std::string>()(std::string("hel") + "lo"));
  CHECK_T(absl::HashOf(1, 2, absl::string_view("x")) == absl::HashOf(1, 2, absl::string_view("x")));
  CHECK_T(absl::HashOf(1, 2, absl::string_view("x")) != absl::HashOf(1, 2, absl::string_view("y")));
  CHECK_T(absl::Hash<int>()(1) != absl::Hash<int>()(2));

  // Random: goes through Randen (the SIMD target on x86_64/aarch64, the portable one elsewhere).
  absl::BitGen gen;
  bool all_in_range = true;
  for (int i = 0; i < 1000; i++) {
    int v = absl::Uniform(gen, 10, 20);
    if (v < 10 || v >= 20) all_in_range = false;
  }
  CHECK_T(all_in_range);
  CHECK_T(absl::Uniform<uint64_t>(gen) != absl::Uniform<uint64_t>(gen) ||
          absl::Uniform<uint64_t>(gen) != absl::Uniform<uint64_t>(gen));
  absl::BitGen seeded(std::seed_seq{1, 2, 3});
  absl::BitGen seeded2(std::seed_seq{1, 2, 3});
  CHECK_T(absl::Uniform<uint64_t>(seeded) == absl::Uniform<uint64_t>(seeded2));

  // Logging/CHECK from the log library.
  CHECK(1 + 1 == 2);

  if (failures) return 1;
  std::printf("OK abseil %s\n", got.c_str());
  return 0;
}
