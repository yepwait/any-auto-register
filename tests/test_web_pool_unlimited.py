"""Zero-count policies and result presentation, fully isolated/offline."""
import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import BackgroundTasks, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, create_engine

from api import chatgpt_web_pool as api, tasks
from core import db
from core.task_runtime import RegisterTaskControl, RegisterTaskStore, StopTaskRequested
from services import chatgpt_web_pool as pool, web_pool_events as events


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    for module in (pool, tasks, db): monkeypatch.setattr(module, 'engine', engine)
    saved = {events.SECRET_KEY: 'test-key'}
    monkeypatch.setattr(pool.config_store, 'get', lambda key, default='': saved.get(key, default))
    monkeypatch.setattr(pool.config_store, 'get_all', lambda: dict(saved))
    monkeypatch.setattr(pool.config_store, 'set_many', saved.update)
    monkeypatch.setattr(tasks, '_task_store', RegisterTaskStore())
    monkeypatch.setattr(tasks, '_save_task_log', Mock())
    monkeypatch.setattr(tasks, '_task_admission_closed', False)
    monkeypatch.setattr(events, 'wake', Mock())
    monkeypatch.setattr(pool.requests.Session, 'request', Mock(side_effect=AssertionError('real network forbidden')))
    # Virtual pacing lets unlimited loops prove >100 attempts without real sleep.
    clock = SimpleNamespace(now=0.0, waits=[])
    def wait(seconds): clock.waits.append(seconds); clock.now += seconds
    monkeypatch.setattr(pool, 'time', SimpleNamespace(monotonic=lambda: clock.now))
    monkeypatch.setattr(pool, 'threading', SimpleNamespace(Event=lambda: SimpleNamespace(wait=wait)))
    monkeypatch.setattr(pool.WebPoolClient, 'snapshot', Mock(return_value=snapshot(0)))
    yield SimpleNamespace(saved=saved, clock=clock)
    engine.dispose()


def settings(**changes):
    return pool.WebPoolSettings(**{'base_url':'https://pool.invalid', 'admin_token':'test-admin',
        'max_attempts_per_run':0,'max_consecutive_failures':0,'daily_registration_limit':0,'delay_seconds':0,**changes})


def snapshot(value):
    return pool.summarize([{'status':'正常','quota':value}] if value else [])


def run(cfg=None, register=None, control=None, should_continue=None, progress=None):
    return pool.replenish(cfg or settings(), control=control or RegisterTaskControl(),
        register=register or Mock(return_value=None), should_continue=should_continue or (lambda:True),
        log=lambda _:None, progress=progress or (lambda _:None))


@pytest.mark.parametrize('attempts,failures',[(0,0),(0,3),(5,0)])
def test_zero_policy_round_trips_without_default_substitution(attempts, failures):
    cfg=settings(max_attempts_per_run=attempts,max_consecutive_failures=failures)
    pool.save_settings(cfg)
    assert pool.WebPoolSettings.from_store()==cfg
    assert pool.WebPoolSettings().max_attempts_per_run==5
    assert pool.WebPoolSettings().max_consecutive_failures==3


@pytest.mark.parametrize('key,value', [('max_attempts_per_run',-1),('max_attempts_per_run',101),('max_consecutive_failures',-1),('max_consecutive_failures',11)])
def test_invalid_bounds_still_rejected(key,value):
    with pytest.raises(ValueError):settings(**{key:value})


def test_unlimited_attempts_still_stops_at_nonzero_consecutive_limit():
    register=Mock(return_value=None)
    result=run(settings(max_consecutive_failures=3),register)
    assert result.attempts==result.registrations==register.call_count==3
    assert '连续失败' in result.reason


def test_unlimited_failures_still_stops_at_finite_attempt_limit():
    register=Mock(return_value=None)
    result=run(settings(max_attempts_per_run=8),register)
    assert result.attempts==result.registrations==register.call_count==8
    assert '每轮' in result.reason


def test_both_unlimited_run_past_hundred_without_hidden_count_cap(isolated):
    register=Mock(return_value=None)
    result=run(settings(daily_registration_limit=125),register=register)
    assert result.attempts==result.registrations==125
    assert result.budget_exhausted
    assert pool.daily_used()==125
    assert result.error_count==125 and len(result.errors)==100
    assert isolated.clock.now>=124


def test_all_three_zero_keep_running_until_user_stops(isolated):
    control=RegisterTaskControl();register=Mock(return_value=None)
    latest=[]
    def progress(result):
        latest[:] = [result]
        if result.registrations == 125 and result.error_count == 125:
            control.request_stop()
    with pytest.raises(StopTaskRequested):
        run(settings(),register=register,control=control,progress=progress)
    assert register.call_count == 125
    assert pool.daily_used() == 125
    assert latest[0].error_count == 125 and len(latest[0].errors) == 100


def test_unlimited_counts_recheck_target_even_after_failed_registration(monkeypatch):
    register=Mock(return_value=None)
    monkeypatch.setattr(pool.WebPoolClient,'snapshot',lambda _:snapshot(100 if register.call_count>=4 else 0))
    result=run(register=register)
    assert result.registrations==4
    assert result.snapshot['remaining_quota']==100
    assert '达到目标' in result.reason


def test_unlimited_counts_keep_daily_budget_reservation():
    register=Mock(return_value=None)
    result=run(settings(daily_registration_limit=3),register)
    assert register.call_count==result.registrations==result.attempts==3
    assert result.budget_exhausted
    assert pool.daily_used()==3


