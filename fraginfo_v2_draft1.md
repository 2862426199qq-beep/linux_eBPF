# fraginfo v2 项目计划书

> **一句话**:把项目从"内存碎片状态观测"升级为 **"内核内存分配慢路径(slowpath)全景观测"**。
>
> v1 回答了"有多碎"和"谁造成的";v2 回答 **"碎片让内核付出了什么代价"** ——
> 补上这一环,项目就从一个统计脚本变成一条完整的因果链。

---

## 一、立意:为什么这是个"够格"的升级

### 1.1 内核视角:一次内存分配的完整决策路径

这是操作系统内存管理最核心的一条路径,教科书级别:

```
alloc_pages(order)
   │
   ├─► 【快路径】get_page_from_freelist()          ← v1 的 fraginfo.c 挂在这里
   │      从伙伴系统各 zone 的 free_area[order] 直接摘块
   │      ├─ 本 migratetype 有货 → 成功返回
   │      └─ 没货 → __rmqueue_fallback() 跨类型借块  ← v1 的 extfraginfo.c 挂在这里
   │                (制造外碎片,打 mm_page_alloc_extfrag 埋点)
   │
   └─► 快路径彻底失败 → 【慢路径】__alloc_pages_slowpath()   ★ v2 的战场 ★
          │
          ├─ ① 唤醒 kswapd,异步回收
          ├─ ② 直接内存规整 direct compaction    ← 【P0】页面迁移,把碎片拼成大块
          ├─ ③ 直接内存回收 direct reclaim       ← 【P1】同步换出/丢缓存
          ├─ ④ 重试(可能多轮 ②③ 循环)
          └─ ⑤ 全部失败 → OOM Killer
```

**关键认知**:碎片本身不是问题,**碎片把分配从"快路径"逼进"慢路径"才是问题**。
慢路径里的 compaction 和 reclaim 都是**同步阻塞**的——申请内存的那个进程会被卡在
内核里,直到规整/回收完成。这就是碎片的真实代价,也是 v2 要量化的东西。

### 1.2 三个文件的分工(升级后)

| 文件 | 挂载点 | 回答的问题 | 内核子系统 |
|------|--------|-----------|-----------|
| `fraginfo.c` (v1) | kprobe `get_page_from_freelist` | 现在有多碎? | 伙伴系统 |
| `extfraginfo.c` (v1) | tracepoint `kmem:mm_page_alloc_extfrag` | 谁在制造碎片? | 伙伴系统 fallback |
| **`compactinfo.c` (P0)** | tracepoint `compaction:*` | **规整代价多大?成功率多少?** | **页面迁移 / 内存规整** |
| **`reclaiminfo.c` (P1)** | tracepoint `vmscan:*` | **回收代价多大?** | **页面回收 / LRU** |

---

## 二、P0:内存规整(compaction)观测 —— 核心模块

### 2.1 内核原理(必须先讲清,这是深度所在)

**内存规整解决的是"外碎片"**:空闲页总量够,但都是零散的小块,凑不出连续大块。
规整的做法是 **页面迁移(page migration)**:

```
规整前:  [用][空][用][空][用][空][用][空]     ← 空闲页分散,凑不出 order-2
                    ↓ 双向扫描
   migrate_pfn →              ← free_pfn
   (从低地址找可移动的页)      (从高地址找空闲页)
                    ↓ 把可移动页搬到高地址空闲处
规整后:  [用][用][用][用][空][空][空][空]     ← 空闲页聚拢,成功凑出大块
```

**为什么 migratetype 分组是规整的前提**:只有标记为 `MOVABLE` 的页才能被搬走
(用户进程的匿名页、页缓存),`UNMOVABLE` 的内核数据结构钉死在原地不能动。
这就呼应了 v1 里 `extfraginfo.c` 抓的 fallback——**跨类型借块污染了分组,
让本该可迁移的区域混入不可移动页,规整就更难成功**。v1 和 v2 在这里逻辑闭环。

**同步 vs 异步规整**:慢路径里会先试 `MIGRATE_ASYNC`(遇到阻塞就放弃,快但易失败),
不行再升级到 `MIGRATE_SYNC_LIGHT`/`MIGRATE_SYNC`(会等 IO、会阻塞,慢但成功率高)。
**规整耗时的长尾,主要来自同步模式**——这是延迟分布里最值得看的部分。
```
MIGRATE_ASYNC(异步规整):快速尝试,如果遇到阻塞(有进程还在用这个页)就放弃,风险是失败率高
MIGRATE_SYNC(同步规整):会等待IO操作完成,能等到进程释放资源,成功率高但耗时长,这是延迟长尾的主要来源
```
### 2.2 挂载点

