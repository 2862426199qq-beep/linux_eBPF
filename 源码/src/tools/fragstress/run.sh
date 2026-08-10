#!/bin/bash
#
# run.sh —— fragstress 总编排：一条命令跑完整个压力实验
#
# ============================ 为什么要有这个脚本 ============================
#
# 三个压力源必须**同时活着**，而且**上场顺序有讲究**：
#
#   1. kstack 先上  —— 趁内存还宽裕，让 UNMOVABLE 的内核栈散布到尽可能多的
#                       pageblock 里。等内存被占满了再创建线程，内核栈会挤在
#                       仅剩的几个区域，污染面反而小。
#   2. holes 后上   —— 把剩下的连续块砸碎。
#   3. hugetlb 最后 —— 在"又碎又脏"的内存上要连续大块，逼出 direct compaction。
#
# 人肉掐这个时序很容易错（前一个还没铺开就上下一个），所以把时序写进脚本：
# 每一步都**等上一步真正就绪**（轮询日志里的完成标记），再进下一步。
#
# 另外这台机器网卡会闪断（dmesg 里一堆 e1000 link down），
# 前台跑会被 SIGHUP 打断，所以建议用 nohup 后台跑、日志落盘。
#
# ============================ 用法 ============================
#
#   sudo -v                                                   # ★ 必须先单独输密码
#   sudo nohup ./run.sh > /tmp/fragstress_run.log 2>&1 &
#   tail -f /tmp/fragstress_run.log
#
# ★ 为什么要先 `sudo -v`：
#   直接 `sudo nohup ... &` 时，sudo 想从终端读密码，但它是后台作业，
#   一读终端就被内核发 SIGTTIN 停住（ps 里看到状态是 T / Stopped），
#   日志会是 0 字节，看起来像"什么都没发生"。
#   先用 `sudo -v` 在前台把密码输掉，凭据缓存 15 分钟，后台那次就不会再问。
#
# 参数用环境变量覆盖（都有默认值）：
#   KSTACK_N=12000  HOLES_MB=4000  HOLES_KB=64  HOLES_PCT=50
#   HP_TARGET=2000  HP_STEP=200
#
# 例：想加大压力
#   sudo HOLES_MB=5000 HP_TARGET=2600 nohup ./run.sh > /tmp/fragstress_run.log 2>&1 &
#
# 脚本结束（或被 Ctrl-C / kill）时会自动清理：
# 大页池归零、holes/kstack 杀掉，机器恢复原状。
#
# ============================ 产物 ============================
#
#   $OUTDIR/snap_*.txt   四个时间点的完整快照（buddyinfo/pagetypeinfo/vmstat/PSI）
#   $OUTDIR/batches.csv  每批大页申请的逐批计数器增量 ← 报告里的主表
#   $OUTDIR/*.log        holes / kstack 各自的输出

set -u

FRAGDIR=$(cd "$(dirname "$0")" && pwd)
cd "$FRAGDIR" || exit 1

KSTACK_N=${KSTACK_N:-12000}
HOLES_MB=${HOLES_MB:-4000}
HOLES_KB=${HOLES_KB:-64}
HOLES_PCT=${HOLES_PCT:-50}
HP_TARGET=${HP_TARGET:-2000}
HP_STEP=${HP_STEP:-200}
OUTDIR=${OUTDIR:-/tmp/fragstress-$(date +%Y%m%d-%H%M%S)}

KSTACK_PID=""
HOLES_PID=""

if [ "$(id -u)" -ne 0 ]; then
    echo "错误：需要 root（要写 /proc/sys/vm/nr_hugepages，要读 /proc/pagetypeinfo）" >&2
    echo "用法：sudo nohup $0 > /tmp/fragstress_run.log 2>&1 &" >&2
    exit 1
fi

if [ ! -x ./holes ] || [ ! -x ./kstack ]; then
    echo "错误：holes / kstack 还没编译，先跑 make" >&2
    exit 1
fi

mkdir -p "$OUTDIR"

