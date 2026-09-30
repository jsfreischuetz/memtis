#!/usr/bin/env python3
"""Run actual metadata helpers with mock pages under ASan/UBSan.

This tests NULL+offset, bounded baseline accounting, and metadata writes without booting the
experimental kernel. It does not replace kernel concurrency/stress testing.
"""
from pathlib import Path
import os
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1] / "linux"


def function(path, signature):
    text = (ROOT / path).read_text()
    start = text.index(signature)
    brace = text.index("{", start)
    depth = 1
    end = brace + 1
    while depth:
        depth += (text[end] == "{") - (text[end] == "}")
        end += 1
    return text[start:end]


PRELUDE = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
#include <stdlib.h>
#include <string.h>
#define PAGE_SIZE 4096UL
#define PAGE_MASK (~(PAGE_SIZE - 1))
#define HPAGE_PMD_NR 512
#define READ_ONCE(x) (x)
#define VM_BUG_ON_PAGE(x, p) assert(!(x))
#define TAIL_MAPPING ((void *)0x400UL)
struct list_head { struct list_head *next, *prev; };
static void init_list(struct list_head *h) { h->next = h->prev = h; }
static bool list_empty(void *p) {
    struct list_head *h = p;
    return h->next == h;
}
static void list_add_tail(struct list_head *item, struct list_head *h) {
    item->prev = h->prev; item->next = h;
    h->prev->next = item; h->prev = item;
}
static void list_move(struct list_head *item, struct list_head *h) {
    item->prev->next = item->next; item->next->prev = item->prev;
    list_add_tail(item, h);
}
typedef unsigned long pte_t;
typedef struct { uint32_t total_accesses; uint16_t nr_accesses;
                 uint8_t cooling_clock; bool may_hot; } pginfo_t;
struct mem_cgroup { bool htmm_enabled; int access_lock;
    unsigned long hotness_hg[16], ebp_hotness_hg[16];
    unsigned long access_map[21], split_threshold, nr_split, nr_split_tail_idx; };
struct mem_cgroup_per_node { struct mem_cgroup *memcg; };
struct page {
    unsigned long flags;
    /* Preserve the real alias between mapping and compound_pginfo[1]. */
    union {
        struct { unsigned long pad1, pad2; void *mapping; };
        struct { unsigned long pad3; pginfo_t compound_pginfo[4]; };
        struct { unsigned long pad4, total_accesses;
            unsigned int hot_utils, skewness_idx, idx, cooling_clock; };
        struct { unsigned long pad5, pad6; pginfo_t *pginfo; };
    };
    bool huge, anon, tracked;
    unsigned int nr;
    struct page *head;
    struct list_head lru, deferred_list;
};
#define PageHtmm(p) ((p)->tracked)
#define SetPageHtmm(p) ((p)->tracked = true)
#define PageAnon(p) ((p)->anon)
#define PageTransHuge(p) ((p)->huge)
#define PageCompound(p) ((p)->huge)
#define thp_nr_pages(p) ((p)->nr)
#define compound_head(p) ((p)->head ? (p)->head : (p))
#define spin_lock(p) ((void)(p))
#define spin_unlock(p) ((void)(p))
#define page_deferred_list(p) (&(p)->deferred_list)
#define PageLRU(p) false
#define TestClearPageLRU(p) true
#define VM_WARN_ON(x) ((void)(x))
#define lru_to_page(h) ((struct page *)((char *)(h)->next - offsetof(struct page, lru)))
static unsigned int htmm_thres_split = 1;
static struct page table_page;
static struct mem_cgroup *owner;
#define page_memcg(p) (owner)
#define virt_to_page(addr) (&table_page)
struct page *get_meta_page(struct page *page) { return &page[3]; }
'''

MAIN = r'''
static void account_failed(struct page *page, struct mem_cgroup *cg) {
    struct list_head tmp, failed;
    struct mem_cgroup_per_node pn = {.memcg = cg};
    init_list(&tmp); init_list(&failed);
    list_add_tail(&page->lru, &tmp);
    check_failed_list(&pn, &tmp, &failed);
    assert(list_empty(&tmp) && failed.next == &page->lru);
    init_list(&page->lru);
}

