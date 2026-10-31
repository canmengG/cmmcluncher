"""In-memory download/install task queue (0.6.0).

The launcher used to allow exactly one download operation at a time.  This
module adds a small scheduler so several installs/content downloads can be
queued and run one after another (the default) or with a low concurrency cap.

Design notes:

* The queue is deliberately in-memory: restarting the launcher clears it.
* Every task owns a cancel event and a ``PauseToken``; the runner passes both
  to the existing download functions, which already know how to pause/resume
  and how to clean up cancelled partial files.
* Shared game folders (``libraries/``, ``assets/``, version JSON) can be
  touched by several queued jobs.  ``HttpClient.download_file`` serialises
  writes to the same destination path, and the UI defaults to one running task
  so version-JSON registration cannot race either.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Literal

from .net import CancelledError, PauseToken

TaskState = Literal[
    "queued",
    "running",
    "paused",
    "cancelling",
    "succeeded",
    "failed",
    "cancelled",
]
TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled"})
_STATE_LABELS = {
    "queued": "等待",
    "running": "下载中",
    "paused": "已暂停",
    "cancelling": "正在取消",
    "succeeded": "完成",
    "failed": "失败",
    "cancelled": "已取消",
}


def state_label(state: str) -> str:
    return _STATE_LABELS.get(state, state)


@dataclass
class TaskResult:
    message: str = ""
    detail: str = ""


@dataclass
class TaskSnapshot:
    id: int
    kind: str
    title: str
    detail: str
    state: str
    state_label: str
    done_bytes: int = 0
    total_bytes: int = 0
    files_done: int = 0
    files_total: int = 0
    message: str = ""
    error: str = ""
    created_at: float = 0.0
    started_at: float | None = None
    finished_at: float | None = None

    @property
    def progress(self) -> float:
        if self.total_bytes > 0:
            return max(0.0, min(1.0, self.done_bytes / self.total_bytes))
        if self.files_total > 0:
            return max(0.0, min(1.0, self.files_done / self.files_total))
        return 0.0


class TaskContext:
    """The handle a task runner uses to report progress and observe control."""

    def __init__(self, task: "QueueTask", queue: "TaskQueue") -> None:
        self._task = task
        self._queue = queue

    @property
    def cancel(self) -> threading.Event:
        return self._task.cancel_event

    @property
    def pause(self) -> PauseToken:
        return self._task.pause_token

    def update(
        self,
        *,
        done_bytes: int | None = None,
        total_bytes: int | None = None,
        files_done: int | None = None,
        files_total: int | None = None,
        message: str | None = None,
    ) -> None:
        self._queue._update_task(
            self._task,
            done_bytes=done_bytes,
            total_bytes=total_bytes,
            files_done=files_done,
            files_total=files_total,
            message=message,
        )

    def log(self, message: str, _level: str = "info") -> None:
        self._queue._log_task(self._task, str(message))


class QueueTask:
    def __init__(
        self,
        task_id: int,
        kind: str,
        title: str,
        detail: str,
        runner: Callable[[TaskContext], TaskResult | str | None],
    ) -> None:
        self.id = task_id
        self.kind = kind
        self.title = title
        self.detail = detail
        self.runner = runner
        self.state: str = "queued"
        self.cancel_event = threading.Event()
        self.pause_token = PauseToken()
        self.done_bytes = 0
        self.total_bytes = 0
        self.files_done = 0
        self.files_total = 0
        self.message = ""
        self.error = ""
        self.logs: list[str] = []
        self.created_at = time.time()
        self.started_at: float | None = None
        self.finished_at: float | None = None

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    @property
    def started(self) -> bool:
        return self.started_at is not None

    def snapshot(self) -> TaskSnapshot:
        return TaskSnapshot(
            id=self.id,
            kind=self.kind,
            title=self.title,
            detail=self.detail,
            state=self.state,
            state_label=state_label(self.state),
            done_bytes=self.done_bytes,
            total_bytes=self.total_bytes,
            files_done=self.files_done,
            files_total=self.files_total,
            message=self.message,
            error=self.error,
            created_at=self.created_at,
            started_at=self.started_at,
            finished_at=self.finished_at,
        )


class TaskQueue:
    """Run queued download jobs with a configurable concurrency limit."""

    def __init__(
        self,
        *,
        max_concurrent: int = 1,
        on_change: Callable[[], None] | None = None,
        log_callback: Callable[[str, str], None] | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._tasks: dict[int, QueueTask] = {}
        self._running: set[int] = set()
        self._threads: dict[int, threading.Thread] = {}
        self._next_id = 1
        self._max_concurrent = max(1, min(8, int(max_concurrent)))
        self._on_change = on_change
        self._log_callback = log_callback
        self._stopping = False
        self._last_notify = 0.0
        self._dispatcher = threading.Thread(
            target=self._dispatch_loop, daemon=True, name="download-queue"
        )
        self._dispatcher.start()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def max_concurrent(self) -> int:
        with self._lock:
            return self._max_concurrent

    def set_max_concurrent(self, value: int) -> int:
        with self._cond:
            self._max_concurrent = max(1, min(8, int(value)))
            self._cond.notify_all()
        return self._max_concurrent

    def add(
        self,
        kind: str,
        title: str,
        detail: str,
        runner: Callable[[TaskContext], TaskResult | str | None],
    ) -> TaskSnapshot:
        with self._cond:
            task = QueueTask(self._next_id, kind, title, detail, runner)
            self._next_id += 1
            self._tasks[task.id] = task
            self._cond.notify_all()
            snapshot = task.snapshot()
        self._notify(force=True)
        return snapshot

    def snapshots(self) -> list[TaskSnapshot]:
        with self._lock:
            return [self._tasks[key].snapshot() for key in sorted(self._tasks)]

    def get(self, task_id: int) -> TaskSnapshot | None:
        with self._lock:
            task = self._tasks.get(int(task_id))
            return task.snapshot() if task is not None else None

    def pause(self, task_id: int) -> bool:
        with self._cond:
            task = self._tasks.get(int(task_id))
            if task is None or task.state in TERMINAL_STATES:
                return False
            if task.state == "queued":
                task.state = "paused"
                task.message = "已暂停（等待开始）"
            elif task.state == "running":
                task.pause_token.pause()
                task.state = "paused"
                task.message = "已暂停"
            else:
                return False
            self._cond.notify_all()
        self._notify(force=True)
        return True

    def resume(self, task_id: int) -> bool:
        with self._cond:
            task = self._tasks.get(int(task_id))
            if task is None or task.state != "paused":
                return False
            if not task.started:
                task.state = "queued"
                task.message = ""
            else:
                task.pause_token.resume()
                task.state = "running"
                task.message = "继续下载"
            self._cond.notify_all()
        self._notify(force=True)
        return True

    def cancel(self, task_id: int) -> bool:
        with self._cond:
            task = self._tasks.get(int(task_id))
            if task is None or task.state in TERMINAL_STATES:
                return False
            if not task.started:
                task.state = "cancelled"
                task.finished_at = time.time()
                task.message = "已取消（未开始）"
            else:
                task.cancel_event.set()
                # Wake a paused runner so it can observe the cancellation.
                task.pause_token.resume()
                task.state = "cancelling"
                task.message = "正在取消…"
            self._cond.notify_all()
        self._notify(force=True)
        return True

    def remove(self, task_id: int) -> bool:
        with self._cond:
            task = self._tasks.get(int(task_id))
            if task is None:
                return False
            if task.state in TERMINAL_STATES or not task.started:
                self._tasks.pop(task.id, None)
                self._cond.notify_all()
                removed = True
            else:
                removed = False
        if removed:
            self._notify(force=True)
        return removed

    def clear_finished(self) -> int:
        with self._cond:
            finished = [
                key
                for key, task in self._tasks.items()
                if task.state in TERMINAL_STATES
            ]
            for key in finished:
                self._tasks.pop(key, None)
            self._cond.notify_all()
        if finished:
            self._notify(force=True)
        return len(finished)

    def stop(self, timeout: float = 5.0, cancel_running: bool = True) -> None:
        """Stop the dispatcher and optionally cancel/await running tasks."""
        with self._cond:
            self._stopping = True
            for task in self._tasks.values():
                if task.state in ("queued", "paused") and not task.started:
                    task.state = "cancelled"
                    task.finished_at = time.time()
                elif task.state in ("running", "paused") and cancel_running:
                    task.cancel_event.set()
                    task.pause_token.resume()
            self._cond.notify_all()
        if cancel_running:
            deadline = time.monotonic() + max(0.0, timeout)
            with self._cond:
                while self._running and time.monotonic() < deadline:
                    self._cond.wait(0.1)
        self._notify(force=True)

    def shutdown(self, timeout: float = 5.0) -> None:
        self.stop(timeout=timeout)

    # ------------------------------------------------------------------
    # Scheduler
    # ------------------------------------------------------------------

    def _dispatch_loop(self) -> None:
        while True:
            with self._cond:
                while (
                    not self._stopping
                    and len(self._running) >= self._max_concurrent
                ):
                    self._cond.wait(0.5)
                if self._stopping:
                    return
                task = next(
                    (
                        item
                        for item in self._tasks.values()
                        if item.state == "queued"
                    ),
                    None,
                )
                if task is None:
                    self._cond.wait(0.5)
                    continue
                task.state = "running"
                task.started_at = time.time()
                task.message = "开始"
                self._running.add(task.id)
                thread = threading.Thread(
                    target=self._run_task,
                    args=(task,),
                    daemon=True,
                    name=f"download-task-{task.id}",
                )
                self._threads[task.id] = thread
            thread.start()
            self._notify(force=True)

    def _run_task(self, task: QueueTask) -> None:
        try:
            result = task.runner(TaskContext(task, self))
            self._finish(task, "succeeded", result)
        except CancelledError:
            self._finish(task, "cancelled", TaskResult(message="已取消"))
        except BaseException as exc:  # noqa: BLE001 - keep the queue alive
            self._finish(task, "failed", TaskResult(message=str(exc)), error=str(exc))
        finally:
            with self._cond:
                self._running.discard(task.id)
                self._threads.pop(task.id, None)
                self._cond.notify_all()
            self._notify(force=True)

    def _finish(
        self,
        task: QueueTask,
        state: str,
        result: TaskResult | str | None,
        *,
        error: str = "",
    ) -> None:
        if isinstance(result, str):
            message = result
        elif result is not None:
            message = result.message
        else:
            message = ""
        with self._cond:
            task.state = state
            task.finished_at = time.time()
            task.error = error
            if message:
                task.message = message
            elif state == "succeeded":
                task.message = "完成"
            self._cond.notify_all()

    # ------------------------------------------------------------------
    # Progress / notifications
    # ------------------------------------------------------------------

    def _update_task(
        self,
        task: QueueTask,
        *,
        done_bytes: int | None = None,
        total_bytes: int | None = None,
        files_done: int | None = None,
        files_total: int | None = None,
        message: str | None = None,
    ) -> None:
        with self._lock:
            if done_bytes is not None:
                task.done_bytes = max(0, int(done_bytes))
            if total_bytes is not None:
                task.total_bytes = max(0, int(total_bytes))
            if files_done is not None:
                task.files_done = max(0, int(files_done))
            if files_total is not None:
                task.files_total = max(0, int(files_total))
            if message is not None:
                task.message = str(message)
        self._notify()

    def _log_task(self, task: QueueTask, message: str) -> None:
        clean = message.strip()
        if not clean:
            return
        with self._lock:
            task.logs.append(clean)
            if len(task.logs) > 50:
                del task.logs[:-50]
            task.message = clean
        callback = self._log_callback
        if callback is not None:
            try:
                callback(clean, "info")
            except Exception:  # noqa: BLE001 - logging must never break a job
                pass
        self._notify()

    def _notify(self, *, force: bool = False) -> None:
        callback = self._on_change
        if callback is None:
            return
        now = time.monotonic()
        if not force and now - self._last_notify < 0.15:
            return
        self._last_notify = now
        try:
            callback()
        except Exception:  # noqa: BLE001 - UI notification is best effort
            pass
