# fraginfo v2 项目计划书(修订版)

> **一句话**:把项目从"内存碎片状态观测"升级为
> **"内核内存分配慢路径的代价量化与归因"**。
>
> v1 回答了"有多碎"和"谁造成的";
> v2 回答 **"碎片让谁付出了什么代价"** —— 注意是**谁**,不只是**什么**。
> 归因闭环是这一版和现成工具拉开差距的地方。

> 初稿存档:`fraginfo_v2_draft1.md`(改动清单见文末附二)。

---

## 一、立意:为什么这是个"够格"的升级

### 1.1 内核视角:一次内存分配的完整决策路径

这是 Linux 内存管理最核心的一条路径,教科书级别:

```
alloc_pages(order)
   │
   ├─► 【快路径】get_page_from_freelist()          ← v1 的 fraginfo.c 挂在这里
   │      从伙伴系统各 zone 的 free_area[order] 直接摘块
   │      ├─ 本 migratetype 有货 → 成功返回
   │      └─ 没货 → __rmqueue_fallback() 跨类型借块  ← v1 的 extfraginfo.c 挂在这里
   │                (制造外碎片,打 mm_page_alloc_extfrag 埋点)
   │
   └─► 快路径失败 → 【慢路径】__alloc_pages_slowpath()      ★ v2 的战场 ★
          │
          ├─ ① 唤醒 kswapd(异步,不阻塞调用者)
          ├─ ② 【仅 costly order 且首次】提前试一次 direct compaction
          ├─ ③ 进入 retry 循环:
          │     ├─ direct reclaim      ← 同步阻塞,可能触发 IO
          │     └─ direct compaction   ← 同步阻塞,CPU 密集
          │     (循环内 reclaim 在前、compact 在后;可多轮)
          └─ ④ 全部失败 → OOM Killer / 分配返回 NULL
```

> **②③ 的先后顺序容易记反**,而这是必被追问的细节:5.15 的
> `__alloc_pages_slowpath()` 里,retry 循环内是 `__alloc_pages_direct_reclaim()`
> 在前、`__alloc_pages_direct_compact()` 在后;只有 costly order(order > 3)
> 在**进入循环前**有一次提前的 compaction pass。动手前对着本机内核源码确认一遍。

**两条必须记住的边界条件**:
- **`order == 0` 的分配永远不会触发 compaction**。规整只为高阶连续块服务。
  这决定了你能观测到什么:普通应用的绝大多数分配根本不进这条路。
  **高阶需求主要来自 THP、hugepages、内核栈(order-2)、slab 大对象、驱动的 DMA 缓冲区。**
- **能不能规整取决于 `gfp_mask`**(`__GFP_DIRECT_RECLAIM`、`__GFP_NORETRY` 等)。
  原子上下文的分配失败就是失败,不会等。

**核心认知**:碎片本身不是问题,**碎片把分配从快路径逼进慢路径才是问题**。
慢路径里的 compaction 和 reclaim 都是**同步阻塞**的 —— 申请内存的那个进程被卡在
内核里,直到规整/回收完成。这就是碎片的真实代价,也是 v2 要量化的东西。

### 1.2 模块分工(升级后)

| 模块 | 挂载点 | 回答的问题 | 优先级 |
|------|--------|-----------|-------|
| `fraginfo.c` (v1) | kprobe `get_page_from_freelist` | 现在有多碎? | 已有 |
| `extfraginfo.c` (v1) | tracepoint `kmem:mm_page_alloc_extfrag` | 谁在制造碎片? | 已有 |
| **`fragstress/`** | 用户态压力注入器 | **能不能可控地造出碎片?** | **P-1** |
| **`compactinfo.c`** | tracepoint `compaction:*` | 规整代价多大?成功率多少? | **P0** |
| **`reclaiminfo.c`** | tracepoint `vmscan:*` | 回收代价多大? | **P1** |
| **`fragbill`(归因)** | 复用 v1 + P0 的数据 | **谁造的碎片?谁付的账?** | **P2 ★创新** |

---

## 二、P-1:压力注入基座(必须最先做)

### 2.1 为什么提到第一位

在一台正常运行的机器上实测:

```
compact_stall 0    compact_fail 0    compact_success 0
allocstall_normal 0    pgscan_direct 0    pgsteal_direct 0
thp_fault_alloc 0      (THP 处于 madvise 模式)
```

