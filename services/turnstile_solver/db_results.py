"""Bounded in-memory results with a creation-based TTL (including final results)."""
import math
import os
import time

# Single solver event loop; these helpers never yield while mutating the cache.
results_db = {}


def _setting(name, default, minimum, maximum):
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


def _ttl_seconds():
    return _setting("SOLVER_RESULT_TTL_SECONDS", 900, 60, 86400)


def _max_results():
    return _setting("SOLVER_RESULT_MAX_ENTRIES", 1000, 1, 100000)


def _created_at(result, now):
    try:
        created = float(result.get("createTime"))
        if math.isfinite(created):
            return min(created, now)
    except (TypeError, ValueError, AttributeError):
        pass
    return now


async def init_db():
    await cleanup_old_results()
    print("[系统] 结果数据库初始化成功 (有界内存模式)")


async def save_result(task_id, task_type, data):
    now = time.time()
    previous = results_db.get(task_id)
    await cleanup_old_results()
    result = dict(data) if isinstance(data, dict) else {"value": data}
    # Final updates used to replace (and lose) createTime, making solved tokens immortal.
    source = previous if isinstance(previous, dict) else result
    result["createTime"] = _created_at(source, now)
    results_db[task_id] = result
    while len(results_db) > _max_results():
        oldest = min(results_db, key=lambda tid: _created_at(results_db[tid], now))
        del results_db[oldest]
    # Do not log solved tokens or challenge data.
    status = result.get("status") or ("failed" if result.get("value") == "CAPTCHA_FAIL" else "complete")
    print(f"[系统] 任务 {task_id} 状态更新: {status}")


async def load_result(task_id):
    result = results_db.get(task_id)
    if result is None:
        return None
    now = time.time()
    if not isinstance(result, dict):
        result = {"value": result, "createTime": now}
        results_db[task_id] = result
    result["createTime"] = _created_at(result, now)
    if now - result["createTime"] >= _ttl_seconds():
        results_db.pop(task_id, None)
        return None
    return result


async def cleanup_old_results(days_old=None):
    """Use the configured TTL by default; retain the legacy days_old override."""
    now = time.time()
    ttl = _ttl_seconds() if days_old is None else max(0, float(days_old) * 86400)
    to_delete = []
    for task_id, result in results_db.items():
        if not isinstance(result, dict):
            result = {"value": result, "createTime": now}
            results_db[task_id] = result
        # Normalize old/malformed records once instead of defaulting to 'now' forever.
        result["createTime"] = _created_at(result, now)
        if now - result["createTime"] >= ttl:
            to_delete.append(task_id)
    for task_id in to_delete:
        results_db.pop(task_id, None)
    return len(to_delete)
