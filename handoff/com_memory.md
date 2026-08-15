# 共享记忆（com_memory）

> **这份文件是什么**：本项目**跨会话、跨电脑的唯一事实源**。
> 用户会在多台机器上、开多个 AI 会话继续这个项目。任何会话开工前**必须先读这份文件**，
> 收工前**必须把新增的事实写回这份文件**。
>
> **和其他文件的分工**：
> | 文件 | 管什么 |
> |---|---|
> | `handoff/com_memory.md`（本文件） | **状态**：已确认的事实、已定的决策、还没定的问题 |
> | `handoff/handoff_task.md` | **行动**：下一个会话具体要干什么 |
> | `fraginfo_v2.md` | **蓝图**：计划书，唯一施工依据，不要改它 |
> | `fraginfo_v2_record.md` | **日志**：每一步做了什么 + 知识点笔记，逐步追加 |
>
> **最后更新**：2026-08-04

---

## 〇、给下一个会话的三条最重要提醒

### 1. ★ 用大白话讲，不要堆术语

用户是**本科在读、正在准备秋招**的开发者，v1 项目是他自己写的，**会 C、懂基本的
Linux 概念，但内核内存管理是新领域**。

**已经踩过的坑**：前一个会话一次性抛出"per-zone 粒度""对齐 padding""`__print_symbolic`"
一堆术语，用户直接回了一句 **"我听不懂你在说什么"**。

**正确做法**：
- 先讲**为什么要做这件事**，再讲怎么做；先给**比喻**，再给术语
- 一次只讲一个概念，讲完停下来确认
- 术语第一次出现必须解释，且给中文类比（例：pageblock ≈ 停车场的一个"片区"）
- 用户说"听不懂"时**不要重复原话换个说法**，要退到更底层重讲

**已经建立、后续请沿用的比喻体系**（用户认可的）：
- **内存 = 停车场**，页 = 车位，进程数据 = 车
- **碎片** = 空位总数够，但被车隔开，凑不出连续 8 个位停大巴
- **规整（compaction）** = 挪车，把车往一头挪，空位就连起来了
- **UNMOVABLE 页** = 趴窝的车，挪不走，一辆就能毁掉整个片区
- **migratetype 分组** = 停车场划片区，能挪的停一片，趴窝的停另一片
- **fallback 污染** = 趴窝区满了，去"能挪"的片区借位，把那片弄脏了

### 2. ★ 用户的机器 sudo 需要密码，AI 执行不了特权命令

所有需要 root 的操作（挂 eBPF、读 tracefs、写 `/proc/sys/vm/*`、跑压力测试）
**必须写成完整命令让用户自己在终端跑**，然后用户把输出贴回来。
**不要自己试 `sudo`，白白浪费轮次。**

### 3. ★ 多机协作：每次开工先拉，收工必推

用户会在**不同电脑**上继续这个项目。Git 仓库是唯一同步渠道。

```bash
# 开工第一件事
cd <项目目录> && git pull

# 收工最后一件事
git add -A && git commit -m "..." && git push
```

**换机器 = 环境事实全部作废**，见下面第二节的警告。

---

## 一、项目是什么（一句话版本）

把一个已完成的 v1 工具（"内存有多碎 / 谁在制造碎片"）升级成 v2
（**"碎片让谁付出了什么代价"**）。技术栈是 **BCC（Python + 内嵌 C 的 eBPF）**。

**这是一个要写进简历、要接受面试追问的项目**，所以：
**用户要的是"能在面试里复述的理解"，不是一堆能跑的代码。**
每写一段内核态代码，都要讲清它对应内核里的哪个行为、为什么这么挂、备选方案是什么。

### 项目定位（不要带偏）

通用 Linux 系统层观测工具。**明确砍掉的方向**（用户和评审员确认过）：
- ❌ 不往"开发板/嵌入式"包装，❌ 不往"服务器/云原生"包装
- ❌ 不加 CMA，❌ 不讨论交叉编译，❌ 不做 libbpf CO-RE 迁移
- ❌ 不学 `guard_tools_minimal`（fs_guard / sched_guard / lkm-fm，与主题无关）
- ❌ 不加内存泄漏检测，❌ 不做 TUI/曲线/颜色（展示层能正确打印就够）
- ❌ 不写内核模块造压力（纯用户态够用）

---

## 二、环境事实（★ 换电脑必须重测 ★）

> **警告**：下面全部是在**用户的虚拟机**上实测的。
> 如果当前会话所在的机器不是这台（内核版本、内存大小、CPU 核数任一不同），
> **这一整节的数据全部作废**，必须重跑一遍下面的命令重新填写，
> 并在本文件里标注是哪台机器的数据。

**采集机器**：xxy-virtual-machine（虚拟机）
**采集时间**：2026-08-02

```
内核     5.15.0-139-generic  x86_64
内存     MemTotal 12208080 kB（约 11.6 GiB）
CPU      4 核
大页     Hugepagesize 2048 kB → pageblock = 2MB = order-9（P2 归因的刻度）
工具链   gcc / make / stress-ng 已装
BCC      用旧包名：import bcc 可用；bpfcc 不存在
         /usr/share/bcc/tools 下没装 bcc 工具集
cgroup   v2，挂在 /sys/fs/cgroup/unified；PSI 可用（/proc/pressure/memory 存在）
内核源码 ★ 只有头文件 /usr/src/linux-headers-5.15.0-139-generic
         ★ 没有 mm/compaction.c、mm/page_alloc.c —— 这是个待解决的遗留问题
```

### ★★ 最关键的一条：这台机器"太干净"

```
compact_stall 0    compact_fail 0    compact_success 0    compact_daemon_wake 0
compact_migrate_scanned 0    compact_free_scanned 0    compact_isolated 0
allocstall_dma/dma32/normal/movable 全 0
pgscan_direct 0    pgsteal_direct 0    pgscan_direct_throttle 0
thp_fault_alloc 0  thp_fault_fallback 0
PSI: some total=0  full total=0
THP: enabled = always [madvise] never
     defrag  = always defer defer+madvise [madvise] never
```

**三套互相独立的内核统计同时报 0** = 这台机器从开机到现在**一次都没进过内存分配慢路径**。

**这是 P-1（压力注入器）必须排在所有编码工作最前面的全部理由**：
不先造出压力，P0/P1 的观测代码写得再对，跑出来也是一张空表。

**复测命令**：
```bash
grep -E "compact_|allocstall|pgscan_direct|pgsteal_direct|thp_fault" /proc/vmstat
cat /proc/pressure/memory
cat /sys/kernel/mm/transparent_hugepage/enabled /sys/kernel/mm/transparent_hugepage/defrag
```

---

## 三、已确认的技术事实（实测，可直接拿来写代码）

### 3.1 compaction 埋点字段（2026-08-02 在本机 tracefs 实测）

本机 `/sys/kernel/debug/tracing/events/compaction/` 下共 **14 个** tracepoint，
P0 要用的四个字段如下（offset/size 抄自 `format` 文件原文）：

| tracepoint | ID | 字段 |
|---|---|---|
| `mm_compaction_try_to_compact_pages` | 559 | `order` int(8/4)；`gfp_mask` gfp_t(12/4, 无符号)；`prio` int(16/4) |
| `mm_compaction_begin` | 561 | `zone_start`(8/8)、`migrate_pfn`(16/8)、`free_pfn`(24/8)、`zone_end`(32/8) 均 unsigned long；`sync` bool(40/1) |
| `mm_compaction_end` | 560 | 同 begin 五个 + `status` int(**44**/4) |
| `mm_compaction_migratepages` | 562 | `nr_migrated`(8/8)、`nr_failed`(16/8) 均 unsigned long |

**与计划书 §3.2 对比：字段名和类型完全一致，可以照计划书写。**

### 3.2 ★ `status` 实际有 9 个取值，计划书只列了 5 个 ★

两个来源交叉确认（`end/format` 的 `print fmt` 里的 `__print_symbolic`
+ `/usr/src/linux-headers-.../include/linux/compaction.h` 的 `enum compact_result`），
**完全一致**：

| 值 | 名字 | 含义 |
|---|---|---|
| 0 | `COMPACT_NOT_SUITABLE_ZONE` | 内部值（头文件注释：internal to compaction） |
| 1 | `COMPACT_SKIPPED` | **没启动**。头文件注释："没可能，或直接回收更合适" |
| 2 | `COMPACT_DEFERRED` | 因过去连续失败被内核主动推迟（退避） |
| 3 | `COMPACT_NO_SUITABLE_PAGE` | 内部值 |
| 4 | `COMPACT_CONTINUE` | 内部值，应继续扫下一个 pageblock |
| 5 | `COMPACT_COMPLETE` | 整个 zone 扫完仍没成功（最坏情况：白扫） |
| 6 | **`COMPACT_PARTIAL_SKIPPED`** | **扫了一部分就退避** ← **计划书漏了这个** |
| 7 | `COMPACT_CONTENDED` | 锁竞争，提前终止 |
| 8 | `COMPACT_SUCCESS` | 判定分配现在能成功了 |

**为什么这条重要**：按计划书那张 5 行表写代码，`status=6` 会掉进"未知"分类，
**规整成功率会算错**。代码里 9 个值全部保留分类，不做"某些值不会出现"的假设。

`0/3/4` 头文件明确标注 *internal to compaction*，理论上只出现在
`mm_compaction_finished` 上，`mm_compaction_end` 打的是 `compact_zone()` 的返回值。
**这个"理论上"要用实测数据验证，不要提前假设。**

### 3.3 三个埋点的粒度不同（直接决定代码结构）

```
一次 direct compaction（= 一个进程被卡住一次）
└── try_to_compact_pages          ← 1 次，外层边界，唯一带 order
    ├── begin(zone A) … end(zone A)     ← per-zone，一次分配要遍历多个 zone
    │   ├── migratepages          ← per 迁移批次！主循环每搬一批打一次
    │   └── migratepages …
    └── begin(zone B) … end(zone B)
```

三条推论：
1. **`order` 只有最外层有** → 必须做两层配对（用 tid 做 key 把外层查出来）。
   这是计划书要求两层结构的真正原因。
2. **`begin` 次数 ≫ `compact_stall`** → 交叉验证时能和 `compact_stall` 对上的是
   **外层次数**，不是内层 begin 次数。报告的对账表必须写清是哪一层的计数。
3. **`migratepages` 一次 begin/end 内会打多次** → `nr_migrated/nr_failed`
   必须**累加**，取最后一次会严重低估。

### 3.4 "规整成功率"至少有三种算法，工具必须说清用的哪种

从 `compaction.h` 的判定函数读出来的内核自己的口径：
- `compaction_made_progress()`：只有 `COMPACT_SUCCESS(8)` 算成功
- `compaction_failed()`：**只有 `COMPACT_COMPLETE(5)` 算真失败**（整个 zone 白扫完）
- `compaction_withdrawn()`：`DEFERRED(2)` / `CONTENDED(7)` / `PARTIAL_SKIPPED(6)`
  算"主动退避"——内核认为再试一次（用更高优先级）还有戏
- `compaction_needs_reclaim()`：`SKIPPED(1)` 意味着**应该先做回收**

**所以 `SKIPPED` 不是"规整失败"，而是"规整判断自己不该上，该让回收先干"** ——
空闲页太少，连做迁移用的目标页都凑不出来。这从代码层面印证了慢路径里
**reclaim 在前、compaction 在后**（计划书 §1.1 强调的顺序）。

### 3.5 白送的两张表（在 `format` 的 `print fmt` 里）

- `end/format` 的 `__print_symbolic` = 现成的 status 枚举映射表
- `try_to_compact_pages/format` 的 `__print_flags` = **完整的 GFP 标志位表连数值**：
  `__GFP_DIRECT_RECLAIM=0x400`、`__GFP_KSWAPD_RECLAIM=0x800`、`__GFP_IO=0x40`、
  `__GFP_FS=0x80`、`__GFP_MOVABLE=0x08`、`__GFP_RECLAIMABLE=0x10`、
  `__GFP_NORETRY=0x10000`、`__GFP_RETRY_MAYFAIL=0x4000`，
  以及 `GFP_TRANSHUGE` / `GFP_TRANSHUGE_LIGHT` / `GFP_KERNEL` / `GFP_ATOMIC` 的组合值。

  **P0 直接能用**：在用户态把 `gfp_mask` 解析成人话（比如判断这次是不是 THP 分配），
  不用去翻 `include/linux/gfp.h`。

### 3.6 一个必须记住的坑：offset 里有 padding

`end` 的 `sync` 在 offset **40**、size 1，`status` 在 **44** ——
中间 41/42/43 是编译器为 int 对齐插的 padding。

**自己照字段列表手抄结构体去解析，会把 `status` 算成 offset 41，读出来全错。**
BCC 的 `args->字段名` 是拿 format 里的 offset 自动生成的，安全。
**结论：永远用 `args->`，永远不要自己算偏移。**

### 3.7 ★ 本机内核栈**不是** order-2 连续块（2026-08-09 实测，与计划书不符）

```
$ grep -E "^CONFIG_(VMAP_STACK|HAVE_ARCH_VMAP_STACK)" /boot/config-5.15.0-139-generic
CONFIG_HAVE_ARCH_VMAP_STACK=y
CONFIG_VMAP_STACK=y
```

计划书与交接文档都写"线程内核栈是 order-2 UNMOVABLE 连续块"，**本机不成立**。
开了 VMAP_STACK 后内核栈走 vmalloc：16KB 的栈 = **4 个互不相邻的 order-0 页**，
靠页表映射成连续虚拟地址，**物理上完全不连续**。

**实测证据**（`./kstack 3000`，见 `fraginfo_v2_record.md` 步骤 2.5）：

| 指标 | 增量 | 说明 |
|---|---|---|
| `KernelStack` | +48096 kB | 3000 × 16.03 KB，每线程 16KB |
| `VmallocUsed` | +48016 kB | ★ 与上一行几乎完全相等 = 内核栈就在 vmalloc 空间里 |
| `SUnreclaim` | +19960 kB | task_struct 等，不可回收 |
| `SReclaimable` | **0** | 一点没动 → 造出来的污染全在不可回收侧 |

**对项目是好消息**：一个 order-2 连续块最多毁掉 1 个 pageblock 的连续性；
4 个散落的 order-0 UNMOVABLE 页可能毁掉 **4 个不同的 pageblock**。
规整能否凑出 order-9（512 个连续页），取决于这 512 页里有没有搬不走的页 ——
**搬不走的页越分散，杀伤力越大**。

**衍生约束**：VMAP_STACK 有 per-CPU 栈缓存（`NR_CACHED_STACKS=2`），
线程退出时栈会被缓存复用。所以 `kstack.c` **必须让线程一直活着**；
反复 create/join 会一直命中缓存，根本不向伙伴系统要新页 = 等于没压。

**★ `报告_P0.md` 必须写这条偏离**，不能沿用"order-2 内核栈"的说法。

### 3.9 ★ 实测：规整成功率有个悬崖（P-1 阶段的核心发现）

两轮实验唯一的差别是**压到多深**：

| | 压到"分配开始失败" | 停在"每批还能拿满" |
|---|---|---|
| `compact_stall` 增量 | 42 | 402 |
| **规整成功率** | **0.0%** | **92.0%** |
| `free/migrate` 扫描比 | **0.81** | **7.36** |

**只要分配还能被满足，规整基本都成功；一旦分配开始失败，规整完全失效。
中间没有平滑过渡。**

扫描比的解读（这张表可直接用于 P0/报告）：

| 扫描比 | 含义 |
|---|---|
| **> 2** | 空闲页扫描器要翻很远才找到落脚点 —— "空位难找" |
| **≈ 1** | 两个扫描器扫描量接近，大致对称相遇 |
| **< 0.5** | 迁移页扫描器要翻很远才找到搬得动的页 —— "**搬得动的页难找**"，UNMOVABLE 占比高 |

复现性：两轮独立测得的"白拿库存"1652 vs 1630，**相差 1.3%**。

### 3.10 ★★ 用 /proc/vmstat 做实验的三条铁律（都是踩坑踩出来的）

1. **必须用"结束绝对值 − 开始绝对值"**。机器不一定是干净的 ——
   第三轮实验时 `compact_stall` 基线已经是 938（前一轮留下的），
   脚本报的"980"是累计值，本轮真实增量只有 42。**差了 23 倍。**
