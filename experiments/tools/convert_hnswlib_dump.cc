#include <array>
#include <bit>
#include <cerrno>
#include <chrono>
#include <cstdint>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <limits>
#include <optional>
#include <sstream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <unordered_set>
#include <vector>

#include <fcntl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>

namespace fs = std::filesystem;

namespace {

constexpr std::uint64_t kHnswlibHeaderBytes = 96;
constexpr std::uint64_t kGraphBeyondHeaderBytes = 16;
constexpr std::uint64_t kEntryNodeBit = 1ULL << 16;
constexpr std::uint64_t kRemoteOffsetLimit = 1ULL << 48;

class ConversionError : public std::runtime_error {
 public:
  using std::runtime_error::runtime_error;
};

class MappedFile {
 public:
  explicit MappedFile(const fs::path& path) : path_(path) {
    fd_ = ::open(path.c_str(), O_RDONLY);
    if (fd_ < 0) {
      throw ConversionError("cannot open input " + path.string() + ": " + std::strerror(errno));
    }

    struct stat info {};
    if (::fstat(fd_, &info) != 0) {
      const std::string message = std::strerror(errno);
      ::close(fd_);
      fd_ = -1;
      throw ConversionError("cannot stat input " + path.string() + ": " + message);
    }
    if (info.st_size <= 0) {
      ::close(fd_);
      fd_ = -1;
      throw ConversionError("input is empty: " + path.string());
    }
    size_ = static_cast<std::uint64_t>(info.st_size);
    data_ = static_cast<const std::uint8_t*>(
        ::mmap(nullptr, static_cast<std::size_t>(size_), PROT_READ, MAP_PRIVATE, fd_, 0));
    if (data_ == MAP_FAILED) {
      data_ = nullptr;
      const std::string message = std::strerror(errno);
      ::close(fd_);
      fd_ = -1;
      throw ConversionError("cannot mmap input " + path.string() + ": " + message);
    }
  }

  MappedFile(const MappedFile&) = delete;
  MappedFile& operator=(const MappedFile&) = delete;

  ~MappedFile() {
    if (data_ != nullptr) {
      ::munmap(const_cast<std::uint8_t*>(data_), static_cast<std::size_t>(size_));
    }
    if (fd_ >= 0) {
      ::close(fd_);
    }
  }

  const std::uint8_t* data() const { return data_; }
  std::uint64_t size() const { return size_; }

  void require(std::uint64_t offset, std::uint64_t bytes, std::string_view description) const {
    if (offset > size_ || bytes > size_ - offset) {
      throw ConversionError(std::string(description) + " is outside input bounds at offset " +
                            std::to_string(offset));
    }
  }

  template <typename T>
  T read(std::uint64_t offset, std::string_view description) const {
    require(offset, sizeof(T), description);
    T value {};
    std::memcpy(&value, data_ + offset, sizeof(T));
    return value;
  }

