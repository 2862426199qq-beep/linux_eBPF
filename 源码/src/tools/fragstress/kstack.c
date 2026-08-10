/*
 * kstack.c —— fragstress 档位 2：UNMOVABLE 污染（整个压力基座的灵魂）
 *
 * ============================ 为什么必须有这一档 ============================
 *
 * 档位 1（holes.c）造出来的碎片是"假碎片"：它申请的全是**用户匿名页**，
 * GFP 标志是 GFP_HIGHUSER_MOVABLE —— MOVABLE，可迁移。
 * 内核一规整，把这些页挨个搬到别处，连续块就又凑出来了。
 * 结果就是：compact_stall 会涨，但 compact_success 也涨，compact_fail 始终是 0。
 * 那我们观测到的就只是"规整很成功"，测不到真正的代价。
 *
 * 真正把 pageblock 钉死的，是**内核自己的对象**：它们是 UNMOVABLE 的，
 * 因为内核代码里到处存着指向它们的**物理地址/直接映射地址**，
 * 一搬走这些指针全废，所以内核**根本没有实现**它们的迁移。
 * 规整扫描器一碰到这种页就只能绕开 —— 这个 pageblock 就永远凑不出 order-9 了。
 *
 * 这个程序用最省事的方式批量制造 UNMOVABLE 对象：**疯狂创建线程**。
 * 每创建一个线程，内核至少要分配：
 *   - 内核栈 16KB（THREAD_SIZE，x86_64）
 *   - task_struct（约 9~10KB，SLUB 的 UNMOVABLE slab）
 *   - 其他若干小对象（信号结构、cred、vma 等）
 *
 * ============================ ★ 与计划书不符的一点（本机实测） ============================
 *
 * 计划书写"内核栈是 order-2 UNMOVABLE 连续块"。**本机不是。**
 * 实测 /boot/config-5.15.0-139-generic：CONFIG_VMAP_STACK=y。
 *
 * 开了 VMAP_STACK 之后，内核栈走 vmalloc：
 *   16KB 的栈 = 4 个**互不相邻**的 order-0 页，靠页表映射成连续的虚拟地址。
 *   物理上完全不连续。
 *
 * 这对我们**更有利**，而且是个值得讲的点：
 *   一个 order-2 连续块，最多毁掉 1 个 pageblock 的连续性；
 *   4 个散落的 order-0 UNMOVABLE 页，可能毁掉 **4 个不同的 pageblock**。
 *   规整能不能凑出 order-9（512 个连续页），取决于这 512 页里有没有搬不走的，
 *   所以"搬不走的页越分散，杀伤力越大"。
 *
 * 另外 VMAP_STACK 还有个 per-CPU 缓存（NR_CACHED_STACKS=2），
 * 线程退出时栈会被缓存复用。所以这个程序**必须让线程一直活着**，
 * 反复 create/join 是没用的 —— 会一直命中缓存，根本不向伙伴系统要新页。
 *
 * ============================ 怎么验证它真的生效了 ============================
 *
 *   grep -E "KernelStack|Slab|SUnreclaim|VmallocUsed" /proc/meminfo
 *
 * KernelStack 会随线程数线性上涨（每线程 16KB）。
 * 更硬的证据是 /proc/pagetypeinfo（需要 root）里 Unmovable 那几行的块数变化。
 *
 * ============================ 用法 ============================
 *
 *   ./kstack [线程数] [每线程用户栈KB]
 *   默认：      8000          64
 *
 * 8000 线程 ≈ 128 MB 内核栈 + 约 80 MB slab，散布在整个 Normal zone 里。
 * 上限受 /proc/sys/kernel/threads-max 和 ulimit -u 约束（本机 94599 / 47299）。
 *
 * 进程会一直挂着。按 Ctrl-C 退出（退出后所有线程销毁，污染立刻消失）。
 */

#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <signal.h>
#include <errno.h>
#include <limits.h>
#include <pthread.h>
#include <semaphore.h>

static volatile sig_atomic_t g_stop = 0;
static sem_t g_never_posted;   /* 永远不会被 post 的信号量：让线程永久睡在这里 */

static void on_signal(int sig)
{
    (void)sig;
    g_stop = 1;
}

/*
 * 工作线程：什么都不干，就是睡着不走。
 *
 * 关键在于"活着"而不是"干活"——我们要的是它占着的那 16KB 内核栈和
 * 那个 task_struct，不是它的 CPU 时间。所以必须用**阻塞睡眠**，
 * 不能用忙等（忙等会把 4 个核跑满，机器直接卡死，什么都测不了）。
 */
static void *worker(void *arg)
{
    (void)arg;
    /* sem_wait 会被信号打断返回 EINTR，所以要循环 */
    while (sem_wait(&g_never_posted) != 0 && errno == EINTR)
        continue;
    return NULL;
}

