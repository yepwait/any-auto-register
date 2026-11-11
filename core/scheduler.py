"""定时任务调度 - 账号有效性检测、trial 到期提醒"""
from datetime import datetime, timezone
from sqlmodel import Session, select
from .db import engine, AccountModel
from .registry import get, load_all
from .base_platform import Account, AccountStatus, RegisterConfig
import threading
import time
import os


class Scheduler:
    def __init__(self):
        self._running = False
        self._thread: threading.Thread = None
        self._wake_event = threading.Event()
        self._loop_interval_seconds = 60
        self._trial_check_interval_seconds = 3600
        self._last_trial_check_at = 0.0
        self._last_cpa_maintenance_at = 0.0
        self._last_inspection_at = 0.0
        self._last_sms_stock_at = 0.0
        self._last_web_pool_at = 0.0

    def start(self):
        if os.getenv("APP_ENABLE_SCHEDULER", "1").lower() in {"0", "false", "no", "off"}:
            print("[Scheduler] 当前为维护/预检模式，未启动自动任务")
            return
        if self._running:
            return
        self._running = True
        
        now = time.time()
        # 将其他维护任务设为当前时间，避免应用一启动就瞬间触发定时任务。
        # 巡检若已开启则允许启动后立即执行；通过界面新开启时也会主动唤醒调度器。
        self._last_trial_check_at = now
        self._last_cpa_maintenance_at = now
        self._last_inspection_at = 0.0
        self._last_sms_stock_at = now
        self._last_web_pool_at = now
        self._wake_event.clear()

        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        print("[Scheduler] 已启动")

    def stop(self):
        self._running = False
        self._wake_event.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=10)

    def trigger_inspection_now(self) -> None:
        """让已开启的自动巡检立即重新计算并唤醒调度线程。"""
        self._last_inspection_at = 0.0
        self._wake_event.set()

    def trigger_sms_stock_now(self) -> None:
        self._last_sms_stock_at = 0.0
        self._wake_event.set()

    def trigger_web_pool_now(self) -> None:
        self._last_web_pool_at = 0.0
        self._wake_event.set()

    def _get_web_pool_interval_seconds(self) -> int:
        from services.chatgpt_web_pool import WebPoolSettings

        settings = WebPoolSettings.from_store()
        return settings.interval_minutes * 60 if settings.enabled else 0

    def run_web_pool(self):
        from api.chatgpt_web_pool import enqueue

        return enqueue(trigger="schedule")

    def _get_sms_stock_interval_seconds(self) -> int:
        from services.sms_stock_service import SmsStockSettings

        settings = SmsStockSettings.from_store()
        return settings.interval_minutes * 60 if settings.enabled else 0

    def run_sms_stock(self):
        from api.sms_stock import enqueue

        return enqueue(trigger="schedule")

    def _loop(self):
        while self._running:
            now = time.time()
            try:
                if now - self._last_trial_check_at >= self._trial_check_interval_seconds:
                    try:
                        self.check_trial_expiry()
                        self._last_trial_check_at = now
                    except Exception as e:
                        print(f"[Scheduler] Trial 检查错误: {e}")

                cpa_interval = self._get_cpa_maintenance_interval_seconds()
                if cpa_interval and now - self._last_cpa_maintenance_at >= cpa_interval:
                    try:
                        self.check_cpa_credentials()
                        self._last_cpa_maintenance_at = now
                    except Exception as e:
                        print(f"[Scheduler] CPA 维护错误: {e}")

                inspection_interval = self._get_inspection_interval_seconds()
                if inspection_interval and now - self._last_inspection_at >= inspection_interval:
                    try:
                        result = self.run_inspection()
                        # 无匹配账号或已有任务时也更新时间，避免每分钟重复尝试。
                        self._last_inspection_at = now
                        if result:
                            print(f"[Scheduler] 已创建自动巡检任务: {result.get('task_id', '')}")
                        else:
                            print("[Scheduler] 自动巡检本轮跳过：无匹配账号或已有任务")
                    except Exception as e:
                        # 单次调度失败不能杀死后台线程，下一轮继续重试。
                        print(f"[Scheduler] 巡检错误: {e}")
                stock_interval = self._get_sms_stock_interval_seconds()
                if stock_interval and now - self._last_sms_stock_at >= stock_interval:
                    self._last_sms_stock_at = now
                    try:
                        result = self.run_sms_stock()
                        print(f"[Scheduler] 接码补库: {result.get('task_id') or result.get('reason')}")
                    except Exception as e:
                        print(f"[Scheduler] 接码补库错误: {type(e).__name__}")
                web_interval = self._get_web_pool_interval_seconds()
                if web_interval and now - self._last_web_pool_at >= web_interval:
                    self._last_web_pool_at = now
                    try:
                        result = self.run_web_pool()
                        print(f"[Scheduler] Web 号池: {result.get('task_id') or result.get('reason')}")
                    except Exception as e:
                        print(f"[Scheduler] Web 号池错误: {type(e).__name__}")
            except Exception as e:
                # 配置/数据库异常也不能让调度线程静默退出。
                print(f"[Scheduler] 调度循环错误: {e}")

            self._wake_event.wait(self._loop_interval_seconds)
            self._wake_event.clear()

    def _get_cpa_maintenance_interval_seconds(self) -> int:
        from services.cpa_manager import get_cpa_maintenance_interval_seconds

        return get_cpa_maintenance_interval_seconds()

    def _get_inspection_interval_seconds(self) -> int:
        from services.inspection_service import InspectionSettings

        settings = InspectionSettings.from_store()
        if not settings.enabled:
            return 0
        return max(60, int(settings.interval_minutes * 60))

    def run_inspection(self):
        from api.inspection import enqueue_scheduled_inspection

        return enqueue_scheduled_inspection()

    def check_trial_expiry(self):
        """检查 trial 到期账号，更新状态"""
        now = int(datetime.now(timezone.utc).timestamp())
        with Session(engine) as s:
            accounts = s.exec(
                select(AccountModel).where(AccountModel.status == "trial")
            ).all()
            updated = 0
            for acc in accounts:
                if acc.trial_end_time and acc.trial_end_time < now:
                    acc.status = AccountStatus.EXPIRED.value
                    acc.updated_at = datetime.now(timezone.utc)
                    s.add(acc)
                    updated += 1
            s.commit()
            if updated:
                print(f"[Scheduler] {updated} 个 trial 账号已到期")

    def check_accounts_valid(self, platform: str = None, limit: int = 50):
        """批量检测账号有效性"""
        load_all()
        with Session(engine) as s:
            q = select(AccountModel).where(
                AccountModel.status.in_(["registered", "trial", "subscribed"])
            )
            if platform:
                q = q.where(AccountModel.platform == platform)
            accounts = s.exec(q.limit(limit)).all()

        results = {"valid": 0, "invalid": 0, "error": 0}
        for acc in accounts:
            try:
                PlatformCls = get(acc.platform)
                plugin = PlatformCls(config=RegisterConfig())
                import json
                account_obj = Account(
                    platform=acc.platform,
                    email=acc.email,
                    password=acc.password,
                    user_id=acc.user_id,
                    region=acc.region,
                    token=acc.token,
                    extra=json.loads(acc.extra_json or "{}"),
                )
                valid = plugin.check_valid(account_obj)
                with Session(engine) as s:
                    a = s.get(AccountModel, acc.id)
                    if a:
                        if acc.platform != "chatgpt":
                            a.status = acc.status if valid else AccountStatus.INVALID.value
                        a.updated_at = datetime.now(timezone.utc)
                        s.add(a)
                        s.commit()
                if valid:
                    results["valid"] += 1
                else:
                    results["invalid"] += 1
            except Exception:
                results["error"] += 1
        return results

    def check_cpa_credentials(self):
        """清理 CPA 中的 error 凭证，并在低于阈值时自动补注册。"""
        from services.cpa_manager import maintain_cpa_credentials

        return maintain_cpa_credentials()


scheduler = Scheduler()
