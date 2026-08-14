// ============================================================================
// compactinfo.c —— v2 主角：direct compaction（直接规整）的代价量化与归因
// ----------------------------------------------------------------------------
// v1 的两个模块回答的是"内存有多碎"和"谁在制造碎片"。
// 本模块回答的是**"所以呢"**：碎片把分配从快路径逼进慢路径之后，
//   ① 进程被同步阻塞了多久（延迟分布，不是平均值）
//   ② 这次规整最后成功了没有（9 种结局的分布）
//   ③ 内核为此搬了多少页、失败了多少页（做了多少无用功）
//
// 挂载点：内核 compaction 子系统的 4 个 tracepoint + 1 个 kretprobe。
//
// ============================================================================
// 一、为什么是"三层 + 一个 kretprobe"这种结构
// ============================================================================
//
// 三个 tracepoint 的**粒度完全不同**（实测确认，见 handoff/com_memory.md 3.3）：
//
//   一次 direct compaction（= 一个进程被卡住一次）
//   └── try_to_compact_pages          ← 1 次，外层边界，**唯一带 order**
//       ├── begin(zone A) … end(zone A)     ← per-zone，一次分配要遍历多个 zone
//       │   ├── migratepages          ← per 迁移批次！主循环每搬一批打一次
//       │   └── migratepages …
//       └── begin(zone B) … end(zone B)
//
// 三条推论直接决定了本文件的结构：
//   1. **order 只有最外层有** → 必须两层配对，用 tid 做 key 把外层查出来。
//      这是"为什么不能只挂 begin/end"的真正答案。
//   2. **begin 次数 ≫ compact_stall** → 报告里能和 /proc/vmstat 的 compact_stall
//      对上的是**外层次数**，不是内层 begin 次数。
//   3. **migratepages 一次 begin/end 内会打多次** → nr_migrated/nr_failed
//      必须**累加**，取最后一次会严重低估。
//
// ★ 为什么还要一个 kretprobe：
//   `try_to_compact_pages` 只有**入口** tracepoint，没有出口 tracepoint。
//   而 v2 的核心指标是"这次分配被卡了多久" —— 没有出口时刻就算不出来。
//   所以在 `try_to_compact_pages` 上挂 kretprobe，拿到：
//     · 退出时刻       → 配上入口时刻 = **本次 direct compaction 的总延迟**
//     · 函数返回值     → enum compact_result，即这次尝试的**最终结局**
//
//   备选方案与取舍：
//     (a) 只用 tracepoint，把"第一个 begin 到最后一个 end"当作总耗时。
//         ✗ 漏掉了进入函数后到第一个 begin 之前、以及最后一个 end 之后的开销，
//           而 status=DEFERRED/SKIPPED 的情况**根本不会打 begin/end**，
//           这类"还没开始就被劝退"的尝试会整个丢失 —— 恰恰是最有意思的一类。
//     (b) 入口也用 kprobe（PT_REGS_PARM2 取 order）。
//         ✗ 参数顺序不是稳定 ABI，换内核版本可能读到垃圾。
//     (c) 本文件的选择：**入口用 tracepoint（稳定 ABI 拿 order/gfp/prio），
//         出口用 kretprobe（只用返回值和时刻，不碰参数布局）**。
//         耦合面最小：kretprobe 只依赖"这个函数存在且非 inline"这一件事。
//         已实测 `try_to_compact_pages` 在 /proc/kallsyms 里是全局符号 T，
//         不会被内联，入口 tracepoint 与 kretprobe 严格 1:1 配对。
//
// ============================================================================
// 二、★ 三重来源过滤（本模块最容易做错的地方）
// ============================================================================
//
// compaction:* 这组 tracepoint 是**三条不同路径共用**的：
//
//   ① direct compaction —— 进程自己被卡住，同步做规整   ← 只有这个是我们要的
//   ② kcompactd         —— 内核后台线程，异步，进程没被卡
//   ③ 手动 /proc/sys/vm/compact_memory —— 管理员触发，也没有进程在等分配
//
// 三者打的是**同一组 begin/end/migratepages**。不过滤的话统计全脏，
// 算出来的"平均规整延迟"会被后台线程的数据严重稀释。
//
// 过滤办法：**只有 ① 会经过 `try_to_compact_pages`**（②③ 直接调 compact_zone）。
// 所以判据是"**当前 tid 有没有一条活跃的外层记录**" —— 没有就直接丢弃。
//
// 这个判断必须**在内核态做**（decision #7）：捞到用户态再扔，
// 等于白付了一次 map 写入 + 一次 perf 传输的开销。
//
// 自证手段：`compact_daemon_wake` 在实验里是会涨的（实测涨了 62），
// 但本模块统计出来的外层次数应该**完全不受它影响**。
// 报告里同时给出 S_BEGIN_REJECT（被过滤掉的内层事件数），
// 它 > 0 就证明过滤器真的在工作，而不是"恰好没有噪声"。
//
// ============================================================================
// 三、★ 为什么本模块【不做限流】（与 v1 的显式差异）
// ============================================================================
//
// v1 的 fraginfo.c 必须限流：它挂在 `get_page_from_freelist` 上，
// **每次内存分配都会触发**（每秒几十万次），而且每次都要扫 zone×order 全表。
// 不限流机器会被拖死。
//
// 本模块的情况完全相反：整整一轮压力实验下来，direct compaction 只发生了
// **402 次**（实测，见 fragstress/README.md）。这点量根本不需要限流；
// 而**限流会静默摧毁延迟分布** —— 被丢掉的恰好是最长尾的那些样本，
// 而长尾才是性能问题的本体。
//
// 所以 delay_map 保留（和 v1 保持接口一致），但**只作为用户态的打印间隔**，
// 内核态不再拿它做采样丢弃。这是相对 v1 的一处有意偏离，报告里要写明。
//
// ============================================================================
// 四、实测的埋点字段契约（offset/size 抄自本机 format 文件原文）
// ============================================================================
//
// sudo cat /sys/kernel/debug/tracing/events/compaction/mm_compaction_try_to_compact_pages/format
//   name: mm_compaction_try_to_compact_pages     ID: 559
//   field:int order;         offset:8;   size:4;  signed:1;
//   field:gfp_t gfp_mask;    offset:12;  size:4;  signed:0;
//   field:int prio;          offset:16;  size:4;  signed:1;
//
// sudo cat .../mm_compaction_begin/format
//   name: mm_compaction_begin                    ID: 561
//   field:unsigned long zone_start;   offset:8;   size:8;  signed:0;
//   field:unsigned long migrate_pfn;  offset:16;  size:8;  signed:0;
//   field:unsigned long free_pfn;     offset:24;  size:8;  signed:0;
//   field:unsigned long zone_end;     offset:32;  size:8;  signed:0;
//   field:bool sync;                  offset:40;  size:1;  signed:0;
//
// sudo cat .../mm_compaction_end/format
//   name: mm_compaction_end                      ID: 560
//   （同 begin 的 5 个字段）
//   field:int status;                 offset:44;  size:4;  signed:1;
//                                            ↑
//   ★ 注意是 44 而不是 41：sync 在 40 占 1 字节，41/42/43 是编译器为 int
//     对齐插的 padding。手抄结构体解析会把 status 读成垃圾。
//     BCC 的 args-> 是拿 format 里的 offset 自动生成的，安全。
//     **结论：永远用 args->，永远不要自己算偏移。**
//
// sudo cat .../mm_compaction_migratepages/format
//   name: mm_compaction_migratepages             ID: 562
//   field:unsigned long nr_migrated;  offset:8;   size:8;  signed:0;
//   field:unsigned long nr_failed;    offset:16;  size:8;  signed:0;
//
// ============================================================================
// 五、enum compact_result 的 9 个取值（★ 计划书只列了 5 个，漏了 6）
// ============================================================================
//
// 两个来源交叉确认过：end/format 里 print fmt 的 __print_symbolic，
// 以及 /usr/src/linux-headers-*/include/linux/compaction.h 的 enum compact_result。
//
//   0 COMPACT_NOT_SUITABLE_ZONE   内部值（头文件注释 internal to compaction）
//   1 COMPACT_SKIPPED             没启动："没可能，或直接回收更合适"
//   2 COMPACT_DEFERRED            因过去连续失败被内核主动推迟（退避）
//   3 COMPACT_NO_SUITABLE_PAGE    内部值
//   4 COMPACT_CONTINUE            内部值，应继续扫下一个 pageblock
//   5 COMPACT_COMPLETE            整个 zone 扫完仍没成功（最坏：白扫）
//   6 COMPACT_PARTIAL_SKIPPED     扫了一部分就退避   ← ★ 计划书漏了这个
//   7 COMPACT_CONTENDED           锁竞争，提前终止
//   8 COMPACT_SUCCESS             判定分配现在能成功了
//
// 本模块把 0~8 全部保留分类，**不做"某些值不会出现"的假设**
// （头文件说 0/3/4 是 internal，理论上只出现在 mm_compaction_finished 上，
//   但这个"理论上"要用实测数据验证，不能提前假设）。
// 越界的值统一落到 STATUS_SLOTS-1 这个"未知"桶，一样会被报出来。
//
// ★ 另外："规整成功率"至少有三种算法，工具必须说清用的哪种。
//   内核自己的口径（读 compaction.h 的判定函数）：
//     compaction_made_progress()  : 只有 SUCCESS(8) 算成功
//     compaction_failed()         : 只有 COMPLETE(5) 算真失败（整个 zone 白扫完）
//     compaction_withdrawn()      : DEFERRED(2)/CONTENDED(7)/PARTIAL_SKIPPED(6)
//                                   算"主动退避"，内核认为换更高优先级还有戏
//     compaction_needs_reclaim()  : SKIPPED(1) 意味着**应该先做回收**
//   所以 SKIPPED 不是"规整失败"，而是"规整判断自己不该上，该让回收先干" ——
//   空闲页太少，连做迁移用的目标页都凑不出来。这从代码层面印证了慢路径里
//   **reclaim 在前、compaction 在后**。
//   本模块只负责**如实上报 9 个桶的原始计数**，怎么算成功率交给用户态，
//   并且报告里必须写明用的是哪个口径。
// ============================================================================