2. **必须做恒等式自检**：`compact_stall = compact_success + compact_fail`。
   实测在累计值和单轮增量上都成立，是最好用的一条自检。
3. **不许相信单次采样**。曾出现单调计数器 **−447** 的增量、
   某增量恰好等于该计数器绝对值。**机制至今未查清**
   （bash + seq_file 的假设已做实验证伪：造 pgfault 每秒 2.5 万的负载，
   `while read < /proc/vmstat` 连读 200 次异常 0 次）。
   对策是四道防线：采样完整性校验、符号校验、恒等式校验、双路对账。

> **观测工具的底线不是"永不出错"，是"出错时不许伪装成正确"。**
> 这条直接决定 P0 的设计：未配对率必须报出来、必须和 `/proc/vmstat` 交叉对账。

### 3.8 hugetlb 能从 DMA32 拿页（解释了"前几批不触发规整"）

hugetlb 池扩容的 GFP 是 `GFP_HIGHUSER_MOVABLE`，**DMA32 在允许的 zonelist 里**。
本机 DMA32 有 **743 个 order-10 块**（≈2.9 GB，≈1486 个潜在大页）常年闲置。
所以前 1000 多个大页请求都能从空闲链表直接摘走，**根本不进慢路径**。

→ 要逼出 direct compaction，必须把 **Normal 和 DMA32 两个 zone 的高阶块都耗掉**，
只压 Normal 是不够的。

---

## 四、已定的决策（不要再重新讨论）

| # | 决策 | 理由 |
|---|---|---|
| 1 | **P0 和 P1 分两个 `.c`，不许合并** | BCC 是加载时现场编译；合并=只想看 reclaim 也得挂上 compaction 埋点，白付开销；分开调试报错定位也清楚 |
| 2 | **`tools/fragstress/` 下是普通用户态 C，不能放进 `src/bpf/`** | `src/bpf/` 只放 BCC 加载的内核态源码，混进去会让人以为压力注入器也是 eBPF |
| 3 | **不新开 Python 文件**，在 `extfrag.py` 里加分支 | 用户明确要求 |
| 4 | **展示层不做深**：内核态数据能正确打印就够，不做 TUI/曲线/颜色 | 不是加分项 |
| 5 | map key 用 **tid**（`bpf_get_current_pid_tgid()` 完整 u64），不是 `>>32` 的 tgid | compaction / reclaim 都是**线程**行为；同进程两线程同时进慢路径会互相覆盖 |
| 6 | `try_to_compact_pages` 是**必需**外层埋点 | `order` 只有它有；begin/end 是 per-zone 的，不是"一次规整"的边界 |
| 7 | **三重来源过滤必须在内核态做** | direct / kcompactd / 手动 `compact_memory` 共用同一组埋点，不过滤统计全脏；捞到用户态再扔是白付开销 |
| 8 | `BPF_LRU_HASH` 兜底 + **必须加 `unpaired` 计数器**，输出报未配对率 | LRU 不是纯优化，是拿"内存泄漏风险"换"静默丢事件"，丢掉的恰好是长尾 |
| 9 | 扫描页数**不用 `free_pfn - migrate_pfn` 硬算** | 扫描器会重启，会算出负数；用 `migratepages` 的 nr_migrated/nr_failed 累加 |
| 10 | 限流沿用 v1 已修正的写法：**固定 `key=0`** | v1 曾有过 key 用变化值的 bug，已修 |
| 11 | 代码注释和输出**全部用中文** | 和 v1 保持一致 |
| 12 | 慢路径顺序：retry 循环里**先 reclaim 后 compaction**；只有 costly order（order>3）在进循环前有一次提前规整 | 必被面试追问的细节 |
| 13 | **`extfrag.py` 加 `mode` 参数决定加载哪个 `.c`**（`frag`/`extfrag`/`compact`/`reclaim`），外加一个十几行的 `if __name__ == "__main__":` argparse 入口做文本打印。不新开文件、不碰 `extfrag_user.py` | 2026-08-11 用户拍板"加入 mode 参数（简易版）"。理由：`compactinfo.c` 必须能独立跑起来验证，否则每次调试都要绕道 curses TUI |
| 14 | **fragstress 剩余档位（`sockflood.c`/`dentry.sh`/`thpload.c`/档位4）暂不补** | 硬门槛已过（402 次 direct compaction），压力基座够用；优先级低于 P0 |

---

## 五、待确认 / 未决问题（下一个会话要处理）

### 5.1 ★ 用户还没回复的两个确认（阻塞开工）

**（1）阶段一的施工步骤顺序，用户尚未点头。** 已列给用户的版本：
```
Step 1 埋点字段核对（✓ 已完成）
Step 2 写 tools/fragstress/ 四档压力注入器
Step 3 验收：compact_stall 必须能顶上去且持续增长（硬门槛）
Step 4 写 src/bpf/compactinfo.c
Step 5 extfrag.py 加分支加载
Step 6 交叉验证（vmstat 对账 + PSI 量级比对）
Step 7 出 报告_P0.md
```

**（2）`extfrag.py` 怎么加 CLI 分支。**
问题背景：`extfrag.py` 是**纯类库**（只有 `ExtFrag` 类，没有 main、没有 argparse），
命令行和展示都在 `extfrag_user.py` 的 curses TUI 里。compaction 数据是事件流+直方图，
塞进那个 curses 表格布局不合适，而用户又要求展示层"能打印出来就够"。

**上一个会话的建议（等用户拍板）**：给 `ExtFrag` 加 `mode` 参数
（`frag` / `extfrag` / `compact` / `reclaim`）决定加载哪个 `.c`，
并在 `extfrag.py` 末尾加一个 `if __name__ == "__main__":` 的简易 argparse 入口，
直接文本打印——不新增文件、不碰 `extfrag_user.py`。

### 5.2 遗留问题

| # | 问题 | 影响 | 怎么解 |
|---|---|---|---|
| 1 | **本机没有内核 C 源码**，只有头文件 | 计划书 D1 要求读 `mm/compaction.c` 的 `compact_zone()` 主循环，做不了 | `sudo apt install linux-source-5.15.0` 或 `apt-get source linux-image-unsigned-$(uname -r)` |
| 2 | **存疑A**：`count_vm_event(COMPACTSTALL)` 是否在 `compact_result == COMPACT_SKIPPED` 时提前 return 不计数 | 若属实 → **外层次数 ≥ `compact_stall`**，差值恰是 SKIPPED 那些。直接决定交叉验证表怎么解释偏差 | 装源码后看 `mm/page_alloc.c` 的 `__alloc_pages_direct_compact()` |
| 3 | **存疑B**：direct compaction 整段是否被 `psi_memstall_enter/leave()` 包住 | 若属实 → PSI 的 `total` 和本工具统计的阻塞时长**同源**，量级必须对得上；对不上说明配对逻辑有问题 | 同上 |

> 存疑 A/B 是上一个会话**凭对 5.15 上游代码的记忆**写的，**未在本机核实**。
> 在核实之前，不要把它们当作事实写进任何报告。

---

## 六、文件地图

```
/home/xxy/wlsp/Linux物理内存碎片检测/          ← Git 仓库根
├── handoff/
│   ├── com_memory.md          ← 本文件：跨会话共享记忆
│   └── handoff_task.md        ← 交接任务书：下一步干什么
├── fraginfo_v2.md             ← ★ 计划书，唯一施工依据，不要改
├── fraginfo_v2_record.md      ← ★ 施工记录 + 知识点笔记，每步追加
├── fraginfo_v2_draft1.md      ← 初稿存档，★ 不要照它做，技术错误已在正式版修正
├── 交接_执行员.md              ← 用户给 AI 的角色说明书
├── 项目指南.md / 笔记.md       ← v1 时期的资料
├── 报告_P0.md                 ← ☐ 阶段一结束时产出（还不存在）
├── 报告_P1.md                 ← ☐ 阶段二结束时产出（还不存在）
└── 源码/
    └── src/
        ├── bpf/
        │   ├── fraginfo.c       ← v1，kprobe get_page_from_freelist，★不要动
        │   ├── extfraginfo.c    ← v1，tracepoint kmem:mm_page_alloc_extfrag，★不要动
        │   ├── compactinfo.c    ← ☐ P0 新增（还不存在）
        │   └── reclaiminfo.c    ← ☐ P1 新增（还不存在）
        ├── extfrag.py           ← v1 类库，要加 mode 分支
        ├── extfrag_user.py      ← v1 curses TUI，本次不动
        └── tools/fragstress/    ← ☐ P-1 新增，普通用户态 C（还不存在）
```

---

## 七、Git 与多机协作约定

**远端**：`https://github.com/2862426199qq-beep/linux_eBPF.git`（分支 `main`）
**仓库根** = `/home/xxy/wlsp/Linux物理内存碎片检测/`
**git 身份**：`pc` / `2862426199@qq.com`

规则：
1. **开工先 `git pull`**，收工必 `git commit && git push`。仓库是两台电脑之间唯一的同步渠道。
2. `handoff/com_memory.md` 和 `fraginfo_v2_record.md` **每次有实质进展都要更新并提交**——
   它们是下一个会话的输入。
3. `.doc` / `.pdf` / `.png` 这些资料已在仓库里，正常提交即可（总共约 4.7 MB）。
4. `__pycache__/`、`*.pyc` 已在 `.gitignore` 里，不要提交。
5. commit message 用中文，说清"做了什么 + 为什么"，例：
   `P-1: 新增 fragstress 四档压力注入器，档位2用海量线程污染 pageblock`

---

## 八、更新日志（每个会话收工时追加一行）

| 日期 | 会话 | 干了什么 |
|---|---|---|
| 2026-08-02 | 会话 1 | 读完交接手册+计划书+v1 源码；实测环境事实（确认三个 0）；实测 compaction 四个埋点 format，发现 `status` 有 9 个取值而计划书只列 5 个；建立 `fraginfo_v2_record.md` |
| 2026-08-04 | 会话 1 | 给用户补讲三块基础（碎片与规整 / eBPF 与埋点 / 项目全貌与进度），已存入 `fraginfo_v2_record.md` 附录；建立 `handoff/` 两份交接文档；初始化 Git 仓库并推送 |
| 2026-08-09 | 会话 2 | 用户拍板"先打通最短链路"。写 `fragstress/` 的 holes.c / kstack.c / hugetlb.sh / Makefile；实测发现 `CONFIG_VMAP_STACK=y`（内核栈不是 order-2，见 3.7）；第一次手工压力实验把 `compact_stall` 从 0 顶到 938 |
| 2026-08-10 | 会话 2 | 写 `run.sh` 把时序从人脑挪进代码；踩坑并修掉：sudo+后台被 SIGTTIN 停住、stdio 全缓冲让就绪标记等不到、`holes` 天然够不着 DMA32（→ 加"库存自检"）、bash nameref 数组下标错误让循环在跨过库存线前一刻静默终止 |
| 2026-08-11 | 会话 2 | **P-1 硬门槛通过**：402 次 direct compaction，跨 3 批持续增长，两套独立算法对账一致。发现"规整成功率悬崖"（见 3.9）与 vmstat 三条铁律（见 3.10）。给 `run.sh` 加四道自证防线。写 `fragstress/README.md`（复现步骤 + 全部踩坑记录） |
| 2026-08-11 | 会话 2 | 写完 P0 内核态 `src/bpf/compactinfo.c` + `extfrag.py` 的 `mode` 分支与文本输出，**首次加载编译通过**（空闲机器上计数全 0，符合预期）。**尚未做"一边挂探针一边施压"的真实验证** ← 明确未完成项。用户要求补一份脱离实验环境的复盘学习任务书 → 见第九节 TASK_P0 |
| 2026-08-13 | 会话 4 | 讲完**站⑤**（用户点名三处不懂：推论1 / 方案a 的源码佐证 / 决策3 过滤源 → 全部重讲并补进 9.6，含"探针间只能靠 map 传数据"这条地基和 T4−T1 vs T3−T2 两张直方图的分工）。**站⑥ 考了第 1~12 题**（成绩与逐题缺口见 9.7 考核记录），新记录理解偏差 3/4/5 与"答'是什么'不答'所以呢'"这个模式。用真源码逐行核对慢路径，补进 9.5：**提前规整是两种条件不只 costly**、水位线 LOW→MIN 的含义、:5121 那条"回收没进展就不许重试规整"的注释（"规整依赖回收"最硬的源码证据）。**★ 修正一条自己之前说错的话**：`compact_stall == outer_enter` **不是恒等式**，`page_alloc.c:4409` 有 `COMPACT_SKIPPED` 早退 → 精确式带减法（见 9.6 ①附、9.9）。修正上一轮口头讲错的时间线（`end` 探针并不取 order，只有 kretprobe 取）|
| 2026-08-15 | 会话 5 | 写完 **`报告_P0.md`**（612 行，阶段一交付物，7 节，数据全部标注【原始】/【转录】，三处数据缺失如实记录）。定稿**简历描述**（`简历描述_v1v2.md` 顶部"★ 简历定稿"节，含两处"与代码现状的出入"待补）。**★ 补全 9.8：站⑥ 18 题全文答案**（每题"一句话 / 展开 / 常见错答"三段，把 9.7 考核记录里的逐题缺口全部内联进答案，可脱离实验环境自测自批）。由 P0 数据推出 **P1 前置硬门槛**（`pgscan_direct = 0`，现有压力形态触发不了 direct reclaim），已写进项目记忆与报告 §7.5 |
| 2026-08-12 | 会话 3 | **★ P0 真实验证通过**：eBPF `outer_enter` 与 `/proc/vmstat compact_stall` **同为 377，完全相等**；三项硬检查全过。完整数据与新发现见 **9.9**。带用户走完 TASK_P0 站①②③④（站⑤⑥未完成，见 9.1 进度列）。修掉 `run.sh` 逐批段一处写死的扫描比解读（潜在谎报）。核对 `compaction.h` 真源码（本机可读），9 个 enum 值 + 4 个判定函数**从"照记录抄"升级为"源码核对过"**。开始装 `linux-hwe-5.15-source-5.15.0` 以解决存疑 A/B |

---

# 九、TASK_P0：复盘学习任务（★ 可在无实验环境的机器上完成）

> **这一节是给用户本人看的自学任务书，不是给 AI 的施工说明。**
>
> **背景**：P-1 与 P0 的实验全部由 AI 执行，用户"走马观花没有吸收"。
> 需要重新回顾：**实验过程 → 现象结果 → 源码分析**。
>
> **约束**：学习用的那台机器**没有实验环境**（没有 BCC、没有这台虚拟机的
> `/proc`、没有内核头文件）。所以本节把**所有数据、所有源码逻辑、所有结论
> 都内嵌在文档里**，全程只需要 `git clone` 下来的这个仓库 + 一个文本编辑器。
> 任何一条"你去跑一下看看"都是本节的失败。

## 9.0 先记住这一句

> **v1 回答"内存有多碎"，v2 回答"所以呢"。**

v1 是**状态观测**：现在有多碎（碎片指数）、谁在制造碎片（fallback 归因）。
v2 是**代价量化**：碎片把分配从快路径逼进慢路径之后，
**谁**被同步阻塞了**多久**、结局如何、这笔账该算给谁。

面试被问"你这个项目解决了什么问题"时，v1 的答案是"能看见碎片"，
v2 的答案是"能算出碎片的**代价**"。后者才是有说服力的那个。

---

## 9.1 学习路线图（建议按序，每一站都有明确的"过关标准"）

| 站 | 主题 | 读什么 | 过关标准 | 进度 |
|---|---|---|---|---|
| ① | 为什么需要 v2 | 本节 9.2 | 能说清"三个 0"是什么、为什么它让 v1 显得浅 | ✅ 2026-08-11 |
| ② | 压力是怎么造出来的 | `fragstress/README.md` + `holes.c` + `kstack.c` | 能说清为什么"随机释放"是灵魂、为什么内核栈能钉死 pageblock | ✅ 2026-08-12 |
| ③ | 实验时间线与四次失败 | 本节 9.4 | 能复述每一次失败的**根因**，不是"改了个 bug" | ✅ 2026-08-12 |
| ④ | 慢路径的内核源码逻辑 | 本节 9.5 | 能画出 `__alloc_pages_slowpath` 的顺序，说清 reclaim 与 compaction 谁先谁后 | ✅ 2026-08-12（有 2 处待纠正，见下） |
| ⑤ | P0 探针的设计取舍 | `bpf/compactinfo.c` 的顶部大注释 + 本节 9.6 | 能说清"为什么要 kretprobe"和"三重来源过滤怎么做" | ✅ 2026-08-13（讲完后用户点名三处不懂，已重讲并补进 9.6）|
| ⑥ | 自测 | 题目 9.7 / **答案 9.8** | 能不看文档回答全部问题 | ◐ 1~12 已闭卷考过（缺口见 9.7 考核记录）；**13~18 只讲过、未考** ← 下次从这里开始。**18 题全文答案已在 9.8，可自测自批** |