```
tracepoint:compaction:mm_compaction_begin    → 规整开始
tracepoint:compaction:mm_compaction_end      → 规整结束(带 status 结果码)
tracepoint:compaction:mm_compaction_try_to_compact_pages  → 带 order/gfp/prio,可选
```

> ⚠️ **动手第一步:自己验证字段**(v1 里已经学过这招,务必执行):
> ```bash
> sudo cat /sys/kernel/debug/tracing/events/compaction/mm_compaction_begin/format
> sudo cat /sys/kernel/debug/tracing/events/compaction/mm_compaction_end/format
> ls /sys/kernel/debug/tracing/events/compaction/    # 看本机内核到底有哪些
> ```
> `begin` 大致有 `zone_start / migrate_pfn / free_pfn / zone_end / sync`,
> `end` 在此基础上多一个 `status`。**以你机器上 format 的实际输出为准**,
> 不同内核版本字段会变——这本身就是 tracepoint 使用的必修课。

### 2.3 要产出的数据

```c
struct compact_event_t {
    u64 delta_ns;          // ★ 本次规整耗时
    u32 pid;
    int order;             // 触发规整的分配阶(需从 try_to_compact_pages 关联)
    int status;            // ★ 规整结果码(见下表)
    u8  sync;              // ★ 同步(1)还是异步(0)模式
    u64 migrate_scanned;   // 迁移扫描器扫过的页数(可由 begin/end 的 pfn 差算出)
    u64 free_scanned;      // 空闲扫描器扫过的页数
    char comm[16];
};
```

**`status` 结果码解读**(`enum compact_result`,值需对照本机内核确认):

| status | 含义 | 说明 |
|--------|------|------|
| `COMPACT_SKIPPED` | 跳过 | 内存太少,规整没意义 |
| `COMPACT_DEFERRED` | 推迟 | **之前连续失败过,内核主动跳过**(防止反复做无用功) |
| `COMPACT_SUCCESS` | 成功 | 凑出了大块 |
| `COMPACT_COMPLETE` | 扫完了但没成功 | **最坏情况:白白扫了一整个 zone** |
| `COMPACT_CONTENDED` | 锁竞争放弃 | 被别的 CPU 抢锁 |

> **这张表是 P0 的价值所在**:能区分"规整成功"和"扫了半天白扫",
> 前者是有效开销,后者是纯粹的性能损失。面试里能讲这个区别 = 真读懂了。

### 2.4 技术要点(典型 eBPF 观测技术,要用上)

**① begin/end 配对测延迟** —— 你已经掌握的模式,直接复用:
```c
BPF_HASH(compact_start, u32, struct start_t);   // key = pid

TRACEPOINT_PROBE(compaction, mm_compaction_begin) {
    u32 pid = bpf_get_current_pid_tgid() >> 32;
    struct start_t s = {};
    s.ts = bpf_ktime_get_ns();
    s.migrate_pfn = args->migrate_pfn;   // 记下起始扫描位置
    s.free_pfn    = args->free_pfn;
    s.sync        = args->sync;
    compact_start.update(&pid, &s);
    return 0;
}

TRACEPOINT_PROBE(compaction, mm_compaction_end) {
    u32 pid = bpf_get_current_pid_tgid() >> 32;
    struct start_t *sp = compact_start.lookup(&pid);
    if (!sp) return 0;                        // 没配上对,丢弃
    u64 delta = bpf_ktime_get_ns() - sp->ts;
    // 扫描页数 = 两个扫描器移动的距离(体现规整"干了多少活")
    u64 migrate_scanned = args->migrate_pfn - sp->migrate_pfn;
    u64 free_scanned    = sp->free_pfn - args->free_pfn;
    ...
    compact_start.delete(&pid);               // ★ 必须删,否则 map 泄漏
    return 0;
}
```

**② 延迟直方图(BPF_HISTOGRAM + log2 分桶)** —— 比记平均值专业得多:
```c
BPF_HISTOGRAM(latency_hist);
latency_hist.increment(bpf_log2l(delta_ns / 1000));   // 微秒,log2 分桶
```
**为什么用直方图不用平均值**:延迟是长尾分布,平均值会被掩盖。
"平均 2ms,但 P99 是 300ms"——后者才是真正卡住用户的那部分。
这是性能观测的**基本方法论**,面试提到会加分。

