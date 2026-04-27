"""
collector.py — System metric collection using psutil.

All functions return raw data (no formatting). Cross-platform;
uses psutil exclusively for portability.

Process scanning is throttled to reduce overhead on systems with
hundreds of processes.
"""

import os
import time
from typing import Dict, List, Tuple, Optional

import psutil

# ---------------------------------------------------------------------------
# Process scan throttle — avoids expensive iteration every cycle
# ---------------------------------------------------------------------------

_process_cache: List[Dict] = []
_last_process_scan: float = 0.0
PROCESS_SCAN_INTERVAL: float = 3.0  # seconds

# ---------------------------------------------------------------------------
# Protected / risky process names (case-insensitive match)
# Killing these can destabilise the system or crash the desktop session.
# ---------------------------------------------------------------------------

RISKY_PROCESSES: set[str] = {
    # macOS
    "kernel_task", "launchd", "windowserver", "dock", "finder",
    "systemuiserver", "loginwindow", "coreaudiod", "coreservicesd",
    "cfprefsd", "distnoted", "logd", "opendirectoryd", "fseventsd",
    "mds", "mds_stores", "spotlight", "airplayxpchelper",
    # Linux
    "systemd", "init", "kthreadd", "rcu_sched", "ksoftirqd",
    "kworker", "dbus-daemon", "networkmanager", "gdm", "sddm",
    "xorg", "xwayland", "pulseaudio", "pipewire",
    # Windows
    "system", "smss.exe", "csrss.exe", "wininit.exe", "services.exe",
    "lsass.exe", "svchost.exe", "explorer.exe", "dwm.exe",
    "winlogon.exe", "taskhostw.exe",
}


def is_risky_process(name: str) -> bool:
    """Check whether a process name belongs to the protected set."""
    return name.lower() in RISKY_PROCESSES


# ---------------------------------------------------------------------------
# CPU metrics
# ---------------------------------------------------------------------------


def get_cpu_usage() -> float:
    """Return overall CPU usage percentage (non-blocking, uses cached value)."""
    return psutil.cpu_percent(interval=None)


def get_per_core_usage() -> List[float]:
    """Return per-core CPU usage percentages."""
    return psutil.cpu_percent(percpu=True)


# ---------------------------------------------------------------------------
# CPU stats — context switches, interrupts, user/kernel time (Fix 3)
# ---------------------------------------------------------------------------


def get_cpu_stats() -> dict:
    """
    Return context switches, interrupts, and user/kernel/idle time split.
    cpu_stats()  → context switches + hardware/software interrupts
    cpu_times()  → time breakdown (user, system/kernel, idle, iowait on Linux)
    """
    stats = psutil.cpu_stats()
    times = psutil.cpu_times()

    result = {
        "ctx_switches":    stats.ctx_switches,      # total since boot
        "interrupts":      stats.interrupts,         # hardware interrupts since boot
        "soft_interrupts": getattr(stats, "soft_interrupts", 0),
        "user_time":   round(times.user, 2),
        "system_time": round(times.system, 2),
        "idle_time":   round(times.idle, 2),
        "iowait":      round(getattr(times, "iowait", 0.0), 2),  # Linux only
    }
    return result


_last_cpu_stats: Optional[dict] = None
_last_cpu_stats_time: float = 0.0


def get_cpu_stat_rates() -> dict:
    """Return context switches/sec and interrupts/sec as rates."""
    global _last_cpu_stats, _last_cpu_stats_time
    now = time.time()
    raw = get_cpu_stats()

    if _last_cpu_stats is None:
        _last_cpu_stats = raw
        _last_cpu_stats_time = now
        return {"ctx_switches_per_sec": 0, "interrupts_per_sec": 0, **raw}

    dt = max(now - _last_cpu_stats_time, 1e-3)
    ctx_rate = (raw["ctx_switches"] - _last_cpu_stats["ctx_switches"]) / dt
    irq_rate = (raw["interrupts"]   - _last_cpu_stats["interrupts"])   / dt

    _last_cpu_stats = raw
    _last_cpu_stats_time = now

    return {
        **raw,
        "ctx_switches_per_sec": round(max(ctx_rate, 0), 1),
        "interrupts_per_sec":   round(max(irq_rate, 0), 1),
    }


