/*
 * memhog.c —— fragstress 档位 5：direct reclaim 触发器（P1 的前置硬门槛）
 *
 * ============================ 为什么需要它 ============================
 *
 * P0 那轮实验的数据里有一个刺眼的组合：
 *
 *     compact_stall  = 377        ← 直接规整发生了 377 次
 *     pgscan_direct  = 0          ← 直接回收一次都没发生
 *     pgsteal_direct = 0
 *
 * 也就是说：**现有的压力形态（hugetlb 抢 order-9）根本触发不了直接回收。**
 *
 * 根因在 mm/page_alloc.c 的慢路径里（v5.15.178 行号）：
 *
 *     :5013   if (can_direct_reclaim &&
 *                 (costly_order || (order > 0 && migratetype != MOVABLE)))
 *     :5017       __alloc_pages_direct_compact(...)   ← 进 retry 循环之前的"提前规整"
 *                 ...拿到页就 goto got_pg
 *
 *     :5058   retry:                                   ← 真正的 retry 循环
 *     :5092       __alloc_pages_direct_reclaim(...)    ← 直接回收在这里，从没被调用
 *
 * order-9（2MB 大页）是 costly order，走的是 :5013 那一发提前规整，
 * 拿到页就直接返回了，**根本没进 retry 循环**。
 *
 * 所以 P1 如果不换压力形态，reclaiminfo 的 map 一定是空的 ——
 * 而空 map 和坏探针长得一模一样（P0 已经吃过一次这个亏）。
 *
 * ============================ 它在内核里干了什么 ============================
 *
 * 手法是最朴素的一种：**用比 kswapd 回收更快的速度申请并写脏匿名内存**。
 *
 *   1) mmap 一块匿名内存（MAP_NORESERVE，只占虚拟地址）
 *   2) memset 整块 → 逐页缺页，真正从伙伴系统摘走物理页
 *   3) 多线程并行做 1)+2)，把分配速率顶到 kswapd 追不上
 *
 * 内核侧会依次发生：
 *
 *   free 跌破 zone 的 low 水位  → 唤醒 kswapd（后台回收，计 pgscan_kswapd）
 *   free 继续跌破 min 水位      → 申请者自己被拉去回收，走
 *                                 __alloc_pages_direct_reclaim() (:5092)
 *                                 → __perform_reclaim() (:4643)
 *                                 → try_to_free_pages() (vmscan.c:3540)
 *                                 → 计 pgscan_direct / pgsteal_direct
 *
 * **注意"kswapd 追不上"才是关键**：如果申请得慢，kswapd 一个人就把水位补回去了，
 * 全程只有 pgscan_kswapd 在涨，pgscan_direct 依然是 0。所以这个程序是多线程的，
 * 而且默认不 sleep —— 它要的就是"抢在 kswapd 前面把水位踩穿"。
 *
 * ============================ ★ 一个必须避开的陷阱 ============================
 *
 * **不要用 cgroup 的 memory.max 来限制它。**
 *
 * 直觉上"把 hog 关进一个 memcg 里限住内存"更安全，但 mm/vmscan.c:2213 写着：
 *
 *     item = current_is_kswapd() ? PGSCAN_KSWAPD : PGSCAN_DIRECT;
 *     if (!cgroup_reclaim(sc))
 *         __count_vm_events(item, nr_scanned);       ← cgroup 触发的回收不计这里
 *     __count_memcg_events(lruvec_memcg(lruvec), item, nr_scanned);
 *
 * **cgroup 内触发的回收不会增加全局 pgscan_direct/pgsteal_direct。**
 * 那样一来 /proc/vmstat 这条交叉验证线就整条失效了 —— 而交叉验证正是本项目
 * 全部可信度的来源。所以这里必须用**全局压力**。
 *
 * 代价是有 OOM 风险，用三道保险顶住（见下）。
 *
 * ============================ 三道安全保险 ============================
 *
 *   1) 开机就把自己的 oom_score_adj 设成 1000
 *      → 万一真触发 OOM killer，**第一个被杀的是本程序**，不是用户的桌面/ssh。
 *        （提高自己的分数不需要 root。）
 *   2) --goal：一旦 pgscan_direct 达标就立刻停手并释放
 *      → 正常情况下远在耗尽内存之前就退出了。
 *   3) --floor：MemAvailable 低于这个值（默认 400 MB）就无条件停手
 *      → 就算 goal 没达到也不再往下压。
 *
 * 另外 SIGINT/SIGTERM 会走正常退出路径，把内存还回去再打印统计。
 *
 * ============================ 用法 ============================
 *
 *   ./memhog                       # 默认：4 线程，最多吃 8 GB，pgscan_direct>0 即停
 *   ./memhog --gb 6 --threads 8    # 顶多吃 6 GB，8 线程（分配更猛）
 *   ./memhog --goal 100000         # 要求 pgscan_direct 至少涨到 10 万才停
 *   ./memhog --hold 60             # 达标后再维持 60 秒（给 eBPF 探针留观测窗口）
 *
 * **不需要 root。** 需要 root 的只有挂 eBPF 探针那一侧。
 *
 * ★ 配合 P1 观测时的正确时序（顺序不能颠倒，P0 踩过）：
 *      1. 先挂探针，等它编译加载完
 *      2. 再跑 memhog
 *      3. 结束后把日志拷出 /tmp
 */