 private:
  fs::path path_;
  int fd_ = -1;
  const std::uint8_t* data_ = nullptr;
  std::uint64_t size_ = 0;
};

struct HnswlibHeader {
  std::uint64_t offset_level0;
  std::uint64_t max_elements;
  std::uint64_t count;
  std::uint64_t size_data_per_element;
  std::uint64_t label_offset;
  std::uint64_t data_offset;
  std::int32_t max_level;
  std::uint32_t entry_internal_id;
  std::uint64_t max_m;
  std::uint64_t max_m0;
  std::uint64_t m;
  double multiplier;
  std::uint64_t ef_construction;
};

struct Options {
  fs::path input;
  fs::path output;
  fs::path manifest;
  std::uint64_t dim = 0;
  std::optional<std::uint64_t> expect_m;
  std::optional<std::uint64_t> expect_ef_construction;
  bool force = false;
};

std::uint64_t checked_add(std::uint64_t left, std::uint64_t right, std::string_view description) {
  if (right > std::numeric_limits<std::uint64_t>::max() - left) {
    throw ConversionError(std::string(description) + " overflows uint64");
  }
  return left + right;
}

std::uint64_t checked_multiply(std::uint64_t left, std::uint64_t right,
                               std::string_view description) {
  if (left != 0 && right > std::numeric_limits<std::uint64_t>::max() / left) {
    throw ConversionError(std::string(description) + " overflows uint64");
  }
  return left * right;
}

std::uint64_t align8(std::uint64_t value) {
  return checked_add(value, 7, "alignment") & ~std::uint64_t {7};
}

std::uint64_t parse_u64(std::string_view text, std::string_view option) {
  std::size_t used = 0;
  unsigned long long value = 0;
  try {
    value = std::stoull(std::string(text), &used, 10);
  } catch (const std::exception&) {
    throw ConversionError("invalid numeric value for " + std::string(option) + ": " +
                          std::string(text));
  }
  if (used != text.size()) {
    throw ConversionError("invalid numeric value for " + std::string(option) + ": " +
                          std::string(text));
  }
  return static_cast<std::uint64_t>(value);
}

void print_usage(std::ostream& out) {
  out << "Usage: convert_hnswlib_dump --input PATH --output PATH --dim N "
         "--manifest PATH [--expect-m N] [--expect-ef-construction N] [--force]\n";
}

Options parse_options(int argc, char** argv) {
  Options options;
  for (int index = 1; index < argc; ++index) {
    const std::string_view argument = argv[index];
    if (argument == "--force") {
      options.force = true;
      continue;
    }
    if (argument == "--help" || argument == "-h") {
      print_usage(std::cout);
      std::exit(0);
    }
    if (index + 1 >= argc) {
      throw ConversionError("missing value after " + std::string(argument));
    }
    const std::string_view value = argv[++index];
    if (argument == "--input") {
      options.input = value;
    } else if (argument == "--output") {
      options.output = value;
    } else if (argument == "--manifest") {
      options.manifest = value;
    } else if (argument == "--dim") {
      options.dim = parse_u64(value, argument);
    } else if (argument == "--expect-m") {
      options.expect_m = parse_u64(value, argument);
    } else if (argument == "--expect-ef-construction") {
      options.expect_ef_construction = parse_u64(value, argument);
    } else {
      throw ConversionError("unknown option: " + std::string(argument));
    }
  }

  if (options.input.empty() || options.output.empty() || options.manifest.empty() ||
      options.dim == 0) {
    throw ConversionError("--input, --output, --dim, and --manifest are required");
  }
  if (options.output == options.input || options.manifest == options.input ||
      options.manifest == options.output) {
    throw ConversionError("input, output, and manifest paths must be distinct");
  }
  return options;
}

HnswlibHeader read_header(const MappedFile& input) {
  if (input.size() < kHnswlibHeaderBytes) {
    throw ConversionError("input is smaller than the 96-byte hnswlib header");
  }
  HnswlibHeader header {};
  header.offset_level0 = input.read<std::uint64_t>(0, "offsetLevel0");
  header.max_elements = input.read<std::uint64_t>(8, "max_elements");
  header.count = input.read<std::uint64_t>(16, "cur_element_count");
  header.size_data_per_element = input.read<std::uint64_t>(24, "size_data_per_element");
  header.label_offset = input.read<std::uint64_t>(32, "label_offset");
  header.data_offset = input.read<std::uint64_t>(40, "offsetData");
  header.max_level = input.read<std::int32_t>(48, "maxlevel");
  header.entry_internal_id = input.read<std::uint32_t>(52, "enterpoint_node");
  header.max_m = input.read<std::uint64_t>(56, "maxM");
  header.max_m0 = input.read<std::uint64_t>(64, "maxM0");
  header.m = input.read<std::uint64_t>(72, "M");
  header.multiplier = input.read<double>(80, "mult");
  header.ef_construction = input.read<std::uint64_t>(88, "ef_construction");
  return header;
}

void validate_header(const HnswlibHeader& header, const Options& options) {
  if (header.offset_level0 != 0) {
    throw ConversionError("unsupported hnswlib layout: offsetLevel0 must be zero");
  }
  if (header.count == 0 || header.count > header.max_elements ||
      header.count > std::numeric_limits<std::uint32_t>::max()) {
    throw ConversionError("invalid hnswlib element count");
  }
  if (header.entry_internal_id >= header.count || header.max_level < 0) {
    throw ConversionError("invalid hnswlib entry point or maximum level");
  }
  if (header.m == 0 || header.max_m != header.m || header.max_m0 != 2 * header.m ||
      header.max_m > std::numeric_limits<std::uint32_t>::max()) {
    throw ConversionError("unsupported hnswlib M/maxM/maxM0 layout");
  }
  if (options.expect_m && *options.expect_m != header.m) {
    throw ConversionError("hnswlib M does not match --expect-m");
  }
  if (options.expect_ef_construction &&
      *options.expect_ef_construction != header.ef_construction) {
    throw ConversionError("hnswlib ef_construction does not match --expect-ef-construction");
  }

  const std::uint64_t level0_bytes = checked_add(
      4, checked_multiply(header.max_m0, 4, "level-0 link storage"),
      "level-0 link storage");
  const std::uint64_t vector_bytes = checked_multiply(options.dim, 4, "vector storage");
  const std::uint64_t expected_label_offset =
      checked_add(level0_bytes, vector_bytes, "label offset");
  const std::uint64_t expected_element_bytes =
      checked_add(expected_label_offset, 8, "element layout");
  if (header.data_offset != level0_bytes || header.label_offset != expected_label_offset ||
      header.size_data_per_element != expected_element_bytes) {
    throw ConversionError(
        "hnswlib layout does not match the requested float32 dimension");
  }
}

std::uint16_t list_count(const MappedFile& input, std::uint64_t offset,
                         std::uint64_t capacity, std::string_view description) {
  const std::uint16_t count = input.read<std::uint16_t>(offset, description);
  if (count > capacity) {
    throw ConversionError(std::string(description) + " count " + std::to_string(count) +
                          " exceeds capacity " + std::to_string(capacity));
  }
  return count;
}

void validate_neighbor_ids(const MappedFile& input, std::uint64_t list_offset,
                           std::uint16_t count, std::uint64_t total,
                           std::string_view description) {
  input.require(list_offset, checked_add(4, checked_multiply(count, 4, "neighbor ids"),
                                         "neighbor list"),
                description);
  for (std::uint16_t index = 0; index < count; ++index) {
    const std::uint32_t neighbor = input.read<std::uint32_t>(
        list_offset + 4 + static_cast<std::uint64_t>(index) * 4, description);
    if (neighbor >= total) {
      throw ConversionError(std::string(description) + " contains out-of-range internal id " +
                            std::to_string(neighbor));
    }
  }
}

std::uint64_t graphbeyond_record_size(std::uint64_t dim, std::uint64_t m,
                                      std::uint32_t level) {
  std::uint64_t size = 16;
  size = checked_add(size, checked_multiply(dim, 4, "GraphBeyond vector"),
                     "GraphBeyond record");
  size = checked_add(size,
                     checked_add(4, checked_multiply(2 * m, 8, "GraphBeyond level 0"),
                                 "GraphBeyond level 0"),
                     "GraphBeyond record");
  size = checked_add(
      size,
      checked_multiply(level,
                       checked_add(4, checked_multiply(m, 8, "GraphBeyond upper level"),
                                   "GraphBeyond upper level"),
                       "GraphBeyond upper levels"),
      "GraphBeyond record");
  return align8(size);
}

class Sha256 {
 public:
  void update(const std::uint8_t* data, std::size_t length) {
    total_bytes_ += length;
    while (length > 0) {
      const std::size_t copied = std::min(length, block_.size() - block_size_);
      std::memcpy(block_.data() + block_size_, data, copied);
      block_size_ += copied;
      data += copied;
      length -= copied;
      if (block_size_ == block_.size()) {
        transform(block_.data());
        block_size_ = 0;
      }
    }
  }

