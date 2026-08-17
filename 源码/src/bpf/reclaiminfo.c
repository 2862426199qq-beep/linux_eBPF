// ============================================================================
// reclaiminfo.c —— v2 阶段 P1：直接回收（direct reclaim）的代价观测
//
// 角色：和 compactinfo.c 并列的第二个"慢路径代价"模块。
//   compactinfo 回答"规整卡了多久"，本模块回答"回收卡了多久、换来几页"。
//   两者**不合并成一个 .c**（决策见 extfrag.py 的 MODE_SRC 注释）。
//
// 挂载点：2 个 tracepoint + 1 对 kprobe/kretprobe。
//
// ----------------------------------------------------------------------------
// 一、为什么 P1 的埋点比 P0 干净得多（核对 v5.15.178 源码后确认）
// ----------------------------------------------------------------------------
// P0 的 compaction 埋点有三个麻烦，P1 一个都没有：
//
//   ┌────────────────┬──────────────────────────┬────────────────────────────┐
//   │                │ P0（compaction）         │ P1（direct reclaim）       │
//   ├────────────────┼──────────────────────────┼────────────────────────────┤
//   │ 来源是否混杂   │ ★ 混。kcompactd、手动    │ **不混**。kswapd 和 memcg  │
//   │                │ compact_memory、direct   │ 各有自己的 tracepoint      │
//   │                │ 三家打同一组 begin/end， │ （mm_vmscan_kswapd_* /     │
//   │                │ 必须做三重来源过滤       │ mm_vmscan_memcg_*），      │
//   │                │                          │ direct_reclaim_* 是专用的  │
//   ├────────────────┼──────────────────────────┼────────────────────────────┤
//   │ order 在哪     │ 只有外层埋点有，内层没， │ **begin 自带 order**，     │
//   │                │ 必须靠外层传下来         │ 不用外层传                 │
//   ├────────────────┼──────────────────────────┼────────────────────────────┤
//   │ begin:end 关系 │ 1:N（一次尝试遍历多个    │ **严格 1:1**，同一个函数体 │
//   │                │ zone，每个 zone 一对）   │ 里一前一后（vmscan.c:3573  │
//   │                │                          │ 和 :3577）                 │
//   └────────────────┴──────────────────────────┴────────────────────────────┘
//
// 所以本模块**没有过滤器**，也没有"内层向外层借 order"的机制。
//
// ----------------------------------------------------------------------------
// 二、那为什么还要一对 kprobe/kretprobe？—— 因为 begin/end 量错了东西
// ----------------------------------------------------------------------------
// mm/vmscan.c:3540 try_to_free_pages() 的函数体（v5.15.178）：
//
//     unsigned long try_to_free_pages(...) {
//         ...
//         if (throttle_direct_reclaim(sc.gfp_mask, zonelist, nodemask))
//                 return 1;                                      // :3570
//         set_task_reclaim_state(current, &sc.reclaim_state);
//         trace_mm_vmscan_direct_reclaim_begin(order, sc.gfp_mask);   // :3573
//         nr_reclaimed = do_try_to_free_pages(zonelist, &sc);
//         trace_mm_vmscan_direct_reclaim_end(nr_reclaimed);           // :3577
//         ...
//         return nr_reclaimed;
//     }
//
// **限流的睡眠发生在 begin 埋点之前。**看 throttle_direct_reclaim()
// 的尾部（:3526-3531）：
//
//     if (!(gfp_mask & __GFP_FS))
//             wait_event_interruptible_timeout(pgdat->pfmemalloc_wait,
//                     allow_direct_reclaim(pgdat), HZ);   // 最多睡 1 秒
//     else
//             wait_event_killable(zone->zone_pgdat->pfmemalloc_wait,
//                     allow_direct_reclaim(pgdat));       // ★ 睡到 kswapd 叫醒，无上限
//
// 于是：
//   · begin → end   量的是**扫描本身**的耗时，不含限流睡眠
//   · 函数入口 → 返回 量的是**进程实际被卡住**的总时长，含限流睡眠
//
// 进程感知到的卡顿是后者。而且内核自己的 PSI 也是按后者算的 ——
// mm/page_alloc.c 里 psi_memstall_enter(:4653) / psi_memstall_leave(:4662)
// 正好夹住 :4657 的 try_to_free_pages() 调用。所以：
//
//     ★ 外层探针的耗时总和 ≈ /proc/pressure/memory 的 total 增量
//     ★ 外层耗时 − 内层耗时 = 被限流睡掉的时间
//
// 前者是本模块唯一的**第三方交叉校验**（PSI 由内核独立统计，不经过我们）。
// 后者是"卡顿到底花在扫描上还是花在排队上"的答案 —— 这两件事的优化方向
// 完全相反：扫描慢要改回收策略，排队久说明 kswapd 追不上、该调水位。
//
// ★★ 外层探针要不要来源过滤 —— 一个**没能完全证实的前提**，如实记在这里
//
//   本机的源码树 /home/xxy/wlsp/ksrc-5.15.178/ **是不完整的**：
//   只有 mm/page_alloc.c、mm/vmscan.c、mm/compaction.c 三个 .c 加一个
//   internal.h。所以"全内核 grep 确认只有一个调用者"这种话**说不了** ——
//   2026-08-17 一度这么写了，是错的，改成下面这样。
//
//   本机真正能验证到的：
//     · 声明在 include/linux/swap.h:379（内核头文件包里有）
//     · /proc/kallsyms 里是 'T'（全局符号，所以挂得上 kprobe）
//     · ★ **不在 Module.symvers 里 → 没有 EXPORT_SYMBOL**
//       → 任何**可加载模块**都调不到它，调用者只可能在内建代码里
//     · 在本机有的这三个 mm 文件里，唯一调用点是
//       mm/page_alloc.c:4657（在 __perform_reclaim() 内）
//
//   剩下的缺口："内建代码的其他部分（fs/、drivers/…）有没有调它"无法本地验证。
//   处理办法不是假设它成立，而是**让它在运行时可测**：
//   下面的 caller_stacks 会把每次进入 try_to_free_pages 的内核调用栈记下来，
//   用户态用 /proc/kallsyms 解析。如果输出里只有 __alloc_pages_slowpath
//   那一条栈，前提就被实测证实了；出现第二条栈，说明有别的调用者，
//   那时候"外层不需要过滤"这个结论要推翻，并且外层与 PSI 的对账也得重算。
//
//   顺带一提，__perform_reclaim() 自己是 static 且被 inline 包着，挂不上。
//
// ----------------------------------------------------------------------------
// 三、★ 一条被自己写错过的账，留在这里当反面教材
// ----------------------------------------------------------------------------
// 2026-08-16 曾把等式记成：
//     kretprobe 次数 = begin 次数 + 被限流次数            ← **错的**
// 错在没读完 throttle_direct_reclaim()。它 return true（→ 早退，不打 begin）
// **只发生在 fatal_signal_pending(current) 成立时**（:3534）；正常被限流的
// 进程是睡一觉、醒来 return false、**继续往下走到 begin**。所以正确的是：
//
//     kretprobe 次数 = begin 次数 + 被限流期间收到致命信号的次数（通常 0）
//     被限流次数     = /proc/vmstat 的 pgscan_direct_throttle
//                      （:3515 count_vm_event 在睡之前就计了，不管后面怎么走）
//
// 教训和 P0 那次一样：**早退分支的位置要读到 return 为止，不能看到
// 一个 if 就下结论**。这次多一条：`if (throttle_direct_reclaim(...)) return 1;`
// 这一行看起来像"被限流就退出"，但真正决定退不退的逻辑在被调函数的**末尾**
// （:3533 的 fatal_signal_pending）。只读调用点读不出来。
//
// ----------------------------------------------------------------------------
// 四、★ 返回值 1 是有歧义的，必须靠 begin 才能消歧
// ----------------------------------------------------------------------------
// :3570 的早退是 `return 1`，而正常路径也可能真的只回收了 1 页。
// 单看 kretprobe 的返回值分不出来。区分办法：**有没有配上 begin**。
//   有 begin → 走的是正常路径，1 就是真回收了 1 页
//   无 begin → 是那个早退
// 本模块把"返回 1"单独计一格（S_RET_ONE），供用户态和 begin 数对照。
//
// ----------------------------------------------------------------------------
// 五、字段布局（sudo cat .../format 实测，2026-08-17，不是抄头文件）
// ----------------------------------------------------------------------------
//   name: mm_vmscan_direct_reclaim_begin          ID: 529
//     field:int   order;         offset:8;   size:4;   signed:1
//     field:gfp_t gfp_flags;     offset:12;  size:4;   signed:0
//
//   name: mm_vmscan_direct_reclaim_end            ID: 526
//     field:unsigned long nr_reclaimed;  offset:8;  size:8;  signed:0
//
// ★ 两边字段宽度不一样：begin 的两个字段都是 4 字节，
//   end 的 nr_reclaimed 是 **8 字节 unsigned long**。
//   照 begin 的模式想当然写成 u32，在小端机上会读到低 32 位 ——
//   页数不大时**看起来完全正常**，溢出时才暴露。这种错误不会自己现形，
//   所以只能靠实测 format 来防。两边都从 offset 8 开始，无 padding。
//   （BCC 的 TRACEPOINT_PROBE 会自己按 format 生成 args 结构体，
//     这里记下来是为了 code review 时能核对，以及换内核版本时知道要重测什么。）
// ============================================================================