**全是 0 —— 从开机到现在一次慢路径都没进过。** 这不是环境有问题,
而是**慢路径本来就是"压力下才出现"的路径**。压力基座如果排在后面,
就会出现"代码全写完了才发现没有数据可看"的局面,前面几天的工作无法验证。

**先能造出压力,再写观测代码。** 而且这个模块本身就是可交付物 ——
"我写了一个能稳定复现 direct compaction stall 的压力注入器",
这句话比多加两个观测指标有用,因为**可复现性是一切性能工作的前提**。

### 2.2 一个陷阱:`compact_memory` 验证不了 direct compaction

```bash
echo 1 | sudo tee /proc/sys/vm/compact_memory   # ← 这条命令有坑
```

它走的是**手动/kcompactd 规整路径**,会打 `mm_compaction_begin/end` 但
**不增加 `compact_stall`**。你会看到工具有事件输出,以为验证通过了,
其实测的是另一条路径。**它只能验证"埋点通不通",不能验证 direct compaction。**

### 2.3 `fragstress`:分层压力注入器

按"由弱到强"分四档,**全部纯用户态,不需要写内核模块**:

**档位 1 —— 制造空洞(外碎片的基础)**
```
大量 mmap 小块 → 随机释放一半 → 剩下的形成"梅花桩"
```
关键:**释放模式必须随机**。顺序释放会被伙伴系统直接合并掉,白做。
这一档能造出"空闲页总量够但零散"的局面,但还不足以让规整失败。

**档位 2 —— 制造 UNMOVABLE 污染(★ 最关键的一档)**

这才是让**规整真正失败**的原因。用户进程的匿名页都是 MOVABLE 的,搬得走;
**真正钉死 pageblock 的是内核对象**。纯用户态触发内核 slab 分配的办法:

```bash
# ① dentry + inode slab:海量小文件/目录
mkdir -p /tmp/frag && cd /tmp/frag
seq 1 200000 | xargs -P8 -n1 touch          # 之后别急着 drop_caches

# ② kernel stack:海量线程
#    每个线程的内核栈是 order-2 的连续 UNMOVABLE 内存,极好的污染源

# ③ file / socket 对象:海量 socket 或打开的 fd
```
观测污染效果:
```bash
watch -n1 'cat /proc/pagetypeinfo'      # Unmovable 行的块数在涨
grep -E "Slab|SUnreclaim" /proc/meminfo
```

**这一档是整个压力基座的灵魂**:它直接对应 v1 里 fallback 污染分组的理论,
也是 P2 归因分析能成立的前提 —— 没有污染,就没有"谁的锅"可查。

**档位 3 —— 制造高阶分配需求(触发 compaction)**
```bash
# 路线 A:THP —— 最容易触发,也是最典型的高阶分配来源
cat /sys/kernel/mm/transparent_hugepage/enabled   # 若是 [madvise] 要改
echo always | sudo tee /sys/kernel/mm/transparent_hugepage/enabled
echo always | sudo tee /sys/kernel/mm/transparent_hugepage/defrag
# 然后跑一个大量分配匿名内存并顺序触碰的程序,看 thp_fault_alloc / thp_fault_fallback

# 路线 B:hugepages —— 触发最直接
echo 500 | sudo tee /proc/sys/vm/nr_hugepages
```

> **THP 是这个项目最好的实验载体**:它是 order-9 分配,几乎必然要规整;
> `defrag=always` 时进程会被同步卡住,`thp_fault_fallback` 计数器还能告诉你
> "有多少次因为凑不出大页而退化成了普通页"。
> 一组 `defrag=always` vs `defrag=never` 的对照数据,就是一份完整的实验报告。

**档位 4 —— 叠加内存压力(逼进 direct reclaim)**
```bash
stress-ng --vm 6 --vm-bytes 90% --vm-keep --timeout 120s
```

### 2.4 验收标准(过不了这关不准往下写)

```bash
watch -n1 'grep -E "compact_stall|compact_fail|compact_success|allocstall|pgsteal_direct|thp_fault_fallback" /proc/vmstat'
```

**`compact_stall` 必须能被顶上去并持续增长**,才算基座可用。
把"跑哪几条命令能稳定复现"记进 `fragstress/README.md` —— 这是后面所有实验的地基。

