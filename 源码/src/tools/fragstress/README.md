# fragstress —— 物理内存碎片压力注入器

**它解决的问题**：一台干净的 Linux 机器上，`/proc/vmstat` 里的
`compact_stall`、`pgscan_direct` 全是 0 —— 从开机到现在，**没有任何一个进程
因为内存问题被卡住过**。在这种机器上，观测慢路径的 eBPF 探针写得再对也是空表。

**所以必须先能可控地造出压力。** 这个目录就是干这个的，
全部是**普通用户态 C 程序和 shell 脚本，和 eBPF 无关**
（`src/bpf/` 只放 BCC 加载的内核态源码，不要把这里的东西混进去）。

---

## 一分钟上手

```bash
make
sudo -v                                                    # ★ 必须先单独输密码，见下方"坑"
sudo nohup ./run.sh > /tmp/fragstress_run.log 2>&1 &
tail -f /tmp/fragstress_run.log
```

跑完自动清理（大页池归零、压力源退出），机器恢复原状。约 3~6 分钟。

**判据**：日志末尾的
`✓ 硬门槛通过：本轮真实触发了 N 次 direct compaction`。

---

## 已验证可复现的结果（本机，内核 5.15.0-139-generic / 4 核 / 12 GB）

实测两轮，同样的命令、同样的默认参数：

| | 第三轮 2026-08-10 23:15 | 第四轮 2026-08-11 00:10 |
|---|---|---|
| 白拿库存（自动测得） | 1652 个大页 | 1630 个大页 |
| 停止原因 | 内存榨干（分配开始失败） | 拿到 402 次 stall，证据充分 |
| 最终大页数 | 1935 | 2067 |
| **`compact_stall`** | **+42** | **+402** |
| `compact_success` | 0 | 370 |
| `compact_fail` | 42 | 32 |
| **规整成功率** | **0.0%** | **92.0%** |
| `compact_isolated` | 179685 页 | 457047 页 |
| `free/migrate` 扫描比 | 0.81 | 7.36 |
| `pgscan_direct` / `pgsteal_direct` | 8731 / 6361 | 4409 / 2958 |
| PSI memory some / full | 178 ms / 156 ms | 4691 ms / 4093 ms |

**两轮测得的"库存"只差 1.3%（1652 vs 1630）** —— 压力配方是确定性的。

### ★ 最重要的发现：规整成功率有个悬崖

两轮的差别**只在于压到多深**：

- 第四轮在"每一批都还能拿满"的时候就收工了 → 成功率 **92%**
- 第三轮一直压到"这一批一个都拿不到" → 成功率 **0%**

也就是说，**只要分配还能被满足，规整基本都成功；一旦分配开始失败，规整就
完全失效**。中间没有平滑过渡，是个悬崖。

两轮的 `free/migrate` 扫描比也正好翻转（7.36 → 0.81），这是同一件事的另一面：

| 扫描比 | 含义 |
|---|---|
| **> 2** | 空闲页扫描器要翻很远才找到落脚点 —— "**空位难找**" |
| **≈ 1** | 两个扫描器扫描量接近，大致对称相遇 |
| **< 0.5** | 迁移页扫描器要翻很远才找到搬得动的页 —— "**搬得动的页难找**"，UNMOVABLE 占比高 |

后者才是 UNMOVABLE 污染的指纹。第三轮压到极限时，内存里剩下的几乎全是
内核栈、slab 和已分配的大页 —— 没有可迁移的页，规整自然全败。

---

## 四个档位的原理

| 档 | 文件 | 干什么 | 内核里发生了什么 |
|---|---|---|---|
| 1 | `holes.c` | mmap 一大片 → 逐页触碰 → **随机序** `MADV_DONTNEED` 一半 | 造出"空闲页总量够但零散"的外部碎片 |
| 2 | `kstack.c` | 创建海量线程并让它们**永久睡着** | ★ 灵魂档。用**内核对象**钉死 pageblock，决定规整会不会真的失败 |
| 3 | `hugetlb.sh` / `run.sh` 第 3 步 | 分批写 `/proc/sys/vm/nr_hugepages` | 制造 order-9 高阶分配需求，逼进慢路径 |
| 4 | （未实现，见 TODO） | stress-ng 压水位 | 逼进 direct reclaim |

