# fraginfo v2 施工记录

> **这份文件是什么**:v2 升级过程的**逐步操作日志 + 知识点笔记**。
> 计划书(`fraginfo_v2.md`)回答"要做什么",这份文件回答"**实际做了什么、为什么这么做、过程中学到了什么**"。
>
> **记录规则**(每一步固定四段):
> 1. **做了什么** —— 完整命令行 / 改了哪个文件
> 2. **实测输出** —— 关键原始数据(不是结论,是可核对的原文)
> 3. **本质说明** —— 这一步背后内核/eBPF 在干什么,为什么非做不可
> 4. **问答与知识点** —— 我(用户)问的问题、执行员想讲的原理,原样留档
>
> 阶段性任务报告(`报告_P0.md` / `报告_P1.md`)会从这份文件里提取素材。

---

## 步骤 0:环境事实核对(2026-08-02)

### 做了什么

在不需要 root 的前提下,把交接手册里列的"环境事实"逐条实测确认一遍,
避免拿着别人的结论施工。

```bash
uname -r
grep -E "compact_|allocstall|pgscan_direct|pgsteal_direct|thp_fault|nr_free_pages" /proc/vmstat
cat /sys/kernel/mm/transparent_hugepage/enabled /sys/kernel/mm/transparent_hugepage/defrag
grep -E "Hugepagesize|MemTotal|Slab|SUnreclaim" /proc/meminfo
cat /proc/pressure/memory
python3 -c "import bcc; print(bcc.__file__)"
which gcc make stress-ng; nproc
```

### 实测输出

```
内核     5.15.0-139-generic  x86_64  虚拟机
内存     MemTotal 12208080 kB (约 11.6 GiB),nr_free_pages 580695 (约 2.2 GiB 空闲)
CPU      4 核
慢路径   compact_stall 0   compact_fail 0   compact_success 0   compact_daemon_wake 0
         compact_migrate_scanned 0   compact_free_scanned 0   compact_isolated 0
         allocstall_dma/dma32/normal/movable 全 0
         pgscan_direct 0   pgsteal_direct 0   pgscan_direct_throttle 0
THP      enabled = always [madvise] never
         defrag  = always defer defer+madvise [madvise] never
         thp_fault_alloc 0   thp_fault_fallback 0
PSI      some total=0    full total=0
slab     Slab 379156 kB   SUnreclaim 126404 kB
大页     Hugepagesize 2048 kB  →  pageblock_order = 9(2MB),P2 归因要用
工具链   gcc / make / stress-ng 已装;bcc 用旧包名(import bcc 可用,bpfcc 不存在)
```

### 本质说明

**全 0 意味着这台机器从开机到现在,一次都没进过 `__alloc_pages_slowpath()` 的
阻塞分支。** 这不是环境坏了,而是慢路径的正常状态 —— 它是"压力下才出现"的路径:

- `compact_stall` = 进程因为**直接内存规整**被同步卡住的次数
- `allocstall_*` / `pgscan_direct` = 进程被拉去干**直接回收**的次数
- `PSI total=0` = 内核自己也认为"没有任何任务因为内存问题被阻塞过"

**三套互相独立的内核统计同时报 0**,这是压力基座(P-1)必须排在最前面的**实证依据**,
不是拍脑袋的排期。观测代码写得再对,没有事件流过埋点,采到的就是空表。

### 知识点

- **`nr_free_pages` 580695 页 ≈ 2.2 GiB 空闲**,但空闲总量大 ≠ 能凑出连续大块。
  v1 的 `score_a`(`__fragmentation_index`)量化的正是这个差距 —— 这也是"外碎片"的定义。
- **`Hugepagesize` 决定 `pageblock_order`**。x86_64 上 pageblock 大小 = 巨页大小 = 2MB
  = 512 页 = order-9。migratetype 是**按 pageblock 为单位**标记的,不是按页 ——
  所以"污染"的最小单位是 2MB。这是 P2 层次 2 归因的基本刻度。
- **只有 4 核**:档位 2 用"海量线程"制造 UNMOVABLE 污染时,起作用的是
  **每个线程 order-2 的内核栈占住 pageblock**,不是并发度。线程数要按几千量级堆,
  跟核数无关。

---

## 步骤 1:compaction 埋点字段实测核对(2026-08-02)

### 做了什么

计划书 §3.2 的字段是"按 5.15 推测"写的,动手前必须拿**本机 tracefs 的 format 文件**
对账。这一步也是任务报告第 2 项("埋点实测确认")的原始素材。

```bash
sudo ls /sys/kernel/debug/tracing/events/compaction/
sudo cat /sys/kernel/debug/tracing/events/compaction/mm_compaction_try_to_compact_pages/format
sudo cat /sys/kernel/debug/tracing/events/compaction/mm_compaction_begin/format
sudo cat /sys/kernel/debug/tracing/events/compaction/mm_compaction_end/format
sudo cat /sys/kernel/debug/tracing/events/compaction/mm_compaction_migratepages/format
grep -n -A25 "enum compact_result" /usr/src/linux-headers-$(uname -r)/include/linux/compaction.h
```

### 实测输出

**本机 `compaction/` 下共 14 个 tracepoint**:

```
mm_compaction_begin                  mm_compaction_end
mm_compaction_try_to_compact_pages   mm_compaction_migratepages
mm_compaction_isolate_migratepages   mm_compaction_isolate_freepages
mm_compaction_suitable               mm_compaction_finished
mm_compaction_defer_compaction       mm_compaction_deferred
mm_compaction_defer_reset            mm_compaction_wakeup_kcompactd
mm_compaction_kcompactd_wake         mm_compaction_kcompactd_sleep
```

**四个要用的埋点,字段实测**(offset/size 直接抄自 format):

| tracepoint | ID | 字段(offset / size / 类型) |
|---|---|---|
| `try_to_compact_pages` | 559 | `order` 8/4 int;`gfp_mask` 12/4 gfp_t(unsigned);`prio` 16/4 int |
| `begin` | 561 | `zone_start` 8/8;`migrate_pfn` 16/8;`free_pfn` 24/8;`zone_end` 32/8(均 unsigned long);`sync` 40/1 bool |
| `end` | 560 | 同 begin 五个字段 + `status` **44**/4 int |
| `migratepages` | 562 | `nr_migrated` 8/8;`nr_failed` 16/8(均 unsigned long) |

**`status` 的实际取值**(两个来源交叉确认,完全一致):

来源一 —— `end/format` 的 `print fmt` 里的 `__print_symbolic`:
```
{0,"not_suitable_zone"} {1,"skipped"} {2,"deferred"} {3,"no_suitable_page"}
{4,"continue"} {5,"complete"} {6,"partial_skipped"} {7,"contended"} {8,"success"}
```

来源二 —— `include/linux/compaction.h` 的 `enum compact_result` 声明顺序:
```
0 COMPACT_NOT_SUITABLE_ZONE   ← 注释:internal to compaction,仅供 tracepoint 细化输出
1 COMPACT_SKIPPED             ← 注释:没能启动,或"直接回收更合适"
2 COMPACT_DEFERRED            ← 注释:因过去连续失败被推迟
3 COMPACT_NO_SUITABLE_PAGE    ← internal
4 COMPACT_CONTINUE            ← internal,应继续扫下一个 pageblock
5 COMPACT_COMPLETE            ← 整个 zone 扫完了仍没成功
6 COMPACT_PARTIAL_SKIPPED     ← 扫了一部分就退避,没扫完
7 COMPACT_CONTENDED           ← 锁竞争,提前终止
8 COMPACT_SUCCESS             ← 判定分配现在能成功了
```

### 与计划书的差异(要写进任务报告)

| 项 | 计划书 §3.2/§3.4 | 本机实测 | 结论 |
|---|---|---|---|
| `try_to_compact_pages` 字段 | order / gfp_mask / prio | 完全一致 | ✅ 按计划书写 |
| `begin` 字段 | zone_start / migrate_pfn / free_pfn / zone_end / sync | 完全一致 | ✅ |
| `end` 字段 | begin + status | 完全一致 | ✅ |
| `migratepages` 字段 | nr_migrated / nr_failed | 完全一致 | ✅ |
| **`status` 取值表** | 只列了 5 个(SKIPPED/DEFERRED/SUCCESS/COMPLETE/CONTENDED) | **实际 9 个** | ⚠️ **要补全**,漏了 NOT_SUITABLE_ZONE(0) / NO_SUITABLE_PAGE(3) / CONTINUE(4) / **PARTIAL_SKIPPED(6)** |
| `gfp_mask` 宽度 | `u32 gfp_mask` | `size:4 signed:0` | ✅ u32 正确 |

**最重要的一条差异**:`COMPACT_PARTIAL_SKIPPED(6)` 计划书没提,但它是**失败结果里
很常见的一种** —— 扫描器还没扫完整个 zone 就退避了。如果按计划书那张 5 行表写代码,
status=6 会掉进"未知"分类,而且会被误当成"不算失败"。**成功率会算错。**

另外 `0/3/4` 三个值头文件明确标注 *internal to compaction*:它们是
`compact_zone()` 主循环内部的中间状态,主要打在 `mm_compaction_finished` 上;
`mm_compaction_end` 打的是 `compact_zone()` 的**返回值**,理论上只会是
`5(COMPLETE) / 6(PARTIAL_SKIPPED) / 7(CONTENDED) / 8(SUCCESS)` 这几个。
**这个"理论上"要用实测数据验证**,代码里 9 个值全部保留分类,不做假设。

### 本质说明:这几条命令到底在干什么

**1. `/sys/kernel/debug/tracing/` 是 tracefs,内核埋点的"目录"。**

内核源码里每写一句 `trace_mm_compaction_begin(...)`,编译时就会在这个目录下
自动生成一个同名子目录,里面的 `format` 文件描述**这个埋点会往环形缓冲区里
写一条什么样的二进制记录**。所以 `format` 不是文档,是**内核当前二进制的自述**——
它跟你正在跑的这个内核 100% 一致,不会像文档那样过期。

**2. `ls compaction/` 是在问:本机内核到底编进了哪些埋点。**

埋点受 `CONFIG_*` 控制,不同发行版、不同版本给的不一样。看到 14 个全在,
说明 `CONFIG_COMPACTION=y` 且 tracepoint 没被裁剪 —— **P0 的三层挂载点在本机是可行的**。
这一条确认了才有资格往下写代码。

**3. `cat .../format` 是在读"字段契约"。**

BCC 的 `TRACEPOINT_PROBE(compaction, mm_compaction_end)` 里,`args->status`
这个写法**能不能编译过、读到的是不是正确的字节**,完全取决于这个 format。
字段名拼错 → 编译报错(还算好);字段类型猜错(比如把 4 字节的 `gfp_t` 当成 8 字节读)
→ **编译能过,数据是垃圾**,而且是那种看起来像数字、实际全错的垃圾。
所以这一步是**唯一能在写代码之前排掉这类静默错误的手段**。

**4. `grep enum compact_result` 是在给数字找含义。**

format 里 `status` 只是个 `int`。`8` 是什么意思?必须去头文件查枚举。
本机 `/usr/src/linux-headers-$(uname -r)/` 里有内核头文件,这是**和运行内核同版本**的
声明,比翻网上的源码可靠。

### 问答与知识点

#### Q(用户):这些操作是做什么的?

见上面"本质说明"四段。一句话总结:**在写任何一行 eBPF 代码之前,先把"我要读的
字段叫什么、多宽、值域是什么"从内核自己嘴里问出来**,而不是从计划书或网上抄。
这是 v1 里已经用过一次的方法(`extfraginfo.c` 的注释里就贴着
`mm_page_alloc_extfrag/format` 的原文),v2 继续沿用。

面试里如果被问"tracepoint 和 kprobe 怎么选",这一步就是最好的论据:
**tracepoint 有稳定的 format 契约可以提前核对,kprobe 挂的是函数符号,
参数顺序随内核版本变,没有任何东西能提前校验。**

#### 知识点 A:`print fmt` 里的 `__print_symbolic` 是白送的枚举表

`end/format` 最后那一大段 `print fmt` 平时没人看,但它里面的
`__print_symbolic(REC->status, {1,"skipped"}, ...)` **直接把枚举值和名字的映射
写死在里面了** —— 而且这份映射是编译内核时生成的,和二进制严格一致。

