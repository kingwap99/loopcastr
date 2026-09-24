"""One build at a time.

A build writes the master playlist, the per-mode playlist, the concat list and the manifest. Two of
them at once - the console button pressed while the refresh service is already rebuilding, or a manual
run during either - interleave those writes, and the last writer wins for each file. Measured: killing a
build made refreshwatch retry, and two builds then wrote the same four files for ten minutes; the lists
happened to come out identical, and the next playout restart came from whichever finished last.

flock on a file in the install directory. The kernel drops it when the holder dies, so a killed or
crashed build never leaves a stale lock behind, which a plain pid file would.
"""
import fcntl
import os

EXIT_BUSY = 4          # mode_build's exit code when another build owns the slot


def lock_path(here):
    return os.path.join(here, ".build.lock")


def _open(path):
    return os.open(path, os.O_RDWR | os.O_CREAT, 0o644)


def holder(here):
    """The pid recorded by the current holder, or 0 when it cannot be read."""
    try:
        with open(lock_path(here), encoding="utf-8") as fh:
            return int((fh.read().strip() or "0"))
    except (OSError, ValueError):
        return 0


def busy(here):
    """Is another process holding the build slot right now? Never keeps the lock."""
    try:
        fd = _open(lock_path(here))
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
    return False


class BuildLock:
    """Held for the lifetime of the process that acquired it."""

    def __init__(self, here):
        self.path = lock_path(here)
        self.fd = None

    def acquire(self):
        """True when this process now owns the build slot, False when someone else does."""
        try:
            fd = _open(self.path)
        except OSError:
            return True                     # cannot create it (read-only dir): do not block the build
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        try:
            os.ftruncate(fd, 0)
            os.write(fd, ("%d\n" % os.getpid()).encode())
        except OSError:
            pass
        self.fd = fd                        # deliberately kept open until the process exits
        return True
