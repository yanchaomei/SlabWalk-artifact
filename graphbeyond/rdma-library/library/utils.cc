#include "utils.hh"

#include <cmath>
#include <cstddef>
#include <map>

void lib_failure(const str&& message) {
  std::cerr << "[ERROR]: " << message << std::endl;
  std::exit(EXIT_FAILURE);
}

// Returns true if `s` looks like a dotted-quad IPv4 literal (e.g., "10.10.1.1"):
// exactly three '.' separators with each of the four segments being 1-3 digits.
// Does not validate that each octet is <= 255 — purely a syntactic bypass so
// callers can pass raw IPs through get_ip() without registering an alias.
static bool is_dotted_quad(const str& s) {
  int dots = 0;
  std::size_t seg_len = 0;
  for (char c : s) {
    if (c == '.') {
      if (seg_len == 0 || seg_len > 3) return false;
      ++dots;
      seg_len = 0;
    } else if (c >= '0' && c <= '9') {
      ++seg_len;
    } else {
      return false;
    }
  }
  if (seg_len == 0 || seg_len > 3) return false;
  return dots == 3;
}

std::string get_ip(const str& node_name) {
  // Bypass: if caller passes a dotted-quad IPv4 literal verbatim, return it
  // unchanged. Lets configs hard-code IPs without touching this table.
  if (is_dotted_quad(node_name)) {
    return node_name;
  }

  std::map<str, str> node_to_ip{
    {"localhost", "127.0.0.1"},
    // SKV cluster RDMA network (100Gbps ConnectX-6 DX, 10.0.0.x)
    {"skv-node1", "10.0.0.61"},
    {"skv-node2", "10.0.0.62"},
    {"skv-node3", "10.0.0.63"},
    {"skv-node4", "10.0.0.64"},
    {"skv-node5", "10.0.0.65"},
    {"skv-node6", "10.0.0.66"},
    {"skv-node7", "10.0.0.67"},
    // Backward-compat aliases (original cluster names mapped to SKV nodes)
    {"cluster11", "10.0.0.61"},
    {"cluster12", "10.0.0.62"},
    {"cluster13", "10.0.0.63"},
    {"cluster14", "10.0.0.64"},
    {"cluster15", "10.0.0.65"},
    {"cluster16", "10.0.0.66"},
    {"cluster17", "10.0.0.67"},
    // CloudLab amd cluster RDMA subnet (10.10.1.0/24)
    {"amd0", "10.10.1.1"},
    {"amd1", "10.10.1.2"},
    {"amd2", "10.10.1.3"},
    {"amd3", "10.10.1.4"},
    {"amd4", "10.10.1.5"},
    {"amd5", "10.10.1.6"},
    {"amd6", "10.10.1.7"},
    {"amd7", "10.10.1.8"},
    {"amd8", "10.10.1.9"},
    {"amd9", "10.10.1.10"},
    {"amd10", "10.10.1.11"},
    {"amd11", "10.10.1.12"},
    // nodeN aliases for the amd cluster (CloudLab profile name)
    {"node0", "10.10.1.1"},
    {"node1", "10.10.1.2"},
    {"node2", "10.10.1.3"},
    {"node3", "10.10.1.4"},
    {"node4", "10.10.1.5"},
    {"node5", "10.10.1.6"},
    {"node6", "10.10.1.7"},
    {"node7", "10.10.1.8"},
    {"node8", "10.10.1.9"},
    {"node9", "10.10.1.10"},
    {"node10", "10.10.1.11"},
    {"node11", "10.10.1.12"},
  };

  lib_assert(node_to_ip.find(node_name) != node_to_ip.end(),
             "Invalid node name: " + node_name);

  return node_to_ip[node_name];
}

f64 compute_throughput(i32 message_size,
                       i32 repeats,
                       Timepoint start,
                       Timepoint end) {
  return message_size / (ToSeconds(end - start).count() / repeats) /
         std::pow(1000, 2);
}

f64 compute_latency(i32 repeats,
                    Timepoint start,
                    Timepoint end,
                    bool is_read_or_atomic) {
  i32 rtt_factor = is_read_or_atomic ? 1 : 2;
  return ToMicroSeconds(end - start).count() / repeats / rtt_factor;
}

void print_status(str&& status) {
  std::cerr << "[STATUS]: " << status << std::endl;
}