#include <linux/gfp.h>
#include <linux/mm.h>
#include <linux/sched.h>
#include <uapi/linux/ptrace.h>

// ---------------------------------------------------------------- 常量

// __GFP_DIRECT_RECLAIM：数值取自 format 文件里 __print_flags 那张表
// （和 compactinfo.c 同一个来源，不去翻 include/linux/gfp.h）
#define GFP_DIRECT_RECLAIM_BIT 0x400

// 判定"这次外层耗时里有限流睡眠"的阈值（微秒）。
// ★ 为什么要阈值而不是"外层 > 内层就算"：两个时刻取自不同探针，
//   中间隔着 set_task_reclaim_state() 等几行代码，本来就该差几微秒。
//   限流睡眠的量级是毫秒到秒（wait_event_killable 等 kswapd 叫醒），
//   1000 μs 这条线离两边都很远，不需要精调。
//   这个数只影响 S_THROTTLE_SLEPT 这一格计数，不影响任何直方图。
#define THROTTLE_SUSPECT_US 1000

// stat_map 的下标。用具名常量而不是裸数字，Python 侧有同名对照表。
#define S_OUTER_ENTER    0  // 进入 try_to_free_pages 次数
#define S_OUTER_EXIT     1  // 从 try_to_free_pages 返回次数（kretprobe 命中）
#define S_OUTER_UNPAIRED 2  // ★ kretprobe 找不到入口记录 = 未配对
#define S_BEGIN          3  // begin 埋点命中次数
#define S_BEGIN_NO_OUTER 4  // ★ begin 找不到外层记录（详见探针里的说明）
#define S_ORDER_MISMATCH 5  // ★ 外层 kprobe 取的 order ≠ begin 报的 order
#define S_END_ACCEPT     6  // end 成功配上 begin
#define S_END_UNPAIRED   7  // ★ end 找不到对应的 begin = 未配对
#define S_ZERO_RECLAIM   8  // nr_reclaimed == 0：卡了一趟但一页没回收到
#define S_THROTTLE_SLEPT 9  // 疑似被限流睡过（外层 − 内层 > 阈值）
#define S_RET_ONE       10  // 返回值恰好 = 1（有歧义，见头部第四节）
#define S_NO_DIRECT_RECL 11 // gfp 里没有 __GFP_DIRECT_RECLAIM（预期为 0）
#define STAT_SLOTS      12

