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

| 站 | 主题 | 读什么 | 过关标准 |
|---|---|---|---|
| ① | 为什么需要 v2 | 本节 9.2 | 能说清"三个 0"是什么、为什么它让 v1 显得浅 |
| ② | 压力是怎么造出来的 | `fragstress/README.md` + `holes.c` + `kstack.c` | 能说清为什么"随机释放"是灵魂、为什么内核栈能钉死 pageblock |
| ③ | 实验时间线与四次失败 | 本节 9.4 | 能复述每一次失败的**根因**，不是"改了个 bug" |
| ④ | 慢路径的内核源码逻辑 | 本节 9.5 | 能画出 `__alloc_pages_slowpath` 的顺序，说清 reclaim 与 compaction 谁先谁后 |
| ⑤ | P0 探针的设计取舍 | `bpf/compactinfo.c` 的顶部大注释 + 本节 9.6 | 能说清"为什么要 kretprobe"和"三重来源过滤怎么做" |
| ⑥ | 自测 | 本节 9.7 | 能不看文档回答全部问题 |

**时间预算**：①②③ 约 2 小时，④ 约 2 小时（最硬的一站），⑤ 约 1.5 小时，⑥ 1 小时。

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

```
__alloc_pages_slowpath():
  ① 若 order > 3（costly order）：进循环前**先来一发 direct compaction**
  ② 进 retry 循环：
       a. wake_all_kswapds()             唤醒后台回收线程
       b. get_page_from_freelist()       再试一次快路径
       c. __alloc_pages_direct_reclaim() ← **先回收**
       d. __alloc_pages_direct_compact() ← **后规整**
       e. 判断要不要 retry / 要不要 OOM
```

**必须记住的两条**：

1. **retry 循环里是"先 reclaim 后 compaction"**，
   只有 costly order（order > 3）在**进循环之前**有一次提前规整。
2. **"direct" 的含义是"发起分配的那个进程自己同步做这件事、被阻塞在这里"** ——
   与之相对的是 kswapd（后台回收）和 kcompactd（后台规整），那两个不阻塞谁。

**为什么是"先回收后规整"**（这个因果链要能讲）：
规整是"搬家"，搬家需要**目标空位**。如果空闲页太少，连搬家用的落脚点都凑不出来，
规整无从下手 —— 所以要先回收出一些空闲页。
这一点在 `enum compact_result` 里有直接对应：
`COMPACT_SKIPPED(1)` 的语义就是 **"别规整了，先去回收"**
（内核判定函数叫 `compaction_needs_reclaim()`）。

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

三条推论：

1. **`order` 只有最外层有** → 必须两层配对（用 tid 做 key 把外层查出来）。
   **这是"为什么不能只挂 begin/end"的答案。**
2. **`begin` 次数 ≫ `compact_stall`** → 能和 `/proc/vmstat` 对账的是
   **外层次数**，不是内层 begin 次数。
3. **`migratepages` 一次 begin/end 内会打多次** → `nr_migrated/nr_failed`
   必须**累加**，取最后一次会严重低估。

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

**判据：只有 direct compaction 会经过 `try_to_compact_pages`**（②③ 直接调
`compact_zone`）→ 内层探针的准入条件是"**当前 tid 有没有活跃的外层记录**"。

**必须在内核态做**（决策 #7）：捞到用户态再扔，等于白付一次 map 写入
加一次 perf 传输的开销。

**而且这个过滤器能自证**：`begin_reject` 计数器 > 0 就证明它真的挡住了东西，
不是"恰好没有噪声"（实测 `compact_daemon_wake` 涨了 62，噪声确实存在）。

### ④ 为什么 map key 用 tid 而不是 tgid（决策 #5）

`bpf_get_current_pid_tgid()` 返回 u64：**高 32 位是 tgid（进程），低 32 位是 pid（线程）**。
v1 的 `extfraginfo.c` 用 `>>32` 取 tgid 做按进程聚合，那是对的。

但 compaction / reclaim 是**线程**行为：同一个进程的两个线程同时进慢路径，
用 tgid 做 key 会**互相覆盖**，配对全乱。所以 v2 用**完整的 u64**（即 tid）。

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

| 题号 | 去哪找 |
|---|---|
| 1 | 9.2 |
| 2~5 | 9.3 |
| 6, 7, 8, 9, 11 | 9.5 |
| 10, 12, 13, 14, 15 | 9.6 |
| 16, 17, 18 | 9.4 |

---

## 9.8 补充材料（导师给的，见 `简述.pdf`）

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
