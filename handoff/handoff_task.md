# 交接任务书（handoff_task）

> **给接手会话**：这份文件说的是**下一步具体干什么**。
> 开工前必须先读 `handoff/com_memory.md`（状态与已定决策）和 `fraginfo_v2.md`（计划书）。
> 你的角色定义在 `交接_执行员.md`。
>
> **最后更新**：2026-08-04

---

## 〇、开工检查清单（照着做，别跳）

```bash
# 1. 拉最新（多机协作，别在旧版本上干活）
cd /home/xxy/wlsp/Linux物理内存碎片检测 && git pull

# 2. 确认机器是不是那台虚拟机。任一不符 → com_memory.md 第二节数据作废，必须重测
uname -r          # 期望 5.15.0-139-generic
nproc             # 期望 4
grep MemTotal /proc/meminfo   # 期望约 12208080 kB

# 3. 确认慢路径计数器还是不是 0（重启过就会归零，跑过压力测试就不是 0）
grep -E "compact_stall|compact_fail|compact_success|pgscan_direct" /proc/vmstat
```

**必读顺序**：
1. `handoff/com_memory.md` ← 状态、已定决策、未决问题
2. `fraginfo_v2.md` ← 计划书，唯一施工依据
3. `fraginfo_v2_record.md` ← 已经做过什么、讲过什么知识点（避免重复讲）
4. `源码/src/bpf/fraginfo.c` + `extfraginfo.c` ← v1 代码风格，新模块要能并进同一套工具

**不要看** `fraginfo_v2_draft1.md`（初稿，有技术错误，已在正式版修正）。

---

## 一、当前进度

```
[✓] 读交接手册 + 计划书 + v1 源码
[✓] 步骤 0：环境事实核对（确认三套内核统计全是 0）
[✓] 步骤 1：compaction 埋点 format 实测核对
        → 字段全对；发现 status 实际 9 个取值，计划书只列 5 个（漏了 PARTIAL_SKIPPED）
[✓] 给用户补讲三块基础（碎片与规整 / eBPF 与埋点 / 项目全貌）
[✓] 步骤 2：写 tools/fragstress/（最短链路版：holes.c + kstack.c + hugetlb.sh
        + run.sh + Makefile + README.md）。sockflood/dentry/thpload 待补，优先级低于 P0
[✓] 步骤 3：验收硬门槛 **通过** —— 402 次 direct compaction，跨 3 批持续增长
        详见 fraginfo_v2_record.md 步骤 6 与 fragstress/README.md
[ ] 步骤 4：写 src/bpf/compactinfo.c      ← ★ 你从这里开始
[ ] 步骤 5：extfrag.py 加分支加载
[ ] 步骤 6：交叉验证（vmstat 对账 + PSI 比对）
[ ] 步骤 7：出 报告_P0.md → 用户拿去给评审员，通过才进阶段二
```

**阶段划分**：阶段一 = P-1 + P0（本次）；阶段二 = P1；P2 创新点先不做。

---

## 二、~~★ 第一件事：把两个悬而未决的确认问掉~~ 已全部解决（2026-08-11）

- **（1）阶段一步骤顺序** → 用户 2026-08-09 拍板"先打通最短链路"，已执行完毕，硬门槛通过
- **（2）`extfrag.py` 命令行分支** → 用户 2026-08-11 拍板"加入 mode 参数（简易版）"，
  见 `com_memory.md` 第四节决策 13

下面这一节保留原文仅作历史记录，不要再拿去问用户。

<details><summary>原文</summary>

用户上一轮被基础概念绕住了，**这两个确认还没回复**，不确认就开工可能白干：

**（1）阶段一步骤顺序是否照第一节那个走？**

**（2）`extfrag.py` 怎么加命令行分支？**
现状：`extfrag.py` 是纯类库（只有 `ExtFrag` 类，无 main、无 argparse），
CLI 和展示都在 `extfrag_user.py` 的 curses TUI 里。
但交接手册要求"不新开 Python 文件"、"展示层能打印出来就够，不做 TUI"。

上一个会话的建议：给 `ExtFrag` 加 `mode` 参数（`frag`/`extfrag`/`compact`/`reclaim`）
决定加载哪个 `.c`，并在 `extfrag.py` 末尾加 `if __name__ == "__main__":` 的简易
argparse 入口，直接文本打印。不新增文件、不碰 `extfrag_user.py`。

> **问的时候用大白话**，别把这两个问题连同一堆术语一起抛出去。参考话术：
> "下一步我要写一个'把机器折腾到内存变碎'的程序。写之前确认两件事：①…… ②……"

</details>

---

## 三、步骤 2：写 `tools/fragstress/`（P-1 压力注入器）—— ✅ 已完成