#include <linux/gfp.h>
#include <linux/mm.h>
#include <linux/sched.h>
#include <uapi/linux/ptrace.h>

// ---------------------------------------------------------------- 常量

#define STATUS_SLOTS 16  // 0~8 是真实取值，9~14 预留，15 = 未知/越界

// __GFP_DIRECT_RECLAIM：数值取自 try_to_compact_pages/format 的 __print_flags 表
// （那张表白送了完整的 GFP 标志位对应数值，不用去翻 include/linux/gfp.h）
#define GFP_DIRECT_RECLAIM_BIT 0x400

// stat_map 的下标。用具名常量而不是裸数字，Python 侧有同名对照表。
#define S_OUTER_ENTER    0  // 外层进入次数 ← 这个才是能和 compact_stall 对账的数
#define S_OUTER_EXIT     1  // 外层退出次数（kretprobe 命中）
#define S_OUTER_UNPAIRED 2  // ★ kretprobe 找不到入口记录 = 未配对
#define S_BEGIN_ACCEPT   3  // 内层 begin 被接纳（属于 direct compaction）
#define S_BEGIN_REJECT   4  // ★ 内层 begin 被过滤（kcompactd / 手动 compact_memory）
#define S_END_ACCEPT     5
#define S_END_UNPAIRED   6  // ★ end 找不到对应的 begin = 未配对
#define S_MIG_ACCEPT     7
#define S_MIG_REJECT     8  // migratepages 被过滤
#define S_NO_DIRECT_RECL 9  // gfp 里居然没有 __GFP_DIRECT_RECLAIM（预期为 0）
#define STAT_SLOTS      10