**③ 按维度分桶统计**:
```c
struct hist_key_t { int order; u8 sync; u64 slot; };   // 组合 key
BPF_HISTOGRAM(dist, struct hist_key_t);
```
能画出"order 越高 / 同步模式 → 规整越慢"的趋势,直接印证碎片理论。

**④ map 选型** —— v1 优化清单里提过的坑,这里正好实践:
```c
BPF_LRU_HASH(compact_start, u32, struct start_t);   // 而非 BPF_HASH
```
进程若在 begin 之后、end 之前退出,记录会永久滞留。`BPF_LRU_HASH` 自动淘汰最老条目,
避免 map 被僵尸记录填满导致漏报。**这是长期运行的观测工具必须考虑的问题**。

**⑤ 限流** —— 沿用 v1 修好的正确写法(固定 `key=0`),别再犯那个 bug。

---

## 三、P1:直接内存回收(direct reclaim)观测

### 3.1 内核原理

慢路径的另一条腿。**回收(reclaim)和规整(compaction)解决的是不同问题**——
这个区别必须能讲清楚:

| | direct reclaim | direct compaction |
|---|---|---|
| 解决 | **内存不够**(总量不足) | **内存够但碎**(总量足,不连续) |
| 手段 | 换出匿名页 / 丢弃页缓存 → **腾出页** | 迁移页面 → **拼出连续块** |
| 触发 | 水位线低于 min | 高阶分配失败 |
| 代价 | 可能触发磁盘 IO,极慢 | CPU 密集扫描 + 页拷贝 |

**`kswapd` vs `direct reclaim`**:前者是后台内核线程异步回收(不阻塞任何人),
后者是**申请内存的进程自己被拉去干回收的活**——进程被同步卡住。
**direct reclaim 频繁 = 内存压力已经大到 kswapd 兜不住了**,是明确的坏信号。

### 3.2 挂载点与实现

```
tracepoint:vmscan:mm_vmscan_direct_reclaim_begin   → 记 ts、order
tracepoint:vmscan:mm_vmscan_direct_reclaim_end     → 算 delta、取 nr_reclaimed
```

同样先验证字段:
```bash
sudo cat /sys/kernel/debug/tracing/events/vmscan/mm_vmscan_direct_reclaim_begin/format
sudo cat /sys/kernel/debug/tracing/events/vmscan/mm_vmscan_direct_reclaim_end/format
```

实现模式和 P0 **完全一致**(begin/end 配对 + LRU map + 直方图),
所以 P1 的实际工作量很小——这也是先做 P0 的原因,骨架搭好后 P1 是复制粘贴级别。

**额外产出 `nr_reclaimed`**:本次回收了多少页。结合耗时可以算出
**"回收效率"(页/毫秒)**——效率越低,说明越是在做无用功。

---

## 四、加分项:关联分析(如果时间允许)

这是把三个文件真正串起来的一步,也是最能体现"系统性思考"的地方:

**在 compaction 事件发生的那一刻,记录当时的碎片指数**。

```
compaction 触发 → 查 zone_map 里该 zone 当前的 score_a/score_b → 一起上报
```

产出结论形如:
> "当 order-4 的 `__fragmentation_index` > 900 时,
> direct compaction 触发频率上升 N 倍,且 `COMPACT_COMPLETE`(白扫)占比达 X%"

**这就从"我测了几个指标"上升到"我验证了碎片指数与系统实际开销的相关性"**,
是一个完整的、可讲的技术结论。

---

## 五、实施步骤(建议 4~6 天)

| 天 | 任务 | 产出 |
|---|------|------|
| D1 | 验证 tracepoint 字段;读内核 `mm/compaction.c` 的 `compact_zone()` 主循环 | 搞懂双扫描器机制,确认可用字段 |
| D2 | 写 `compactinfo.c`:begin/end 配对 + 基础延迟统计 | 能打印每次规整耗时 |
| D3 | 加 status 分类、sync 区分、直方图分桶 | 能看到延迟分布和成功率 |
| D4 | 写 `reclaiminfo.c`(复用 D2/D3 骨架) | direct reclaim 延迟统计 |
| D5 | 制造压力场景验证 + `/proc/vmstat` 交叉校验 | 数据可信 |
| D6 | (可选)关联分析 + 整理结论 | 完整叙事 |