**时间预算**：①②③ 约 2 小时，④ 约 2 小时（最硬的一站），⑤ 约 1.5 小时，⑥ 1 小时。

### ★ 站①~⑥ 学习中暴露的理解偏差（复习时重点看）

用户答检查题时答错的地方，**大多是容易反过来记的**，写在这里以免重复踩。
**下一个会话应当抽查偏差 2 和偏差 3。**

**偏差 1：以为"只跑 `holes.c` 不跑 `kstack.c`，`compact_stall` 也是 0"。**
错。`compact_stall` 的触发条件**只是"要不到高阶块"，跟页搬不搬得动无关**，
而 `holes.c` 一个人就足够把高阶块砸光（实测 Normal order-10 从 616 → 14）。
正确的分工是：

| 计数器 | 触发条件 | 靠哪一档 |
|---|---|---|
| `compact_stall` | **要不到**高阶块 | 档位 1（造碎片）+ 档位 3（提需求） |
| `compact_fail` | 挪了**也凑不出来** | **档位 2**（造不可迁移的钉子） |

**偏差 2：把 free/migrate 扫描比的方向记反了**（以为压力大 → 瓶颈在 free 侧）。
正确的记法只有一句：

> **哪一侧要找的东西稀缺，那一侧就是瓶颈，它的扫描数就爆。**

- 压力**轻** → 稀缺的是**连续空位**（可搬的匿名页遍地都是）→ free 侧爆 → 比值**大**
- 压力**重** → 稀缺的是**搬得动的页**（全被内核栈/slab/已分配大页占了）→ migrate 侧爆 → 比值**小**

"压力大 = 能搬的页很多"是错的：压力大恰恰意味着**能搬的页已被消耗殆尽**。

**★ 2026-08-13 追记：这条用户一共答反了三次**（第三次才对）。
根治办法是**别背结论，背推法** —— 从"内核把非空闲页往**高地址**搬"这一条出发：
搬走的页从低处开始找（migrate 低→高），落脚点从高处开始找（free 高→低），
**相向而行、相遇即收工**（同向就没法定义"扫完了"）。
两个方向和比值的含义都能从这一条推出来。

**偏差 3：以为"顺序释放的问题是很快被 migrate 扫描器搬走"。**
错，而且错在时间点上：**顺序释放在 `free()` 返回之前就已经失败了，活不到规整那一步。**
`__free_one_page()` 每释放一个页就立刻检查它的**伙伴**是否也空闲，是就当场合并升阶：

```
顺序释放 PFN 100,101,102,103 → 释放 101 时 100 已空闲，当场合并成 order-1
                             → 103 时 102-103 合并，继而 100-103 合成 order-2
                             → 一路向上，free list 原封不动恢复 → 什么都没破坏
随机释放 PFN 100, 7, 253, 61 → 每个的伙伴都还被占着 → 一个都合不了 → 高阶块永久打散
```

**★ 而且"相邻"这个词不准，面试会被追**：合并要求的是**伙伴（buddy）**关系，
由 PFN 异或决定 `buddy_pfn = pfn ^ (1 << order)`。
反例：order-0 的 PFN **1 和 2 相邻但不是伙伴**（1 的伙伴是 0，2 的伙伴是 3），
两个都空闲也不合并。
推论：**随机释放的破坏力是概率性的、不是 100%** —— 释放得多总会撞上成对的。
这也是 `holes.c` 只随机释放一部分（而不是全部）的原因：全释放等于把内存还回去。

**偏差 4：把 UNMOVABLE 的作用理解成"增大压力"。**
太糊，把两件事混成一件了。准确的分工：

| 注入器 | 洞的类型 | 规整能处理吗 | 后果 |
|---|---|---|---|
| `holes.c` | **MOVABLE**（用户态匿名页） | 搬得走 | **卡一下但会成功** → `compact_stall` 涨，成功率仍高 |
| `kstack.c` | **UNMOVABLE**（内核栈） | **搬不动** | **规整失败** → 成功率掉下去 |

> **UNMOVABLE 页的作用不是让规整变慢，是让规整彻底失败。**
> 一个搬不动的页钉在 pageblock 里，整个 pageblock 就永远凑不成高阶块 ——
> 把周围全搬干净也没用。这就是 3.9 那个成功率悬崖（92%→0%）必须靠 `kstack.c` 的原因。

**偏差 5：把"三个 0"记成 `compact_stall` / `compact_fail` / ?。**
错。三个 0 是 `compact_stall` / **`pgscan_direct`** / `/proc/pressure/memory`，
挑的是**三个互相独立的维度**（规整 / 回收 / 压力）。
`compact_fail` 不算独立信息 —— `compact_stall` 都是 0 了它必然是 0。
**而且最容易漏的是那条推论**：三个 0 的机器上**探针写得再对也是空表**，
连"代码对不对"都验证不了 → **所以 v2 第一步不是写探针，是先造出真实的代价（P-1）**。
（这正是用户 2026-08-12 那个 meta 问题"为什么不像 v1 那样直接做工具"的答案根源。）

**另外，站④原文里有一处数字被实测打回**：原写"快/慢路径频率差六个数量级"，
实测是**三个**（`pgalloc_*` 空闲机器 988 次/秒；`holes` 触碰 4GB 时 ≈24700 次/秒；
direct compaction 峰值 ≈20 次/秒）。但真正的分界不在倍数，而在：
**快路径永远在跑，慢路径在健康机器上从不跑**（本机开机至今为 0）。

---

## 9.2 站①：为什么需要 v2 —— "三个 0"

项目开工时在开发机上实测：

```
compact_stall     0        ← 从没有进程因为要连续内存而被同步卡住过
pgscan_direct     0        ← 从没有进程因为内存不足而被拉去做回收
/proc/pressure/memory  some total=0  full total=0    ← 从没有内存压力
```

**这三个 0 意味着：v1 观测的那些"碎片指数"，从来没有真正造成过任何后果。**

于是 v1 的处境是：能报出"碎片指数 0.85"，但答不上来"0.85 会让谁慢多少"。
**这就是"只答了有多碎，没答所以呢"。**

→ 所以 v2 的第一步不是写探针，而是**先造出真实的代价**（P-1 阶段）。
   在一台"三个 0"的机器上，探针写得再对也是空表。

**要能回答**：为什么不能直接写探针？（答：没有事件可测，连"代码对不对"都无法验证）

---

## 9.3 站②：压力注入器的三个核心原理

详细版在 `源码/src/tools/fragstress/README.md`，这里只列必须记住的三条。

### ① `holes.c`：为什么"随机释放"是全部灵魂

```
顺序释放 → 还回去的页彼此相邻
         → 伙伴系统的 __free_one_page() 一路向上合并
         → order-0 合成 order-1、order-1 合成 order-2 …
         → 最后又变回大块，白干

随机释放 → 还回去的页的"伙伴"(buddy) 大概率还被占着
         → 合并在第一步就断了
         → 空闲页只能以小块挂在低 order 链表上  ← 这才是外部碎片
```

实测（只用了 512 MB 就打出效果，Normal zone）：

| order | 施压前 | 施压后 |
|---|---|---|
| 0 (4KB) | 6758 | 7396 ↑ |
| 7 (512KB) | 65 | 11 |
| 8 (1MB) | 45 | **3** |
| 9 (**2MB**) | 12 | **3** |

**高阶块被砸碎、碎屑堆到低阶 —— 这就是外部碎片的教科书形态。**

### ② `kstack.c`：为什么必须用内核对象来污染

用户程序申请的匿名页 GFP 是 `GFP_HIGHUSER_MOVABLE` —— **MOVABLE，可迁移**。
内核一规整就把它们搬走，连续块又凑出来了。所以光有 `holes.c`，
结果会是"`compact_stall` 涨了，但 `compact_fail` 始终是 0"，测不到真正的代价。

**真正钉死 pageblock 的是内核自己的对象**：内核代码里到处存着指向它们的
直接映射地址，一搬走指针全废，所以内核**根本没有实现**它们的迁移 ——
规整扫描器碰到就只能绕开，这个 pageblock 永远凑不出 order-9。

实测（`./kstack 3000`，3000 个永久睡着的线程）：

| 指标 | 增量 | 说明 |
|---|---|---|
| `KernelStack` | +48096 kB | 3000 × **16.03 KB**，每线程 16KB 内核栈，分毫不差 |
| `VmallocUsed` | +48016 kB | ★ 与上一行**几乎完全相等** |
| `SUnreclaim` | +19960 kB | task_struct 等，**不可回收** |
| `SReclaimable` | **0** | **一点没动** |

**第二行是关键证据**：内核栈的增量原封不动体现在 vmalloc 用量上
→ 证明本机 `CONFIG_VMAP_STACK=y`，**内核栈不是 order-2 连续块，
而是 4 个互不相邻的 order-0 页**，靠页表映射成连续虚拟地址。

**这对项目更有利**（面试可讲的点）：一个 order-2 连续块最多毁掉 1 个 pageblock；
4 个散落的 order-0 UNMOVABLE 页可能毁掉 **4 个不同的 pageblock**。
规整能否凑出 order-9（512 个连续页），取决于这 512 页里有没有搬不走的 ——
**搬不走的页越分散，杀伤力越大**。

12000 线程实测让 Normal zone 的 **Unmovable pageblock 从 287 涨到 393（+106）**。

**衍生约束**：VMAP_STACK 有 per-CPU 栈缓存（`NR_CACHED_STACKS=2`），
线程退出时栈会被复用 → 所以 `kstack.c` **必须让线程一直活着**，
反复 create/join 会一直命中缓存，等于没压。

### ③ `hugetlb.sh`：为什么用大页池而不是 THP

**order-0 的分配永远不会触发规整** —— 单页哪儿都有。必须有人要大块。

```
echo N > /proc/sys/vm/nr_hugepages
  → set_max_huge_pages() → alloc_pool_huge_page() → __alloc_pages(order=9)
  → 快路径 get_page_from_freelist() 拿不到 order-9
  → 进 __alloc_pages_slowpath()
  → order 9 > 3 属于 costly order，先来一发 direct compaction
  → try_to_compact_pages()      ← ★ P0 埋点就在这儿
  → compact_stall++
```

关键：这条路径是**同步**的 —— 写 sysctl 的那个 shell 被卡在内核里直到规整结束。

不用 THP 的原因：本机 `defrag=[madvise]`，只有显式 `madvise(MADV_HUGEPAGE)`
的区域失败时才做**同步**规整；普通程序失败了就悄悄退化成 4KB 页、
顺手唤醒 kcompactd 走人 —— 那是**异步**路径，**不算 `compact_stall`**。

### ★ 一个必须记住的陷阱

```bash
echo 1 > /proc/sys/vm/compact_memory     # ← 不能用它验证 direct compaction
```

它走**手动规整**路径（`compact_node`），会正常打出 `mm_compaction_begin/end`
埋点，看起来一切正常，**但不增加 `compact_stall`** —— 压根没有进程在等分配。
**它只能验证"埋点挂没挂上"，不能验证 direct compaction。**

---

## 9.4 站③：实验时间线 —— 四次失败，每次都是一类不同的错

**这一站是本任务书最有价值的部分。** 不是看结论，是看"为什么第一次没成功"。
面试里"讲一个你踩过的坑"，这四个任选一个都够用。

### 第一次（手工执行）：成功了，但过程失控

三个终端手工按 kstack → holes → hugetlb 的顺序跑，`compact_stall` 从 0 顶到 **938**。

但事后发现两个问题：

1. **网卡闪断把压力源杀了。** `dmesg` 里满屏 `e1000: ens33 NIC Link is Down`，
   终端掉线 → SIGHUP → `holes` 和 `kstack` 双双死亡。
   **而实验还在继续跑，数据静悄悄地失真。**
2. **人肉掐时序不可靠**（用户原话："执行时机拿捏不准"）。

→ 催生了 `run.sh`：把时序写进代码，每步轮询上一步的**就绪标记**而不是 `sleep` 猜。

### 第二次失败：`compact_stall` 全程 0 —— 目标值定低了

10 批大页申请，`compact_stall` **每批都是 0**。查四个快照，发现
**DMA32 那一行在 A/B/C 三个时间点逐字不变**：

```
A_基线    DMA32  115  79  79  66  61  48  43  45  37  39  705
B_kstack后 DMA32  115  79  79  66  61  48  43  45  37  39  705   ← 完全没变
C_holes后  DMA32  115  79  79  66  61  48  43  45  37  39  705   ← 还是没变
D_hugetlb后 DMA32 115  79  79  66  61  48  43  45  37   1   40   ← 大页全从这儿摘走
```

**根因**：内核给用户匿名页分配内存时**优先从最高的 zone 拿（Normal）**，
只有 Normal 快见底才 fallback 到 DMA32。`holes` 只要 4 GB，
而当时 `MemFree` 还有 5.9 GB —— Normal 从没紧张过，
所以 DMA32 的 **705 个 order-10 完整块（≈1410 个大页）**一直闲着。

而 hugetlb 池扩容的 GFP 是 `GFP_HIGHUSER_MOVABLE`，**DMA32 在允许的
zonelist 里** → 2000 个大页全从那儿白拿走了，一次规整都不用做。

**当时的"白拿库存"≈ 2087 个，目标定的 2000 —— 正好够，差 88 个就跨过门槛。**

**修法**：光加大 `holes` 治不了本，它天然够不着 DMA32。
正确做法是**先算库存，再把目标顶到库存之上**：

```bash
# order-9 块 = 1 个 2MB 大页；order-10 块 = 4MB = 2 个大页
# buddyinfo 第 14、15 列分别是 order-9、order-10 的块数
INVENTORY=$(awk '/^Node/ { n += $14 + 2*$15 } END { print n+0 }' /proc/buddyinfo)
NEED=$(( INVENTORY * 14 / 10 + 200 ))      # 目标至少比库存高 40%
CAP=$(( (MemAvailable_MB - 800) / 2 ))     # 但要留 800MB 余量，别 OOM
```

> **这一次失败的教训不是"参数调错了"，而是"我以为我在施压，其实压力
> 根本没落到该落的地方"。** 观测类工作里，"确认压力真的生效"和
> "确认观测真的准确"是同等重要的两件事。

### 第三次失败：脚本在即将出结果的前一刻静默崩掉

日志最后一行是 `✗ 硬门槛未过：compact_stall 全程为 0`，读起来像压力策略不行。
实际是：

```
./run.sh: 行 98: _dst["$k"]: 数组下标不正确
停止原因：达到目标数量 2791          ← ★ 纯粹的谎报
```

递增循环在第 10 批终止，**恰好停在 1800 个大页，而当时库存是 1851** ——
死在了"再要一批就必须规整"的前一刻。而收尾逻辑照常打印"达到目标数量 2791"。

**这是观测类项目最要命的失效模式：工具坏了，但它报告说自己好着呢。**

**修法与更重要的原则**：

| 防线 | 做法 |
|---|---|
| 采样完整性 | 校验想要的 10 个计数器**一个不少**，缺了就报错并标记该批不可信 |
| 符号校验 | 单调递增计数器**不许出现负增量** |
| 恒等式校验 | **`compact_stall = compact_success + compact_fail`**（一次规整要么成功要么失败） |
| 双路对账 | 总量**另外从绝对值算一遍**，与逐批累加互相对账，不一致以前者为准 |
| 退出自证 | 循环退出时打印 `cur / HP_TARGET / INVENTORY` 三个真实值，让"提前退出"没法伪装成"正常完成" |

> **观测工具的底线不是"永不出错"，是"出错时不许伪装成正确"。**
>
> 这句话直接决定了 P0 的设计：未配对率必须报出来、
> 必须和 `/proc/vmstat` 交叉对账、报告里不许写"效果良好"。