  std::string finish() {
    const std::uint64_t bit_length = total_bytes_ * 8;
    block_[block_size_++] = 0x80;
    if (block_size_ > 56) {
      std::fill(block_.begin() + static_cast<std::ptrdiff_t>(block_size_), block_.end(), 0);
      transform(block_.data());
      block_size_ = 0;
    }
    std::fill(block_.begin() + static_cast<std::ptrdiff_t>(block_size_), block_.begin() + 56, 0);
    for (int index = 0; index < 8; ++index) {
      block_[63 - index] = static_cast<std::uint8_t>(bit_length >> (index * 8));
    }
    transform(block_.data());

    std::ostringstream rendered;
    rendered << std::hex << std::setfill('0');
    for (const std::uint32_t word : state_) {
      rendered << std::setw(8) << word;
    }
    return rendered.str();
  }

 private:
  static constexpr std::array<std::uint32_t, 64> kRoundConstants = {
      0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1,
      0x923f82a4, 0xab1c5ed5, 0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3,
      0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786,
      0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
      0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147,
      0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13,
      0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b,
      0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
      0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a,
      0x5b9cca4f, 0x682e6ff3, 0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208,
      0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2};

  static std::uint32_t rotate_right(std::uint32_t value, std::uint32_t bits) {
    return (value >> bits) | (value << (32 - bits));
  }