---

## 三、P0:内存规整(compaction)观测 —— 核心模块

### 3.1 内核原理(必须先讲清,这是深度所在)

**内存规整解决的是外碎片**:空闲页总量够,但都是零散的小块,凑不出连续大块。
手段是**页面迁移(page migration)**:

```
规整前:  [用][空][用][空][用][空][用][空]     ← 空闲页分散,凑不出 order-2
                    ↓ 双向扫描
   migrate_pfn →              ← free_pfn
   (从低地址找可迁移的页)      (从高地址找空闲页)
                    ↓ 把可迁移页搬到高地址空闲处
规整后:  [用][用][用][用][空][空][空][空]     ← 空闲页聚拢,成功凑出大块
```

**migratetype 分组是规整的前提**:只有标记为 `MOVABLE` 的页(用户匿名页、页缓存)
才搬得走,`UNMOVABLE` 的内核数据结构钉死在原地。这就呼应了 v1 的 `extfraginfo.c` ——
**跨类型 fallback 借块污染了分组,让本该可迁移的区域混入不可移动页,规整就更难成功**。
v1 和 v2 在这里逻辑闭环,这也正是 P2 归因分析的理论基础。

**同步 vs 异步规整**:慢路径里先试 `MIGRATE_ASYNC`(遇阻塞就放弃,快但易失败),
不行再升级到 `MIGRATE_SYNC_LIGHT`/`MIGRATE_SYNC`(会等 IO、会阻塞,慢但成功率高)。
**规整耗时的长尾主要来自同步模式** —— 这是延迟分布里最值得看的部分。

### 3.2 挂载点(三层)

```
tracepoint:compaction:mm_compaction_try_to_compact_pages   ← 【外层,必需】
        一次"直接规整"的真实边界,带 order / gfp_mask / prio
tracepoint:compaction:mm_compaction_begin / _end           ← 【内层,per-zone】
        一次分配会遍历多个 zone,打出多对 begin/end;end 带 status
tracepoint:compaction:mm_compaction_migratepages           ← 【可选但推荐】
        带 nr_migrated / nr_failed,比 pfn 差值可靠得多
```

> **外层的 `try_to_compact_pages` 不能省**,原因有两条:
> - `order` **根本不在 begin/end 的字段里**,只有它有 —— 而 order 是最想要的维度
> - begin/end 是 per-zone 的,不是"一次规整"的正确边界

**动手第一步:自己验证字段**(v1 里已经学过这招,务必执行):
```bash
cd /sys/kernel/debug/tracing/events/compaction/
ls                                          # 本机内核到底有哪些
sudo cat mm_compaction_try_to_compact_pages/format
sudo cat mm_compaction_begin/format
sudo cat mm_compaction_end/format
```
`begin` 大致是 `zone_start / migrate_pfn / free_pfn / zone_end / sync`,
`end` 在此基础上多一个 `status`。**以本机 format 的实际输出为准**,
不同内核版本字段会变 —— 这本身就是 tracepoint 使用的必修课。

### 3.3 必须做的三重来源过滤

`compaction:*` 这组 tracepoint 被**三条路径共用**。不过滤,
"规整成功率"这个核心结论会直接失真:

| 来源 | 特征 | 要不要 |
|---|---|---|
| **direct compaction** | 进程上下文,有外层 `try_to_compact_pages` | ✅ 唯一要的 |
| **kcompactd** | `comm` 以 `kcompactd` 开头 | ❌ 后台异步,不阻塞任何人 |
| **手动 `/proc/sys/vm/compact_memory`** | 无外层配对 | ❌ 人为触发 |

实现:**在内核态过滤**(别捞到用户态再扔,白白浪费开销)——
以"当前 tid 是否有活跃的 `try_to_compact_pages` 记录"作为准入条件。

### 3.4 实现要点

**① map key:用 tid,不是 tgid**

```c
// ❌ >> 32 取到的是 tgid(进程号)
u32 pid = bpf_get_current_pid_tgid() >> 32;

// ✅ 正确:compaction 是线程行为
u64 key = bpf_get_current_pid_tgid();   // 完整 u64,信息不丢
// 或 u32 tid = (u32)bpf_get_current_pid_tgid();
```
同一进程的两个线程同时进慢路径,用 tgid 当 key 会互相覆盖 begin 记录。