**★ 附一条诚实记录**：AI 当时提出的解释是"bash 按块读 + lseek 回退与
seq_file 冲突"，然后**做实验证伪了**（造 `pgfault` 每秒 2.5 万的背景负载，
`while read < /proc/vmstat` 连读 200 次，异常 0 次）。
**机制至今未查清，没有编一个解释顶上去。**
这也是一种能力：**分得清"实测的"和"我猜的"**。

### 第四次（成功）：干净数据

零可疑行、零缺键、零报错，**两套独立算法互相印证**：

| | 权威总量（绝对值相减） | 逐批累加 | 一致性 |
|---|---|---|---|
| `compact_stall` | 402 | 402 | 完全一致 |
| 规整成功率 | 370/402 = 92.0% | 92.0% | 完全一致 |
| `pgscan_direct` | 4409 | 4409 | 完全一致 |
| `compact_migrate_scanned` | 314873 | 314539 | 差 0.1%（批次间隙的活动） |

逐批表（**这张是报告和简历的主表**）：

| 批 | 目标 | 实得 | 耗时 s | stall | success | fail | kcompactd | migr_scan | free_scan |
|---|---|---|---|---|---|---|---|---|---|
| 1 | 1467 | 1467 | 10.06 | **0** | 0 | 0 | 0 | 0 | 0 |
| 2 | 1667 | 1667 | 3.27 | 64 | 57 | 7 | 13 | 72357 | 74643 |
| 3 | 1867 | 1867 | 5.81 | 159 | 142 | 17 | 25 | 120973 | 1643387 |
| 4 | 2067 | 2067 | 3.34 | 179 | 171 | 8 | 23 | 121209 | 600841 |

**第 1 批全 0，正是"库存自检"预测的白拿区；第 2 批越过库存线，规整立刻出现。**
—— 一个能**预测**自己什么时候会看到现象的实验，比"碰巧看到了"强得多。

### ★★ 核心发现：规整成功率有个悬崖

两轮实验的差别**只在于压到多深**：

| | 压到"分配开始失败" | 停在"每批还能拿满" |
|---|---|---|
| `compact_stall` 增量 | 42 | 402 |
| **规整成功率** | **0.0%** | **92.0%** |
| `compact_isolated` | 179685 页（≈700MB） | 457047 页 |
| `free/migrate` 扫描比 | **0.81** | **7.36** |

**只要分配还能被满足，规整基本都成功；一旦分配开始失败，规整完全失效。
中间没有平滑过渡，是个悬崖。**

扫描比正好翻转，这是同一件事的另一面（**这张表要背下来**）：

| 扫描比 | 含义 |
|---|---|
| **> 2** | 空闲页扫描器要翻很远才找到落脚点 —— "**空位难找**" |
| **≈ 1** | 两个扫描器扫描量接近，大致对称相遇 |
| **< 0.5** | 迁移页扫描器要翻很远才找到搬得动的页 —— "**搬得动的页难找**"，UNMOVABLE 占比高 |

**后者才是 UNMOVABLE 污染的指纹。** 压到极限时，内存里剩下的几乎全是
内核栈、slab 和已分配的大页 —— 没有可迁移的页，规整自然全败
（`compact_isolated` 179685：**隔离了 700MB 准备搬，一个 order-9 都没凑出来**）。

> 同一套工具在两种压力深度下给出**相反**的比值 —— 这比"一次跑通"更有说服力，
> 说明工具真的在测量物理现象，不是在输出常量。

### 复现性

两轮独立实验测得的"白拿库存"：**1652 vs 1630，相差 1.3%**。
**可复现性是一切性能工作的前提** —— 这句话现在有数据支撑了。

### 一个留给 P0 回答的开放问题

第一批（纯白拿、零规整零回收）的耗时：第三轮 **44.78 秒**，第四轮 **10.06 秒**，
同样规模差 4 倍。说明这段时间**不是**花在慢路径上。

→ **光看耗时不能判断有没有进慢路径，必须看计数器。**
   这正是要做 eBPF 精确埋点的理由：`/proc/vmstat` 只给总数，
   给不出"这一次分配等了多久、等在哪个环节"。

另一个待解释的现象：每次 stall 的平均 PSI 时间，
失败那轮 177.8/42 = **4.2 ms**，成功那轮 4691/402 = **11.7 ms** ——
**失败的规整反而更便宜**。合理猜测是失败路径提前退出（DEFERRED/SKIPPED），
但要 P0 的 per-attempt 延迟直方图按 `status` 分维度才能证实。**现在不写进结论。**

---

## 9.5 站④：慢路径的内核源码逻辑（最硬的一站）

**这一站没有实验，全是源码逻辑。必须能不看文档画出来。**

### 分配的两条路

```
alloc_pages(gfp, order)
  └─ __alloc_pages()
       ├─ get_page_from_freelist()        ← 快路径。从伙伴系统空闲链表直接摘
       │                                     ★ v1 的 fraginfo.c 就 kprobe 在这儿
       └─ (拿不到) __alloc_pages_slowpath()   ← 慢路径。★ v2 的战场
```

**v1 挂快路径，所以它每次分配都触发（每秒几十万次）→ 必须限流。
v2 挂慢路径，整轮实验只发生 402 次 → 不需要限流，而且限流会毁掉长尾。**
这是两个模块设计上最根本的差异，也是"为什么 v2 不沿用 v1 的限流写法"的答案。

### 慢路径里的顺序（必被追问）

★ **2026-08-13 用真源码（`ksrc-5.15.178/mm/page_alloc.c`）逐行核对过，行号可信。**

```
__alloc_pages_slowpath()                                  page_alloc.c

  ① 重算 alloc_flags —— ★ 水位线从 LOW 放宽到 MIN               :4979
  ② wake_all_kswapds()        叫醒后台回收线程（不等它）        :4993
  ③ get_page_from_freelist()  用放宽后的尺子再量一次            :4999
  ④ ★★ 提前那一发规整（只对两类分配）                          :5013
        if (can_direct_reclaim && can_compact &&
            ( costly_order                          ← ① order > 3
              || (order > 0 && migratetype != MOVABLE) )  ← ② 非可移动高阶
            && !gfp_pfmemalloc_allowed(gfp_mask))
                __alloc_pages_direct_compact(...)         :5017

  retry:                                                        :5058
  ⑤ wake_all_kswapds()        循环里再叫一次                    :5061
  ⑥ get_page_from_freelist()  每次花钱之后都先免费试一次        :5079
  ⑦ __alloc_pages_direct_reclaim()   ← **先回收**               :5092
  ⑧ __alloc_pages_direct_compact()   ← **后规整**               :5097
  ⑨ should_reclaim_retry()  → goto retry                        :5115
  ⑩ should_compact_retry()  → goto retry                        :5126

  ⑪ 出不去 → OOM killer / 返回 NULL
```

**必须记住的四条**：

1. **retry 循环里是"先 reclaim 后 compaction"**，
   进循环之前有一发提前规整，条件是**两种**（不只 costly！）：
   `order > 3`，**或者** `order > 0 且 migratetype != MOVABLE`。
   内核 :5008 的注释讲了理由：*"as it's likely that we have enough base pages
   and don't need to reclaim"* + 非 MOVABLE 那类是 *"prevent permanent fragmentation"*。
   **共同点：这两类的特征都是"回收帮不上忙"**（回收放出的是零散单页，凑不成大块）。
2. **"direct" 的含义是"发起分配的那个进程自己同步做这件事、被阻塞在这里"** ——
   与之相对的是 kswapd（后台回收）和 kcompactd（后台规整），那两个不阻塞谁。
3. **★ "放宽水位再试一次"不是又找了一遍内存，是换了一把更松的尺子重新量。**
   每个 zone 有三条空闲页红线 `high > low > min`。分配的判据不是"有没有页"，
   而是"**给完你之后剩余空闲还在红线之上吗**"，掉线下就拒绝——哪怕 free list 上还有页。
   留这条线是给**不能失败**的分配保命：中断上下文的原子分配、以及**回收流程自己**
   （回收也要用内存，用光就死锁）。
   快路径用 `ALLOC_WMARK_LOW`（:5293/:5431），慢路径 `gfp_to_alloc_flags()` 一进来就
   降到 `ALLOC_WMARK_MIN`（:4719）。**内存一页没多，只是判定标准降低了。**
4. **慢路径的排序原则：先试免费的，不行才花钱。**
   ③⑥ 换尺子重量（免费）→ ④⑧ 搬家（花 CPU）→ ⑦ 回收（花 CPU + 可能 IO）→ ⑪ OOM（最贵）。
   所以每次回收或规整之后都紧跟一次 `get_page_from_freelist`。

**为什么是"先回收后规整"**（这个因果链要能讲）：
规整是"搬家"，搬家需要**目标空位**。如果空闲页太少，连搬家用的落脚点都凑不出来，
规整无从下手 —— 所以要先回收出一些空闲页。
**一句话：规整不创造内存，它只是搬家。**

源码里有**两处**直接证据（面试点名一个具体枚举值，比讲一段道理有说服力）：

**证据 1** —— `compaction_needs_reclaim()` 九个值里只对一个返回真：

```c
static inline bool compaction_needs_reclaim(enum compact_result result)
{
        return result == COMPACT_SKIPPED;      // ★ 只有这一个
}
```
配套注释：*"compaction was skipped because there are not enough order-0 pages
... **regular reclaim has to try harder and reclaim something**"*

**证据 2（更硬，2026-08-13 新翻到）** —— 循环末尾第 ⑩ 步 :5121 的注释把依赖关系写死了：

> *"It doesn't make any sense to retry for the compaction if the order-0 reclaim
> is not able to make any progress **because the current implementation of the
> compaction depends on the sufficient amount of free memory**"*

```c
if (did_some_progress > 0 && can_compact &&      // ★ 回收有进展才准重试规整
        should_compact_retry(...))
        goto retry;
```

> **"回收没进展 → 连规整重试的资格都没有。"** 这是"规整依赖回收"最直白的一句源码。

### 规整本身：双扫描器

```
一个 zone 内：

  低地址 ──────────────────────────────────────────► 高地址
  │                                                      │
  migrate_pfn →→→→→→→                    ←←←←←←← free_pfn
  （找"搬得走的页"）                      （找"能落脚的空位"）
                        两者相遇 = 这一轮结束
```

- **迁移页扫描器**从 zone 低地址往上走，找 MOVABLE 的页
- **空闲页扫描器**从 zone 高地址往下走，找空闲块当落脚点
- 找到一对就调页迁移把页搬过去，原地就腾出连续空间

**为什么扫描比有意义**：理想情况两侧扫描量应该接近（对称相遇）。
一旦严重偏斜，偏斜的方向就告诉你**瓶颈在哪一侧**（见 9.4 那张表）。

**为什么不能用 `free_pfn - migrate_pfn` 算"扫了多少页"**（决策 #9）：
扫描器会**重启**，硬算会得出负数或荒谬的巨大值。
扫描量的权威来源是 `/proc/vmstat` 的
`compact_migrate_scanned` / `compact_free_scanned`。

### `enum compact_result` 的 9 个取值

★ **计划书只列了 5 个，漏了 `PARTIAL_SKIPPED(6)`**。
两个来源交叉确认过：`end/format` 里 `print fmt` 的 `__print_symbolic`，
以及 `include/linux/compaction.h` 的 `enum compact_result`。

| 值 | 名字 | 含义 |
|---|---|---|
| 0 | `NOT_SUITABLE_ZONE` | 内部值 |
| 1 | `SKIPPED` | **没启动**："没可能，或直接回收更合适" |
| 2 | `DEFERRED` | 因过去连续失败被内核主动推迟（退避） |
| 3 | `NO_SUITABLE_PAGE` | 内部值 |
| 4 | `CONTINUE` | 内部值，应继续扫下一个 pageblock |
| 5 | `COMPLETE` | 整个 zone 扫完仍没成功（最坏：白扫） |
| 6 | **`PARTIAL_SKIPPED`** | 扫了一部分就退避 ← **计划书漏的那个** |
| 7 | `CONTENDED` | 锁竞争，提前终止 |
| 8 | `SUCCESS` | 判定分配现在能成功了 |

### ★ "规整成功率"至少有三种算法 —— 工具必须说清用的哪种

从 `compaction.h` 的判定函数读出来的**内核自己的口径**：

| 判定函数 | 口径 |
|---|---|
| `compaction_made_progress()` | 只有 `SUCCESS(8)` 算成功 |
| `compaction_failed()` | **只有 `COMPLETE(5)` 算真失败**（整个 zone 白扫完） |
| `compaction_withdrawn()` | `DEFERRED(2)` / `CONTENDED(7)` / `PARTIAL_SKIPPED(6)` 算"主动退避"，内核认为换更高优先级还有戏 |
| `compaction_needs_reclaim()` | `SKIPPED(1)` 意味着**应该先做回收** |

**所以 `SKIPPED` 不是"规整失败"，而是"规整判断自己不该上、该让回收先干"。**
这从代码层面印证了慢路径里 reclaim 在前、compaction 在后。

**面试价值**：被问"你的成功率怎么算的"，能答出"有三种口径，我用的是
`compaction_made_progress` 那种，并且在报告里写明了" —— 这比报一个数字强得多。

---

## 9.6 站⑤：P0 探针的设计取舍

**先读 `源码/src/bpf/compactinfo.c` 顶部那 150 行大注释**，它是自包含的。
这里只列必须能复述的四条。

### ① 三个埋点的粒度完全不同

```
一次 direct compaction（= 一个进程被卡住一次）
└── try_to_compact_pages          ← 1 次，外层边界，**唯一带 order**
    ├── begin(zone A) … end(zone A)     ← per-zone，一次分配要遍历多个 zone
    │   ├── migratepages          ← per 迁移批次！主循环每搬一批打一次
    │   └── migratepages …
    └── begin(zone B) … end(zone B)
```

各埋点带什么字段（tracefs `format` 实测）：

```
try_to_compact_pages :  order, gfp_mask, prio              ← ★ 只有这里有 order
begin                :  zone_start, migrate_pfn, free_pfn, zone_end, sync
end                  :  同上 5 个 + status                  ← 没有 order
migratepages         :  nr_migrated, nr_failed             ← 没有 order
```

### ★★ 这一节的地基：eBPF 探针之间怎么传数据（用户 2026-08-13 点名要记的）

> **eBPF 的探针之间不共享变量。每次触发都是独立的一次函数调用，栈是新的。
> 探针之间传数据只有一条路：写进 map，另一个探针再读出来。**

这条不只管 compaction —— **任何"测某个操作耗时"的 eBPF 工具都是这个套路**：
入口存时刻、出口取时刻相减。业内管这张表叫 **timing map / 配对表**。

### 三条推论

**推论 1：`order` 只有最外层有 → 必须两层配对。**

设想只挂 `begin/end`：`end` 触发时手上只有 `zone_start / migrate_pfn / free_pfn /
sync / status`。**你能算出"这个 zone 的规整花了 5 ms"，但说不出这 5 ms 是为了凑多大的块。**
而 order-3（32 KB）和 order-9（2 MB）的规整难度差一个量级，
**混在一张表里算平均等于什么都没测**。

**准确的说法不是"order 过期了"，是"order 不在 begin/end 自己的字段里"** ——
它们要用就必须从别处取，而"别处"只能是我们在入口自己存下的那条 map 记录：

| | 入口有探针吗 | map 里有记录吗 | order 从哪来 |
|---|---|---|---|
| 只挂 begin/end | ❌ | ❌ 没人存过 | **无处可取** |
| 现在的实现 | ✅ | ✅ 入口存的 | `o->order` |

**完整时间线（★ 注意：`end` 探针并不取 order，只有 kretprobe 取）**：

```
T1  外层 tracepoint    args->order = 9
      outer_map[tid] = { ts:T1, order:9, gfp, prio }        ★ 存

T2  begin              ① 查 outer_map[tid] → 只做准入判断（不取 order）
                       ② zone_map[tid] = { ts:T2, sync }    ★ 存

T3  end                ① 查 outer_map[tid] → 同样只做准入
                       ② 查 zone_map[tid] → 取回 T2
                          zone_lat[ sync, log2(T3−T2) ] += 1
                       ③ zone_map.delete(tid)               ★ 用完删

T4  kretprobe          ① 查 outer_map[tid] → 取回 T1 和 order   ★ order 在这才用上
                          attempt_lat[ order, log2(T4−T1) ] += 1
                       ② outer_map.delete(tid)              ★ 用完删
```