#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <unistd.h>
#include <errno.h>
#include <signal.h>
#include <pthread.h>
#include <sys/mman.h>
#include <time.h>

/* ---------------- 可调参数（命令行覆盖） ---------------- */
static long   opt_cap_gb   = 8;      /* 最多吃多少 GB */
static int    opt_threads  = 4;      /* 分配线程数 */
static long   opt_chunk_mb = 64;     /* 每次 mmap 的块大小 */
static long   opt_goal     = 1;      /* pgscan_direct 增量达到多少就停 */
static long   opt_floor_mb = 400;    /* MemAvailable 低于此值无条件停手 */
static int    opt_hold_s   = 0;      /* 达标后再维持多少秒 */

/* ---------------- 全局状态 ---------------- */
#define MAX_CHUNKS 65536
static void  *chunks[MAX_CHUNKS];
static size_t chunk_sz[MAX_CHUNKS];
static int    n_chunks = 0;
static pthread_mutex_t chunk_lock = PTHREAD_MUTEX_INITIALIZER;

static volatile sig_atomic_t stop_flag = 0;   /* 1 = 所有线程停止分配 */
static const char *stop_reason = "未知";

static void on_signal(int sig)
{
	(void)sig;
	stop_flag = 1;
	stop_reason = "收到中断信号";
}

/* ---------------- /proc 读取小工具 ---------------- */

/*
 * 从 /proc/vmstat 之类的 "key value" 文件里取一个数。
 * 取不到返回 -1（★ 不返回 0：0 和"没读到"必须区分得开，
 *   P-1 阶段就是因为把"没读到"当成 0 才谎报了一次）。
 */
static long read_kv(const char *path, const char *key)
{
	FILE *f = fopen(path, "r");
	if (!f)
		return -1;

	char line[256];
	size_t klen = strlen(key);
	long val = -1;

	while (fgets(line, sizeof(line), f)) {
		if (strncmp(line, key, klen) == 0 &&
		    (line[klen] == ' ' || line[klen] == ':')) {
			val = strtol(line + klen + 1, NULL, 10);
			break;
		}
	}
	fclose(f);
	return val;
}

static long vmstat(const char *key)   { return read_kv("/proc/vmstat", key); }
static long meminfo_kb(const char *key) { return read_kv("/proc/meminfo", key); }

static double now_s(void)
{
	struct timespec ts;
	clock_gettime(CLOCK_MONOTONIC, &ts);
	return ts.tv_sec + ts.tv_nsec / 1e9;
}

/* 把自己标成 OOM killer 的头号目标 —— 保险 1 */
static void mark_self_as_oom_victim(void)
{
	FILE *f = fopen("/proc/self/oom_score_adj", "w");
	if (!f) {
		fprintf(stderr, "警告：写不了 oom_score_adj，OOM 保险失效\n");
		return;
	}
	fprintf(f, "1000\n");
	fclose(f);
}

