// ============================================================================
// extfraginfo.c —— 内存碎片检测的【支线】eBPF 程序（仅 -s 模式加载）
// ----------------------------------------------------------------------------
// 和主线 fraginfo.c 的根本区别：
//   主线用 kprobe 全量扫 zone×order，回答"现在有多碎"；
//   本支线用 tracepoint 只在【真实碎片事件】发生时触发，回答"是谁在制造碎片"。
// 触发点 mm_page_alloc_extfrag：内核被迫跨 migratetype "偷"别人的块时打的埋点，
//   这是外碎片化最直接的证据。按进程(PID)聚合计数，写入 counts_map。
// ============================================================================

#include <linux/gfp.h>
#include <linux/mm.h>
#include <linux/sched.h>
#include <uapi/linux/ptrace.h>

// ---- 一条记录 = 某个进程触发 fallback 的聚合信息 ----
struct data_t {
  u64 pfn;             //  最近一次 fallback 的页帧号
  int alloc_order;     // 进程想要的 order
  int fallback_order;  // 实际借到的 order（比 alloc_order 大）
  pid_t pid;           // 进程 PID
  u64 count;           // ★ 该进程累计 fallback 次数
  char pcomm[32];      //  进程名
};

// ============ BPF map ============
//这个"查有没有、有就更新"的操作，哈希表是 O(1)，非常合适
//得遍历整个数组找 PID，是 O(n)。而且 PID 的值可以很大（Linux 默认最大 32768），数组按 PID 做下标会浪费大量空间。
BPF_HASH(counts_map, pid_t, struct data_t);  // 内核写/Py读：按 PID 聚合计数
BPF_HASH(last_time_map, u64, u64);            // 限流用（同样存在主线那个 key bug）
BPF_ARRAY(delay_map, int, 1);                 // Py写/内核读：采样间隔(秒)

// ----------------------------------------------------------------------------
// TRACEPOINT_PROBE(kmem, mm_page_alloc_extfrag)：
//   挂内核 tracepoint kmem:mm_page_alloc_extfrag。
//   参数通过 args-> 访问（如 args->pfn），由 tracepoint 格式自动提供。
// mm_page_alloc_extfrag---->include/trace/events/kmem.h
/*
int tracepoint__kmem__mm_page_alloc_extfrag(struct tracepoint__kmem__mm_page_alloc_extfrag *args)
*/
// ----------------------------------------------------------------------------
TRACEPOINT_PROBE(kmem, mm_page_alloc_extfrag) {
  // ---- 限流（与 fraginfo.c 同款逻辑，也同款 BUG：key 用了变化的时间戳）----
  u64 *last_time, current_time = bpf_ktime_get_ns(); // 获取当前时间
  int key = 0;
  last_time = last_time_map.lookup(&key);  // ← 同 BUG：key 应固定为 0
  
  int *delay_ptr = delay_map.lookup(&key);
  int delay;
  if (delay_ptr) {
    delay = *delay_ptr;
  }
  if (last_time && (current_time - *last_time < delay * 1000000000)) {
    return 0;
  }

  struct data_t *data, zero = {};//
  pid_t pid = bpf_get_current_pid_tgid() >> 32;  // 高 32 位是 tgid(进程PID)

  // ---- 按 PID 聚合：第一次见到该进程就初始化，否则累加计数 ----
  data = counts_map.lookup(&pid);
  if (!data) {
    //struct tracepoint__kmem__mm_page_alloc_extfrag *args 内核
    /**
    xxy@xxy-virtual-machine:~/wlsp$ sudo cat /sys/kernel/debug/tracing/events/kmem/mm_page_alloc_extfrag/format 
    [sudo] xxy 的密码： 
    name: mm_page_alloc_extfrag
    ID: 539
    format:
            field:unsigned short common_type;       offset:0;       size:2; signed:0;
            field:unsigned char common_flags;       offset:2;       size:1; signed:0;
            field:unsigned char common_preempt_count;       offset:3;       size:1; signed:0;
            field:int common_pid;   offset:4;       size:4; signed:1;

            field:unsigned long pfn;        offset:8;       size:8; signed:0;
            field:int alloc_order;  offset:16;      size:4; signed:1;
            field:int fallback_order;       offset:20;      size:4; signed:1;
            field:int alloc_migratetype;    offset:24;      size:4; signed:1;
            field:int fallback_migratetype; offset:28;      size:4; signed:1;
            field:int change_ownership;     offset:32;      size:4; signed:1; */
    // 该 PID 第一次触发 fallback，初始化一条新记录，count 从 1 起
    zero.pid = pid;
    zero.pfn = args->pfn;
    zero.alloc_order = args->alloc_order;//进程本来想要的块 之阶
    zero.fallback_order = args->fallback_order;//实际上取别的migratetype借了多大阶的 块
    zero.count = 1;
    bpf_get_current_comm(&zero.pcomm, sizeof(zero.pcomm));
    counts_map.update(&pid, &zero);
  } else {
    // 已存在 → 次数 +1，并刷新最近一次的 pfn/order 信息
    data->count += 1;
    data->pfn = args->pfn;
    data->alloc_order = args->alloc_order;
    data->fallback_order = args->fallback_order;
    bpf_get_current_comm(&data->pcomm, sizeof(data->pcomm));
    counts_map.update(&pid, data);
  }
  last_time_map.update(&key, &current_time);
  return 0;
}