**两个延迟量的不是一回事，两张表都要**：

| 差值 | 表 | 分维度 | 回答什么 |
|---|---|---|---|
| **T4 − T1** | `attempt_lat` | **order** | 这次分配一共被卡了多久 ← **主指标，"代价"** |
| **T3 − T2** | `zone_lat` | **sync** | 单个 zone 上真正扫+搬花了多久 ← 分解 |

必然 **T4−T1 ≥ Σ(T3−T2)**，差额 = 循环开销 + 被 `continue` 跳过的 zone。
实测 `begin_accept/outer_enter = 484/377 ≈ 1.28`，即**一次外层延迟里套着 1~2 段内层延迟**。

> 口径一句话：**外层测代价（用户等了多久），内层测机制（花在哪个 zone、同步还是异步）。**

**推论 2：`begin` 和 `compact_stall` 粒度不同 → 只能和外层对账。**（详见 9.6 ①附）

**推论 3：`migratepages` 一次 begin/end 内会打多次** → `nr_migrated/nr_failed`
必须**累加**，取最后一次会严重低估。

### ①附　★ `compact_stall` 精确对账等式（2026-08-13 核对源码后修正）

**先修正一条之前写得不够准的话。** 9.9 里写了 "`outer_enter` 与 `compact_stall`
同为 377，完全相等"——**数据是真的，但"恒等"的说法不成立。**
`count_vm_event(COMPACTSTALL)` 不在 `try_to_compact_pages` 里，而在**它的调用者**里，
而且**前面有一个早退**：

```c
__alloc_pages_direct_compact()                          page_alloc.c:4384
    psi_memstall_enter()                                :4394   ← 压力计时开始
    *compact_result = try_to_compact_pages(...)         :4397   ← 真正干活的
    psi_memstall_leave()                                :4402
    if (*compact_result == COMPACT_SKIPPED) return NULL; :4409  ← ★★ 早退，不计数！
    count_vm_event(COMPACTSTALL);                       :4410   ← compact_stall++
    page = get_page_from_freelist(...)                  :4418   ← 搬完再试一次
    if (page) { count_vm_event(COMPACTSUCCESS); ... }   :4425
    count_vm_event(COMPACTFAIL);                        :4433
```

**所以精确的等式是：**

```
compact_stall  ==  outer_exit  −  (返回值 == COMPACT_SKIPPED 的次数)
```

2026-08-12 那批数据 `attempt_status` 是 `SUCCESS 376 / CONTENDED 1`，
**`SKIPPED` 一次都没有** → 减数为 0 → 退化成 377 == 377。
**但内存真紧张时 `SKIPPED` 会大量出现，那时 `compact_stall` 会明显小于 `outer_enter`。**
→ **`报告_P0.md` 里这条必须写成带减法的等式，并注明当日减数为 0。**

**为什么不能和 `begin` 对账 —— 因为两个方向都会偏，误差无法用系数校正：**

| 剧情 | outer | begin | compact_stall |
|---|---|---|---|
| 正常，遍历 2 个 zone | 1 | **2** | 1 |
| 正常，1 个 zone 就拿到 | 1 | **1** | 1 |
| 所有 zone 都 `compaction_deferred` → `continue` | 1 | **0** | 1（DEFERRED≠SKIPPED，照涨）|
| 返回 `COMPACT_SKIPPED` | 1 | 0 | **0** |

> **`compact_stall` 的粒度是"一次尝试"，`begin` 的粒度是"一个 zone"。
> 粒度不同的两个计数器天生不可对账。** 要对账只能找同粒度的那一层 = 外层探针。

**⚠️ 别把 deferred 和 contended 混**（用户 2026-08-13 混过一次）：
`compaction_deferred` 是**进 `compact_zone` 之前**就被劝退（begin 不打）；
`COMPACT_CONTENDED` 是**已经进去了**才遇到锁竞争（begin 早打过了）。

→ 由此得出**外层探针的第三个用途**：它是**唯一能和 `/proc/vmstat` 对账的一层**。
没有它，eBPF 的数据就是一组无法与内核官方计数交叉验证的孤立数字。

### ② 为什么还要一个 kretprobe（★ 最值得讲的设计决策）

`try_to_compact_pages` **只有入口 tracepoint，没有出口 tracepoint**。
而 v2 的核心指标是"这次分配被卡了多久" —— 没有出口时刻就算不出来。

| 备选方案 | 取舍 |
|---|---|
| 只用 tracepoint，把"第一个 begin 到最后一个 end"当总耗时 | ✗ 漏掉入口到第一个 begin 之间的开销；更致命的是 **`status=DEFERRED/SKIPPED` 根本不打 begin/end**，这类"还没开始就被劝退"的尝试会整个丢失 —— 恰恰是最有意思的一类 |
| 入口也用 kprobe，`PT_REGS_PARM2` 取 order | ✗ 参数顺序不是稳定 ABI，换内核版本可能读到垃圾 |
| **入口用 tracepoint（稳定 ABI 拿 order/gfp/prio），出口用 kretprobe（只用返回值和时刻）** | ✓ 耦合面最小，只依赖"这个函数存在且非 inline"。实测它在 `/proc/kallsyms` 里是全局符号 `T`，入口与 kretprobe 严格 1:1 |

额外白赚：**kretprobe 的返回值就是 `enum compact_result`**，即最终结局。

### ③ 三重来源过滤（本模块最容易做错的地方）

`compaction:*` 是**三条路径共用**的：

| 来源 | 特征 | 要不要 |
|---|---|---|
| direct compaction | 进程自己被卡住，同步 | ★ 只要这个 |
| kcompactd | 内核后台线程，异步，进程没被卡 | 不要 |
| 手动 `compact_memory` | 管理员触发，也没有进程在等分配 | 不要 |

不过滤，"平均规整延迟"会被后台线程严重稀释。
**实测这不是理论担忧**：`migratepages` 收到 3138 条，其中 **2075 条是噪声**，
有效只有 1063 条 —— **噪声比数据多一倍**。

**麻烦在于三者打出来的事件长得一模一样**：真正干活的是 `compact_zone()`
（扫描、挪页都在里面），三个内层埋点就在它里面。三条路最后都跑进同一个
`compact_zone()`，**没有任何字段能区分来源**。

**判据：终点相同，但"怎么走进来"不同 —— 只有 direct compaction 经过
`try_to_compact_pages`。** 2026-08-12 查过 `compact_zone()` 的**全部**调用点，共四个：

| 行号 | 调用者 | 来源 | 经过 `try_to_compact_pages` |
|---|---|---|---|
| 2544 | `compact_zone_order()` ← 2605 `try_to_compact_pages` | ① direct | **✅ 只有这一条** |
| 2673 | `proactive_compact_node()` | ② kcompactd 主动规整 | ❌ |
| 2703 | `compact_node()` ← 2761 `sysctl_compaction_handler` | ③ 管理员 `echo 1` | ❌ |
| 2859 | `kcompactd_do_work()` | ② kcompactd 主循环 | ❌ |

→ 内层探针的准入条件就是"**当前 tid 有没有活跃的外层记录**"。

> **记忆画面（登记台）**：停车场门口有个登记台 = `try_to_compact_pages`。
> 只有**找不到车位的司机**（进程自己）会去登记台叫人挪车；
> **保安**（kcompactd）和**经理**（管理员）走员工通道，不登记。
> 站在挪车现场看见有人在挪车，怎么知道是哪一种？**去登记台查有没有这个人的记录。**

**所以外层探针有三个用途**（很容易只记住第一个）：
1. 拿 `order`（推论 1）
2. **当准入凭证** —— 它在 map 里的存在**本身**就是"这次是 direct compaction"的证明
3. 唯一能和 `/proc/vmstat` 对账的一层（①附）

**★ 由此顺带答出一道高频题**：`echo 1 > /proc/sys/vm/compact_memory`
**为什么不能用来验证 direct compaction？**
不是因为"它是异步的"（它其实是同步的），而是因为**它绕开了 `try_to_compact_pages`**，
后果有两个、两头都错：
- **`compact_stall` 不涨**（那个计数器在慢路径里加）→ **它根本不能当验收手段**
- **begin/end 照常打**（最后也跑进同一个 `compact_zone`）→ **只挂内层的工具会误收**

> 一边"没有信号"，一边"有假信号"。**这是最坏的一种验证手段。**
> 句式记住：问"为什么不能用 X 验证"，答案永远是"**X 会让指标在不该动时动、
> 在该动时不动**"，光说"X 是别的东西"不够。

**必须在内核态做**（决策 #7）：捞到用户态再扔，等于白付一次 map 写入
加一次 perf 传输的开销。

**而且这个过滤器能自证**：`begin_reject` 计数器 > 0 就证明它真的挡住了东西，
不是"恰好没有噪声"（实测 `compact_daemon_wake` 涨了 62，噪声确实存在）。

### ④ 为什么 map key 用 tid 而不是 tgid（决策 #5）

`bpf_get_current_pid_tgid()` 返回 u64：**高 32 位是 tgid（进程），低 32 位是 pid（线程）**。
v1 的 `extfraginfo.c` 用 `>>32` 取 tgid 做按进程聚合，那是对的。

但 compaction / reclaim 是**线程**行为：同一个进程的两个线程同时进慢路径，
用 tgid 做 key 会**互相覆盖**，配对全乱。所以 v2 用**完整的 u64**（即 tid）。

**★ 答这题不能只说"tid 唯一"，要说清用错了会怎样**（这是"什么"和"为什么"的区别）：

```
线程 A(tid=100)、B(tid=101) 同属进程 99，同时陷入慢路径：
用 tgid=99 做 key：
   A 进入 → map[99] = { ts:T1, order:9 }
   B 进入 → map[99] = { ts:T5, order:3 }   ★ 把 A 的记录覆盖了
   A 退出 → 查 map[99] 拿到 B 的数据 → 延迟算成 T4−T5、order 记成 3   ← 全错
   B 退出 → map[99] 已被 A 删掉 → 记一笔"未配对"
```

**后果不是崩溃，是安静地算出一堆错数字。**
（`compactinfo.c` 里 `zone_t` 上方那句注释"同一个线程在同一时刻只会在一个 zone 上
做规整，begin/end 严格嵌套，不会交错"——**这个"不交错"的前提只有用 tid 才成立**。）

> 通用规则：**配对表的 key 必须唯一标识"一条执行流"。内核里执行流的单位是线程 →
> 永远用 tid。** 多线程程序上用 tgid 是 eBPF 新手最经典的 bug 之一。

### ⑤ 四道自证机制（P0 的可信度全靠这个）

| 机制 | 抓什么 |
|---|---|
| `outer_unpaired` / `end_unpaired` | LRU map 淘汰掉的是**停留最久**的表项 = 延迟最长的样本。**不报未配对率，延迟统计就不可信** |
| **直方图样本数 == 外层退出 − 未配对** | 抓"静默丢样本"。BCC 的 `increment()` 失败时不报错、没有返回值可查 |
| `begin_reject` > 0 | 证明过滤器在工作 |
| `no_direct_reclaim` 预期恒为 0 | direct compaction 必然带 `__GFP_DIRECT_RECLAIM`（"允许我阻塞"）。一旦非 0，说明我们对这条路径的理解有偏差 |

**★ 顺手抓到的一个真隐患**：`BPF_HISTOGRAM(名字, key类型)` 两参数形式
展开成 `max_entries = 64`。二维 key（order × log2 桶）最坏要 330 项，
超了 `increment()` 会**静默失败**。改成三参数显式开 1024。

### ⑥ 两条通用写法

- **计数一律用 `lock_xadd(p, 1)`，不用 `*p += 1`**。
  后者是"读-改-写"三步，4 个核同时执行会互相覆盖丢计数。
  **统计工具连自己的计数都能丢，后面的对账全是假的。**
- **map 里已有的表项直接通过 `lookup` 返回的指针改，不需要再 `update`**。
  `lookup` 返回的是指向 map 内部那份数据的指针，写进去当场生效。
  （v1 的 `extfraginfo.c` 里那个 `update` 是多余的一次值拷贝。）

---

## 9.7 站⑥：自测题（不看文档回答）

### 基础（答不上就回去重读对应站）

1. "三个 0"是哪三个？为什么它们让 v1 显得浅？
2. `holes.c` 为什么必须**随机**释放？顺序释放会发生什么？
3. 为什么光有 `holes.c` 不够，必须加 `kstack.c`？
4. 本机内核栈是 order-2 连续块吗？用什么数据证明的？
5. `echo 1 > /proc/sys/vm/compact_memory` 为什么不能用来验证 direct compaction？
6. `get_page_from_freelist` 在快路径还是慢路径？v1 为什么必须限流？

### 进阶

7. 慢路径 retry 循环里，reclaim 和 compaction 谁先？只有什么情况例外？
8. 为什么是"先回收后规整"？用 `enum compact_result` 里的哪个值能佐证？
9. 双扫描器分别从哪头走、各找什么？扫描比 > 2 和 < 0.5 分别说明什么？
10. `compact_stall` 能和 P0 的哪一层计数对账？为什么不是 `begin` 次数？
11. "规整成功率"有几种口径？你用的是哪种？
12. map key 为什么用 tid 而不是 tgid？

### 面试追问级（这些是加分项）

13. `try_to_compact_pages` 只有入口 tracepoint 没有出口，你怎么测总延迟的？
    为什么不用"第一个 begin 到最后一个 end"？
14. 三重来源过滤怎么做的？**怎么证明过滤器真的生效了**（而不是恰好没噪声）？
15. 为什么用 `BPF_LRU_HASH` 而不是普通 hash？这笔交易换掉了什么？
16. 你观测到规整成功率从 92% 掉到 0%，是什么导致的？这说明了什么？
17. 你的工具出过一次"报告说自己正常、实际上崩了"的事故。你后来加了什么机制？
18. 你有没有提出过一个假设然后自己把它证伪了？

### 参考答案位置

**★ 全文答案见 9.8（2026-08-15 补全）。** 下表是答案背后的详细论证在哪一站：

| 题号 | 展开论证在 |
|---|---|
| 1 | 9.2 |
| 2~5 | 9.3 |
| 6, 7, 8, 9, 11 | 9.5 |
| 10, 12, 13, 14, 15 | 9.6 |
| 16, 17, 18 | 9.4 |

### ★ 考核记录（2026-08-13，闭卷，第 1~12 题）

**第一遍成绩：基础 6 题 2.5 分，进阶 6 题 1.5 分。重答后大部分补齐。**
下一个会话不必从头重考，**按下表只抽查"要补"那几条**。

| 题 | 第一遍 | 重答 | 缺口 / 要补什么 |
|---|---|---|---|
| 1 三个 0 | 半对 | — | `compact_fail` 记错→是 `pgscan_direct`；漏"空表→先造代价"推论（偏差 5）|
| 2 随机释放 | **机制错** | ✅ | 错在归因给规整；正解是 `__free_one_page()` 当场合并（偏差 3）|
| 3 kstack | 半对 | — | UNMOVABLE 是让规整**失败**不是变慢（偏差 4）|
| 4 内核栈 order-2 | 空 | ◐ | `CONFIG_VMAP_STACK=y`；**"虚拟内存 order-2"这个说法要改**（order 是 buddy 的词，vmalloc 区不用它描述）；"可能不连续"→**几乎必然不连续**；**只给了配置证据，漏了行为证据**（KernelStack 涨而 order-2 未减）|
| 5 compact_memory | **全错**（答"异步"）| ◐ | 它其实是同步的；真原因是绕开 `try_to_compact_pages`。**重答只说了"它是什么"，没说"所以验证不了什么"**（见 9.6 ③末）|
| 6 快/慢路径 + 限流 | 半对 | — | 限流由挂点频率决定：24700/s vs 20/s |
| 7 谁先 | ✅ | — | 提前规整是**两种**条件，不只 costly |
| 8 为什么先回收 | 机制对 | ◐ | **漏了题目点名的枚举值 `COMPACT_SKIPPED`** |
| 9 双扫描器 | **反了（第 3 次）** | ✅ | 已用"往高地址搬"推法钉住（偏差 2）|
| 10 对账 | 层对/理由错 | ◐ | 把 deferred 说成"zone 被其它进程占据"（那是 CONTENDED）；正解是**粒度 1:N、两方向都偏**（9.6 ①附）|
| 11 成功率口径 | 列 3 漏 1 | ◐ | 补内层宽松 89.5%；要答"我报 99.7%，因为粒度对齐到一次尝试" |
| 12 tid/tgid | 定义对 | ◐ | **只答了"是什么"，没答"用错会怎样"**（安静算错，不是崩溃）|