**② 两层配对结构**
```c
struct outer_t { u64 ts; int order; u32 gfp_mask; int prio; };    // try_to_compact_pages
struct inner_t { u64 ts; u64 migrate_pfn, free_pfn; u8 sync; };   // begin

BPF_HASH(outer_start, u64, struct outer_t, 4096);   // key = pid_tgid
BPF_HASH(inner_start, u64, struct inner_t, 4096);
```
外层给你 `order` 和"一次完整直接规整的总耗时";
内层给你"每个 zone 扫了多少、结果如何"。**两个粒度都有价值,别只做一层。**

配对逻辑:
```c
TRACEPOINT_PROBE(compaction, mm_compaction_end) {
    u64 key = bpf_get_current_pid_tgid();
    struct inner_t *ip = inner_start.lookup(&key);
    if (!ip) { unpaired.increment(0); return 0; }   // ★ 未配对也要计数
    struct outer_t *op = outer_start.lookup(&key);
    if (!op) return 0;                              // ★ 无外层 = 非 direct,过滤掉
    u64 delta = bpf_ktime_get_ns() - ip->ts;
    ...
    inner_start.delete(&key);                       // ★ 必须删
    return 0;
}
```

**③ `BPF_LRU_HASH` 是取舍,不是优化**

LRU 防的是 map 被"有 begin 无 end"的僵尸记录撑爆,
但**代价是淘汰会让 end 找不到 begin,泄漏变成了漏报**:

```
BPF_HASH     : 满了 → update 失败 → 新事件丢失(且旧僵尸永远占位)
BPF_LRU_HASH : 满了 → 淘汰最老 → 长事务被静默丢弃(正是你最想看的长尾!)
```

**这不是纯粹的优化,是 trade-off。** 建议做法:
用 `BPF_LRU_HASH` 兜底 + **额外记一个 `unpaired` 计数器**,上报时把"未配对率"一起打出来。

> 面试被问"BPF map 会泄漏吗",答案不该是"我用了 LRU",而是
> **"我用 LRU 兜底,并统计了未配对率来量化这个兜底带来的数据损失"**。
> 前者是背了个知识点,后者是做过工程。

**④ 扫描页数别用 pfn 差硬算**

一次 `compact_zone` 内扫描器可能重启(`free_pfn` 重置回 zone 尾),
`end.migrate_pfn - begin.migrate_pfn` 会出现负数或离谱大值。三选一:
- 用 `mm_compaction_migratepages` 的 `nr_migrated / nr_failed`(最准)
- 保留 pfn 差但做**合理性检查**(负数或超过 zone 大小 → 标记为无效样本)
- 用 `/proc/vmstat` 的 `compact_migrate_scanned` 做量级校对

**⑤ 延迟直方图(log2 分桶)**
```c
struct hist_key_t { int order; u8 sync; u64 slot; };
BPF_HISTOGRAM(dist, struct hist_key_t);
dist.increment((struct hist_key_t){order, sync, bpf_log2l(delta_ns / 1000)});
```
**为什么用直方图不用平均值**:延迟是长尾分布,平均值会把问题掩盖掉。
"平均 2ms,但 P99 是 300ms" —— 后者才是真正卡住用户的那部分。
这是性能观测的**基本方法论**。按 `order` / `sync` 分维度,
能直接画出"order 越高、同步模式 → 规整越慢"的趋势,用数据印证碎片理论。

**⑥ `status` 结果码**(`enum compact_result`,**取值随内核版本变,必须对照本机确认**)

| status | 含义 | 说明 |
|--------|------|------|
| `COMPACT_SKIPPED` | 跳过 | 内存太少,规整没意义 |
| `COMPACT_DEFERRED` | 推迟 | **之前连续失败过,内核主动退避**(防止反复做无用功) |
| `COMPACT_SUCCESS` | 成功 | 凑出了大块 |
| `COMPACT_COMPLETE` | 扫完了但没成功 | **最坏情况:白白扫了一整个 zone** |
| `COMPACT_CONTENDED` | 锁竞争放弃 | 被别的 CPU 抢锁 |

**这张表是 P0 的价值所在**:能区分"规整成功"(有效开销)和
"扫了半天白扫"(纯粹的性能损失)。能讲清这个区别 = 真读懂了。