# ---------------------------------------------------------------- 清理
# 无论正常结束还是被打断，都把机器恢复原状。
cleanup() {
    echo
    echo "===== 清理 ====="
    [ -n "$HOLES_PID" ]  && kill -INT "$HOLES_PID"  2>/dev/null && echo "  已停 holes (pid $HOLES_PID)"
    [ -n "$KSTACK_PID" ] && kill -INT "$KSTACK_PID" 2>/dev/null && echo "  已停 kstack (pid $KSTACK_PID)"
    echo 0 > /proc/sys/vm/nr_hugepages 2>/dev/null && echo "  大页池已归零"
    sleep 2
    echo "  当前 MemFree: $(awk '/^MemFree:/{print $2" kB"}' /proc/meminfo)"
    echo "  产物目录: $OUTDIR"
}
trap cleanup EXIT INT TERM

# ---------------------------------------------------------------- 工具函数

# 把 /proc/vmstat 读进关联数组。
#
# ★ 2026-08-09 修 bug：原来这里用 `local -n _dst=$1` 做 nameref，
#   循环体是 `_dst["$k"]=$v`。实测报错：
#       ./run.sh: 行 98: _dst["$k"]: 数组下标不正确
#   然后**整个递增循环在第 10 批悄悄终止**，恰好停在 1800 个大页 ——
#   而当时的"白拿库存"是 1851 个。也就是说脚本死在了
#   "再要一批就必须规整" 的前一刻，导致 compact_stall 假阴性为 0。
#
#   触发条件是 `$k` 为空（读到空行/半截行时 bash 认为下标非法）。
#   修法有两点：① 显式跳过空 key；② **不用 nameref**，
#   改成两个固定数组 VM_B / VM_A，少一层间接、少一个出错面。
declare -A VM_B VM_A

# 只取真正要用的计数器。用 awk 一次筛完再用管道喂给 read ——
# 好处是 bash 完全不直接读 /proc，少一个出错面。
VM_KEYS="compact_stall compact_fail compact_success compact_daemon_wake \
compact_migrate_scanned compact_free_scanned compact_isolated \
pgscan_direct pgsteal_direct allocstall_movable"

# ★ 2026-08-10 第三轮实验暴露的问题：batches.csv 里出现了**物理上不可能的值** ——
#   第 2 批 compact_daemon_wake 增量 = **-447**（单调递增计数器出现负增量），
#   第 3 批 compact_migrate_scanned 增量 = 3142214（恰好等于该计数器的绝对值）。
#   对照快照绝对值，本轮真实增量只有 42 次 stall，CSV 里那个 939 是假的。
#
#   机制**尚未查清**。曾假设是 bash 的 "按块读 + lseek 回退" 与 seq_file 冲突，
#   但做过实验证伪：制造 pgfault 每秒 2.5 万的背景负载，
#   `while read < /proc/vmstat` 连读 200 次，**异常 0 次**。所以那个解释不成立。
#
#   在查清之前采取的对策是"不依赖机制也能守住正确性"：
#     ① 每次采样后校验想要的键**一个不少**，缺了就报错并把该批标记为不可信；
#     ② 逐批增量做**符号与恒等式校验**（不许负数；stall 应等于 success+fail）；
#     ③ 全程总量**另外从快照绝对值算一遍**，与逐批累加互相对账，不一致时以快照为准。
#   —— 观测工具的底线不是"永不出错"，是"出错时不许伪装成正确"。
snap_vmstat() {   # $1 = B（施压前） | A（施压后）；返回非 0 表示本次采样不可信
    local k v missing=""
    if [ "$1" = "B" ]; then VM_B=(); else VM_A=(); fi
    while read -r k v; do
        [ -n "$k" ] || continue
        [ -n "${v:-}" ] || continue
        if [ "$1" = "B" ]; then VM_B["$k"]=$v; else VM_A["$k"]=$v; fi
    done < <(awk -v want=" $VM_KEYS " 'index(want, " "$1" ") > 0 { print $1, $2 }' /proc/vmstat)

    for k in $VM_KEYS; do
        if [ "$1" = "B" ]; then
            [ -n "${VM_B[$k]:-}" ] || missing="$missing $k"
        else
            [ -n "${VM_A[$k]:-}" ] || missing="$missing $k"
        fi
    done
    if [ -n "$missing" ]; then
        echo "  ！采样($1)缺键:$missing —— 本批数据不可信"
        return 1
    fi
    return 0
}