**★ 用户自己提出的一个好直觉，值得记**：看到"成功率有四种口径"时说"感觉怪怪的"。
这个不适感是对的 —— 它来自"成功率应该只有一个数"的默认假设。**这题的真考点就是打掉它**：

> **"成功"没有唯一定义，必须先说清"谁的成功、在什么粒度上"。**
> 四种口径 = **2×2**：粒度（一个 zone / 一次尝试）× 判据（规整自己成功 / 分配最终拿到页）。
> 而 **"报一个数字却不知道它是哪种口径"正是 v1 那类工具的通病：看起来精确，实际上没有定义。**

**一个反复出现的答题模式（下次要专门纠）**：用户倾向于答"**它是什么**"，
而题目问的是"**它导致什么后果**"（第 5、12 题都栽在这儿）。
→ **面试里"是什么"只是前半句，不接"所以呢"等于没答。**

---

## 9.8 站⑥参考答案（18 题全文，2026-08-15 补全）

> **怎么用这一节**：不要顺着读。先闭卷答，再翻这里对。
> 每题三段固定结构：
> **【一句话】**= 面试时先说的那句 → **【展开】**= 追问时的第二层 →
> **【常见错答】**= 自己已经踩过的坑，重点看这段。
>
> **★ 贯穿 18 题的一条元规则**（考核暴露出的最大问题）：
> 题目问"为什么"时，答"**它是什么**"只算前半句，必须接"**所以会怎样**"。
> 第 5、12 题就是栽在只答了前半句。

---

### 基础

#### 1. "三个 0"是哪三个？为什么它们让 v1 显得浅？

**【一句话】** 开发机上实测 `compact_stall = 0`、`pgscan_direct = 0`、
`/proc/pressure/memory` 的 some/full total 全为 0 —— 意思是**从来没有任何进程
因为内存问题被真正卡住过**。

**【展开】**
- `compact_stall = 0`：没有进程因为要连续内存而被同步拉去做规整。
- `pgscan_direct = 0`：没有进程因为内存不足而被拉去做直接回收。
- PSI 全 0：内核自己都认为这台机器没有内存压力。

v1 能报出"外部碎片化指数 0.85"，但答不上"0.85 会让谁慢多少" ——
**指标存在，后果不存在**。这就是"只答了有多碎，没答所以呢"。

**→ 由此推出的那条最关键的结论**（第一遍漏掉的）：
**v2 的第一步不能是写探针，必须先造出真实的代价（P-1 阶段）。**
在一台三个 0 的机器上，探针写得再对也只能得到空表 ——
而**空 map 和坏探针长得一模一样**，连"代码写对了没有"都无法验证。

**【常见错答】** 把第二个 0 说成 `compact_fail`。
`compact_fail` 不是独立信息 —— `compact_stall = compact_success + compact_fail`，
stall 为 0 时它必然为 0，说了等于没说。三个 0 要覆盖**三条不同的路径**：
规整、回收、以及内核自己的压力感知。

---

#### 2. `holes.c` 为什么必须**随机**释放？顺序释放会发生什么？

**【一句话】** 顺序释放的页会被伙伴系统在 `free()` 里**当场合并回大块**，
等于什么碎片都没造出来；随机释放才能让每个空洞的伙伴保持被占用，合不掉。

**【展开】** 关键在 `__free_one_page()`：每释放一页，内核立刻算

```c
buddy_pfn = pfn ^ (1 << order);
```

去看**伙伴**是不是也空闲，空闲就合并，然后进位到上一阶继续试。
- 顺序释放：刚还回去的页彼此是伙伴 → 一路合并 → 又变回大块。
- 随机释放：每个空洞的伙伴多半还被占着 → 合并链在第 0 阶就断了 → 碎片留下。

**【常见错答（第一遍就错在这）】** 把失败归因给"规整把它们合回去了"。
**不对 —— 根本轮不到规整，在 `free()` 里就已经合并完了。**
这是**分配器行为**，不是**规整行为**，两件事发生在完全不同的时刻。

还有一条要记住：**伙伴 ≠ 相邻**。PFN 1 和 2 相邻，但 `1^1=0`、`2^1=3`，
它们不是伙伴，永远合不到一起。所以随机释放只是**概率上**破坏合并 ——
`holes.c` 因此只释放一部分而不是全部。

---

#### 3. 为什么光有 `holes.c` 不够，必须加 `kstack.c`？

**【一句话】** `holes.c` 造出来的洞全是**用户态可迁移页**，规整器一挪就好了；
必须掺进**搬不动的内核对象**（内核栈），碎片才是规整**治不好**的。

**【展开】** 用停车场比喻：
- `holes.c` = 车停得稀稀拉拉 → 保安挪一挪就腾出连片空位（MOVABLE，规整**能**解决）。
- `kstack.c` = 往车位里塞**趴窝的车**（UNMOVABLE：内核栈、slab）→ 挪不走，
  这一片区永远凑不出连片空位。

只有第二种才能测出"规整失败"这一类事件 —— 而 P0 最有价值的数据
（成功率悬崖、扫描比 < 0.5）全部来自这一类。

**【常见错答】** 说 UNMOVABLE"让规整变慢"。
**是让规整失败，不是变慢。** 两者的观测后果完全不同：
变慢 → 延迟直方图右移；失败 → `status` 变成 `COMPACT_COMPLETE`、成功率掉。
P0 实测的是后者（成功率从 92% 掉到 0%）。

---

#### 4. 本机内核栈是 order-2 连续块吗？用什么数据证明的？

**【一句话】** 不是。本机 `CONFIG_VMAP_STACK=y`，内核栈走 vmalloc ——
虚拟地址上连续 16 KB，物理上**几乎必然是 4 个互不相干的 order-0 页**。

**【展开】** 两类证据，缺一不可：

| 证据 | 内容 | 单独够不够 |
|---|---|---|
| 配置证据 | `CONFIG_VMAP_STACK=y` | ✗ 只能说明"内核允许这么干" |
| **行为证据** | 施压时 `/proc/meminfo` 的 `KernelStack` 一路涨，
而 `buddyinfo` 的 **order-2 空闲块数并没有相应下降** | ✓ 这才证明它真的没在拿 order-2 |

**【常见错答（两处）】**
1. 说"是**虚拟内存的 order-2**"。**order 是伙伴系统的词**，
   只用来描述物理页块；vmalloc 区不用 order 描述，这句话本身不成立。
   正确说法是"虚拟地址连续 16 KB，物理上 4 个独立的 order-0 页"。
2. 说"物理上**可能**不连续"。**是几乎必然不连续** —— vmalloc 本来就是
   逐页拿 order-0 再拼虚拟地址，凑巧连续才是小概率。
3. **只给配置证据。** 面试官会追问"那你怎么知道运行时真走了这条路"，
   必须能拿出行为证据。

---

#### 5. `echo 1 > /proc/sys/vm/compact_memory` 为什么不能用来验证 direct compaction？

**【一句话】** 因为它**绕开了 `try_to_compact_pages`**，
结果是**两头都错**：该动的指标不动，不该动的信号照打。

**【展开】** 它的调用链是
`sysctl_compaction_handler`(:2761) → `compact_node`(:2703) → `compact_zone`，
完全不经过慢路径。所以：

| | 后果 |
|---|---|
| `compact_stall` **不涨** | 那个计数器加在慢路径里 → **它根本不能当验收手段（没有信号）** |
| begin/end **照常打** | 最后跑进的是同一个 `compact_zone` → **只挂内层探针的工具会误收（假信号）** |

> **一边没有信号，一边有假信号 —— 这是最坏的一种验证手段。**
>
> 记住这个句式：问"为什么不能用 X 验证 Y"，答案永远是
> "**X 会让指标在不该动时动、在该动时不动**"。

**【常见错答（第一遍全错）】** 说它是"异步的"。**它其实是同步的**，
写 sysctl 的那个进程会一直等到规整跑完才返回。异步的是 kcompactd，
两回事。
**重答时的第二个坑**：只说了"它是手动规整不是 direct compaction"——
这只答了"它是什么"，没答"**所以验证不了什么**"。

---

#### 6. `get_page_from_freelist` 在快路径还是慢路径？v1 为什么必须限流？

**【一句话】** 两条路上都有，但 v1 挂的是**快路径**那次；
限流的必要性不是由函数决定的，是由**挂点的触发频率**决定的。

**【展开】** `get_page_from_freelist` 在 `page_alloc.c` 里被调用 4 次：
快路径 1 次，慢路径 3 次（:4418 规整后、:4683 回收后、:4999、:5079）。
v1 挂的是快路径入口 —— **每一次内存分配都会走**。

实测频率：

| | 空闲 | 压力下 | 要不要限流 |
|---|---|---|---|
| v1 快路径挂点 | 988 次/秒 | **~24700 次/秒** | **必须限流**，否则探针开销本身就是负载 |
| v2 慢路径挂点 | 0 | 峰值 **~20 次/秒** | **绝不能限流** |

**→ 这题真正的考点是后半句**：v2 **不但不限流，限流还会毁掉数据**。
慢路径事件本来就稀疏，而我们要的恰恰是**长尾延迟**；
采样一丢，尾巴就没了，"最坏卡了多久"这个核心指标直接失效。

---

### 进阶

#### 7. 慢路径 retry 循环里，reclaim 和 compaction 谁先？只有什么情况例外？

**【一句话】** 循环里**先回收后规整**（`:5092` 回收 → `:5097` 规整）；
例外是**进循环之前**那一发提前规整（`:5013-5017`）。

**【展开】** 例外条件有**两种**，不只 costly：

```c
if (can_direct_reclaim &&
    (costly_order ||
     (order > 0 && ac->migratetype != MIGRATE_MOVABLE))
    && !gfp_pfmemalloc_allowed(gfp_mask)) {
```

- `costly_order`（order > 3）：回收大量 order-0 也不一定能拼出这么大的块，
  先规整更划算。
- `order > 0 && migratetype != MOVABLE`：**防止永久性碎片** ——
  不可移动类型一旦 fallback 去别的片区借位，污染是不可逆的。

内核在 `:5008` 的注释原话给的就是这两条理由
（"we have enough base pages and don't need to reclaim" / "prevent permanent fragmentation"）。

**【常见错答】** 只记住 costly 那一条。**漏掉第二条会答不出"为什么 order-1
的 UNMOVABLE 分配也会提前规整"。**

---

#### 8. 为什么是"先回收后规整"？用 `enum compact_result` 里的哪个值能佐证？

**【一句话】** 因为**规整自己需要空闲页当落脚点** ——
空闲页太少时规整根本开不了工，直接返回 **`COMPACT_SKIPPED`**。

**【展开】** 两处源码证据：

1. **枚举值本身**：`compaction_needs_reclaim()`（`compaction.h:130`）
   判的就是 `COMPACT_SKIPPED` 这一个值，函数名直接写着"需要先回收"。
2. **注释 + 代码守卫**（`page_alloc.c:5121`）：
   > "It doesn't make any sense to retry for the compaction if the order-0
   > reclaim is not able to make any progress because the current
   > implementation of the compaction depends on the sufficient amount of
   > free memory"

   落到代码上是 `if (did_some_progress > 0 && can_compact && should_compact_retry(...))`
   —— **回收没进展，压根不给规整重试的机会。**

**【展开·类比】** 挪车需要**至少一个空位**当中转，一个空位都没有就谁也挪不动。
回收 = 拖走几辆车腾出中转位。

**【常见错答】** 机制讲对了，但**漏掉题目点名要的那个枚举值**。
题目问"用哪个值佐证"，答案里没出现 `COMPACT_SKIPPED` 就是没答上。

---

#### 9. 双扫描器分别从哪头走、各找什么？扫描比 > 2 和 < 0.5 分别说明什么？

**【一句话】** migrate scanner **低地址 → 高地址**找**搬得动的页**；
free scanner **高地址 → 低地址**找**空位**；两者相遇 = 这一轮扫完了。

**【展开·怎么当场推出来，不要背】**
> 内核的目标是**把还在用的页往高地址集中，把低地址腾成连片空闲**。
> 那么：要搬的东西从低地址开始找（migrate ↑），
> 目的地从高地址开始找（free ↓）。**方向自然就出来了。**

比值的读法 —— 记住一条总规律：**哪一侧要找的东西稀缺，哪一侧的扫描数就爆**。

| free/migrate | 含义 | 对应现实 |
|---|---|---|
| **> 2** | 空闲扫描器翻了很远才找到落脚点 → **空位稀缺** | 实测 7.36（压力适中、规整成功率 92%）|
| ≈ 1 | 两侧大致对称相遇 | 正常 |
| **< 0.5** | 迁移扫描器翻了很远才找到搬得动的页 → **可迁移页稀缺** | 实测 0.81（压到极限、成功率 0%）—— **UNMOVABLE 污染的指纹** |

**【常见错答（这题方向答反了三次）】** 一定要用上面那句"往高地址搬"现推，
不要试图记忆"migrate 是低还是高"。另外 `> 2 = 空闲充裕` 也是反的 ——
**扫描数大 = 难找 = 稀缺**，不是充裕。

---

#### 10. `compact_stall` 能和 P0 的哪一层计数对账？为什么不是 `begin` 次数？

**【一句话】** 只能和**外层探针**（`try_to_compact_pages` 的入口/出口）对账，
因为 `compact_stall` 和外层是 **1:1**，和 begin 是 **1:N**。

**【展开·精确等式】**（这一条是 2026-08-13 核对源码后修正的，**不是恒等式**）

```
compact_stall == outer_exit − (返回 COMPACT_SKIPPED 的次数)
```

因为计数器加在 `__alloc_pages_direct_compact()` 里（`page_alloc.c:4410`），
而 **:4409 有一句提前返回**：

```c
if (*compact_result == COMPACT_SKIPPED)
    return NULL;          // ← 在计数之前就走了
count_vm_event(COMPACTSTALL);
```

2026-08-12 那轮 SKIPPED = 0，所以退化成 377 == 377 —— **是巧合，不是恒等**。

**为什么不能和 begin 对账：**

| 场景 | 外层 | begin 次数 | 差在哪 |
|---|---|---|---|
| 正常，遍历 2 个 zone | 1 | 2 | **偏多** |
| 正常，1 个 zone 就成功 | 1 | 1 | 恰好相等（不代表规律）|
| 所有 zone 都 `deferred` | 1 | **0** | **偏少** |
| 返回 `COMPACT_SKIPPED` | 1（但 stall 不加） | 0 | 两边都偏 |

> **粒度是 1:N，而且误差是双向的** —— 既可能多也可能少，
> 所以连"大致成比例"都谈不上，不能拿来对账。

**【常见错答】** 把 deferred 说成"zone 被其它进程占据、访问失败"。
**那是 `COMPACT_CONTENDED`（锁竞争），两者完全不同：**

| | `compaction_deferred()` | `COMPACT_CONTENDED` |
|---|---|---|
| 触发 | 这个 zone **过去连续失败**过 → 攒够冷却期再试 | 当场**抢锁失败** |
| 发生位置 | 进 `compact_zone` **之前**，直接 `continue` | 已经**在** `compact_zone` 里面 |
| begin/end | **完全不打** | **begin 已经打了** |

另外"不会进入 `try_to_compact_pages`"这句也是错的 ——
**那个函数永远会进**，被跳过的是里面的 `compact_zone`。

---

#### 11. "规整成功率"有几种口径？你用的是哪种？

**【一句话】** 同一批数据能算出**四个**都正确的成功率，
因为"成功"没有唯一定义；**我们报的是 376/377 = 99.7%**，
即"**以一次分配尝试为粒度、看规整本身是否成功**"。

**【展开·四种口径是一个 2×2】**

|  | 判据：规整自己成功 | 判据：分配最终拿到页 |
|---|---|---|
| **粒度：一个 zone** | 373/484 = **77.1%**（严格：只算 `COMPACT_SUCCESS`）| (484−51)/484 = **89.5%**（宽松：只把 `COMPACT_COMPLETE` 算失败）|
| **粒度：一次尝试** | **376/377 = 99.7%**　← **本工具报这个** | 377/377 = **100.0%**（`/proc/vmstat` 报这个）|

