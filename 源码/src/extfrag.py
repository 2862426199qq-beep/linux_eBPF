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

    def __init__(self, interval=2, output_extfrag_index=False,
                 output_unusable_index=False, output_count=False,
                 zone_info=False):
        """初始化 ExtFrag 实例

        Args:
            interval:             数据采集间隔（秒），通过 delay_map
                                  传递给 eBPF 程序，控制内核态采样频率
            output_extfrag_index: 是否输出外部碎片指数（extfrag_index）
            output_unusable_index:是否输出不可用内存指数
            output_count:         是否启用进程级分配计数。
                                  为 True 时加载 extfraginfo.c（含 fallback 计数），
                                  为 False 时加载 fraginfo.c（仅碎片信息）
            zone_info:            是否输出详细的 Zone 信息
        """
        self.interval = interval
        self.output_extfrag_index = output_extfrag_index
        self.output_unusable_index = output_unusable_index
        self.output_count = output_count
        self.zone_info = zone_info

        # 根据是否启用计数功能，加载不同的 eBPF 程序
        # fallback 是指：当某个 migratetype 的 freelist 里没有合适的空闲块时，内核退而求其次，从其他 migratetype 的 freelist 里"借"内存。
        # extfraginfo.c: 除碎片信息外，还通过 kprobe 挂载内存分配函数，
        #                记录每次 fallback 分配的进程 PID、comm、PFN 等信息
        # fraginfo.c:    仅采集各 order 的空闲块统计，开销更小
        if self.output_count:
            self.b = BPF(src_file="./bpf/extfraginfo.c")
        else:
            self.b = BPF(src_file="./bpf/fraginfo.c")

        # 将采集间隔写入 eBPF 侧的 BPF_HASH delay_map
        # eBPF 程序通过读取此 map 来决定每次采样的时间间隔
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
