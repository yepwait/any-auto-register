"""Turnstile Solver 的可选、仅管理自身子进程的生命周期。"""
import atexit
import os
import signal
import subprocess
import sys
import threading
import time

import requests

_proc: subprocess.Popen | None = None
_log_file = None
_lock = threading.Lock()
_stop_requested = threading.Event()
_start_thread: threading.Thread | None = None
_lifecycle_generation = 0
_start_thread_generation: int | None = None
_owned_descendants: dict[int, tuple[str, int]] = {}


def _solver_enabled() -> bool:
    # 协议注册不需要常驻浏览器，只有明确开启才自动拉起。
    return os.getenv("APP_ENABLE_SOLVER", "0").strip().lower() in {"1", "true", "yes", "on"}


def _solver_port() -> int:
    return int(os.getenv("SOLVER_PORT", "8889"))


def _solver_url() -> str:
    return (os.getenv("LOCAL_SOLVER_URL") or f"http://127.0.0.1:{_solver_port()}").rstrip("/")


def _solver_bind_host() -> str:
    return os.getenv("SOLVER_BIND_HOST", "0.0.0.0")


def _solver_browser_type() -> str:
    return os.getenv("SOLVER_BROWSER_TYPE", "camoufox")


def _solver_threads() -> int:
    """每个常驻浏览器都很吃内存，默认仅 1 个，仍允许显式调整。"""
    try:
        value = int(str(os.getenv("SOLVER_THREADS", "1")).strip())
    except (TypeError, ValueError):
        value = 1
    return max(1, min(value, 16))


def _startup_timeout() -> float:
    try:
        value = float(os.getenv("SOLVER_START_TIMEOUT", "30"))
    except (TypeError, ValueError):
        value = 30.0
    return max(1.0, min(value, 300.0))


def is_running() -> bool:
    try:
        r = requests.get(f"{_solver_url()}/", timeout=2)
        return r.status_code < 500
    except Exception:
        return False


def _linux_process_identity(pid: int):
    """Return (PPID, start ticks, PGID); start ticks protect against PID reuse."""
    if not sys.platform.startswith("linux"):
        return None
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as stream:
            # comm may contain spaces/parentheses; fields after its final ')' are stable.
            fields = stream.read().rsplit(")", 1)[1].split()
        return int(fields[1]), fields[19], int(fields[2])
    except (OSError, ValueError, IndexError):
        return None


def _remember_owned_descendants(proc: subprocess.Popen) -> None:
    """Playwright browsers detach into their own groups; record only this child's tree."""
    if not sys.platform.startswith("linux"):
        return
    roots = [proc.pid] if proc.poll() is None else []
    for pid, (started, _group) in list(_owned_descendants.items()):
        identity = _linux_process_identity(pid)
        if identity is not None and identity[1] == started:
            roots.append(pid)
    visited = set()
    while roots:
        parent = roots.pop()
        if parent in visited:
            continue
        visited.add(parent)
        try:
            # /proc children avoids broad process-name/port scans or extra dependencies.
            with open(f"/proc/{parent}/task/{parent}/children", encoding="utf-8") as stream:
                children = [int(value) for value in stream.read().split()]
        except (OSError, ValueError):
            continue
        for child in children:
            identity = _linux_process_identity(child)
            if identity is None or identity[0] != parent:
                continue
            _owned_descendants[child] = (identity[1], identity[2])
            roots.append(child)


def _signal_owned_descendants(*, force: bool) -> None:
    for pid, (started, _group) in reversed(list(_owned_descendants.items())):
        identity = _linux_process_identity(pid)
        if identity is None or identity[1] != started:
            continue
        try:
            sig = signal.SIGKILL if force else signal.SIGTERM
            if identity[2] == pid:
                os.killpg(pid, sig)  # Detached browser group, still identified by start ticks.
            else:
                os.kill(pid, sig)
        except ProcessLookupError:
            pass
        except OSError as exc:
            print(f"[Solver] 无法清理自有后代 PID={pid}: {exc}")


def _signal_owned_process(proc: subprocess.Popen, *, force: bool) -> None:
    """只终止 Popen 创建的进程组/进程树，绝不按端口或进程名杀其他服务。"""
    if os.name == "nt":
        if proc.poll() is not None:
            return  # 不对已经回收、可能被系统复用的 PID 执行 taskkill。
        # Windows 没有 killpg；/T 限定为本次 Popen 的后代进程。
        try:
            command = ["taskkill", "/PID", str(proc.pid), "/T"]
            if force:
                command.append("/F")
            result = subprocess.run(
                command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=5, check=False,
            )
            if result.returncode == 0 or proc.poll() is not None:
                return
        except (OSError, subprocess.TimeoutExpired):
            pass
        if not force:
            # 先强制处理整棵树，再考虑单独 kill 父进程，避免先杀父进程后丢失后代。
            _signal_owned_process(proc, force=True)
            return
    else:
        if proc.poll() is not None:
            # 已回收父 PID 可能被复用；只有身份仍匹配的自有后代可证明旧组仍属于我们。
            group_owned = False
            for pid, (started, _group) in _owned_descendants.items():
                identity = _linux_process_identity(pid)
                if identity is not None and identity[1] == started and identity[2] == proc.pid:
                    group_owned = True
                    break
            if not group_owned:
                return
        # start_new_session=True 确保 solver/driver 在独立进程组内。
        try:
            os.killpg(proc.pid, signal.SIGKILL if force else signal.SIGTERM)
            return
        except ProcessLookupError:
            return
        except OSError:
            pass
    if proc.poll() is None:
        if force:
            proc.kill()
        else:
            proc.terminate()