// sum_map 的下标：需要总量（而不只是分布）的几个数
#define R_PAGES       0  // 累计回收到的页数
#define R_OUTER_NS    1  // ★ 累计外层耗时（ns）—— 拿这个和 PSI total 对账
#define R_INNER_NS    2  // 累计内层耗时（ns）—— 纯扫描
#define R_THROTTLE_NS 3  // 累计（外层 − 内层）—— 排队/限流的时间
#define SUM_SLOTS     4

// ---------------------------------------------------------------- 数据结构

// 外层上下文：进程被卡在 try_to_free_pages 里的一整段。key = tid
struct outer_t {
  u64 ts;        // 进入函数的时刻（ns）
  u64 inner_ns;  // ★ 内层（begin→end）耗时，由 end 探针回填
  int order;     // 从 PT_REGS_PARM2 取
  u32 gfp_mask;  // 从 PT_REGS_PARM3 取
  u32 saw_begin; // 是否打过 begin —— 用来给"返回值 1"消歧
};

// 内层上下文：begin→end 之间。key = tid
// ★ 为什么 key 只用 tid 就够（和 compactinfo.c 同一个理由）：
//   回收是**线程**行为，begin/end 在同一个函数体里严格 1:1 且不嵌套，
//   同一个线程不可能同时处在两次 direct reclaim 里。
//   用 tgid 会让同进程的两个线程互相覆盖 —— 这是 v1 踩过的坑。
struct inner_t {
  u64 ts;
  int order;
  u32 gfp_flags;
};