/* ---------------- 分配线程 ---------------- */

static void *worker(void *arg)
{
	(void)arg;
	size_t chunk = (size_t)opt_chunk_mb * 1024 * 1024;

	while (!stop_flag) {
		void *p = mmap(NULL, chunk, PROT_READ | PROT_WRITE,
			       MAP_PRIVATE | MAP_ANONYMOUS | MAP_NORESERVE, -1, 0);
		if (p == MAP_FAILED) {
			stop_flag = 1;
			stop_reason = "mmap 失败（虚拟地址或 overcommit 限制）";
			break;
		}

		/* 真正写脏，逼出缺页 —— 只 mmap 不写是拿不到物理页的 */
		memset(p, 0xA5, chunk);

		pthread_mutex_lock(&chunk_lock);
		if (n_chunks < MAX_CHUNKS) {
			chunks[n_chunks]  = p;
			chunk_sz[n_chunks] = chunk;
			n_chunks++;
		} else {
			munmap(p, chunk);
			stop_flag = 1;
			stop_reason = "块表满";
		}
		pthread_mutex_unlock(&chunk_lock);
	}
	return NULL;
}

/*
 * 维持期：**边还边要**，把总占用维持在水位线附近不动，
 * 但持续制造新的分配请求。
 *
 * ★ 2026-08-15 实测教训（第一版写错了，必须记住）：
 *
 * 第一版的维持期是"反复重写已有的页"（只 touch 不新增）。
 * 实测那 20 秒里 pgscan_direct 从 3691 **一格没动** ——
 * 重写的是**已经在内存里的页，根本不产生分配**，自然不会触发回收。
 *
 * 结果就是：eBPF 探针挂上去，观测窗口里一个事件都收不到。
 *
 * > **要观测分配路径，就必须持续制造"分配"这个动作本身，
 * >   而不是制造"内存占用"这个状态。**
 *
 * 所以改成：munmap 掉一块，立刻 mmap+memset 一块新的。
 * 总占用不变（不会越压越深、不会 OOM），但每一轮都是**全新的缺页**，
 * 而且是在"free 已经贴着水位线"的前提下发生的 —— 每次都很可能踩进直接回收。
 */
static volatile unsigned long recycle_cursor = 0;
static volatile unsigned long recycle_count  = 0;

static void *recycler(void *arg)
{
	(void)arg;
	while (!stop_flag) {
		pthread_mutex_lock(&chunk_lock);
		if (n_chunks == 0) {
			pthread_mutex_unlock(&chunk_lock);
			break;
		}
		int i = (int)(recycle_cursor++ % (unsigned long)n_chunks);
		void  *old = chunks[i];
		size_t sz  = chunk_sz[i];
		chunks[i]  = NULL;          /* 占位，防止别的线程同时挑中它 */
		pthread_mutex_unlock(&chunk_lock);

		if (!old) {                 /* 撞上了别的线程正在换的块，跳过 */
			usleep(1000);
			continue;
		}

		munmap(old, sz);            /* 先还 —— 总占用短暂下降一个块 */

		void *fresh = mmap(NULL, sz, PROT_READ | PROT_WRITE,
				   MAP_PRIVATE | MAP_ANONYMOUS | MAP_NORESERVE, -1, 0);
		if (fresh == MAP_FAILED) {
			pthread_mutex_lock(&chunk_lock);
			chunk_sz[i] = 0;
			pthread_mutex_unlock(&chunk_lock);
			stop_flag = 1;
			stop_reason = "维持期 mmap 失败";
			break;
		}
		memset(fresh, 0x5A, sz);    /* 再要 —— 全新缺页，这才是我们要的分配动作 */

		pthread_mutex_lock(&chunk_lock);
		chunks[i] = fresh;
		recycle_count++;
		pthread_mutex_unlock(&chunk_lock);
	}
	return NULL;
}

/* ---------------- 主流程 ---------------- */