同理 `try_to_compact_pages` 那一长串 `__print_flags(REC->gfp_mask, "|", {...})`
就是一张**完整的 GFP 标志位表**,连数值都给了:

```
__GFP_DIRECT_RECLAIM = 0x400      __GFP_KSWAPD_RECLAIM = 0x800
__GFP_IO   = 0x40                 __GFP_FS    = 0x80
__GFP_MOVABLE = 0x08              __GFP_RECLAIMABLE = 0x10
__GFP_NORETRY = 0x10000           __GFP_RETRY_MAYFAIL = 0x4000
GFP_TRANSHUGE / GFP_TRANSHUGE_LIGHT / GFP_KERNEL / GFP_ATOMIC ... 的组合值
```

**这对 P0 直接有用**:计划书 §1.1 说"能不能规整取决于 gfp_mask",
有了这张表,就能在用户态把 `gfp_mask` 解析成人话
(比如判断这次分配是不是 THP:`gfp_mask` 匹配 `GFP_TRANSHUGE*`)。
**不用去翻 `include/linux/gfp.h`,内核已经把表贴脸上了。**

#### 知识点 B:offset 里有空洞,所以绝不能自己算结构体布局

`end` 的字段偏移:`sync` 在 **40**、size 1;`status` 在 **44**。
中间 41/42/43 三个字节是**编译器为了让 int 4 字节对齐插的 padding**。

如果自己照着字段列表手抄一个 C 结构体去解析,很容易写成 `sync` 后面紧跟 `status`
(offset 41),**读出来全是错的**。BCC 的 `args->` 之所以安全,正是因为它是拿
format 里的 offset 自动生成访问代码的。
**结论:永远用 `args->字段名`,永远不要自己算偏移。**

#### 知识点 C:三个埋点的粒度完全不同,这决定了代码怎么写

```
一次 direct compaction(进程被卡住的一次)
└── try_to_compact_pages          ← 1 次,外层边界,唯一带 order
    ├── begin(zone A) ... end(zone A)     ← per-zone,一次分配要遍历多个 zone
    │   ├── migratepages          ← per 迁移批次!主循环每搬一批打一次
    │   ├── migratepages
    │   └── ...
    └── begin(zone B) ... end(zone B)
```

三条推论,直接影响 `compactinfo.c` 的写法:

1. **`order` 只有最外层有** → 想按 order 分维度做直方图,必须先建立
   "外层 → 内层"的关联,靠 tid 做 key 把外层记录查出来。这就是计划书要求
   两层配对的真正原因,不是为了好看。
2. **`begin/end` 的次数会明显多于 `compact_stall`** → 交叉验证时,
   能和 `compact_stall` 对上的是**外层次数**,不是内层 begin 次数。
   报告里的对账表要写清楚是哪一层的计数。
3. **`migratepages` 在一次 begin/end 内会打多次** → `nr_migrated / nr_failed`
   必须**累加**,取最后一次会严重低估。计划书 §3.4④ 说它"比 pfn 差值可靠",
   前提是累加着用。

#### 知识点 D:`COMPACT_SKIPPED` 的真实含义,呼应慢路径的顺序

头文件对 `COMPACT_SKIPPED` 的注释是:
> compaction didn't start as it was not possible **or direct reclaim was more suitable**

再看同一个头文件里的判定函数:
```c
/* Compaction needs reclaim to be performed first, so it can continue. */
static inline bool compaction_needs_reclaim(enum compact_result result)
{
    /* Compaction backed off due to watermark checks for order-0 ... */
    if (result == COMPACT_SKIPPED)  return true;
}
```

**`SKIPPED` 不是"规整失败",而是"规整判断自己不该上,该让回收先干"** ——
空闲页太少,连做迁移用的目标页都凑不出来,这时候搬家没意义,得先腾地方。
这正好从代码层面印证了计划书 §1.1 强调的那个顺序:
**retry 循环里 reclaim 在前、compaction 在后**,因为 compaction 依赖 reclaim
先把水位顶起来。

另外两个判定函数也值得记(面试能直接用):
- `compaction_failed()`:**只有 `COMPACT_COMPLETE` 才算真失败**(整个 zone 白扫完)
- `compaction_withdrawn()`:`DEFERRED` / `CONTENDED` / `PARTIAL_SKIPPED`
  算"主动退避",内核认为**再试一次(用更高优先级)还有戏**

**所以"规整成功率"至少有三种算法**:`status==SUCCESS` 的比例、
`1 - failed 比例`、`1 - (failed + withdrawn) 比例`。工具里要说清自己用的是哪一种,
不能含糊。这比多加一个指标有价值得多。

### 遗留问题

1. **本机没有内核 C 源码,只有头文件。** `/usr/src/` 下只有
   `linux-headers-5.15.0-139-generic`,没有 `mm/compaction.c` / `mm/page_alloc.c`。
   计划书 D1 要求"读 `compact_zone()` 主循环"、以及要确认
   `count_vm_event(COMPACTSTALL)` 到底打在哪一行,都需要源码。
   **待办**:装源码包再确认(命令见下方"下一步")。
2. 以下两条是执行员根据 5.15 上游代码的记忆写的,**尚未在本机核实,先标为存疑**:
   - `COMPACTSTALL` 计数点在 `__alloc_pages_direct_compact()` 里,而且
     **`compact_result == COMPACT_SKIPPED` 时会提前 return、不计数** →
     若属实,则"外层 try_to_compact_pages 次数 **≥** `compact_stall`",
     差值恰好是 SKIPPED 的那些。这条直接决定交叉验证表怎么解释偏差。
   - direct compaction 整段被 `psi_memstall_enter/leave()` 包住 →
     若属实,PSI 的 `total` 和本工具统计的阻塞时长**同源**,量级必须对得上;
     对不上就说明配对逻辑有问题。这是计划书选 PSI 做第二验证线的底层原因。

---

## 步骤 1.5:基础补课(2026-08-04)

### 背景

步骤 1 的讲解一次性抛出了太多术语,用户明确反馈 **"我听不懂你在说什么"**。
退回来用大白话重讲了三块。**这次的教训写进了 `handoff/com_memory.md` 第〇节:
先讲为什么,再讲怎么做;先给比喻,再给术语;一次只讲一个概念。**

以下是补课内容存档,后续讲解请**沿用同一套比喻**,不要另起炉灶。

---

### 补课一:内存碎片 & 内存规整

#### 1. 内存是按"块"发的

物理内存最小单位是**页**(4KB)。但内核不是一页一页随便发,
而是**只发 2 的整数次方页的连续块**,这套机制叫**伙伴系统**:

| 叫法 | 大小 |
|---|---|
| order-0 | 1 页 = 4KB |
| order-1 | 2 页 = 8KB |
| order-2 | 4 页 = 16KB |
| order-9 | 512 页 = 2MB |

**order 越高 → 要求的连续内存越长 → 越难凑出来。**

#### 2. ★ 停车场比喻(用户认可,后续统一沿用)

| 比喻 | 对应 |
|---|---|
| 停车场 | 物理内存 |
| 车位 | 页(4KB) |
| 车 | 进程/内核的数据 |
| **需要连续 8 个车位的大巴** | **高阶分配(order-3)** |
| **趴窝开不走的车** | **UNMOVABLE 页(内核数据结构)** |
| **停车场划片区** | **migratetype 分组(pageblock,2MB 一块)** |

**碎片**:100 个车位停了 50 辆,空 50 个位,但每个空位都被车隔开 ——
总数够,**连续的不够**,大巴停不进来。

对应到内存:`nr_free_pages` 显示空着 2.2GB,但可能一个 2MB 连续块都凑不出来。
**v1 算的两个碎片指数,量化的就是"空闲总量"和"能用的连续块"之间的这个差距。**

#### 3. 谁需要大块

order-0 几乎永远能满足,普通程序遇不到碎片问题。**只有要大块的才撞墙**:
- **THP(透明大页)** —— 一次要 2MB 连续(order-9),最典型
- **hugepages** —— 同上,手动预留
- **内核栈** —— 每创建一个线程,内核给 16KB 连续(order-2)
- **网卡驱动的 DMA 缓冲区** —— 硬件要求物理连续

> **关键边界条件:order == 0 的分配永远不会触发规整。**
> 所以造压力的本质 = **制造大量高阶分配需求**。

#### 4. 规整 = 挪车

```
挪之前:  [车][空][车][空][车][空][车][空]
挪之后:  [车][车][车][车][空][空][空][空]   ← 连续 4 个空位出来了
```

内核派**两个扫描器**:一个从低地址往上找"能挪走的页",一个从高地址往下找"空位",
相向而行,把页搬进空位,直到两个扫描器相遇。这就是**双扫描器**机制。
搬完改页表让进程指向新位置,进程全程不知情。

#### 5. ★ 为什么规整会失败:有些车挪不走

用户程序的内存(匿名页、文件缓存)都**能挪** —— 改页表就行。
但**内核自己的数据结构挪不走**:内核代码里到处是指向它们的裸指针,挪一下全废。
这类叫 **UNMOVABLE**。

**一辆趴窝的车堵在中间,整个片区就永远凑不出连续车位。**
这就是规整失败的根本原因。

#### 6. 分组与"污染"

内核的应对:**划片区**(migratetype),能挪的停一片,挪不走的停另一片。
理想情况规整只需要去"能挪"那片干活。

**但分组会被打破**:"挪不走"那片满了,内核不能让分配失败,只好
**去"能挪"的片区借一块** —— 借完那块地方就混进了挪不走的车。
这就是 **fallback 污染**。

> **这里正是 v1 和 v2 接上的地方**:v1 的 `extfraginfo.c` 挂的
> `mm_page_alloc_extfrag` 埋点,抓的就是"跨区借块"这个动作 ——
> **它记录了每一次污染,以及是哪个进程干的**。P2 归因就靠它。

#### 7. 代价:v2 真正要测的东西

一个进程申请 2MB 内存时:

```
① 快路径:直接从伙伴系统拿现成大块
   有 → 几微秒返回                      ← v1 观测这里
   没有 ↓
② 慢路径:内核开始想办法
   ├─ 直接回收(reclaim)   :内存总量不够 → 换出/丢弃页,腾空间
   └─ 直接规整(compaction):内存够但碎   → 挪车,拼连续块
                                          ← v2 观测这里
③ 都失败 → 分配失败 / OOM 杀进程
```

**"直接(direct)"是重点**:干这些活的不是后台线程,
**就是申请内存的那个进程自己**。它被卡在内核里一动不动,可能 200ms 甚至更久。

> **碎片本身不可怕,碎片把分配从快路径逼进慢路径才可怕。**
> **v2 量化的就是进程被卡住的这段时间。**

而且能分出两种情况,这是工具的核心价值:
- 卡了 200ms **规整成功了** → 钱花得值
- 卡了 200ms **扫完一整片啥也没凑出来** → **纯亏**

---

### 补课二:eBPF 和"打卡点"

#### 1. 问题:怎么知道内核在干什么

内核在你的程序之下运行,看不见。传统办法是改内核源码加打印 → 重编译 → 重启。
**没人受得了。**

#### 2. eBPF:往内核里塞一小段程序

允许你写一小段程序,**动态加载进正在运行的内核**,挂在某个位置;
内核每次执行到那里,你的程序就跑一次。不改内核、不重启、卸载干净。

安全靠 **verifier(验证器)**:加载前全部走查,
**不许无限循环、不许乱访问指针、不许调用任意函数**,过不了就拒绝加载。

> 这就是为什么 eBPF 里写 C 处处受限。v1 代码里的 `bpf_probe_read_kernel()`
> 就是典型:**不能直接 `zone->free_area[order].nr_free` 解引用内核指针,
> 必须用专门函数安全拷贝**,否则 verifier 直接拒。

#### 3. 挂在哪:两种挂法

| | **kprobe** | **tracepoint(打卡点)** |
|---|---|---|
| 挂在哪 | 任意内核**函数**入口 | 内核作者**预先埋好**的位置 |
| 灵活性 | 高,想挂哪挂哪 | 低,只有埋了的能挂 |
| 稳定性 | **差**,函数改名/参数变就废 | **好**,是对外承诺的接口 |
| 能否提前校验 | 不能 | **能** —— 就是 `format` 文件 |

**v1 两种都用了**:`fraginfo.c` 是 kprobe(挂 `get_page_from_freelist`),
`extfraginfo.c` 是 tracepoint(挂 `kmem:mm_page_alloc_extfrag`)。
**v2 的两个新模块全用 tracepoint。**