# 取某个计数器的增量（缺 key 一律按 0 算，绝不让算术展开炸掉循环）
vm_delta() {   # $1 = 计数器名
    echo $(( ${VM_A[$1]:-0} - ${VM_B[$1]:-0} ))
}

# 取 PSI memory 的 some/full 累计微秒数。
# 读不到时回 0 —— 否则 $(( 空 - 空 )) 会抛算术错误，同样能弄死循环。
psi_total() {   # $1 = some|full
    local v
    v=$(awk -v want="$1" '$1==want { for (i=2;i<=NF;i++) if ($i ~ /^total=/) { sub("total=","",$i); print $i } }' \
        /proc/pressure/memory 2>/dev/null)
    echo "${v:-0}"
}

# 完整快照
full_snap() {   # $1 = 标签
    local f="$OUTDIR/snap_$1.txt"
    {
        echo "########## 快照: $1   时间: $(date '+%F %T') ##########"
        echo "===== buddyinfo ====="
        echo "        order:      0     1     2     3     4     5     6     7     8     9    10"
        cat /proc/buddyinfo
        echo; echo "===== pagetypeinfo ====="
        cat /proc/pagetypeinfo
        echo; echo "===== vmstat（关键项）====="
        grep -E "^(compact_|allocstall|pgscan_direct|pgsteal_direct|thp_|pgmigrate|nr_free_pages)" /proc/vmstat
        echo; echo "===== meminfo ====="
        grep -E "MemTotal|MemFree|MemAvailable|Cached|KernelStack|^Slab|SReclaimable|SUnreclaim|VmallocUsed|HugePages|AnonHugePages" /proc/meminfo
        echo; echo "===== PSI memory ====="
        cat /proc/pressure/memory
    } > "$f"
    echo "  [快照] $1 -> $f"
}

# 轮询等待某个日志文件里出现标记串
wait_marker() {  # $1=日志 $2=标记 $3=超时秒 $4=描述
    local i=0
    while [ "$i" -lt "$3" ]; do
        grep -q "$2" "$1" 2>/dev/null && return 0
        sleep 1
        i=$((i + 1))
        [ $((i % 15)) -eq 0 ] && echo "  ... 等待$4（已 ${i}s）"
    done
    echo "  ！超时 ${3}s 仍未等到「$2」，继续往下走（结果可能不准，报告里要写明）"
    return 1
}

banner() { echo; echo "================ $* ================"; }

# ---------------------------------------------------------------- 开场

banner "fragstress 压力实验"
echo "产物目录 : $OUTDIR"
echo "参数     : kstack=$KSTACK_N 线程 | holes=${HOLES_MB}MB/${HOLES_KB}KB/释放${HOLES_PCT}% | hugetlb 目标=${HP_TARGET}(步长${HP_STEP})"
echo "内存预算 : 常驻约 $((KSTACK_N * 16 / 1024 + KSTACK_N * 7 / 1024 + HOLES_MB * (100 - HOLES_PCT) / 100 + HP_TARGET * 2)) MB / 总共 $(awk '/^MemTotal:/{print int($2/1024)}' /proc/meminfo) MB"
echo

# 先把上一轮可能残留的状态清掉
echo 0 > /proc/sys/vm/nr_hugepages
pkill -INT -x holes  2>/dev/null && echo "清掉了上一轮残留的 holes"
pkill -INT -x kstack 2>/dev/null && echo "清掉了上一轮残留的 kstack"
sleep 2

full_snap "A_基线"