int main(void) {
    pte_t *ptes = aligned_alloc(PAGE_SIZE, PAGE_SIZE);
    pginfo_t entries[512] = {0};
    assert(ptes);
    /* The crash used a nonzero slot: a final-pointer NULL test missed it. */
    table_page.tracked = true;
    table_page.pginfo = NULL;
    assert(get_pginfo_from_pte(&ptes[1]) == NULL);
    assert(get_pginfo_from_pte(&ptes[511]) == NULL);
    table_page.tracked = false;
    table_page.pginfo = (void *)0xdeadbeef;
    assert(get_pginfo_from_pte(&ptes[1]) == NULL);
    table_page.tracked = true;
    table_page.pginfo = entries;
    assert(get_pginfo_from_pte(&ptes[0]) == &entries[0]);
    assert(get_pginfo_from_pte(&ptes[511]) == &entries[511]);

    struct page *src = calloc(512, sizeof(*src));
    struct page *dst = calloc(512, sizeof(*dst));
    struct mem_cgroup cg = {.htmm_enabled = true};
    assert(src && dst);
    dst->huge = dst->anon = true;
    dst->nr = 512;
    src->huge = src->anon = true;
    src->nr = 512;
    src[3].head = src;
    init_list(&src->deferred_list);
    src[3].idx = 256; /* preexisting/untracked page in an enabled cgroup */
    cg.hotness_hg[0] = 1024;
    owner = &cg;
    account_split(src);
    uncharge_htmm_page(src, &cg);
    account_failed(src, &cg);
    assert(cg.hotness_hg[0] == 1024);
    src[3].tracked = true;
    assert(htmm_thp_metadata_valid(src)); /* layout valid, histogram index invalid */
    account_split(src);
    uncharge_htmm_page(src, &cg);
    account_failed(src, &cg);
    assert(cg.hotness_hg[0] == 1024);
    /* Permit the original wrong accounting on file and untracked pages.
     * Metadata-writing helpers must still reject those layouts. */
    for (int file = 0; file < 2; file++) {
        src[3].idx = 0;
        src->anon = !file;
        src[3].tracked = file;
        assert(!htmm_thp_metadata_valid(src));
        cg.hotness_hg[0] = 1024;
        cg.ebp_hotness_hg[0] = 1024;
        account_split(src);
        assert(cg.hotness_hg[0] == 512);
        account_failed(src, &cg);
        assert(cg.hotness_hg[0] == 1024);
        uncharge_htmm_page(src, &cg);
        assert(cg.hotness_hg[0] == 512);
        assert(cg.ebp_hotness_hg[0] == 512);
        assert(!check_split_huge_page(&cg, &src[3], false));
        copy_transhuge_pginfo(src, dst);
        assert(!dst[3].tracked);
    }
    /* Actual undersized allocations expose premature tail reads to ASan. */
    for (int n = 1; n <= 4; n *= 2) {
        struct page *small = calloc(n, sizeof(*small));
        assert(small);
        small->huge = n > 1;
        small->nr = n;
        cg.hotness_hg[0] = 1024;
        account_split(small);
        account_failed(small, &cg);
        uncharge_htmm_page(small, &cg);
        assert(cg.hotness_hg[0] == 1024);
        free(small);
    }
    src->anon = true;
    src[3].tracked = true;
    assert(htmm_thp_metadata_valid(src));
    src[3].skewness_idx = 21;
    assert(htmm_thp_metadata_valid(src)); /* scalar values do not veto the layout */
    cg.split_threshold = 1;
    cg.nr_split = 512;
    assert(!check_split_huge_page(&cg, &src[3], false));
    assert(cg.nr_split == 512);
    src[3].skewness_idx = 20;
    cg.access_map[20] = 1;
    assert(check_split_huge_page(&cg, &src[3], false));
    assert(cg.nr_split == 0 && cg.access_map[20] == 0);
    src[3].skewness_idx = 0;
    src->nr = 1;
    assert(!htmm_thp_metadata_valid(src));
    src->nr = 512;
    uncharge_htmm_page(src, &cg);
    assert(cg.hotness_hg[0] == 512);
    uncharge_htmm_page(src, &cg);
    uncharge_htmm_page(src, &cg);
    assert(cg.hotness_hg[0] == 0); /* no unsigned underflow */
    cg.hotness_hg[0] = 1024;
    account_split(src);
    assert(cg.hotness_hg[0] == 512);
    owner = NULL;
    account_split(src); /* uncharged THP */

    pginfo_t expected[512];
    for (int i = 0; i < 512; i++) {
        expected[i] = (pginfo_t){i + 17, i + 1, 123, true};
        src[4 + i / 4].compound_pginfo[i % 4] = expected[i];
    }
    /* Migration must retain baseline scalar metadata, even when its values
     * are invalid for histogram indexing. Guard the array users instead. */
    src[3].idx = 256;
    src[3].skewness_idx = 21;
    copy_transhuge_pginfo(src, dst);
    assert(dst[3].idx == 256);
    assert(dst[3].skewness_idx == 21);
    /* Baseline copies access counts only; do not require the reverted
     * cooling/may_hot preservation or tail-mapping copy-order fixes. */
    assert(dst[4].compound_pginfo[0].nr_accesses == expected[0].nr_accesses);
    assert(dst[4].compound_pginfo[0].total_accesses == expected[0].total_accesses);
    assert(dst[4].compound_pginfo[0].cooling_clock == 0);
    assert(!dst[4].compound_pginfo[0].may_hot);
    free(src); free(dst); free(ptes);
    return 0;
}
'''


if __name__ == "__main__":
    if "bool htmm_thp_metadata_valid(" not in (ROOT / "mm/htmm_core.c").read_text():
        print("SKIP: fix6 crash-safety regressions; the original baseline intentionally removes these safeguards.")
        raise SystemExit(0)
    source = PRELUDE
    for path, signature in [
        ("arch/x86/include/asm/pgtable.h", "static inline pginfo_t *get_pginfo_from_pte("),
        ("mm/htmm_core.c", "bool htmm_thp_metadata_valid("),
        ("mm/htmm_core.c", "bool check_split_huge_page("),
        ("mm/htmm_core.c", "unsigned int get_idx("),
        ("mm/htmm_core.c", "void uncharge_htmm_page("),
        ("mm/htmm_core.c", "void check_failed_list("),
        ("mm/htmm_core.c", "void copy_transhuge_pginfo("),
    ]:
        source += "\n" + function(path, signature)
    split = function("mm/huge_memory.c", "int split_huge_page_to_list(")
    start = split.index("struct mem_cgroup *memcg = page_memcg(head);")
    block = split[split.rfind("{", 0, start):split.index("#endif", start)]
    source += "\nstatic void account_split(struct page *head)\n" + block
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp)
        (path / "metadata.c").write_text(source + MAIN)
        subprocess.run(["gcc", "-Wall", "-Wextra", "-Werror", "-Wno-unused-parameter", "-O1", "-g",
                        "-fsanitize=address,undefined", "-fno-sanitize-recover=all",
                        str(path / "metadata.c"), "-o", str(path / "metadata")], check=True)
        # LSan cannot inspect threads in Codex's ptrace sandbox; ASan/UBSan
        # remain enabled for the actual memory-safety regression checks.
        subprocess.run([str(path / "metadata")], check=True,
                       env=dict(os.environ, ASAN_OPTIONS="detect_leaks=0"))
    print("PASS: missing PTE metadata, bounded baseline accounting, small pages, split eligibility, baseline migration metadata copy")
