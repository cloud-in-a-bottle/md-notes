import asyncio
import time
from collections.abc import Callable
from collections.abc import Coroutine
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from openhost_test_harness import OpenhostStack


def _wait_healthy(stack: OpenhostStack, timeout: float = 60) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = stack.owner_session.get(f"{stack.url}/health", timeout=2)
            if r.status_code == 200:
                return
        except Exception:
            pass
        time.sleep(1)
    raise RuntimeError(f"App did not become healthy at {stack.url}/health within {timeout}s")


@pytest.fixture(scope="session")
def stack():
    with OpenhostStack() as s:
        _wait_healthy(s)
        yield s


@pytest.fixture
def run_async() -> Callable[[Coroutine[Any, Any, Any]], Any]:
    """Runs a coroutine to completion on a fresh event loop in a worker thread.

    Playwright's sync API (the browser tests) leaves an event loop running on the main thread for the rest of the
    session, so a plain asyncio.run() in a later test raises RuntimeError.
    """

    def run(coro: Coroutine[Any, Any, Any]) -> Any:
        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result()

    return run