> **这解释了步骤 1 在干什么**:tracepoint 每次打卡记一条记录,
> `format` 文件就是这条记录的**表头说明**(哪几列、每列多宽、什么类型)。
> `cat` 出来核对 = 确认表头,免得写代码时把列读错。

#### 4. map:内核和用户态之间唯一的桥

eBPF 程序跑在内核里,**不能 printf、不能写文件**。数据怎么给 Python?
答案是 **map** —— 一块内核和用户态都能访问的共享区,像个字典。

> v1 的 `BPF_HASH(zone_map, ...)` 就是它。内核态算完写进去,
> `extfrag.py` 里 `self.b["zone_map"]` 读出来打印。**唯一通道。**

#### 5. BCC:把这一切粘起来

用 **Python 主程序 + 内嵌 C** 写工具:Python 负责加载、读 map、打印;
C 负责在内核里采集。

**重要特性:BCC 是运行时现场编译的** —— 每次跑 `python3 extfrag.py`,
它当场把 `.c` 编译成 eBPF 字节码再加载。

> **这解释了那条硬性要求:P0 和 P1 必须分两个 `.c`,不许合并。**
> 合并了,只想看 reclaim 时 compaction 那套埋点也会被一起挂上,白付开销。
> v1 拆成两个 `.c` 也是同样道理。

---

### 补课三:项目全貌与进度

#### v1(已完成)回答两个问题

| 模块 | 挂法 | 回答 |
|---|---|---|
| `fraginfo.c` | kprobe `get_page_from_freelist` | **现在有多碎?** 扫每个 zone 每个 order 算碎片指数 |
| `extfraginfo.c` | tracepoint `mm_page_alloc_extfrag` | **谁在制造碎片?** 每次跨区借块记下是哪个进程 |

#### v2 回答:碎片让谁付出了什么代价

| 模块 | 是什么 | 状态 |
|---|---|---|
| **P-1 `fragstress/`** | 压力注入器 —— 把机器折腾到内存变碎、且不断有大块需求 | 下一步 |
| **P0 `compactinfo.c`** | 观测**规整**:每次卡多久、成没成功 | 排队 |
| **P1 `reclaiminfo.c`** | 观测**回收**:每次卡多久、回收了多少页 | 排队 |
| **P2 归因** | 把"谁污染的"和"谁被卡的"对上 —— 最出彩的部分 | 暂不做 |

#### 为什么 P-1 必须排第一

```
compact_stall 0   ← 没有任何进程因规整被卡过
pgscan_direct 0   ← 没有任何进程因回收被卡过
PSI total = 0     ← 内核自己也说:没有任务因内存问题阻塞过
```
**三套独立统计同时报 0。** P0 代码写得再完美,跑起来也是空表。

#### 交付节奏

- **阶段一** = P-1 + P0 → 出 `报告_P0.md`,用户拿去给评审员
- **阶段二** = P1 → 出 `报告_P1.md`
- P2 等前两阶段评审完再说

---

## 步骤 1.6:建立跨会话/跨机器交接机制(2026-08-04)

### 做了什么

用户要远程办公,会在**另一台电脑**上继续这个项目,需要多个 AI 会话共享同一份记忆。

新建 `handoff/` 两份文档,并把整个项目目录初始化为 Git 仓库推到 GitHub:

| 文件 | 管什么 |
|---|---|
| `handoff/com_memory.md` | **状态**:已确认的事实、已定的决策(12 条)、未决问题、环境事实、文件地图 |
| `handoff/handoff_task.md` | **行动**:下一个会话具体干什么、开工检查清单、P-1 详细设计、报告格式 |

远端:`https://github.com/2862426199qq-beep/linux_eBPF.git`,仓库根 = 项目目录。

### 本质说明

**这不是"备份",是"让状态可迁移"。**

一个 AI 会话的上下文是易失的——关掉就没了;换台电脑,新会话对项目一无所知。
唯一能跨会话、跨机器传递的,是**写进文件并同步到远端的东西**。

所以设计上做了**状态/行动分离**:
- `com_memory.md` 记**不会随进度变的事实**(埋点字段、已定决策、环境数据)
- `handoff_task.md` 记**随进度变的行动**(下一步干什么)

这样每次收工只需要更新后者 + 给前者追加一行日志,不用重写整份文档。

### 知识点:两条容易被忽略的坑

**① 环境事实会随机器失效。**
`com_memory.md` 第二节顶部加了醒目警告:换机器后 `uname -r` / `nproc` /
`MemTotal` 任一不符,**整节数据作废必须重测**。
否则新会话会拿着虚拟机的数据在另一台机器上做判断,结论全错。

**② 未验证的推断必须标记出来。**
`com_memory.md` 里把两条"凭上游代码记忆写的"结论明确标成 **存疑A / 存疑B**,
并写明"核实之前不要当作事实写进任何报告"。
**观测工具项目里,分不清"实测的"和"推断的"是最危险的事** ——
这也正是计划书要求做 vmstat + PSI 双线交叉验证的同一个道理。

---

---

## 步骤 2:写 `tools/fragstress/`(P-1 压力注入器,最短链路版)

**用户决策(2026-08-09)**:采用"**先打通最短链路**"——只写 `holes.c` + `kstack.c`
+ `hugetlb.sh` 三个文件,立刻验证 `compact_stall` 动不动;动了再补齐
`sockflood.c` / `dentry.sh` / `thpload.c` / `run.sh`。
理由:如果方向就是错的(比如这台 12G 虚拟机压根压不出规整),只白写三个文件。

第二个待确认项(`extfrag.py` 怎么加命令行入口)与写 C 程序无关,**推迟到步骤 5 再问**。

### 2.1 开工前的环境复核(2026-08-09 实测)

| 项 | 值 | 结论 |
|---|---|---|
| `uname -r` | 5.15.0-139-generic | 同一台机器 |
| `nproc` | 4 | 同一台机器 |
| `MemTotal` | **12208084 kB** | 记录里原写 12208080,差 4 kB,以本次为准 |
| `compact_stall` / `pgscan_direct` | 0 / 0 | 仍然全 0 |
| PSI memory | `some total=0` / `full total=0` | 仍然全 0 |

### 2.2 ★ 与计划书不符的重大发现:`CONFIG_VMAP_STACK=y`

计划书与交接文档都写"线程内核栈是 **order-2 UNMOVABLE 连续块**"。**本机不是。**

```
$ grep -E "^CONFIG_(VMAP_STACK|HAVE_ARCH_VMAP_STACK)" /boot/config-5.15.0-139-generic
CONFIG_HAVE_ARCH_VMAP_STACK=y
CONFIG_VMAP_STACK=y
```

开启 VMAP_STACK 后,内核栈走 **vmalloc**:16KB 的栈 = **4 个互不相邻的 order-0 页**,
靠页表映射成连续的虚拟地址,**物理上完全不连续**。

**这对本项目是好消息**(而且是面试可讲的点):

> 一个 order-2 连续块,最多毁掉 1 个 pageblock 的连续性;
> 4 个散落的 order-0 UNMOVABLE 页,可能毁掉 **4 个不同的 pageblock**。
> 规整能否凑出 order-9(512 个连续页),取决于这 512 页里有没有搬不走的页,
> 所以"搬不走的页越分散,杀伤力越大"。

**衍生结论**:VMAP_STACK 有 per-CPU 栈缓存(`NR_CACHED_STACKS=2`),
线程退出时栈会被缓存复用。所以 `kstack.c` **必须让线程一直活着**——
反复 create/join 会一直命中缓存,根本不向伙伴系统要新页,等于没压。

**★ 报告_P0.md 必须写进这条偏离**,不能沿用"order-2 内核栈"的错误说法。

### 2.3 其他实测事实

| 项 | 实测 | 影响 |
|---|---|---|
| THP `enabled` | `always [madvise] never` | 印证:hugetlb 当第一发子弹是对的 |
| THP `defrag` | `... [madvise] never` | 只有 `MADV_HUGEPAGE` 区域才同步规整 |
| `nr_hugepages` | 0 | 大页池是空的,有充足上涨空间 |
| `threads-max` / `ulimit -u` | 94599 / 47299 | `kstack` 线程数上限约 4.7 万 |
| `/proc/pagetypeinfo` | **权限不够** | 必须 root 才能读,得让用户跑 |
| Normal zone order-9/10 | 41 / 674 | |
| DMA32 zone order-9/10 | 3 / **743** | ★ 见下 |

**为什么 `compact_stall` 一直是 0**:两个 zone 加起来有 **1417 个 order-10 块 ≈ 5.5 GB**
的完整连续内存。任何高阶分配请求都能从空闲链表上**直接摘走**,压根不用规整。
特别注意 **DMA32 那 743 块**:hugetlb 的 GFP 是 `GFP_HIGHUSER_MOVABLE`,
DMA32 在允许的 zonelist 里,所以那里的 ~1486 个潜在大页也得先耗掉。

### 2.4 三个文件的设计要点

| 文件 | 档位 | 核心手法 | 为什么这么写 |
|---|---|---|---|
| `holes.c` | 1 | mmap 一大片 → 逐页触碰 → **随机序** `MADV_DONTNEED` 一半 | 顺序释放会被 `__free_one_page()` 一路合并回大块,**白干**;随机释放让 buddy 大概率还被占着,合并第一步就断 |
| `kstack.c` | 2 | 创建海量线程并**永久阻塞**在 `sem_wait` | 要的是它占的 16KB 内核栈 + task_struct,不是 CPU 时间。**绝不能忙等**,4 核会跑满 |
| `hugetlb.sh` | 3 | 分批写 `/proc/sys/vm/nr_hugepages` | 唯一**同步**、必然进慢路径的高阶分配路径 |

**`holes.c` 用 `MADV_DONTNEED` 而不是 `munmap`——这是个关键设计决策**:

- `munmap` 挖一个洞就把 VMA 切开一次,洞多了会撞
  `/proc/sys/vm/max_map_count`(默认 65530)直接 ENOMEM
- `MADV_DONTNEED` 只做 `zap_page_range()`:解页表映射、把物理页还给伙伴系统,
  **VMA 一个都不动**,全程只有 1 个 VMA,想挖多少洞挖多少洞
- 代价:再次访问会读到全 0(重新缺页给零页)。对压力工具无所谓

**随机种子固定为 20260809**:性能实验最怕"上次能复现这次不行",
同样的参数必须挖出同样位置的洞。**可复现性是一切性能工作的前提**。

### 2.5 冒烟测试结果(小剂量,不需要 root)

**`./holes 512 64 50`** —— 512 MB 就打出了明显效果(Normal zone):

| order | 施压前 | 施压后 | 变化 |
|---|---|---|---|
| 0 (4KB) | 6758 | 7396 | ↑ 碎屑堆积 |
| 7 (512KB) | 65 | 11 | ↓ 83% |
| 8 (1MB) | 45 | **3** | ↓ 93% |
| 9 (**2MB**) | 12 | **3** | ↓ 75% |

高阶块被砸碎、碎屑堆到低阶——**外部碎片的教科书形态**。

**`./kstack 3000 64`** —— 直接实证了 VMAP_STACK:

| 指标 | 前 | 后 | 增量 | 含义 |
|---|---|---|---|---|
| `KernelStack` | 12960 kB | 61056 kB | **+48096** | 3000 × **16.03 KB**,每线程 16KB,分毫不差 |
| `VmallocUsed` | 64004 kB | 112020 kB | **+48016** | ★ 与上一行**几乎完全相等** |
| `SUnreclaim` | 107508 kB | 127468 kB | +19960 | 3000 × 6.65 KB(task_struct 等),**不可回收** |
| `SReclaimable` | 167568 kB | 167568 kB | **0** | 一点没动 |

- 第 2 行是**内核栈走 vmalloc 的直接证据**——增量原封不动体现在 vmalloc 用量上
- 第 3、4 行证明造出来的污染**全部落在不可回收侧**,正是我们要的 UNMOVABLE

### 2.6 编译期踩的一个坑

`PTHREAD_STACK_MIN` 编译报 undeclared。原因:**glibc 2.34 起它从编译期常量
变成了运行时值**(`sysconf(_SC_THREAD_STACK_MIN)`),`<limits.h>` 里不再无条件定义。
改成用 `sysconf` 查、查不到退回 16KB 常量。