# ---------------------------------------------------------------- 第 1 步：kstack

banner "第 1 步：kstack —— 铺 UNMOVABLE 污染（$KSTACK_N 线程）"
./kstack "$KSTACK_N" 64 > "$OUTDIR/kstack.log" 2>&1 &
KSTACK_PID=$!
echo "  kstack pid=$KSTACK_PID，日志 $OUTDIR/kstack.log"
wait_marker "$OUTDIR/kstack.log" "实际创建" 180 "kstack 铺完"
grep -E "实际创建|KernelStack|SUnreclaim|VmallocUsed" "$OUTDIR/kstack.log" | tail -8
full_snap "B_kstack后"

# ---------------------------------------------------------------- 第 2 步：holes

banner "第 2 步：holes —— 砸碎连续块（${HOLES_MB}MB）"
./holes "$HOLES_MB" "$HOLES_KB" "$HOLES_PCT" > "$OUTDIR/holes.log" 2>&1 &
HOLES_PID=$!
echo "  holes pid=$HOLES_PID，日志 $OUTDIR/holes.log"
wait_marker "$OUTDIR/holes.log" "随机挖洞完成" 300 "holes 挖完洞"
grep -E "触碰完成|随机挖洞完成" "$OUTDIR/holes.log"
echo "  --- 挖洞后 buddyinfo ---"
echo "        order:      0     1     2     3     4     5     6     7     8     9    10"
cat /proc/buddyinfo
full_snap "C_holes后"

# ---------------------------------------------------------------- 第 3 步：hugetlb 递增申请

banner "第 3 步：hugetlb —— 逐批要大页，观察慢路径何时被逼出来"

# ---- ★ 库存自检：先算清楚"不用规整就能白拿多少大页" ----
#
# 这一步是 2026-08-09 第一次自动化实验失败后加的。那次失败的全过程：
#   目标定 2000 个大页，结果 compact_stall 全程 0，一次规整都没发生。
#   查快照发现 DMA32 那一行在 A/B/C 三个时间点**逐字不变**——holes 压根没碰它。
#
# 原因：内核给用户匿名页分配内存时**优先从最高的 zone 拿**（Normal），
#       只有 Normal 快见底才 fallback 到 DMA32。holes 只要 4GB，
#       而当时 MemFree 还有 5.9GB，Normal 从没紧张过，
#       所以 DMA32 的 705 个 order-10 完整块（≈1410 个大页）一直闲着。
#
# 换算一下当时的"白拿库存"：Normal 633 + DMA32 1449 + DMA 5 ≈ 2087 个。
# 我们要 2000 —— **正好够，一次规整都不用做**。差 88 个就跨过门槛了。
#
# 结论：光加大 holes 治不了本，它天然够不着 DMA32。
#       正确做法是**先算库存，再把目标顶到库存之上**。
#
# 换算规则：order-9 块 = 1 个 2MB 大页；order-10 块 = 4MB = 2 个大页。
#           buddyinfo 每行第 14、15 列分别是 order-9、order-10 的块数。
INVENTORY=$(awk '/^Node/ { n += $14 + 2*$15 } END { print n+0 }' /proc/buddyinfo)

# 目标至少要比库存高 40%，否则全从空闲链表摘走，测不到任何慢路径
NEED=$(( INVENTORY * 14 / 10 + 200 ))
EFFECTIVE=$HP_TARGET
if [ "$EFFECTIVE" -lt "$NEED" ]; then
    EFFECTIVE=$NEED
fi

# 但也不能把机器撑爆：留 800MB 余量，其余才允许拿去做大页
AVAIL_MB=$(awk '/^MemAvailable:/{print int($2/1024)}' /proc/meminfo)
CAP=$(( (AVAIL_MB - 800) / 2 ))
[ "$CAP" -lt 100 ] && CAP=100
if [ "$EFFECTIVE" -gt "$CAP" ]; then
    EFFECTIVE=$CAP
fi