# ---------------------------------------------------------------------------
# Memory metrics (Fix 7 — buffers/cached/page fault rate)
# ---------------------------------------------------------------------------


_last_page_faults: Optional[Tuple[int, int, float]] = None  # minor, major, ts


def get_memory_usage() -> dict:
    """Return memory statistics with pressure estimate, buffers/cached, swap rates."""
    global _last_page_faults
    mem = psutil.virtual_memory()
    swap = psutil.swap_memory()

    # Use swap in/out as the page fault rate proxy — works cross-platform
    now = time.time()
    swap_in  = getattr(swap, "sin",  0)   # pages swapped in
    swap_out = getattr(swap, "sout", 0)   # pages swapped out
    swap_in_rate = swap_out_rate = 0.0

    if _last_page_faults is not None:
        last_in, last_out, last_ts = _last_page_faults
        dt = max(now - last_ts, 1e-3)
        swap_in_rate  = max((swap_in  - last_in)  / dt, 0.0)
        swap_out_rate = max((swap_out - last_out) / dt, 0.0)

    _last_page_faults = (swap_in, swap_out, now)

    ram_pressure  = mem.percent
    swap_pressure = swap.percent * 0.7
    pressure      = max(ram_pressure, swap_pressure)

    return {
        "total":          mem.total,
        "used":           mem.used,
        "available":      mem.available,
        "percent":        mem.percent,
        "buffers":        getattr(mem, "buffers", 0),   # Linux only
        "cached":         getattr(mem, "cached",  0),   # Linux only
        "shared":         getattr(mem, "shared",  0),
        "pressure":       round(pressure, 1),
        "swap_used":      swap.used,
        "swap_total":     swap.total,
        "swap_percent":   swap.percent,
        "swap_in_rate":   round(swap_in_rate, 2),    # pages/sec in
        "swap_out_rate":  round(swap_out_rate, 2),   # pages/sec out
    }


# ---------------------------------------------------------------------------
# Disk and network I/O (rate-based)
# ---------------------------------------------------------------------------

_last_disk: Optional[Tuple[int, int, float]] = None  # read_bytes, write_bytes, ts
_last_net: Optional[Tuple[int, int, float]] = None  # bytes_sent, bytes_recv, ts


def get_disk_io_rates() -> Dict[str, float]:
    """Return disk read/write throughput in MB/s."""
    global _last_disk
    now = time.time()
    counters = psutil.disk_io_counters()
    if counters is None:
        return {"read_mb_s": 0.0, "write_mb_s": 0.0}

    read_bytes = counters.read_bytes
    write_bytes = counters.write_bytes

    if _last_disk is None:
        _last_disk = (read_bytes, write_bytes, now)
        return {"read_mb_s": 0.0, "write_mb_s": 0.0}

    last_read, last_write, last_ts = _last_disk
    dt = max(now - last_ts, 1e-3)
    read_mb_s = max((read_bytes - last_read) / dt / (1024 * 1024), 0.0)
    write_mb_s = max((write_bytes - last_write) / dt / (1024 * 1024), 0.0)

    _last_disk = (read_bytes, write_bytes, now)
    return {
        "read_mb_s": round(read_mb_s, 2),
        "write_mb_s": round(write_mb_s, 2),
    }


