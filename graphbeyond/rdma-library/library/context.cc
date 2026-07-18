#include "context.hh"

#include <arpa/inet.h>
#include <sys/socket.h>
#include <unistd.h>

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <iostream>

#include "queue_pair.hh"
#include "utils.hh"

Context::Context(Configuration& config,
                 const i32 device_idx,
                 bool create_shared_rcq)
    : config_(config) {
  i32 num_devices = 0;
  IBDeviceList device_list = ibv_get_device_list(&num_devices);

  lib_assert(num_devices > 0, "No InfiniBand devices found");
  lib_assert(device_list != nullptr, "Device list is null");

  // Allow operator override via GB_RDMA_DEV=<device-name> (e.g. "mlx5_1").
  // This pins the HCA before the auto-active-port scan below, which is needed
  // on clusters where multiple HCAs report ACTIVE but only one is wired to the
  // experiment fabric (the auto-pick would otherwise land on the wrong NIC).
  const char* gb_dev = std::getenv("GB_RDMA_DEV");
  i32 forced_dev_idx = -1;
  if (gb_dev != nullptr) {
    for (i32 j = 0; j < num_devices; ++j) {
      const char* dn = ibv_get_device_name(device_list[j]);
      if (dn && strcmp(dn, gb_dev) == 0) {
        forced_dev_idx = j;
        fprintf(stderr, "[GB_RDMA_DEV] forcing device %s (idx=%d)\n", dn, j);
        fflush(stderr);
        break;
      }
    }
    if (forced_dev_idx < 0) {
      fprintf(stderr,
              "[GB_RDMA_DEV] requested device '%s' not found among %d "
              "ibv devices; falling back to auto-select\n",
              gb_dev, num_devices);
      fflush(stderr);
    }
  }

  // Auto-select the first device whose configured port is ACTIVE. If
  // device_idx is explicitly non-zero we honor that. This is needed on the
  // SKV cluster where mlx5_0 is DOWN and mlx5_1 is the active RoCE port.
  // GB_RDMA_DEV (resolved above) takes precedence over the auto-scan.
  i32 selected_idx = device_idx;
  if (forced_dev_idx >= 0) {
    selected_idx = forced_dev_idx;
  } else if (selected_idx == 0) {
    for (i32 i = 0; i < num_devices; ++i) {
      ibv_context* probe = ibv_open_device(device_list[i]);
      if (!probe) continue;
      ibv_port_attr attr{};
      if (ibv_query_port(probe, config_.device_port, &attr) == 0
          && attr.state == IBV_PORT_ACTIVE) {
        selected_idx = i;
        ibv_close_device(probe);
        break;
      }
      ibv_close_device(probe);
    }
  }
  lib_assert(0 <= selected_idx && selected_idx < num_devices,
             "Device " + std::to_string(selected_idx) + " not found");

  device_ = device_list[selected_idx];

  std::cerr << num_devices << " device(s) found" << std::endl;
  std::cerr << "Selected device: " << ibv_get_device_name(device_)
            << " (idx=" << selected_idx << ")" << std::endl;

  context_ = ibv_open_device(device_);
  lib_assert(device_ && context_, "Cannot open device");

  // allocate protection domain
  protection_domain_ = ibv_alloc_pd(context_);

  // query port
  lib_assert(
    ibv_query_port(context_, config_.device_port, &port_attributes_) == 0,
    "Cannot query port " + std::to_string(config_.device_port));

  // RoCE: query the GID for global routing. On Ethernet link-layer, LID is 0
  // and QPs must transition to RTR with is_global=1 + dgid set. We need to
  // pick an IPv4-mapped RoCE v2 GID (the one our cluster's Ethernet routing
  // actually uses), not the link-local IPv6 one which would have the same
  // RoCE v2 type but get dropped on the wire.
  if (port_attributes_.link_layer == IBV_LINK_LAYER_ETHERNET) {
    // Scan all GID indices for one that is RoCE v2 + IPv4-mapped
    // (raw bytes [0..10] = 0, bytes [10..12] = 0xFF 0xFF). Fall back to the
    // first non-zero GID if no IPv4 entry exists.
    auto is_ipv4_mapped = [](const ibv_gid& g) {
      for (int i = 0; i < 10; ++i) {
        if (g.raw[i] != 0) return false;
      }
      return g.raw[10] == 0xFF && g.raw[11] == 0xFF;
    };

    gid_index_ = 0;
    bool found = false;
    // Note: the unused ibv_gid_entry probe array was removed because the older
    // MLNX_OFED 4.7 headers shipped on CloudLab Utah amd nodes do not declare
    // ibv_gid_entry. We only call ibv_query_gid (legacy API) below.
    // GIDs are sparse; iterate up to 16 indices and pick the first IPv4 RoCE v2
    for (u8 idx = 0; idx < 16; ++idx) {
      ibv_gid probe{};
      if (ibv_query_gid(context_, config_.device_port, idx, &probe) != 0) {
        continue;
      }
      // Skip the all-zero "empty" entries
      bool all_zero = true;
      for (int i = 0; i < 16; ++i) if (probe.raw[i] != 0) { all_zero = false; break; }
      if (all_zero) continue;
      if (is_ipv4_mapped(probe)) {
        gid_ = probe;
        gid_index_ = idx;
        found = true;
        // Prefer the higher-numbered IPv4 GID (RoCE v2 over v1 on Mellanox).
        // The kernel reports v1 at lower index, v2 at higher.
      }
    }
    lib_assert(found,
               "No IPv4-mapped GID found on device "
                 + str{ibv_get_device_name(device_)}
                 + " (cluster is not RoCE-over-IPv4?)");

    // Allow operator override via GB_GID_IDX=<n>. Some clusters (e.g. CloudLab
    // Utah amd nodes) expose multiple IPv4 RoCE v2 GIDs on different VLANs /
    // bond subinterfaces, and the auto-pick (highest-index IPv4) may land on
    // the wrong subnet (e.g. control plane instead of the 10.10.1.x experiment
    // network). Setting GB_GID_IDX forces a specific sgid_index.
    if (const char* env_gid = std::getenv("GB_GID_IDX")) {
      int forced = std::atoi(env_gid);
      lib_assert(forced >= 0 && forced < 16,
                 "GB_GID_IDX out of range [0,16): " + str{env_gid});
      ibv_gid probe{};
      lib_assert(ibv_query_gid(context_, config_.device_port,
                               static_cast<u8>(forced), &probe) == 0,
                 "GB_GID_IDX=" + str{env_gid} + " not queryable on device "
                   + str{ibv_get_device_name(device_)});
      gid_ = probe;
      gid_index_ = static_cast<u8>(forced);
      std::cerr << "[GB_GID_IDX] overriding RoCE GID index -> "
                << forced << std::endl;
    }

    char addr[64];
    snprintf(addr, sizeof(addr), "%d.%d.%d.%d",
             gid_.raw[12], gid_.raw[13], gid_.raw[14], gid_.raw[15]);
    std::cerr << "Link layer: Ethernet (RoCE), using GID index "
              << static_cast<int>(gid_index_)
              << " (local IPv4 " << addr << ")" << std::endl;
  } else {
    std::cerr << "Link layer: InfiniBand, using LID " << port_attributes_.lid
              << std::endl;
  }

  // create completion queues
  send_cq_ =
    ibv_create_cq(context_, config_.max_send_queue_wr, nullptr, nullptr, 0);
  receive_cq_ =
    ibv_create_cq(context_, config_.max_recv_queue_wr, nullptr, nullptr, 0);

  lib_assert(send_cq_ && receive_cq_, "Cannot create completion queues");

  if (create_shared_rcq) {
    ibv_srq_init_attr attributes{};
    attributes.srq_context = context_;
    attributes.attr.max_wr = config_.max_recv_queue_wr;
    attributes.attr.max_sge = 1;
    shared_receive_cq_ = ibv_create_srq(protection_domain_, &attributes);

    lib_assert(shared_receive_cq_,
               "Cannot create shared receive completion queue");
  }

  ibv_free_device_list(device_list);
}

