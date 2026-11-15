// Consumer for the protobuf full runtime, without protoc: messages are built from a FileDescriptorProto
// constructed in code and handled through reflection (DynamicMessage), plus descriptor.proto's own
// generated classes and the well-known types, which are part of the runtime.
#include <cstdio>
#include <cstdlib>
#include <memory>
#include <string>

#include "google/protobuf/descriptor.h"
#include "google/protobuf/descriptor.pb.h"
#include "google/protobuf/dynamic_message.h"
#include "google/protobuf/io/coded_stream.h"
#include "google/protobuf/io/zero_copy_stream_impl_lite.h"
#include "google/protobuf/json/json.h"
#include "google/protobuf/message.h"
#include "google/protobuf/stubs/common.h"
#include "google/protobuf/text_format.h"
#include "google/protobuf/timestamp.pb.h"
#include "google/protobuf/duration.pb.h"
#include "google/protobuf/util/time_util.h"

namespace gpb = ::google::protobuf;

static int failures = 0;

#define CHECK_T(cond)                                       \
  do {                                                      \
    if (!(cond)) {                                          \
      std::printf("FAIL line %d: %s\n", __LINE__, #cond);   \
      failures++;                                           \
    }                                                       \
  } while (0)

static std::string hex(const std::string& s) {
  static const char* d = "0123456789abcdef";
  std::string out;
  for (unsigned char c : s) {
    out += d[c >> 4];
    out += d[c & 15];
  }
  return out;
}

int main() {
  // The release declared in package.toml. GOOGLE_PROTOBUF_VERSION is 7MMMppp for the C++ runtime 7.<minor>.<patch>;
  // the registry version is <minor>.<patch>.
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
  CHECK_T(std::string(PROTOBUF_VERSION_SUFFIX).empty());
  GOOGLE_PROTOBUF_VERIFY_VERSION;

  // Wire primitives: varints and zigzag, with known bytes.
  {
    std::string buf;
    gpb::io::StringOutputStream sos(&buf);
    gpb::io::CodedOutputStream out(&sos);
    out.WriteVarint32(1);
    out.WriteVarint32(300);                    // ac 02
    out.WriteVarint64(0xFFFFFFFFFFFFFFFFull);  // ten bytes
    out.WriteLittleEndian32(0x01020304);
    out.WriteTag(gpb::internal::WireFormatLite::MakeTag(2, gpb::internal::WireFormatLite::WIRETYPE_LENGTH_DELIMITED));
    out.WriteVarint32(3);
    out.WriteRaw("abc", 3);
    CHECK_T(gpb::internal::WireFormatLite::ZigZagEncode32(-1) == 1u);
    CHECK_T(gpb::internal::WireFormatLite::ZigZagEncode64(-2) == 3ull);
    out.Trim();
    CHECK_T(hex(buf) == "01" "ac02" "ffffffffffffffffff01" "04030201" "12" "03" "616263");

    gpb::io::ArrayInputStream ais(buf.data(), static_cast<int>(buf.size()));
    gpb::io::CodedInputStream in(&ais);
    uint32_t a = 0, b = 0, le = 0, tag = 0, len = 0;
    uint64_t big = 0;
    std::string s;
    CHECK_T(in.ReadVarint32(&a) && a == 1);
    CHECK_T(in.ReadVarint32(&b) && b == 300);
    CHECK_T(in.ReadVarint64(&big) && big == 0xFFFFFFFFFFFFFFFFull);
    CHECK_T(in.ReadLittleEndian32(&le) && le == 0x01020304u);
    tag = in.ReadTag();
    CHECK_T(gpb::internal::WireFormatLite::GetTagFieldNumber(tag) == 2);
    CHECK_T(in.ReadVarint32(&len) && len == 3 && in.ReadString(&s, 3) && s == "abc");
  }

  // descriptor.proto's own generated classes: build a FileDescriptorProto, round trip it through bytes.
  gpb::FileDescriptorProto fdp;
  fdp.set_name("registry_test.proto");
  fdp.set_package("rt");
  fdp.set_syntax("proto3");
  {
    gpb::EnumDescriptorProto* e = fdp.add_enum_type();
    e->set_name("Color");
    e->add_value()->set_name("COLOR_UNSPECIFIED");
    gpb::EnumValueDescriptorProto* green = e->add_value();
    green->set_name("GREEN");
    green->set_number(1);

    gpb::DescriptorProto* inner = fdp.add_message_type();
    inner->set_name("Inner");
    gpb::FieldDescriptorProto* f = inner->add_field();
    f->set_name("note");
    f->set_number(1);
    f->set_type(gpb::FieldDescriptorProto::TYPE_STRING);
    f->set_label(gpb::FieldDescriptorProto::LABEL_OPTIONAL);

    gpb::DescriptorProto* msg = fdp.add_message_type();
    msg->set_name("Person");
    auto add = [&](const char* name, int number, gpb::FieldDescriptorProto::Type type,
                   gpb::FieldDescriptorProto::Label label, const char* type_name = nullptr) {
      gpb::FieldDescriptorProto* fp = msg->add_field();
      fp->set_name(name);
      fp->set_number(number);
      fp->set_type(type);
      fp->set_label(label);
      if (type_name) fp->set_type_name(type_name);
    };
    add("id", 1, gpb::FieldDescriptorProto::TYPE_INT32, gpb::FieldDescriptorProto::LABEL_OPTIONAL);
    add("name", 2, gpb::FieldDescriptorProto::TYPE_STRING, gpb::FieldDescriptorProto::LABEL_OPTIONAL);
    add("scores", 3, gpb::FieldDescriptorProto::TYPE_SINT64, gpb::FieldDescriptorProto::LABEL_REPEATED);
    add("inner", 4, gpb::FieldDescriptorProto::TYPE_MESSAGE, gpb::FieldDescriptorProto::LABEL_OPTIONAL, ".rt.Inner");
    add("color", 5, gpb::FieldDescriptorProto::TYPE_ENUM, gpb::FieldDescriptorProto::LABEL_OPTIONAL, ".rt.Color");
  }
  std::string fdp_bytes;
  CHECK_T(fdp.SerializeToString(&fdp_bytes));
  gpb::FileDescriptorProto fdp2;
  CHECK_T(fdp2.ParseFromString(fdp_bytes));
  CHECK_T(fdp2.message_type_size() == 2 && fdp2.message_type(1).field_size() == 5);
  CHECK_T(fdp2.SerializeAsString() == fdp_bytes);
  CHECK_T(gpb::DescriptorProto::descriptor()->full_name() == "google.protobuf.DescriptorProto");
  CHECK_T(gpb::FileDescriptorProto::descriptor()->FindFieldByName("message_type") != nullptr);

  // A pool built from it, and DynamicMessage over it.
  gpb::DescriptorPool pool;
  const gpb::FileDescriptor* fd = pool.BuildFile(fdp);
  CHECK_T(fd != nullptr);
  if (!fd) return 1;
  const gpb::Descriptor* person = fd->FindMessageTypeByName("Person");
  CHECK_T(person != nullptr && person->full_name() == "rt.Person" && person->field_count() == 5);
  gpb::DynamicMessageFactory factory(&pool);
  const gpb::Message* proto = factory.GetPrototype(person);
  std::unique_ptr<gpb::Message> m(proto->New());
  const gpb::Reflection* r = m->GetReflection();
  const gpb::FieldDescriptor* f_id = person->FindFieldByName("id");
  const gpb::FieldDescriptor* f_name = person->FindFieldByName("name");
  const gpb::FieldDescriptor* f_scores = person->FindFieldByName("scores");
  const gpb::FieldDescriptor* f_inner = person->FindFieldByName("inner");
  const gpb::FieldDescriptor* f_color = person->FindFieldByName("color");
  r->SetInt32(m.get(), f_id, 150);
  r->SetString(m.get(), f_name, "Ada");
  r->AddInt64(m.get(), f_scores, -1);
  r->AddInt64(m.get(), f_scores, 2);
  gpb::Message* inner_msg = r->MutableMessage(m.get(), f_inner);
  inner_msg->GetReflection()->SetString(inner_msg, f_inner->message_type()->FindFieldByName("note"), "hi");
  r->SetEnum(m.get(), f_color, f_color->enum_type()->FindValueByName("GREEN"));

  // Serialise: id=150 -> 08 96 01; name -> 12 03 "Ada"; scores packed (proto3) 1a 02 01 04;
  // inner -> 22 04 0a 02 "hi"; color=1 -> 28 01.
  std::string wire;
  CHECK_T(m->SerializeToString(&wire));
  CHECK_T(hex(wire) == "089601" "1203416461" "1a020104" "22040a026869" "2801");

  // Parse into a fresh message, compare through reflection and text format.
  std::unique_ptr<gpb::Message> m2(proto->New());
  CHECK_T(m2->ParseFromString(wire));
  CHECK_T(m2->GetReflection()->GetInt32(*m2, f_id) == 150);
  CHECK_T(m2->GetReflection()->GetString(*m2, f_name) == "Ada");
  CHECK_T(m2->GetReflection()->FieldSize(*m2, f_scores) == 2);
  CHECK_T(m2->GetReflection()->GetRepeatedInt64(*m2, f_scores, 0) == -1);
  CHECK_T(m2->GetReflection()->GetRepeatedInt64(*m2, f_scores, 1) == 2);
  CHECK_T(m2->GetReflection()->GetEnumValue(*m2, f_color) == 1);
  CHECK_T(m2->ByteSizeLong() == wire.size());

  std::string text;
  gpb::TextFormat::Printer printer;
  printer.SetSingleLineMode(true);
  CHECK_T(printer.PrintToString(*m, &text));
  CHECK_T(text == "id: 150 name: \"Ada\" scores: -1 scores: 2 inner { note: \"hi\" } color: GREEN ");

  std::unique_ptr<gpb::Message> m3(proto->New());
  CHECK_T(gpb::TextFormat::ParseFromString("id: 7 name: \"Bob\" scores: [5, 6] color: GREEN", m3.get()));
  CHECK_T(m3->GetReflection()->GetInt32(*m3, f_id) == 7);
  CHECK_T(m3->GetReflection()->FieldSize(*m3, f_scores) == 2);
  CHECK_T(!gpb::TextFormat::ParseFromString("id: \"not a number\"", m3.get()));

  // Unknown fields survive a parse/serialise round trip.
  {
    std::string with_unknown = wire;
    with_unknown += std::string("\xf8\x07\x2a", 3);  // field 127, varint 42
    std::unique_ptr<gpb::Message> mu(proto->New());
    CHECK_T(mu->ParseFromString(with_unknown));
    CHECK_T(mu->GetReflection()->GetUnknownFields(*mu).field_count() == 1);
    CHECK_T(mu->SerializeAsString() == with_unknown);
  }

  // JSON (part of the full runtime), through the same pool.
  {
    std::string json;
    gpb::json::PrintOptions po;
    auto st = gpb::json::MessageToJsonString(*m, &json, po);
    CHECK_T(st.ok());
    CHECK_T(json == "{\"id\":150,\"name\":\"Ada\",\"scores\":[\"-1\",\"2\"],\"inner\":{\"note\":\"hi\"},\"color\":\"GREEN\"}");
  }

  // Well-known types compiled into the runtime.
  gpb::Timestamp ts = gpb::util::TimeUtil::SecondsToTimestamp(1700000000);
  CHECK_T(gpb::util::TimeUtil::ToString(ts) == "2023-11-14T22:13:20Z");
  gpb::Timestamp parsed;
  CHECK_T(gpb::util::TimeUtil::FromString("2023-11-14T22:13:20Z", &parsed) && parsed.seconds() == 1700000000);
  gpb::Duration d = gpb::util::TimeUtil::MillisecondsToDuration(1500);
  CHECK_T(gpb::util::TimeUtil::ToString(d) == "1.500s");

  if (failures) return 1;
  std::printf("OK protobuf %s\n", got.c_str());
  return 0;
}