def get_network_io_rates() -> Dict[str, float]:
    """Return network upload/download throughput in MB/s."""
    global _last_net
    now = time.time()
    counters = psutil.net_io_counters()
    if counters is None:
        return {"up_mb_s": 0.0, "down_mb_s": 0.0}

    sent = counters.bytes_sent
    recv = counters.bytes_recv

    if _last_net is None:
        _last_net = (sent, recv, now)
        return {"up_mb_s": 0.0, "down_mb_s": 0.0}

    last_sent, last_recv, last_ts = _last_net
    dt = max(now - last_ts, 1e-3)
    up_mb_s = max((sent - last_sent) / dt / (1024 * 1024), 0.0)
    down_mb_s = max((recv - last_recv) / dt / (1024 * 1024), 0.0)

    _last_net = (sent, recv, now)
    return {
        "up_mb_s": round(up_mb_s, 2),
        "down_mb_s": round(down_mb_s, 2),
    }


# ---------------------------------------------------------------------------
# Disk partitions with inode stats (Fix 6)
# ---------------------------------------------------------------------------


def get_disk_partitions() -> List[Dict]:
    """
    Return per-partition disk usage including inode stats.
    Skips pseudo-filesystems (proc, sysfs, devtmpfs, etc.)

    Inodes: every file/directory consumes exactly one inode.
    When inodes run out, you can't create new files even if space remains.
    """
    SKIP_FSTYPES = {"proc", "sysfs", "devtmpfs", "devpts", "tmpfs",
                    "cgroup", "cgroup2", "pstore", "debugfs",
                    "securityfs", "fusectl", "hugetlbfs", "mqueue"}
    results = []
    for part in psutil.disk_partitions(all=False):
        if part.fstype in SKIP_FSTYPES:
            continue
        try:
            usage = psutil.disk_usage(part.mountpoint)

            # Inode stats (Unix only — os.statvfs)
            inodes_used  = 0
            inodes_total = 0
            inodes_pct   = 0.0
            try:
                sv = os.statvfs(part.mountpoint)
                inodes_total = sv.f_files
                inodes_free  = sv.f_ffree
                inodes_used  = inodes_total - inodes_free
                inodes_pct   = round(inodes_used / inodes_total * 100, 1) if inodes_total > 0 else 0.0
            except (AttributeError, ZeroDivisionError, OSError):
                pass  # Windows doesn't have statvfs

            results.append({
                "mountpoint":   part.mountpoint,
                "fstype":       part.fstype,
                "total":        usage.total,
                "used":         usage.used,
                "free":         usage.free,
                "percent":      usage.percent,
                "inodes_used":  inodes_used,
                "inodes_total": inodes_total,
                "inodes_pct":   inodes_pct,
            })
        except (PermissionError, OSError):
            continue
    return results


# ---------------------------------------------------------------------------
# Load average
# ---------------------------------------------------------------------------


def get_load_average() -> Dict[str, float]:
    """Return 1m, 5m, 15m CPU load averages if supported by the OS."""
    try:
        one, five, fifteen = os.getloadavg()
        return {
            "one": round(one, 2),
            "five": round(five, 2),
            "fifteen": round(fifteen, 2),
        }
    except (OSError, AttributeError):
        return {"one": 0.0, "five": 0.0, "fifteen": 0.0}


# ---------------------------------------------------------------------------
# Synchronization & Deadlock stats (Fix 5)
# ---------------------------------------------------------------------------


