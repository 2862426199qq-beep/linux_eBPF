#!/usr/bin/env python3
"""
Linux 物理内存碎片检测工具 - ExtFrag 类
========================================
功能：通过 eBPF (BPF Compiler Collection) 采集内核内存碎片数据，
      计算碎片指数（extfrag_index / unusable_index），并输出统计结果。

依赖：
  - BCC (BPF Compiler Collection)：用于加载 eBPF 程序，读取内核数据
  - eBPF 程序：fraginfo.c（碎片信息采集）/ extfraginfo.c（含分配计数采集）

主要指标：
  - extfrag_index（外部碎片指数）：衡量某个 order 下空闲块中可用大块的比例，
    值越高表示碎片化越严重（范围 0~1，以 "xx.xxx" 格式展示为 0~1000）
  - unusable_index（不可用指数）：反映大块连续内存分配失败的程度
  - free_blocks_total / free_blocks_suitable：各 order 的空闲块总数与可用块数
  - fallback_order：当目标 order 分配失败时，向更高 order 借块的记录
"""

try:
    from bpfcc import BPF          # BCC 新版 Python 包名
except ImportError:
    from bcc import BPF            # BCC 旧版 Python 包名（向后兼容）
import os
import time
import ctypes


class ExtFrag:
    """Linux 物理内存碎片分析类

    通过 BCC 框架加载 eBPF 内核探针，周期性采集各 NUMA 节点、各 Zone
    的内存碎片信息，提供碎片指数计算和数据查询接口。

    典型用法:
        extfrag = ExtFrag(interval=2, output_count=True)
        zone_data = extfrag.get_zone_data()
        count_data = extfrag.get_count_data()
    """

    # ------------------------------------------------------------------
    # mode → eBPF 源文件的映射表
    #
    # 为什么用 mode 而不是继续加 output_xxx 布尔参数：
    #   BCC 是**加载时现场编译**的，一次 BPF() 只能加载一个 .c。
    #   模块越多，布尔参数的组合就越容易出现"两个都为 True"这种无意义状态。
    #   mode 是互斥的单选，天然表达了"一次只挂一套探针"这个事实。
    #
    # 为什么 compaction 和 reclaim 不合并成一个 .c（决策 #1）：
    #   合并 = 只想看 reclaim 的人也得挂上 compaction 的埋点，白付开销；
    #   而且分开之后编译报错能直接定位到是哪套探针的问题。
    # ------------------------------------------------------------------
    MODE_SRC = {
        'frag':    'fraginfo.c',       # v1 主线：现在有多碎（kprobe 全量扫 zone×order）
        'extfrag': 'extfraginfo.c',    # v1 支线：谁在制造碎片（跨 migratetype fallback）
        'compact': 'compactinfo.c',    # v2 P0：规整代价多大、成功率多少
        'reclaim': 'reclaiminfo.c',    # v2 P1：直接回收代价多大（尚未实现）
    }

    def __init__(self, interval=2, output_extfrag_index=False,
                 output_unusable_index=False, output_count=False,
                 zone_info=False, mode=None):
        """初始化 ExtFrag 实例

        Args:
            interval:             数据采集间隔（秒），通过 delay_map 传给 eBPF 程序
            output_extfrag_index: 是否输出外部碎片指数（extfrag_index）
            output_unusable_index:是否输出不可用内存指数
            output_count:         v1 的老开关，保留以兼容 extfrag_user.py。
                                  为 True 等价于 mode='extfrag'，为 False 等价于 mode='frag'
            zone_info:            是否输出详细的 Zone 信息
            mode:                 ★ 挂哪一套探针，见 MODE_SRC。
                                  显式给了 mode 就以 mode 为准；不给则回退到
                                  output_count 的老语义，保证 v1 调用方一行都不用改
        """
        self.interval = interval
        self.output_extfrag_index = output_extfrag_index
        self.output_unusable_index = output_unusable_index
        self.output_count = output_count
        self.zone_info = zone_info

        # mode 未显式指定时，从 v1 的 output_count 推导，保持向后兼容
        if mode is None:
            mode = 'extfrag' if output_count else 'frag'
        if mode not in self.MODE_SRC:
            raise ValueError(
                f"未知 mode={mode!r}，可选：{', '.join(self.MODE_SRC)}")
        self.mode = mode

        # ★ 源文件路径按本文件位置解析，而不是依赖当前工作目录。
        #   原来写的是相对路径 "./bpf/xxx.c"，从别的目录跑就会找不到文件。
        src = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           'bpf', self.MODE_SRC[mode])
        if not os.path.exists(src):
            raise FileNotFoundError(f"mode={mode} 需要的源文件不存在：{src}")
        self.b = BPF(src_file=src)

        # 将采集间隔写入 eBPF 侧的 delay_map
        #
        # ★ 注意语义差异：
        #   v1（frag/extfrag）用它做**内核态采样限流** —— 那两个模块挂在
        #   每次内存分配都会走的路径上，不限流会拖死机器。
        #   v2（compact）**不做内核态限流**，delay_map 只当**用户态打印间隔**用：
        #   一整轮压力实验里 direct compaction 只发生几百次，限流没必要；
        #   而限流会静默丢掉停留最久的样本 —— 那恰恰是长尾，是性能问题的本体。
        delay_key = 0
        self.b["delay_map"][delay_key] = ctypes.c_int(interval)

    def calculate_scoreA(self, extfrag_index):
        """计算并格式化外部碎片指数（extfrag_index）

        eBPF 程序返回的 extfrag_index 是一个整数值（实际值 × 1000），
        此处将其还原为 "xx.xxx" 格式的字符串，便于展示。

        计算公式：
            extfrag_index = 1 - (free_blocks_suitable / free_blocks_total)
            值域 [0, 1]，越接近 1 表示碎片化越严重

        Args:
            extfrag_index: eBPF 侧传来的整数值（实际碎片指数 × 1000）

        Returns:
            str: 格式化后的字符串，如 " 0.850"（整数部分 2 位右对齐，
                 小数部分 3 位补零）
        """
        extfrag_index_int_part = int(extfrag_index) // 1000   # 整数部分
        extfrag_index_dec_part = int(extfrag_index) % 1000    # 小数部分
        return f"{extfrag_index_int_part:2d}.{extfrag_index_dec_part:03d}"

    def calculate_scoreB(self, unusable_index):
        """计算并格式化不可用内存指数（unusable_index）

        unusable_index 衡量要满足某个 order 的分配请求时，
        需要向更高 order 借用空闲块的程度。值越高说明该 order
        下的可用连续内存越不足。

        Args:
            unusable_index: eBPF 侧传来的整数值（实际指数 × 1000）

        Returns:
            str: 格式化后的字符串，如 " 0.500"
        """
        unusable_index_int_part = int(unusable_index) // 1000
        unusable_index_dec_part = int(unusable_index) % 1000
        return f"{unusable_index_int_part:2d}.{unusable_index_dec_part:03d}"

    def get_zone_data(self, filter_node_id=None):
        """获取所有 Zone 的详细碎片数据

        从 eBPF 的 zone_map（BPF_HASH）中读取每个 Zone 的内存统计，
        并按 Zone 名称（如 "Normal", "DMA32"）分组，按 order 排序。

        zone_map 中每条记录包含：
          - name:              Zone 名称（Normal / DMA32 / DMA / Movable 等）
          - node_id:           NUMA 节点 ID
          - zone_start_pfn:    Zone 起始页帧号
          - spanned_pages:     Zone 跨越的总页数
          - present_pages:     Zone 中实际存在的页数
          - order:             内存 order（0 ~ MAX_ORDER-1），代表 2^order 个连续页
          - free_blocks_total: 该 order 下空闲块总数
          - free_blocks_suitable: 该 order 下能满足大块分配的可用块数
          - free_pages:        空闲页总数
          - score_a:           extfrag_index（外部碎片指数 × 1000）
          - score_b:           unusable_index（不可用指数 × 1000）

        Args:
            filter_node_id: 若不为 None，则只返回指定 NUMA 节点的数据

        Returns:
            dict: {zone_name: [data_dict_list]}，每个 Zone 名称对应一个列表，
                  列表中的每个元素是一条 order 数据，已按 order 升序排列
        """
        zone_data_dict = {}
        zone_map = self.b["zone_map"]          # 获取 eBPF 哈希表 zone_map

        for key, value in zone_map.items():
            # 解码 Zone 名称（内核中为 char 数组，需去除尾部的 \x00 填充）
            comm = value.name.decode('utf-8', 'replace').rstrip('\x00')
            node_id = value.node_id

            # 如果指定了过滤节点，跳过不匹配的记录
            if filter_node_id is not None and node_id != filter_node_id:
                continue

            # 组装一条完整的 Zone 数据记录
            data = {
                'comm': comm,
                'zone_pfn': value.zone_start_pfn,
                'spanned_pages': value.spanned_pages,
                'present_pages': value.present_pages,
                'order': value.order,                              # order 级别
                'free_blocks_total': value.free_blocks_total,      # 该 order 空闲块总数
                'free_blocks_suitable': value.free_blocks_suitable,# 可用大块数
                'free_pages': value.free_pages,                    # 空闲页数
                'scoreA': self.calculate_scoreA(value.score_a),    # 格式化外部碎片指数
                'scoreB': self.calculate_scoreB(value.score_b),    # 格式化不可用指数
                'node_id': value.node_id
            }

            # 按 Zone 名称分组
            if comm not in zone_data_dict:
                zone_data_dict[comm] = []
            zone_data_dict[comm].append(data)

            # 对每个 Zone 内的数据按 order 升序排列（order 0 最小块 → order 10 最大块）
            for comm in zone_data_dict:
                zone_data_dict[comm].sort(key=lambda x: x['order'])

        return zone_data_dict

    def get_view_data(self, filter_node_id=None):
        """获取视图展示所需的精简数据

        与 get_zone_data() 不同，该方法仅提取 scoreB（不可用指数）
        和 order 两个字段，并以 (node_id, comm) 元组为键组织数据，
        适合用于终端表格或热力图展示。

        Args:
            filter_node_id: 若不为 None，则只返回指定 NUMA 节点的数据

        Returns:
            dict: {(node_id, zone_name): {'scoreB': str, 'order': int}}
                  按键（node_id, zone_name）排序
        """
        zone_data_dict = {}
        ret_dict = {}
        zone_map = self.b["zone_map"]

        for key, value in zone_map.items():
            comm = value.name.decode('utf-8', 'replace').rstrip('\x00')
            node_id = value.node_id

            if filter_node_id is not None and node_id != filter_node_id:
                continue

            # 仅保留不可用指数和 order，适合简洁展示
            data = {
                'scoreB': self.calculate_scoreB(value.score_b),
                'order': value.order,
            }

            # 以 (node_id, Zone名称) 作为复合键
            zone_data_dict[(node_id, comm)] = data

        # 按复合键排序后返回
        sorted_keys = sorted(zone_data_dict.keys())
        for key in sorted_keys:
            ret_dict[key] = zone_data_dict[key]
        return ret_dict

    def get_nr_zones(self, filter_node_id=None):
        """获取每个 NUMA 节点下的 Zone 列表

        遍历 zone_map，统计每个 node_id 下有哪些 Zone 类型，
        用于确定每个 NUMA 节点的 Zone 数量（nr_zones）。

        注意：nr_zones 在 get_node_data() 中会除以 11（MAX_ORDER），
        因为每个 Zone 在 zone_map 中按 order 展开存储了多条记录。

        Args:
            filter_node_id: 若不为 None，则只返回指定 NUMA 节点的数据

        Returns:
            dict: {node_id: [zone_name_list]}
                  如 {0: ['DMA', 'DMA32', 'Normal'], 1: ['Normal']}
        """
        node_zone_map = {}
        zone_map = self.b["zone_map"]

        for key, value in zone_map.items():
            comm = value.name.decode('utf-8', 'replace').rstrip('\x00')
            node_id = value.node_id

            if filter_node_id is not None and node_id != filter_node_id:
                continue

            data = {
                'scoreB': self.calculate_scoreB(value.score_b),
                'order': value.order,
            }

            # 按 NUMA 节点 ID 分组，收集该节点下的所有 Zone 名称
            if node_id not in node_zone_map:
                node_zone_map[node_id] = []      # 初始化空列表
            node_zone_map[node_id].append(comm)   # 追加 Zone 名称

        return node_zone_map

    def get_node_data(self):
        """获取 NUMA 节点级别的元数据

        从 pgdat_map（eBPF 中存储 pg_data_t 结构体信息的哈希表）
        读取每个 NUMA 节点的 pgdat 指针和 node_id，并结合
        get_nr_zones() 计算该节点的 Zone 数量。

        nr_zones 计算方式：
            zone_map 中每个 Zone 按 order (0~10) 存储 11 条记录，
            所以 nr_zones = 该节点的 zone_map 条目数 / 11

        Returns:
            dict: {node_id: {'pgdat_ptr': int, 'nr_zones': int, 'node_id': int}}
        """
        node_data_dict = {}
        pgdat_map = self.b["pgdat_map"]            # pgdat 结构体的 eBPF 哈希表
        zone_data = self.get_nr_zones()            # 获取每个节点的 Zone 列表

        for key, value in pgdat_map.items():
            node_id = value.node_id
            # 每个 Zone 在 zone_map 中有 MAX_ORDER(11) 条记录（order 0~10）
            # 除以 11 即得实际的 Zone 数量
            nr_zones = int(len(zone_data.get(node_id, [])) / 11)

            data = {
                'pgdat_ptr': value.pgdat_ptr,      # 内核 pg_data_t 指针地址
                'nr_zones': nr_zones,              # 该节点下的 Zone 数量
                'node_id': value.node_id
            }
            node_data_dict[node_id] = data

        return node_data_dict

    def get_count_data(self):
        """获取进程级内存分配 fallback 计数

        仅在 output_count=True（加载 extfraginfo.c）时有数据。
        eBPF 程序通过 kprobe 挂载内核内存分配路径（如
        __rmqueue_fallback），记录每次因目标 order 空闲块不足
        而向更高 order 借用（fallback）的事件。

        counts_map 中每条记录包含：
          - pcomm:         触发 fallback 的进程名（comm）
          - pid:           进程 PID
          - pfn:           分配的页帧号（PFN）
          - alloc_order:   请求的原始 order
          - fallback_order:实际分配使用的 order（高于 alloc_order）
          - count:         fallback 事件发生的累计次数

        Returns:
            list: 按 count 降序排列的 fallback 事件列表，
                  次数最高的排在前面，便于定位碎片化的主要"受害者"
        """
        count_data_list = []
        counts_map = self.b["counts_map"]   # eBPF 侧的 fallback 计数哈希表

        # 遍历 counts_map，提取所有 fallback 事件记录
        for key, value in counts_map.items():
            _comm = value.pcomm.decode('utf-8', 'replace').rstrip('\x00')
            data = {
                'pcomm': _comm,                    # 进程名
                'pid': value.pid,                  # 进程 ID
                'pfn': value.pfn,                  # 分配的页帧号
                'alloc_order': value.alloc_order,   # 请求的 order
                'fallback_order': value.fallback_order,  # 实际降级使用的 order
                'count': value.count               # fallback 发生次数
            }
            count_data_list.append(data)

        # 按 count 降序排序：次数最高的进程排在最前，
        # 便于快速识别受碎片化影响最严重的进程
        count_data_list.sort(key=lambda x: x['count'], reverse=True)

        return count_data_list

    # ======================================================================
    # v2 P0：direct compaction 的代价与结局（mode='compact'）
    # ======================================================================

    # enum compact_result 的 9 个取值。
    # ★ 计划书只列了 5 个，漏了 PARTIAL_SKIPPED(6)；这里 0~8 全保留，
    #   越界值落到 UNKNOWN 桶，一样上报，不做"某些值不会出现"的假设。
    STATUS_NAMES = [
        'NOT_SUITABLE_ZONE',   # 0 内部值
        'SKIPPED',             # 1 没启动：没可能，或直接回收更合适
        'DEFERRED',            # 2 因过去连续失败被主动推迟（退避）
        'NO_SUITABLE_PAGE',    # 3 内部值
        'CONTINUE',            # 4 内部值，应继续扫下一个 pageblock
        'COMPLETE',            # 5 整个 zone 扫完仍没成功（最坏：白扫）
        'PARTIAL_SKIPPED',     # 6 扫了一部分就退避  ← 计划书漏的那个
        'CONTENDED',           # 7 锁竞争，提前终止
        'SUCCESS',             # 8 判定分配现在能成功了
    ]
    STATUS_SLOTS = 16          # 与 compactinfo.c 的 STATUS_SLOTS 保持一致

    # stat_map 的下标，必须与 compactinfo.c 里的 S_* 宏逐一对应
    STAT_NAMES = [
        'outer_enter',      # 0 外层进入次数 ← 能和 /proc/vmstat 的 compact_stall 对账
        'outer_exit',       # 1 外层退出次数（kretprobe 命中）
        'outer_unpaired',   # 2 ★ kretprobe 找不到入口记录 = 未配对
        'begin_accept',     # 3 内层 begin 被接纳
        'begin_reject',     # 4 ★ 内层 begin 被过滤（kcompactd / 手动 compact_memory）
        'end_accept',       # 5
        'end_unpaired',     # 6 ★ end 找不到对应 begin = 未配对
        'mig_accept',       # 7
        'mig_reject',       # 8
        'no_direct_reclaim',  # 9 gfp 里没有 __GFP_DIRECT_RECLAIM（预期恒为 0）
    ]

    def _hist_to_dict(self, table, dim_field):
        """把带二维 key 的 BPF_HISTOGRAM 读成 {维度值: {log2桶: 计数}}

        BCC 的直方图 key 是个结构体，这里第一维是 order 或 sync，
        第二维 slot 是 bpf_log2l 的结果。
        slot=n 表示落在 [2^(n-1), 2^n) 微秒这个区间。
        """
        out = {}
        for key, value in table.items():
            dim = getattr(key, dim_field)
            out.setdefault(dim, {})[key.slot] = value.value
        return out

    def get_compact_data(self):
        """获取 direct compaction 的全部统计（仅 mode='compact' 有数据）

        Returns:
            dict，包含：
              stat            各类计数（含★未配对数、★被过滤数）
              attempt_status  外层最终结局分布 {状态名: 次数}
              zone_status     内层 per-zone 结局分布 {状态名: 次数}
              migrated/failed 累计搬走 / 搬失败的页数
              attempt_lat     每次尝试的总延迟直方图 {order: {slot: 次数}}
              zone_lat        每个 zone 的规整延迟直方图 {sync: {slot: 次数}}
              unpaired_rate   ★ 未配对率，延迟统计可信度的唯一凭据
        """
        if self.mode != 'compact':
            raise RuntimeError(f"get_compact_data() 需要 mode='compact'，当前是 {self.mode!r}")

        stat = {}
        for i, name in enumerate(self.STAT_NAMES):
            stat[name] = self.b["stat_map"][ctypes.c_int(i)].value

        def read_status(tbl):
            d = {}
            for i in range(self.STATUS_SLOTS):
                n = self.b[tbl][ctypes.c_int(i)].value
                if n == 0:
                    continue
                name = (self.STATUS_NAMES[i] if i < len(self.STATUS_NAMES)
                        else f'UNKNOWN({i})')
                d[name] = n
            return d

        # ★ 未配对率：进去了但没能配上出口的比例。
        #   lru_hash 满了会淘汰**停留最久**的表项，也就是延迟最长的样本。
        #   所以这个数不报出来，延迟分布就是不可信的 —— 这是硬要求，不是可选项。
        enter = stat['outer_enter']
        unpaired = stat['outer_unpaired'] + stat['end_unpaired']
        return {
            'stat': stat,
            'attempt_status': read_status("attempt_status"),
            'zone_status': read_status("zone_status"),
            'migrated': self.b["mig_map"][ctypes.c_int(0)].value,
            'failed':   self.b["mig_map"][ctypes.c_int(1)].value,
            'attempt_lat': self._hist_to_dict(self.b["attempt_lat"], 'order'),
            'zone_lat':    self._hist_to_dict(self.b["zone_lat"], 'sync'),
            'unpaired_rate': (unpaired / enter) if enter else 0.0,
        }

    @staticmethod
    def _fmt_hist(buckets, label):
        """把 {log2桶: 计数} 渲染成一行行文本直方图"""
        if not buckets:
            return ["    （无样本）"]
        lines = []
        peak = max(buckets.values())
        for slot in sorted(buckets):
            lo = 0 if slot == 0 else (1 << (slot - 1))
            hi = (1 << slot) - 1
            n = buckets[slot]
            bar = '*' * max(1, int(40 * n / peak))
            lines.append(f"    {lo:>8} ~ {hi:<8} μs | {n:>7} |{bar}")
        return lines

    def print_compact(self):
        """把 compaction 统计打印成纯文本

        交接手册明确要求：展示层能正确打印就够，不做 TUI / 曲线 / 颜色。
        """
        d = self.get_compact_data()
        s = d['stat']

        print("=" * 72)
        print("direct compaction 统计（只统计被同步卡住的进程，已排除 kcompactd 与手动规整）")
        print("=" * 72)

        print(f"外层进入 try_to_compact_pages : {s['outer_enter']}"
              "   ← 拿这个和 /proc/vmstat 的 compact_stall 对账")
        print(f"外层退出（kretprobe）         : {s['outer_exit']}")
        print(f"内层 begin 接纳 / 被过滤      : {s['begin_accept']} / {s['begin_reject']}")
        print(f"内层 end   接纳               : {s['end_accept']}")
        print(f"migratepages 接纳 / 被过滤    : {s['mig_accept']} / {s['mig_reject']}")
        print()
        print(f"★ 未配对率                    : {d['unpaired_rate'] * 100:.2f}%"
              f"   (外层 {s['outer_unpaired']} + 内层 {s['end_unpaired']})")
        if s['begin_reject'] or s['mig_reject']:
            print("  ✓ 过滤器确实在工作（有事件被挡掉了），不是'恰好没有噪声'")
        else:
            print("  ！被过滤数为 0：要么本次真没有 kcompactd/手动规整活动，"
                  "要么过滤逻辑没生效 —— 对照 /proc/vmstat 的 compact_daemon_wake 判断")
        if s['no_direct_reclaim']:
            print(f"  ！{s['no_direct_reclaim']} 次分配的 gfp 里没有 __GFP_DIRECT_RECLAIM，"
                  "与'direct compaction 必然允许阻塞'的理解矛盾，需要查")
        print()

        print("外层最终结局（enum compact_result，来自 try_to_compact_pages 的返回值）：")
        total = sum(d['attempt_status'].values())
        for name, n in sorted(d['attempt_status'].items(), key=lambda x: -x[1]):
            pct = 100 * n / total if total else 0
            print(f"    {name:<20} {n:>7}  ({pct:5.1f}%)")
        if total:
            succ = d['attempt_status'].get('SUCCESS', 0)
            print(f"  规整成功率（内核 compaction_made_progress 口径：只有 SUCCESS 算成功）"
                  f" = {succ}/{total} = {100 * succ / total:.1f}%")
            print("  注：SKIPPED 不是'规整失败'，而是'规整判断自己不该上、该让回收先干'")
        print()

        print("内层 per-zone 结局（mm_compaction_end 的 status）：")
        for name, n in sorted(d['zone_status'].items(), key=lambda x: -x[1]):
            print(f"    {name:<20} {n:>7}")
        print()

        print(f"累计迁移页数：成功 {d['migrated']}  失败 {d['failed']}"
              f"{'  → 迁移失败率 %.1f%%' % (100 * d['failed'] / (d['migrated'] + d['failed'])) if (d['migrated'] + d['failed']) else ''}")
        print()

        # ★ 交叉校验：直方图里的样本总数，应当等于"外层退出数 − 外层未配对数"。
        #   不相等就说明有样本被静默丢掉了（最可能是直方图 map 满了 ——
        #   BCC 的 increment() 失败时不报错、没有返回值可查）。
        #   这条等式是发现"静默丢样本"的唯一手段，必须打出来。
        hist_n = sum(sum(b.values()) for b in d['attempt_lat'].values())
        expect_n = s['outer_exit'] - s['outer_unpaired']
        if hist_n == expect_n:
            print(f"✓ 交叉校验通过：延迟直方图样本数 {hist_n} = 外层退出 {s['outer_exit']}"
                  f" − 未配对 {s['outer_unpaired']}")
        else:
            print(f"！交叉校验失败：直方图样本数 {hist_n} ≠ 期望 {expect_n}"
                  f"（外层退出 {s['outer_exit']} − 未配对 {s['outer_unpaired']}）")
            print("  → 有样本被静默丢掉了，最可能是直方图 map 满。延迟分布不可信，不要写进报告。")
        print()

        print("每次 direct compaction 的总延迟（按 order 分维度；不用平均值，长尾会被抹平）：")
        for order in sorted(d['attempt_lat']):
            print(f"  order = {order}  （{(1 << order) * 4} KB）")
            for line in self._fmt_hist(d['attempt_lat'][order], 'order'):
                print(line)
        print()

        print("每个 zone 的规整延迟（按 sync 分维度：异步遇锁竞争就放弃，同步会等）：")
        for sync in sorted(d['zone_lat']):
            print(f"  sync = {sync}  （{'同步迁移' if sync else '异步迁移'}）")
            for line in self._fmt_hist(d['zone_lat'][sync], 'sync'):
                print(line)

    def run(self):
        """运行主循环

        以 interval 为间隔持续休眠，等待 eBPF 程序在后台采集数据。
        用户可通过 Ctrl+C 中断循环。

        注意：实际的数据读取和展示逻辑通常在外部调用方（如主脚本）
        中实现，此处的 run() 仅维持进程不退出，确保 eBPF 程序
        持续运行并更新 map 数据。
        """
        while True:
            try:
                time.sleep(self.interval)          # 等待下一个采样周期
            except KeyboardInterrupt:
                exit()                              # 优雅退出