// 延迟直方图的 key：两维（order × log2 桶）
// ★ 为什么按 order 分维度：order-0（普通匿名页）和 order-9（THP）触发的
//   回收，目标页数和退出条件都不同，混在一起算平均值等于什么都没测。
struct lat_key_t {
  u32 order;
  u32 slot;
};

// ---------------------------------------------------------------- BPF map
//
// ★ 配对表用 lru_hash 而不是 hash：普通 hash 满了之后新事件直接写入失败，
//   且僵尸表项永不回收 = 内存泄漏。lru_hash 满了淘汰最久未用的。
//   但这是**一笔交易不是纯优化**：被淘汰的恰恰是停留最久的表项，
//   也就是延迟最长的样本 —— 而长尾才是性能问题的本体。
//   所以必须配 S_OUTER_UNPAIRED / S_END_UNPAIRED，**输出时必须报未配对率**。
//   （BCC 0.12.0 没有 BPF_LRU_HASH 宏，用底层的 BPF_TABLE("lru_hash",...)）
BPF_TABLE("lru_hash", u64, struct outer_t, outer_map, 10240);  // key = tid
BPF_TABLE("lru_hash", u64, struct inner_t, inner_map, 10240);  // key = tid

// ★ 必须显式给容量：BCC 0.12 的两参数 BPF_HISTOGRAM 只开 64 项，
//   而我们的 key 是二维的（11 个 order × 30 个桶 = 330 项最坏），
//   溢出时 increment() **静默失败**，没有返回值可查。开到 1024 留余量，
//   同时用户态用"直方图样本数 == 外层退出 − 未配对"这条等式做交叉校验。
BPF_HISTOGRAM(outer_lat, struct lat_key_t, 1024);  // 进程被卡住的总时长（μs，log2）
BPF_HISTOGRAM(inner_lat, struct lat_key_t, 1024);  // 纯扫描耗时（μs，log2）
BPF_HISTOGRAM(recl_hist, struct lat_key_t, 1024);  // 每次回收到的页数（log2）

// ★ 调用者诊断：把每次进入 try_to_free_pages 的内核栈存下来，按栈聚合计数。
//   目的见头部第二节末尾 —— 把"只有一个调用者"这个**本机无法证实的前提**
//   变成一条实测数据。用户态拿 stackid 去 caller_stacks 取栈、用
//   /proc/kallsyms 解析成函数名。
//
//   开销：direct reclaim 一轮实验只发生几百次，每次一个 bpf_get_stackid，
//   完全可忽略。不做采样 —— 采样会让"有没有第二个调用者"这个是非问题
//   变成概率问题，而这里要的恰恰是确定性答案。
BPF_STACK_TRACE(caller_stacks, 1024);
BPF_HASH(caller_count, int, u64, 256);   // key = stackid

BPF_ARRAY(stat_map, u64, STAT_SLOTS);  // 各类计数 + ★未配对计数
BPF_ARRAY(sum_map,  u64, SUM_SLOTS);   // 需要总量的几个数（页数、三个时长）
BPF_ARRAY(delay_map, int, 1);          // Py 写/内核读：仅作用户态打印间隔
                                       // （extfrag.py 无条件写这个 map，
                                       //   不声明会 KeyError；和 v1 保持接口一致）

// ---------------------------------------------------------------- 两条通用写法
//
// ★ 计数一律用 lock_xadd(p, 1)，不用 *p += 1。后者编译成读-改-写三步，
//   多核并发会互相覆盖丢计数。统计工具连自己的计数都能丢，后面对账全是假的。
//
// ★ map 里已有的表项，直接通过 lookup 返回的指针改，不需要再 update。
//   lookup 返回的是指向 map 内部那份数据的指针，写进去当场生效。

// 小工具：给 stat_map 的某一格加 1。
// P0 的 compactinfo.c 里这三行是每处手抄一遍的（`idx=...; c=lookup; if(c) xadd`），
// 抄了十几遍。抽成函数不是为了少打字，是因为手抄漏掉 `if (c)` 就会是
// 空指针解引用 —— verifier 会直接拒绝加载，但那时报的是一长串字节码错误，
// 定位不到是哪一处抄漏了。
static __always_inline void bump(int idx) {
  u64 *c = stat_map.lookup(&idx);
  if (c) lock_xadd(c, 1);
}