Context::~Context() {
  lib_assert(!shared_receive_cq_ || ibv_destroy_srq(shared_receive_cq_) == 0,
             "Cannot destroy shared receive completion queue");
  lib_assert(ibv_destroy_cq(receive_cq_) == 0,
             "Cannot destroy receive completion queue");
  lib_assert(ibv_destroy_cq(send_cq_) == 0,
             "Cannot destroy send completion queue");
  lib_assert(ibv_dealloc_pd(protection_domain_) == 0,
             "Cannot deallocate protection domain");
  lib_assert(ibv_close_device(context_) == 0, "Cannot close device.");

  close_server_socket();
}

void Context::bind_to_port(u32 tcp_port) {
  server_socket_ = socket(AF_INET, SOCK_STREAM, 0);
  lib_assert(server_socket_ >= 0, "Cannot open socket.");

  sockaddr_in address{};
  address.sin_family = AF_INET;
  address.sin_port = htons(tcp_port);

  // activate reuse address option
  i32 option_val = 1;
  lib_assert(setsockopt(server_socket_,
                        SOL_SOCKET,
                        SO_REUSEADDR,
                        &option_val,
                        sizeof(option_val)) == 0,
             "Cannot set socket option to reuse address");

  lib_assert(
    bind(server_socket_, (sockaddr*)&address, sizeof(sockaddr_in)) == 0,
    "Cannot bind to port " + std::to_string(tcp_port));

  lib_assert(listen(server_socket_, 128) == 0, "Cannot listen on socket");
}

