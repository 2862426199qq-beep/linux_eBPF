/*
 * holes.c —— fragstress 档位 1：外部碎片制造机（俗称"梅花桩"）
 *
 * ============================ 它在内核里干了什么 ============================
 *
 * 目标：制造"空闲页总量很多，但没有一块是连成片的"这种局面 —— 这就是**外部碎片**。
 *
 * 三步：
 *   1) mmap 一大片匿名内存                  → 只占虚拟地址，还没有真正的物理页
 *   2) 逐页写一个字节（touch）               → 触发缺页异常，内核从伙伴系统真正摘走物理页
 *   3) 随机顺序对其中一半做 MADV_DONTNEED    → 把这些物理页还给伙伴系统，但**还回去的位置是散的**
 *
 * 第 3 步的"随机"是这个程序的全部灵魂：
 *   - 顺序释放：还回去的页彼此相邻，伙伴系统的 __free_one_page() 会一路向上合并，
 *     order-0 合成 order-1、order-1 合成 order-2 …… 最后又变回大块。白干。
 *   - 随机释放：还回去的页的"伙伴"（buddy）大概率还被占着，合并在第一步就断了，
 *     于是空闲页只能以小块的形式挂在低 order 的链表上。
 *
 * 验证方法：跑之前跑之后各看一次 /proc/buddyinfo，
 *          高 order（8/9/10）的计数应该显著下降，低 order（0/1/2）显著上升。
 *
 * ============================ 为什么用 MADV_DONTNEED 而不是 munmap ============================
 *
 * 两者都能把物理页还给伙伴系统，但：
 *   - munmap 会把一个 VMA（虚拟内存区域）从中间切开，挖一个洞就多一个 VMA。
 *     内核有上限 /proc/sys/vm/max_map_count（默认 65530），挖几万个洞就会 ENOMEM。
 *   - MADV_DONTNEED 只做 zap_page_range()：解除页表映射、把物理页 put 回伙伴系统，
 *     VMA 本身一个都不动。全程只有 1 个 VMA，想挖多少洞挖多少洞。
 *
 * 副作用要知道：MADV_DONTNEED 过的匿名页再次访问会读到全 0（重新缺页分配零页），
 * 对我们这个压力工具无所谓，但这是它和 munmap 语义上的真实区别。
 *
 * ============================ 用法 ============================
 *
 *   ./holes [总量MB] [块大小KB] [释放百分比] [随机种子]
 *   默认：       4096      64          50         20260809
 *
 * 固定随机种子是为了**可复现**：同样的参数，每次挖出来的洞的位置完全一样。
 * 性能实验最怕"上次能复现这次不行"，所以种子必须能锁定。
 *
 * 跑起来后进程会**一直挂着不退出**（因为没被释放的那一半要继续占着内存，
 * 一退出就全还回去了，碎片当场消失）。按 Ctrl-C 结束。
 */

#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <signal.h>
#include <errno.h>
#include <time.h>
#include <sys/mman.h>

#define PAGE_SIZE 4096UL

static volatile sig_atomic_t g_stop = 0;

static void on_signal(int sig)
{
    (void)sig;
    g_stop = 1;
}

/* 打印 /proc/buddyinfo：这是观察外部碎片最直接的一个文件 */
static void dump_buddyinfo(const char *tag)
{
    FILE *fp = fopen("/proc/buddyinfo", "r");
    char line[512];

    if (!fp) {
        fprintf(stderr, "[holes] 打不开 /proc/buddyinfo: %s\n", strerror(errno));
        return;
    }
    printf("\n===== buddyinfo（%s）=====\n", tag);
    printf("        order:      0     1     2     3     4     5     6     7     8     9    10\n");
    while (fgets(line, sizeof(line), fp))
        fputs(line, stdout);
    printf("（order-9 = 2MB = 一个 pageblock；order-10 = 4MB）\n");
    fclose(fp);
}

/* Fisher-Yates 洗牌：等概率生成一个随机排列，O(n)。
 * 用它决定"按什么顺序挑块出来释放"。 */
static void shuffle(unsigned int *arr, size_t n, unsigned int seed)
{
    size_t i;

    srandom(seed);
    for (i = n - 1; i > 0; i--) {
        /* random() 只有 31 位，用两次拼出足够大的范围，避免 n 很大时取模偏斜 */
        unsigned long r = ((unsigned long)random() << 31) | (unsigned long)random();
        size_t j = r % (i + 1);
        unsigned int tmp = arr[i];

        arr[i] = arr[j];
        arr[j] = tmp;
    }
}

