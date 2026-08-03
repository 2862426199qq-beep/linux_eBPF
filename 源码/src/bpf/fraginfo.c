// ============================================================================
// fraginfo.c —— 内存碎片检测的【主线】eBPF 程序（-v / -z / 默认模式加载）
// ----------------------------------------------------------------------------
// 角色：生产者。kprobe 挂在内核分配入口 get_page_from_freelist 上，
//       每次系统分配内存就被触发一次，全量扫描 zone × order，算出碎片分数，
//       通过 zone_map 这张 BPF map 交给用户态 Python 去渲染。
// 配套：extfrag.py 负责读 zone_map、还原小数、画图。
// ============================================================================
#include <linux/gfp.h>
#include <linux/mm.h>
#include <linux/mmzone.h>
#include <uapi/linux/ptrace.h>

#define MAX_ORDER 10   // 伙伴系统最高阶：order 0..10，共 11 档（4KB ~ 4MB）

// ---- NUMA 节点(pglist_data)的精简信息，最终传给用户态 ----
struct pgdat_info {
  u64 pgdat_ptr;   // 节点起始 pfn（这里复用字段存的是 node_start_pfn）
  int nr_zones;    // 该节点的 zone 数量
  int node_id;     // NUMA 节点号
};

// ---- 核心数据结构：一条记录 = 某个 (zone, order) 的碎片快照 ----
// 这个结构体的字段会被 BCC 自动映射成 Python 里的 value.xxx
struct zone_info {
  u64 zone_ptr;              // zone 结构体指针（同时用作 map key 的基址）
  u64 zone_start_pfn;        // zone 起始页帧号
  u64 spanned_pages;         // zone 跨越的总页数
  u64 present_pages;         // zone 中实际存在的页数
  unsigned long free_pages;            // 当前 order 视角下的空闲页总数
  unsigned long free_blocks_total;     // 空闲块总数
  unsigned long free_blocks_suitable;  // 能凑出本 order 大块的空闲块数
  char name[32];             // zone 名（"DMA"/"DMA32"/"Normal"/"Movable"）
  int order;                 // 当前是第几阶
  int score_a;               // __fragmentation_index 结果（-1000 ~ 1000）
  int score_b;               // unusable_free_index 结果（0 ~ 1000）
  int node_id;               // 所属 NUMA 节点
};

// ---- 内核 alloc_context 的镜像声明（用于从 kprobe 参数里取 zonelist）----
// 内核没把这个结构体导出给 BPF，所以在这里照着内核定义手抄一份。
struct alloc_context {
  struct zonelist *zonelist;
  nodemask_t *nodemask;
  struct zoneref *preferred_zoneref;
  int migratetype;// 迁移类型：MIGRATE_UNMOVABLE / MIGRATE_MOVABLE / MIGRATE_RECLAIMABLE / MIGRATE_CMA
  enum zone_type highest_zoneidx;
  bool spread_dirty_pages;
};

// ---- 一次扫描某个 order 时的中间统计量（算分用的临时容器）----
struct contig_page_info {
  unsigned long free_pages;
  unsigned long free_blocks_total;
  unsigned long free_blocks_suitable;
};

// ============ 4 张 BPF map：内核与用户态之间唯一的桥 ============
/*
- BPF_HASH(...) —— 这个是 BCC 提供的宏(可以理解成 BCC 框架的"API"),作用是"帮我创建一张哈希表类型的 BPF map"。
- last_time_map —— 这是作者给这张表起的名字,纯自定义。你把它改成 xxx_map 功能完全一样。
- 后面两个 u64 —— key 类型是 u64,value 类型是 u64。
 */
BPF_HASH(pgdat_map, u64, struct pgdat_info);  // 内核写/Py读：NUMA 节点元信息
BPF_HASH(zone_map, u64, struct zone_info);    // 内核写/Py读：★核心★ 碎片数据
BPF_HASH(last_time_map, u64, u64);            // 内核读写：限流用的上次采样时间
BPF_ARRAY(delay_map, int, 1);                 // Py写/内核读：采样间隔(秒)

// score_b: 空闲页里有多少比例凑不出目标阶大块，0~1000（0~100.0%），越高越碎。
// eBPF 无浮点，全程 ×1000，Python 端 //1000 还原。
// order: 目标阶  info: fill_contig_page_info 的统计结果
static int unusable_free_index(unsigned int order,
                               struct contig_page_info *info) {
  if (info->free_pages == 0)
    return 1000;   // 一页都没有 → 100% 不可用
  return div_u64(  // 用内核宏 div_u64 而非裸 '/'，因为 eBPF 限制 64 位除法
      (info->free_pages - (info->free_blocks_suitable << order)) * 1000ULL,
      info->free_pages);
}

// score_a: 外部碎片指数，-1000~1000。负=有大块(非瓶颈)，正=页多但碎(是瓶颈)，0=无空闲块(缺内存)。
// order: 目标阶  info: fill_contig_page_info 的统计结果
static int __fragmentation_index(unsigned int order,
                                 struct contig_page_info *info) {
  unsigned long requested = 1UL << order;   // 本次请求需要 2^order 个连续页
  if (WARN_ON_ONCE(order > MAX_ORDER))
    return 0;
  if (!info->free_blocks_total)
    return 0;                  // 没有任何空闲块 → 是缺内存，不算碎片
  if (info->free_blocks_suitable)
    return -1000;              // 有现成合适大块 → 碎片不是瓶颈
  unsigned long ideal_blocks = div_u64(info->free_pages * 1000ULL, requested);
  unsigned long actual_blocks = info->free_blocks_total;
  // 理想≈实际 → 比值≈1 → 结果≈0(不碎)；实际≫理想 → 比值趋0 → 结果→1000(碎)
  // +1000 垫底防止整数除法截断为 0
  return 1000 - div_u64(1000 + ideal_blocks, actual_blocks);
}