echo "  空闲链表里现成的高阶块 → 可白拿约 $INVENTORY 个大页（不触发任何规整）"
echo "  为越过这条线，目标至少需要 $NEED 个"
echo "  内存上限允许 $CAP 个（MemAvailable ${AVAIL_MB}MB，留 800MB 余量）"
if [ "$EFFECTIVE" -ne "$HP_TARGET" ]; then
    echo "  → 目标从 $HP_TARGET 自动调整为 $EFFECTIVE"
else
    echo "  → 目标保持 $HP_TARGET"
fi
if [ "$EFFECTIVE" -le "$INVENTORY" ]; then
    echo "  ！警告：内存上限低于库存，本轮很可能测不到规整。"
    echo "    建议加大 HOLES_MB 把 MemAvailable 压下去，或换台内存更小的机器。"
fi
HP_TARGET=$EFFECTIVE
echo

# 第一批的"起跳点"：库存的 90%。低于这个数不可能触发规整，没必要慢慢爬。
JUMP=$(( INVENTORY * 9 / 10 ))
[ "$JUMP" -lt "$HP_STEP" ] && JUMP=$HP_STEP
echo "  第一批直接跳到 $JUMP（库存的 90%），之后每批 +$HP_STEP"
echo

# 拿到足够证据就提前收工，不必把机器榨到 OOM
STALL_GOAL=${STALL_GOAL:-300}
cum_stall=0
batches_with_stall=0
bad_rows=0
stop_reason="达到目标数量 $HP_TARGET"

# ★ 权威基线：递增开始前把绝对值单独存一份。
# 全程总量以"结束绝对值 − 这份基线"为准，再和逐批累加互相对账。
declare -A RAMP0
while read -r k v; do RAMP0["$k"]=$v; done \
    < <(awk -v want=" $VM_KEYS " 'index(want, " "$1" ") > 0 { print $1, $2 }' /proc/vmstat)
RAMP0_PSI_SOME=$(psi_total some)
RAMP0_PSI_FULL=$(psi_total full)

echo "batch,target,got,secs,compact_stall,compact_success,compact_fail,compact_daemon_wake,migrate_scanned,free_scanned,pgscan_direct,pgsteal_direct,psi_some_us,psi_full_us" \
    > "$OUTDIR/batches.csv"

printf "%-6s %-7s %-7s %-8s %-8s %-8s %-8s %-9s %-12s %-12s %-10s\n" \
       "批次" "目标" "实得" "耗时s" "stall" "success" "fail" "kcompactd" "migr_scan" "free_scan" "psi_some_ms"
echo "-------------------------------------------------------------------------------------------------------------"

batch=0
cur=$(cat /proc/sys/vm/nr_hugepages)