**⑦ 限流** —— 沿用 v1 已修正的写法(固定 `key=0`)。

### 3.5 交叉验证(两条线)

**线一:vmstat 计数器**
```bash
grep -E "compact_" /proc/vmstat
# compact_stall           ← 应与你统计的 direct compaction 次数吻合
# compact_fail            ← 应与 status != SUCCESS 的次数吻合
# compact_success
# compact_migrate_scanned ← 校对你算的扫描页数量级
# compact_daemon_wake     ← 这部分应被你过滤掉,不该出现在统计里
```
最后一行尤其有价值:**它能直接证明三重过滤做对了**。

**线二:PSI(压力失速信息)**
```bash
cat /proc/pressure/memory
# some avg10=... total=...   ← 至少一个任务因内存阻塞的累计时长(微秒)
# full avg10=... total=...   ← 所有任务都被阻塞的累计时长
```
`total` 字段是**内核自己统计的"因内存问题被阻塞的总时长"**,
和你要测的东西同源。压力实验前后取差值,应该与你统计的
compaction + reclaim 阻塞总时长在同一量级。

> 两条独立的验证线(计数器 + 阻塞时长),比只对一条更有说服力。
> **"我用 /proc/vmstat 和 PSI 两套内核原生数据交叉验证了 eBPF 观测结果"**
> —— 这句话的分量,比多加两个功能重得多。

---

## 四、P1:直接内存回收(direct reclaim)观测

### 4.1 内核原理

慢路径的另一条腿。**回收和规整解决的是不同问题**,这个区别必须能讲清楚:

| | direct reclaim | direct compaction |
|---|---|---|
| 解决 | **内存不够**(总量不足) | **内存够但碎**(总量足,不连续) |
| 手段 | 换出匿名页 / 丢弃页缓存 → **腾出页** | 迁移页面 → **拼出连续块** |
| 触发 | 水位线低于 min | 高阶分配失败 |
| 代价 | 可能触发磁盘 IO,极慢 | CPU 密集扫描 + 页拷贝 |

**`kswapd` vs `direct reclaim`**:前者是后台内核线程异步回收(不阻塞任何人),
后者是**申请内存的进程自己被拉去干回收的活** —— 进程被同步卡住。
**direct reclaim 频繁 = 内存压力已经大到 kswapd 兜不住了**,是明确的坏信号。

### 4.2 挂载点与实现

```
tracepoint:vmscan:mm_vmscan_direct_reclaim_begin   → 记 ts、order
tracepoint:vmscan:mm_vmscan_direct_reclaim_end     → 算 delta、取 nr_reclaimed
```
同样先验证字段:
```bash
sudo cat /sys/kernel/debug/tracing/events/vmscan/mm_vmscan_direct_reclaim_begin/format
sudo cat /sys/kernel/debug/tracing/events/vmscan/mm_vmscan_direct_reclaim_end/format
```

实现模式和 P0 **完全一致**(begin/end 配对 + 直方图 + 未配对率统计),
所以 P1 的实际工作量很小 —— 这也是先做 P0 的原因,骨架搭好后 P1 近乎复制粘贴。
**key 同样要用 tid**,direct reclaim 也是线程行为。

**额外产出 `nr_reclaimed`**:本次回收了多少页。结合耗时可算出
**回收效率(页/毫秒)** —— 效率越低,越说明是在做无用功。

交叉验证:
```bash
grep -E "allocstall|pgscan_direct|pgsteal_direct" /proc/vmstat
# pgsteal_direct / pgscan_direct = 直接回收的"命中率",可与你的效率指标对照
```

---

## 五、P2:创新点 —— 从"测量"走向"归因"和"结论"

> 坦白说:P0 的 compaction 延迟统计,bcc 已有 `compactsnoop`;
> P1 的 direct reclaim,bcc 已有 `drsnoop`。
> **只做 P0+P1,你交付的是两个现成工具的复刻。**
> 真正的差异化在这一节。三个创新点,按性价比排序。

### 创新点 A:碎片"责任账单" —— 制造者与受害者的归因闭环 ★★★

**现有工具的共同盲区**:它们只测"发生了什么",
**没有一个把"谁制造了碎片"和"谁付出了代价"关联起来**。