void Context::close_server_socket() const {
  if (server_socket_ >= 0) {
    close(server_socket_);
  }
}

std::pair<QP, u32> Context::wait_for_connection() {
  QP queue_pair = std::make_unique<QueuePair>(this);

  QPInfo receive_buffer{}, send_buffer{};
  send_buffer.lid = get_lid();
  send_buffer.qp_number = queue_pair->get_qp_num();
  send_buffer.gid = gid_;
  send_buffer.gid_index = gid_index_;
  ssize_t qp_size = sizeof(QPInfo);

  i32 tcp_socket = accept(server_socket_, nullptr, nullptr);
  lib_assert(tcp_socket >= 0, "Cannot open socket.");

  lib_debug("Exchange QP information with client");
  lib_assert(recv(tcp_socket, &receive_buffer, qp_size, 0) == qp_size,
             "Received an incorrect number of bytes");
  lib_assert(send(tcp_socket, &send_buffer, qp_size, 0) == qp_size,
             "Transmitted an incorrect number of bytes");

  std::cerr << "pairing: " << queue_pair->get_qp_num() << " -- "
            << receive_buffer.qp_number << std::endl;

  queue_pair->transition_to_rtr(receive_buffer);
  queue_pair->transition_to_rts();

  // TODO: set remote user data

  close(tcp_socket);

  return {std::move(queue_pair), receive_buffer.node_id};
}

QP Context::connect_to_server(const str& address, u32 tcp_port, u32 node_id) {
  QP queue_pair = std::make_unique<QueuePair>(this);

  QPInfo send_buffer{}, receive_buffer{};
  send_buffer.lid = get_lid();
  send_buffer.qp_number = queue_pair->get_qp_num();
  send_buffer.node_id = node_id;
  send_buffer.gid = gid_;
  send_buffer.gid_index = gid_index_;
  ssize_t qp_size = sizeof(QPInfo);

  sockaddr_in remote_address{};
  remote_address.sin_family = AF_INET;
  remote_address.sin_port = htons(tcp_port);
  inet_pton(AF_INET, address.c_str(), &(remote_address.sin_addr));

  i32 tcp_socket = socket(AF_INET, SOCK_STREAM, 0);
  lib_assert(tcp_socket >= 0, "Cannot open socket.");

  lib_debug("Connect to server with address " + address);
  while (connect(tcp_socket, (sockaddr*)&remote_address, sizeof(sockaddr_in)) !=
         0) {
    // wait until server opens a connection
  }

  lib_debug("Exchange QP information with server");
  lib_assert(send(tcp_socket, &send_buffer, qp_size, 0) == qp_size,
             "Transmitted an incorrect number of bytes");
  lib_assert(recv(tcp_socket, &receive_buffer, qp_size, 0) == qp_size,
             "Received an incorrect number of bytes");

  std::cerr << "pairing: " << queue_pair->get_qp_num() << " -- "
            << receive_buffer.qp_number << std::endl;

  queue_pair->transition_to_rtr(receive_buffer);
  queue_pair->transition_to_rts();
  close(tcp_socket);

  return queue_pair;
}

void Context::post_shared_receive(MemoryRegion& region) {
  ibv_recv_wr work_request{};
  ibv_sge scatter_gather_entry{};
  ibv_recv_wr* bad_work_request{nullptr};

  lib_assert(shared_receive_cq_, "No shared receive CQ exists");

  scatter_gather_entry.addr = region.get_address();
  scatter_gather_entry.length = region.get_size_in_bytes();
  scatter_gather_entry.lkey = region.get_lkey();

  work_request.wr_id = reinterpret_cast<u64>(&region);
  work_request.next = nullptr;
  work_request.sg_list = &scatter_gather_entry;
  work_request.num_sge = 1;

  lib_assert(ibv_post_srq_recv(
               get_shared_receive_cq(), &work_request, &bad_work_request) == 0,
             "Cannot post shared receive request");
  lib_debug("Shared receive request successfully posted");
}

