# This file is part of Xpra.
# Copyright (C) 2026 Antoine Martin <antoine@xpra.org>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.


class FakeClock:
    def __init__(self, now=100.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeSource:
    """ the `GLib.Source` methods which the window sources use to own their timers """

    def __init__(self, scheduler, delay):
        self.scheduler = scheduler
        self.delay = delay
        self.callback = None
        self.tid = 0

    def set_callback(self, callback):
        self.callback = callback

    def attach(self, _context=None):
        self.tid = self.scheduler.timeout_add(self.delay, self.callback)
        return self.tid

    def destroy(self):
        if tid := self.tid:
            self.tid = 0
            self.scheduler.source_remove(tid)


class FakeScheduler:
    def __init__(self):
        self.tasks = {}
        self.scheduled = []
        self.cancelled = []
        self.next_id = 1

    def timeout_source_new(self, delay):
        return FakeSource(self, delay)

    def timeout_add(self, delay, callback, *args):
        tid = self.next_id
        self.next_id += 1
        self.tasks[tid] = callback, args
        self.scheduled.append((tid, delay))
        return tid

    def source_remove(self, tid):
        self.cancelled.append(tid)
        self.tasks.pop(tid, None)

    def idle_add(self, callback, *args):
        return self.timeout_add(0, callback, *args)

    def fire(self, tid):
        # a timer may be identified by its id or by the source which owns it:
        tid = getattr(tid, "tid", tid)
        callback, args = self.tasks.pop(tid)
        return callback(*args)