（51 正好是 `COMPACT_COMPLETE` 的次数。）

**四个判据函数都在 `include/linux/compaction.h`**，各自只认一个值：

| 函数 | 行 | 只认 |
|---|---|---|
| `compaction_made_progress()` | 106 | `COMPACT_SUCCESS` |
| `compaction_failed()` | 120 | `COMPACT_COMPLETE` |
| `compaction_needs_reclaim()` | 130 | `COMPACT_SKIPPED` |
| `compaction_withdrawn()` | 147 | `DEFERRED` / `CONTENDED` / `PARTIAL_SKIPPED` |

**为什么选 99.7% 这一档**：用户关心的是"**我这次分配有没有被规整救回来**"，
粒度必须对齐到**一次分配尝试**，而不是内核内部遍历了几个 zone。

**★ 这题真正的考点**（"感觉怪怪的"那个直觉是对的）：
不适感来自"成功率应该只有一个数"的默认假设，**题目就是要打掉它**。

> **"成功"没有唯一定义，必须先说清"谁的成功、在什么粒度上"。**
> **报一个数字却说不出它是哪种口径，正是 v1 那类工具的通病：
> 看起来精确，实际上没有定义。**

顺带能答出 vmstat 的局限：`COMPACTSUCCESS`(:4425) 是在 :4418 的
`get_page_from_freelist` **拿到页之后**才加的 —— 所以 vmstat 的成功率
回答的是"**分配最终有没有拿到页**"，不是"规整有没有成功"。

---

#### 12. map key 为什么用 tid 而不是 tgid？

**【一句话】** 因为 compaction 是**线程**行为；用 tgid 做 key，
同进程的两个线程会**互相覆盖**，**后果不是崩溃，是安静地算出一堆错数字**。

**【展开·走一遍事故】**
`bpf_get_current_pid_tgid()` 返回 u64：高 32 位 tgid（进程），低 32 位 pid（线程）。
v1 按进程聚合用 `>>32` 取 tgid，那是对的；v2 要配对，必须用完整 u64。

```
线程 A(tid=100)、B(tid=101) 同属进程 99，同时陷入慢路径：
用 tgid=99 做 key：
   A 进入 → map[99] = { ts:T1, order:9 }
   B 进入 → map[99] = { ts:T5, order:3 }   ★ 覆盖了 A 的记录
   A 退出 → 查 map[99] 拿到 B 的数据 → 延迟算成 T4−T5、order 记成 3   ← 全错
   B 退出 → map[99] 已被 A 删掉 → 记一笔"未配对"
```

`compactinfo.c` 里 `zone_t` 上方那句注释"同一线程同一时刻只在一个 zone 上规整，
begin/end 严格嵌套不交错"——**这个"不交错"的前提只有用 tid 才成立**。

> 通用规则：**配对表的 key 必须唯一标识"一条执行流"；
> 内核里执行流的单位是线程 → 永远用 tid。**
> 多线程程序上用 tgid 是 eBPF 新手最经典的 bug 之一。

**【常见错答】** 只答"tid 唯一、tgid 是进程号"——
**这是"是什么"，题目问的是"用错会怎样"。** 不把上面那段覆盖过程讲出来，
面试官无法判断你是真踩过还是背的。

---

### 面试追问级

#### 13. `try_to_compact_pages` 只有入口 tracepoint 没有出口，你怎么测总延迟的？为什么不用"第一个 begin 到最后一个 end"？

**【一句话】** 在出口挂 **kretprobe** 取 T4，总延迟 = **T4 − T1**；
不用 begin/end 是因为**有一整类尝试根本不打 begin/end**，会被整个丢掉 ——
而那恰恰是最有意思的一类。

**【展开·三个备选方案的取舍】**

| 方案 | 取舍 |
|---|---|
| 只用 tracepoint，第一个 begin → 最后一个 end | ✗ 漏掉入口到第一个 begin 的开销；**更致命：`DEFERRED`/`SKIPPED` 不打 begin/end**，"还没开始就被劝退"的尝试整个消失 |
| 入口也用 kprobe，`PT_REGS_PARM2` 取 order | ✗ 参数顺序不是稳定 ABI，换内核版本可能读到垃圾 |
| **入口 tracepoint（稳定 ABI 拿 order/gfp/prio）+ 出口 kretprobe（只用返回值和时刻）** | ✓ 耦合面最小，只依赖"这个函数存在且非 inline"。实测它在 `/proc/kallsyms` 里是全局符号 `T`，入口与 kretprobe 严格 1:1 |

**白赚的一点**：**kretprobe 的返回值就是 `enum compact_result`** —— 结局免费拿到。

**【展开·T1~T4 时间线，以及两个延迟各自量什么】**

```
T1  外层 tracepoint 入口   → outer_map[tid] = {ts:T1, order:9}
T2  内层 begin（zone A）   → zone_map[tid]  = {ts:T2, sync:1}   ← 先查 outer_map 准入
T3  内层 end  （zone A）   → 取 zone_map 的 T2，算 T3−T2 存进 zone_lat[sync]
        （…可能还有 zone B 的 T2'/T3'…）
T4  出口 kretprobe        → 取 outer_map 的 T1，算 T4−T1 存进 attempt_lat[order]
```

| | 谁取 order | 量的是什么 |
|---|---|---|
| **T4 − T1**（kretprobe） | **只有这里取 order** | **一次分配总共被卡了多久**（含 deferred/skipped 的空转）|
| T3 − T2（end） | 不取 order，key 用 `sync` | 单个 zone 上真正干活的时间 |

必然有 **T4 − T1 ≥ Σ(T3 − T2)**，差值就是遍历 zone、被 defer 掉的那些开销。

**【常见错答】** 说"因为 deferred 会 continue 跳过"——
方向对，但要说清**跳过的后果是 begin/end 一次都不打**，
所以不是"少算一点"，是"**整条样本消失**"。

**【顺带一提】** 之前有过一句错话："T3 end 触发时拿回 order=9"。
**`end` 探针从不读 order** —— 它的 key 是 `sync`。order 只在 T1 存、T4 取。

---

#### 14. 三重来源过滤怎么做的？**怎么证明过滤器真的生效了**（而不是恰好没噪声）？

**【一句话】** 判据是"**当前 tid 在 outer_map 里有没有活跃记录**"；
证明靠 **`begin_reject` 计数器 > 0** —— 它数的就是被挡下来的事件条数。

**【展开·为什么必须这么判】**
`compaction:*` 是三条路径共用的，而**三者打出来的事件长得一模一样**
（最后都跑进同一个 `compact_zone`，没有任何字段能区分来源）：

| 来源 | 特征 | 要不要 |
|---|---|---|
| direct compaction | 进程自己被卡住，同步 | ★ 只要这个 |
| kcompactd | 内核后台线程，异步，没进程在等 | 不要 |
| 手动 `compact_memory` | 管理员触发，也没进程在等 | 不要 |

**终点相同，但"怎么走进来"不同 —— 只有 direct compaction 经过
`try_to_compact_pages`。** 查过 `compact_zone()` 的全部四个调用点：

| 行号 | 调用者 | 来源 | 经过 `try_to_compact_pages` |
|---|---|---|---|
| 2544 | `compact_zone_order()` ← 2605 `try_to_compact_pages` | direct | **✅ 只有这一条** |
| 2673 | `proactive_compact_node()` | kcompactd 主动 | ❌ |
| 2703 | `compact_node()` ← 2761 sysctl | 管理员 | ❌ |
| 2859 | `kcompactd_do_work()` | kcompactd 主循环 | ❌ |

> **登记台画面**：停车场门口有个登记台 = `try_to_compact_pages`。
> 只有**找不到车位的司机**去登记台叫人挪车；保安（kcompactd）和经理（管理员）
> 走员工通道，不登记。站在挪车现场怎么分辨？**去登记台查有没有这个人的记录。**

**【展开·"证明生效"才是本题的真考点】**
过滤器最坏的失效方式是"它没工作，但恰好没噪声，看起来一切正常"。
所以必须有**主动信号**：

- `begin_reject > 0` —— 实测确实 > 0，同轮 `compact_daemon_wake` 涨了 62，
  **噪声真实存在且真的被挡住了**。
- 不过滤会有多严重？实测 `migratepages` 收到 **3138 条，其中 2075 条是噪声**，
  有效只有 1063 —— **噪声比数据还多一倍**，"平均规整延迟"会被后台线程稀释到没意义。

**必须在内核态过滤**（决策 #7）：捞到用户态再扔，等于白付一次 map 写入
加一次 perf 传输的开销。

---

#### 15. 为什么用 `BPF_LRU_HASH` 而不是普通 hash？这笔交易换掉了什么？

**【一句话】** 普通 hash 满了之后**新表项直接插不进去**，而且僵尸表项永远不会被清；
LRU 用"**静默丢失最老的样本**"换掉了"**内存泄漏 + 新事件全丢**"。

**【展开·怕的是什么】**
配对表的删除依赖"出口一定会来"。但有几类情况出口永远不来：
进程被 kill、探针 miss、内核路径提前返回 —— 这些表项就成了**僵尸**，
一直占着槽位。普通 hash 里它们会**慢慢把 map 撑满**，之后**所有新事件都插不进去**。

**【展开·代价，这才是"这笔交易"的答案】**
LRU 淘汰的是**停留最久的表项** —— 而停留最久 = **延迟最长的那些样本**。
**也就是说，LRU 恰好优先丢掉我们最想要的长尾数据。**
所以它必须配一个自证机制：**`outer_unpaired` / `end_unpaired` 必须报出来**。
**不报未配对率，延迟统计就不可信。**

**【实现细节】** BCC 0.12 没有 `BPF_LRU_HASH` 宏，
用 `BPF_TABLE("lru_hash", ...)` 展开写。

**【常见错答（两处）】**
1. "LRU HASH 就是 `BPF_HASH`"。**`BPF_HASH` 恰恰是我们要避开的那个普通 hash。**
2. "普通 hash 没有 delete 就会内存泄漏"——不精确。
   代码里**是有** delete 的；问题出在**出口永远不来的那些表项**，
   是**僵尸表项**，不是"忘了写 delete"。
3. **最重要的一处**：只说"LRU 更安全"，**没说换掉了什么**。
   题目字面就问了"这笔交易换掉了什么"，不答 trade-off 等于没答。

---

#### 16. 你观测到规整成功率从 92% 掉到 0%，是什么导致的？这说明了什么？

**【一句话】** 两轮实验**唯一的差别是压到多深**：停在"每批还能拿满"时成功率 92%，
压到"分配开始失败"时成功率 **0%** —— **中间没有平滑过渡，是个悬崖**。

**【展开·数据】**

| | 压到"分配开始失败" | 停在"每批还能拿满" |
|---|---|---|
| `compact_stall` 增量 | 42 | 402 |
| **规整成功率** | **0.0%** | **92.0%** |
| `compact_isolated` | 179685 页（≈700 MB） | 457047 页 |
| **free/migrate 扫描比** | **0.81** | **7.36** |

**【展开·机制】** 压到极限时内存里剩下的几乎全是内核栈、slab、已分配大页 ——
**没有可迁移的页了**。扫描比翻转到 < 1 正是这件事的另一面（见第 9 题）。
最能说明问题的一个数：`compact_isolated = 179685`，
**隔离了 700 MB 准备搬，一个 order-9 都没凑出来。**

**【展开·"说明了什么"（这半句更重要）】**
1. **规整不是"越缺内存越努力"，而是"缺到一定程度就彻底失效"** ——
   所以监控上不能只看"有没有在规整"，必须看成功率。
2. 对工具本身是最强的可信度证明：**同一套代码在两种压力深度下给出相反的比值**，
   说明它在测量真实物理现象，不是在输出常量。

**【诚实边界】** 这个悬崖**目前只复现过一次**，报告 §7.3 已如实记录为未复现项。

---

#### 17. 你的工具出过一次"报告说自己正常、实际上崩了"的事故。你后来加了什么机制？

**【一句话】** P-1 第三次实验里，`run.sh` 的 bash 数组下标出错让递增循环
在第 10 批就静默终止，**收尾逻辑照常打印"停止原因：达到目标数量 2791"** ——
纯粹的谎报。之后加了**五道校验**。

**【展开·事故现场】**

```
./run.sh: 行 98: _dst["$k"]: 数组下标不正确
停止原因：达到目标数量 2791          ← ★ 谎报
```

死在第 10 批，**恰好停在 1800 个大页，而当时库存 1851** ——
**死在"再要一批就必须触发规整"的前一刻**。日志最后一行写着
"✗ 硬门槛未过：compact_stall 全程为 0"，读起来完全像是压力策略不行。

**【展开·加的五道防线】**

| 防线 | 做法 |
|---|---|
| 采样完整性 | 校验想要的 10 个计数器**一个不少**，缺了就报错并标记该批不可信 |
| 符号校验 | 单调递增计数器**不许出现负增量** |
| 恒等式校验 | `compact_stall = compact_success + compact_fail` |
| 双路对账 | 总量另外**从绝对值再算一遍**，与逐批累加互相对账 |
| **退出自证** | 循环退出时打印 `cur / HP_TARGET / INVENTORY` 三个真实值，**让"提前退出"没法伪装成"正常完成"** |

**【展开·这条经验直接决定了 P0 的设计】**

> **观测工具的底线不是"永不出错"，是"出错时不许伪装成正确"。**

落到 P0 上就是四道自证机制：

| 机制 | 抓什么 |
|---|---|
| `outer_unpaired` / `end_unpaired` | LRU 淘汰的是延迟最长的样本，不报未配对率则延迟统计不可信 |
| **直方图样本数 == 外层退出 − 未配对** | 抓"静默丢样本"：BCC 的 `increment()` 失败时**不报错、无返回值可查** |
| `begin_reject > 0` | 证明过滤器在工作，而不是恰好没噪声 |
| `no_direct_reclaim` 预期恒为 0 | 一旦非 0，说明我们对这条路径的理解有偏差 |

以及报告纪律：**不许写"完美实现""效果良好"，跑不通就写跑不通。**

**【顺带抓到的真隐患】** `BPF_HISTOGRAM(名字, key类型)` 两参数形式展开成
`max_entries = 64`；二维 key（order × log2 桶）最坏 330 项，超了
`increment()` **静默失败**。改成三参数显式开 1024 —— 这正是第二道防线抓出来的。

---

#### 18. 你有没有提出过一个假设然后自己把它证伪了？

**【一句话】** 有。针对上面那次 `/proc/vmstat` 读取异常，我提出过
"bash 按块读 + lseek 回退与 seq_file 冲突"的解释，**然后做实验把它推翻了，
并且没有再编一个解释顶上去**。

**【展开·怎么证伪的】**
造 `pgfault` 每秒 2.5 万的背景负载，`while read < /proc/vmstat` 连读 200 次 ——
**异常 0 次**。假设不成立。

**【展开·处理方式】** 机制**至今未查清**，报告 §7.3 里如实列为未解决问题。
**没有用一个听起来合理的解释把洞填上。**

> **这本身就是能力的一部分：分得清"实测的"和"我猜的"。**

同一条纪律在项目里还有两处体现，可以一起讲：
1. **对账等式的自我修正**：一开始把 `compact_stall == outer_enter` 当**恒等式**，
   后来查源码发现 `page_alloc.c:4409` 有 `COMPACT_SKIPPED` 的提前返回，
   改写成 `compact_stall == outer_exit − SKIPPED 次数`。
   那轮数据 SKIPPED = 0 所以恰好相等 —— **是巧合，我原来的推理是错的**。
2. **PSI 平均值的反常**（失败那轮 4.2 ms/次 < 成功那轮 11.7 ms/次，
   "失败的规整反而更便宜"）：有合理猜测（失败路径提前退出），
   但要按 `status` 分维度才能证实，**所以现在不写进结论**。

---

## 9.9 ★ P0 真实验证（2026-08-12，本项目最有说服力的一组数据）

> 这是 `compactinfo.c` 写出来之后**第一次抓到真实事件**。
> 在此之前它只做到"能编译、能加载"，map 全空 —— 而空 map 和坏探针长得一样。
> 产物：`/tmp/compactinfo.log`、`/tmp/fragstress_run.log`、
> `/tmp/fragstress-20260812-115647/`（**注意 `/tmp` 重启会清，要留证据必须拷出来**）。