  void transform(const std::uint8_t* block) {
    std::array<std::uint32_t, 64> words {};
    for (std::size_t index = 0; index < 16; ++index) {
      words[index] = (static_cast<std::uint32_t>(block[index * 4]) << 24) |
                     (static_cast<std::uint32_t>(block[index * 4 + 1]) << 16) |
                     (static_cast<std::uint32_t>(block[index * 4 + 2]) << 8) |
                     static_cast<std::uint32_t>(block[index * 4 + 3]);
    }
    for (std::size_t index = 16; index < words.size(); ++index) {
      const std::uint32_t s0 = rotate_right(words[index - 15], 7) ^
                               rotate_right(words[index - 15], 18) ^
                               (words[index - 15] >> 3);
      const std::uint32_t s1 = rotate_right(words[index - 2], 17) ^
                               rotate_right(words[index - 2], 19) ^
                               (words[index - 2] >> 10);
      words[index] = words[index - 16] + s0 + words[index - 7] + s1;
    }

    std::uint32_t a = state_[0];
    std::uint32_t b = state_[1];
    std::uint32_t c = state_[2];
    std::uint32_t d = state_[3];
    std::uint32_t e = state_[4];
    std::uint32_t f = state_[5];
    std::uint32_t g = state_[6];
    std::uint32_t h = state_[7];
    for (std::size_t index = 0; index < words.size(); ++index) {
      const std::uint32_t sigma1 = rotate_right(e, 6) ^ rotate_right(e, 11) ^ rotate_right(e, 25);
      const std::uint32_t choice = (e & f) ^ (~e & g);
      const std::uint32_t temp1 = h + sigma1 + choice + kRoundConstants[index] + words[index];
      const std::uint32_t sigma0 = rotate_right(a, 2) ^ rotate_right(a, 13) ^ rotate_right(a, 22);
      const std::uint32_t majority = (a & b) ^ (a & c) ^ (b & c);
      const std::uint32_t temp2 = sigma0 + majority;
      h = g;
      g = f;
      f = e;
      e = d + temp1;
      d = c;
      c = b;
      b = a;
      a = temp1 + temp2;
    }
    state_[0] += a;
    state_[1] += b;
    state_[2] += c;
    state_[3] += d;
    state_[4] += e;
    state_[5] += f;
    state_[6] += g;
    state_[7] += h;
  }