// ============================================================================
// 外层入口：kprobe on try_to_free_pages
//   签名（mm/vmscan.c:3540）：
//     unsigned long try_to_free_pages(struct zonelist *zonelist, int order,
//                                     gfp_t gfp_mask, nodemask_t *nodemask)
//   → PARM2 = order，PARM3 = gfp_mask
//
//   为什么入口用 kprobe 而不是 tracepoint：这个位置**没有 tracepoint**。
//   begin 埋点在限流之后（见头部第二节），量不到被卡住的总时长。
// ============================================================================
int kprobe__try_to_free_pages(struct pt_regs *ctx, struct zonelist *zonelist,
                              int order, gfp_t gfp_mask) {
  u64 tid = bpf_get_current_pid_tgid();  // ★ 完整 u64 = tid，不是 >>32 的 tgid
  struct outer_t o = {};

  o.ts       = bpf_ktime_get_ns();
  o.inner_ns = 0;
  o.order    = order;
  o.gfp_mask = (u32)gfp_mask;
  o.saw_begin = 0;

  outer_map.update(&tid, &o);
  bump(S_OUTER_ENTER);

  // ★ 记录调用栈，用来实测"到底谁在调 try_to_free_pages"（见头部第二节末尾）。
  //   BPF_F_FAST_STACK_CMP：只比栈的哈希不逐帧比，够用且快。
  //   取不到栈时 stackid < 0，不计 —— 宁可少记一条，不要把错误的 key
  //   混进聚合结果里（-EEXIST/-EFAULT 都是负数，会被当成合法 key）。
  int sid = caller_stacks.get_stackid(ctx, 0);
  if (sid >= 0) {
    u64 zero = 0, *cnt = caller_count.lookup_or_try_init(&sid, &zero);
    if (cnt) lock_xadd(cnt, 1);
  }

  // 自检：direct reclaim 的定义就是"允许阻塞去回收"，
  // gfp 里必然带 __GFP_DIRECT_RECLAIM。这一格预期恒为 0；
  // 不为 0 说明对这条路径的理解有误，要停下来查，不是无害的噪声。
  if (!(gfp_mask & GFP_DIRECT_RECLAIM_BIT))
    bump(S_NO_DIRECT_RECL);

  return 0;
}

// ============================================================================
// 内层 begin：mm_vmscan_direct_reclaim_begin
//   ★ 这是 direct reclaim **专用**的埋点（kswapd 走 mm_vmscan_kswapd_wake、
//     memcg 走 mm_vmscan_memcg_reclaim_begin），所以**不需要任何过滤**。
//     这也是 P1 比 P0 简单的根本原因。
// ============================================================================
TRACEPOINT_PROBE(vmscan, mm_vmscan_direct_reclaim_begin) {
  u64 tid = bpf_get_current_pid_tgid();
  struct inner_t in = {};

  in.ts        = bpf_ktime_get_ns();
  in.order     = args->order;      // ★ begin 自带 order，不用外层传
  in.gfp_flags = args->gfp_flags;

  inner_map.update(&tid, &in);
  bump(S_BEGIN);

  struct outer_t *o = outer_map.lookup(&tid);
  if (!o) {
    // ★ 有 begin 却没有外层记录，来源有三：
    //   (1) 进程在探针加载前就已经进了 try_to_free_pages（只在启动瞬间出现）
    //   (2) lru_hash 淘汰了外层表项
    //   (3) 结构性来源 —— 如果这一格**持续增长**，说明存在
    //       try_to_free_pages 之外的调用者也在打这个 tracepoint，
    //       也就是"全内核只有一个调用者"这个前提被推翻了。
    //       那时候整个外层-内层对账都要重做，不能当噪声忽略。
    bump(S_BEGIN_NO_OUTER);
    return 0;
  }

  o->saw_begin = 1;

  // ★ 免费的一致性自证：外层 kprobe 从寄存器取的 order，
  //   和 begin 埋点自己报的 order，是**两条独立路径拿到的同一个值**。
  //   对不上说明 PT_REGS_PARM 的取参假设错了（比如函数签名变了、
  //   或者被编译器改了调用约定）。这种错误在别处不会现形 ——
  //   order 只是被拿去分桶，错了也不会崩，只会让直方图悄悄归错维度。
  if (o->order != args->order)
    bump(S_ORDER_MISMATCH);

  return 0;
}