### 为什么 `holes.c` 的"随机"是全部灵魂

- **顺序释放**：还回去的页彼此相邻，伙伴系统的 `__free_one_page()` 会一路向上
  合并，order-0 合成 order-1、order-1 合成 order-2…… 最后又变回大块，**白干**
- **随机释放**：还回去的页的伙伴（buddy）大概率还被占着，合并在第一步就断了，
  空闲页只能以小块挂在低 order 链表上

随机种子固定为 `20260809`。**可复现性是一切性能工作的前提** ——
同样的参数必须挖出同样位置的洞。

### 为什么用 `MADV_DONTNEED` 而不是 `munmap`

两者都能把物理页还给伙伴系统，但：

- `munmap` 挖一个洞就把 VMA 切开一次，洞多了会撞
  `/proc/sys/vm/max_map_count`（默认 65530）直接 ENOMEM
- `MADV_DONTNEED` 只做 `zap_page_range()`：解页表映射、把物理页还回去，
  **VMA 一个都不动**，全程只有 1 个 VMA

代价：再次访问会读到全 0（重新缺页给零页）。对压力工具无所谓。

### ★ 档位 2 与计划书不符的一点：本机内核栈不是 order-2 连续块

```
$ grep -E "^CONFIG_(VMAP_STACK|HAVE_ARCH_VMAP_STACK)" /boot/config-$(uname -r)
CONFIG_HAVE_ARCH_VMAP_STACK=y
CONFIG_VMAP_STACK=y
```

开了 `VMAP_STACK` 后内核栈走 **vmalloc**：16KB 的栈 = **4 个互不相邻的
order-0 页**，靠页表映射成连续虚拟地址，**物理上完全不连续**。

实测证据（`./kstack 3000`）：

| 指标 | 增量 | 说明 |
|---|---|---|
| `KernelStack` | +48096 kB | 3000 × **16.03 KB**，每线程 16KB |
| `VmallocUsed` | +48016 kB | ★ 与上一行几乎完全相等 = 内核栈就在 vmalloc 空间里 |
| `SUnreclaim` | +19960 kB | task_struct 等，不可回收 |
| `SReclaimable` | **0** | 一点没动 → 污染全在不可回收侧 |

**这对本项目更有利**：一个 order-2 连续块最多毁掉 1 个 pageblock 的连续性；
4 个散落的 order-0 UNMOVABLE 页可能毁掉 **4 个不同的 pageblock**。
规整能否凑出 order-9（512 个连续页），取决于这 512 页里有没有搬不走的页 ——
**搬不走的页越分散，杀伤力越大**。

12000 线程实测让 Normal zone 的 **Unmovable pageblock 从 287 涨到 393（+106）**。

**衍生约束**：VMAP_STACK 有 per-CPU 栈缓存（`NR_CACHED_STACKS=2`），
线程退出时栈会被缓存复用。所以 `kstack.c` **必须让线程一直活着** ——
反复 create/join 会一直命中缓存，根本不向伙伴系统要新页，等于没压。

---

## `run.sh` 里两个非显而易见的设计

### 1. 上场顺序：kstack → holes → hugetlb

`kstack` **必须先上**。趁内存还宽裕，内核栈才能散布到尽可能多的 pageblock；
等内存被占满了再创建线程，内核栈会挤在仅剩的几个区域，**污染面反而小**。

每一步都轮询上一步日志里的就绪标记（`实际创建` / `随机挖洞完成`），
**不用 `sleep` 猜**。

### 2. ★ 库存自检：先算清楚"不用规整就能白拿多少大页"

这是踩坑踩出来的。第一次自动化实验目标定 2000 个大页，
结果 `compact_stall` 全程 0 —— 一次规整都没发生。查快照发现
**DMA32 那一行在 A/B/C 三个时间点逐字不变**，`holes` 压根没碰它。

原因：**内核给用户匿名页分配内存时优先从最高的 zone 拿（Normal），
只有 Normal 快见底才 fallback 到 DMA32。** `holes` 只要 4 GB，
而当时 `MemFree` 还有 5.9 GB，Normal 从没紧张过，
所以 DMA32 的 705 个 order-10 完整块（≈1410 个大页）一直闲着。