// ----------------------------------------------------------------------------
// fill_contig_page_info: 扫一个 zone 的 free_area[0..10]，统计三个量。
// 这是算分前的"数数"步骤，score_a / score_b 都依赖它的输出。
// suitable_order = 目标阶,高于它的块可拆出若干目标块
// ----------------------------------------------------------------------------
static void fill_contig_page_info(struct zone *zone,
                                  unsigned int suitable_order,
                                  struct contig_page_info *info) {
  unsigned int order;
  info->free_pages = 0;
  info->free_blocks_total = 0;
  info->free_blocks_suitable = 0;
  for (order = 0; order <= MAX_ORDER; order++) {
    unsigned long blocks;// 这一阶的空闲块数
    unsigned long nr_free;
    // ★门槛★ 不能直接写 nr_free = zone->free_area[order].nr_free;
    //   eBPF 里直接解引用内核指针会被 Verifier 拒绝（坏指针会让内核 panic），
    //   必须用 bpf_probe_read_kernel 把值"安全拷贝"到本地变量。
    bpf_probe_read_kernel(&nr_free, sizeof(nr_free),
                          &zone->free_area[order].nr_free);//安全地读出这一阶由几个空闲块
    blocks = nr_free;
    info->free_blocks_total += blocks;       // 累加各阶空闲块数
    info->free_pages += blocks << order;     // 块数 × 2^order = 页数， blocks × 2^order
    if (order >= suitable_order)             // 比目标阶大的块可拆成多个目标块
      info->free_blocks_suitable += blocks << (order - suitable_order);// blocks × 2^(order-suitable_order) 
  }
}

// ============================================================================
// 主体：kprobe 挂在 get_page_from_freelist 上。
// BCC 命名魔法：函数名前缀 kprobe__ 后面跟内核函数名，自动 attach，
// 参数 (gfp_mask, order, alloc_flags, ac) 直接对应被 hook 内核函数的入参。
// 内核每分配一次内存，这个函数就跑一次 → 它是整条链路的"生产者"。
// ============================================================================
int kprobe__get_page_from_freelist(struct pt_regs *ctx, gfp_t gfp_mask,
                                   unsigned int order, int alloc_flags,
                                   const struct alloc_context *ac) {
  // 限流：每次分配都触发，按 delay_map 设的间隔(秒)采样
  u64 *last_time, current_time = bpf_ktime_get_ns();
  int key = 0;
  last_time = last_time_map.lookup(&key);
  int delay = 0;
  int *delay_ptr = delay_map.lookup(&key);
  if (delay_ptr) delay = *delay_ptr;
  if (last_time && (current_time - *last_time < delay * 1000000000ULL))
    return 0;

  struct pglist_data *pgdat;
  struct zone *z;
  struct zoneref *zref;
  int i;
  unsigned int a_order;

  // 从首选 zone 拿到所属 NUMA 节点
  pgdat = ac->preferred_zoneref->zone->zone_pgdat;

  // 外层:遍历 fallback zonelist；  MAX_NR_ZONES从哪来的？
  for (i = 0; i < MAX_NR_ZONES; i++) {
    struct zone_info zone_data = {};
    struct pgdat_info pgdat_data = {};
    struct pgdat_info *a_pgdat;
    struct pglist_data *pgdata;
    u64 node_key, zone_key;
    zref = &pgdat->node_zonelists[ZONELIST_FALLBACK]._zonerefs[i];
    z = zref->zone;
    if (!z)
      continue;

    // 记录 NUMA 节点信息（每个节点只写一次）
    pgdata = z->zone_pgdat;
    if (!pgdata)
      continue;
    node_key = (u64)pgdata;
    a_pgdat = pgdat_map.lookup(&node_key); //返回指针
    if (!a_pgdat) {
      pgdat_data.pgdat_ptr = (u64)pgdata->node_start_pfn;
      pgdat_data.nr_zones = pgdata->nr_zones;
      pgdat_data.node_id = pgdata->node_id;
      pgdat_map.update(&node_key, &pgdat_data);
    }

    // 填充 zone 固定属性（与 order 无关）
    zone_data.zone_ptr = (u64)z;
    zone_data.zone_start_pfn = z->zone_start_pfn;
    zone_data.spanned_pages = z->spanned_pages;
    zone_data.present_pages = z->present_pages;
    zone_data.node_id = z->zone_pgdat->node_id;
    bpf_probe_read_kernel_str(&zone_data.name, sizeof(zone_data.name), z->name);

    // 内层:遍历 order 0~10 算分
    for (a_order = 0; a_order <= MAX_ORDER; ++a_order) {
      zone_data.order = a_order;
      // key = zone_ptr + order, 保证每个 (zone,order) 独立一条
      zone_key = zone_data.zone_ptr + zone_data.order;

      struct contig_page_info ctg_info;
      fill_contig_page_info(z, a_order, &ctg_info);
      zone_data.free_blocks_suitable = ctg_info.free_blocks_suitable;
      zone_data.free_blocks_total = ctg_info.free_blocks_total;
      zone_data.free_pages = ctg_info.free_pages;
      zone_data.score_b = unusable_free_index(a_order, &ctg_info);
      zone_data.score_a = __fragmentation_index(a_order, &ctg_info);
      zone_map.update(&zone_key, &zone_data);  // 写进 map, Python 端渲染
      zone_key++;
    }
  }
  last_time_map.update(&key, &current_time);
  return 0;
}
