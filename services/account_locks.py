"""按账号 ID 的进程内互斥锁。

巡检、手动补 RT、接码补库都可能刷新同一批凭证。入队锁只保护“创建任务”这段，
挡不住两个已创建任务同时处理同一个账号，因此对会轮换凭证的操作再加一层账号级
RLock。单进程内即可防止凭证互相覆盖；多实例部署仍需数据库级租约/版本号。
"""
from __future__ import annotations

import threading

_guard = threading.Lock()
_locks: dict[int, threading.RLock] = {}


def get_account_lock(account_id: int) -> threading.RLock:
    key = int(account_id)
    with _guard:
        lock = _locks.get(key)
        if lock is None:
            lock = threading.RLock()
            _locks[key] = lock
        return lock
