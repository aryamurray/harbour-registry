// Consumer test for googletest. `main` comes from gmock_main (InitGoogleMock + RUN_ALL_TESTS), so the
// program's last line is what a listener registered here prints after the framework's own summary,
// and only when every test passed. googletest has no version macro, so the version cannot be compared
// with REGISTRY_TEST_UPSTREAM_VERSION; the variable is still required.
#include <gmock/gmock.h>
#include <gtest/gtest-spi.h>
#include <gtest/gtest.h>

#include <cstdio>
#include <cstdlib>
#include <map>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

using ::testing::_;
using ::testing::ElementsAre;
using ::testing::HasSubstr;
using ::testing::InSequence;
using ::testing::Return;
using ::testing::StrictMock;

namespace {

class Store {
public:
    virtual ~Store() = default;
    virtual int Get(const std::string &key) = 0;
    virtual void Put(const std::string &key, int value) = 0;
};

class MockStore : public Store {
public:
    MOCK_METHOD(int, Get, (const std::string &), (override));
    MOCK_METHOD(void, Put, (const std::string &, int), (override));
};

int Bump(Store &s, const std::string &key) {
    int v = s.Get(key) + 1;
    s.Put(key, v);
    return v;
}

TEST(Assertions, Basics) {
    EXPECT_EQ(2 + 2, 4);
    EXPECT_NE(std::string("a"), "b");
    EXPECT_LT(1, 2);
    EXPECT_DOUBLE_EQ(0.1 + 0.2, 0.3);
    EXPECT_STREQ("abc", "abc");
    EXPECT_THAT(std::string("hello world"), HasSubstr("o w"));
    EXPECT_THAT(std::vector<int>({1, 2, 3}), ElementsAre(1, 2, 3));
}

// Exceptions: googletest must catch what a test throws and EXPECT_THROW must see it.
TEST(Assertions, Exceptions) {
    EXPECT_THROW(throw std::runtime_error("boom"), std::runtime_error);
    EXPECT_ANY_THROW(std::vector<int>().at(1));
    EXPECT_NO_THROW(std::string("fine"));
}

// The error path: a failing assertion is reported as a failure (captured here, not propagated), and
// a death test sees a process die with the expected message (fork + stderr matching).
TEST(ErrorPaths, FailuresAreDetected) {
    EXPECT_NONFATAL_FAILURE(EXPECT_EQ(1, 2), "Expected equality of these values");
    EXPECT_FATAL_FAILURE(ASSERT_TRUE(false), "Value of: false");
}

TEST(ErrorPaths, DeathTest) {
    GTEST_FLAG_SET(death_test_style, "threadsafe");
    EXPECT_DEATH({ std::fputs("fatal: bad state\n", stderr); std::abort(); }, "fatal: bad state");
    EXPECT_EXIT(std::exit(3), ::testing::ExitedWithCode(3), "");
}

TEST(Mocks, ExpectCallAndSequence) {
    StrictMock<MockStore> s;
    {
        InSequence seq;
        EXPECT_CALL(s, Get("hits")).WillOnce(Return(41));
        EXPECT_CALL(s, Put("hits", 42));
    }
    EXPECT_EQ(Bump(s, "hits"), 42);

    MockStore lax;
    EXPECT_CALL(lax, Get(_)).Times(2).WillRepeatedly(Return(0));
    EXPECT_CALL(lax, Put(_, 1)).Times(2);
    Bump(lax, "a");
    Bump(lax, "b");
}

// Threads: gtest's own mutexes need pthread on the link line (the manifest's public system_libs).
TEST(Threads, ConcurrentExpectations) {
    std::vector<std::thread> pool;
    std::vector<int> results(4, 0);
    for (int i = 0; i < 4; i++) pool.emplace_back([&results, i] { results[i] = i * i; });
    for (auto &t : pool) t.join();
    EXPECT_THAT(results, ElementsAre(0, 1, 4, 9));
}

class Params : public ::testing::TestWithParam<std::pair<int, int>> {};
TEST_P(Params, Squares) { EXPECT_EQ(GetParam().first * GetParam().first, GetParam().second); }
INSTANTIATE_TEST_SUITE_P(Squares, Params,
                         ::testing::Values(std::make_pair(1, 1), std::make_pair(5, 25), std::make_pair(12, 144)));

template <typename T> class Typed : public ::testing::Test {};
using Types = ::testing::Types<int, long, std::string>;
TYPED_TEST_SUITE(Typed, Types);
TYPED_TEST(Typed, DefaultConstructs) { EXPECT_EQ(TypeParam(), TypeParam{}); }

// Prints `OK ...` after the framework's summary, only if everything passed.
class OkListener : public ::testing::EmptyTestEventListener {
    void OnTestProgramEnd(const ::testing::UnitTest &unit) override {
        if (unit.Passed() && unit.successful_test_count() == unit.test_to_run_count() && unit.test_to_run_count() > 0)
            std::printf("OK googletest: %d tests (assertions, exceptions, failure detection, death tests, "
                        "gmock expectations, threads, parameterised and typed tests)\n", unit.successful_test_count());
    }
};
const bool registered = [] {
    if (!std::getenv("REGISTRY_TEST_UPSTREAM_VERSION") || !*std::getenv("REGISTRY_TEST_UPSTREAM_VERSION")) {
        std::printf("FAIL: REGISTRY_TEST_UPSTREAM_VERSION is not set (run this through tools/test.py)\n");
        std::exit(1);
    }
    ::testing::UnitTest::GetInstance()->listeners().Append(new OkListener);
    return true;
}();

}  // namespace