### 执行方式（顺序不能颠倒）

```bash
sudo -v                                  # 先在前台过密码，否则后台作业被 SIGTTIN 挂住
grep -E "^(compact_|pgscan_direct|pgsteal_direct|allocstall)" /proc/vmstat > /tmp/vmstat.t0
cd 源码/src
sudo nohup python3 extfrag.py --mode compact --interval 30 --duration 1500 > /tmp/compactinfo.log 2>&1 &
sleep 20                                 # BCC 要现场编译+挂载，等它真加载完
cd tools/fragstress && sudo nohup ./run.sh > /tmp/fragstress_run.log 2>&1 &
```

**★ 探针必须先挂**：eBPF 探针是被内核事件触发的，**探针没挂上的那段时间里发生的规整，事后一个都补不回来**。

### 三项硬检查（全过）

| 检查 | 实测 | 意义 |
|---|---|---|
| eBPF `outer_enter` vs `/proc/vmstat` `compact_stall` 增量 | **377 vs 377，一个不差**（★ 但**不是恒等**，见下） | 两个互不相干的计数器给出同一数字 → 工具没在瞎编 |
| `begin_reject > 0` | **171** | 三重来源过滤真在工作，不是摆设 |
| 直方图样本数 == `outer_exit − unpaired` | 377 == 377 − 0 | 没丢样本；LRU_HASH 没因容量不足丢记录 |
| `begin_accept ≫ outer_enter` | 484 > 377 | 内层是 per-zone 粒度（一次外层遍历多个 zone） |

**未配对率 0.00%**（377 进 / 377 出）。

> ★ **2026-08-13 修正：那条 377=377 不是恒等式。**
> `count_vm_event(COMPACTSTALL)` 在 `page_alloc.c:4410`，**前面 :4409 有个早退**
> `if (*compact_result == COMPACT_SKIPPED) return NULL;` ——
> **返回 SKIPPED 时 `compact_stall` 不涨。**
> 精确等式：`compact_stall == outer_exit − (返回 COMPACT_SKIPPED 的次数)`。
> 当日 `SKIPPED = 0` 所以减数为 0，等式退化成相等。**数据没错，我原来给的理由不完整。**
> 详细推导与"为什么不能和 begin 对账"见 **9.6 ①附**。
> **写报告时用带减法的那个式子。**

### eBPF 侧完整输出

```
外层进入 / 退出            377 / 377
内层 begin 接纳 / 被过滤   484 / 171
内层 end   接纳            484
migratepages 接纳 / 被过滤 1063 / 2075      ← 被挡掉的比收下的多一倍
外层结局：SUCCESS 376 (99.7%)  CONTENDED 1 (0.3%)
内层结局：SUCCESS 373  PARTIAL_SKIPPED 55  COMPLETE 51  CONTENDED 5   （合计 484 ✓）
累计迁移页：成功 166973 / 失败 8071  → 迁移失败率 4.6%
```

**`migratepages` 有 2/3 是噪声。** 不做来源过滤，"平均迁移多少页"会被 kcompactd
的后台活动污染成三倍。而 `compact_daemon_wake = 86` 从旁证实那些被挡掉的事件真实存在
（不是过滤器乱挡）—— **两份日志互相印证**。

### 真正的交付数据：代价有多大（`/proc/vmstat` 永远给不出）

```
每次 direct compaction 的总延迟（order = 9，即 2MB）
        512 ~ 1023   μs |  50
       1024 ~ 2047   μs | 141  ← 众数
       2048 ~ 4095   μs | 132
       4096 ~ 8191   μs |  31
       8192 ~ 16383  μs |   8
      16384 ~ 32767  μs |   5
      32768 ~ 65535  μs |   1  ← 最坏一次 65 毫秒
```

**一句话版本（可直接用于简历/面试）**：
**碎片让每次 2MB 分配同步阻塞 1~2 ms（众数），P99 约 16 ms，最坏 65 ms。**

这就是 v1 答不出、v2 能答的那个"所以呢"：
v1 只能说"碎片指数 0.87"；v2 能说"因此有 377 次分配被卡住，中位数 1~2ms，尾部 65ms"。

### `/proc` 侧（run.sh，与 eBPF 完全独立，不含一行 eBPF）

```
compact_stall 377 = compact_success 377 + compact_fail 0   （恒等式 ✓）
compact_daemon_wake 86
compact_isolated 1140569
free/migrate 扫描比 = 6209849/1615448 = 3.84   （>2：空位难找）
pgscan_direct 0   pgsteal_direct 0   allocstall_movable 0
PSI some/full = 694 ms / 689 ms
逐批：批1-4 stall=0（白拿区）→ 批5 stall=95 → 批6 134 → 批7 148
库存自检：可白拿 826 → 第一批跳到 743 → 目标保持 2000，提前收工于 1943
```

**库存自检成功"预测"了现象出现的时机**：预测批 1-4 是白拿区（stall=0），
实测正是如此；越过库存线的批 5 立刻出现 95 次规整。
**一个能预测自己什么时候会看到现象的实验，比"碰巧看到了"强得多。**

### ★★ 新发现 1：`377 stall / 0 pgscan_direct` 是 costly order 提前规整的实测证据

乍看矛盾：慢路径是"先回收后规整"，怎么可能规整 377 次而直接回收 0 次？

解释：hugetlb 要 **order-9 > 3 = costly order**，走的是
`__alloc_pages_slowpath()` **retry 循环之前**那一发提前规整（9.5 第②节的第①步）。
规整成功率 99.7%，拿到页就直接返回了，**retry 循环压根没进去**，
所以 `__alloc_pages_direct_reclaim()` 一次都没被调用。

> **这组数字只能由"规整发生在回收之前"来解释。**
> 它把内核对 costly order 的特殊处理**实测出来了**，不是从注释里读出来的。

对照：order-2（非 costly）**不可能**出现这个组合，它会先付回收的代价。

### ★★ 新发现 2：同一批数据能算出四个"成功率"，全都对

| 口径 | 算法 | 结果 | 回答的问题 |
|---|---|---|---|
| `compaction_made_progress`（严格） | 373/484 | **77.1%** | 内层每个 zone 真挪成了吗 |
| 不算"真失败"（宽松） | (484−51)/484 | **89.5%** | 排除"整个 zone 白扫"之外都算没输 |
| 外层返回值（**本工具报的**） | 376/377 | **99.7%** | 规整这次**行动**成功了吗 |
| `/proc/vmstat` 口径 | 377/377 | **100.0%** | **分配最终拿到页了吗** |

那 **1 次差异**的真实剧情：规整返回 `COMPACT_CONTENDED`（锁竞争放弃），
但分配器回头一试，页从别处拿到了 → **规整失败，分配成功**。
所以 vmstat 记成功、eBPF 记失败。**两者都没错，问的不是一个问题。**

**面试价值**：被问"你这个成功率怎么算的"，能答出四种口径 + 实测到的那 1 次差异
+ 差异的机制 —— 这比报一个数字强得多。

### ★ `PARTIAL_SKIPPED` 出现 55 次 = 计划书那个漏项的实测代价

计划书只列了 5 个 `status` 值，漏了 `PARTIAL_SKIPPED(6)`。本轮内层实测它出现 **55 次 / 484**。
**照计划书写会无声无息地丢掉 11% 的样本。**
→ 这是"埋点上线前必须实测 `format`、不能照文档抄"的直接证据。

### 本轮源码核对（本机 `include/linux/compaction.h` 可读，路径见 3.x）

`/lib/modules/5.15.0-139-generic/build/include/linux/compaction.h` **本机存在**
（BCC 编译就靠它）。已核对：`enum compact_result` **确为 9 个值**，
四个判定函数（`compaction_made_progress` 只认 `SUCCESS`、`compaction_failed` 只认
`COMPLETE`、`compaction_withdrawn` 收 `DEFERRED/CONTENDED/PARTIAL_SKIPPED`、
`compaction_needs_reclaim` 只认 `SKIPPED`）全部与 9.5 所述一致。

两条内核注释原文（**照抄，不是解读**）：

```c
	/* compaction didn't start as it was not possible or direct reclaim
	 * was more suitable */
	COMPACT_SKIPPED,
```
```c
static inline bool compaction_needs_reclaim(enum compact_result result)
{
	/* Compaction backed off due to watermark checks for order-0
	 * so the regular reclaim has to try harder and reclaim something. */
	if (result == COMPACT_SKIPPED)
```

→ **`SKIPPED` 不是"规整失败"，是"规整判断自己不该上、该让回收更卖力"**，
内核自己写的。同时这也是"若某轮全是 `SKIPPED`，则 `pgscan_direct` 必然**大**、
`compact_*_scanned` 必然**小**"的依据（易错点：`pgscan_direct` 是**回收**的扫描，
不是规整的扫描）。

### 本轮必须如实记下的三条

1. **没复现"成功率悬崖"。** 本轮成功率 100%、`compact_fail = 0`，因为脚本攒够
   377 次证据就**提前收工**了，停在 1943 个大页，没进入上轮那个失败区间。
   **3.9 那组悬崖数据至今只有一次观测，没有第二次独立复现。**
   要复现须调大 `STALL_GOAL` 或把目标顶到内存上限。
2. **`batches.csv` 异常没复现，但不等于已解决。** 本轮 7 行全部单调、
   增量合理、`95+134+148=377` 对得上。上轮那个"物理上不可能的负增量"**机制仍未查清**。
   "没再出现" ≠ "已修好"。
3. **12000 线程的 UNMOVABLE 污染仍不足以让规整失败**（`compact_fail = 0`）。
   说明规整失败**不是"有污染就会发生"，而是"污染密度 × 需求压力"共同越过门槛才发生**。

### 存疑状态（截至 2026-08-12）

| 编号 | 内容 | 状态 |
|---|---|---|
| — | `enum compact_result` 9 值 + 4 判定函数 | ✅ 本机头文件核对（`compaction.h` 在头文件包里） |
| 存疑 A | 慢路径 costly order 提前规整的条件 | ✅ **2026-08-12 源码核实，原判断正确**（见下） |
| 存疑 B | 双扫描器调用顺序；`isolate_freepages()` 是否只由 `compaction_alloc()` 触发 | ✅ **2026-08-12 源码核实，原判断正确**（见下） |

### 源码在哪（★ 取源码的正确姿势，别再走 apt 那条弯路）

```
/home/xxy/wlsp/ksrc-5.15.178/mm/{page_alloc.c, compaction.c, vmscan.c, internal.h}
```
（**在 Git 仓库之外**，不要提交进仓库）

**为什么是 5.15.178**：`cat /proc/version_signature` →
`Ubuntu 5.15.0-139.149~20.04.1-generic 5.15.178`，末尾就是**上游基线版本**。
`mm/` 核心逻辑基本是纯上游代码，Ubuntu 很少改动。

取法（**不需要 sudo**，单文件几十~两百 KB，秒级）：

```bash
D=/home/xxy/wlsp/ksrc-5.15.178/mm; mkdir -p $D
B='https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git/plain'
for f in page_alloc.c compaction.c vmscan.c internal.h; do
  curl -sS -m 90 -o "$D/$f" "$B/mm/$f?h=v5.15.178"
done
```

**★ 走 apt 的两条弯路，别重复踩**：

1. 包名不是 `linux-source-5.15.0`。本机是 Ubuntu 20.04（focal，原生内核系列 5.4），
   5.15 来自 **HWE 通道**（`linux-generic-hwe-20.04` 已装）。
   判断标准只看 `uname -r`，不看发行版版本。
2. **但正确包名 `linux-hwe-5.15-source-5.15.0` 装上也没用 —— 它是个空壳。**
   实测 `dpkg -L` 只有 `changelog.Debian.gz` 和 `copyright` 两个文件，
   `/usr/src` 下**一个 tarball 都没有**（下载量 74 kB 就已经说明问题，
   真源码是 100 MB+ 量级）。而且仓库里最新只有 `-126.136`，
   与运行的 `-139.149` 差 13 个 ABI 版本，`deb-src` 也未启用。
   → **教训：让别人装包之前先 `apt-cache show` 看 Size，装完先 `dpkg -L` 看清单。**

### 存疑 A 的答案（`page_alloc.c` 实测行号）

```c
	/*
	 * For costly allocations, try direct compaction first, as it's likely
	 * that we have enough base pages and don't need to reclaim. For non-
	 * movable high-order allocations, do that as well, as compaction will
	 * try prevent permanent fragmentation by migrating from blocks of the
	 * same migratetype.
	 */
	if (can_direct_reclaim && can_compact &&
			(costly_order ||
			   (order > 0 && ac->migratetype != MIGRATE_MOVABLE))
			&& !gfp_pfmemalloc_allowed(gfp_mask)) {
		page = __alloc_pages_direct_compact(...);
```

→ **提前规整不只给 costly order，还给"非 MOVABLE 的任意高阶分配"。**
所以 **order-2 的内核栈（`GFP_KERNEL` → UNMOVABLE）也走提前规整；
order-2 的用户匿名页（MOVABLE）不走，会先付回收的代价。**
（注：本版还多一个 `can_compact = gfp_compaction_allowed(gfp_mask)` 前置条件。）

### 慢路径顺序的精确行号（站④第 2 条的源码依据）

| 行 | 内容 |
|---|---|
| 4993 | `wake_all_kswapds()` ← 循环**之前** |
| **5016** | `__alloc_pages_direct_compact()` ← **★ costly / 非 MOVABLE 高阶的那一发提前规整** |
| 5058 | `retry:` ← 循环开始 |
| 5061 | `wake_all_kswapds()` |
| **5092** | `__alloc_pages_direct_reclaim()` ← 注释 `/* Try direct reclaim and then allocating */` |
| **5098** | `__alloc_pages_direct_compact()` ← 注释 `/* Try direct compaction and then allocating */` |
| 5115 / 5126 | `should_reclaim_retry()` / `should_compact_retry()` |

→ **提前规整在循环外（5016）；循环内回收（5092）严格早于规整（5098）。**
这就是 9.9 那个 `377 stall / 0 pgscan_direct` 的源码级解释。

### 存疑 B 的答案（`compaction.c` 实测）

```c
static struct page *compaction_alloc(struct page *migratepage, unsigned long data)
{
	if (list_empty(&cc->freepages)) {
		isolate_freepages(cc);        /* ← 第 1688 行，全文件唯一调用点 */
```

`isolate_freepages()` 在整个 `compaction.c` 里**只被调用一次**，
就在 `compaction_alloc()`（`migrate_pages()` 的 get_new_page 回调）里，
而且**只在"手上的落脚点用完了"时才调**。

而 `compact_zone()` 主循环第一句是 `switch (isolate_migratepages(cc))`，
遇到 `ISOLATE_NONE`（一个能搬的页都没找到）直接 `goto check_drain` ——
**整个迁移步骤被跳过，空闲侧扫描器一步都不走。**

→ **"空闲侧是被迁移侧叫来的"得到源码证实。** 两个扫描器的工作量在结构上就不对称，
这正是扫描比方向的成因：迁移侧找不到东西时，分母涨、分子不涨 → 比值变小。

---

## 9.10 补充材料（导师给的，见 `简述.pdf`）

- **BCC / eBPF 入门**：https://www.bilibili.com/video/BV1Ke411g739/
- **Linux 内存管理**：https://www.bilibili.com/video/BV134421S7qs/?p=107
  （重点看：碎片化、页块分配、分配器、伙伴系统、分配和回收机制）

导师列的"必须掌握的"7 条，与本节的对应关系：

| 导师要求 | 本节对应 |
|---|---|
| Tracepoint 和 kprobe 两种方式及原理 | 9.6 ② |
| eBPF 原理和运行流程 | 9.5 开头 + 9.6 |
| eBPF 如何与内核交互（map 共享数据） | 9.6 ⑤⑥ |
| Python 侧如何配合 eBPF | `extfrag.py` 的 `mode` 分支与 `get_compact_data()` |
| 钩子挂载的函数、什么时候被调用、伙伴系统/slub/内外碎片定义 | 9.3 + 9.5 |
| 碎片化指数怎么算的 | v1 的 `calculate_scoreA/B`（×1000 是因为 eBPF 没有浮点） |
| 整个项目运行逻辑（Python → 系统调用 → 内核 → 目标函数 → eBPF 收集 → 共享给应用） | 9.5 + 9.6 |