while [ "$cur" -lt "$HP_TARGET" ]; do
    batch=$((batch + 1))
    next=$((cur + HP_STEP))
    # 第一批直接跳到库存的 90%：这一段全是从空闲链表白拿的，
    # 逐批 200 个爬过去纯属浪费时间，也把表格里真正有信息量的行冲淡了。
    # 小步长留给"越过库存线之后"那一段 —— 规整就发生在那里。
    if [ "$batch" -eq 1 ] && [ "$JUMP" -gt "$next" ]; then next=$JUMP; fi
    if [ "$next" -gt "$HP_TARGET" ]; then next=$HP_TARGET; fi

    sample_ok=1
    snap_vmstat B || sample_ok=0
    psi_s_b=$(psi_total some); psi_f_b=$(psi_total full)
    t0=$(date +%s.%N)

    echo "$next" > /proc/sys/vm/nr_hugepages

    t1=$(date +%s.%N)
    snap_vmstat A || sample_ok=0
    psi_s_a=$(psi_total some); psi_f_a=$(psi_total full)
    got=$(cat /proc/sys/vm/nr_hugepages)

    secs=$(awk -v a="$t0" -v b="$t1" 'BEGIN{printf "%.2f", b-a}')

    d_stall=$(vm_delta compact_stall);          d_succ=$(vm_delta compact_success)
    d_fail=$(vm_delta compact_fail);            d_wake=$(vm_delta compact_daemon_wake)
    d_ms=$(vm_delta compact_migrate_scanned);   d_fs=$(vm_delta compact_free_scanned)
    d_pgs=$(vm_delta pgscan_direct);            d_pgt=$(vm_delta pgsteal_direct)
    d_psis=$(( ${psi_s_a:-0} - ${psi_s_b:-0} )); d_psif=$(( ${psi_f_a:-0} - ${psi_f_b:-0} ))

    # ---- 逐批自检：单调计数器不许出负数；stall 应当等于 success + fail ----
    note=""
    for one in "$d_stall" "$d_succ" "$d_fail" "$d_wake" "$d_ms" "$d_fs" "$d_pgs" "$d_pgt"; do
        [ "$one" -lt 0 ] && note="负增量!"
    done
    if [ $((d_succ + d_fail)) -ne "$d_stall" ]; then
        note="${note}stall≠成功+失败"
    fi
    [ "$sample_ok" -eq 0 ] && note="${note} 采样缺键"
    if [ -n "$note" ]; then
        bad_rows=$((bad_rows + 1))
        note="  ← ★可疑: $note"
    fi

    printf "%-6s %-7s %-7s %-8s %-8s %-8s %-8s %-9s %-12s %-12s %-10s%s\n" \
           "$batch" "$next" "$got" "$secs" "$d_stall" "$d_succ" "$d_fail" "$d_wake" \
           "$d_ms" "$d_fs" "$((d_psis / 1000))" "$note"

    echo "$batch,$next,$got,$secs,$d_stall,$d_succ,$d_fail,$d_wake,$d_ms,$d_fs,$d_pgs,$d_pgt,$d_psis,$d_psif" \
        >> "$OUTDIR/batches.csv"

    cum_stall=$((cum_stall + d_stall))
    [ "$d_stall" -gt 0 ] && batches_with_stall=$((batches_with_stall + 1))

    # ★ 先更新 cur 再判断退出。
    #   原来 `cur=$got` 写在两个 break 之后，从 STALL_GOAL 那条路径退出时
    #   cur 还是上一批的旧值，退出诊断打印出 "cur=1867" 而实际已经是 2067 —— 误报。
    prev=$cur
    cur=$got

    if [ "$got" -eq "$prev" ]; then
        stop_reason="这一批一个都没拿到，内存榨干"
        echo "  ！$stop_reason，提前收工。"
        break
    fi
    # 证据够了就停：既省时间，也避免继续榨到 OOM 把压力源自己搞死
    if [ "$cum_stall" -ge "$STALL_GOAL" ] && [ "$batches_with_stall" -ge 3 ]; then
        stop_reason="已累计 $cum_stall 次 direct compaction（跨 $batches_with_stall 批），证据充分"
        echo "  ✓ $stop_reason，提前收工。"
        break
    fi
done

# ★ 循环退出必须自证：上一轮有过"循环在第 10 批悄悄终止"的事故，
#   当时日志里只有一句 "停止原因：达到目标数量"，而实际只跑到 1800/2791。
#   所以这里把退出时的真实状态打出来，任何"提前退出"都藏不住。
echo "停止原因：$stop_reason"
echo "  循环退出时状态：已跑 $batch 批 | 当前大页数 cur=$cur | 目标 HP_TARGET=$HP_TARGET | 库存基线 INVENTORY=$INVENTORY"
if [ "$cur" -lt "$HP_TARGET" ] && [ "$cum_stall" -lt "$STALL_GOAL" ]; then
    echo "  ！异常：既没到目标、也没拿到足够 stall 证据就退出了 —— 说明循环被什么东西打断了，查上面的报错"
fi
if [ "$cur" -le "$INVENTORY" ]; then
    echo "  ！注意：cur($cur) 还没超过库存($INVENTORY)，本轮压根没越过'必须规整'那条线，"
    echo "    compact_stall=0 属于预期而非失败。"