// mig_map 的下标
#define M_MIGRATED 0
#define M_FAILED   1

// ---------------------------------------------------------------- 数据结构

// 外层上下文：一次 direct compaction 尝试的全貌。key = tid
struct outer_t {
  u64 ts;             // 进入 try_to_compact_pages 的时刻（ns）
  u64 nr_migrated;    // ★ 本次尝试内所有 migratepages 的累加
  u64 nr_failed;      // ★ 同上
  int order;          // 想要的 order —— 只有外层埋点才有
  u32 gfp_mask;
  int prio;           // 规整优先级（COMPACT_PRIO_*）
  u32 nr_zones;       // 本次尝试遍历了几个 zone（= begin 命中次数）
  u32 last_sync;      // 最后一个 zone 用的是同步还是异步迁移
};

// 内层 per-zone 上下文：用来把 begin 和 end 配成对。key = tid
// 为什么 key 只用 tid 就够：同一个线程在同一时刻只会在一个 zone 上做规整，
// begin/end 严格嵌套，不会交错。
struct zone_t {
  u64 ts;             // begin 的时刻
  u64 zone_start;
  u64 migrate_pfn;    // 迁移页扫描器起点（从低地址往上走）
  u64 free_pfn;       // 空闲页扫描器起点（从高地址往下走）
  u64 zone_end;
  u32 sync;
};