另:`pthread_attr_setguardsize(&attr, 0)` 是必须的——每个线程用户栈是一个独立 VMA,
几万线程会撞 `max_map_count`,去掉 guard page 能省掉一半 VMA。

---

## 步骤 3:第一次正式压力实验(手工时序版)—— ★ 硬门槛通过

### 3.1 结果:`compact_stall` 从 0 → 938

用户按"终端1 kstack → 终端2 holes → 终端3 hugetlb"的顺序手工执行。
中途 `holes`/`kstack` 因终端掉线被 SIGHUP 杀掉(见 3.3),但**慢路径已经被成功逼出来**:

| 指标 | 实验前 | 实验后 | 含义 |
|---|---|---|---|
| `compact_stall` | 0 | **938** | 938 次进程被**同步卡住**做规整 |
| `compact_success` | 0 | 445 | |
| `compact_fail` | 0 | **493** | **失败率 52.6%** —— UNMOVABLE 污染确实起作用了 |
| `compact_daemon_wake` | 0 | 447 | kcompactd 也被唤醒 ← **P0 过滤器必须排除的噪声源** |
| `compact_migrate_scanned` | 0 | 2716539 | 迁移页扫描器扫过的页数 |
| `compact_free_scanned` | 0 | 11259221 | 空闲页扫描器扫过的页数 |
| `pgscan_direct` / `pgsteal_direct` | 0 / 0 | 60267 / 33988 | direct reclaim 也被触发 → **P1 的数据源有了** |
| `allocstall_movable` | 0 | 523 | |
| PSI memory `some total` | 0 | 8313273 µs (8.31 s) | |
| PSI memory `full total` | 0 | 7459603 µs (7.46 s) | |

### 3.2 知识点:双扫描器的"指纹"

`compact_free_scanned / compact_migrate_scanned = 11259221 / 2716539 = **4.14**`

规整的两个扫描器是**相向而行**的:
- 迁移页扫描器从 zone 低地址往上走,找**可以搬走的页**
- 空闲页扫描器从 zone 高地址往下走,找**可以落脚的空位**
- 两者在中间碰头,这一轮结束

理想情况两边扫描量应该接近。**差 4 倍说明空闲侧要翻很远才能找到一个能落脚的空位**
—— 这正是"碎片严重"在内核内部留下的可量化指纹。这条可以直接写进报告。

### 3.3 两个操作层面的坑(都不是内核问题,但会毁掉实验)

**坑① 网卡闪断把压力源杀了。**
`dmesg` 里满屏 `e1000: ens33 NIC Link is Down / Up`,终端掉线 → SIGHUP →
`holes` 和 `kstack` 双双死亡。等实验做完去 `ps` 才发现"都不在了"。
**压力源死了但脚本还在跑,数据会静悄悄地失真。**

**坑② 人肉掐时序不可靠。** 用户原话:"执行时机拿捏不准"。
三个压力源要按顺序上、还要保证前一个真正铺开了再上下一个,
这个不该交给人。→ 直接催生了 `run.sh`。

### 3.4 一次被证伪的判断(如实记录)

看到 `hugetlb.sh` 每批耗时 10 秒却报告"计数器全没变",我第一反应是
"`show_delta` 里的 `join` 有 bug"。**用真实数据单测后证明 `join` 逻辑是对的。**

真实原因是:前 5 批(250~1250 个大页)**确实没有触发规整** ——
它们是从 **DMA32 那 743 个 order-10 块**里直接摘走的(hugetlb 的 GFP 是
`GFP_HIGHUSER_MOVABLE`,DMA32 在允许的 zonelist 里)。
那 10 秒是 `holes` 在同时抢内存造成的,不是规整。

> **教训**:观测工具项目里,看到"反直觉的数据"第一反应不该是"我的工具坏了",
> 也不该是"数据没问题"。**先做一个能分辨这两者的实验。**

---

## 步骤 4:写 `run.sh` —— 把时序从人脑挪进代码

### 4.1 设计要点

| 做法 | 解决的问题 |
|---|---|
| 上场顺序写死:kstack → holes → hugetlb | **kstack 必须先上**:趁内存宽裕,让内核栈散布到尽可能多的 pageblock;等内存被占满再创建线程,内核栈会挤在仅剩几个区域,污染面反而小 |
| 每步**轮询上一步日志里的完成标记**(`实际创建` / `随机挖洞完成`),不用 `sleep` 猜 | 时序可靠 |
| `trap cleanup EXIT INT TERM` | 无论正常结束还是被打断,都归零大页池、杀掉压力源,机器恢复原状 |
| 开跑前先清残留(`nr_hugepages=0` + `pkill`) | 上一轮的 2127 个大页还占着 4.2 GB |
| 结束时 `kill -0` 检查两个压力源**是否全程存活**,死了就大声报出来 | 坑① 再也不会静悄悄发生 |
| 逐批增量落成 `batches.csv` | 直接就是报告里的主表 |
| 四个时间点的完整快照(A 基线 / B kstack 后 / C holes 后 / D hugetlb 后) | 每一档压力各自的贡献可以拆开看 |

### 4.2 ★ 坑③:`sudo` + 后台 = 进程被 SIGTTIN 停住

第一次启动 `sudo nohup ./run.sh > log 2>&1 &`,**日志 0 字节、进程列表里啥也没有**。

`ps -eo pid,stat,args` 一看,状态是 **`T`(Stopped)**:

```
36189 T  sudo nohup ./run.sh
```

原因:`sudo` 要从终端读密码,但它是**后台作业**;后台作业读控制终端
会被内核发 `SIGTTIN` 停住。日志是 0 字节,看起来像"什么都没发生"。

**修法**:先在前台单独把密码输掉,凭据缓存 15 分钟,后台那次就不会再问。

```bash
sudo -v
sudo nohup ./run.sh > /tmp/fragstress_run.log 2>&1 &
```

这条已写进 `run.sh` 的用法注释和 `Makefile` 的提示里。

### 4.3 本轮参数(相对手工版调小)

`HOLES_MB` 4500 → **4000**,`HP_TARGET` 2500 → **2000**。
原因:手工版把内存榨得太干,**把压力源自己给榨死了**(OOM)。
压力源死了就等于没压,数据反而更差。

---

## 步骤 5:第一次自动化实验 —— **失败**,`compact_stall` 全程 0

如实记录:第一次 `run.sh` 跑完,10 批大页申请,**`compact_stall` 每批都是 0**。
硬门槛没过。下面是完整的归因过程。

### 5.1 现象

| 批次 | 目标 | 实得 | 耗时 s | compact_stall |
|---|---|---|---|---|
| 1~10 | 200→2000 | 全部拿满 | 5.38 / 5.34 / 3.28 / 0.36 / 0.23 / 0.39 / 0.75 / 0.21 / 0.17 / 0.97 | **全部 0** |

耗时越到后面越短(0.17~0.97 秒),典型的"从空闲链表直接摘走"。

### 5.2 ★ 根因:`holes` 天生够不着 DMA32

四个快照的 DMA32 行**逐字对比**:

```
A_基线      DMA32  115  79  79  66  61  48  43  45  37  39  705
B_kstack后  DMA32  115  79  79  66  61  48  43  45  37  39  705   ← 完全没变
C_holes后   DMA32  115  79  79  66  61  48  43  45  37  39  705   ← 还是完全没变
D_hugetlb后 DMA32  115  79  79  66  61  48  43  45  37   1   40   ← 大页全从这儿摘走
```

**内核给用户匿名页分配内存时优先从最高的 zone 拿(Normal),
只有 Normal 快见底才 fallback 到 DMA32。**
`holes` 只要了 4 GB,而快照 C 时 `MemFree` 还有 5.9 GB —— Normal 从没紧张过,
所以 DMA32 的 705 个 order-10 完整块(≈1410 个大页)一直闲着,
最后被 hugetlb 一口气全摘走了。

### 5.3 差了多少 —— 用数据说话

把快照 C 换算成"不用规整就能白拿的大页数"
(order-9 块 = 1 个 2MB 大页;order-10 块 = 4MB = 2 个大页):

| zone | order-9 | order-10 | 可白拿大页 |
|---|---|---|---|
| Normal | 1 | 316 | 633 |
| DMA32 | 39 | 705 | **1449** |
| DMA | 1 | 2 | 5 |
| | | **合计** | **≈ 2087** |

**目标定的是 2000。库存 2087。正好够,一次规整都不用做 —— 差 88 个就跨过门槛了。**

对照:上一次手工跑之所以成功(938 次 stall),是因为目标定的 2500,越过了库存线。

**所以这不是压力策略错了,是目标值定低了。**

### 5.4 修法:让脚本自己算库存,再把目标顶到库存之上

光加大 `holes` 治不了本 —— 它天然够不着 DMA32。
在 `run.sh` 第 3 步开头加了**库存自检**:

```bash
# order-9 块 = 1 个大页;order-10 块 = 2 个大页
# buddyinfo 第 14、15 列分别是 order-9、order-10 的块数
INVENTORY=$(awk '/^Node/ { n += $14 + 2*$15 } END { print n+0 }' /proc/buddyinfo)
NEED=$(( INVENTORY * 14 / 10 + 200 ))     # 至少比库存高 40%
CAP=$(( (MemAvailable_MB - 800) / 2 ))    # 但要留 800MB 余量,别 OOM
```

三个数取合适的那个,并把决策过程打印出来。**当前机器实测**:
库存 2237(DMA32 独占 1449),`NEED` = 3331,`CAP` = 4008。

同时加了**提前收工**条件:累计 `compact_stall ≥ 300` 且跨 ≥3 批就停,
既省时间,也避免继续榨到 OOM 把压力源自己搞死。

### 5.5 ★ 另一个真 bug:stdio 全缓冲让就绪标记永远等不到

`run.sh` 靠轮询 `kstack.log` 里的「实际创建」判断该步是否就绪。
实测**白等了 300 秒超时**,而 `kstack.log` 是 **0 字节**。

但同一时刻 `/proc/meminfo` 显示:

```
KernelStack: 13360 kB → 205648 kB   = +192288 kB = 12000 × 16.02 KB
```

**12000 个线程早就铺好了**,只是那几行字还躺在进程的 stdio 缓冲区里。

**原理**:glibc 的默认缓冲策略取决于 stdout 连的是什么 ——
连终端 → **行缓冲**(按回车就看见);连文件/管道 → **全缓冲**(攒够 4KB 才写)。
本程序总共才输出几百字节,被重定向到日志后,**进程退出前一个字节都不落盘**。

> 这是 C 语言里最经典的一类"代码没错、行为不对":
> **在终端跑得好好的程序,一重定向到日志就什么都不输出。**
> 不是程序卡住了,是字还在缓冲区里。

**修法**:`holes.c` / `kstack.c` 开头各加一行

```c
setvbuf(stdout, NULL, _IOLBF, 0);   /* 强制行缓冲 */
```

命令行等价物是 `stdbuf -oL`。修完实测:标记在进程仍存活时就落盘了。

**注意**:运行中的 bash 脚本**不能改**(bash 是逐块读取脚本文件的,改了会错乱),
所以 `run.sh` 的修改必须等本轮跑完再动;`.c` 文件可以随时改,但
**重编译会报 `Text file busy`**(二进制正被执行中),也要等进程结束。

### 5.6 这一轮里仍然成立的好数据

失败的实验也有产出:

| 指标 | A 基线 | B kstack后 | C holes后 | D hugetlb后 |
|---|---|---|---|---|
| Normal order-0 | 34777 | 3119 | 5692 | 2330 |
| Normal order-7 | 512 | 510 | **16** | 16 |
| Normal order-8 | 249 | 249 | **3** | 3 |
| Normal order-9 | 242 | 240 | **1** | 0 |
| Normal order-10 | 664 | 615 | **316** | 0 |
| MemFree | 8901788 kB | 8479096 kB | 6010308 kB | 1898428 kB |
| Normal 的 Unmovable 块数 | 287 | **393** | 399 | — |

- **B 列证明 `kstack` 的污染是真的**:12000 个线程让 Normal zone 的
  **Unmovable pageblock 从 287 涨到 393(+106)** ——
  106 个原本可以整块规整出 order-9 的 pageblock 被永久钉死了