static void dump_kernel_mem(const char *tag)
{
    FILE *fp = fopen("/proc/meminfo", "r");
    char line[256];

    if (!fp) {
        fprintf(stderr, "[kstack] 打不开 /proc/meminfo: %s\n", strerror(errno));
        return;
    }
    printf("\n===== 内核侧内存占用（%s）=====\n", tag);
    while (fgets(line, sizeof(line), fp)) {
        if (!strncmp(line, "MemFree:",     8) ||
            !strncmp(line, "KernelStack:", 12) ||
            !strncmp(line, "Slab:",        5) ||
            !strncmp(line, "SReclaimable:", 13) ||
            !strncmp(line, "SUnreclaim:",  11) ||
            !strncmp(line, "VmallocUsed:", 12))
            fputs(line, stdout);
    }
    fclose(fp);
}

int main(int argc, char **argv)
{
    long nthreads   = (argc > 1) ? strtol(argv[1], NULL, 10) : 8000;
    size_t stack_kb = (argc > 2) ? strtoul(argv[2], NULL, 10) : 64;

    pthread_attr_t attr;
    long created = 0;
    long min_stack;
    int rc;

    if (nthreads <= 0) {
        fprintf(stderr, "用法: %s [线程数] [每线程用户栈KB]\n", argv[0]);
        return 1;
    }

    /*
     * ★ 必须改成行缓冲。
     * glibc 的默认策略是：stdout 连着终端 → 行缓冲；连着文件/管道 → **全缓冲**（4KB）。
     * 本程序总共才输出几百字节，被 run.sh 重定向到日志文件时，
     * 这些字符串会一直躺在进程的 stdio 缓冲区里，**进程退出前一个字节都不落盘**。
     * run.sh 靠轮询日志里的「实际创建」判断本步是否就绪，
     * 缓冲住就等于那个标记永远不出现 —— 实测白等了 300 秒超时。
     */
    setvbuf(stdout, NULL, _IOLBF, 0);

    signal(SIGINT,  on_signal);
    signal(SIGTERM, on_signal);
    sem_init(&g_never_posted, 0, 0);

    /*
     * 用户栈开小一点，并把 guard page 设成 0。
     * 理由：每个线程的用户栈是一个独立 VMA，几万个线程就是几万个 VMA，
     *       会撞上 /proc/sys/vm/max_map_count（默认 65530）。
     *       guardsize=0 能省掉一半 VMA。
     * 注意：我们要的是**内核栈**（内核自己分配的，控制不了大小），
     *       用户栈开多小都不影响污染效果。
     */
    pthread_attr_init(&attr);
    /* glibc 2.34 起 PTHREAD_STACK_MIN 变成了运行时值，编译期常量不再保证存在，
     * 所以统一用 sysconf 查；查不到就退回 x86_64 的老常量 16KB。 */
    min_stack = sysconf(_SC_THREAD_STACK_MIN);
    if (min_stack <= 0)
        min_stack = 16 * 1024;
    if ((long)(stack_kb * 1024) < min_stack)
        stack_kb = (size_t)min_stack / 1024;
    pthread_attr_setstacksize(&attr, stack_kb * 1024);
    pthread_attr_setguardsize(&attr, 0);
    pthread_attr_setdetachstate(&attr, PTHREAD_CREATE_DETACHED);

    printf("[kstack] 计划创建 %ld 个线程，每线程用户栈 %zu KB\n", nthreads, stack_kb);
    printf("[kstack] 预期内核开销：约 %ld MB 内核栈（每线程 16KB，VMAP_STACK 下是 4 个散页）\n",
           nthreads * 16 / 1024);

    dump_kernel_mem("创建线程前");

    while (created < nthreads && !g_stop) {
        pthread_t tid;

        rc = pthread_create(&tid, &attr, worker, NULL);
        if (rc != 0) {
            /* 撞到 threads-max / RLIMIT_NPROC / 内存不足都会走到这里。
             * 这不算失败——能创建多少算多少，如实报出来即可。 */
            fprintf(stderr, "[kstack] 第 %ld 个线程创建失败: %s（到此为止）\n",
                    created + 1, strerror(rc));
            break;
        }
        created++;
        if (created % 1000 == 0)
            printf("[kstack] 已创建 %ld 个线程 ...\n", created);
    }

    printf("[kstack] 实际创建 %ld / %ld 个线程\n", created, nthreads);
    dump_kernel_mem("创建线程后");

    printf("\n[kstack] PID=%d，进程保持存活以维持 UNMOVABLE 污染。\n", getpid());
    printf("[kstack] 按 Ctrl-C 退出（退出后所有内核栈立刻归还，污染消失）\n");

    while (!g_stop)
        pause();

    printf("\n[kstack] 收到退出信号，进程退出，%ld 个线程一并销毁。\n", created);
    pthread_attr_destroy(&attr);
    return 0;
}