fi

full_snap "D_hugetlb后"

# ---------------------------------------------------------------- 结果判定

banner "结果"

# 压力源是不是全程都活着？死了的话数据要打折扣，必须如实说
alive_note=""
kill -0 "$KSTACK_PID" 2>/dev/null || alive_note="${alive_note}  ！kstack 中途死了（可能被 OOM killer 杀了），污染在实验后段是失效的\n"
kill -0 "$HOLES_PID"  2>/dev/null || alive_note="${alive_note}  ！holes 中途死了（可能被 OOM killer 杀了），碎片在实验后段是失效的\n"
if [ -n "$alive_note" ]; then
    echo "压力源存活情况："
    printf "%b" "$alive_note"
    echo "  → 报告里必须写明这一点，不能当作干净数据用"
    dmesg 2>/dev/null | grep -i -E "killed process|out of memory" | tail -5
else
    echo "压力源存活情况：kstack 与 holes 全程存活 ✓"
fi
echo

# ============================================================================
# ★ 权威总量：直接用"递增结束的绝对值 − 递增开始的绝对值"。
# 这一段不依赖逐批采样，所以逐批数据出问题时它依然可信。
# ============================================================================
declare -A RAMP1
while read -r k v; do RAMP1["$k"]=$v; done \
    < <(awk -v want=" $VM_KEYS " 'index(want, " "$1" ") > 0 { print $1, $2 }' /proc/vmstat)

rd() { echo $(( ${RAMP1[$1]:-0} - ${RAMP0[$1]:-0} )); }

A_STALL=$(rd compact_stall);  A_SUCC=$(rd compact_success); A_FAIL=$(rd compact_fail)
A_WAKE=$(rd compact_daemon_wake)
A_MS=$(rd compact_migrate_scanned); A_FS=$(rd compact_free_scanned)
A_ISO=$(rd compact_isolated)
A_PGS=$(rd pgscan_direct); A_PGT=$(rd pgsteal_direct); A_ALLOC=$(rd allocstall_movable)
A_PSIS=$(( $(psi_total some) - RAMP0_PSI_SOME ))
A_PSIF=$(( $(psi_total full) - RAMP0_PSI_FULL ))

echo "★ 权威总量（递增结束绝对值 − 递增开始绝对值，不依赖逐批采样）："
printf "  %-26s %s\n" compact_stall "$A_STALL"
printf "  %-26s %s\n" compact_success "$A_SUCC"
printf "  %-26s %s\n" compact_fail "$A_FAIL"
printf "  %-26s %s\n" compact_daemon_wake "$A_WAKE"
printf "  %-26s %s\n" compact_isolated "$A_ISO"
printf "  %-26s %s\n" compact_migrate_scanned "$A_MS"
printf "  %-26s %s\n" compact_free_scanned "$A_FS"
printf "  %-26s %s\n" pgscan_direct "$A_PGS"
printf "  %-26s %s\n" pgsteal_direct "$A_PGT"
printf "  %-26s %s\n" allocstall_movable "$A_ALLOC"
printf "  %-26s %s ms / %s ms\n" "PSI some / full" "$((A_PSIS / 1000))" "$((A_PSIF / 1000))"
echo
# 恒等式自检：一次 direct compaction 要么成功要么失败，两者之和必须等于 stall
if [ $((A_SUCC + A_FAIL)) -eq "$A_STALL" ]; then
    echo "  ✓ 恒等式成立：stall($A_STALL) = success($A_SUCC) + fail($A_FAIL)"
else
    echo "  ！恒等式不成立：stall($A_STALL) ≠ success($A_SUCC)+fail($A_FAIL) —— 需要解释，不要当干净数据用"
fi
if [ $((A_SUCC + A_FAIL)) -gt 0 ]; then
    echo "  规整成功率 = $A_SUCC/$((A_SUCC + A_FAIL)) = $(awk -v s="$A_SUCC" -v n="$((A_SUCC + A_FAIL))" 'BEGIN{printf "%.1f", 100*s/n}')%"