- **C 列证明 `holes` 的碎片是真的**:order-7 以上几乎被清空
- 两者都生效了,**唯独没人去要超过库存的大页**

---

## 步骤 6:第二、三次自动化实验 —— ★ 硬门槛通过,并发现"规整成功率悬崖"

### 6.1 第三轮(2026-08-10 23:15):压到极限,成功率 0%

库存自检生效了(库存 1652 → 目标自动抬到 2512),**真的越过了库存线**。

**但发现一个关键陷阱:这台机器没有重启过**,`A_基线` 里 `compact_stall`
已经是 **938**(第一次手工跑留下的)。脚本打印的"980"是累计值,不是本轮增量。
→ **教训:凡是用 `/proc/vmstat` 做实验,必须用"结束绝对值 − 开始绝对值",
不能直接看绝对值,也不能假设机器是干净的。**

本轮真实增量(从 A/D 两个快照的绝对值相减):

| 计数器 | A 基线 | D 结束 | 增量 |
|---|---|---|---|
| `compact_stall` | 938 | 980 | **+42** |
| `compact_success` | 445 | 445 | **0** |
| `compact_fail` | 493 | 535 | **+42** |
| `compact_isolated` | 1303618 | 1483303 | +179685 |
| `pgscan_direct` / `pgsteal_direct` | 60267 / 33988 | 68998 / 40349 | +8731 / +6361 |
| PSI some / full | — | — | +177.8 ms / +155.6 ms |

**42 次直接规整,42 次全败,成功率 0%。**
内核隔离了 179685 个页(约 700 MB)准备搬,一个 order-9 都没凑出来。

### 6.2 ★ 又一个"工具在骗人"的事故

`batches.csv` 里出现**物理上不可能的值**:

- 第 2 批 `compact_daemon_wake` 增量 = **−447**(单调递增计数器出负数)
- 第 3 批 `compact_migrate_scanned` 增量 = 3142214,**恰好等于该计数器的绝对值**
- 第 3 批 `compact_stall` 增量写着 939,而本轮真实增量只有 42

同一行里有的计数器对、有的错,**不是整体性失败,是零散错**。

提出的假设:bash 的"按块读 + lseek 回退"与 `/proc/vmstat` 这个 seq_file 冲突。
**做实验证伪了**:造 `pgfault` 每秒 2.5 万的背景负载,
`while read < /proc/vmstat` 连读 200 次,**异常 0 次**;
换成管道 `< <(cat /proc/vmstat)` 也是 0 次。

**机制至今未查清,没有编一个解释。** 改成"不依赖机制也能守住正确性"的四道防线:

| 防线 | 做法 |
|---|---|
| 采样完整性 | 用 awk 一次筛出 10 个需要的计数器再喂给 read;校验一个不少,缺了就报错并标记该批不可信 |
| 符号校验 | 单调计数器**不许出现负增量** |
| 恒等式校验 | **`stall` 必须等于 `success + fail`**(一次规整要么成功要么失败) |
| 双路对账 | 总量**另外从递增前后绝对值算一遍**,与逐批累加对账,不一致以前者为准 |

顺带修掉两处**工具骗人**的地方:

1. 扫描比的解读原来写死"远大于 1 说明…",实测 0.05 也照样打印那句 → 改成按区间三种解读
2. 退出诊断打印的 `cur` 是上一批的旧值(`cur=$got` 写在 break 之后)→ 提到 break 之前

### 6.3 第四轮(2026-08-11 00:10):干净数据,成功率 92%

零可疑行、零缺键、零报错。**两套独立算法互相印证**:

| | 权威总量(绝对值相减) | 逐批累加 | 一致性 |
|---|---|---|---|
| `compact_stall` | 402 | 402 | 完全一致 |
| 规整成功率 | 370/402 = 92.0% | 92.0% | 完全一致 |
| `pgscan_direct` | 4409 | 4409 | 完全一致 |
| `compact_migrate_scanned` | 314873 | 314539 | 差 0.1%(批次间隙的活动) |

逐批表:

| 批 | 目标 | 实得 | 耗时 s | stall | success | fail | kcompactd | migr_scan | free_scan | psi_some ms |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 1467 | 1467 | 10.06 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| 2 | 1667 | 1667 | 3.27 | 64 | 57 | 7 | 13 | 72357 | 74643 | 863 |
| 3 | 1867 | 1867 | 5.81 | 159 | 142 | 17 | 25 | 120973 | 1643387 | 2389 |
| 4 | 2067 | 2067 | 3.34 | 179 | 171 | 8 | 23 | 121209 | 600841 | 1429 |

第 1 批全 0,正是库存自检预测的"白拿区";第 2 批开始越线,规整立刻出现。

### 6.4 ★★ 核心发现:规整成功率有个悬崖

两轮的差别**只在于压到多深**:

| | 第三轮 | 第四轮 |
|---|---|---|
| 停止原因 | 内存榨干(**分配开始失败**) | 拿到 402 次 stall 提前收工(**每批都还拿满**) |
| 规整成功率 | **0.0%** | **92.0%** |
| `free/migrate` 扫描比 | **0.81** | **7.36** |

**只要分配还能被满足,规整基本都成功;一旦分配开始失败,规整就完全失效。
中间没有平滑过渡,是个悬崖。**

扫描比正好翻转,这是同一件事的另一面:

| 扫描比 | 含义 |
|---|---|
| **> 2** | 空闲页扫描器要翻很远才找到落脚点 —— "**空位难找**" |
| **≈ 1** | 两个扫描器扫描量接近,大致对称相遇 |
| **< 0.5** | 迁移页扫描器要翻很远才找到搬得动的页 —— "**搬得动的页难找**",UNMOVABLE 占比高 |

**后者才是 UNMOVABLE 污染的指纹。** 第三轮压到极限时,内存里剩下的几乎全是
内核栈、slab 和已分配的大页 —— 没有可迁移的页,规整自然全败。

> 同一套工具在两种碎片形态下给出**相反**的比值 —— 这比"一次跑通"更有说服力,
> 说明工具真的在测量物理现象,不是在输出常量。

### 6.5 复现性

两轮独立实验测得的"白拿库存":**1652 vs 1630,相差 1.3%**。
同样的命令、同样的默认参数,把机器压到同样的碎片程度。
**可复现性是一切性能工作的前提** —— 这句话现在有数据支撑了。

### 6.6 一个留给 P0 回答的问题

第一批(纯白拿、零规整零回收)的耗时:第三轮 **44.78 秒**,第四轮 **10.06 秒**,
**同样规模差 4 倍**。说明这段时间不是花在慢路径上。

→ **光看耗时不能判断有没有进慢路径,必须看计数器。**
这正是要做 eBPF 精确埋点的理由:`/proc/vmstat` 只给总数,
给不出"这一次分配等了多久、等在哪个环节"。

另一个待解释的现象:每次 stall 的平均 PSI 时间,
第三轮 177.8/42 = **4.2 ms**,第四轮 4691/402 = **11.7 ms** ——
**失败的规整反而更便宜**。合理猜测是失败路径提前退出(deferred/skipped),
但这需要 P0 的 per-attempt 延迟直方图按 `status` 分维度才能证实。
**现在不写进结论。**

---

## 阶段 P-1 交付物清单

| 文件 | 行数 | 说明 |
|---|---|---|
| `源码/src/tools/fragstress/holes.c` | ~200 | 档位 1,外部碎片制造机 |
| `源码/src/tools/fragstress/kstack.c` | ~190 | 档位 2,UNMOVABLE 污染 |
| `源码/src/tools/fragstress/hugetlb.sh` | ~130 | 档位 3,高阶分配需求 |
| `源码/src/tools/fragstress/run.sh` | ~330 | 总编排 + 库存自检 + 四道自证防线 |
| `源码/src/tools/fragstress/Makefile` | ~45 | 编译 + `make check` |
| `源码/src/tools/fragstress/README.md` | ~300 | ★ 复现步骤、原理、所有踩过的坑 |

**硬门槛状态:通过**(402 次 direct compaction,跨 3 批持续增长)。

---

---

## ★ 阶段 P0 的记录去哪了（2026-08-15 补索引）

**P0 阶段（`compactinfo.c` + 真实验证）当时没有写进本文件**，散在别处。
为免下次找不到，这里留索引：

| 内容 | 在哪 |
|---|---|
| P0 正式交付报告（7 节，给评审员） | `报告_P0.md` |
| P0 真实验证的完整原始数据 | `handoff/com_memory.md` **§9.9** |
| P0 探针的设计取舍（为什么要 kretprobe、三重过滤） | `handoff/com_memory.md` **§9.6** |
| 慢路径内核源码逻辑（带 v5.15.178 行号） | `handoff/com_memory.md` **§9.5** |
| 18 道自测题的全文答案 | `handoff/com_memory.md` **§9.8** |

**从 P1 开始，实验过程重新记回本文件**（步骤 7 起），
`com_memory.md` 只放"跨会话必须知道的结论"，两边分工不再混。

---

## 步骤 7:P1 开工 —— 前置硬门槛（2026-08-15）

### 7.1 为什么 P1 不能直接写探针

P0 那轮的数据里有一个刺眼的组合：

```
compact_stall  = 377     ← 直接规整发生了 377 次
pgscan_direct  = 0       ← 直接回收一次都没发生
pgsteal_direct = 0
```

**现有压力形态（hugetlb 抢 order-9）根本触发不了直接回收。**

根因在慢路径的结构（`mm/page_alloc.c`,v5.15.178）：

```
:5013   if (can_direct_reclaim &&
            (costly_order || (order > 0 && migratetype != MOVABLE)))
:5017       __alloc_pages_direct_compact(...)     ← 进 retry 循环"之前"的提前规整
            ...拿到页就 goto got_pg

:5058   retry:                                     ← 真正的 retry 循环
:5092       __alloc_pages_direct_reclaim(...)      ← 直接回收在这里,从没被调用
```

order-9 是 costly order，走 `:5013` 那一发提前规整就拿到页返回了，
**根本没进 retry 循环**。

> **所以 P1 的第 0 步不是写代码，是换压力形态。**
> 性质完全等同于 P-1 之于 P0：**空 map 和坏探针长得一模一样**，
> 不先造出事件，连"探针写对没有"都无法验证。

### 7.2 源码勘察结果（写代码前先把结构定死）

#### ① 埋点位置：比 P0 干净得多

`mm/vmscan.c:3540 try_to_free_pages()`：

```c
	if (throttle_direct_reclaim(sc.gfp_mask, zonelist, nodemask))
		return 1;                                        // :3570 ★ 提前返回

	set_task_reclaim_state(current, &sc.reclaim_state);
	trace_mm_vmscan_direct_reclaim_begin(order, sc.gfp_mask);   // :3573

	nr_reclaimed = do_try_to_free_pages(zonelist, &sc);          // :3575

	trace_mm_vmscan_direct_reclaim_end(nr_reclaimed);            // :3577
```

**和 P0 的 compaction 比，结构上有三处关键差异**：

| | P0 (compaction) | P1 (reclaim) |
|---|---|---|
| 内层埋点是否被多来源共用 | **是**（direct / kcompactd / 管理员三条路共用 `compact_zone`）→ 必须做三重过滤 | **否**。`mm_vmscan_direct_reclaim_begin/end` 只有 direct reclaim 会打；kswapd 和 memcg 各有自己的 tracepoint | 
| begin 里有没有 order | **没有**（只有 zone/nid/idx）→ 必须靠外层探针传 | **有**（`TP_PROTO(int order, gfp_t gfp_flags)`）|
| 结局怎么拿 | 出口没有 tracepoint → 必须加 kretprobe 取返回值 | **end 直接给 `nr_reclaimed`** |
| begin/end 配对关系 | 1:N（外层一次，内层遍历多个 zone） | **1:1**（同一个函数体内，中间只夹一个调用） |

> **结论：计划书里写的"P1 骨架复用 P0"只对了一半。**
> 三重来源过滤和"外层传 order"这两块**不需要**了 ——
> reclaim 的 tracepoint 位置比 compaction 好得多。

**但有一处必须复用 P0 的教训**：`:3570` 那个 `throttle_direct_reclaim` 的提前返回，
**结构上和 `page_alloc.c:4409` 的 `COMPACT_SKIPPED` 早退一模一样** ——
走这条路的话 begin/end 一个都不打，事件整个消失。
所以仍然值得在 `try_to_free_pages` 上挂一个 kretprobe。