def get_sync_stats() -> dict:
    """
    Aggregate synchronization and resource contention metrics.

    D-state (disk-sleep / STATUS_DISK_SLEEP) = process is blocked waiting
    for I/O to complete and cannot be interrupted — the closest observable
    proxy for deadlock / severe resource contention in a running system.

    Voluntary ctx switches   = process willingly yielded CPU (e.g. waiting for I/O).
    Involuntary ctx switches = scheduler preempted the process (e.g. time slice expired).
    A high involuntary ratio indicates heavy CPU contention between threads.
    """
    thread_states = {"running": 0, "sleeping": 0, "disk-sleep": 0,
                     "stopped": 0, "zombie": 0, "other": 0}
    total_threads  = 0
    total_fds      = 0
    d_state_procs  = []   # PIDs stuck in uninterruptible sleep
    vol_ctx_total  = 0
    invol_ctx_total = 0

    for proc in psutil.process_iter(["pid", "name", "status", "num_threads"]):
        try:
            info  = proc.info
            status = info.get("status", "other")
            nthreads = info.get("num_threads", 0) or 0
            total_threads += nthreads

            # Count by state
            if status in thread_states:
                thread_states[status] += 1
            else:
                thread_states["other"] += 1

            # D-state = blocked, uninterruptible
            if status == psutil.STATUS_DISK_SLEEP:
                d_state_procs.append({
                    "pid":  info["pid"],
                    "name": info["name"] or "Unknown",
                })

            # File descriptors (Unix)
            try:
                total_fds += proc.num_fds()
            except (AttributeError, psutil.AccessDenied):
                pass

            # Context switches
            try:
                ctx = proc.num_ctx_switches()
                vol_ctx_total   += ctx.voluntary
                invol_ctx_total += ctx.involuntary
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                pass

        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

    # System-wide FD limit (Unix only)
    try:
        import resource
        fd_limit = resource.getrlimit(resource.RLIMIT_NOFILE)[1]
    except Exception:
        fd_limit = -1

    total_ctx = vol_ctx_total + invol_ctx_total
    invol_ratio = round(invol_ctx_total / total_ctx * 100, 1) if total_ctx > 0 else 0.0

    return {
        "thread_states":    thread_states,
        "total_threads":    total_threads,
        "total_fds":        total_fds,
        "fd_limit":         fd_limit,
        "d_state_procs":    d_state_procs[:10],   # cap at 10 for payload size
        "d_state_count":    len(d_state_procs),
        "vol_ctx_total":    vol_ctx_total,
        "invol_ctx_total":  invol_ctx_total,
        "invol_ctx_ratio":  invol_ratio,           # % of ctx switches that were involuntary
    }


# ---------------------------------------------------------------------------
# Process list (throttled) — Fix 4: extended with RSS, VMS, state, threads
# ---------------------------------------------------------------------------


def classify_process(name: str, username: Optional[str], nice: Optional[int]) -> str:
    """Classify process as 'system', 'user', or 'background'."""
    if is_risky_process(name) or (username and username.lower() in {"root", "system", "localservice", "networkservice"}):
        return "system"
    if nice is not None and nice > 0:
        return "background"
    return "user"