# ============================================================================
# 简易命令行入口
#
# 为什么加在这里而不是新开一个 .py：交接手册明确要求"不新开 Python 文件"。
# 为什么需要它：compactinfo.c 必须能**独立跑起来验证**，
#   否则每次调试内核态代码都得绕道 extfrag_user.py 的 curses TUI，很别扭。
# 这个入口只做一件事：挂探针 → 等 → 打印文本。不做 TUI、不做曲线。
#
# 用法：
#   sudo python3 extfrag.py --mode compact --duration 60
#   sudo python3 extfrag.py --mode compact --interval 5     # 每 5 秒打印一次
# ============================================================================
if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(
        description="内存碎片与慢路径代价观测（eBPF / BCC）")
    ap.add_argument('--mode', default='compact', choices=list(ExtFrag.MODE_SRC),
                    help="挂哪一套探针（默认 compact）")
    ap.add_argument('--interval', type=int, default=5,
                    help="打印间隔（秒），默认 5")
    ap.add_argument('--duration', type=int, default=0,
                    help="总运行时长（秒），0 = 直到 Ctrl-C")
    ap.add_argument('--once', action='store_true',
                    help="只在结束时打印一次汇总，中途不打印")
    args = ap.parse_args()

    if os.geteuid() != 0:
        raise SystemExit("需要 root：挂载 eBPF 探针和读 tracefs 都要特权")

    ef = ExtFrag(interval=args.interval, mode=args.mode)
    print(f"[extfrag] 已加载 {ExtFrag.MODE_SRC[args.mode]}，mode={args.mode}，"
          f"间隔 {args.interval}s"
          + (f"，运行 {args.duration}s" if args.duration else "，Ctrl-C 结束"))

    if args.mode != 'compact':
        # 其余 mode 的展示层是 v1 的 extfrag_user.py，这里不重复实现
        raise SystemExit(
            f"mode={args.mode} 的展示请用 extfrag_user.py；"
            "本入口目前只负责 compact 的文本输出")

    started = time.time()
    try:
        while True:
            time.sleep(args.interval)
            if not args.once:
                ef.print_compact()
                print()
            if args.duration and (time.time() - started) >= args.duration:
                break
    except KeyboardInterrupt:
        print()
    finally:
        print("=" * 72)
        print("最终汇总")
        ef.print_compact()
