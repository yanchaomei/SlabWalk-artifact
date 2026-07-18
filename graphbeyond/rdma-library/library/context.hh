#ifndef RDMA_LIBRARY_CONTEXT_HH
#define RDMA_LIBRARY_CONTEXT_HH

#include <infiniband/verbs.h>

#include <utility>

#include "configuration.hh"
#include "memory_region.hh"
#include "queue_pair.hh"

// forward declarations
class MemoryRegion;
struct QPInfo;
class QueuePair;

struct ReceiveInfo {
  MemoryRegion* mr{nullptr};
  u32 bytes_written{};
};

class Context {
public:
  using IBDeviceList = ibv_device**;
  using Configuration = configuration::Configuration;

public:
  explicit Context(Configuration& config,
                   i32 device_idx = 0,
                   bool create_shared_rcq = false);

  ~Context();
  Context(const Context&) = delete;
  Context& operator=(const Context&) = delete;

  ibv_context* get_raw_context() { return context_; }
  ibv_pd* get_protection_domain() { return protection_domain_; }
  ibv_cq* get_send_cq() { return send_cq_; }
  ibv_cq* get_receive_cq() { return receive_cq_; }
  ibv_srq* get_shared_receive_cq() { return shared_receive_cq_; }
  Configuration& get_config() const { return config_; }
  u16 get_lid() const { return port_attributes_.lid; }
  // RoCE additions: GID for global routing when LID is 0 (Ethernet link layer)
  const ibv_gid& get_gid() const { return gid_; }
  u8 get_gid_index() const { return gid_index_; }
  bool is_roce() const { return port_attributes_.link_layer == IBV_LINK_LAYER_ETHERNET; }

  void bind_to_port(u32 tcp_port);
  void close_server_socket() const;
  std::pair<u_ptr<QueuePair>, u32> wait_for_connection();
  u_ptr<QueuePair> connect_to_server(const str& address,
                                     u32 tcp_port,
                                     u32 node_id = 0);

  void post_shared_receive(MemoryRegion& region);

  static i32 poll_recv_cq(ibv_wc* work_completion,
                          i32 max_cqes,
                          ibv_cq* recv_cq,
                          ReceiveInfo* recv_info = nullptr);
  i32 poll_recv_cq(ibv_wc* work_completion,
                   i32 max_cqes,
                   ReceiveInfo* recv_info = nullptr);
  ReceiveInfo receive();
  void receive(i32 n);
  static i32 poll_send_cq(ibv_wc* work_completion,
                          i32 max_cqes,
                          ibv_cq* send_cq,
                          const func<void(u64)>& id_handler);
  static i32 poll_send_cq(ibv_wc* work_completion,
                          i32 max_cqes,
                          ibv_cq* send_cq);
  i32 poll_send_cq(ibv_wc* work_completion, i32 max_cqes);
  i32 poll_send_cq_until_completion();
  void poll_send_cq_until_completion(i32 n);

private:
  Configuration& config_;
  i32 server_socket_{-1};

  ibv_device* device_{nullptr};
  ibv_context* context_{nullptr};
  ibv_pd* protection_domain_{nullptr};

  // completion queues
  ibv_cq* send_cq_{nullptr};
  ibv_cq* receive_cq_{nullptr};
  ibv_srq* shared_receive_cq_{nullptr};

  ibv_port_attr port_attributes_{};

  // RoCE additions
  ibv_gid gid_{};
  u8 gid_index_{0};
};

#endif  // RDMA_LIBRARY_CONTEXT_HH
