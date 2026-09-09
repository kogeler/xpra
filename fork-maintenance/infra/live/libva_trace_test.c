/* Copyright (C) 2026 kogeler
 * Compile the distribution's actual trace writer, not a reimplementation.
 * Only wall time and diagnostic messages are controlled. Native file opens,
 * writes, closes, permissions and independent per-display log managers are real.
 */
#define _GNU_SOURCE 1
#include <sys/time.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <dirent.h>

static int fixed_gettimeofday(struct timeval *tv, void *zone)
{
    (void)zone;
    tv->tv_sec = 12345;
    tv->tv_usec = 0;
    return 0;
}

#define gettimeofday fixed_gettimeofday
#include "va_trace.c"
#undef gettimeofday

void va_infoMessage(VADisplay dpy, const char *message, ...)
{
    (void)dpy;
    (void)message;
}

void va_errorMessage(VADisplay dpy, const char *message, ...)
{
    (void)dpy;
    (void)message;
}

static void require(int ok, const char *message)
{
    if (!ok) {
        fprintf(stderr, "TRACE-CONTROL unexpected: %s\n", message);
        exit(1);
    }
}

static int contents_equal(const char *path, const char *expected)
{
    char bytes[128] = {0};
    FILE *stream = fopen(path, "r");
    require(stream != NULL, "read trace");
    size_t size = fread(bytes, 1, sizeof(bytes), stream);
    require(!ferror(stream), "read complete");
    require(fclose(stream) == 0, "close read");
    return size == strlen(expected) && memcmp(bytes, expected, size) == 0;
}

static int descriptor_count(void)
{
    DIR *directory = opendir("/proc/self/fd");
    require(directory != NULL, "descriptor directory");
    int count = 0;
    while (readdir(directory))
        count++;
    require(closedir(directory) == 0, "close descriptor directory");
    return count;
}

int main(void)
{
    umask(0077);
    int initial_fds = descriptor_count();
    struct va_trace first = {.fn_log_env = "trace"};
    struct va_trace second = {.fn_log_env = "trace"};
    require(pthread_mutex_init(&first.resource_mutex, NULL) == 0, "first mutex");
    require(pthread_mutex_init(&second.resource_mutex, NULL) == 0, "second mutex");
    struct trace_log_file *one = &first.log_files_manager.log_file[0];
    struct trace_log_file *two = &second.log_files_manager.log_file[0];
    const pid_t thread = va_gettid();
    require(open_tracing_log_file(&first, one, thread) == 0, "first open");
    require(fputs("first\n", one->fp_log) >= 0, "first write");
    stop_tracing2log_file(&first, one);
    require(contents_equal(one->fn_log, "first\n"), "initial contents");

    /* A second display in the same thread and second used to truncate one. */
    require(open_tracing_log_file(&second, two, thread) == 0, "second open");
    require(fputs("second\n", two->fp_log) >= 0, "second write");
    stop_tracing2log_file(&second, two);
    require(contents_equal(two->fn_log, "second\n"), "second contents");
    int collision = strcmp(one->fn_log, two->fn_log) == 0;
    require(contents_equal(one->fn_log, collision ? "second\n" : "first\n"),
            "collision must be the only clean-control defect");

    /* Reopening the SAME display must append to its existing exact file. */
    char *saved = strdup(two->fn_log);
    require(saved != NULL, "save filename");
    require(open_tracing_log_file(&second, two, thread) == 0, "reopen");
    require(strcmp(saved, two->fn_log) == 0, "stable name on append");
    require(open_tracing_log_file(&second, two, thread + 1) == -1,
            "busy foreign thread rejected");
    require(fputs("tail\n", two->fp_log) >= 0, "append");
    stop_tracing2log_file(&second, two);
    require(contents_equal(saved, "second\ntail\n"), "append preserves contents");
    struct stat metadata;
    require(stat(saved, &metadata) == 0 && (metadata.st_mode & 0777) == 0600,
            "private regular trace permissions");
    require(unlink(saved) == 0, "remove second trace");
    if (!collision)
        require(unlink(one->fn_log) == 0, "remove first trace");
    free(saved);
    free(one->fn_log);
    free(two->fn_log);
    pthread_mutex_destroy(&first.resource_mutex);
    pthread_mutex_destroy(&second.resource_mutex);

    struct va_trace failed = {.fn_log_env = "absent/trace"};
    struct trace_log_file bad = {0};
    require(open_tracing_log_file(&failed, &bad, thread) == -1, "open failure");
    require(!bad.fn_log && !bad.fp_log && !bad.used, "failed open has no owner");
    require(descriptor_count() == initial_fds, "no descriptor leak");
    puts(collision ? "TRACE-CONTROL collision-reproduced" : "TRACE-CONTROL preserved");
    return collision ? 42 : 0;
}