// 延迟直方图的 key：两维（order × log2 桶）
// 为什么按 order 分维度：order-3 和 order-9 的规整成本差一个量级，
// 混在一起算平均值等于什么都没测。
struct alat_key_t {
  u32 order;
  u32 slot;
};

// 内层 per-zone 延迟直方图的 key：按 sync 分维度
// 为什么按 sync 分：异步迁移遇到锁竞争就放弃，同步迁移会等 —— 成本天差地别。
struct zlat_key_t {
  u32 sync;
  u32 slot;
};

// ---------------------------------------------------------------- BPF map
//
// ★ 为什么外层/内层的配对表用 lru_hash 而不是普通 hash：
//   配对表存在"进去了但没出来"的风险（探针漏触发、内核走了没被覆盖的分支、
//   map 满了）。普通 hash 满了之后新事件会**直接写入失败**，
//   而且旧的僵尸表项永远不会被清 —— 这是**内存泄漏**。
//   lru_hash 满了会淘汰最久未用的表项，不会泄漏。
//
//   但这**不是纯优化，是一笔交易**：它拿"内存泄漏风险"换了"静默丢事件"。
//   被淘汰掉的恰恰是**停留最久的那些** —— 也就是延迟最长的尾部样本，
//   而长尾才是性能问题的本体。
//   所以必须配 S_OUTER_UNPAIRED / S_END_UNPAIRED 两个计数器，
//   **输出时必须报出未配对率**。不报未配对率的延迟统计是不可信的。
//
// BCC 0.12.0 没有 BPF_LRU_HASH 这个便捷宏（实测确认），
// 但底层的 BPF_TABLE("lru_hash", ...) 在，效果完全一样。
BPF_TABLE("lru_hash", u64, struct outer_t, outer_map, 10240);  // key = tid
BPF_TABLE("lru_hash", u64, struct zone_t,  zone_map,  10240);  // key = tid

// ★ 必须显式给容量。BCC 0.12 的 BPF_HISTOGRAM(名字, key类型) 两参数形式
//   展开成 BPF_TABLE("histogram", key, u64, name, **64**) —— 只有 64 项。
//   我们的 key 是二维的（order × log2 桶），最坏 11 个 order × 30 个桶 = 330 项，
//   一旦超出，`increment()` 会**静默失败**：没有报错、没有返回值可查，
//   丢掉的样本无声无息 —— 而这正是本项目最不能接受的失效模式。
//   开到 1024，留足余量；同时用户态会用
//   "直方图样本总数 == 外层退出数 − 未配对数" 这条等式做交叉校验，
//   万一还是溢出了，那条等式会立刻不成立。
BPF_HISTOGRAM(attempt_lat, struct alat_key_t, 1024);  // 每次尝试的总延迟（μs，log2）
BPF_HISTOGRAM(zone_lat,    struct zlat_key_t, 1024);  // 每个 zone 的规整延迟（μs，log2）

BPF_ARRAY(attempt_status, u64, STATUS_SLOTS);   // 外层最终结局分布（kretprobe 返回值）
BPF_ARRAY(zone_status,    u64, STATUS_SLOTS);   // 内层 per-zone 结局分布（end 的 status）
BPF_ARRAY(stat_map,       u64, STAT_SLOTS);     // 各类计数 + ★未配对计数
BPF_ARRAY(mig_map,        u64, 2);              // 累计搬走/搬失败的页数
BPF_ARRAY(delay_map,      int, 1);              // Py 写/内核读：仅作用户态打印间隔