int main(int argc, char **argv)
{
    size_t total_mb   = (argc > 1) ? strtoul(argv[1], NULL, 10) : 4096;
    size_t block_kb   = (argc > 2) ? strtoul(argv[2], NULL, 10) : 64;
    unsigned int pct  = (argc > 3) ? (unsigned int)strtoul(argv[3], NULL, 10) : 50;
    unsigned int seed = (argc > 4) ? (unsigned int)strtoul(argv[4], NULL, 10) : 20260809;

    size_t total_bytes, block_bytes, nblocks, nfree;
    unsigned int *order_arr;
    char *base;
    size_t i;
    struct timespec t0, t1;

    if (total_mb == 0 || block_kb == 0 || pct > 100) {
        fprintf(stderr, "用法: %s [总量MB] [块大小KB] [释放百分比0-100] [随机种子]\n", argv[0]);
        return 1;
    }
    if ((block_kb * 1024) % PAGE_SIZE) {
        fprintf(stderr, "[holes] 块大小必须是 4KB 的整数倍\n");
        return 1;
    }

    total_bytes = total_mb * 1024UL * 1024UL;
    block_bytes = block_kb * 1024UL;
    nblocks     = total_bytes / block_bytes;
    nfree       = nblocks * pct / 100;

    /*
     * ★ 必须改成行缓冲，理由同 kstack.c：
     * stdout 被重定向到文件时 glibc 默认全缓冲，进度和「随机挖洞完成」这个
     * 就绪标记不会落盘，run.sh 的轮询就永远等不到。
     */
    setvbuf(stdout, NULL, _IOLBF, 0);

    signal(SIGINT,  on_signal);
    signal(SIGTERM, on_signal);

    printf("[holes] 计划：申请 %zu MB，切成 %zu 个 %zu KB 的块，随机释放其中 %zu 个（%u%%），种子 %u\n",
           total_mb, nblocks, block_kb, nfree, pct, seed);

    dump_buddyinfo("施压前");

    /* ---- 第 1 步：申请虚拟地址空间 ----
     * 这一步内核只是记一笔 VMA，一个物理页都没分。
     * 所以哪怕申请的量超过物理内存也可能成功（overcommit），真正的分配发生在第 2 步。
     */
    base = mmap(NULL, total_bytes, PROT_READ | PROT_WRITE,
                MAP_PRIVATE | MAP_ANONYMOUS | MAP_NORESERVE, -1, 0);
    if (base == MAP_FAILED) {
        fprintf(stderr, "[holes] mmap %zu MB 失败: %s\n", total_mb, strerror(errno));
        return 1;
    }
    printf("[holes] mmap 成功，虚拟地址 %p ~ %p\n", base, base + total_bytes);

    /* ---- 第 2 步：逐页触碰，逼内核真正分配物理页 ----
     * 每写一个字节 → 缺页异常 → handle_mm_fault() → do_anonymous_page()
     * → alloc_page(GFP_HIGHUSER_MOVABLE) → 从伙伴系统的 MOVABLE 链表摘一页。
     * 注意 GFP 里的 MOVABLE：用户匿名页是**可迁移**的，规整时能被搬走。
     * 这也是为什么光靠这个程序还不够，必须配合档位 2 的 UNMOVABLE 污染。
     */
    clock_gettime(CLOCK_MONOTONIC, &t0);
    for (i = 0; i < total_bytes; i += PAGE_SIZE) {
        base[i] = (char)(i >> 12);
        if ((i & ((256UL << 20) - 1)) == 0 && i)
            printf("[holes] 已触碰 %zu MB ...\n", i >> 20);
    }
    clock_gettime(CLOCK_MONOTONIC, &t1);
    printf("[holes] 触碰完成，耗时 %.2f 秒，%zu MB 现在是真实驻留内存（RSS）\n",
           (t1.tv_sec - t0.tv_sec) + (t1.tv_nsec - t0.tv_nsec) / 1e9, total_mb);

    /* ---- 第 3 步：随机顺序挖洞 ---- */
    order_arr = malloc(nblocks * sizeof(*order_arr));
    if (!order_arr) {
        fprintf(stderr, "[holes] malloc 索引数组失败\n");
        return 1;
    }
    for (i = 0; i < nblocks; i++)
        order_arr[i] = (unsigned int)i;
    shuffle(order_arr, nblocks, seed);

    clock_gettime(CLOCK_MONOTONIC, &t0);
    for (i = 0; i < nfree; i++) {
        char *p = base + (size_t)order_arr[i] * block_bytes;

        if (madvise(p, block_bytes, MADV_DONTNEED) != 0) {
            fprintf(stderr, "[holes] madvise(DONTNEED) 第 %zu 块失败: %s\n",
                    i, strerror(errno));
            break;
        }
    }
    clock_gettime(CLOCK_MONOTONIC, &t1);
    printf("[holes] 随机挖洞完成：释放了 %zu 块 / 共 %zu 块，耗时 %.2f 秒\n",
           i, nblocks, (t1.tv_sec - t0.tv_sec) + (t1.tv_nsec - t0.tv_nsec) / 1e9);

    dump_buddyinfo("施压后");

    printf("\n[holes] PID=%d，进程保持存活以维持碎片状态。\n", getpid());
    printf("[holes] 按 Ctrl-C 退出（退出后剩余 %zu MB 会一次性归还，碎片会被合并掉）\n",
           total_mb - (nfree * block_kb / 1024));

    while (!g_stop)
        pause();

    printf("\n[holes] 收到退出信号，释放全部内存。\n");
    munmap(base, total_bytes);
    free(order_arr);
    return 0;
}
