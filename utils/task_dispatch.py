from __future__ import annotations

from typing import Any


def enqueue_task(task: Any, *args: Any, **kwargs: Any) -> None:
    try:
        task.delay(*args, **kwargs)
    except Exception:
        task(*args, **kwargs)