// ============================================================================
// 内层 end：mm_vmscan_direct_reclaim_end
//   ★ nr_reclaimed 是 8 字节 unsigned long（实测 format），不是 4 字节。
//   和 begin 严格 1:1（vmscan.c:3573 / :3577，同一个函数体，无循环无分支）。
// ============================================================================
TRACEPOINT_PROBE(vmscan, mm_vmscan_direct_reclaim_end) {
  u64 tid = bpf_get_current_pid_tgid();
  u64 now = bpf_ktime_get_ns();
  u64 *c;
  int idx;

  struct inner_t *in = inner_map.lookup(&tid);
  if (!in) {
    // 未配对：探针加载前就进了回收，或 lru_hash 淘汰。如实计数，不静默丢弃。
    bump(S_END_UNPAIRED);
    return 0;
  }

  u64 inner_ns = now - in->ts;
  u64 delta_us = inner_ns / 1000;

  struct lat_key_t k = {};
  k.order = in->order;
  k.slot  = bpf_log2l(delta_us);
  inner_lat.increment(k);

  // 回收到的页数分布。
  // ★ 为什么单独统计"回收到 0 页"：那是**完全白卡一趟** ——
  //   进程付了全部延迟，一页没换到。这个比例是"回收还有没有用"的直接指标，
  //   log2 直方图的 0 桶（0 和 1 混在一格）区分不出来，所以另计一格。
  u64 nr = args->nr_reclaimed;  // 8 字节
  if (nr == 0) {
    bump(S_ZERO_RECLAIM);
  } else {
    struct lat_key_t rk = {};
    rk.order = in->order;
    rk.slot  = bpf_log2l(nr);
    recl_hist.increment(rk);
  }

  idx = R_PAGES;
  c = sum_map.lookup(&idx);
  if (c) lock_xadd(c, nr);

  idx = R_INNER_NS;
  c = sum_map.lookup(&idx);
  if (c) lock_xadd(c, inner_ns);

  // 把内层耗时回填给外层，让 kretprobe 能算出"外层 − 内层 = 排队时间"
  struct outer_t *o = outer_map.lookup(&tid);
  if (o) o->inner_ns = inner_ns;

  bump(S_END_ACCEPT);
  inner_map.delete(&tid);
  return 0;
}

// ============================================================================
// 外层出口：kretprobe on try_to_free_pages
//   量的是**进程实际被卡住的总时长**（含限流睡眠），
//   这个数的总和才是能和 /proc/pressure/memory 对账的那个。
// ============================================================================
int kretprobe__try_to_free_pages(struct pt_regs *ctx) {
  u64 tid = bpf_get_current_pid_tgid();
  u64 *c;
  int idx;

  bump(S_OUTER_EXIT);

  // 返回值 = nr_reclaimed，但 1 是有歧义的（见头部第四节）
  u64 ret = PT_REGS_RC(ctx);
  if (ret == 1)
    bump(S_RET_ONE);

  struct outer_t *o = outer_map.lookup(&tid);
  if (!o) {
    bump(S_OUTER_UNPAIRED);
    return 0;
  }

  u64 outer_ns = bpf_ktime_get_ns() - o->ts;

  struct lat_key_t k = {};
  k.order = o->order;
  k.slot  = bpf_log2l(outer_ns / 1000);
  outer_lat.increment(k);

  idx = R_OUTER_NS;
  c = sum_map.lookup(&idx);
  if (c) lock_xadd(c, outer_ns);

  // ★ 排队时间 = 外层 − 内层。只有配上了内层才算得出来。
  //   没配上内层的情况（o->inner_ns == 0）有两种，不能混：
  //     · 走了 :3570 的早退（被限流 + 致命信号）→ 根本没进扫描
  //     · 内层未配对（探针加载前 / lru 淘汰）
  //   两种都不该往 R_THROTTLE_NS 里加，否则会把整段外层耗时
  //   当成"排队时间"，把这个数虚高。
  if (o->inner_ns > 0 && outer_ns > o->inner_ns) {
    u64 queue_ns = outer_ns - o->inner_ns;

    idx = R_THROTTLE_NS;
    c = sum_map.lookup(&idx);
    if (c) lock_xadd(c, queue_ns);

    if (queue_ns / 1000 > THROTTLE_SUSPECT_US)
      bump(S_THROTTLE_SLEPT);
  }

  outer_map.delete(&tid);
  return 0;
}