fi
# ★ 扫描比的解读必须跟着数值走。
#   上一轮这里写死了"远大于 1 说明…"，结果实测 0.05 也照样打印那句话 —— 工具在骗人。
if [ "$A_MS" -gt 0 ]; then
    RATIO=$(awk -v f="$A_FS" -v m="$A_MS" 'BEGIN{printf "%.2f", f/m}')
    echo -n "  free/migrate 扫描比 = $A_FS/$A_MS = $RATIO  "
    awk -v r="$RATIO" 'BEGIN{
        if (r > 2)        print "（>2：空闲侧要翻很远才找得到落脚点）";
        else if (r < 0.5) print "（<0.5：迁移侧要翻很远才找得到搬得动的页 —— UNMOVABLE 占比高）";
        else              print "（0.5~2：两侧扫描量接近，双扫描器大致对称相遇）";
    }'
fi
if [ "$A_PGS" -gt 0 ]; then
    echo "  直接回收效率 steal/scan = $A_PGT/$A_PGS = $(awk -v s="$A_PGT" -v n="$A_PGS" 'BEGIN{printf "%.1f", 100*s/n}')%"
fi
echo
if [ "$bad_rows" -gt 0 ]; then
    echo "  ！逐批表里有 $bad_rows 行被标记为可疑（负增量 / 恒等式不成立 / 采样缺键）"
    echo "    → 这些行的数值不要写进报告；总量请用上面这段权威值"
fi
echo
if [ "$A_STALL" -gt 0 ]; then
    echo "  ✓ 硬门槛通过：本轮真实触发了 $A_STALL 次 direct compaction"
else
    echo "  ✗ 硬门槛未过：本轮 compact_stall 增量为 0"
    echo "    → 大页全是从空闲链表直接摘走的。加大 HOLES_MB，或提高 HP_TARGET 越过库存线。"
fi
echo
echo "逐批累加（仅供与上面对账；不一致时以权威总量为准）："
if ! command -v python3 >/dev/null 2>&1; then
    echo "  （python3 不可用，直接看 batches.csv）"
else
python3 - "$OUTDIR/batches.csv" <<'PY'
import csv, sys
rows = list(csv.DictReader(open(sys.argv[1])))
if not rows:
    print("  没有任何批次数据")
    sys.exit()
keys = ["compact_stall","compact_success","compact_fail","compact_daemon_wake",
        "migrate_scanned","free_scanned","pgscan_direct","pgsteal_direct"]
tot = {k: sum(int(r[k]) for r in rows) for k in keys}
for k in keys:
    print(f"  {k:<24} {tot[k]}")
n = tot["compact_success"] + tot["compact_fail"]
if n:
    print(f"  {'规整成功率':<20} {tot['compact_success']}/{n} = {100*tot['compact_success']/n:.1f}%")
if tot["migrate_scanned"]:
    print(f"  {'free/migrate 扫描比':<19} {tot['free_scanned']/tot['migrate_scanned']:.2f}  "
          f"（远大于 1 说明空闲侧要翻很远才找得到落脚点 = 碎得厉害）")
print()
if tot["compact_stall"] > 0:
    print("  ✓ 硬门槛通过：compact_stall 被顶起来了")
    grow = sum(1 for r in rows if int(r["compact_stall"]) > 0)
    print(f"  ✓ {grow}/{len(rows)} 批出现了 direct compaction（持续增长，不是只涨一次）")
else:
    print("  ✗ 硬门槛未过：compact_stall 全程为 0")
    print("    → 大页全是从空闲链表直接摘走的。加大 HOLES_MB 或 HP_TARGET 再来一次。")
PY
fi

echo
echo "逐批明细: $OUTDIR/batches.csv"
echo "四个快照: $OUTDIR/snap_A_基线.txt  snap_B_kstack后.txt  snap_C_holes后.txt  snap_D_hugetlb后.txt"