### 5.1 验证方法(工程严谨性,别跳过)

**制造碎片和压力**:
```bash
# 制造碎片:大量分配释放混合大小的内存
stress-ng --vm 4 --vm-bytes 80% --timeout 60s

# 手动触发全系统规整(最直接的验证手段)
echo 1 | sudo tee /proc/sys/vm/compact_memory

# 制造高阶分配需求
echo 500 | sudo tee /proc/sys/vm/nr_hugepages
```

**交叉验证(关键!)** —— 用内核自己的统计计数器校对你的观测:
```bash
grep -E "compact_|pgscan|pgsteal" /proc/vmstat
# compact_stall      : 直接规整发生次数    ← 应与你统计的 begin 次数吻合
# compact_fail       : 规整失败次数        ← 应与 status != SUCCESS 吻合
# compact_success    : 规整成功次数
# compact_daemon_wake: kcompactd 唤醒次数
```

**"我用 /proc/vmstat 的内核原生计数器交叉验证了 eBPF 观测结果的准确性"**
——这句话在面试里的分量,比多加两个功能重得多。

---

## 六、完成后的项目叙事

**升级前**:"我用 eBPF 做了个内存碎片检测工具,能算碎片指数。"

**升级后**:

> "我做了一套 Linux 内存分配慢路径的观测工具。内核分配内存时,快路径直接从伙伴系统
> 摘块;凑不出连续块就会掉进慢路径,被迫做**内存规整**(迁移页面拼大块)或
> **直接回收**(换出页面腾空间),这两者都是同步阻塞的,是碎片真正的性能代价。
>
> 我用 kprobe 挂 `get_page_from_freelist` 全量扫描伙伴系统算碎片指数,用
> `kmem:mm_page_alloc_extfrag` tracepoint 归因到具体进程,再用 `compaction:*` 和
> `vmscan:*` 两组 tracepoint 量化规整/回收的耗时分布和成功率。
>
> 最终验证了:碎片指数升高会显著增加 direct compaction 的触发频率,且相当比例的规整
> 是扫完整个 zone 却没凑出大块(`COMPACT_COMPLETE`)的无效开销。观测结果用
> `/proc/vmstat` 的内核原生计数器做了交叉校验。"

### 能接住的追问

- **"compaction 和 reclaim 什么区别?"** → 见 §3.1 那张表(碎不碎 vs 够不够)
- **"为什么规整能成功?什么页不能迁移?"** → migratetype 分组 / MOVABLE vs UNMOVABLE
- **"为什么用直方图不用平均延迟?"** → 长尾分布,P99 才是用户体感
- **"tracepoint 和 kprobe 怎么选?"** → 项目里两种都用了,有现成对照
- **"eBPF 观测本身有开销吗?"** → 限流设计 + 只在事件发生时触发(tracepoint 优于全量扫描)
- **"BPF map 会泄漏吗?"** → begin 有 end 无的场景 + `BPF_LRU_HASH` 兜底

---

## 七、技术清单(自查用)

做完 v2,以下你都该能讲:

**操作系统 / 内核**
- [ ] 伙伴系统 free_area / order / migratetype 分组
- [ ] 内外碎片的区别;fallback 为何加剧碎片
- [ ] `__alloc_pages_slowpath` 的完整决策序列
- [ ] 页面迁移与内存规整的双扫描器机制
- [ ] 同步 vs 异步规整;`COMPACT_DEFERRED` 的退避设计
- [ ] direct reclaim vs kswapd;水位线(min/low/high)
- [ ] 为什么 UNMOVABLE 页会毁掉规整效果

**eBPF / 观测**
- [ ] tracepoint vs kprobe 的取舍与稳定性差异
- [ ] BPF map 作为唯一跨调用状态;LRU 防泄漏
- [ ] begin/end 事件配对测延迟的通用模式
- [ ] 直方图 + log2 分桶;为什么看分布不看均值
- [ ] verifier 约束:`bpf_probe_read_kernel`、无浮点、有界循环
- [ ] 观测工具自身的开销控制(限流、内核态过滤)

---

## 附:不做什么(避免范围膨胀)

- ❌ 不学 `guard_tools_minimal` 的 fs_guard / sched_guard / lkm-fm(与主题无关)
- ❌ 不加内存泄漏检测(独立课题,该是另一个项目)
- ❌ 不动 Python 展示层(不是加分项)
- ✅ 只做一件事:**把碎片的代价量化清楚**