```
   进程 A 频繁 fallback 跨类型借块       (v1 的 extfraginfo 能抓到)
        ↓ 污染了某个 pageblock 的 migratetype 分组
        ↓ 【断层 —— 现有工具到此为止】
   进程 B 申请高阶内存 → 掉进慢路径 → 被 compaction 卡了 200ms   (P0 能抓到)
        ↓ 而且规整还失败了(COMPACT_COMPLETE),因为该区域有不可迁移页
```

**你手里正好两头都有,要做的就是接上这个断层。** 分两个层次,先易后难:

**层次 1 —— 时间窗口相关性(一天能做完)**

按时间窗口聚合,输出一张"账单表":

```
时间窗     fallback 制造者(次数)      被 stall 的受害者(累计阻塞)  规整失败率
──────────────────────────────────────────────────────────────────────────
T+0~10s    nginx(1203)  mysqld(89)    java(340ms)                   12%
T+10~20s   nginx(3401)  ...           java(1.2s)                    67%   ← 相关性明显
```
再给出"fallback 速率 vs stall 总时长"的相关系数。
成本极低,结论已经有说服力。

**层次 2 —— pageblock 级精确归因(真正的硬货)**
```
① extfraginfo 抓到 fallback 时,记录 pfn → 算出 pageblock 编号
   → map 登记:"pageblock #N 被进程 X 污染过"
② compaction 迁移失败时,拿到失败区间的 pfn → 算出 pageblock 编号
   → 反查 map:"这块是谁污染的?"
③ 输出:"进程 X 污染的 pageblock,导致进程 Y 的规整失败 N 次"
```
实现细节:
- `pageblock_id = pfn >> pageblock_order`(x86_64 通常 order=9,即 2MB,**需确认本机**)
- 用 `BPF_LRU_HASH(pageblock_owner, u64 blk_id, struct owner_t)` ——
  这里 LRU 语义天然合适:老的污染记录本来就该被淘汰
- 精确的失败页 pfn 未必拿得到,退而求其次用
  `mm_compaction_migratepages` 的失败计数 + begin/end 的 pfn 区间做区间归属

**为什么这是真创新**:它把工具从"指标采集器"变成 **"责任定位器"** ——
直接回答那句"到底是谁的锅"。这个问题在真实排障里价值极高,
而且据我所知没有现成工具做。**这是整个 v2 里最值得投入的一块。**

### 创新点 B:`extfrag_threshold` 默认值的实证检验 ★★

内核用 `__fragmentation_index` 判断"这次规整值不值得做",
阈值是 `/proc/sys/vm/extfrag_threshold`,**默认 500 —— 一个经验值**。

v1 已经能算碎片指数了,那就顺手做个**实证研究**:

```
① 在 compaction 触发的那一刻,记录当时该 zone 的碎片指数(v1 已有能力)
② 记录这次规整的最终 status(成功 / 白扫 / 跳过)
③ 把(碎片指数, 是否成功)画成分布 / ROC 曲线
④ 回答:500 这个阈值,在你造出的负载上分得开吗?最优分割点是多少?
```

**产出结论形如**:
> "在 order-9(THP)分配场景下,`extfrag_threshold=500` 的默认值导致 X% 的
> 无效规整(COMPACT_COMPLETE);实测最优分割点在 ~700,
> 调整后 direct compaction 的平均阻塞时间下降 Y%"

**成本很低**(数据 P0 已经采到了,只是多做一步分析),
**但性质完全不同**:从"我实现了一个工具"变成 **"我用工具得出了一个结论"**。
这种东西能写技术博客,也能拿去社区讨论。

**顺带一个几乎白送的对照实验**:`defrag=always` vs `defrag=never` 下
跑同一套 THP 负载,对比 `thp_fault_fallback` 和 stall 总时长 ——
这就是业界"该不该关 THP"那个著名争论的一手数据。

### 创新点 C:碎片健康度 SLI —— 一个能上监控的单一指标 ★

把一堆指标压成一个可运维的数:

```
碎片税(fragmentation tax) = 单位时间内进程因 compaction/reclaim 阻塞的总时长
                            ─────────────────────────────────────────────
                                          总 CPU 时间
```

**它有个天然的校验物**:`/proc/pressure/memory` 的 `total` 字段
就是内核自己算的"因内存阻塞的累计时长"。你的"碎片税"是它的**细分归因版本** ——
PSI 只告诉你"卡了多久",你的指标能告诉你"其中多少是碎片造成的、卡在谁身上"。
**能说清自己的指标和内核原生 PSI 的关系,是这个创新点的真正价值。**

