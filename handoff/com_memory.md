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