而 hugetlb 池扩容的 GFP 是 `GFP_HIGHUSER_MOVABLE`，**DMA32 在允许的
zonelist 里** —— 于是 2000 个大页全从那儿白拿走了。

当时的白拿库存 ≈ 2087 个，目标 2000 —— **正好够，差 88 个就跨过门槛**。

**光加大 `holes` 治不了本，它天然够不着 DMA32。** 正确做法是先算库存：

```bash
# order-9 块 = 1 个 2MB 大页；order-10 块 = 4MB = 2 个大页
# buddyinfo 第 14、15 列分别是 order-9、order-10 的块数
INVENTORY=$(awk '/^Node/ { n += $14 + 2*$15 } END { print n+0 }' /proc/buddyinfo)
NEED=$(( INVENTORY * 14 / 10 + 200 ))      # 目标至少比库存高 40%
CAP=$(( (MemAvailable_MB - 800) / 2 ))     # 但要留 800MB 余量，别 OOM
```

第一批直接跳到库存的 90%（那一段全是白拿的，逐批爬过去纯属浪费时间），
小步长留给越过库存线之后那一段 —— 规整就发生在那里。

---

## ★ 一个必须避开的陷阱

```bash
echo 1 | sudo tee /proc/sys/vm/compact_memory     # ← 别用它来验证
```

它走的是**手动规整**路径（`compact_node`），会正常打出
`mm_compaction_begin` / `mm_compaction_end` 埋点，看起来一切正常，
但**不增加 `compact_stall`** —— 压根没有进程在等一次分配。

用它验证会得出"链路通了"的错误结论，实际测的是完全另一条路。
**它只能验证"埋点挂没挂上"，不能验证 direct compaction。**

---

## 操作层面的坑（都不是内核问题，但会毁掉实验）

### 坑① `sudo` + 后台 = 进程被 SIGTTIN 停住

```bash
sudo nohup ./run.sh > log 2>&1 &     # 日志 0 字节，ps 里状态是 T (Stopped)
```

`sudo` 要从终端读密码，但它是后台作业；后台作业读控制终端会被内核发
`SIGTTIN` 停住。**日志 0 字节，看起来像"什么都没发生"。**

修法：先 `sudo -v` 在前台把密码输掉，凭据缓存 15 分钟。

### 坑② stdio 全缓冲让就绪标记永远等不到

`run.sh` 靠轮询日志里的标记判断该步是否就绪。实测**白等了 300 秒超时**，
而 `kstack.log` 是 **0 字节** —— 但同时 `KernelStack` 已经涨了 192 MB，
**12000 个线程早就铺好了**，那几行字还躺在 stdio 缓冲区里。

glibc 的默认缓冲策略取决于 stdout 连的是什么：连终端 → **行缓冲**；
连文件/管道 → **全缓冲**（攒够 4KB 才写）。本程序总共才输出几百字节，
被重定向后进程退出前一个字节都不落盘。

修法：`holes.c` / `kstack.c` 开头各加 `setvbuf(stdout, NULL, _IOLBF, 0)`。
命令行等价物是 `stdbuf -oL`。

### 坑③ 终端掉线会杀掉压力源

本机网卡会闪断（`dmesg` 里一堆 `e1000: ens33 NIC Link is Down`），
终端掉线 → SIGHUP → `holes` 和 `kstack` 双双死亡，
**而脚本还在跑，数据静悄悄失真**。

修法：用 `nohup` 后台跑；`run.sh` 结束时用 `kill -0` 检查两个压力源
**是否全程存活**，死了就大声报出来。

### 坑④ 运行中不能改 bash 脚本

bash 是**逐块读取脚本文件**的，改了正在运行的 `.sh` 会让执行错乱。
`.c` 文件可以随时改，但**重编译会报 `Text file busy`**（二进制正被执行）。
两者都要等本轮跑完再动。

---

## 工具自证机制（为什么可以相信它的输出）