**当前状态**：硬门槛通过（402 次 direct compaction）。完整结果与全部踩坑记录见
`源码/src/tools/fragstress/README.md` 与 `fraginfo_v2_record.md` 步骤 2~6。
下面的原始设计说明保留备查。

### 3.1 这个模块要解决什么问题（讲给用户听的版本）

用户这台机器**太干净**：`compact_stall`、`pgscan_direct`、PSI 全是 0，
说明从开机到现在，**没有任何一个进程因为内存问题被卡住过**。

在这种机器上，P0 的观测代码写得再完美，跑起来也是空表——什么都测不到，
连"代码对不对"都无法验证。

**所以必须先造压力。** 而且这个模块本身就是可交付物：
"我写了一个能稳定复现 direct compaction stall 的压力注入器" ——
**可复现性是一切性能工作的前提**，这句话在面试里比多两个观测指标有用。

### 3.2 目录与文件（路径已定，不要自己发明）

```
源码/src/tools/fragstress/
├── holes.c        # 档位1：mmap 海量小块 → 随机序释放一半，造"梅花桩"
├── kstack.c       # 档位2：海量线程 → 每个线程 order-2 内核栈，UNMOVABLE 污染
├── sockflood.c    # 档位2：海量 socket/fd → slab 里的 UNMOVABLE 对象
├── dentry.sh      # 档位2：海量小文件 → dentry/inode slab（跑完别 drop_caches）
├── thpload.c      # 档位3：大量匿名内存 + 顺序触碰 → order-9 THP 分配需求
├── hugetlb.sh     # 档位3：写 nr_hugepages，直接要连续大页
├── run.sh         # 档位4 + 编排：叠 stress-ng，按档位组合，采集前后快照
├── Makefile
└── README.md      # ★ 必须写：跑哪几条命令能稳定复现 compact_stall 增长
```

**硬性约束**（来自交接手册）：
- 这些是**普通用户态 C 程序，和 eBPF 无关**，绝不能放进 `源码/src/bpf/`
  （那个目录只放 BCC 加载的内核态源码，混进去会让人误以为压力注入器也是 eBPF）

### 3.3 四档的原理（写代码时要给用户讲清）

| 档 | 干什么 | 内核里发生了什么 |
|---|---|---|
| 1 | mmap 海量小块，**随机序**释放一半 | 造出"空闲页总量够但零散"的局面。**释放必须随机**——顺序释放会被伙伴系统直接合并掉，白做 |
| 2 | 海量线程 / socket / 小文件 | ★ **最关键的一档**。用户匿名页都是 MOVABLE 的搬得走，**真正钉死 pageblock 的是内核对象**：线程内核栈是 order-2 UNMOVABLE，dentry/inode/socket 是 slab 对象。这一档决定了**规整会不会真的失败** |
| 3 | THP / hugepages | 制造**高阶分配需求**。order-0 永远不触发规整，必须有人要大块才会进慢路径 |
| 4 | stress-ng 内存压力 | 把水位压低，逼进 direct reclaim |

**档位 2 是整个压力基座的灵魂**：它直接对应 v1 里 fallback 污染分组的理论，
也是 P2 归因分析能成立的前提——没有污染，就没有"谁的锅"可查。

### 3.4 上一个会话的技术判断（供参考，未验证）

**最可能第一个顶起 `compact_stall` 的是 `nr_hugepages` 路线，不是 THP。**
理由：hugetlb 池扩容是同步的高阶分配，直接走慢路径；
而 THP 在本机 `defrag=[madvise]` 下只对 `MADV_HUGEPAGE` 区域同步规整，
要改成 `defrag=always` 才稳。

建议：**hugetlb 当"打通链路的第一发子弹"，THP 当"能出对照实验数据的主力载体"**
（`defrag=always` vs `never` 的对照组是创新点 B 的白送数据）。

### 3.5 一个必须避开的陷阱（计划书 §2.2）

```bash
echo 1 | sudo tee /proc/sys/vm/compact_memory   # ← 有坑
```
它走的是**手动/kcompactd 规整路径**，会打 `mm_compaction_begin/end`，
但**不增加 `compact_stall`**。你会看到工具有事件输出、以为验证通过了，
其实测的是另一条路径。**它只能验证"埋点通不通"，不能验证 direct compaction。**

### 3.6 需要用户跑的命令（AI 执行不了 sudo）

改内核参数、跑压力、看结果全都要用户在终端执行。**写成完整可复制的命令块**，
并明确告诉用户"跑完把哪几段输出贴回来"。至少要采集：

