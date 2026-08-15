# fragstress —— 碎片压力注入器
#
# 注意：这里全部是**普通用户态 C 程序，和 eBPF 完全无关**。
# 它们的作用是把机器折腾到内存变碎、逼内核走慢路径，
# 好让 src/bpf/ 里的 eBPF 探针有东西可测。
#
# 用法：
#   make            编译全部
#   make clean      清理
#   make check      看一眼当前碎片状态（不需要 root）

CC      := gcc
CFLAGS  := -O2 -Wall -Wextra -g
LDLIBS_PTHREAD := -lpthread

TARGETS := holes kstack memhog

.PHONY: all clean check

all: $(TARGETS)
	@chmod +x hugetlb.sh run.sh
	@echo "编译完成：$(TARGETS)"
	@echo "脚本已加可执行权限：hugetlb.sh run.sh"
	@echo ""
	@echo "跑完整实验（时序全自动）："
	@echo "  sudo -v      # 先单独输密码，否则后台的 sudo 会被 SIGTTIN 停住"
	@echo "  sudo nohup ./run.sh > /tmp/fragstress_run.log 2>&1 &"
	@echo "  tail -f /tmp/fragstress_run.log"

holes: holes.c
	$(CC) $(CFLAGS) -o $@ $<

kstack: kstack.c
	$(CC) $(CFLAGS) -o $@ $< $(LDLIBS_PTHREAD)

# P1 用：把内存压到 kswapd 追不上，逼出 direct reclaim
memhog: memhog.c
	$(CC) $(CFLAGS) -o $@ $< $(LDLIBS_PTHREAD)

clean:
	rm -f $(TARGETS)

# 快速查看当前碎片状况，跑压力前后各来一次做对比
check:
	@echo "===== /proc/buddyinfo ====="
	@echo "        order:      0     1     2     3     4     5     6     7     8     9    10"
	@cat /proc/buddyinfo
	@echo ""
	@echo "===== 慢路径计数器 /proc/vmstat ====="
	@grep -E "compact_stall|compact_fail|compact_success|compact_daemon_wake|allocstall|pgscan_direct|pgsteal_direct|thp_fault" /proc/vmstat
	@echo ""
	@echo "===== 内核侧内存 /proc/meminfo ====="
	@grep -E "MemFree|MemAvailable|KernelStack|^Slab|SUnreclaim|HugePages_Total|HugePages_Free" /proc/meminfo
	@echo ""
	@echo "===== 内存压力 /proc/pressure/memory ====="
	@cat /proc/pressure/memory
