# Linux 物理内存碎片检测与慢路径代价归因（eBPF / BCC）

用 eBPF 观测 Linux 内核内存分配的**慢路径代价**：碎片把分配从快路径逼进慢路径后，
进程被 **direct compaction（直接规整）** 和 **direct reclaim（直接回收）**
同步阻塞了多久、成功与否、以及**这笔账该算在谁头上**。

- **v1（已完成）**：内存有多碎（碎片指数） + 谁在制造碎片（跨类型 fallback 归因）
- **v2（进行中）**：碎片让**谁**付出了**什么**代价

技术栈：**BCC**（Python + 内嵌 C 的 eBPF）。开发环境内核 5.15.0-139-generic / x86_64。

---

## 仓库导航

| 路径 | 说明 |
|---|---|
| `handoff/com_memory.md` | **跨会话/跨机器的共享记忆**：已确认事实、已定决策、未决问题 |
| `handoff/handoff_task.md` | **交接任务书**：下一步具体干什么 |
| `fraginfo_v2.md` | v2 计划书（唯一施工依据） |
| `fraginfo_v2_record.md` | 施工记录 + 内核知识点笔记（逐步追加） |
| `fraginfo_v2_draft1.md` | 计划书初稿存档（含已修正的技术错误，仅供回溯） |
| `源码/src/bpf/` | eBPF 内核态源码（BCC 加载） |
| `源码/src/extfrag.py` | Python 侧类库：加载 eBPF、读 map |
| `源码/src/extfrag_user.py` | curses 展示层（v1） |
| `源码/src/tools/fragstress/` | 碎片压力注入器（纯用户态 C，与 eBPF 无关） |

## 模块分工

| 模块 | 挂载点 | 回答的问题 | 状态 |
|---|---|---|---|
| `bpf/fraginfo.c` | kprobe `get_page_from_freelist` | 现在有多碎？ | ✅ v1 |
| `bpf/extfraginfo.c` | tracepoint `kmem:mm_page_alloc_extfrag` | 谁在制造碎片？ | ✅ v1 |
| `tools/fragstress/` | 用户态压力注入 | 能不能可控地造出碎片？ | 🚧 P-1 |
| `bpf/compactinfo.c` | tracepoint `compaction:*` | 规整代价多大？成功率多少？ | ☐ P0 |
| `bpf/reclaiminfo.c` | tracepoint `vmscan:*` | 回收代价多大？ | ☐ P1 |
| 归因 | 复用上述数据 | 谁造的碎片？谁付的账？ | ☐ P2 |

## 运行

需要 root（挂载 eBPF、读 tracefs）：

```bash
cd 源码/src
sudo python3 extfrag_user.py        # v1 展示层
```

## 开发约定

- 每次开工先 `git pull`，收工必 `git commit && git push` —— 仓库是多机之间唯一的同步渠道
- `handoff/com_memory.md` 和 `fraginfo_v2_record.md` 有实质进展就更新并提交
- 代码注释与输出统一用中文