def test_stop_during_retry_pacing_prevents_next_attempt(isolated):
    control=RegisterTaskControl();register=Mock(return_value=None)
    original_wait=lambda seconds: setattr(isolated.clock,'now',isolated.clock.now+seconds)
    def wait(seconds): original_wait(seconds);control.request_stop()
    pool.threading.Event=lambda:SimpleNamespace(wait=wait)
    with pytest.raises(StopTaskRequested):run(register=register,control=control)
    assert register.call_count==1


def test_config_change_stops_before_next_attempt_without_using_budget():
    register=Mock(return_value=None)
    # Existing non-raising end-of-run path remains compatible for a zero delay finite run.
    with pytest.raises(StopTaskRequested):run(register=register,should_continue=lambda:register.call_count<1)
    assert register.call_count==pool.daily_used()==1


def test_unknown_quota_prevents_registration_even_all_limits_zero(monkeypatch):
    monkeypatch.setattr(pool.WebPoolClient,'snapshot',Mock(return_value={**snapshot(0),'remaining_quota':None,'quota_known':False}))
    register=Mock()
    with pytest.raises(pool.QuotaUnavailable):run(register=register)
    register.assert_not_called();assert pool.daily_used()==0


def test_remote_error_breaks_instead_of_becoming_infinite_retry(monkeypatch):
    monkeypatch.setattr(pool.WebPoolClient,'snapshot',Mock(side_effect=pool.PoolError('HTTP 503')))
    register=Mock()
    with pytest.raises(pool.PoolError):run(register=register)
    register.assert_not_called();assert pool.daily_used()==0


def client():
    app=FastAPI();app.include_router(api.router);app.include_router(tasks.router)
    return TestClient(app)


def test_api_zero_config_requires_fee_confirmation_when_enabled(monkeypatch):
    monkeypatch.setattr('core.scheduler.scheduler.trigger_web_pool_now',Mock())
    assert client().put('/chatgpt-web-pool/config',json={'config':settings(enabled=True).model_dump()}).status_code==400
    response=client().put('/chatgpt-web-pool/config',json={'config':settings(enabled=True).model_dump(),'confirm_registration':True})
    assert response.status_code==200
    assert response.json()['config']['max_attempts_per_run']==0
    assert response.json()['config']['max_consecutive_failures']==0


def test_queued_unlimited_task_keeps_zero_total_and_explicit_metadata():
    pool.save_settings(settings())
    response=api.enqueue(trigger='manual',background_tasks=BackgroundTasks())
    state=tasks._get_task_snapshot(response['task_id'])
    assert state['total']==0 and state['progress']=='0/不限'
    assert state['meta']['attempt_limit_unlimited']
    assert state['meta']['settings']['max_attempts_per_run']==0


def test_ended_unlimited_task_is_not_reported_as_zero_attempts(monkeypatch):
    cfg=settings();pool.save_settings(cfg)
    def replenish(_settings,**kwargs):
        value=pool.PoolRunResult(attempts=7,registrations=7,success=7,snapshot=snapshot(100),reason='达到目标')
        kwargs['progress'](value);return value
    monkeypatch.setattr(pool,'replenish',replenish)
    bg=BackgroundTasks();task=api.enqueue(trigger='manual',background_tasks=bg);asyncio.run(bg())
    state=tasks._get_task_snapshot(task['task_id'])
    assert state['status']=='done' and state['registered']==7
    assert state['total']==0 and state['progress']=='7/不限'


def test_quota_unknown_has_snapshot_and_explanatory_outcome_not_failed(monkeypatch):
    cfg=settings();pool.save_settings(cfg)
    unknown={**snapshot(0),'remaining_quota':None,'quota_known':False,'unknown_quota_accounts':47}
    monkeypatch.setattr(pool.WebPoolClient,'snapshot',Mock(return_value=unknown))
    response=client().post('/chatgpt-web-pool/run',json={'confirm_registration':True})
    state=tasks._get_task_snapshot(response.json()['task_id'])
    assert state['status']=='done' and state['meta']['outcome']=='quota_unknown'
    assert state['meta']['snapshot']['unknown_quota_accounts']==47
    assert state['registered']==state['success']==state['error_count']==0
    assert state['errors']==[]


def test_webhook_unknown_quota_does_not_self_retry_or_count_failure(monkeypatch):
    cfg=settings(enabled=True);pool.save_settings(cfg);events.accept(cfg)
    monkeypatch.setattr(pool.WebPoolClient,'snapshot',Mock(return_value={**snapshot(0),'remaining_quota':None,'quota_known':False}))
    bg=BackgroundTasks();result=api.dispatch_pending_trigger(background_tasks=bg);asyncio.run(bg())
    assert result['triggered']
    state=events.status();assert not state['pending'] and not state['active_task_id']
    assert state['status']=='paused' and state['failure_runs']==0


def test_bounded_error_tail_preserves_actual_error_count_in_runtime_and_db(monkeypatch):
    cfg=settings();pool.save_settings(cfg)
    def replenish(_settings,**kwargs):
        value=pool.PoolRunResult(attempts=125,registrations=125,snapshot=snapshot(0),reason='停止')
        for i in range(125):value.add_error(str(i))
        kwargs['progress'](value)
        active=next(item for item in tasks._task_store.list_snapshots() if item['source']==pool.SOURCE)
        assert active['error_count']==125
        return value
    monkeypatch.setattr(pool,'replenish',replenish)
    bg=BackgroundTasks();task=api.enqueue(trigger='manual',background_tasks=bg);asyncio.run(bg())
    state=tasks._get_task_snapshot(task['task_id'])
    assert len(state['errors'])==100 and state['error_count']==125
    summary=tasks._list_persisted_tasks(source=pool.SOURCE)[0]
    assert summary['error_count']==125 and summary['errors']==[]
    tasks._task_store.remove(task['task_id'])
    assert tasks._get_task_snapshot(task['task_id'])['error_count']==125