踩过一次"工具坏了但报告说自己好着呢"的坑：某一轮 `batches.csv` 里出现了
**物理上不可能的值** —— 单调递增计数器出现 **-447** 的增量、
某个增量恰好等于该计数器的绝对值。而脚本照常打印
`停止原因：达到目标数量 2791`，实际只跑到 1800/2791 —— **纯粹的谎报**。

**机制至今未查清**（曾假设是 bash "按块读 + lseek 回退"与 seq_file 冲突，
但做实验证伪了：制造 `pgfault` 每秒 2.5 万的背景负载，
`while read < /proc/vmstat` 连读 200 次，异常 0 次）。

所以采取"**不依赖机制也能守住正确性**"的四道防线：

| 防线 | 做法 |
|---|---|
| 采样完整性 | 每次采样后校验 10 个计数器**一个不少**，缺了就报错并把该批标记不可信 |
| 符号校验 | 单调计数器**不许出现负增量** |
| 恒等式校验 | 一次 direct compaction 要么成功要么失败，**`stall` 必须等于 `success + fail`** |
| 双路对账 | 全程总量**另外从递增前后的绝对值算一遍**，与逐批累加互相对账，不一致时以前者为准 |

第四轮实测：两套算法给出 `stall` 402 vs 402、成功率 92.0% vs 92.0%、
`pgscan_direct` 4409 vs 4409，扫描数差 334/314873（0.1%，来自批次间隙的活动）。
零可疑行、零缺键。

> **观测工具的底线不是"永不出错"，是"出错时不许伪装成正确"。**

另外解读文字必须跟着数值走：扫描比的说明曾被写死成"远大于 1 说明…"，
结果实测 0.05 也照样打印那句话 —— 那就是工具在骗人。现在按区间分三种解读。

---

## 参数

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `KSTACK_N` | 12000 | 线程数（上限受 `threads-max` / `ulimit -u` 约束，本机 94599 / 47299） |
| `HOLES_MB` | 4000 | `holes` 申请总量 |
| `HOLES_KB` | 64 | 挖洞的块大小 |
| `HOLES_PCT` | 50 | 随机释放的百分比 |
| `HP_TARGET` | 2000 | 大页目标（**会被库存自检自动抬高**） |
| `HP_STEP` | 200 | 每批递增步长 |
| `STALL_GOAL` | 300 | 累计 stall 达到这个数且跨 ≥3 批就提前收工 |

想看"压到极限"那一端（成功率悬崖），把 `STALL_GOAL` 设得很大：

```bash
sudo STALL_GOAL=100000 nohup ./run.sh > /tmp/fragstress_run.log 2>&1 &
```

---

## 产物

```
/tmp/fragstress-<时间戳>/
├── snap_A_基线.txt        四个时间点的完整快照
├── snap_B_kstack后.txt      （buddyinfo / pagetypeinfo / vmstat / meminfo / PSI）
├── snap_C_holes后.txt
├── snap_D_hugetlb后.txt
├── batches.csv            逐批增量 ← 报告里的主表
├── holes.log
└── kstack.log
```

---

## 单独使用某个档位

```bash
./holes 4000 64 50          # 4000MB，64KB 块，随机释放 50%（Ctrl-C 退出）
./kstack 12000 64           # 12000 线程（Ctrl-C 退出）
sudo ./hugetlb.sh 2500 250  # 递增到 2500 个大页，步长 250
sudo ./hugetlb.sh 0         # 归零
make check                  # 看一眼当前碎片状态（不需要 root）
```

注意 `/proc/pagetypeinfo` **需要 root** 才能读。

---

## TODO

- [ ] 档位 2 补齐 `sockflood.c`（海量 socket → slab UNMOVABLE 对象）
      和 `dentry.sh`（海量小文件 → dentry/inode slab，跑完别 `drop_caches`）
- [ ] 档位 3 补 `thpload.c`：`defrag=always` vs `never` 的对照实验
      （本机 THP 默认是 `[madvise]`，普通程序失败了只会异步唤醒 kcompactd）
- [ ] 档位 4 叠 stress-ng 把水位压低，逼进 direct reclaim
- [ ] 查清 `batches.csv` 曾出现非法增量的真正机制
