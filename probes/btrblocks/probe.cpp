// Tiny executable qualification, not a backend or a performance benchmark.
#include "btrblocks.hpp"
#include "compression/Datablock.hpp"
#include "storage/Chunk.hpp"
#include "storage/Relation.hpp"

#include <cstring>
#include <iostream>
#include <stdexcept>
#include <type_traits>

using namespace btrblocks;

static void require(bool condition, const char* message) {
  if (!condition) throw std::runtime_error(message);
}

static void integers(size_t count) {
  Vector<INTEGER> values(count);
  for (size_t i = 0; i < count; ++i) values[i] = static_cast<INTEGER>(i % 65536);
  Relation relation;
  relation.addColumn(Column("tokens", std::move(values)));
  auto input = relation.getChunk({Range(0, count)}, 0);
  Datablock codec(relation);
  BytesArray compressed;
  const auto stats = codec.compress(input, compressed);
  auto restored = codec.decompress(compressed);
  require(restored.tuple_count == count, "integer count mismatch");
  for (size_t i = 0; i < count; ++i) {
    require(restored.get<INTEGER>(0, i) == static_cast<INTEGER>(i % 65536), "ID mismatch");
  }
  std::cout << "integer rows=" << count << " scheme="
            << static_cast<unsigned>(stats.used_compression_schemes[0]) << " passed\n";
}

static void strings(const std::vector<std::string>& rows) {
  // Vector<str> is a specialized mmap-backed indexed pool, not an assignable
  // vector<string_view>. Build its actual layout; Relation then creates the
  // native adjacent int32-offset Chunk representation.
  Vector<str> values;
  size_t payload = 0;
  for (const auto& row : rows) payload += row.size();
  values.fileSize = sizeof(Vector<str>::Data) + rows.size() * sizeof(StringIndexSlot) + payload;
  values.data = static_cast<Vector<str>::Data*>(
      mmap(nullptr, values.fileSize, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0));
  require(values.data != MAP_FAILED, "mmap failed");
  values.data->count = rows.size();
  size_t offset = sizeof(Vector<str>::Data) + rows.size() * sizeof(StringIndexSlot);
  for (size_t i = 0; i < rows.size(); ++i) {
    values.data->slot[i] = {rows[i].size(), offset};
    std::memcpy(reinterpret_cast<char*>(values.data) + offset, rows[i].data(), rows[i].size());
    offset += rows[i].size();
  }
  Relation string_relation;
  string_relation.addColumn(Column("value", std::move(values)));
  auto input = string_relation.getChunk({Range(0, rows.size())}, 0);
  Datablock codec(string_relation);
  BytesArray compressed;
  const auto stats = codec.compress(input, compressed);
  auto restored = codec.decompress(compressed);
  require(restored.tuple_count == rows.size(), "string count mismatch");
  for (size_t i = 0; i < rows.size(); ++i) {
    const auto view = restored(0, i);
    const std::string owned(view.data(), view.size());
    require(owned == rows[i], "string bytes mismatch");
  }
  std::cout << "string rows=" << rows.size() << " scheme="
            << static_cast<unsigned>(stats.used_compression_schemes[0]) << " passed\n";
}

int main(int argc, char** argv) {
  static_assert(std::is_same_v<INTEGER, int32_t>);
  try {
    require(argc == 2, "expected case: integers, empty, all_empty, unicode, long");
    const std::string name = argv[1];
    BtrBlocksConfig::configure();
    if (name == "integers") {
      for (auto count : {1u, 65535u, 65536u}) integers(count);
    } else if (name == "empty") {
      strings({""});
    } else if (name == "all_empty") {
      strings({"", "", ""});
    } else if (name == "unicode") {
      strings({"hello", "hello", std::string("a\0b", 3), "😀", "中文", "العربية", "é", "é", "a\nb"});
    } else if (name == "long") {
      strings({std::string(100000, 'x'), "", "end"});
    } else {
      throw std::runtime_error("unknown probe case");
    }
    return 0;
  } catch (const std::exception& error) {
    std::cerr << error.what() << '\n';
    return 1;
  }
}
