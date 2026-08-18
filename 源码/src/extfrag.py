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
        'reclaim': 'reclaiminfo.c',    # v2 P1：直接回收代价多大
    }

    # ------------------------------------------------------------------
    # /proc/vmstat 里需要对账的计数器
    #
    # 为什么工具自己要读 vmstat：
    #   eBPF 数出来的数字**没有第二个来源就无法证伪**。P0 阶段发现的
    #   "compact_stall == 外层退出 − COMPACT_SKIPPED 次数"这条等式，
    #   就是靠 vmstat 才能验的。原来这个对账要人工做（拿工具输出去和
    #   `grep compact /proc/vmstat` 比），漏做一次就等于没有自证。
    #
    # 为什么两套 mode 的键放在一个集合里：
    #   读一次 /proc/vmstat 的成本和读几个键无关（seq_file 一次生成全部），
    #   分开反而多一次 open/read。
    # ------------------------------------------------------------------
    VMSTAT_KEYS = (
        # --- compact（P0）---
        'compact_stall', 'compact_success', 'compact_fail',
        'compact_daemon_wake', 'compact_isolated',
        'compact_migrate_scanned', 'compact_free_scanned',
        # --- reclaim（P1）---
        'pgscan_direct', 'pgsteal_direct', 'pgscan_direct_throttle',
        'allocstall_dma', 'allocstall_dma32',
        'allocstall_normal', 'allocstall_movable',
    )

    @classmethod
    def read_vmstat(cls):
        """读一次 /proc/vmstat，只取 VMSTAT_KEYS 里的键

        ★ 取不到的键返回 -1 而**不是 0**。
          返回 0 会让"这台机器没这个计数器"和"这个计数器真的是 0"
          变成同一个值，增量算出来是假的 0 —— 而假的 0 会让自检
          "恰好通过"。fragstress/memhog.c 的 read_kv() 是同一个约定。
        """
        out = {k: -1 for k in cls.VMSTAT_KEYS}
        try:
            with open('/proc/vmstat') as f:
                for line in f:
                    parts = line.split()
                    if len(parts) == 2 and parts[0] in out:
                        out[parts[0]] = int(parts[1])
        except OSError as e:
            print(f"！读 /proc/vmstat 失败（{e}），vmstat 对账这一节不可信")
        return out

    @staticmethod
    def read_psi():
        """读 /proc/pressure/memory，返回 {'some': total_us, 'full': total_us}

        PSI 是内核**独立统计**的内存压力，不经过我们的探针 ——
        所以它是 P1 唯一的第三方交叉校验来源。格式：
            some avg10=0.00 avg60=0.00 avg300=0.00 total=8391452
            full avg10=0.00 avg60=0.00 avg300=0.00 total=6499962
        total 单位是微秒。取不到返回 None（不返回 0，理由同 read_vmstat）。
        """
        out = {}
        try:
            with open('/proc/pressure/memory') as f:
                for line in f:
                    parts = line.split()
                    if not parts:
                        continue
                    for p in parts[1:]:
                        if p.startswith('total='):
                            out[parts[0]] = int(p[6:])
        except OSError:
            return None            # 内核没开 CONFIG_PSI，或没这个文件
        return out or None

    @staticmethod
    def vmstat_delta(base, now, key):
        """算增量；任一端缺失（-1）就返回 None，由调用方显式说"没这个数"

        不返回 0：见 read_vmstat 的说明。
        """
        b, n = base.get(key, -1), now.get(key, -1)
        if b < 0 or n < 0:
            return None
        return n - b

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

        # ★ vmstat 基线必须在 BPF() **之后**取，不能在之前。
        #   BCC 是加载时现场调 clang 编译的，BPF() 这一步要花一两秒；
        #   基线放在它前面，这一两秒里发生的事件会进 vmstat 增量却没进
        #   eBPF 计数，对账凭空差出一截。放在后面，偏差窗口只剩
        #   "探针挂好" 到 "open(/proc/vmstat)" 之间的几微秒。
        #   残留偏差的方向是固定的：**eBPF 可能多算，不会少算**
        #   （窗口内的事件探针抓到了，但已被算进基线）。
        self.vmstat_base = self.read_vmstat()
        self.psi_base = self.read_psi()

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

    # reclaiminfo.c 的 stat_map 下标，必须与那边的 S_* 宏逐一对应
    RECL_STAT_NAMES = [
        'outer_enter',      # 0 进入 try_to_free_pages
        'outer_exit',       # 1 从 try_to_free_pages 返回（kretprobe 命中）
        'outer_unpaired',   # 2 ★ kretprobe 找不到入口记录
        'begin',            # 3 begin 埋点命中
        'begin_no_outer',   # 4 ★ begin 找不到外层记录（前提被推翻的信号之一）
        'order_mismatch',   # 5 ★ kprobe 取的 order ≠ begin 报的 order（预期 0）
        'end_accept',       # 6
        'end_unpaired',     # 7 ★ end 找不到 begin
        'zero_reclaim',     # 8 卡了一趟但一页没回收到
        'throttle_slept',   # 9 疑似被限流睡过
        'ret_one',          # 10 返回值 = 1（有歧义，见 reclaiminfo.c 头部第四节）
        'no_direct_reclaim',  # 11 gfp 里没有 __GFP_DIRECT_RECLAIM（预期 0）
        'stack_fail',       # 12 get_stackid 失败，这次的调用栈没记上
        'hist_fail_outer',  # 13 ★ outer_lat 自增失败（BCC increment 静默失败）
        'hist_fail_inner',  # 14 ★ inner_lat 自增失败
        'hist_fail_recl',   # 15 ★ recl_hist 自增失败
    ]

    # sum_map 的下标，与 reclaiminfo.c 的 R_* 宏对应
    RECL_SUM_NAMES = ['pages', 'outer_ns', 'inner_ns', 'throttle_ns']

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
    def _fmt_hist(buckets, label, unit='μs'):
        """把 {log2桶: 计数} 渲染成一行行文本直方图

        ★ unit 必须由调用方给：本函数原来把单位硬编码成 'μs'，
          结果 P1 的"每次回收到的页数"直方图被标成了「8 ~ 15 μs」——
          页数被当成时间显示。这种错误不会让程序崩，只会让**读的人**
          得出错误结论，而且截图进报告后很难再发现。
        """
        if not buckets:
            return ["    （无样本）"]
        lines = []
        peak = max(buckets.values())
        for slot in sorted(buckets):
            lo = 0 if slot == 0 else (1 << (slot - 1))
            hi = (1 << slot) - 1
            n = buckets[slot]
            bar = '*' * max(1, int(40 * n / peak))
            lines.append(f"    {lo:>8} ~ {hi:<8} {unit} | {n:>7} |{bar}")
        return lines

    def print_compact(self):
        """把 compaction 统计打印成纯文本

        交接手册明确要求：展示层能正确打印就够，不做 TUI / 曲线 / 颜色。
        """
        d = self.get_compact_data()
        s = d['stat']
        vm = self.read_vmstat()          # 现在的 vmstat，和 self.vmstat_base 比

        print("=" * 72)
        print("direct compaction 统计（只统计被同步卡住的进程，已排除 kcompactd 与手动规整）")
        print("=" * 72)

        d_stall = self.vmstat_delta(self.vmstat_base, vm, 'compact_stall')
        print(f"外层进入 try_to_compact_pages : {s['outer_enter']}"
              f"   ← 对账 /proc/vmstat compact_stall 增量 = "
              f"{'（读不到）' if d_stall is None else d_stall}")
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

        # ------------------------------------------------------------------
        # ★ 与 /proc/vmstat 对账（第三方来源，用来证伪 eBPF 自己的数）
        # ------------------------------------------------------------------
        print("与 /proc/vmstat 对账：")

        # ① compact_stall 恒等式。
        #    内核在 page_alloc.c:4409 判到 COMPACT_SKIPPED 会**早退**，
        #    而早退发生在入口 tracepoint 之前 —— 但 kretprobe 挂在函数返回上，
        #    照样命中。所以：
        #        compact_stall = 外层退出 − COMPACT_SKIPPED 次数
        #    这不是"应该差不多"，是能对上整数的等式。对不上就是有 bug。
        n_skipped = d['attempt_status'].get('SKIPPED', 0)
        expect_stall = s['outer_exit'] - n_skipped
        if d_stall is None:
            print("  ！读不到 compact_stall，stall 恒等式无法验证")
        elif d_stall == expect_stall:
            print(f"  ✓ compact_stall 恒等式成立：{d_stall} = 外层退出 {s['outer_exit']}"
                  f" − SKIPPED {n_skipped}")
        else:
            print(f"  ！compact_stall 恒等式不成立：vmstat {d_stall} ≠ "
                  f"{expect_stall}（外层退出 {s['outer_exit']} − SKIPPED {n_skipped}）"
                  f"，差 {d_stall - expect_stall}")
            print("    → 可能原因：观测窗口内有别的进程也在规整（本工具只过滤 "
                  "kcompactd 和手动规整，不区分进程）；或早退路径的理解有误。")

        # ② stall = success + fail（内核自己的三个计数器之间的关系）
        d_succ = self.vmstat_delta(self.vmstat_base, vm, 'compact_success')
        d_fail = self.vmstat_delta(self.vmstat_base, vm, 'compact_fail')
        if None not in (d_stall, d_succ, d_fail):
            mark = '✓' if d_stall == d_succ + d_fail else '！'
            print(f"  {mark} compact_stall {d_stall} vs success {d_succ}"
                  f" + fail {d_fail} = {d_succ + d_fail}")
            if d_stall != d_succ + d_fail:
                print("    → 这三个计数器由内核在同一处更新，对不上说明采样窗口"
                      "跨越了别的规整活动")

        # ③ ★ 双扫描器扫描量与扫描比 —— 判别"碎片的性质"，不是"碎片的多少"
        #
        #   规整的机制是两个扫描器对着走（compaction.c）：
        #     migrate 扫描器从 pageblock 低地址往高走，找**可移动的页**；
        #     free    扫描器从高地址往低走，找**空位**；
        #   两者相遇即本次结束。所以两边扫的量之比反映的是"哪一边稀缺"：
        #
        #     比值 > 2    找空位难 → 空闲块本身不够（内存真的满了/太碎）
        #     0.5 ~ 2     两边都在正常干活
        #     比值 < 0.5  找可移动页难 → 扫过的大多搬不走
        #                 = UNMOVABLE 页散布的指纹（这是最坏的碎片形态：
        #                   规整对它无效，因为内核自己的页搬不动）
        #
        #   为什么扫描量只能从 vmstat 取、不能从 tracepoint 算：
        #     mm_compaction_begin/end 带的是两个扫描器的 pfn 位置，
        #     但 compaction 中途会 **重启扫描器**，硬拿 pfn 相减会得出
        #     负数或荒谬的巨大值（见 compactinfo.c:455 的说明）。
        #     compact_migrate_scanned / compact_free_scanned 是内核逐页累加的，
        #     不受重启影响 —— 所以这两个数**只有这一个可信来源**。
        #
        #   ★★ 但这两个来源的**统计人口不一样**，这是个必须写出来的缺陷：
        #     eBPF 侧过滤掉了 kcompactd 和手动规整，只留被同步卡住的进程；
        #     vmstat 的两个 scanned 计数器**是全局的，kcompactd 扫的也算进去**。
        #     所以：
        #       · 扫描比本身仍然可读 —— 它描述的是"这段时间内，这台机器上
        #         所有规整活动"面对的是哪种碎片，是个环境属性；
        #       · 但"每搬走 1 页扫 N 页"是**混了两拨人口的数**（分子含
        #         kcompactd 的扫描量，分母只有 direct compaction 的迁移量），
        #         只能当数量级参考，不能当 direct compaction 的性价比报出去。
        #     判断污染有多重：看下面打印的 compact_daemon_wake 增量。
        #     它是 0 才说明这段时间只有 direct compaction 在动。
        d_ms = self.vmstat_delta(self.vmstat_base, vm, 'compact_migrate_scanned')
        d_fs = self.vmstat_delta(self.vmstat_base, vm, 'compact_free_scanned')
        print()
        print("双扫描器（判别碎片性质，唯一可信来源是 vmstat，见上方注释）：")
        if d_ms is None or d_fs is None:
            print("  ！读不到 compact_migrate_scanned / compact_free_scanned")
        elif d_ms == 0 and d_fs == 0:
            print("  两个扫描器都没动 —— 本窗口内没有实际发生的规整"
                  "（可能全是 SKIPPED 早退）")
        else:
            print(f"  migrate 扫描 {d_ms} 页 / free 扫描 {d_fs} 页")
            # 人口污染检查：vmstat 的 scanned 是全局的，kcompactd 也算进去
            d_wake = self.vmstat_delta(self.vmstat_base, vm, 'compact_daemon_wake')
            if d_wake is None:
                print("  ！读不到 compact_daemon_wake，无法判断 kcompactd 污染程度")
            elif d_wake == 0:
                print("  ✓ compact_daemon_wake 增量 0：这段扫描量全部来自"
                      "被卡住的进程，和 eBPF 侧同一拨人口")
            else:
                print(f"  ！compact_daemon_wake 增量 {d_wake}："
                      "扫描量里混进了 kcompactd 的份")
                print("    → 扫描比仍可读（它描述环境），但下面那个"
                      "\"每搬走 1 页扫 N 页\"分子分母人口不同，只当数量级看")
            if d_fs == 0:
                print("  ！free 扫描量为 0 而 migrate 非 0：比值无意义，不做解读")
            else:
                ratio = d_ms / d_fs
                if ratio > 2:
                    verdict = "找空位难 → 空闲块不够（内存满或极碎）"
                elif ratio < 0.5:
                    verdict = ("★ 找可移动页难 → 扫过的大多搬不走，"
                               "UNMOVABLE 散布的指纹；规整对这种碎片基本无效")
                else:
                    verdict = "两边都在正常干活"
                print(f"  扫描比 migrate/free = {ratio:.2f}  → {verdict}")
                # 每次迁移要扫多少页 —— 规整的"性价比"
                if d['migrated']:
                    print(f"  每搬走 1 页平均要扫 "
                          f"{(d_ms + d_fs) / d['migrated']:.1f} 页")
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

    # ========================================================================
    # P1：direct reclaim
    # ========================================================================

    def get_reclaim_data(self):
        """读 reclaiminfo.c 的所有 map"""
        stat = {}
        for i, name in enumerate(self.RECL_STAT_NAMES):
            stat[name] = self.b["stat_map"][ctypes.c_int(i)].value
        summ = {}
        for i, name in enumerate(self.RECL_SUM_NAMES):
            summ[name] = self.b["sum_map"][ctypes.c_int(i)].value

        # ★ 调用栈聚合，分成**两个**维度 —— 2026-08-18 观测轮改的，原因见下。
        #
        #   direct_callers：按**第 2 帧**（try_to_free_pages 的直接调用者）聚合。
        #     这才是"到底有几个调用者"这个是非问题的答案。
        #   alloc_sites  ：按前 6 帧的完整栈聚合，回答的是另一个问题 ——
        #     "谁在申请内存导致的回收"，属于归因（P2 的雏形）。
        #
        #   原来这里只有一个维度、用整条 6 帧栈当 key 去数"有几个调用者"，
        #   结果 9 条栈的第 2 帧其实**全是 __alloc_pages**，工具却报
        #   "出现 9 条不同调用栈，前提可能被推翻"。分叉发生在第 3 帧往后，
        #   那是分配点不是调用点 —— 把两个问题混成了一个。
        #
        # ★ 还有一个更严重的：原来写的是 `callers[文本] = v.value`，**赋值不是累加**。
        #   多个 stackid 只要前 6 帧文本相同就互相覆盖，
        #   实测 9 条栈加起来只有 60 次，而外层进入是 1299 次 —— 分布完全失真。
        #   下面改成 += 之后，两个维度的总和都应当 = 外层进入 − stack_fail，
        #   print_reclaim 里对这条等式做了显式核对。
        direct_callers = {}
        alloc_sites = {}
        try:
            stacks = self.b["caller_stacks"]
            for k, v in self.b["caller_count"].items():
                sid = k.value
                try:
                    frames = [self.b.ksym(a).decode('utf-8', 'replace')
                              if isinstance(self.b.ksym(a), bytes)
                              else str(self.b.ksym(a))
                              for a in stacks.walk(sid)]
                except Exception:
                    frames = [f"（栈 {sid} 解析失败）"]
                n = v.value
                # 第 0 帧是 try_to_free_pages 自己，第 1 帧才是调用者
                who = frames[1] if len(frames) > 1 else '（栈只有一帧）'
                direct_callers[who] = direct_callers.get(who, 0) + n
                site = ' ← '.join(frames[:6])
                alloc_sites[site] = alloc_sites.get(site, 0) + n
        except KeyError:
            pass          # 没有这两个 map（不该发生，但不让它把整个输出搞崩）

        return {
            'stat': stat,
            'sum': summ,
            'outer_lat': self._hist_to_dict(self.b["outer_lat"], 'order'),
            'inner_lat': self._hist_to_dict(self.b["inner_lat"], 'order'),
            'recl_hist': self._hist_to_dict(self.b["recl_hist"], 'order'),
            'direct_callers': direct_callers,
            'alloc_sites': alloc_sites,
        }

    def print_reclaim(self):
        """把 direct reclaim 统计打印成纯文本"""
        d = self.get_reclaim_data()
        s, sm = d['stat'], d['sum']
        vm = self.read_vmstat()

        print("=" * 72)
        print("direct reclaim 统计（内核埋点本身就是 direct 专用，无需过滤 "
              "kswapd / memcg）")
        print("=" * 72)

        print(f"外层进入 try_to_free_pages : {s['outer_enter']}")
        print(f"外层退出（kretprobe）      : {s['outer_exit']}")
        print(f"内层 begin / end           : {s['begin']} / {s['end_accept']}")
        print()

        # ---------------- 未配对率：延迟分布可不可信的前提 ----------------
        # lru_hash 满了淘汰的是**停留最久**的表项，也就是延迟最长的样本。
        # 不报这个数，下面的延迟直方图就是不可信的 —— 硬要求，不是可选项。
        unpaired = s['outer_unpaired'] + s['end_unpaired']
        rate = (unpaired / s['outer_enter']) if s['outer_enter'] else 0.0
        print(f"★ 未配对率                 : {rate * 100:.2f}%"
              f"（外层 {s['outer_unpaired']} + 内层 {s['end_unpaired']}）")

        # ---------------- 几个"预期恒为 0"的自检格 ----------------
        print()
        print("自检（下面每一格预期都是 0，非 0 表示对内核路径的理解有误）：")
        if s['order_mismatch']:
            print(f"  ！order_mismatch = {s['order_mismatch']}："
                  "外层 kprobe 从寄存器取的 order 和 begin 埋点报的不一致")
            print("    → PT_REGS_PARM2 的取参假设错了（函数签名或调用约定变了）。"
                  "所有按 order 分维度的直方图都归错桶了，不可信。")
        else:
            print("  ✓ order_mismatch = 0：两条独立路径拿到的 order 一致，"
                  "取参假设成立")
        if s['no_direct_reclaim']:
            print(f"  ！no_direct_reclaim = {s['no_direct_reclaim']}："
                  "gfp 里没有 __GFP_DIRECT_RECLAIM，与'直接回收必然允许阻塞'矛盾")
        else:
            print("  ✓ no_direct_reclaim = 0")
        if s['begin_no_outer']:
            print(f"  ！begin_no_outer = {s['begin_no_outer']}："
                  "有 begin 却没有对应的外层记录")
            print("    → 若持续增长，说明 try_to_free_pages 之外还有别的代码"
                  "在打这个 tracepoint，外层-内层对账要重做（见下方调用者栈）")
        else:
            print("  ✓ begin_no_outer = 0")

        # ---------------- ★ 调用者栈：把一个假设变成实测 ----------------
        # 见 reclaiminfo.c 头部第二节：本机源码树只有 3 个 mm 文件，
        # "try_to_free_pages 只有一个调用者"这句话本地验证不了，
        # 所以直接把调用栈测出来。
        print()
        print("★ 谁在调 try_to_free_pages（实测调用栈的第 2 帧，"
              "不是靠读源码假设的）：")
        dc, sites = d['direct_callers'], d['alloc_sites']
        if not dc:
            print("    （没有样本）")
        else:
            for who, n in sorted(dc.items(), key=lambda x: -x[1]):
                print(f"    {n:>7} 次  {who}")
            # 调用栈总数对账：应当等于 外层进入 − get_stackid 失败次数
            got = sum(dc.values())
            want = s['outer_enter'] - s['stack_fail']
            if got == want:
                print(f"  ✓ 栈样本总数 {got} = 外层进入 {s['outer_enter']} "
                      f"− 取栈失败 {s['stack_fail']}")
            else:
                print(f"  ！栈样本总数 {got} ≠ 期望 {want}"
                      f"（外层进入 {s['outer_enter']} − 取栈失败 "
                      f"{s['stack_fail']}），下面的占比不可信")
            if len(dc) == 1:
                print("  ✓ 只有一个直接调用者 → '唯一调用者'这个前提被实测证实，"
                      "外层探针确实不需要来源过滤")
            else:
                print(f"  ！出现 {len(dc)} 个不同的直接调用者 —— "
                      "'外层不需要过滤'的结论必须推翻，外层-内层对账要重做。")

            # ---- 另一个维度：谁在申请内存（归因，不是判定调用者的依据）----
            print()
            print("  谁的分配触发了回收（完整栈前 6 帧，这是归因不是调用者判定）：")
            for site, n in sorted(sites.items(), key=lambda x: -x[1])[:10]:
                print(f"    {n:>7} 次  {site}")
            if len(sites) > 10:
                print(f"    …… 另有 {len(sites) - 10} 条栈未显示")

        # ---------------- 限流：外层与内层的差额 ----------------
        # ★ vmscan.c:3569 那行 `if (throttle_direct_reclaim(...)) return 1;`
        #   看着像"被限流就早退"，其实 throttle_direct_reclaim 只在
        #   fatal_signal_pending 时才返回 true（:3533）；正常被限流的进程是
        #   在 :3530 wait_event_killable 睡一觉，醒来继续走到 begin。所以：
        #       外层退出 − begin = 被限流**且期间收到致命信号**的次数（通常 0）
        #       真正的被限流次数 = /proc/vmstat 的 pgscan_direct_throttle
        print()
        print("限流（throttle_direct_reclaim）：")
        early = s['outer_exit'] - s['begin']
        d_thr = self.vmstat_delta(self.vmstat_base, vm, 'pgscan_direct_throttle')
        print(f"  外层退出 − begin = {early}"
              "   ← 这是'被限流且收到致命信号'的次数，不是被限流总次数")
        print(f"  pgscan_direct_throttle 增量 = "
              f"{'（读不到）' if d_thr is None else d_thr}"
              "   ← 这才是被限流总次数（:3515 在睡之前就计了）")
        if d_thr == 0 and s['throttle_slept'] == 0:
            print("  → 本轮完全没有限流发生，所以'外层 − 内层'里没有睡眠成分，"
                  "那点差额只是两个探针之间几行代码的开销")
        elif d_thr:
            print(f"  → 发生了限流。疑似睡过的次数（外层−内层 > 1ms）："
                  f"{s['throttle_slept']}")

        # ---------------- 时间的分解 ----------------
        print()
        outer_s = sm['outer_ns'] / 1e9
        inner_s = sm['inner_ns'] / 1e9
        queue_s = sm['throttle_ns'] / 1e9
        print("时间去哪了：")
        print(f"  外层总耗时（进程实际被卡住）: {outer_s:.3f} s")
        print(f"  内层总耗时（纯扫描）        : {inner_s:.3f} s")
        if outer_s > 0:
            print(f"  差额（排队/限流）           : {queue_s:.3f} s"
                  f"  = 外层的 {100 * queue_s / outer_s:.1f}%")
        else:
            print(f"  差额（排队/限流）           : {queue_s:.3f} s")
        if s['outer_exit']:
            print(f"  平均每次被卡住              : "
                  f"{outer_s * 1000 / s['outer_exit']:.2f} ms")

        # ---------------- 回收的产出 ----------------
        print()
        print("回收到了什么：")
        print(f"  累计回收页数（本工具）      : {sm['pages']}"
              f"  = {sm['pages'] * 4 / 1024:.1f} MB")
        d_steal = self.vmstat_delta(self.vmstat_base, vm, 'pgsteal_direct')
        d_scan = self.vmstat_delta(self.vmstat_base, vm, 'pgscan_direct')
        if d_steal is not None:
            diff = sm['pages'] - d_steal
            mark = '✓' if abs(diff) <= max(16, abs(d_steal) * 0.01) else '！'
            print(f"  {mark} 对账 pgsteal_direct 增量  : {d_steal}"
                  f"（差 {diff}）")
            # ★ 这两个数**本来就不该完全相等**，而且差值的符号是固定的：
            #   本工具读的是 end 埋点的 nr_reclaimed，也就是 sc->nr_reclaimed；
            #   而 mm/vmscan.c:3082（shrink_node 里）有
            #       sc->nr_reclaimed += reclaim_state->reclaimed_slab;
            #   —— slab shrinker 回收的页算进 nr_reclaimed，
            #   但 pgsteal_direct 只在 :2229 的 shrink_inactive_list 里计 LRU 页，
            #   **不含 slab**。所以正差额 = 这段时间 shrinker 释放的 slab 页
            #   （dentry / inode 缓存之类）。负差额才是真异常。
            if 0 < diff:
                print(f"    → 正差额 {diff} 页（约 {diff * 4 / 1024:.1f} MB）"
                      "= slab shrinker 回收的页：它算进 nr_reclaimed"
                      "（vmscan.c:3082）但不算进 pgsteal_direct（只计 LRU 页）")
            elif diff < 0:
                print(f"    ！负差额 {diff}：本工具比内核**少**数了页，"
                      "这个方向没有已知的合理解释，要查")
            if mark == '！':
                print("    → 差得多。注意 pgsteal_direct 是**全局**计数器，"
                      "而本工具只统计探针在线期间的事件；")
                print("      另外 cgroup 内的回收不计入全局 pgsteal_direct"
                      "（mm/vmscan.c:2213），若有容器在跑就会对不上。")
        if d_scan is not None and sm['pages']:
            if d_scan > 0:
                print(f"  pgscan_direct 增量          : {d_scan}"
                      f"  → 每回收 1 页要扫 {d_scan / sm['pages']:.1f} 页"
                      "（这个比值越大说明 LRU 上越难找到可回收的页）")
            else:
                # 本工具说回收了页，内核说一页都没扫过 —— 两者不可能同时为真
                print(f"  ！pgscan_direct 增量 = {d_scan}，但本工具报告回收了 "
                      f"{sm['pages']} 页 —— 互相矛盾")
                print("    → 要么基线取错了（探针挂载与读基线的先后），"
                      "要么回收发生在 cgroup 内（mm/vmscan.c:2213："
                      "cgroup 内回收不计全局计数器）")
        # ALLOCSTALL：★ 必须四个桶全加。按 gfp_zone(gfp_mask) 分桶，
        # 匿名页用 GFP_HIGHUSER_MOVABLE → 落在 allocstall_movable，
        # 哪怕这台机器的 Movable zone 是空的。只加 normal+dma32 会漏掉绝大部分。
        buckets = ['allocstall_dma', 'allocstall_dma32',
                   'allocstall_normal', 'allocstall_movable']
        parts = [self.vmstat_delta(self.vmstat_base, vm, k) for k in buckets]
        if None not in parts:
            total_as = sum(parts)
            detail = '  '.join(f"{k.split('_')[1]}={v}"
                               for k, v in zip(buckets, parts) if v)
            print(f"  Σallocstall_*（四桶全加）   : {total_as}"
                  f"   [{detail or '全为 0'}]")
            # 等式：Σallocstall = begin 次数 + do_try_to_free_pages 内 retry 次数
            #       （vmscan.c:3330 的 retry: 标签在 :3334 的计数之前）
            #   所以 Σallocstall ≥ begin，差额就是 retry 次数。这**不是恒等式**。
            if total_as >= s['begin']:
                print(f"    → 减去 begin {s['begin']} 后剩 "
                      f"{total_as - s['begin']}，这是 do_try_to_free_pages "
                      "内部 goto retry 的次数（vmscan.c:3330 的 retry 标签"
                      "在 :3334 计数之前）")
            else:
                print(f"    ！Σallocstall {total_as} < begin {s['begin']}，"
                      "方向不对：这个不等式应当恒成立，需要查")

        # ---------------- ★ PSI 交叉校验，连同它的两个偏差一起说清 ----------------
        print()
        print("★ 与 /proc/pressure/memory 对账（第三方来源，内核独立统计）：")
        psi_now = self.read_psi()
        if not psi_now or not self.psi_base:
            print("  （读不到 PSI，可能内核没开 CONFIG_PSI）")
        else:
            d_some = psi_now.get('some', 0) - self.psi_base.get('some', 0)
            d_full = psi_now.get('full', 0) - self.psi_base.get('full', 0)
            print(f"  PSI some 增量 {d_some / 1e6:.3f} s / "
                  f"full 增量 {d_full / 1e6:.3f} s")
            print(f"  本工具外层总耗时 {outer_s:.3f} s")
            if d_some > 0:
                print(f"  比值（本工具 / PSI some）= {outer_s * 1e6 / d_some:.2f}")
            print("  ★ 这**不是恒等式**，两个方向的偏差同时存在，别当成"
                  "\"对上了就正确\"：")
            print("    ① PSI 的口径更宽：psi_memstall_enter 在内核里有 5 处，"
                  "本工具只覆盖 1 处")
            print("       （page_alloc.c:4653 直接回收 ← 我们；"
                  "page_alloc.c:4395 直接规整；")
            print("        vmscan.c:3898 kswapd；vmscan.c:4514 node reclaim；"
                  "compaction.c:2960 kcompactd）")
            print("       → 压力大时 kswapd 那一处的贡献可能远超我们这一处，"
                  "使 PSI 偏大")
            print("    ② PSI some 是**墙钟**：N 个线程同时卡住只算一份时间，"
                  "本工具是逐次求和")
            print("       → 并发越高，本工具的总和相对 PSI 越偏大")
            print("    所以这条校验能发现的是**数量级错误**"
                  "（比如差 1000 倍 = 单位搞错了），不能证明数字精确。")
            try:
                with open('/proc/sys/vm/zone_reclaim_mode') as f:
                    zrm = f.read().strip()
                print(f"    （zone_reclaim_mode = {zrm}"
                      f"{'，所以 node reclaim 那一处不贡献' if zrm == '0' else ''}）")
            except OSError:
                pass

        # ---------------- 交叉校验：直方图有没有静默丢样本 ----------------
        print()
        hist_n = sum(sum(b.values()) for b in d['outer_lat'].values())
        expect_n = s['outer_exit'] - s['outer_unpaired']
        if hist_n == expect_n:
            print(f"✓ 交叉校验通过：外层延迟直方图样本数 {hist_n} = "
                  f"外层退出 {s['outer_exit']} − 未配对 {s['outer_unpaired']}")
        else:
            lost = expect_n - hist_n
            pct = (lost / expect_n * 100) if expect_n else 0.0
            print(f"！交叉校验失败：直方图样本数 {hist_n} ≠ 期望 {expect_n}"
                  f"，丢了 {lost} 个（{pct:.2f}%）")
            # ★ 这里原来写的是"最可能是 map 满"。2026-08-18 的观测轮证伪了它：
            #   三张直方图容量都是 1024，实际只用了 9 个 key。别再拿它当结论。
            print(f"  自增失败计数：outer {s['hist_fail_outer']} / "
                  f"inner {s['hist_fail_inner']} / recl {s['hist_fail_recl']}")
            if s['hist_fail_outer'] == lost:
                print("  → 丢的样本全部出在 lookup_or_try_init 失败这一步"
                      "（不是 map 满：容量 1024，实际只用了几个 key）")
            elif s['hist_fail_outer'] == 0:
                print("  → 自增没失败过，说明丢在别处，回去查读 map 的时机")
            else:
                print("  → 自增失败只解释了一部分，剩下的另有来源")
            if pct < 2.0:
                print(f"  影响面：丢失 {pct:.2f}%，分布的形状和分位数基本不受影响，"
                      "但样本总数不能直接当成事件数用")
            else:
                print("  影响面：丢失比例已经不小，延迟分布不可信")

        # ---------------- 直方图 ----------------
        print()
        print("进程被卡住的总时长（外层：含限流睡眠，这是用户感知到的卡顿）：")
        for order in sorted(d['outer_lat']):
            print(f"  order = {order}  （{(1 << order) * 4} KB）")
            for line in self._fmt_hist(d['outer_lat'][order], 'order'):
                print(line)
        print()
        print("纯扫描耗时（内层 begin→end，不含限流睡眠）：")
        for order in sorted(d['inner_lat']):
            print(f"  order = {order}  （{(1 << order) * 4} KB）")
            for line in self._fmt_hist(d['inner_lat'][order], 'order'):
                print(line)
        print()
        print(f"每次回收到的页数（log2 桶；另有 {s['zero_reclaim']} 次"
              "回收到 0 页 —— 那是白卡一趟，全部延迟没换到一页）：")
        for order in sorted(d['recl_hist']):
            print(f"  order = {order}")
            for line in self._fmt_hist(d['recl_hist'][order], 'order', unit='页'):
                print(line)
        # ★ 32~63 那一格是有来历的，不是巧合：try_to_free_pages 的
        #   scan_control 里 .nr_to_reclaim = SWAP_CLUSTER_MAX = 32
        #   （mm/vmscan.c:3545），shrink_node 一旦攒够 32 页就返回。
        #   所以"众数落在 32~63"本身就是一条对账 —— 直方图对上了内核常量。
        recl_all = {}
        for b in d['recl_hist'].values():
            for slot, n in b.items():
                recl_all[slot] = recl_all.get(slot, 0) + n
        if recl_all:
            top = max(recl_all, key=lambda x: recl_all[x])
            tot = sum(recl_all.values())
            lo = 0 if top == 0 else (1 << (top - 1))
            print(f"  众数落在 {lo} ~ {(1 << top) - 1} 页"
                  f"（{recl_all[top]}/{tot} = {recl_all[top] / tot * 100:.1f}%）"
                  + ("  ← 正好压住 SWAP_CLUSTER_MAX = 32（vmscan.c:3545 的 "
                     ".nr_to_reclaim），说明绝大多数调用是'扫够 32 页立刻返回'"
                     if lo == 32 else ""))

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

    # 本入口负责 v2 的两个文本展示；v1 的 frag/extfrag 仍走 extfrag_user.py 的 TUI
    PRINTERS = {
        'compact': 'print_compact',   # P0
        'reclaim': 'print_reclaim',   # P1
    }
    if args.mode not in PRINTERS:
        raise SystemExit(
            f"mode={args.mode} 的展示请用 extfrag_user.py；"
            f"本入口只负责 {'/'.join(PRINTERS)} 的文本输出")
    show = getattr(ef, PRINTERS[args.mode])

    started = time.time()
    try:
        while True:
            time.sleep(args.interval)
            if not args.once:
                show()
                print()
            if args.duration and (time.time() - started) >= args.duration:
                break
    except KeyboardInterrupt:
        print()
    finally:
        print("=" * 72)
        print("最终汇总")
        show()