> ### ⚠️ 2026-08-17 更正：上面这段的**结论部分是错的**
>
> 当时紧接着写的是"用 **kretprobe 次数 − end 次数 = 被 throttle 掉的次数**
> 当自证机制"。**这句话不成立**，写代码时读完 `throttle_direct_reclaim()`
> 的函数体才发现。
>
> 错因：只读了调用点 `if (throttle_direct_reclaim(...)) return 1;`，
> 就以为"被限流 → 返回 true → 早退"。但真正决定返回值的逻辑在**被调函数的末尾**：
>
> ```c
> count_vm_event(PGSCAN_DIRECT_THROTTLE);              // :3515 ★ 睡之前就计数
>
> if (!(gfp_mask & __GFP_FS))
>         wait_event_interruptible_timeout(pgdat->pfmemalloc_wait,
>                 allow_direct_reclaim(pgdat), HZ);    // :3527 最多睡 1 秒
> else
>         wait_event_killable(zone->zone_pgdat->pfmemalloc_wait,
>                 allow_direct_reclaim(pgdat));        // :3530 ★ 睡到 kswapd 叫醒，无上限
>
> if (fatal_signal_pending(current))
>         return true;                                 // :3533-3534 ★ 只有这里返回 true
> out:
>         return false;
> ```
>
> 也就是说：**正常被限流的进程是睡一觉、醒来返回 false、继续往下走到 begin 埋点。**
> `return true` 只发生在睡眠期间收到致命信号（进程正在被杀）时。所以：
>
> ```
> ✗ 错：kretprobe 次数 = begin 次数 + 被限流次数
> ✓ 对：kretprobe 次数 = begin 次数 + 被限流且期间收到致命信号的次数（通常 0）
> ✓ 对：被限流次数 = /proc/vmstat 的 pgscan_direct_throttle（:3515 在睡之前就计了）
> ```
>
> **但外层 kretprobe 不该撤掉，反而更重要了** —— 只是它量的东西变了：
> 限流的睡眠发生在 begin **之前**，所以
>
> ```
> begin → end        = 纯扫描耗时（不含限流睡眠）
> 函数入口 → 返回     = 进程实际被卡住的总时长（含限流睡眠）★ 用户感知到的就是这个
> 两者之差           = 排队/限流睡掉的时间
> ```
>
> 而且 PSI 窗口（`page_alloc.c:4653/4662`）包的是**整个 `try_to_free_pages` 调用**，
> 所以能和 PSI 对账的是**外层**那个数，不是内层。
> 内层单独测出来的意义在于把"卡顿花在扫描上"和"卡顿花在排队上"分开 ——
> 这两件事的优化方向相反：扫描慢要改回收策略，排队久说明 kswapd 追不上、该调水位。
>
> **教训（和 P0 那次同类但更深一层）**：P0 学到的是"早退分支可能在埋点之前"；
> 这次学到的是"**早退条件可能不在调用点，而在被调函数末尾** ——
> `if (f()) return;` 这种写法，读调用点读不出 `f()` 到底什么时候返回真"。

#### ② tracepoint 字段（从 `/usr/src/.../include/trace/events/vmscan.h` 读的）

```c
DECLARE_EVENT_CLASS(mm_vmscan_direct_reclaim_begin_template,
	TP_PROTO(int order, gfp_t gfp_flags),
	TP_STRUCT__entry(
		__field(	int,	order		)
		__field(	gfp_t,	gfp_flags	)
	), ...);

DECLARE_EVENT_CLASS(mm_vmscan_direct_reclaim_end_template,
	TP_PROTO(unsigned long nr_reclaimed),
	TP_STRUCT__entry(
		__field(	unsigned long,	nr_reclaimed	)
	), ...);
```

**★ 按 P0 的教训，这个不算数 —— 必须以本机 tracefs 的 `format` 文件为准**
（P0 就是因为信了计划书才漏掉 `status` 的 4 个取值，还踩了 offset padding）。
待跑的命令见 7.4 第 ① 步。

#### ③ 延迟窗口能和 PSI 对账

`__perform_reclaim()`（`page_alloc.c:4643`）：

```c
	psi_memstall_enter(&pflags);
	fs_reclaim_acquire(gfp_mask);
	noreclaim_flag = memalloc_noreclaim_save();
	progress = try_to_free_pages(...);          // ← 我们量的就是这一段
	memalloc_noreclaim_restore(noreclaim_flag);
	fs_reclaim_release(gfp_mask);
	psi_memstall_leave(&pflags);
```

**PSI 的计时窗口正好套在 `try_to_free_pages` 外面一层。**
所以 eBPF 测出来的"总回收延迟"应当≈`/proc/pressure/memory` 的 total 增量 ——
这是 P1 现成的第三方交叉验证线（P0 用过同样的手法）。

另外 `__perform_reclaim` 是 `static`、且被 `static inline` 的
`__alloc_pages_direct_reclaim` 包着，**挂不了 kprobe**；
`try_to_free_pages` 是全局符号，可以挂。

### 7.3 ★ 一个差点掉进去的坑：不能用 cgroup 限内存来造压力

直觉上"把内存 hog 关进一个 memcg、`memory.max` 限住"更安全。**但这条路是错的。**

`mm/vmscan.c:2212`：

```c
	item = current_is_kswapd() ? PGSCAN_KSWAPD : PGSCAN_DIRECT;
	if (!cgroup_reclaim(sc))
		__count_vm_events(item, nr_scanned);        // ← cgroup 触发的回收不计这里
	__count_memcg_events(lruvec_memcg(lruvec), item, nr_scanned);
```

**cgroup 内触发的回收不增加全局 `pgscan_direct` / `pgsteal_direct`。**

后果：eBPF 侧能抓到事件，`/proc/vmstat` 侧却是 0 ——
**交叉验证这条线整条失效**，而它正是本项目全部可信度的来源。

> 记下来的通用教训：**造压力的手段会改变验证手段是否成立。**
> 选压力形态之前，先确认它不会把对账基准打掉。

所以必须用**全局压力**，代价是有 OOM 风险，用三道保险顶（见 7.4）。

### 7.4 写了 `memhog.c`（fragstress 档位 5）

`源码/src/tools/fragstress/memhog.c`，~370 行，`make memhog` 编译通过（`-Wall -Wextra` 零告警）。

手法：**多线程 mmap 匿名内存 + memset 写脏**，把分配速率顶到 kswapd 追不上。

内核侧的预期链条：

```
free 跌破 low  → 唤醒 kswapd（后台回收，计 pgscan_kswapd）
free 跌破 min  → 申请者自己被拉去回收
                 __alloc_pages_direct_reclaim() :5092
                 → __perform_reclaim()          :4643
                 → try_to_free_pages()          vmscan.c:3540
                 → 计 pgscan_direct / pgsteal_direct
```

三道安全保险：

| # | 保险 | 作用 |
|---|---|---|
| 1 | 启动即把自己 `oom_score_adj` 设成 1000 | 真 OOM 时**第一个被杀的是 memhog**，不是桌面/ssh（提高自己的分数不需要 root）|
| 2 | `--goal`：`pgscan_direct` 一达标立刻停手并释放 | 正常情况下远在内存耗尽前退出 |
| 3 | `--floor`：`MemAvailable` 低于阈值（默认 400 MB）无条件停手 | goal 没达到也不再往下压 |

另有一条自证：`pgscan_direct` 和 `allocstall_*` **必须同为 0 或同为非 0**，
不一致就打警告（P-1 谎报事故的同类防线）。

### 7.5 ★ 实测：这台机器的分配速率上不去（关键约束）

先做了两次小规模冒烟测试（各 1~2.5 GB，跑完立即释放，不构成压力）：

| 线程数 | 吃下 | 耗时 | 速率 | `pgscan_direct` |
|---|---|---|---|---|
| 2 | 1.12 GB | 17.0 s | **66 MB/s** | 0 |
| 8 | 2.50 GB | 16.7 s | **150 MB/s** | 0 |

4 线程翻到 8 线程只快了 2.3 倍 —— **4 个 vCPU 已经打满，150 MB/s 就是天花板。**

单独测了带宽，定位到瓶颈在**缺页异常**而不是内存带宽：

```
首次触碰 512MB: 6.474 s -> 79 MB/s      ← 每次缺页约 50 µs
二次重写 512MB: 0.294 s -> 1739 MB/s    ← 差 22 倍
```

50 µs/缺页比正常慢一个数量级。环境事实：`systemd-detect-virt = vmware`，
THP 是 `[madvise]`（所以拿的全是 4KB 页，512 MB 要 131072 次缺页）。
**合理怀疑是 VMware 宿主侧的按需供页/气球，但没有证据，不写成结论。**

**这个约束直接决定了策略**：150 MB/s 大概率**跑不赢** kswapd 回收干净页缓存的速度
（那不需要 I/O，轻松上 GB/s）。硬压只有在页缓存耗尽、kswapd 被迫换页到磁盘之后
才有机会赢 —— 那时离 OOM 也不远了，窗口很窄。

### 7.6 由此定的两套方案

**方案 B（推荐，主选）：人为抬高 min 水位**

不去比谁快，而是**把终点线往前挪**。`vm.min_free_kbytes` 现在是 67584 kB（66 MB）：

```
Node 0, zone   Normal  free=474837  min=12725  low=15906  high=19087
Node 0, zone    DMA32  free=764090  min=4148   low=5185   high=6222
```

把 `min_free_kbytes` 抬到 2 GB，min 水位就跟着涨约 30 倍，
**free 立刻贴到 min 附近** —— 此时只要再吃几百 MB，每一次分配都会跌破 min，
直接回收当场就来。

- 优点：**不需要真的把内存耗尽 → 基本没有 OOM 风险**；快、可复现、可精确调节
- 优点：走的是**完全相同的内核代码路径**（`:5092`），观测到的事件是真的
- 代价：属于人为制造的低内存假象，**报告里必须写清楚**，不能假装是自然负载
- 反转：一条 sysctl 就能恢复

**方案 A（备选）：硬压到换页**

`./memhog --gb 12 --threads 8 --goal 1 --floor 200`，靠吃穿页缓存 + 换页拖慢 kswapd。
更"自然"，但慢（≈60 s 起）、窗口窄、OOM 风险实打实。**方案 B 不行再上。**

### 7.7 ★ 硬门槛实测：方案 A 直接就过了（2026-08-15）

**方案 B（抬水位）没用上** —— 它需要 `sudo sysctl`，而 AI 这边跑不了特权命令；
方案 A（硬压）**完全不需要 root**，先跑了，结果一次通过。

证据全部存在 `~/p1_evidence/`（★ 不在 `/tmp`，P0 就是在这里丢过一次原始日志）。

基线（干净得可以当教科书）：

```
pgscan_direct 0   pgsteal_direct 0   pgscan_kswapd 0
allocstall_dma 0  allocstall_dma32 0  allocstall_normal 0  allocstall_movable 0
compact_stall 0   pswpout 0
/proc/pressure/memory   some total=0   full total=0
```

#### 三轮实验

| 轮 | 命令要点 | 吃下 | 耗时 | `pgscan_direct` | `allocstall` 合计 | `pswpout` |
|---|---|---|---|---|---|---|
| 1 | `--gb 11 --threads 8 --goal 1 --floor 250` | 4.62 GB | 31.1 s | **3624** | 7 | 0 |
| 2 | 同上 + `--hold 20`（重写式维持） | 6.44 GB | 36.9 s | 3691 | 6 | 149 |
| 3 | `--goal 999999999 --floor 400 --hold 40`（换块式维持） | 9.12 GB | 48.1 s | **49596** | **491** | 85713 |

**第 1 轮就过了硬门槛**，而且是在 `MemAvailable` 还有 4.3 GB 的时候触发的 ——
比预想的早得多。说明**直接回收看的是 zone 级水位，不是"总内存还剩多少"**。

三轮累计 PSI：

```
some total = 8391452 µs = 8.39 s
full total = 6499962 µs = 6.50 s
```

504 次 allocstall → **平均每次 stall 约 16.6 ms**。
这就是 P1 的第三方对账基准（和 P0 用 PSI 校验规整延迟是同一手法）。

#### ★ 教训 1：工具自己报了一次假警报（第 1 轮）

第 1 轮结束时 memhog 打出：