  std::array<std::uint32_t, 8> state_ = {0x6a09e667, 0xbb67ae85, 0x3c6ef372,
                                          0xa54ff53a, 0x510e527f, 0x9b05688c,
                                          0x1f83d9ab, 0x5be0cd19};
  std::array<std::uint8_t, 64> block_ {};
  std::size_t block_size_ = 0;
  std::uint64_t total_bytes_ = 0;
};

std::string sha256_bytes(const std::uint8_t* data, std::uint64_t bytes) {
  Sha256 digest;
  constexpr std::uint64_t kChunk = 1ULL << 30;
  std::uint64_t offset = 0;
  while (offset < bytes) {
    const std::uint64_t count = std::min(kChunk, bytes - offset);
    digest.update(data + offset, static_cast<std::size_t>(count));
    offset += count;
  }
  return digest.finish();
}

std::string sha256_file(const fs::path& path) {
  std::ifstream input(path, std::ios::binary);
  if (!input) {
    throw ConversionError("cannot reopen output for SHA-256: " + path.string());
  }
  Sha256 digest;
  std::array<std::uint8_t, 1024 * 1024> buffer {};
  while (input) {
    input.read(reinterpret_cast<char*>(buffer.data()), buffer.size());
    const std::streamsize count = input.gcount();
    if (count > 0) {
      digest.update(buffer.data(), static_cast<std::size_t>(count));
    }
  }
  if (!input.eof()) {
    throw ConversionError("failed while hashing output: " + path.string());
  }
  return digest.finish();
}

template <typename T>
void write_pod(std::ofstream& output, const T& value) {
  output.write(reinterpret_cast<const char*>(&value), sizeof(value));
  if (!output) {
    throw ConversionError("failed while writing converted dump");
  }
}

void write_zeroes(std::ofstream& output, std::uint64_t bytes) {
  static constexpr std::array<char, 4096> zeroes {};
  while (bytes > 0) {
    const std::uint64_t count = std::min<std::uint64_t>(bytes, zeroes.size());
    output.write(zeroes.data(), static_cast<std::streamsize>(count));
    if (!output) {
      throw ConversionError("failed while writing converted dump padding");
    }
    bytes -= count;
  }
}

std::string json_escape(std::string_view value) {
  std::ostringstream rendered;
  for (const unsigned char character : value) {
    switch (character) {
      case '\\': rendered << "\\\\"; break;
      case '"': rendered << "\\\""; break;
      case '\n': rendered << "\\n"; break;
      case '\r': rendered << "\\r"; break;
      case '\t': rendered << "\\t"; break;
      default:
        if (character < 0x20) {
          rendered << "\\u" << std::hex << std::setw(4) << std::setfill('0')
                   << static_cast<int>(character) << std::dec;
        } else {
          rendered << character;
        }
    }
  }
  return rendered.str();
}

fs::path temporary_path_for(const fs::path& target) {
  const auto nonce = std::chrono::steady_clock::now().time_since_epoch().count();
  return target.parent_path() /
         ("." + target.filename().string() + ".tmp." + std::to_string(::getpid()) + "." +
          std::to_string(nonce));
}

void ensure_parent(const fs::path& path) {
  const fs::path parent = path.parent_path();
  if (!parent.empty()) {
    fs::create_directories(parent);
  }
}

void publish_no_replace(const fs::path& temporary, const fs::path& target) {
  if (::link(temporary.c_str(), target.c_str()) != 0) {
    const std::string message = std::strerror(errno);
    throw ConversionError("refusing to overwrite " + target.string() + ": " + message);
  }
  fs::remove(temporary);
}

void publish_replace(const fs::path& temporary, const fs::path& target) {
  std::error_code error;
  fs::rename(temporary, target, error);
  if (error) {
    throw ConversionError("cannot publish " + target.string() + ": " + error.message());
  }
}

struct ConversionPlan {
  HnswlibHeader header;
  std::uint64_t base_start;
  std::uint64_t source_upper_stride;
  std::vector<std::uint64_t> upper_offsets;
  std::vector<std::uint32_t> levels;
  std::vector<std::uint64_t> output_offsets;
  std::uint64_t output_bytes;
  std::uint32_t entry_uid;
};

ConversionPlan validate_and_plan(const MappedFile& input, const Options& options) {
  ConversionPlan plan {};
  plan.header = read_header(input);
  validate_header(plan.header, options);
  plan.base_start = kHnswlibHeaderBytes;
  plan.source_upper_stride = checked_add(
      4, checked_multiply(plan.header.max_m, 4, "hnswlib upper link storage"),
      "hnswlib upper link storage");
  if (plan.source_upper_stride > std::numeric_limits<std::uint32_t>::max()) {
    throw ConversionError("hnswlib upper-level stride exceeds serialized link-size range");
  }

  const std::uint64_t base_bytes = checked_multiply(
      plan.header.count, plan.header.size_data_per_element, "hnswlib level-0 storage");
  std::uint64_t upper_cursor = checked_add(plan.base_start, base_bytes, "hnswlib base region");
  input.require(plan.base_start, base_bytes, "hnswlib level-0 storage");

  plan.upper_offsets.resize(static_cast<std::size_t>(plan.header.count));
  plan.levels.resize(static_cast<std::size_t>(plan.header.count));
  std::uint32_t observed_max_level = 0;
  for (std::uint64_t internal_id = 0; internal_id < plan.header.count; ++internal_id) {
    const std::uint32_t link_size =
        input.read<std::uint32_t>(upper_cursor, "hnswlib upper link-list size");
    upper_cursor = checked_add(upper_cursor, 4, "hnswlib upper link-list size");
    if (link_size % plan.source_upper_stride != 0) {
      throw ConversionError("hnswlib upper link-list size is not a whole number of levels");
    }
    const std::uint64_t level64 = link_size / plan.source_upper_stride;
    if (level64 > std::numeric_limits<std::uint32_t>::max()) {
      throw ConversionError("hnswlib node level exceeds uint32");
    }
    const std::uint32_t level = static_cast<std::uint32_t>(level64);
    plan.upper_offsets[internal_id] = upper_cursor;
    plan.levels[internal_id] = level;
    observed_max_level = std::max(observed_max_level, level);
    input.require(upper_cursor, link_size, "hnswlib upper link lists");
    upper_cursor = checked_add(upper_cursor, link_size, "hnswlib upper link lists");
  }
  if (upper_cursor != input.size()) {
    throw ConversionError("hnswlib record walk does not end at the exact input file size");
  }
  if (static_cast<std::uint32_t>(plan.header.max_level) != observed_max_level ||
      plan.levels[plan.header.entry_internal_id] != observed_max_level) {
    throw ConversionError("hnswlib maximum level and entry-point level are inconsistent");
  }

  plan.output_offsets.resize(static_cast<std::size_t>(plan.header.count));
  std::uint64_t output_cursor = kGraphBeyondHeaderBytes;
  for (std::uint64_t internal_id = 0; internal_id < plan.header.count; ++internal_id) {
    plan.output_offsets[internal_id] = output_cursor;
    output_cursor = checked_add(
        output_cursor,
        graphbeyond_record_size(options.dim, plan.header.m, plan.levels[internal_id]),
        "GraphBeyond dump size");
    if (output_cursor >= kRemoteOffsetLimit) {
      throw ConversionError("GraphBeyond dump exceeds the 48-bit RemotePtr offset space");
    }
  }
  plan.output_bytes = output_cursor;

  std::unordered_set<std::uint32_t> labels;
  labels.reserve(static_cast<std::size_t>(plan.header.count));
  for (std::uint64_t internal_id = 0; internal_id < plan.header.count; ++internal_id) {
    const std::uint64_t base = checked_add(
        plan.base_start,
        checked_multiply(internal_id, plan.header.size_data_per_element,
                         "hnswlib element offset"),
        "hnswlib element offset");
    const std::uint64_t list0 = checked_add(base, plan.header.offset_level0, "level-0 list");
    const std::uint16_t count0 =
        list_count(input, list0, plan.header.max_m0, "hnswlib level-0 list");
    input.require(list0, 4, "hnswlib level-0 list header");
    if ((input.data()[list0 + 2] & 1U) != 0) {
      throw ConversionError("hnswlib source contains a deleted node");
    }
    validate_neighbor_ids(input, list0, count0, plan.header.count,
                          "hnswlib level-0 list");

    const std::uint64_t label64 = input.read<std::uint64_t>(
        checked_add(base, plan.header.label_offset, "label offset"), "hnswlib label");
    if (label64 > std::numeric_limits<std::uint32_t>::max()) {
      throw ConversionError("hnswlib label does not fit GraphBeyond's 32-bit uid");
    }
    const std::uint32_t label = static_cast<std::uint32_t>(label64);
    if (!labels.insert(label).second) {
      throw ConversionError("hnswlib source contains duplicate external labels");
    }
    if (internal_id == plan.header.entry_internal_id) {
      plan.entry_uid = label;
    }

    for (std::uint32_t level = 1; level <= plan.levels[internal_id]; ++level) {
      const std::uint64_t list_offset = checked_add(
          plan.upper_offsets[internal_id],
          checked_multiply(level - 1, plan.source_upper_stride, "upper-list offset"),
          "upper-list offset");
      const std::uint16_t count =
          list_count(input, list_offset, plan.header.max_m, "hnswlib upper list");
      validate_neighbor_ids(input, list_offset, count, plan.header.count,
                            "hnswlib upper list");
    }
  }
  return plan;
}

void write_neighbor_list(std::ofstream& output, const MappedFile& input,
                         const ConversionPlan& plan, std::uint64_t source_list_offset,
                         std::uint64_t capacity) {
  const std::uint16_t source_count =
      list_count(input, source_list_offset, capacity, "source neighbor list");
  const std::uint32_t output_count = source_count;
  write_pod(output, output_count);
  for (std::uint64_t index = 0; index < capacity; ++index) {
    std::uint64_t pointer = 0;
    if (index < source_count) {
      const std::uint32_t neighbor = input.read<std::uint32_t>(
          source_list_offset + 4 + index * 4, "source neighbor id");
      pointer = plan.output_offsets[neighbor];
    }
    write_pod(output, pointer);
  }
}

void write_converted_dump(const fs::path& path, const MappedFile& input,
                          const ConversionPlan& plan, const Options& options) {
  std::ofstream output(path, std::ios::binary | std::ios::trunc);
  if (!output) {
    throw ConversionError("cannot create temporary output: " + path.string());
  }
  const std::uint64_t entry_pointer = plan.output_offsets[plan.header.entry_internal_id];
  write_pod(output, plan.output_bytes);
  write_pod(output, entry_pointer);

  for (std::uint64_t internal_id = 0; internal_id < plan.header.count; ++internal_id) {
    const std::uint64_t record_start = plan.output_offsets[internal_id];
    const std::uint64_t base = plan.base_start + internal_id * plan.header.size_data_per_element;
    const std::uint64_t header =
        internal_id == plan.header.entry_internal_id ? kEntryNodeBit : 0;
    const std::uint64_t label64 =
        input.read<std::uint64_t>(base + plan.header.label_offset, "hnswlib label");
    const std::uint32_t uid = static_cast<std::uint32_t>(label64);
    const std::uint32_t level = plan.levels[internal_id];
    write_pod(output, header);
    write_pod(output, uid);
    write_pod(output, level);

    const std::uint64_t vector_bytes = options.dim * 4;
    input.require(base + plan.header.data_offset, vector_bytes, "hnswlib vector");
    output.write(reinterpret_cast<const char*>(input.data() + base + plan.header.data_offset),
                 static_cast<std::streamsize>(vector_bytes));
    if (!output) {
      throw ConversionError("failed while writing converted vectors");
    }

    write_neighbor_list(output, input, plan, base + plan.header.offset_level0,
                        plan.header.max_m0);
    for (std::uint32_t current_level = 1; current_level <= level; ++current_level) {
      const std::uint64_t source_list =
          plan.upper_offsets[internal_id] +
          static_cast<std::uint64_t>(current_level - 1) * plan.source_upper_stride;
      write_neighbor_list(output, input, plan, source_list, plan.header.max_m);
    }

    const std::uint64_t record_end = record_start +
        graphbeyond_record_size(options.dim, plan.header.m, level);
    const std::streamoff written = output.tellp();
    if (written < 0 || static_cast<std::uint64_t>(written) > record_end) {
      throw ConversionError("converted record exceeded its planned size");
    }
    write_zeroes(output, record_end - static_cast<std::uint64_t>(written));
  }
  output.flush();
  if (!output) {
    throw ConversionError("failed while flushing converted dump");
  }
  output.close();
  if (fs::file_size(path) != plan.output_bytes) {
    throw ConversionError("converted dump size does not match its free_ptr");
  }
}

void validate_output_neighbor_list(const MappedFile& output, const MappedFile& input,
                                   const ConversionPlan& plan,
                                   std::uint64_t output_list_offset,
                                   std::uint64_t source_list_offset,
                                   std::uint64_t capacity) {
  const std::uint16_t source_count =
      list_count(input, source_list_offset, capacity, "source validation list");
  const std::uint32_t output_count =
      output.read<std::uint32_t>(output_list_offset, "output neighbor count");
  if (output_count != source_count) {
    throw ConversionError("post-write validation found a neighbor-count mismatch");
  }
  for (std::uint64_t index = 0; index < capacity; ++index) {
    const std::uint64_t actual = output.read<std::uint64_t>(
        output_list_offset + 4 + index * 8, "output neighbor pointer");
    std::uint64_t expected = 0;
    if (index < source_count) {
      const std::uint32_t neighbor = input.read<std::uint32_t>(
          source_list_offset + 4 + index * 4, "source neighbor id");
      expected = plan.output_offsets[neighbor];
    }
    if (actual != expected) {
      throw ConversionError("post-write validation found a neighbor-pointer mismatch");
    }
  }
}

void validate_converted_dump(const fs::path& path, const MappedFile& input,
                             const ConversionPlan& plan, const Options& options) {
  const MappedFile output(path);
  if (output.size() != plan.output_bytes ||
      output.read<std::uint64_t>(0, "GraphBeyond free_ptr") != plan.output_bytes ||
      output.read<std::uint64_t>(8, "GraphBeyond entry pointer") !=
          plan.output_offsets[plan.header.entry_internal_id]) {
    throw ConversionError("post-write validation found an invalid dump header");
  }

  for (std::uint64_t internal_id = 0; internal_id < plan.header.count; ++internal_id) {
    const std::uint64_t record = plan.output_offsets[internal_id];
    const std::uint64_t source_base =
        plan.base_start + internal_id * plan.header.size_data_per_element;
    const std::uint64_t expected_header =
        internal_id == plan.header.entry_internal_id ? kEntryNodeBit : 0;
    const std::uint64_t source_label =
        input.read<std::uint64_t>(source_base + plan.header.label_offset, "source label");
    if (output.read<std::uint64_t>(record, "output node header") != expected_header ||
        output.read<std::uint32_t>(record + 8, "output uid") != source_label ||
        output.read<std::uint32_t>(record + 12, "output level") != plan.levels[internal_id]) {
      throw ConversionError("post-write validation found a node-metadata mismatch");
    }

    const std::uint64_t vector_bytes = options.dim * 4;
    output.require(record + 16, vector_bytes, "output vector");
    input.require(source_base + plan.header.data_offset, vector_bytes, "source vector");
    if (std::memcmp(output.data() + record + 16,
                    input.data() + source_base + plan.header.data_offset,
                    static_cast<std::size_t>(vector_bytes)) != 0) {
      throw ConversionError("post-write validation found a vector mismatch");
    }

    std::uint64_t output_list = record + 16 + vector_bytes;
    validate_output_neighbor_list(output, input, plan, output_list,
                                  source_base + plan.header.offset_level0,
                                  plan.header.max_m0);
    output_list += 4 + plan.header.max_m0 * 8;
    for (std::uint32_t level = 1; level <= plan.levels[internal_id]; ++level) {
      const std::uint64_t source_list =
          plan.upper_offsets[internal_id] +
          static_cast<std::uint64_t>(level - 1) * plan.source_upper_stride;
      validate_output_neighbor_list(output, input, plan, output_list, source_list,
                                    plan.header.max_m);
      output_list += 4 + plan.header.max_m * 8;
    }

    const std::uint64_t record_end =
        record + graphbeyond_record_size(options.dim, plan.header.m,
                                         plan.levels[internal_id]);
    if (output_list > record_end) {
      throw ConversionError("post-write validation found an oversized record");
    }
    for (std::uint64_t offset = output_list; offset < record_end; ++offset) {
      if (output.data()[offset] != 0) {
        throw ConversionError("post-write validation found nonzero alignment padding");
      }
    }
    const std::uint64_t expected_next =
        internal_id + 1 < plan.header.count ? plan.output_offsets[internal_id + 1]
                                            : plan.output_bytes;
    if (record_end != expected_next) {
      throw ConversionError("post-write validation found a record-boundary mismatch");
    }
  }
}

void write_manifest(const fs::path& path, const Options& options,
                    const ConversionPlan& plan, std::uint64_t source_bytes,
                    const std::string& source_sha, const std::string& output_sha) {
  std::ofstream output(path, std::ios::trunc);
  if (!output) {
    throw ConversionError("cannot create temporary manifest: " + path.string());
  }
  output << "{\n"
         << "  \"converter\": \"convert_hnswlib_dump-v1\",\n"
         << "  \"source_format\": \"hnswlib-0.8.0-native-64le\",\n"
         << "  \"format\": \"graphbeyond-hnsw-single-mn-v1\",\n"
         << "  \"source_path\": \"" << json_escape(fs::absolute(options.input).string())
         << "\",\n"
         << "  \"output_path\": \"" << json_escape(fs::absolute(options.output).string())
         << "\",\n"
         << "  \"source_sha256\": \"" << source_sha << "\",\n"
         << "  \"output_sha256\": \"" << output_sha << "\",\n"
         << "  \"source_bytes\": " << source_bytes << ",\n"
         << "  \"output_bytes\": " << plan.output_bytes << ",\n"
         << "  \"count\": " << plan.header.count << ",\n"
         << "  \"dim\": " << options.dim << ",\n"
         << "  \"m\": " << plan.header.m << ",\n"
         << "  \"max_m0\": " << plan.header.max_m0 << ",\n"
         << "  \"ef_construction\": " << plan.header.ef_construction << ",\n"
         << "  \"max_level\": " << plan.header.max_level << ",\n"
         << "  \"entry_internal_id\": " << plan.header.entry_internal_id << ",\n"
         << "  \"entry_uid\": " << plan.entry_uid << ",\n"
         << "  \"graph_preserved\": true,\n"
         << "  \"post_write_validation\": \"full_graph_payload_and_pointers\",\n"
         << "  \"deleted_nodes_accepted\": false\n"
         << "}\n";
  output.flush();
  if (!output) {
    throw ConversionError("failed while writing conversion manifest");
  }
}

void convert(const Options& options) {
  if (std::endian::native != std::endian::little || sizeof(std::size_t) != 8) {
    throw ConversionError("converter requires a little-endian 64-bit host");
  }
  if (!options.force && (fs::exists(options.output) || fs::exists(options.manifest))) {
    throw ConversionError("refusing to overwrite existing output or manifest without --force");
  }
  ensure_parent(options.output);
  ensure_parent(options.manifest);
  const fs::path temporary_output = temporary_path_for(options.output);
  const fs::path temporary_manifest = temporary_path_for(options.manifest);

  try {
    const MappedFile input(options.input);
    const ConversionPlan plan = validate_and_plan(input, options);
    const std::string source_sha = sha256_bytes(input.data(), input.size());
    write_converted_dump(temporary_output, input, plan, options);
    validate_converted_dump(temporary_output, input, plan, options);
    const std::string output_sha = sha256_file(temporary_output);
    write_manifest(temporary_manifest, options, plan, input.size(), source_sha, output_sha);

    if (options.force) {
      publish_replace(temporary_output, options.output);
      publish_replace(temporary_manifest, options.manifest);
    } else {
      publish_no_replace(temporary_output, options.output);
      try {
        publish_no_replace(temporary_manifest, options.manifest);
      } catch (...) {
        fs::remove(options.output);
        throw;
      }
    }

    std::cout << "converted count=" << plan.header.count << " dim=" << options.dim
              << " M=" << plan.header.m << " max_level=" << plan.header.max_level
              << " output_bytes=" << plan.output_bytes << " output=" << options.output
              << '\n';
  } catch (...) {
    std::error_code ignored;
    fs::remove(temporary_output, ignored);
    fs::remove(temporary_manifest, ignored);
    throw;
  }
}

}  // namespace

int main(int argc, char** argv) {
  try {
    convert(parse_options(argc, argv));
    return 0;
  } catch (const std::exception& error) {
    std::cerr << "error: " << error.what() << '\n';
    return 1;
  }
}
