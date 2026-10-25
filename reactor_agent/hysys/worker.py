from __future__ import annotations

import multiprocessing as mp
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from concurrent.futures import Future, TimeoutError
from pathlib import Path
from typing import Any

from reactor_agent.domain.models import BackendCapabilities, ReactorModelSpec, SimulationResult
from reactor_agent.hysys.adapter import HysysAdapterError, HysysComAdapter


@contextmanager
def _global_hysys_lock(timeout_seconds: int):
    if os.name != "nt":
        yield
        return
    import msvcrt

    lock_path = os.path.join(tempfile.gettempdir(), "reactor-agent-hysys.lock")
    with open(lock_path, "a+b") as lock_file:
        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() == 0:
            lock_file.write(b"0")
            lock_file.flush()
        deadline = time.monotonic() + timeout_seconds
        while True:
            lock_file.seek(0)
            try:
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise HysysAdapterError("session_busy", "Timed out waiting for the shared HYSYS session.", True)
                time.sleep(0.1)
        try:
            yield
        finally:
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)


def _worker_main(connection: Any, progid: str, visible: bool) -> None:
    adapter = HysysComAdapter(progid, visible)
    try:
        while True:
            request = connection.recv()
            if request["operation"] == "shutdown":
                break
            try:
                operation = request["operation"]
                if operation == "inspect":
                    result = adapter.inspect().model_dump(mode="json")
                elif operation == "create_empty_case":
                    result = adapter.create_empty_case(request["destination"])
                elif operation == "copy_case":
                    result = adapter.copy_case(request["source"], request["destination"])
                elif operation == "read_streams":
                    result = [item.model_dump(mode="json") for item in adapter.read_open_case_streams(request["case_index"])]
                elif operation == "run_model":
                    result = adapter.run_model(ReactorModelSpec.model_validate(request["spec"]), request["destination"]).model_dump(mode="json")
                elif operation == "verify_saved_case":
                    result = adapter.verify_saved_case(request["source"])
                else:
                    raise HysysAdapterError("forbidden_operation", f"Operation {operation!r} is not allowlisted.")
                connection.send({"ok": True, "result": result})
            except HysysAdapterError as exc:
                connection.send({"ok": False, "category": exc.category, "message": str(exc), "retryable": exc.retryable})
            except Exception as exc:
                connection.send({"ok": False, "category": "worker_error", "message": str(exc), "retryable": False})
    finally:
        adapter.close_thread()
        connection.close()


class HysysWorker:
    """Single isolated process; all COM interfaces are created in its STA main thread."""

    def __init__(self, timeout_seconds: int | None = None):
        self.timeout_seconds = timeout_seconds or int(os.getenv("HYSYS_TIMEOUT_SECONDS", "300"))
        self._context = mp.get_context("spawn")
        self._lock = threading.Lock()
        self._process: mp.Process | None = None
        self._connection: Any = None

    def start(self) -> None:
        if self._process and self._process.is_alive():
            return
        parent, child = self._context.Pipe()
        self._process = self._context.Process(
            target=_worker_main,
            args=(child, os.getenv("HYSYS_PROGID", "HYSYS.Application"), os.getenv("HYSYS_VISIBLE", "false").lower() == "true"),
            name="hysys-sta-worker",
            daemon=True,
        )
        self._process.start()
        child.close()
        self._connection = parent

    def _call(self, operation: str, **payload: Any) -> Any:
        with self._lock:
            with _global_hysys_lock(self.timeout_seconds):
                self.start()
                if self._connection is None:
                    raise HysysAdapterError("worker_unavailable", "HYSYS worker pipe is unavailable.", True)
                try:
                    self._connection.send({"operation": operation, **payload})
                    if not self._connection.poll(self.timeout_seconds):
                        self.stop(force=True)
                        raise HysysAdapterError("timeout", f"HYSYS {operation} exceeded {self.timeout_seconds}s. Inspect HYSYS before resubmitting; the server may still be calculating.", False)
                    response = self._connection.recv()
                except (EOFError, OSError) as exc:
                    self.stop(force=True)
                    raise HysysAdapterError("worker_crashed", str(exc), True) from exc
                if not response["ok"]:
                    raise HysysAdapterError(response["category"], response["message"], response.get("retryable", False))
                return response["result"]

    def inspect(self) -> BackendCapabilities:
        return BackendCapabilities.model_validate(self._call("inspect"))

    def create_empty_case(self, destination: str | Path) -> dict[str, Any]:
        return self._call("create_empty_case", destination=str(Path(destination).resolve()))

    def copy_case(self, source: str | Path, destination: str | Path) -> dict[str, Any]:
        return self._call("copy_case", source=str(Path(source).resolve()), destination=str(Path(destination).resolve()))

    def read_streams(self, case_index: int = 0) -> list[dict[str, Any]]:
        return self._call("read_streams", case_index=case_index)

    def run_model(self, spec: ReactorModelSpec, destination: str | Path) -> SimulationResult:
        return SimulationResult.model_validate(self._call("run_model", spec=spec.model_dump(mode="json"), destination=str(destination)))

    def verify_saved_case(self, source: str | Path) -> dict[str, Any]:
        return self._call("verify_saved_case", source=str(source))

    def stop(self, force: bool = False) -> None:
        process = self._process
        if process is None:
            return
        if process.is_alive() and not force and self._connection is not None:
            try:
                self._connection.send({"operation": "shutdown"})
                process.join(timeout=5)
            except (BrokenPipeError, OSError):
                pass
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
        if self._connection:
            self._connection.close()
        self._connection = None
        self._process = None