def _stop_owned_locked() -> None:
    """调用者持有 _lock；等待超时后强杀并回收，不遗失仍存活的句柄。"""
    global _proc, _log_file
    proc = _proc
    if proc is not None:
        _remember_owned_descendants(proc)
        try:
            _signal_owned_process(proc, force=False)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _remember_owned_descendants(proc)
                _signal_owned_descendants(force=True)
                _signal_owned_process(proc, force=True)
                proc.wait(timeout=5)
            # 主进程退出不代表其 detached 浏览器后代退出，按已记录的身份再清理。
            _remember_owned_descendants(proc)
            _signal_owned_descendants(force=True)
            _signal_owned_process(proc, force=True)
        except (OSError, subprocess.TimeoutExpired) as exc:
            print(f"[Solver] 停止进程失败，保留 PID={proc.pid} 以便重试: {exc}")
            if proc.poll() is None:
                return
        _proc = None
        _owned_descendants.clear()
    if _log_file is not None:
        _log_file.close()
        _log_file = None


def start(_generation: int | None = None):
    global _proc, _log_file
    with _lock:
        if _generation is not None and _generation != _lifecycle_generation:
            return  # 尚未取得锁的旧启动线程已被 stop() 取消。
        if not _solver_enabled():
            print("[Solver] 已禁用，跳过自动启动")
            return
        if _proc is not None:
            if _proc.poll() is None:
                print("[Solver] 自有进程已启动或正在启动")
                return
            _stop_owned_locked()
        if is_running():
            print("[Solver] 已有服务在运行（不接管该进程）")
            return
        solver_script = os.path.join(os.path.dirname(__file__), "turnstile_solver", "start.py")
        log_path = os.path.join(os.path.dirname(__file__), "turnstile_solver", "solver.log")
        _stop_requested.clear()
        _owned_descendants.clear()
        try:
            _log_file = open(log_path, "a", encoding="utf-8")
            process_options = {"start_new_session": True} if os.name != "nt" else {
                "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP,
            }
            _proc = subprocess.Popen(
                [
                    sys.executable, "-u", solver_script,
                    "--browser_type", _solver_browser_type(),
                    "--thread", str(_solver_threads()),
                    "--host", _solver_bind_host(),
                    "--port", str(_solver_port()),
                ],
                stdout=_log_file,
                stderr=subprocess.STDOUT,
                **process_options,
            )
            proc = _proc
        except Exception as exc:
            _stop_owned_locked()
            print(f"[Solver] 无法启动，日志: {log_path}: {exc}")
            return

    # 不在等待就绪期间持锁，stop() 可立即取消启动中的 solver。
    deadline = time.monotonic() + _startup_timeout()
    while not _stop_requested.wait(0.25):
        with _lock:
            if _proc is not proc:
                return
            _remember_owned_descendants(proc)
            if proc.poll() is not None:
                print(f"[Solver] 启动失败，退出码={proc.returncode}，日志: {log_path}")
                _stop_owned_locked()
                return
        if is_running():
            with _lock:
                if _proc is proc and not _stop_requested.is_set():
                    print(f"[Solver] 已启动 PID={proc.pid}")
            return
        if time.monotonic() >= deadline:
            with _lock:
                if _proc is proc:
                    print(f"[Solver] 启动超时，清理子进程，日志: {log_path}")
                    _stop_owned_locked()
            return


def stop():
    global _lifecycle_generation
    # 先通知启动线程，避免其在关闭后继续等待/重新拉起。
    _stop_requested.set()
    with _lock:
        _lifecycle_generation += 1
        _stop_owned_locked()


def start_async():
    """在后台启动；重复调用不堆积启动线程。"""
    global _start_thread, _start_thread_generation
    with _lock:
        if (_start_thread is not None and _start_thread.is_alive()
                and _start_thread_generation == _lifecycle_generation):
            return
        _start_thread_generation = _lifecycle_generation
        _start_thread = threading.Thread(
            target=start, args=(_lifecycle_generation,), daemon=True, name="solver-start",
        )
        _start_thread.start()


atexit.register(stop)