// ---------------------------------------------------------------- 两条通用写法
//
// ★ 写法一：计数一律用 lock_xadd(p, 1)，不用 *p += 1。
//   `*p += 1` 编译成"读-改-写"三步，4 个核同时执行会互相覆盖，丢计数。
//   lock_xadd 是原子加。统计工具连自己的计数都能丢的话，后面的对账全是假的。
//
// ★ 写法二：map 里已有的表项，直接通过 lookup 返回的指针改，**不需要再 update**。
//   lookup 返回的是**指向 map 内部那份数据的指针**，写进去当场生效。
//   （v1 的 extfraginfo.c 里是 `data->count += 1; counts_map.update(...)`，
//     那个 update 是多余的一次值拷贝。这里不沿用。）
//
// ============================================================================
// 第 1 层（外层入口）：mm_compaction_try_to_compact_pages
//   这是**唯一带 order 的埋点**，也是三重来源过滤的准入凭证。
//   位置：try_to_compact_pages() 函数开头。
// ============================================================================
TRACEPOINT_PROBE(compaction, mm_compaction_try_to_compact_pages) {
  u64 tid = bpf_get_current_pid_tgid();  // ★ 完整 u64 = tid，不是 >>32 的 tgid
                                         // 理由：compaction 是**线程**行为，
                                         // 同进程两个线程同时进慢路径会互相覆盖。
  struct outer_t o = {};

  o.ts       = bpf_ktime_get_ns();
  o.order    = args->order;
  o.gfp_mask = args->gfp_mask;
  o.prio     = args->prio;

  outer_map.update(&tid, &o);

  int idx = S_OUTER_ENTER;
  u64 *c = stat_map.lookup(&idx);
  if (c) lock_xadd(c, 1);

  // 自证：direct compaction 必然带 __GFP_DIRECT_RECLAIM（"允许我阻塞"）。
  // 这个计数预期恒为 0；一旦不为 0，说明我们对这条路径的理解有偏差，
  // 必须停下来查，而不是当噪声忽略。
  if (!(args->gfp_mask & GFP_DIRECT_RECLAIM_BIT)) {
    idx = S_NO_DIRECT_RECL;
    c = stat_map.lookup(&idx);
    if (c) lock_xadd(c, 1);
  }
  return 0;
}

// ============================================================================
// 第 1 层（外层出口）：kretprobe on try_to_compact_pages
//   BCC 按函数名前缀自动挂载，不需要 Python 侧写 attach 代码。
//   拿两样东西：退出时刻（→ 总延迟）、返回值（→ enum compact_result 最终结局）。
// ============================================================================
int kretprobe__try_to_compact_pages(struct pt_regs *ctx) {
  u64 tid = bpf_get_current_pid_tgid();
  struct outer_t *o = outer_map.lookup(&tid);
  int idx, status;
  u64 *c;

  idx = S_OUTER_EXIT;
  c = stat_map.lookup(&idx);
  if (c) lock_xadd(c, 1);

  if (!o) {
    // ★ 未配对的三个来源，如实计数，不要静默丢弃：
    //   (1) 进程在我们加载探针之前就已经进了这个函数（只在启动瞬间出现）
    //   (2) lru_hash 把表项淘汰掉了（map 压力大时）
    //   (3) ★ 结构性来源，2026-08-12 核对 mm/compaction.c v5.15.178 才发现：
    //       try_to_compact_pages() 开头是
    //           if (!gfp_compaction_allowed(gfp_mask))
    //                   return COMPACT_SKIPPED;      ← 早退
    //           trace_mm_compaction_try_to_compact_pages(...);  ← 埋点在后
    //       **早退发生在入口 tracepoint 之前**，而 kretprobe 挂在函数返回上，
    //       不管从哪条路返回都会打 → 必然产生"有出口无入口"的未配对。
    //       这不是偶发噪声，是这条路径的固有行为。
    //       （2026-08-12 实测 unpaired=0，因为 hugetlb 池扩容的 GFP 允许规整，
    //         从没走这条早退；换个 caller 就会出现，届时不要误判为探针出错。）
    idx = S_OUTER_UNPAIRED;
    c = stat_map.lookup(&idx);
    if (c) lock_xadd(c, 1);
    return 0;
  }

  u64 delta_us = (bpf_ktime_get_ns() - o->ts) / 1000;

  // 延迟直方图：log2 分桶，按 order 分维度。
  // ★ 为什么不用平均值：规整延迟是典型的长尾分布，
  //   99% 的尝试可能是几十微秒，1% 是几十毫秒 —— 平均值会把这 1% 抹平，
  //   而那 1% 才是用户感知到的卡顿。
  struct alat_key_t ak = {};
  ak.order = o->order;
  ak.slot  = bpf_log2l(delta_us);
  attempt_lat.increment(ak);

  // 最终结局：kretprobe 的返回值就是 enum compact_result
  status = PT_REGS_RC(ctx);
  if (status < 0 || status >= STATUS_SLOTS - 1)
    status = STATUS_SLOTS - 1;  // 越界统一落"未知"桶，一样上报
  c = attempt_status.lookup(&status);
  if (c) lock_xadd(c, 1);

  // 本次尝试累计搬了多少页、失败多少页 → 汇总到全局
  idx = M_MIGRATED;
  c = mig_map.lookup(&idx);
  if (c) lock_xadd(c, o->nr_migrated);
  idx = M_FAILED;
  c = mig_map.lookup(&idx);
  if (c) lock_xadd(c, o->nr_failed);

  outer_map.delete(&tid);
  return 0;
}