### 建议取舍

**必做 A 的层次 1 + B**,加起来 1.5~2 天,是性价比最高的组合。
A 的层次 2 有时间就上,它是最出彩的部分;C 是收尾的十几分钟工作。

---

## 六、实施步骤(7~8 天)

| 天 | 任务 | 验收标准(硬性) |
|---|------|-----------------|
| **D0** | 写 `fragstress` 四档压力注入器 | **`compact_stall` 能稳定被顶上去**,复现步骤写进 README |
| D1 | 验证 tracepoint 字段;读 `mm/compaction.c` 的 `compact_zone()` 主循环 | 说得清双扫描器机制;确认字段和 `enum compact_result` 取值 |
| D2 | `compactinfo.c`:两层配对 + 三重过滤 + 基础延迟 | 能打印每次 direct compaction 耗时,`kcompactd` 不出现在结果里 |
| D3 | 加 status 分类、sync 区分、order 维度直方图、未配对率 | 能看到延迟分布和规整成功率 |
| D4 | **交叉验证**:vmstat 对账 + PSI 量级比对 | begin 次数 ≈ `compact_stall`,失败次数 ≈ `compact_fail` |
| D5 | `reclaiminfo.c`(复用 D2/D3 骨架) | direct reclaim 延迟分布 + 回收效率 |
| D6 | **创新点 B**:碎片指数 vs 规整结果的相关性 + THP defrag 对照实验 | 一张能说明问题的分布图 + 一个阈值建议 |
| D7 | **创新点 A 层次 1**:责任账单(时间窗口相关性) | 能输出"制造者 / 受害者"对照表 |
| D8 | (机动)创新点 A 层次 2 / 创新点 C / 整理结论 | — |

**风险预留**:D0 最容易超时。把机器稳定逼进 direct compaction 可能单独吃掉一整天,
别把工期排满。

---

## 七、完成后的项目叙事

**升级前**:"我用 eBPF 做了个内存碎片检测工具,能算碎片指数。"

**升级后**:

> "我做了一套 Linux 内存分配慢路径的观测与归因工具。
>
> 内核分配内存时,快路径直接从伙伴系统摘块;凑不出连续块就会掉进慢路径,
> 被迫做**内存规整**(迁移页面拼大块)或**直接回收**(换出页面腾空间),
> 这两者都是同步阻塞的,是碎片真正的性能代价。
>
> 我先写了一个**分层的碎片压力注入器** —— 因为正常机器上这条路径根本不触发,
> `compact_stall` 一直是 0,没有可复现的压力场景,后面的测量都是空的。
> 其中最关键的一档是用海量小文件和线程去制造内核对象,污染 pageblock 的
> migratetype 分组,这样规整才会真的失败。
>
> 观测上,用 kprobe 挂 `get_page_from_freelist` 算碎片指数,用
> `kmem:mm_page_alloc_extfrag` 归因碎片制造者,用 `compaction:*` 的三层
> tracepoint 量化规整的耗时分布和成功率 —— 这里要做三重来源过滤,
> 因为 kcompactd 和手动触发共用同一组埋点,不过滤统计就是脏的。
>
> **真正花心思的是归因**:我把"谁制造碎片"和"谁被卡住"关联了起来,
> 按 pageblock 追踪污染来源,能直接回答'这次规整失败是谁的锅'。
> 另外用采到的数据检验了内核 `extfrag_threshold` 默认值 500 的合理性,
> 发现在高阶分配场景下它会导致相当比例的无效规整。
>
> 所有观测结果都用 `/proc/vmstat` 的计数器和 PSI 的阻塞时长做了双线交叉校验。"

### 能接住的追问

