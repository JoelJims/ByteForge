#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif

#include <cerrno>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <fcntl.h>
#include <iostream>
#include <string>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>
#include <vector>

#if defined(__AVX2__)
#include <immintrin.h>
#endif

namespace {

bool parse_hex(const char* encoded, std::string& value) {
    const size_t length = std::strlen(encoded);
    if (length % 2 != 0) return false;
    value.resize(length / 2);
    for (size_t i = 0; i < value.size(); ++i) {
        unsigned int byte = 0;
        if (std::sscanf(encoded + 2 * i, "%2x", &byte) != 1) return false;
        value[i] = static_cast<char>(byte);
    }
    return true;
}

bool matches(const char* row, size_t row_length, const std::string& needle,
             const std::string& operation) {
    if (operation == "equals") {
        return row_length == needle.size() &&
               (row_length == 0 || std::memcmp(row, needle.data(), row_length) == 0);
    }
    if (operation == "prefix") {
        return row_length >= needle.size() &&
               (needle.empty() || std::memcmp(row, needle.data(), needle.size()) == 0);
    }
    if (needle.empty()) return true;
    return ::memmem(row, row_length, needle.data(), needle.size()) != nullptr;
}

void scan_contains_simd(const char* data, size_t file_size, const std::string& needle,
                        std::vector<uint64_t>& result_ids, uint64_t& rows,
                        uint64_t& payload_bytes) {
#if defined(__AVX2__)
    if (needle.empty() || needle.find('\n') != std::string::npos) {
        size_t position = 0;
        uint64_t row_id = 0;
        while (position < file_size) {
            const char* row_start = data + position;
            const void* newline = std::memchr(row_start, '\n', file_size - position);
            const size_t row_length = newline
                ? static_cast<const char*>(newline) - row_start : file_size - position;
            if (matches(row_start, row_length, needle, "contains")) {
                result_ids.push_back(row_id);
            }
            ++rows;
            payload_bytes += row_length;
            position += row_length + (newline ? 1 : 0);
            ++row_id;
        }
        return;
    }
    const __m256i newline_vector = _mm256_set1_epi8('\n');
    const __m256i first_byte_vector = _mm256_set1_epi8(needle[0]);
    uint64_t rows_before = 0;
    uint64_t linefeeds = 0;
    uint64_t previous_match_row = UINT64_MAX;
    size_t offset = 0;
    for (; offset + 32 <= file_size; offset += 32) {
        const __m256i chunk = _mm256_loadu_si256(
            reinterpret_cast<const __m256i*>(data + offset));
        const uint32_t newline_mask = static_cast<uint32_t>(
            _mm256_movemask_epi8(_mm256_cmpeq_epi8(chunk, newline_vector)));
        const uint32_t newlines = static_cast<uint32_t>(__builtin_popcount(newline_mask));
        linefeeds += newlines;
        uint32_t candidates = static_cast<uint32_t>(
            _mm256_movemask_epi8(_mm256_cmpeq_epi8(chunk, first_byte_vector)));
        while (candidates != 0) {
            const unsigned int lane = static_cast<unsigned int>(__builtin_ctz(candidates));
            candidates &= candidates - 1;
            const size_t match_offset = offset + lane;
            if (needle.size() > file_size - match_offset ||
                std::memcmp(data + match_offset, needle.data(), needle.size()) != 0) {
                continue;
            }
            const uint32_t before_mask = lane == 0 ? 0 : ((uint32_t{1} << lane) - 1);
            const uint64_t row = rows_before +
                static_cast<uint64_t>(__builtin_popcount(newline_mask & before_mask));
            if (row != previous_match_row) result_ids.push_back(row);
            previous_match_row = row;
        }
        rows_before += newlines;
    }
    for (; offset < file_size; ++offset) {
        if (data[offset] == '\n') {
            ++linefeeds;
            ++rows_before;
            continue;
        }
        if (needle.size() > file_size - offset || data[offset] != needle[0] ||
            std::memcmp(data + offset, needle.data(), needle.size()) != 0) {
            continue;
        }
        const uint64_t row = rows_before;
        if (row != previous_match_row) result_ids.push_back(row);
        previous_match_row = row;
    }
    rows = linefeeds + (file_size != 0 && data[file_size - 1] != '\n' ? 1 : 0);
    payload_bytes = file_size - linefeeds;
#else
    size_t position = 0;
    uint64_t row_id = 0;
    while (position < file_size) {
        const char* row_start = data + position;
        const void* newline = std::memchr(row_start, '\n', file_size - position);
        const size_t row_length = newline
            ? static_cast<const char*>(newline) - row_start : file_size - position;
        if (matches(row_start, row_length, needle, "contains")) result_ids.push_back(row_id);
        ++rows;
        payload_bytes += row_length;
        position += row_length + (newline ? 1 : 0);
        ++row_id;
    }
#endif
}

void write_u64_le(uint64_t value) {
    unsigned char bytes[8];
    for (unsigned int i = 0; i < 8; ++i) {
        bytes[i] = static_cast<unsigned char>((value >> (8 * i)) & 0xff);
    }
    if (std::fwrite(bytes, 1, sizeof(bytes), stdout) != sizeof(bytes)) {
        std::perror("write result IDs");
        std::exit(2);
    }
}

int fail(const char* message) {
    std::fprintf(stderr, "raw_query: %s: %s\n", message, std::strerror(errno));
    return 2;
}

}  // namespace

