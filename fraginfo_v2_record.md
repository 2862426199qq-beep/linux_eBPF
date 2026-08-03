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

## 下一步

- [ ] **等用户确认两件事**:① 阶段一步骤顺序;② `extfrag.py` 加 `__main__` 入口的做法
- [ ] 装内核源码,核实两条存疑项:
      `sudo apt install linux-source-5.15.0` 或 `apt-get source linux-image-unsigned-$(uname -r)`
- [ ] 开始写 `源码/src/tools/fragstress/`(P-1 压力注入器)
- [ ] 验收硬门槛:`compact_stall` 必须能顶上去并持续增长