def get_process_list() -> List[Dict]:
    """
    Return a list of running processes with:
    - pid, name, cpu, memory, rss, status, threads, fds
    - vol_ctx, invol_ctx (per-process context switches)
    - risky (bool), type: "system" | "user" | "background"

    Results are cached for PROCESS_SCAN_INTERVAL seconds to avoid
    expensive iteration on every metric cycle.
    """
    global _process_cache, _last_process_scan

    now = time.time()
    if now - _last_process_scan < PROCESS_SCAN_INTERVAL and _process_cache:
        return _process_cache

    procs: List[Dict] = []
    attrs = ["pid", "name", "cpu_percent", "memory_percent",
             "memory_info", "username", "nice", "status", "num_threads"]

    for proc in psutil.process_iter(attrs):
        try:
            info = proc.info
            pname = info["name"] or "Unknown"
            risky = is_risky_process(pname)
            ptype = classify_process(pname, info.get("username"), info.get("nice"))

            mem_info = info.get("memory_info")
            rss = mem_info.rss if mem_info else 0

            # Voluntary vs non-voluntary context switches (per-process)
            try:
                ctx = proc.num_ctx_switches()
                vol_ctx   = ctx.voluntary
                invol_ctx = ctx.involuntary
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                vol_ctx = invol_ctx = 0

            # Open file descriptors (Unix only)
            try:
                fds = proc.num_fds()
            except (AttributeError, psutil.AccessDenied, psutil.NoSuchProcess):
                fds = -1  # -1 = unavailable (Windows)

            procs.append({
                "pid":      info["pid"],
                "name":     pname,
                "cpu":      round(info.get("cpu_percent") or 0.0, 1),
                "memory":   round(info.get("memory_percent") or 0.0, 1),
                "rss":      rss,          # bytes — format in frontend
                "status":   info.get("status", "?"),   # running/sleeping/zombie/disk-sleep
                "threads":  info.get("num_threads", 0),
                "fds":      fds,
                "vol_ctx":  vol_ctx,
                "invol_ctx": invol_ctx,
                "risky":    risky,
                "type":     ptype,
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

    _process_cache = procs
    _last_process_scan = now
    return procs


# ---------------------------------------------------------------------------
# Process Tree — parent-child hierarchy (fork/exec visualisation)
# ---------------------------------------------------------------------------


def get_process_tree(compact: bool = True) -> List[Dict]:
    """
    Build a flat list of processes with parent-child (PPID) links and depth.

    OS Concept: Every process is created via fork(). The resulting tree
    starts at PID 1 (init/launchd/systemd) and branches out.  This
    one-pass algorithm avoids recursion: it builds a children dict keyed
    by PID, then walks root → leaves assigning depth.

    compact mode (default ON): Hides childless system-owned processes that
    are idle (CPU < 0.1, Memory < 0.1). This dramatically reduces noise
    on macOS where launchd spawns 400+ idle daemon children.

    Returns a list of dicts sorted by tree order (DFS), each containing:
      pid, ppid, name, cpu, memory, status, threads, depth, username, is_user
    """
    procs_by_pid: Dict[int, Dict] = {}
    children_map: Dict[int, List[int]] = {}

    for proc in psutil.process_iter(["pid", "ppid", "name", "cpu_percent",
                                      "memory_percent", "status", "num_threads",
                                      "username"]):
        try:
            info = proc.info
            pid = info["pid"]
            ppid = info.get("ppid", 0) or 0
            username = info.get("username", "") or ""
            # On macOS, user processes run under the logged-in user
            is_user = username not in ("root", "_windowserver", "_hidd",
                                        "_spotlight", "_coreaudiod", "")
            procs_by_pid[pid] = {
                "pid": pid,
                "ppid": ppid,
                "name": info["name"] or "Unknown",
                "cpu": round(info.get("cpu_percent") or 0.0, 1),
                "memory": round(info.get("memory_percent") or 0.0, 1),
                "status": info.get("status", "?"),
                "threads": info.get("num_threads", 0),
                "depth": 0,
                "children_count": 0,
                "username": username,
                "is_user": is_user,
            }
            children_map.setdefault(ppid, []).append(pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

    # ── Compact mode: prune idle system leaf nodes ──
    if compact:
        keep_pids = set()
        for pid, p in procs_by_pid.items():
            has_kids = len(children_map.get(pid, [])) > 0
            is_interesting = (
                has_kids
                or p["is_user"]
                or p["cpu"] >= 0.1
                or p["memory"] >= 0.1
                or p["status"] in ("zombie", "disk-sleep")
                or pid <= 1
            )
            if is_interesting:
                keep_pids.add(pid)
                # Walk up ancestors to keep the tree connected
                ancestor = p["ppid"]
                while ancestor in procs_by_pid and ancestor not in keep_pids:
                    keep_pids.add(ancestor)
                    ancestor = procs_by_pid[ancestor]["ppid"]

        # Rebuild children_map with only kept PIDs
        filtered_children: Dict[int, List[int]] = {}
        for pid in keep_pids:
            ppid = procs_by_pid[pid]["ppid"]
            if ppid in keep_pids:
                filtered_children.setdefault(ppid, []).append(pid)
        children_map = filtered_children
        active_pids = keep_pids
    else:
        active_pids = set(procs_by_pid.keys())

    # Find roots — PIDs whose parent is not in active set OR parent is self
    roots = [pid for pid in active_pids
             if procs_by_pid[pid]["ppid"] not in active_pids
             or procs_by_pid[pid]["ppid"] == pid]

    # DFS to assign depth and produce tree-ordered list
    result: List[Dict] = []
    stack = [(pid, 0) for pid in sorted(roots, reverse=True)]
    visited: set = set()

    while stack:
        pid, depth = stack.pop()
        if pid in visited or pid not in procs_by_pid:
            continue
        visited.add(pid)
        if compact and pid not in keep_pids:
            continue
        node = procs_by_pid[pid]
        node["depth"] = depth
        kids = children_map.get(pid, [])
        node["children_count"] = len(kids)
        result.append(node)

        # Sort: processes with children first, then by PID ascending
        kids_sorted = sorted(kids, key=lambda c: (-len(children_map.get(c, [])), c))
        for child_pid in reversed(kids_sorted):
            if child_pid != pid:
                stack.append((child_pid, depth + 1))

    return result


# ---------------------------------------------------------------------------
# Process Detail — deep dive into a single PID (viva-ready)
# ---------------------------------------------------------------------------


def get_process_detail(pid: int) -> Optional[Dict]:
    """
    Return detailed OS-level information for a single process.

    OS Concepts covered:
    - Page faults (minor = in-memory, major = required disk I/O)
    - Open file descriptors (Unix "everything is a file")
    - Network connections (sockets as file descriptors)
    - Context switches (voluntary = I/O wait, involuntary = preempted)
    - Memory maps (virtual address space regions)
    - CPU affinity (which cores the scheduler can assign)
    - Nice value (process priority / scheduling)
    """
    try:
        proc = psutil.Process(pid)
        info = proc.as_dict(attrs=[
            "pid", "name", "ppid", "username", "status",
            "cpu_percent", "memory_percent", "memory_info",
            "num_threads", "nice", "create_time",
        ])

        # Page faults (memory_info contains pfaults on macOS, others on Linux)
        mem_info = info.get("memory_info")
        page_faults = {
            "rss": mem_info.rss if mem_info else 0,
            "vms": mem_info.vms if mem_info else 0,
        }
        # macOS has pfaults; Linux has no direct per-process page fault in memory_info
        if mem_info and hasattr(mem_info, "pfaults"):
            page_faults["total_faults"] = mem_info.pfaults
        if mem_info and hasattr(mem_info, "pageins"):
            page_faults["major_faults"] = mem_info.pageins  # required disk read

        # Context switches
        try:
            ctx = proc.num_ctx_switches()
            ctx_switches = {"voluntary": ctx.voluntary, "involuntary": ctx.involuntary}
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            ctx_switches = {"voluntary": 0, "involuntary": 0}

        # Open files
        try:
            open_files = [{"path": f.path, "fd": f.fd}
                          for f in proc.open_files()[:20]]  # cap at 20
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            open_files = []

        # Network connections (sockets)
        try:
            connections = []
            for c in proc.connections(kind="inet")[:15]:
                connections.append({
                    "fd": c.fd,
                    "family": "IPv4" if c.family.name == "AF_INET" else "IPv6",
                    "type": "TCP" if c.type.name == "SOCK_STREAM" else "UDP",
                    "local": f"{c.laddr.ip}:{c.laddr.port}" if c.laddr else "",
                    "remote": f"{c.raddr.ip}:{c.raddr.port}" if c.raddr else "",
                    "status": c.status,
                })
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            connections = []

        # File descriptor count
        try:
            num_fds = proc.num_fds()
        except (AttributeError, psutil.AccessDenied, psutil.NoSuchProcess):
            num_fds = -1

        # Threads list
        try:
            threads = [{"id": t.id, "user_time": round(t.user_time, 3),
                         "system_time": round(t.system_time, 3)}
                       for t in proc.threads()[:30]]
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            threads = []

        # CPU affinity (not available on macOS)
        try:
            affinity = proc.cpu_affinity()
        except (AttributeError, psutil.AccessDenied, psutil.NoSuchProcess):
            affinity = None

        return {
            "pid": info["pid"],
            "name": info["name"],
            "ppid": info.get("ppid", 0),
            "username": info.get("username", "?"),
            "status": info.get("status", "?"),
            "cpu": round(info.get("cpu_percent") or 0.0, 1),
            "memory": round(info.get("memory_percent") or 0.0, 1),
            "nice": info.get("nice"),
            "num_threads": info.get("num_threads", 0),
            "num_fds": num_fds,
            "create_time": info.get("create_time", 0),
            "page_faults": page_faults,
            "ctx_switches": ctx_switches,
            "open_files": open_files,
            "connections": connections,
            "threads": threads,
            "cpu_affinity": affinity,
        }
    except psutil.NoSuchProcess:
        return None
    except psutil.AccessDenied:
        return {"error": f"Access denied for PID {pid}"}


# ---------------------------------------------------------------------------
# Health Score — weighted composite metric (0–100)
# ---------------------------------------------------------------------------


def compute_health_score(cpu: float, mem_pressure: float,
                         swap_in_rate: float, swap_out_rate: float,
                         d_state_count: int, load_one: float,
                         zombie_count: int) -> Dict:
    """
    Compute a composite system health score (100 = perfect, 0 = critical).

    Weights:
      CPU usage       → 25%
      Memory pressure → 25%
      Swap activity   → 15%  (page fault rate proxy)
      D-state procs   → 15%  (deadlock / I/O block proxy)
      Load average    → 10%  (relative to core count)
      Zombie procs    → 10%  (process lifecycle issues)

    Thresholds are tuneable — lower them during demos to trigger
    WARNING/CRITICAL states on a quiet laptop.
    """
    cores = psutil.cpu_count(logical=True) or 1

    # Allow some background CPU jitter (up to 15%) without penalty
    cpu_penalty = min(max(0, cpu - 15) * (100 / 85), 100)
    
    # Modern OSs cache heavily, so 50-60% memory usage is normal idle.
    # We only start penalizing memory above 60%
    mem_penalty = min(max(0, mem_pressure - 60) * (100 / 40), 100)

    # Swap: >500 pages/sec = fully bad (tune to 50 for demos)
    swap_rate = swap_in_rate + swap_out_rate
    swap_penalty = min(swap_rate / 500 * 100, 100)

    # D-state: each process stuck = 25 penalty points
    dstate_penalty = min(d_state_count * 25, 100)

    # Load: ratio to core count; > 2x = fully bad
    load_ratio = load_one / cores if cores > 0 else 0
    load_penalty = min(load_ratio / 2.0 * 100, 100)

    # Zombie: each zombie = 10 penalty points
    zombie_penalty = min(zombie_count * 10, 100)

    weighted = (
        cpu_penalty * 0.25 +
        mem_penalty * 0.25 +
        swap_penalty * 0.15 +
        dstate_penalty * 0.15 +
        load_penalty * 0.10 +
        zombie_penalty * 0.10
    )

    score = max(0, round(100 - weighted, 1))

    # Classification
    if score >= 80:
        grade = "Healthy"
        level = "normal"
    elif score >= 60:
        grade = "Warning"
        level = "warning"
    elif score >= 40:
        grade = "Stressed"
        level = "warning"
    else:
        grade = "Critical"
        level = "critical"

    return {
        "score": score,
        "grade": grade,
        "level": level,
        "breakdown": {
            "cpu": round(cpu_penalty, 1),
            "memory": round(mem_penalty, 1),
            "swap": round(swap_penalty, 1),
            "dstate": round(dstate_penalty, 1),
            "load": round(load_penalty, 1),
            "zombie": round(zombie_penalty, 1),
        },
    }
