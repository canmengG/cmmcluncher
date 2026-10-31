"""Tests for the in-memory download task queue (0.6.0)."""

from __future__ import annotations

import threading
import time
import unittest

from mc_launcher.core.net import CancelledError
from mc_launcher.core.taskqueue import TaskQueue


def _wait_until(predicate, timeout: float = 6.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _wait_finished(queue: TaskQueue, count: int, timeout: float = 6.0) -> bool:
    return _wait_until(
        lambda: sum(
            1 for item in queue.snapshots() if item.state in
            ("succeeded", "failed", "cancelled")
        )
        >= count,
        timeout,
    )


class TaskQueueTests(unittest.TestCase):
    def make_queue(self, max_concurrent: int = 1) -> TaskQueue:
        queue = TaskQueue(max_concurrent=max_concurrent)
        self.addCleanup(queue.stop, 3.0)
        return queue

    def test_runs_tasks_in_order_with_one_worker(self) -> None:
        queue = self.make_queue(1)
        events: list[str] = []
        lock = threading.Lock()

        def runner(name: str):
            def run(_context):
                with lock:
                    events.append(f"start-{name}")
                time.sleep(0.02)
                with lock:
                    events.append(f"end-{name}")
                return f"done-{name}"

            return run

        for name in ("a", "b", "c"):
            queue.add("test", name, "", runner(name))
        self.assertTrue(_wait_finished(queue, 3))
        self.assertEqual(
            events,
            ["start-a", "end-a", "start-b", "end-b", "start-c", "end-c"],
        )
        states = [item.state for item in queue.snapshots()]
        self.assertEqual(states, ["succeeded", "succeeded", "succeeded"])

    def test_concurrency_cap(self) -> None:
        queue = self.make_queue(2)
        first_gate = threading.Event()
        second_gate = threading.Event()
        started: list[str] = []
        lock = threading.Lock()

        def blocking(name: str, gate: threading.Event):
            def run(_context):
                with lock:
                    started.append(name)
                gate.wait(4.0)
                return ""

            return run

        queue.add("test", "one", "", blocking("one", first_gate))
        queue.add("test", "two", "", blocking("two", second_gate))
        queue.add("test", "three", "", blocking("three", threading.Event()))
        self.assertTrue(_wait_until(lambda: len(started) == 2))
        self.assertEqual(sorted(started), ["one", "two"])
        self.assertEqual(queue.snapshots()[2].state, "queued")
        first_gate.set()
        second_gate.set()
        self.assertTrue(_wait_finished(queue, 3))

    def test_pause_and_resume_running_task(self) -> None:
        queue = self.make_queue(1)
        observed: list[str] = []
        started = threading.Event()

        def runner(context):
            observed.append("start")
            started.set()
            while not context.pause.paused:
                time.sleep(0.005)
            context.pause.wait(cancel=context.cancel)
            observed.append("resumed")
            return "ok"

        task = queue.add("test", "pause", "", runner)
        self.assertTrue(started.wait(4.0))
        self.assertTrue(_wait_until(lambda: queue.get(task.id).state == "running"))
        self.assertTrue(queue.pause(task.id))
        time.sleep(0.05)
        self.assertEqual(queue.get(task.id).state, "paused")
        self.assertNotIn("resumed", observed)
        self.assertTrue(queue.resume(task.id))
        self.assertTrue(_wait_finished(queue, 1))
        self.assertEqual(queue.get(task.id).state, "succeeded")
        self.assertIn("resumed", observed)

    def test_cancel_running_task(self) -> None:
        queue = self.make_queue(1)

        def runner(context):
            while not context.cancel.is_set():
                time.sleep(0.01)
            raise CancelledError("cancelled by test")

        task = queue.add("test", "cancel", "", runner)
        self.assertTrue(_wait_until(lambda: queue.get(task.id).state == "running"))
        self.assertTrue(queue.cancel(task.id))
        self.assertTrue(_wait_finished(queue, 1))
        self.assertEqual(queue.get(task.id).state, "cancelled")

    def test_cancel_queued_task(self) -> None:
        queue = self.make_queue(1)
        gate = threading.Event()
        queue.add("test", "blocker", "", lambda _context: gate.wait(4.0))
        queued = queue.add("test", "queued", "", lambda _context: "")
        self.assertTrue(queue.cancel(queued.id))
        self.assertEqual(queue.get(queued.id).state, "cancelled")
        gate.set()
        self.assertTrue(_wait_finished(queue, 2))

    def test_failure_is_isolated(self) -> None:
        queue = self.make_queue(1)

        def boom(_context):
            raise RuntimeError("boom")

        first = queue.add("test", "boom", "", boom)
        second = queue.add("test", "ok", "", lambda _context: "fine")
        self.assertTrue(_wait_finished(queue, 2))
        self.assertEqual(queue.get(first.id).state, "failed")
        self.assertIn("boom", queue.get(first.id).error)
        self.assertEqual(queue.get(second.id).state, "succeeded")

    def test_progress_snapshot(self) -> None:
        queue = self.make_queue(1)
        started = threading.Event()
        release = threading.Event()

        def runner(context):
            context.update(
                done_bytes=50,
                total_bytes=200,
                files_done=1,
                files_total=4,
                message="half",
            )
            started.set()
            release.wait(4.0)
            return "done"

        task = queue.add("test", "progress", "detail", runner)
        self.assertTrue(started.wait(4.0))
        snapshot = queue.get(task.id)
        self.assertEqual(snapshot.done_bytes, 50)
        self.assertEqual(snapshot.total_bytes, 200)
        self.assertEqual(snapshot.files_done, 1)
        self.assertEqual(snapshot.files_total, 4)
        self.assertAlmostEqual(snapshot.progress, 0.25)
        release.set()
        self.assertTrue(_wait_finished(queue, 1))
        self.assertEqual(queue.get(task.id).message, "done")

    def test_progress_fallback_uses_files(self) -> None:
        queue = self.make_queue(1)
        started = threading.Event()
        release = threading.Event()

        def runner(context):
            context.update(files_done=2, files_total=8, message="files")
            started.set()
            release.wait(4.0)
            return ""

        task = queue.add("test", "files", "", runner)
        self.assertTrue(started.wait(4.0))
        self.assertAlmostEqual(queue.get(task.id).progress, 0.25)
        release.set()
        self.assertTrue(_wait_finished(queue, 1))

    def test_remove_and_clear_finished(self) -> None:
        queue = self.make_queue(1)
        task = queue.add("test", "one", "", lambda _context: "ok")
        self.assertTrue(_wait_finished(queue, 1))
        self.assertTrue(queue.remove(task.id))
        self.assertIsNone(queue.get(task.id))
        queue.add("test", "two", "", lambda _context: "ok")
        self.assertTrue(_wait_finished(queue, 1))
        self.assertEqual(queue.clear_finished(), 1)
        self.assertEqual(queue.snapshots(), [])

    def test_max_concurrent_is_clamped(self) -> None:
        queue = self.make_queue(0)
        self.assertEqual(queue.max_concurrent, 1)
        self.assertEqual(queue.set_max_concurrent(99), 8)

    def test_stop_cancels_running(self) -> None:
        queue = TaskQueue(max_concurrent=1)
        stopped = threading.Event()

        def runner(context):
            while not context.cancel.is_set():
                time.sleep(0.01)
            stopped.set()
            raise CancelledError("stop")

        queue.add("test", "stop", "", runner)
        self.assertTrue(_wait_until(lambda: queue.snapshots()[0].state == "running"))
        queue.stop(timeout=3.0)
        self.assertTrue(stopped.wait(3.0))


if __name__ == "__main__":
    unittest.main()
