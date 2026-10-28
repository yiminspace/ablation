"""Coalesced GUI update notifications."""

import threading


class UpdateEvents:
    def __init__(self):
        self._condition = threading.Condition()
        self._revision = 0
        self._closed = False

    def revision(self):
        with self._condition:
            return self._revision

    def publish(self):
        with self._condition:
            self._revision += 1
            self._condition.notify_all()

    def wait(self, revision, timeout=15):
        with self._condition:
            self._condition.wait_for(lambda: self._closed or revision != self._revision, timeout)
            return None if self._closed else self._revision

    def close(self):
        with self._condition:
            self._closed = True
            self._condition.notify_all()