// static function
i32 Context::poll_recv_cq(ibv_wc* work_completion,
                          const i32 max_cqes,
                          ibv_cq* recv_cq,
                          ReceiveInfo* recv_info) {
  // caution: work_completion and recv_info must be arrays of size max_cqes
  i32 num_entries = ibv_poll_cq(recv_cq, max_cqes, work_completion);

  if (num_entries > 0) {
    // verify completion status
    for (i32 i = 0; i < num_entries; ++i) {
      if (work_completion[i].status != IBV_WC_SUCCESS) {
        fprintf(stderr,
                "[RDMA-WC-ERR][recv] status=%d (%s) vendor_err=0x%x opcode=%d "
                "wr_id=%lu byte_len=%u qp_num=%u\n",
                work_completion[i].status,
                ibv_wc_status_str(work_completion[i].status),
                work_completion[i].vendor_err,
                static_cast<int>(work_completion[i].opcode),
                static_cast<unsigned long>(work_completion[i].wr_id),
                work_completion[i].byte_len,
                work_completion[i].qp_num);
        fflush(stderr);
      }
      lib_assert(work_completion[i].status == IBV_WC_SUCCESS,
                 "Receive request failed");
      lib_debug("Receive request completed");

      if (recv_info && work_completion[i].opcode == IBV_WC_RECV) {
        recv_info[i].mr =
          reinterpret_cast<MemoryRegion*>(work_completion[i].wr_id);
        recv_info[i].bytes_written = work_completion[i].byte_len;
      }
    }

  } else if (num_entries < 0) {
    lib_failure("Cannot poll receive completion queue");
  }

  return num_entries;
}

i32 Context::poll_recv_cq(ibv_wc* work_completion,
                          const i32 max_cqes,
                          ReceiveInfo* recv_info) {
  lib_assert(max_cqes <= config_.max_recv_queue_wr,
             "expected number of WCs exceeds number of max WRs");

  return poll_recv_cq(work_completion, max_cqes, receive_cq_, recv_info);
}

ReceiveInfo Context::receive() {
  ibv_wc work_completion{};
  ReceiveInfo recv_info{};
  i32 num_entries;

  do {
    num_entries = poll_recv_cq(&work_completion, 1, &recv_info);
  } while (num_entries == 0);

  return recv_info;
}

// receive exactly n completion events
void Context::receive(i32 n) {
  vec<ibv_wc> work_completions(n);
  i32 num_entries = 0;

  do {
    num_entries += poll_recv_cq(work_completions.data(), n);
  } while (num_entries < n);
}

// static function
i32 Context::poll_send_cq(ibv_wc* work_completion,
                          const i32 max_cqes,
                          ibv_cq* send_cq,
                          const func<void(u64)>& id_handler) {
  // caution: work_completion must be an array of size max_cqes
  i32 num_entries = ibv_poll_cq(send_cq, max_cqes, work_completion);

  if (num_entries > 0) {
    // verify completion status
    for (i32 i = 0; i < num_entries; ++i) {
      if (work_completion[i].status != IBV_WC_SUCCESS) {
        fprintf(stderr,
                "[RDMA-WC-ERR][send] status=%d (%s) vendor_err=0x%x opcode=%d "
                "wr_id=%lu byte_len=%u qp_num=%u\n",
                work_completion[i].status,
                ibv_wc_status_str(work_completion[i].status),
                work_completion[i].vendor_err,
                static_cast<int>(work_completion[i].opcode),
                static_cast<unsigned long>(work_completion[i].wr_id),
                work_completion[i].byte_len,
                work_completion[i].qp_num);
        fflush(stderr);
      }
      lib_assert(work_completion[i].status == IBV_WC_SUCCESS,
                 "Send request failed");

      id_handler(work_completion[i].wr_id);
    }
    lib_debug("Send request completed");

  } else if (num_entries < 0) {
    lib_failure("Cannot poll completion queue");
  }

  return num_entries;
}

// static function
i32 Context::poll_send_cq(ibv_wc* work_completion,
                          const i32 max_cqes,
                          ibv_cq* send_cq) {
  return poll_send_cq(work_completion, max_cqes, send_cq, [](u64) {});
}

i32 Context::poll_send_cq(ibv_wc* work_completion, const i32 max_cqes) {
  lib_assert(max_cqes <= config_.max_send_queue_wr,
             "expected number of WCs exceeds number of max WRs");

  return poll_send_cq(work_completion, max_cqes, send_cq_);
}

i32 Context::poll_send_cq_until_completion() {
  ibv_wc work_completion{};
  i32 num_entries;

  do {
    num_entries = poll_send_cq(&work_completion, 1);
  } while (num_entries == 0);

  return num_entries;
}

// poll completion until we get exactly n completion events
void Context::poll_send_cq_until_completion(i32 n) {
  vec<ibv_wc> work_completions(n);
  i32 num_entries = 0;

  do {
    num_entries += poll_send_cq(work_completions.data(), n);
  } while (num_entries < n);
}
