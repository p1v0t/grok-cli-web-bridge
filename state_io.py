"""Atomic JSON and an OS lock shared by local bridge processes."""
import contextlib
import json
import os
import tempfile
import threading
import time

_thread_lock = threading.RLock()
_depth = threading.local()


@contextlib.contextmanager
def state_lock(directory):
    with _thread_lock:
        if getattr(_depth, "active", False):
            yield
            return
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (directory / ".state.lock").open("a+b") as handle:
            handle.seek(0, 2)
            if handle.tell() == 0:
                handle.write(b"0"); handle.flush()
            deadline = time.monotonic() + 15
            while True:
                try:
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Estado ocupado por outra operação. Tente novamente.") from None
                    time.sleep(0.05)
            _depth.active = True
            try:
                yield
            finally:
                _depth.active = False
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle, fcntl.LOCK_UN)


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            name = handle.name
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.flush(); os.fsync(handle.fileno())
        os.replace(name, path)
        name = None
    finally:
        if name is not None:
            os.unlink(name)