int main(int argc, char** argv) {
    if (argc != 4 || (std::string(argv[2]) != "contains" &&
                      std::string(argv[2]) != "equals" &&
                      std::string(argv[2]) != "prefix")) {
        std::fprintf(stderr, "usage: raw_query PATH contains|equals|prefix NEEDLE_HEX\n");
        return 2;
    }
    const std::string operation(argv[2]);
    std::string needle;
    if (!parse_hex(argv[3], needle)) {
        std::fprintf(stderr, "raw_query: invalid hexadecimal query\n");
        return 2;
    }

    const int fd = ::open(argv[1], O_RDONLY | O_CLOEXEC);
    if (fd < 0) return fail("open failed");
    struct stat metadata {};
    if (::fstat(fd, &metadata) != 0) {
        const int result = fail("stat failed");
        ::close(fd);
        return result;
    }
    if (!S_ISREG(metadata.st_mode) || metadata.st_size < 0) {
        std::fprintf(stderr, "raw_query: input must be a regular file\n");
        ::close(fd);
        return 2;
    }
    const size_t file_size = static_cast<size_t>(metadata.st_size);
    const char* data = nullptr;
    if (file_size != 0) {
        void* mapping = ::mmap(nullptr, file_size, PROT_READ, MAP_PRIVATE, fd, 0);
        if (mapping == MAP_FAILED) {
            const int result = fail("mmap failed");
            ::close(fd);
            return result;
        }
        data = static_cast<const char*>(mapping);
    }

    std::vector<uint64_t> result_ids;
    uint64_t row_id = 0;
    uint64_t rows = 0;
    uint64_t payload_bytes = 0;
    size_t position = 0;
    const clock_t cpu_start = std::clock();
    const timespec wall_start = [] {
        timespec value{};
        ::clock_gettime(CLOCK_MONOTONIC, &value);
        return value;
    }();

    if (operation == "contains") {
        scan_contains_simd(data, file_size, needle, result_ids, rows, payload_bytes);
    } else {
        while (position < file_size) {
            const char* row_start = data + position;
            const size_t remaining = file_size - position;
            const void* newline = std::memchr(row_start, '\n', remaining);
            const size_t row_length = newline
                                          ? static_cast<const char*>(newline) - row_start
                                          : remaining;
            if (matches(row_start, row_length, needle, operation)) result_ids.push_back(row_id);
            ++rows;
            payload_bytes += row_length;
            position += row_length;
            if (newline) ++position;  // Exclude only LF; CR remains part of the row.
            ++row_id;
        }
    }

    const clock_t cpu_end = std::clock();
    timespec wall_end{};
    ::clock_gettime(CLOCK_MONOTONIC, &wall_end);
    const int64_t elapsed_ns =
        static_cast<int64_t>(wall_end.tv_sec - wall_start.tv_sec) * 1000000000LL +
        static_cast<int64_t>(wall_end.tv_nsec - wall_start.tv_nsec);
    const uint64_t wall_ns = static_cast<uint64_t>(elapsed_ns);
    const uint64_t cpu_ns = static_cast<uint64_t>(
        (static_cast<double>(cpu_end - cpu_start) / CLOCKS_PER_SEC) * 1000000000.0);

    write_u64_le(static_cast<uint64_t>(result_ids.size()));
    for (const uint64_t id : result_ids) write_u64_le(id);
    if (std::fflush(stdout) != 0) return fail("flush failed");
    std::fprintf(stderr,
                 "{\"rows\":%llu,\"payload_bytes\":%llu,\"matches\":%llu,"
                 "\"scan_wall_ns\":%llu,\"scan_cpu_ns\":%llu}\n",
                 static_cast<unsigned long long>(rows),
                 static_cast<unsigned long long>(payload_bytes),
                 static_cast<unsigned long long>(result_ids.size()),
                 static_cast<unsigned long long>(wall_ns),
                 static_cast<unsigned long long>(cpu_ns));

    if (file_size != 0) ::munmap(const_cast<char*>(data), file_size);
    ::close(fd);
    return 0;
}
