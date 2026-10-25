"""One local queue consumer per database; HYSYS itself remains globally serialized."""
import os
import time
from reactor_agent.service import ReactorAgentService


def run_consumer():
    service = ReactorAgentService()
    lock_path = service.database.path.with_suffix(".consumer.lock")
    with open(lock_path, "a+b") as lock:
        if os.name == "nt":
            import msvcrt
            lock.seek(0)
            lock.write(b"0")
            lock.flush()
            lock.seek(0)
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise RuntimeError("A queue consumer is already running for this database.")
        service.database.recover_interrupted_queue_items(older_than_seconds=int(os.getenv("HYSYS_TIMEOUT_SECONDS", "300")) + 60)
        print("Local queue consumer ready", flush=True)
        next_recovery = time.monotonic() + 30
        while True:
            if time.monotonic() >= next_recovery:
                service.database.recover_interrupted_queue_items(older_than_seconds=int(os.getenv("HYSYS_TIMEOUT_SECONDS", "300")) + 60)
                next_recovery = time.monotonic() + 30
            result = service.run_one_queued()
            if result:
                print(f"Queue run completed: {result['status']}", flush=True)
            else:
                time.sleep(0.5)
