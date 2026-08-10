#!/bin/bash
#
# hugetlb.sh —— fragstress 档位 3 的"第一发子弹"：制造高阶分配需求
#
# ============================ 它在内核里干了什么 ============================
#
# 前两档（holes / kstack）只是把内存**弄乱**，但弄乱本身不会触发规整。
# 规整是被"有人要连续大块但拿不到"这件事逼出来的。order-0 的分配永远不会触发规整
# —— 单页哪儿都有，随便给一个就行。
#
# 所以必须有人来要大块。这里选 hugetlb（大页池），因为它是**最直接、最同步**的一条路：
#
#   echo N > /proc/sys/vm/nr_hugepages
#     → hugetlb_sysctl_handler() → set_max_huge_pages()
#     → alloc_pool_huge_page() → alloc_fresh_huge_page() → __alloc_pages(order=9)
#     → 快路径 get_page_from_freelist() 拿不到 order-9
#     → 进 __alloc_pages_slowpath()
#     → order 9 > 3 属于 costly order，先来一发 direct compaction
#     → try_to_compact_pages()  ← ★ 我们 P0 要挂的埋点就在这儿
#     → compact_stall++
#
# 关键点：这条路径是**同步**的 —— 写 sysctl 的这个进程（也就是你的 shell）
# 会被卡在内核里直到规整结束。这正是我们要量化的"慢路径代价"。
#
# ============================ 为什么不用 THP ============================
#
# 本机实测：
#   /sys/kernel/mm/transparent_hugepage/enabled = always [madvise] never
#   /sys/kernel/mm/transparent_hugepage/defrag  = ... [madvise] never
#
# defrag=madvise 意味着：只有显式 madvise(MADV_HUGEPAGE) 过的区域，
# THP 分配失败时才会做**同步**规整；普通程序失败了就悄悄退化成 4KB 页，
# 顺手唤醒一下 kcompactd 就走人 —— 那是**异步**路径，不算 compact_stall。
#
# 所以 THP 留给档位 3 的后半段（thpload.c，做 defrag=always/never 的对照实验），
# 打通链路这一步用 hugetlb 最稳。
#
# ============================ ★ 一个必须避开的陷阱 ============================
#
#   echo 1 > /proc/sys/vm/compact_memory      # ← 别用它来验证！
#
# 它走的是**手动规整**路径（compact_node），会正常打出
# mm_compaction_begin / mm_compaction_end 埋点，看起来一切正常，
# 但它**不增加 compact_stall**，因为压根没有进程在等一次分配。
# 用它验证会得出"链路通了"的错误结论，实际测的是完全另一条路。
#
# 它只能用来验证"埋点挂没挂上"，不能用来验证 direct compaction。
#
# ============================ 用法 ============================
#
#   sudo ./hugetlb.sh <想要的大页数量> [每轮递增步长]
#   例：sudo ./hugetlb.sh 2000 500
#
# 一个大页 = Hugepagesize = 2048 kB = order-9。2000 个 = 4 GB。
# 脚本会分批递增申请，每批打印一次 vmstat 的变化，方便看清楚
# "从第几批开始内核开始规整了"。
#
# 结束后记得归零：sudo ./hugetlb.sh 0

set -u

COUNTERS='compact_stall|compact_fail|compact_success|compact_daemon_wake|compact_migrate_scanned|compact_free_scanned|compact_isolated|allocstall_normal|allocstall_movable|pgscan_direct|pgsteal_direct'

if [ "$(id -u)" -ne 0 ]; then
    echo "错误：需要 root（要写 /proc/sys/vm/nr_hugepages）。请用 sudo 跑。" >&2
    exit 1
fi

TARGET=${1:-}
STEP=${2:-500}

if [ -z "$TARGET" ]; then
    echo "用法: sudo $0 <想要的大页数量> [每轮递增步长]" >&2
    echo "     一个大页 = $(grep Hugepagesize /proc/meminfo | awk '{print $2" "$3}')" >&2
    exit 1
fi

snapshot() {
    grep -E "^($COUNTERS) " /proc/vmstat
}

show_delta() {
    # $1 = 标签, $2 = before 文件
    echo "--- vmstat 变化（$1）---"
    join -j1 <(sort "$2") <(snapshot | sort) 2>/dev/null | \
    awk '{ d = $3 - $2; if (d != 0) printf "  %-28s %12d -> %-12d  (+%d)\n", $1, $2, $3, d }'
    echo "  （只列出有变化的项；全都没变说明这一批是从空闲链表直接摘走的，没进慢路径）"
}

BEFORE=$(mktemp)
snapshot > "$BEFORE"

echo "===== 起始状态 ====="
echo "nr_hugepages 当前值: $(cat /proc/sys/vm/nr_hugepages)"
grep -E "HugePages_Total|HugePages_Free|Hugepagesize" /proc/meminfo
echo
echo "--- buddyinfo（申请前）---"
cat /proc/buddyinfo
echo
echo "--- 关键计数器（申请前）---"
cat "$BEFORE"
echo

if [ "$TARGET" -eq 0 ]; then
    echo "===== 归零：释放大页池 ====="
    echo 0 > /proc/sys/vm/nr_hugepages
    sleep 1
    echo "nr_hugepages 现在: $(cat /proc/sys/vm/nr_hugepages)"
    grep -E "HugePages_Total|HugePages_Free" /proc/meminfo
    rm -f "$BEFORE"
    exit 0
fi

CUR=$(cat /proc/sys/vm/nr_hugepages)
BATCH_BEFORE=$(mktemp)

while [ "$CUR" -lt "$TARGET" ]; do
    NEXT=$((CUR + STEP))
    [ "$NEXT" -gt "$TARGET" ] && NEXT=$TARGET

    snapshot > "$BATCH_BEFORE"
    echo "===== 申请 $NEXT 个大页（约 $((NEXT * 2)) MB）====="

    T0=$(date +%s.%N)
    echo "$NEXT" > /proc/sys/vm/nr_hugepages
    T1=$(date +%s.%N)

    GOT=$(cat /proc/sys/vm/nr_hugepages)
    printf "  写 sysctl 耗时: %.3f 秒（这段时间当前进程被同步卡在内核里）\n" \
           "$(echo "$T1 - $T0" | bc -l)"
    echo "  实际拿到: $GOT 个（要求 $NEXT 个）"
    show_delta "本批" "$BATCH_BEFORE"
    echo

    if [ "$GOT" -eq "$CUR" ]; then
        echo "  ！这一批一个都没拿到，内存已经榨干，停止递增。"
        break
    fi
    CUR=$GOT
done

echo "===== 最终状态 ====="
grep -E "HugePages_Total|HugePages_Free|Hugepagesize" /proc/meminfo
echo
echo "--- buddyinfo（申请后）---"
cat /proc/buddyinfo
echo
show_delta "总计（相对脚本启动时）" "$BEFORE"
echo
echo "验收判据：compact_stall 必须 > 0 且能持续增长。"
echo "如果 compact_stall 还是 0，说明大页全是从空闲链表直接摘走的 —— 碎片造得不够，"
echo "回去加大 holes 的量或者提高 hugetlb 的目标数量。"
echo
echo "用完记得归零：sudo $0 0"

rm -f "$BEFORE" "$BATCH_BEFORE"