```
⚠ 自检不一致：pgscan_direct 增量 3624，但 allocstall 增量 0 ——
  两者应当同为 0 或同为非 0，数据存疑。
```

**是自检写错了，不是数据有问题。** 查 `/proc/vmstat` 发现
**`allocstall_movable = 7`**，而我的自检只加了 `normal + dma32`。

根因在 `mm/vmscan.c:3334`：

```c
	if (!cgroup_reclaim(sc))
		__count_zid_vm_events(ALLOCSTALL, sc->reclaim_idx, 1);
```

计数按 **`sc->reclaim_idx = gfp_zone(gfp_mask)`** 分桶，
而普通用户态匿名页用 `GFP_HIGHUSER_MOVABLE` → `gfp_zone()` 返回 **`ZONE_MOVABLE`**。
**哪怕本机 Movable zone 是空的（`free=0`），桶还是按 gfp 的"意图"分，
不是按最后实际从哪个 zone 拿到页分。**

> **通用教训：按 zone 分桶的计数器必须四个桶全加，少一个就会把正常读成异常。**
> 这次是"假警报"（虚惊），但同样的错误反过来就是"假通过"——
> P-1 那次谎报事故就是后者。

已修（`memhog.c` 自检段的注释里留了完整推导）。

#### ★ 教训 2：维持期第一版设计是错的 —— 观测窗口会是空的

第一版维持期的做法是"反复重写已有的页"。第 2 轮实测：

```
维持 14.1s  MemAvail=3202 MB  scan_direct 增量=3691
维持 20.1s  MemAvail=2670 MB  scan_direct 增量=3691    ← 20 秒一格没动
```

**重写的是已经在内存里的页，根本不产生分配，自然不触发回收。**
探针挂上去，整个观测窗口一个事件都收不到。

> **要观测分配路径，必须持续制造"分配"这个动作本身，
> 而不是制造"内存占用"这个状态。**

改成**边还边要**：munmap 一块，立刻 mmap+memset 一块新的。
总占用不变（不会越压越深），但每一轮都是全新的缺页，
而且发生在"free 已经贴着水位线"的前提下。

第 3 轮验证：维持期内 `pgscan_direct` 涨了 **30862**（占全轮的 62%），
`allocstall` 从个位数涨到 **491 次** —— 够做分阶延迟直方图了。

#### ★ 教训 3：刹车踩晚了

第 3 轮压到 `MemAvailable` 只剩 **84 MB**，比设定的 `--floor 400` 深得多。
原因是主压期停手后 **kswapd 还有一大批换页积压没做完，MemAvailable 会继续下滑**。
维持期的刹车线已从 `floor/2` 收紧到 `floor*3/4`。

没有触发 OOM，三道保险都没用上 —— 但这是运气好，不是设计好。

#### 由此确定的 P1 对账等式（写探针之前必须先定死）

```
✗ kretprobe(try_to_free_pages) 次数 = begin 次数 + throttle 早退次数    ← 2026-08-17 已证伪
Σ allocstall_*(四个桶)              = begin 次数 + do_try_to_free_pages 内部 retry 次数
```

> **第一条是错的**，更正见 7.2 ① 那个 ⚠️ 块。正确的一组是：
>
> ```
> kretprobe 次数 = begin 次数 + 被限流且期间收到致命信号的次数（通常 0）
> 被限流次数     = /proc/vmstat 的 pgscan_direct_throttle          (vmscan.c:3515)
> 外层耗时 − 内层耗时 = 被限流睡掉的时间                            (:3527 / :3530)
> Σ 外层耗时     ≈ /proc/pressure/memory 的 total 增量（**不是恒等式**，见 7.9 ③）
> ```

第二条的修正项来自 `do_try_to_free_pages` 里的 `retry:` 标签（:3330），
**ALLOCSTALL 的计数点 :3334 在 retry 标签之后** —— 同一次调用可能计多次。
两个 `goto retry` 都在"这一轮什么都没回收到"的兜底路径上（:3396 / :3405）。
所以只能写成不等式 `Σallocstall_* ≥ begin 次数`，差额就是 retry 次数。

> 和 P0 的 `compact_stall == outer_exit − SKIPPED` 是同一个形状：
> **对账等式基本都不是恒等式，一定要先找出修正项。**
> 本轮实测 `pgscan_direct_throttle = 0`，所以第一条那个错版本这次**会退化成相等** ——
> 如果没去读 `throttle_direct_reclaim()` 的函数体，
> 这个错等式会在实测中"通过"，然后被当成已验证的结论写进报告。
> **这是本项目第二次遇到"错误的自检恰好通过"**（第一次是 memhog 的 allocstall 分桶）。

### 7.9 探针与展示层落地（2026-08-17）

#### ① `format` 实测结果（用户跑的 sudo，不是抄头文件）

```
mm_vmscan_direct_reclaim_begin   ID: 529
  field:int   order;         offset:8;   size:4;  signed:1
  field:gfp_t gfp_flags;     offset:12;  size:4;  signed:0

mm_vmscan_direct_reclaim_end     ID: 526
  field:unsigned long nr_reclaimed;  offset:8;  size:8;  signed:0
```

头文件这次没骗人，字段名和类型都对得上。但有一个坑值得单独记：
**`begin` 的两个字段都是 4 字节，`end` 的 `nr_reclaimed` 是 8 字节 `unsigned long`。**
照 `begin` 的模式想当然写成 `u32`，在小端机上会读到低 32 位 ——
页数不大时**看起来完全正常**，只在溢出时才暴露。
这种错误不会自己现形，只能靠实测 `format` 来防。两边都从 offset 8 起，无 padding。

顺带发现：BCC 的 `TRACEPOINT_PROBE` 是**读 tracefs 的 `format` 文件现场生成 `args`
结构体**的。所以非 root 连编译都过不去（只有前向声明，报
`incomplete definition of type`）—— 这也反过来说明字段名必须和 `format` 完全一致。

#### ② ★ 发现 `ksrc-5.15.178/` 是不完整的源码树

```
ksrc-5.15.178/
└── mm/
    ├── compaction.c
    ├── internal.h
    ├── page_alloc.c
    └── vmscan.c          ← 总共只有 3 个 .c
```

**所以本记录里凡是写"全内核 grep 确认"的地方都要打折。**
2026-08-17 一度在 `reclaiminfo.c` 的注释里写了"全内核 `try_to_free_pages`
只有一个调用者，已 grep 确认"—— 那是**言过其实**，实际只 grep 了这 3 个文件。

本机真正能验证到的（换了个思路，从符号导出入手）：

| 事实 | 来源 | 说明 |
|---|---|---|
| 声明在 `include/linux/swap.h:379` | 内核头文件包 | 有声明 |
| `/proc/kallsyms` 里是 `T` | 本机 | 全局符号，kprobe 挂得上 |
| **不在 `Module.symvers` 里** | 内核头文件包 | ★ 没 `EXPORT_SYMBOL` → **任何可加载模块都调不到它**，调用者只可能在内建代码里 |
| 这 3 个 mm 文件里唯一调用点是 `page_alloc.c:4657` | 本机 grep | 在 `__perform_reclaim()` 内 |

剩下的缺口（`fs/`、`drivers/` 等内建代码有没有调它）本地验证不了。
**处理办法不是假设它成立，而是让它变成运行时可测的**：
`reclaiminfo.c` 里加了 `BPF_STACK_TRACE(caller_stacks)`，
每次进入 `try_to_free_pages` 记一条内核栈，用户态用 `/proc/kallsyms` 解析。
输出里只有一条栈 → 前提被实测证实；出现第二条 → 结论推翻，外层对账要重算。

> 这条比"读源码确认"强，因为它在**目标内核上直接测**，
> 不依赖手上这份源码是不是完整、是不是同一个版本。

#### ③ PSI 交叉校验的**真实强度**（别把它说过头）

原先记的是"eBPF 测出的总延迟 ≈ `/proc/pressure/memory` 增量"。
查了 `psi_memstall_enter` 的全部调用点，这话要缩水：

| 位置 | 属于谁 | 本工具覆盖？ |
|---|---|---|
| `page_alloc.c:4653` | **直接回收** | ✅ 我们 |
| `page_alloc.c:4395` | 直接规整 | ❌（那是 P0） |
| `vmscan.c:3898` | **kswapd**（`balance_pgdat`） | ❌ |
| `vmscan.c:4514` | node reclaim（`__node_reclaim`） | ❌（本机 `zone_reclaim_mode=0`，不贡献） |
| `compaction.c:2960` | kcompactd | ❌ |

两个方向的偏差**同时存在**：

1. **PSI 口径更宽** → PSI 偏大。memhog 那三轮 `pswpout=85713`，kswapd 极忙，
   `vmscan.c:3898` 那一处的贡献可能远超我们这一处。
   所以 7.7 里记的"8.39 s / 504 次 = 16.6 ms 每次"**不能当作直接回收的平均延迟** ——
   那个 8.39 s 里有大量 kswapd 的份。这个数只能当上界。
2. **PSI `some` 是墙钟** → 本工具偏大。N 个线程同时卡住，PSI 只算一份时间，
   本工具是逐次求和。8 线程压力下本工具的总和可以是 PSI 的好几倍。

> **结论：这条校验能发现的是数量级错误（比如差 1000 倍 = 单位搞错了），
> 不能证明数字精确。** 已按这个措辞写进 `extfrag.py` 的输出里，
> 避免以后自己看输出时又把它当成强校验。

#### ④ 写了什么

**`源码/src/bpf/reclaiminfo.c`（新建）**：2 个 tracepoint + 1 对 kprobe/kretprobe
+ 1 个调用栈诊断。**没有来源过滤器**（埋点本身就是 direct 专用），
**没有"内层向外层借 order"**（begin 自带 order）。

12 格 `stat_map`，其中 4 格是**预期恒为 0 的自检位**：

| 自检位 | 非 0 意味着 |
|---|---|
| `order_mismatch` | 外层 kprobe 从寄存器取的 order ≠ begin 报的 order → `PT_REGS_PARM2` 取参假设错了。**这是免费的自证**：同一个值走两条独立路径拿到，对不上就是取参错。错了不会崩，只会让直方图悄悄归错维度。 |
| `no_direct_reclaim` | gfp 里没 `__GFP_DIRECT_RECLAIM`，与"直接回收必然允许阻塞"矛盾 |
| `begin_no_outer` | 有 begin 没外层记录 → 若持续增长，说明有别的调用者 |
| （`ret_one` 不是 0/非 0 判断） | `:3570` 的早退是 `return 1`，而正常路径也可能真回收了 1 页。单看返回值分不出来，要靠**有没有配上 begin** 消歧 |

**`源码/src/extfrag.py`（改，不新开 py 文件）**：
- `read_vmstat` / `read_psi` / `vmstat_delta`：取不到的键返回 **-1 / None，不返回 0**
  （返回 0 会让"没这个计数器"和"计数器真是 0"变成同一个值，让自检**假通过**）
- vmstat 基线在 `BPF()` **之后**取：BCC 现场调 clang 要一两秒，
  基线放前面会凭空差出一截。残留偏差方向固定：eBPF 可能多算，不会少算
- `print_reclaim()`：`--mode reclaim` 的文本输出
- **补上了简历第 2/3 点欠的那段**：`print_compact()` 现在自己读 vmstat，
  自动验 `compact_stall == 外层退出 − SKIPPED` 这条恒等式，
  并算双扫描器扫描比（>2 找空位难 / <0.5 UNMOVABLE 指纹 / 中间正常）

#### ⑤ 写代码过程中发现的两个自己的 bug

| bug | 后果 | 为什么危险 |
|---|---|---|
| `_fmt_hist()` 把单位硬编码成 `μs` | "每次回收到的页数"直方图被标成 `8 ~ 15 μs`，**页数显示成时间** | 程序不崩，只让**读的人**得出错误结论；截图进报告后基本发现不了。已加 `unit` 参数 |
| `pgscan_direct` 增量为 0 时打印"每回收 1 页要扫 0.0 页" | 用一个荒谬的比值掩盖了"两个来源互相矛盾"这件事 | 同上：不报错，只是悄悄输出假数。已改成显式报矛盾 |

两个都属于同一类：**输出层的错误不会被程序自己发现**。

#### ⑥ 验证到哪一步了（诚实划线）