// ============================================================================
// 第 2 层：mm_compaction_begin —— per-zone，一次尝试可能触发多次
//   这里是**三重来源过滤**真正生效的地方。
// ============================================================================
TRACEPOINT_PROBE(compaction, mm_compaction_begin) {
  u64 tid = bpf_get_current_pid_tgid();
  int idx;
  u64 *c;

  // ★ 准入判据：当前 tid 有没有活跃的外层记录。
  //   没有 → 这次 begin 来自 kcompactd 或手动 compact_memory，直接丢弃。
  struct outer_t *o = outer_map.lookup(&tid);
  if (!o) {
    idx = S_BEGIN_REJECT;
    c = stat_map.lookup(&idx);
    if (c) lock_xadd(c, 1);
    return 0;
  }

  struct zone_t z = {};
  z.ts          = bpf_ktime_get_ns();
  z.zone_start  = args->zone_start;
  z.migrate_pfn = args->migrate_pfn;
  z.free_pfn    = args->free_pfn;
  z.zone_end    = args->zone_end;
  z.sync        = args->sync ? 1 : 0;
  zone_map.update(&tid, &z);

  o->nr_zones += 1;      // 直接改 map 里那份数据，不需要再 update（见上文"写法二"）
  o->last_sync = z.sync;

  idx = S_BEGIN_ACCEPT;
  c = stat_map.lookup(&idx);
  if (c) lock_xadd(c, 1);
  return 0;
}

// ============================================================================
// 第 2 层：mm_compaction_end —— 和 begin 配对，产出 per-zone 延迟与结局
// ============================================================================
TRACEPOINT_PROBE(compaction, mm_compaction_end) {
  u64 tid = bpf_get_current_pid_tgid();
  int idx, status;
  u64 *c;

  // 同样的准入判据（kcompactd 的 end 也要挡掉）
  struct outer_t *o = outer_map.lookup(&tid);
  if (!o)
    return 0;

  struct zone_t *z = zone_map.lookup(&tid);
  if (!z) {
    // ★ 未配对：有 end 没 begin。如实计数。
    idx = S_END_UNPAIRED;
    c = stat_map.lookup(&idx);
    if (c) lock_xadd(c, 1);
    return 0;
  }

  u64 delta_us = (bpf_ktime_get_ns() - z->ts) / 1000;

  struct zlat_key_t zk = {};
  zk.sync = z->sync;
  zk.slot = bpf_log2l(delta_us);
  zone_lat.increment(zk);

  status = args->status;
  if (status < 0 || status >= STATUS_SLOTS - 1)
    status = STATUS_SLOTS - 1;
  c = zone_status.lookup(&status);
  if (c) lock_xadd(c, 1);

  idx = S_END_ACCEPT;
  c = stat_map.lookup(&idx);
  if (c) lock_xadd(c, 1);

  // ★ 注意这里没有用 free_pfn - migrate_pfn 去算"扫了多少页"（decision #9）：
  //   两个扫描器会重启，硬算会得出负数或荒谬的巨大值。
  //   扫描量的权威来源是 /proc/vmstat 的 compact_migrate_scanned /
  //   compact_free_scanned，迁移量的权威来源是下面 migratepages 的累加。

  zone_map.delete(&tid);
  return 0;
}

// ============================================================================
// 第 3 层：mm_compaction_migratepages —— per 迁移批次，一次 begin/end 内会打多次
//   所以必须**累加**到外层记录上，取最后一次会严重低估。
// ============================================================================
TRACEPOINT_PROBE(compaction, mm_compaction_migratepages) {
  u64 tid = bpf_get_current_pid_tgid();
  int idx;
  u64 *c;

  struct outer_t *o = outer_map.lookup(&tid);
  if (!o) {
    idx = S_MIG_REJECT;
    c = stat_map.lookup(&idx);
    if (c) lock_xadd(c, 1);
    return 0;
  }

  o->nr_migrated += args->nr_migrated;  // ★ 累加，不是赋值
  o->nr_failed   += args->nr_failed;    // 直接改 map 内的数据，无需 update

  idx = S_MIG_ACCEPT;
  c = stat_map.lookup(&idx);
  if (c) lock_xadd(c, 1);
  return 0;
}
