"""How busy the server really is, measured every second: processor, memory and disk.

Nothing here counts people. New heavy work (another video, another recording) starts only while the machine has
room, waits in line for a moment when it is nearly full, and is let through the second it frees up again. That is
what keeps a rush from slowing everyone down or crashing the server."""
import os
import shutil
import threading
import time

from . import config

BUSY_CPU = float(config.env("PEAKCUT_BUSY_CPU", "82"))          # percent: above this, no extra videos are started
MIN_FREE_RAM_GB = float(config.env("PEAKCUT_MIN_FREE_RAM_GB", "1.5"))
MIN_FREE_DISK_GB = float(config.env("PEAKCUT_MIN_FREE_DISK_GB", "3"))

_state = {"cpu": 0.0, "ram_free": 99.0, "disk_free": 99.0, "at": 0.0}
_force = None            # tests only: pretend the server is busy (True) or free (False)
_started = False
_lock = threading.Lock()


def _cpu_times():
    """(busy, total) processor time so far, in any unit."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        idle, kernel, user = wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME()
        ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user))
        n = lambda f: (f.dwHighDateTime << 32) | f.dwLowDateTime          # noqa: E731
        total = n(kernel) + n(user)                                        # kernel time includes idle time
        return total - n(idle), total
    with open("/proc/stat", encoding="ascii") as f:
        v = [int(x) for x in f.readline().split()[1:9]]
    return sum(v) - v[3] - v[4], sum(v)                                    # everything except idle and waiting on disk


def _process_cpu_ticks() -> int:
    """CPU ticks used by processes visible inside this environment."""
    total = 0
    try:
        pids = os.listdir("/proc")
    except OSError:
        return 0
    for pid in pids:
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/stat", encoding="ascii") as f:
                raw = f.read()
            fields = raw[raw.rfind(")") + 2:].split()
            total += int(fields[11]) + int(fields[12])
        except (OSError, ValueError, IndexError):
            continue
    return total


def _ram_free_gb() -> float:
    if os.name == "nt":
        import ctypes

        class M(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong)] + [
                (n, ctypes.c_ulonglong) for n in ("total", "avail", "tpf", "apf", "tv", "av", "aev")]
        m = M()
        m.dwLength = ctypes.sizeof(M)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
        return m.avail / 1e9
    with open("/proc/meminfo", encoding="ascii") as f:
        for line in f:
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / 1e6
    return 99.0


def _watch():
    try:
        last = _cpu_times()
    except Exception:  # noqa: BLE001 - a system whose processor we cannot measure (a Mac): leave that out, keep the rest
        last = None
    proc_last = None
    while True:
        time.sleep(1)
        if last is not None:
            try:
                now = _cpu_times()
                used, total = now[0] - last[0], now[1] - last[1]
                last = now
                if total > 0 and used > 0:
                    # Normal system-wide CPU measurement.
                    _state["cpu"] = 0.55 * _state["cpu"] + 0.45 * (100.0 * used / total)
                else:
                    # Android PRoot may expose a frozen /proc/stat.
                    # Measure CPU ticks from processes visible inside this environment.
                    proc_now = _process_cpu_ticks()
                    mono_now = time.monotonic()
                    if proc_last is not None:
                        old_ticks, old_time = proc_last
                        elapsed = mono_now - old_time
                        if elapsed > 0:
                            tick_rate = os.sysconf("SC_CLK_TCK")
                            try:
                                cpu_count = len(os.sched_getaffinity(0))
                            except (AttributeError, OSError):
                                cpu_count = os.cpu_count() or 1
                            delta = max(0, proc_now - old_ticks)
                            percent = 100.0 * delta / (tick_rate * elapsed * max(1, cpu_count))
                            _state["cpu"] = 0.55 * _state["cpu"] + 0.45 * min(100.0, percent)
                    proc_last = (proc_now, mono_now)
            except Exception:  # noqa: BLE001
                pass
        try:
            _state["ram_free"] = _ram_free_gb()
        except Exception:  # noqa: BLE001
            pass
        try:
            if time.time() - _state["at"] > 15:
                _state["disk_free"] = shutil.disk_usage(config.DATA_DIR).free / 1e9
                _state["at"] = time.time()
        except Exception:  # noqa: BLE001
            pass


def start():
    global _started
    with _lock:
        if not _started:
            _started = True
            threading.Thread(target=_watch, daemon=True, name="load").start()


def busy() -> bool:
    """True while the machine is close to full: nothing extra should start right now."""
    if _force is not None:
        return _force
    start()
    return _state["cpu"] >= BUSY_CPU or _state["ram_free"] < MIN_FREE_RAM_GB


def disk_full() -> bool:
    start()
    return _state["disk_free"] < MIN_FREE_DISK_GB


def state() -> dict:
    start()
    return {"cpu": round(_state["cpu"]), "ram_free_gb": round(_state["ram_free"], 1), "disk_free_gb": round(_state["disk_free"], 1),
            "busy": busy(), "busy_cpu": BUSY_CPU}
