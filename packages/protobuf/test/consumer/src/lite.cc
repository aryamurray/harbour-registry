// Consumer for the protobuf lite runtime (no descriptors, no reflection): wire primitives, arenas, and the
// repeated containers.
#include <cstdio>
#include <cstdlib>
#include <string>

#include "google/protobuf/arena.h"
#include "google/protobuf/io/coded_stream.h"
#include "google/protobuf/io/zero_copy_stream_impl_lite.h"
#include "google/protobuf/repeated_field.h"
#include "google/protobuf/repeated_ptr_field.h"
#include "google/protobuf/stubs/common.h"
#include "google/protobuf/wire_format_lite.h"

namespace gpb = ::google::protobuf;
using WF = gpb::internal::WireFormatLite;

static int failures = 0;

#define CHECK_T(cond)                                       \
  do {                                                      \
    if (!(cond)) {                                          \
      std::printf("FAIL line %d: %s\n", __LINE__, #cond);   \
      failures++;                                           \
    }                                                       \
  } while (0)

int main() {
  const char* want = std::getenv("REGISTRY_TEST_UPSTREAM_VERSION");
  if (want == nullptr || *want == '\0') {
    std::printf("FAIL: REGISTRY_TEST_UPSTREAM_VERSION is not set (run this through tools/test.py)\n");
    return 1;
  }
  std::string got = std::to_string(GOOGLE_PROTOBUF_VERSION / 1000 % 1000) + "." +
                    std::to_string(GOOGLE_PROTOBUF_VERSION % 1000);
  if (got != want) {
    std::printf("FAIL: protobuf reports version %s, but package.toml declares %s\n", got.c_str(), want);
    return 1;
  }
  CHECK_T(gpb::internal::VersionString(GOOGLE_PROTOBUF_VERSION) == "7." + got);

  // Encode a message by hand with the wire-format helpers: field 1 int32 = 150, field 2 string "Ada".
  std::string buf;
  {
    gpb::io::StringOutputStream sos(&buf);
    gpb::io::CodedOutputStream out(&sos);
    WF::WriteInt32(1, 150, &out);
    WF::WriteString(2, "Ada", &out);
  }
  static const unsigned char expect[] = {0x08, 0x96, 0x01, 0x12, 0x03, 'A', 'd', 'a'};
  CHECK_T(buf.size() == sizeof(expect) && buf.compare(0, buf.size(), reinterpret_cast<const char*>(expect), sizeof(expect)) == 0);
  CHECK_T(WF::Int32Size(150) == 2);
  CHECK_T(WF::StringSize(std::string("Ada")) == 4);

  // And decode it again.
  {
    gpb::io::ArrayInputStream ais(buf.data(), static_cast<int>(buf.size()));
    gpb::io::CodedInputStream in(&ais);
    uint32_t tag = in.ReadTag();
    CHECK_T(WF::GetTagFieldNumber(tag) == 1 && WF::GetTagWireType(tag) == WF::WIRETYPE_VARINT);
    uint32_t v = 0;
    CHECK_T(in.ReadVarint32(&v) && v == 150);
    tag = in.ReadTag();
    CHECK_T(WF::GetTagFieldNumber(tag) == 2 && WF::GetTagWireType(tag) == WF::WIRETYPE_LENGTH_DELIMITED);
    uint32_t len = 0;
    std::string s;
    CHECK_T(in.ReadVarint32(&len) && len == 3 && in.ReadString(&s, 3) && s == "Ada");
    CHECK_T(in.ReadTag() == 0);  // end of input
  }

  // Truncated input is rejected rather than read past.
  {
    gpb::io::ArrayInputStream ais(buf.data(), 2);
    gpb::io::CodedInputStream in(&ais);
    in.ReadTag();
    uint32_t v = 0;
    CHECK_T(!in.ReadVarint32(&v));
  }

  // Arena and repeated fields.
  gpb::Arena arena;
  auto* rf = gpb::Arena::Create<gpb::RepeatedField<int>>(&arena);
  for (int i = 0; i < 100; i++) rf->Add(i * i);
  CHECK_T(rf->size() == 100 && rf->Get(99) == 99 * 99);
  auto* rp = gpb::Arena::Create<gpb::RepeatedPtrField<std::string>>(&arena);
  rp->Add()->assign("one");
  *rp->Add() = "two";
  CHECK_T(rp->size() == 2 && rp->Get(1) == "two");
  CHECK_T(arena.SpaceUsed() > 0);

  if (failures) return 1;
  std::printf("OK protobuf-lite %s\n", got.c_str());
  return 0;
}
