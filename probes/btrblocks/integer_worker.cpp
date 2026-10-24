// Framed, persistent integer-only adapter to the actual pinned BtrBlocks core.
#include "btrblocks.hpp"
#include "compression/Datablock.hpp"
#include "storage/Chunk.hpp"
#include "storage/Relation.hpp"
#include <cstring>
#include <iostream>
#include <stdexcept>
#include <sys/resource.h>

using namespace btrblocks;
static void check(bool okay, const char* message) {
  if (!okay) throw std::runtime_error(message);
}
static uint32_t read32() {
  uint32_t value;
  std::cin.read(reinterpret_cast<char*>(&value), 4);
  check(bool(std::cin), "truncated request");
  return value;
}
static void write32(uint32_t value) {
  std::cout.write(reinterpret_cast<const char*>(&value), 4);
}
int main(int argc, char** argv) {
  try {
    uint32_t endian = 1;
    check(*reinterpret_cast<uint8_t*>(&endian) == 1, "little-endian worker required");
    if (argc == 2 && std::string(argv[1]) == "--info") {
      std::cout << "{\"backend\":\"btrblocks\",\"revision\":"
        "\"c950c3c01c1fbf91007936b05781d431a29a60d3\","
        "\"protocol\":3,\"integer_bits\":32,\"endian\":\"little\","
        "\"pointer_bytes\":" << sizeof(void*) << ",\"sanitized\":"
        << BYTEFORGE_SANITIZED << ",\"fsst_safety_v1\":" << BYTEFORGE_FSST_FIXED
        << ",\"compiler\":\"" << __VERSION__ << "\",\"selection\":\"SAMPLE\","
           "\"sample_count\":10,\"sample_run_size\":64,\"max_depth\":3,"
           "\"cpu_limit_seconds\":3600,\"address_space_limit_bytes\":"
        << (BYTEFORGE_SANITIZED ? "null" : "2147483648") << "}\n";
      return 0;
    }
    check(argc == 1, "unexpected arguments");
    const rlimit cpu{3600, 3600}, core{0, 0};
    check(setrlimit(RLIMIT_CPU, &cpu) == 0 && setrlimit(RLIMIT_CORE, &core) == 0,
          "cannot apply native process limits");
    if (!BYTEFORGE_SANITIZED) {
      const rlimit memory{2147483648ULL, 2147483648ULL};
      check(setrlimit(RLIMIT_AS, &memory) == 0, "cannot apply native address-space limit");
    }
    BtrBlocksConfig::configure();
    while (std::cin.peek() != EOF) {
      const int operation = std::cin.get();
      const uint32_t count = read32(), size = read32();
      check(count >= 1 && count <= 65536 && size <= (4u << 20), "invalid request bounds");
      check(operation == 1 || operation == 2 || operation == 3, "unknown operation");
      std::vector<uint8_t> bytes(size);
      std::cin.read(reinterpret_cast<char*>(bytes.data()), size);
      check(bool(std::cin), "truncated body");
      Vector<INTEGER> values(count);
      if (operation == 1) {
        check(size == count * 4, "integer length mismatch");
        std::memcpy(values.data, bytes.data(), size);
        for (size_t i = 0; i < count; ++i) check(values[i] >= 0, "negative integer");
      } else {
        std::fill(values.begin(), values.end(), 0);
      }
      Relation relation;
      relation.addColumn(Column("integers", std::move(values)));
      Datablock codec(relation);
      BytesArray compressed;
      uint32_t scheme;
      if (operation == 1) {
        auto input = relation.getChunk({Range(0, count)}, 0);
        auto stats = codec.compress(input, compressed);
        auto* header = reinterpret_cast<DatablockMeta*>(compressed.get());
        header->padding = 0;
        header->attributes_meta[0].padding = 0;
        header->attributes_meta[0].bias = 0;
        scheme = stats.used_compression_schemes[0];
        write32(stats.total_db_size);
        write32(scheme);
        write32(stats.total_data_size);
        std::cout.write(reinterpret_cast<const char*>(compressed.get()), stats.total_db_size);
      } else {
        check(size >= sizeof(DatablockMeta) + sizeof(ColumnMeta), "short native block");
        compressed = makeBytesArray(size + SIMD_EXTRA_BYTES);
        std::memset(compressed.get(), 0, size + SIMD_EXTRA_BYTES);
        std::memcpy(compressed.get(), bytes.data(), size);
        const auto* meta = reinterpret_cast<const DatablockMeta*>(compressed.get());
        const auto& column = meta->attributes_meta[0];
        check(meta->count == count && meta->column_count == 1 && meta->size == size,
              "native header mismatch");
        check(column.column_type == ColumnType::INTEGER && column.compression_type <= 5 &&
              column.offset >= 32 && column.offset < size && column.nullmap_offset <= size &&
              static_cast<uint8_t>(column.bitmap_type) <= 3, "invalid native column metadata");
        auto result = codec.decompress(compressed);
        std::vector<uint8_t> byte_values;
        if (operation == 3) byte_values.resize(count);
        for (size_t i = 0; i < count; ++i) {
          check(result.has(0, i), "null decoded integer");
          const auto value = result.get<INTEGER>(0, i);
          check(value >= 0, "invalid decoded integer");
          if (operation == 3) {
            check(value <= 255, "decoded byte ID outside range");
            byte_values[i] = static_cast<uint8_t>(value);
          }
        }
        write32(operation == 3 ? count : count * 4);
        write32(column.compression_type);
        write32(0);
        if (operation == 3) {
          std::cout.write(reinterpret_cast<const char*>(byte_values.data()), count);
        } else {
          std::cout.write(reinterpret_cast<const char*>(result.columns[0].get()), count * 4);
        }
      }
      std::cout.flush();
      check(bool(std::cout), "response write failed");
    }
    return 0;
  } catch (const std::exception& error) {
    std::cerr << error.what() << '\n';
    return 1;
  }
}
