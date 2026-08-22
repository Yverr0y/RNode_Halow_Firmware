#ifndef TEST_STUB_BASIC_INCLUDE_H
#define TEST_STUB_BASIC_INCLUDE_H

#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>
#include <string.h>
#include <stdio.h>
#include <stdlib.h>

typedef uint8_t  uint8;
typedef uint16_t uint16;
typedef uint32_t uint32;
typedef uint64_t uint64;
typedef int8_t   int8;
typedef int16_t  int16;
typedef int32_t  int32;
typedef int64_t  int64;

struct os_semaphore { int32_t count; };
struct os_mutex     { int32_t dummy; };
struct os_task      { int32_t dummy; };

#define OS_TASK_PRIORITY_REALTIME 0
#define OS_TASK_PRIORITY_LOW      3

uint64_t os_jiffies(void);
uint64_t os_msecs_to_jiffies(uint32_t ms);
uint32_t os_jiffies_to_msecs(uint64_t j);
int32_t  os_sema_down(struct os_semaphore *s, int32_t tmo_ms);
void     os_sema_up(struct os_semaphore *s);
void     os_sema_init(struct os_semaphore *s, int32_t count);
int32_t  os_mutex_lock(struct os_mutex *m, int32_t tmo_ms);
int32_t  os_mutex_init(struct os_mutex *m);
void     os_mutex_unlock(struct os_mutex *m);
void     os_task_init(const uint8_t *name, struct os_task *t, void (*fn)(void *), void *arg);
void     os_task_set_stacksize(struct os_task *t, uint32_t sz);
void     os_task_set_priority(struct os_task *t, uint8_t prio);
void     os_task_run(struct os_task *t);
void     os_sleep_ms(uint32_t ms);
void     os_sleep(int sec);
void     *os_malloc(uint32_t size);
void     os_free(void *p);


/* Preempt the real osal headers with a minimal, platform-free set.
 * Defining their include guards here makes any later real include a no-op,
 * so every TU in the test build sees ONE consistent declaration set. */
#define _OS_TASK_H_
#define __OS_TIME_H_
#define __OS_SLEEP_H_
#define _SYS_HEAP_H_

#define __bobj
#define os_snprintf snprintf
#define DIFF_JIFFIES(j1,j2) ((j2)-(j1))

#include <time.h>

struct os_task_info { uint32_t time; };
struct sys_heap { int dummy; };
struct timespec;               /* provided by <time.h> above */
struct timeval;
struct timezone;

int32_t os_task_runtime(struct os_task_info *t, int32_t count);
void    os_systime(struct timespec *tm);
uint32_t sysheap_totalsize(struct sys_heap *heap);
uint32_t sysheap_freesize(struct sys_heap *heap);
extern struct sys_heap sram_heap;
#endif