- **"compaction 和 reclaim 什么区别?"** → §4.1 那张表(碎不碎 vs 够不够)
- **"为什么规整会失败?什么页不能迁移?"** → migratetype 分组 / UNMOVABLE 污染 / 被 pin 住的页
- **"order-0 分配会触发 compaction 吗?"** → 不会,规整只为高阶连续块服务
- **"高阶分配都来自哪?"** → THP、hugepages、内核栈(order-2)、slab 大对象、驱动 DMA
- **"为什么用直方图不用平均延迟?"** → 长尾分布,P99 才是用户体感
- **"tracepoint 和 kprobe 怎么选?"** → 项目里两种都用了,有现成对照
- **"BPF map 会泄漏吗?"** → LRU 兜底,但代价是长事务被静默丢弃;所以我额外统计了未配对率
- **"怎么保证数据准确?"** → 三重过滤 + vmstat 对账 + PSI 比对 + 未配对率
- **"eBPF 观测本身有开销吗?"** → 限流 + 内核态过滤 + tracepoint 优于全量扫描

---

## 八、技术清单(自查用)

**操作系统 / 内核**
- [ ] 伙伴系统 free_area / order / migratetype 分组
- [ ] 内外碎片的区别;fallback 为何加剧碎片
- [ ] `__alloc_pages_slowpath` 的完整决策序列(含 reclaim/compact 的真实顺序)
- [ ] 页面迁移与内存规整的双扫描器机制
- [ ] 同步 vs 异步规整;`COMPACT_DEFERRED` 的退避设计
- [ ] direct reclaim vs kswapd;水位线(min/low/high)
- [ ] 为什么 UNMOVABLE 页会毁掉规整效果;什么操作会产生 UNMOVABLE 页
- [ ] `__fragmentation_index` 的计算方式与 `extfrag_threshold` 的作用
- [ ] THP 的分配路径、`defrag` 各档位的含义、fallback 的代价
- [ ] PSI 是什么、它统计的是哪种时间

**eBPF / 观测**
- [ ] tracepoint vs kprobe 的取舍与稳定性差异
- [ ] BPF map 作为唯一跨调用状态;LRU 的收益与代价
- [ ] begin/end 事件配对测延迟;多层嵌套事件的配对
- [ ] 共用埋点的来源过滤(内核态过滤优于用户态)
- [ ] 直方图 + log2 分桶;为什么看分布不看均值
- [ ] verifier 约束:`bpf_probe_read_kernel`、无浮点、有界循环
- [ ] 观测工具自身的开销控制(限流、内核态过滤、采样)

**工程方法**
- [ ] 可复现的压力场景是性能工作的前提
- [ ] 用内核原生数据交叉验证自己的观测(而且不止一条线)
- [ ] 数据可信度要自己量化(未配对率、无效样本标记)

---

## 附一:不做什么(避免范围膨胀)

- ❌ 不学 `guard_tools_minimal` 的 fs_guard / sched_guard / lkm-fm(与主题无关)
- ❌ 不加内存泄漏检测(独立课题,该是另一个项目)
- ❌ 不动 Python 展示层(不是加分项)
- ❌ 不写内核模块造压力(纯用户态的档位 2 已经够用)
- ✅ 只做两件事:**把碎片的代价量化清楚,并归因到人**

---

## 附二:相对初稿(`fraginfo_v2_draft1.md`)的改动

> 施工时不必看这一节,它只用于回溯"为什么是现在这个方案"。

| 改动 | 原因 |
|---|---|
| **压力基座从 D5 提到最优先(P-1)**,升级为独立可交付模块 | 正常机器 `compact_stall=0`,不先造出压力,写完代码没数据看 |
| **关联分析从"可选"升为 P2 核心**,拆成三个具体创新点 | 只做 P0+P1,交付的是 bcc 现成工具的复刻 |
| 修正慢路径流程图顺序 | 初稿把 reclaim/compact 的先后画反了 |
| map key 由 tgid 改 tid | 多线程进程会互相覆盖,是实打实的 bug |
| `try_to_compact_pages` 由"可选"改"必需",挂载点改为三层 | `order` 不在 begin/end 字段里;begin/end 是 per-zone 的 |
| 新增**三重来源过滤** | kcompactd / 手动触发 / direct 共用同一组埋点,不过滤统计全是脏的 |
| LRU map 的取舍重写,新增"未配对率"统计 | 初稿把 LRU 说成纯优化,实际是拿泄漏换漏报 |
| 扫描页数不再用 pfn 差硬算 | 扫描器会重启,差值可能为负 |
| 新增 PSI(`/proc/pressure/memory`)作为第二条交叉验证线 | 内核原生的阻塞时长统计,和本项目要测的东西同源 |
| 工期 4~6 天 → 7~8 天 | 压力场景复现的真实成本 |