```bash
# 压力前快照
grep -E "compact_|allocstall|pgscan_direct|pgsteal_direct|thp_fault" /proc/vmstat > /tmp/vmstat.before
cat /proc/pagetypeinfo > /tmp/pagetype.before
cat /proc/pressure/memory > /tmp/psi.before

# …跑压力…

# 压力后快照（同样三份，改成 .after）
```

---

## 四、步骤 3：验收（硬门槛，过不了不准写 P0）

```bash
watch -n1 'grep -E "compact_stall|compact_fail|compact_success|allocstall|pgsteal_direct|thp_fault_fallback" /proc/vmstat'
```

**验收标准：`compact_stall` 必须能被顶上去并持续增长**（不是涨一次就停）。

**过不了怎么办**：停下来告诉用户，一起调压力策略。
**不准硬着头皮往下写 P0 的观测代码**——这是交接手册的明确要求。

把"跑哪几条命令能稳定复现"写进 `fragstress/README.md`，这是后面所有实验的地基。

---

## 五、步骤 4~6：`compactinfo.c`（P0）要点速查

细节看计划书 §3，这里只列**必须做对、做错就白干**的几条
（每条的实测依据都在 `com_memory.md` 第三节）：

1. **map key 用 tid**（`bpf_get_current_pid_tgid()` 完整 u64），不是 `>>32` 的 tgid
2. **三层埋点**：外层 `try_to_compact_pages`（唯一带 `order`）+ 内层 per-zone
   `begin`/`end` + `migratepages`（`nr_migrated`/`nr_failed` 要**累加**）
3. **三重来源过滤，在内核态做**：以"当前 tid 有没有活跃的外层记录"作为准入条件，
   过滤掉 kcompactd 和手动 `compact_memory`
4. **`status` 分类要覆盖全部 9 个值**（计划书那张 5 行表不全，漏了 `PARTIAL_SKIPPED=6`）
5. **`BPF_LRU_HASH` + `unpaired` 计数器**，输出必须报未配对率
6. **延迟直方图 log2 分桶**，按 `order` / `sync` 分维度（不要用平均值，长尾会被抹平）
7. **限流固定 `key=0`**（v1 已修正的写法，别再犯）
8. **永远用 `args->字段名`**，不要自己算 offset（`end` 的 `sync`(40) 和 `status`(44)
   之间有 3 字节对齐 padding）
9. 交叉验证：能和 `compact_stall` 对上的是**外层次数**，不是内层 `begin` 次数；
   `compact_daemon_wake` 应该完全不出现在统计里（这条能直接证明过滤做对了）

---

## 六、步骤 7：阶段报告 `报告_P0.md`（放项目根目录）

评审员靠这份报告判断做得对不对，**要有可核对的原始数据，不要只有结论**：

1. **交付物清单** —— 新增/修改了哪些文件、各多少行、怎么跑（完整命令行）
2. **埋点实测确认** —— `format` 的实际字段输出；和计划书不一致的地方要指出并说明怎么改的
   （★ `status` 9 个取值这条必须写进去）
3. **压力注入效果** —— 施压前后 `/proc/vmstat` 关键计数器对照表
   + `/proc/pagetypeinfo` 的 Unmovable 行变化，证明 §2.4 验收标准过了
4. **交叉验证表** —— 工具统计值 vs `/proc/vmstat` vs PSI，三列并排，
   **偏差要给出解释**；对不上就如实写对不上
5. **未配对率** —— 实测多少，是否在可接受范围
6. **一段内核原理复述** —— 用自己的话讲双扫描器和迁移失败的原因。
   **这段是给用户面试用的，不要抄计划书原文**
7. **偏离与遗留** —— 哪里没按计划书做、为什么；哪些没解决；下一阶段的风险

> **报告里不要写"完美实现""效果良好"这类话。跑不通就写跑不通，
> 数据对不上就写对不上——评审员要的是真实状态。**

---

## 七、贯穿始终的纪律

1. **每个阶段开工前，先把该阶段的施工步骤列给用户确认，再动手**
2. **遇到计划书没覆盖、或和实际内核对不上的情况，停下来问用户**，不要自己改方案往下冲
3. **代码注释和输出全部用中文**，和 v1 保持一致
4. **每写一段内核态代码，先讲清它对应内核里的哪个行为、为什么这么挂、备选方案是什么**
   —— 用户要的是能在面试里复述的理解，不是一堆能跑的代码
5. **用大白话**，见 `com_memory.md` 第〇节（用户明确说过"我听不懂你在说什么"）
6. **每一步操作、用户的每个提问、你讲的每个知识点，都要追加进 `fraginfo_v2_record.md`**
   —— 这是用户明确要求的
7. **收工必须 `git push`**，并在 `com_memory.md` 第八节追加一行更新日志