static void usage(const char *me)
{
	fprintf(stderr,
		"用法: %s [选项]\n"
		"  --gb N        最多吃多少 GB（默认 %ld）\n"
		"  --threads N   分配线程数（默认 %d）\n"
		"  --chunk MB    每次 mmap 的块大小（默认 %ld）\n"
		"  --goal N      pgscan_direct 增量达到 N 就停（默认 %ld）\n"
		"  --floor MB    MemAvailable 低于此值无条件停手（默认 %ld）\n"
		"  --hold S      达标后再维持 S 秒，给探针留观测窗口（默认 %d）\n",
		me, opt_cap_gb, opt_threads, opt_chunk_mb, opt_goal,
		opt_floor_mb, opt_hold_s);
}

int main(int argc, char **argv)
{
	for (int i = 1; i < argc; i++) {
		if (i + 1 < argc && strcmp(argv[i], "--gb") == 0)
			opt_cap_gb = atol(argv[++i]);
		else if (i + 1 < argc && strcmp(argv[i], "--threads") == 0)
			opt_threads = atoi(argv[++i]);
		else if (i + 1 < argc && strcmp(argv[i], "--chunk") == 0)
			opt_chunk_mb = atol(argv[++i]);
		else if (i + 1 < argc && strcmp(argv[i], "--goal") == 0)
			opt_goal = atol(argv[++i]);
		else if (i + 1 < argc && strcmp(argv[i], "--floor") == 0)
			opt_floor_mb = atol(argv[++i]);
		else if (i + 1 < argc && strcmp(argv[i], "--hold") == 0)
			opt_hold_s = atoi(argv[++i]);
		else {
			usage(argv[0]);
			return 1;
		}
	}

	signal(SIGINT,  on_signal);
	signal(SIGTERM, on_signal);
	mark_self_as_oom_victim();

	/* ---- 基线（★ 一律用"结束值 − 开始值"，绝不信累计值） ---- */
	long base_scan_direct   = vmstat("pgscan_direct");
	long base_steal_direct  = vmstat("pgsteal_direct");
	long base_scan_kswapd   = vmstat("pgscan_kswapd");
	long base_stall_normal  = vmstat("allocstall_normal");
	long base_stall_dma32   = vmstat("allocstall_dma32");
	long base_stall_dma     = vmstat("allocstall_dma");
	long base_stall_movable = vmstat("allocstall_movable");
	long base_compact_stall = vmstat("compact_stall");
	long base_pswpout       = vmstat("pswpout");

	if (base_scan_direct < 0 || base_stall_normal < 0) {
		fprintf(stderr, "错误：/proc/vmstat 里读不到必需的计数器，中止\n");
		return 1;
	}

	printf("=== memhog：direct reclaim 触发器 ===\n");
	printf("上限 %ld GB / %d 线程 / 块 %ld MB / 目标 pgscan_direct 增量 ≥ %ld / "
	       "下限 MemAvailable %ld MB\n",
	       opt_cap_gb, opt_threads, opt_chunk_mb, opt_goal, opt_floor_mb);
	printf("基线：pgscan_direct=%ld pgscan_kswapd=%ld allocstall_normal=%ld\n\n",
	       base_scan_direct, base_scan_kswapd, base_stall_normal);
	printf("%7s %9s %11s %13s %13s %13s\n",
	       "秒", "已吃GB", "MemAvail", "scan_kswapd", "scan_direct", "allocstall");
	fflush(stdout);

	pthread_t th[64];
	if (opt_threads > 64)
		opt_threads = 64;
	for (int i = 0; i < opt_threads; i++)
		pthread_create(&th[i], NULL, worker, NULL);

	double t0 = now_s();
	long cap_bytes = opt_cap_gb * 1024L * 1024L * 1024L;
	double next_print = 0;

	while (!stop_flag) {
		usleep(100 * 1000);

		pthread_mutex_lock(&chunk_lock);
		long allocated = 0;
		for (int i = 0; i < n_chunks; i++)
			allocated += chunk_sz[i];
		pthread_mutex_unlock(&chunk_lock);

		long d_scan   = vmstat("pgscan_direct")     - base_scan_direct;
		long d_kswapd = vmstat("pgscan_kswapd")     - base_scan_kswapd;
		long d_stall  = vmstat("allocstall_normal") - base_stall_normal;
		long avail_mb = meminfo_kb("MemAvailable") / 1024;
		double el = now_s() - t0;

		if (el >= next_print) {
			printf("%7.1f %9.2f %9ld MB %13ld %13ld %13ld\n",
			       el, allocated / 1073741824.0, avail_mb,
			       d_kswapd, d_scan, d_stall);
			fflush(stdout);
			next_print = el + 1.0;
		}

		/* ---- 三个停止条件 ---- */
		if (d_scan >= opt_goal) {
			stop_flag = 1;
			stop_reason = "★ 达标：pgscan_direct 已增长到目标值";
		} else if (avail_mb >= 0 && avail_mb < opt_floor_mb) {
			stop_flag = 1;
			stop_reason = "触到 MemAvailable 下限（保险 3），停手";
		} else if (allocated >= cap_bytes) {
			stop_flag = 1;
			stop_reason = "吃满了 --gb 上限";
		}
	}

	/* ---- 维持期：只重写不新增 ---- */
	if (opt_hold_s > 0) {
		printf("\n--- 进入维持期 %d 秒（边还边要：总占用不变，持续制造新分配）---\n",
		       opt_hold_s);
		fflush(stdout);
		stop_flag = 0;
		pthread_t ch[8];
		int nch = opt_threads > 8 ? 8 : opt_threads;
		for (int i = 0; i < nch; i++)
			pthread_create(&ch[i], NULL, recycler, NULL);

		double h0 = now_s();
		long hold_base_scan = vmstat("pgscan_direct");
		while (now_s() - h0 < opt_hold_s && !stop_flag) {
			usleep(1000 * 1000);
			long d_hold  = vmstat("pgscan_direct") - hold_base_scan;
			long avail_mb = meminfo_kb("MemAvailable") / 1024;
			printf("  维持 %5.1fs  MemAvail=%ld MB  换块 %lu 次  "
			       "维持期内 scan_direct 增量=%ld\n",
			       now_s() - h0, avail_mb, recycle_count, d_hold);
			fflush(stdout);
			/*
			 * ★ 2026-08-15 实测收紧：原来这里写的是 floor/2，
			 * 结果一轮跑下来 MemAvailable 一路掉到 84 MB —— 太深了。
			 * 原因是主压期停手后 kswapd 还有一大批换页积压没做完，
			 * MemAvailable 会**继续下滑一段**，刹车必须提前踩。
			 */
			if (avail_mb >= 0 && avail_mb < opt_floor_mb * 3 / 4) {
				printf("  维持期触到下限的 3/4，提前结束\n");
				break;
			}
		}
		stop_flag = 1;
		for (int i = 0; i < nch; i++)
			pthread_join(ch[i], NULL);
		printf("--- 维持期结束：共换块 %lu 次，期内 pgscan_direct 增量 %ld ---\n",
		       recycle_count, vmstat("pgscan_direct") - hold_base_scan);
	}

	for (int i = 0; i < opt_threads; i++)
		pthread_join(th[i], NULL);

	/* ---- 先把结束值抓下来，再释放内存 ----
	 * 顺序很重要：munmap 之后 MemAvailable 就恢复了，抓晚了看不出压到多深。 */
	long end_scan_direct   = vmstat("pgscan_direct");
	long end_steal_direct  = vmstat("pgsteal_direct");
	long end_scan_kswapd   = vmstat("pgscan_kswapd");
	long end_stall_normal  = vmstat("allocstall_normal");
	long end_stall_dma32   = vmstat("allocstall_dma32");
	long end_stall_dma     = vmstat("allocstall_dma");
	long end_stall_movable = vmstat("allocstall_movable");
	long end_compact_stall = vmstat("compact_stall");
	long end_pswpout       = vmstat("pswpout");
	long min_avail_mb      = meminfo_kb("MemAvailable") / 1024;

	long allocated = 0;
	for (int i = 0; i < n_chunks; i++) {
		if (!chunks[i])          /* 维持期正好换到一半的槽位 */
			continue;
		allocated += chunk_sz[i];
		munmap(chunks[i], chunk_sz[i]);
	}

	printf("\n=== 结束：%s ===\n", stop_reason);
	printf("共吃下 %.2f GB，耗时 %.1f 秒，退出前 MemAvailable = %ld MB\n\n",
	       allocated / 1073741824.0, now_s() - t0, min_avail_mb);

	printf("%-22s %12s\n", "计数器（增量）", "值");
	printf("%-22s %12ld\n", "pgscan_direct",     end_scan_direct   - base_scan_direct);
	printf("%-22s %12ld\n", "pgsteal_direct",    end_steal_direct  - base_steal_direct);
	printf("%-22s %12ld\n", "pgscan_kswapd",     end_scan_kswapd   - base_scan_kswapd);
	printf("%-22s %12ld\n", "allocstall_normal", end_stall_normal  - base_stall_normal);
	printf("%-22s %12ld\n", "allocstall_dma32",  end_stall_dma32   - base_stall_dma32);
	printf("%-22s %12ld\n", "allocstall_dma",    end_stall_dma     - base_stall_dma);
	printf("%-22s %12ld  ★\n", "allocstall_movable",
	       end_stall_movable - base_stall_movable);
	printf("%-22s %12ld\n", "compact_stall",     end_compact_stall - base_compact_stall);
	printf("%-22s %12ld\n", "pswpout（换出页）",  end_pswpout       - base_pswpout);

	long d_scan = end_scan_direct - base_scan_direct;
	printf("\n");
	if (d_scan > 0) {
		printf("★ 硬门槛通过：direct reclaim 被触发了，pgscan_direct 增量 = %ld 页"
		       "（约 %.1f MB）\n", d_scan, d_scan * 4.0 / 1024);
		printf("  P1 的探针有东西可测了。\n");
	} else {
		printf("✗ 硬门槛未过：pgscan_direct 增量为 0 —— kswapd 全程跟得上。\n");
		printf("  下一步试：加大 --threads（分配更猛）、减小 --chunk、或抬高 --gb。\n");
	}

	/*
	 * 自证：allocstall 是"进了几次慢路径回收"，pgscan_direct 是"扫了几页"。
	 * 两者要么同时为 0，要么同时非 0；一个 0 一个非 0 说明读数有问题。
	 *
	 * ★ 2026-08-15 修正：原来只加了 normal + dma32，结果第一次实测就报了假警报。
	 *
	 * 根因在 mm/vmscan.c:3334：
	 *     __count_zid_vm_events(ALLOCSTALL, sc->reclaim_idx, 1);
	 * 计数是按 **sc->reclaim_idx = gfp_zone(gfp_mask)** 分桶的，
	 * 而普通用户态匿名页用的是 GFP_HIGHUSER_MOVABLE → gfp_zone() 返回
	 * **ZONE_MOVABLE**，所以计的是 allocstall_movable。
	 * 哪怕本机 Movable zone 是空的（free=0），桶还是按 gfp 的**意图**分的，
	 * 不是按最后实际从哪个 zone 拿到页分的。
	 *
	 * 教训：**按 zone 分桶的计数器必须四个桶全加**，
	 * 少加一个就会把"正常"读成"异常"。
	 */
	long d_stall = (end_stall_normal  - base_stall_normal) +
		       (end_stall_dma32   - base_stall_dma32)  +
		       (end_stall_dma     - base_stall_dma)    +
		       (end_stall_movable - base_stall_movable);
	if ((d_scan > 0) != (d_stall > 0))
		printf("\n⚠ 自检不一致：pgscan_direct 增量 %ld，但 allocstall 增量 %ld —— "
		       "两者应当同为 0 或同为非 0，数据存疑。\n", d_scan, d_stall);

	return d_scan > 0 ? 0 : 2;
}