| 项 | 状态 |
|---|---|
| C 代码 clang 编译 | ✅ 零错误（用手工补的 `args` 结构体绕开 tracefs 权限，见 ① ） |
| BPF verifier 接受 | ✅ **2026-08-18 通过**（见下） |
| `print_reclaim` 全分支能跑 | ✅ 用假 BPF 后端跑通，两个显示 bug 就是这么抓到的 |
| `print_compact` 改动未回归 | ✅ 同上 |
| 运行时数字正确 | ❌ **未验证**，等正式观测轮 |

#### ⑦ verifier 关（2026-08-18，`--duration 5` 空载）

```
sudo python3 extfrag.py --mode reclaim --duration 5
```

四个探针（kprobe + kretprobe + 2 tracepoint）全部挂载成功，程序加载运行无错。
唯一输出是 BCC 那三个 `__HAVE_BUILTIN_BSWAP*__ macro redefined` 警告 ——
BCC 自己的命令行 `-D` 和内核头文件重复定义，P0 也有，无害。

**这一轮多确认了一件手工桩验不了的事**：这次 BCC 是**读目标机上真实的
`format` 文件**生成 `args` 结构体的（有 root 了）。编译通过 →
`args->order` / `args->gfp_flags` / `args->nr_reclaimed` 三个字段名
在这个内核上确实存在且拼写正确。之前用手工补的结构体只能验语法，
验不了"字段名和真实内核一致"。

事件数全 0 —— 5 秒空载、没有内存压力，这是**预期结果**，不是探针没工作。
（能区分"没事件"和"探针坏了"的依据：如果探针没挂上，BCC 会在 attach 阶段
就抛异常，不会正常打印汇总。）

verifier 通过意味着：所有 map 访问、指针解引用、`bpf_probe_read` 边界
都被内核检查过了。**但运行时的数值正确性一点都没验** ——
零事件的情况下，那些字段读取路径根本没被执行到具体数据。

### 7.10 第一次正式观测轮（2026-08-18）—— 数据到手，同时暴露 3 个 bug

命令（两个终端，A 先起）：

```
sudo python3 extfrag.py --mode reclaim --interval 30 --duration 240   # 终端 A
./memhog --gb 11 --threads 8 --goal 999999999 --floor 800 --hold 60   # 终端 B
```

**第 0 轮是空跑**：终端 B 忘了跑。判据不是"工具报 0"，而是
`/proc/pressure/memory` 的 `total=0` —— 那是**开机以来**的累计值，
配合 `pgsteal_direct 0` / `pswpout 0`，说明本次开机内核层面一次内存压力都没发生。
（这条判据值得记：用累计计数器区分"没压力"和"工具没测到"，比看瞬时值可靠。）

#### ① 主要结果

| 项 | 值 |
|---|---|
| 外层进入 / 退出 / begin / end | 1299 / 1299 / 1299 / 1299（**全等**） |
| 未配对率 | 0.00% |
| `order_mismatch` / `no_direct_reclaim` / `begin_no_outer` | 0 / 0 / 0 |
| 限流次数（`pgscan_direct_throttle`） | **0** |
| 外层总耗时 / 内层总耗时 | 11.683 s / 11.662 s，差 **0.2%** |
| 平均每次卡住 | 8.99 ms |
| 回收页数（本工具） | 75636（295.5 MB） |
| `pgsteal_direct` 增量 | 75241（差 **+395**） |
| `pgscan_direct` 增量 | 437707 → 每回收 1 页扫 **5.8** 页 |
| Σallocstall | 1299 = begin，`goto retry` 次数 **0**；`movable=1258 / normal=41` |
| 零回收次数 | **0**（一次白卡都没有） |
| PSI some / full | 3.445 s / 2.670 s，本工具/PSI = **3.39** |
| kswapd 扫描量 | `pgscan_kswapd` 2751277（是 direct 的 6.3 倍） |

**限流 0 次 + 外内层差 0.2%** → 这轮的卡顿**几乎全部花在扫描上，没有排队成分**。
这正是 7.9 ③ 里说的"两个相反的优化方向"，实测明确落在了"回收策略/swap"这一边，
而不是"水位线/kswapd 跟不上"那一边。**这个结论是本工具能给、`/proc/vmstat` 给不了的**
—— vmstat 只有次数和页数，没有时间分解。

#### ② ★ 直方图众数对上了内核常量

回收页数直方图：**940/1291（72.8%）落在 32~63 页**。
来历在 `mm/vmscan.c:3545` —— `try_to_free_pages` 的 `scan_control`
里 `.nr_to_reclaim = SWAP_CLUSTER_MAX` = **32**，`shrink_node` 攒够 32 页就返回。
所以绝大多数调用是"扫够 32 页立刻走人"。
这是一条**没有事先设计、事后才发现的对账**：直方图的形状被内核常量解释了。
已把这条判定写进 `print_reclaim`（众数落在 32~63 时自动提示）。

#### ③ `+395` 页的差额不是 bug，是两个计数器口径不同

本工具读的是 end 埋点的 `nr_reclaimed`，也就是 `sc->nr_reclaimed`。
而 `mm/vmscan.c:3082`（`shrink_node` 里）：

```c
sc->nr_reclaimed += reclaim_state->reclaimed_slab;
```

**slab shrinker 释放的页算进 `nr_reclaimed`，但不算进 `pgsteal_direct`**
（后者只在 `:2229` 的 `shrink_inactive_list` 里计 LRU 页）。
所以正差额 = 这段时间 shrinker 释放的 slab 页（395 页 ≈ 1.5 MB 的 dentry/inode 缓存）。
**差额的符号是固定的：只可能为正。负差额才是真异常**——这条已写进输出。

#### ④ ★★ bug D1：调用栈计数用赋值覆盖（严重）

`extfrag.py` 原来写的是 `callers[栈文本] = v.value` —— **赋值不是累加**。
多个 stackid 只要前 6 帧文本相同就互相覆盖。
现形方式：9 条栈的次数加起来只有 **60**，而外层进入是 **1299**。

如果不是顺手加了那句"次数之和应该等于进入次数"的心算，这个 bug 会**直接进报告**——
因为每条栈的次数看上去都很合理（34、9、4、3……），只有总和暴露它。
修法：`callers[k] = callers.get(k, 0) + n`，并在输出里加一条显式对账
`Σ栈样本 = 外层进入 − get_stackid 失败次数`（后者是新加的 `S_STACK_FAIL` 格，
原来 `sid < 0` 是静默跳过的，分不清"没取到栈"和"聚合写错了"）。

#### ⑤ ★★ bug D2：把"分配来源"当成了"调用者"

工具报「出现 9 条不同调用栈 —— 前提可能被推翻」。但把 9 条栈的**第 2 帧**并排看：

```
try_to_free_pages ← __alloc_pages ← alloc_pages     ← proc_pid_cmdline_read ...
try_to_free_pages ← __alloc_pages ← alloc_pages_vma ← __read_swap_cache_async ...
try_to_free_pages ← __alloc_pages ← alloc_pages     ← pte_alloc_one ...
（其余 6 条同样）
```

**9 条栈的第 2 帧全是 `__alloc_pages`**（`__perform_reclaim` 被内联掉了）。
分叉发生在第 3 帧往后 —— 那是**谁在申请内存**，不是谁在调 `try_to_free_pages`。

所以 7.9 ② 里那个"本机无法证实的前提"（唯一调用者）**被实测证实了**，
外层不需要来源过滤的结论站得住。错的是我的判定粒度：拿整条 6 帧栈去数"有几个调用者"。

修法：拆成两个维度 —— `direct_callers` 按第 2 帧聚合（回答是非问题），
`alloc_sites` 按前 6 帧聚合（回答归因问题）。

**顺带的收获**：`alloc_sites` 免费给出了 P2 归因的雏形 ——
在直接回收里卡住的不只有 memhog，还有读 `/proc/*/cmdline` 的进程、
swap 预读、缺页、写时复制、文件页预读。（具体占比要等 D1 修好后重测。）

#### ⑥ bug D3：直方图确实在丢样本，而我打印的诊断是**错的**

| 直方图 | 实际样本 | 应有 | 丢 |
|---|---|---|---|
| `outer_lat` | 1287 | 1299 | 12（0.92%） |
| `inner_lat` | 1291 | 1299 | 8（0.62%） |
| `recl_hist` | 1291 | 1299 | 8（0.62%） |

输出里打的是「最可能是直方图 map 满」。**这个解释已被证伪**：
三张表容量都是 1024，实际只用了 9 个 key。

真实原因**至今没定位**。候选两个，都要另做实验才能分辨：
预分配 hash map 的桶锁在同 CPU 重入时返回 `-EBUSY`；
`lookup_or_try_init` 的 `BPF_NOEXIST` 竞争。

定位不了，至少不让它继续静默：把 `hist.increment(k)` 换成自写的 `HIST_INC` 宏，
`lookup_or_try_init` 返回空时计进 `S_HIST_FAIL_*` 三格。
下一轮观测就能直接看出"丢的样本是不是全出在这一步"，而不是只能猜。
同时把"延迟分布不可信"这句改成按丢失比例分级 —— 0.9% 不影响分布形状，
原来那句话说过头了。

#### ⑦ 这三个 bug 的共同点

D1、D2、D3 全都**不会让程序崩，也不会被 verifier 拦下**，
全部只在"数字之间对不上"的时候才现形：
D1 靠总和、D2 靠人眼看栈的第 2 帧、D3 靠直方图样本数 vs 计数器。
——**自检等式的价值在这一轮被兑现了三次**。
反过来说：没有对账等式的那些输出（比如 `alloc_sites` 的占比），
现在依然没有任何东西能证明它是对的。

### 7.11 待办

- [x] **硬门槛：`pgscan_direct` 增量 > 0** —— 通过（491 次事件，方案 A）
- [x] 实测两个 tracepoint 的 `format` 文件（用户跑 sudo，见 7.9 ①）
- [x] `try_to_free_pages` 是全局符号 —— `/proc/kallsyms` 里是 `T`，kprobe 挂得上
      （`do_try_to_free_pages` 是 `t`，挂不了，正好也不需要）
- [x] 写 `源码/src/bpf/reclaiminfo.c`
- [x] `extfrag.py` 加 `--mode reclaim` 分支（不新开 py 文件）
- [x] `extfrag.py` 补双扫描器扫描比 + `compact_stall` 自动对账（简历第 2/3 点靠它兑现）
- [x] **需要 root**：让 verifier 过一遍 —— 2026-08-18 通过，见 7.9 ⑦
- [x] 正式观测轮：**先挂探针 → 再跑 memhog → 结束后拷证据出来**（顺序不可颠倒）
      2026-08-18 完成，见 7.10；`--gb 11 --threads 8 --floor 800 --hold 60`
- [x] 修 D1（栈计数覆盖）、D2（调用者判定粒度）、D3（自增失败计数化），见 7.10 ④⑤⑥
- [ ] **需要 root**：复测一轮，验三件事 ——
      ① `Σ栈样本 = 外层进入 − stack_fail`（D1 修好没）
      ② 直接调用者是否唯一 = `__alloc_pages`（D2 修好后应当只剩一条）
      ③ `hist_fail_*` 三格是否恰好等于丢失数（D3 定位）
- [ ] 写 `报告_P1.md`（等复测数据，用复测那一轮的数字，不用 08-18 这轮）

---

## 下一步（P-1 时期遗留，部分已完成）

- [x] 待用户拍板:`extfrag.py` 怎么加命令行入口(`mode` 参数 + `__main__` argparse)
- [x] 写 `源码/src/bpf/compactinfo.c`(P0)
- [x] 装内核源码核实两条存疑项（`ksrc-5.15.178`,**不进仓库**）
- [ ] 补齐 fragstress 档位 2/3/4 的剩余文件（`sockflood.c` / `dentry.sh` / `thpload.c`,优先级低于 P1）
- [x] `extfrag.py` 补 15 行：从 `/proc/vmstat` 读 `compact_migrate_scanned` /
      `compact_free_scanned` 算扫描比（2026-08-17 完成，实际写成了 vmstat 对账一整节；
      顺带发现一个缺陷：vmstat 的 scanned 是**全局**的、含 kcompactd 的份，
      而 eBPF 侧过滤掉了 kcompactd —— 两边人口不同，所以
      "每搬走 1 页扫 N 页"是混了两拨人口的数，只能当数量级看。
      工具会打印 `compact_daemon_wake` 增量让人判断污染有多重）
