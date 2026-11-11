"""Resource regressions: all HTTP/process/browser/SDK actions are local mocks."""

import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest
import yaml

from services import solver_manager as manager
from services.turnstile_solver import api_solver, db_results

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def managed_solver(monkeypatch):
    monkeypatch.setattr(manager, '_proc', None)
    monkeypatch.setattr(manager, '_log_file', None)
    monkeypatch.setattr(manager, '_start_thread', None)
    monkeypatch.setattr(manager, '_start_thread_generation', None)
    monkeypatch.setattr(manager, '_lifecycle_generation', 0)
    monkeypatch.setattr(manager, '_owned_descendants', {})
    monkeypatch.setattr(manager, '_stop_requested', manager.threading.Event())
    monkeypatch.delenv('APP_ENABLE_SOLVER', raising=False)
    monkeypatch.delenv('SOLVER_THREADS', raising=False)
    with mock.patch.object(manager, 'is_running', return_value=False) as probe, mock.patch.object(
        manager.subprocess, 'Popen'
    ) as spawn, mock.patch.object(manager, '_remember_owned_descendants'), mock.patch.object(
        manager, '_signal_owned_descendants'
    ), mock.patch.object(manager, '_signal_owned_process') as send_signal:
        yield SimpleNamespace(probe=probe, spawn=spawn, send_signal=send_signal)
    # Never let a mocked child reach the real atexit handler.
    manager._proc = None
    manager._log_file = None


def test_solver_opt_in_defaults_never_probe_or_spawn(managed_solver):
    manager.start()
    manager.stop()
    assert manager._solver_threads() == 1
    managed_solver.probe.assert_not_called()
    managed_solver.spawn.assert_not_called()
    managed_solver.send_signal.assert_not_called()


@pytest.mark.parametrize('value, expected', [('bad', 1), ('0', 1), ('-3', 1), ('2', 2), ('999', 16)])
def test_solver_thread_setting_is_bounded(monkeypatch, value, expected):
    monkeypatch.setenv('SOLVER_THREADS', value)
    assert manager._solver_threads() == expected


def test_solver_does_not_take_over_or_stop_external_service(managed_solver, monkeypatch):
    monkeypatch.setenv('APP_ENABLE_SOLVER', '1')
    managed_solver.probe.return_value = True
    manager.start()
    manager.stop()
    managed_solver.spawn.assert_not_called()
    managed_solver.send_signal.assert_not_called()


def test_solver_failed_spawn_closes_log(managed_solver, monkeypatch):
    monkeypatch.setenv('APP_ENABLE_SOLVER', '1')
    log = mock.Mock()
    managed_solver.spawn.side_effect = OSError('offline spawn failure')
    with mock.patch('builtins.open', return_value=log):
        manager.start()
    log.close.assert_called_once()
    assert manager._proc is None and manager._log_file is None


def test_solver_start_timeout_reaps_only_owned_child(managed_solver, monkeypatch):
    monkeypatch.setenv('APP_ENABLE_SOLVER', 'yes')
    proc = mock.Mock(pid=12345, returncode=None)
    proc.poll.return_value = None
    managed_solver.spawn.return_value = proc
    log = mock.Mock()
    with mock.patch('builtins.open', return_value=log), mock.patch.object(
        manager.time, 'monotonic', side_effect=[0.0, 99.0]
    ), mock.patch.object(manager._stop_requested, 'wait', return_value=False):
        manager.start()
    assert manager._proc is None and manager._log_file is None
    log.close.assert_called_once()
    proc.wait.assert_called_with(timeout=5)
    args, kwargs = managed_solver.spawn.call_args
    assert args[0][args[0].index('--thread') + 1] == '1'
    if os.name == 'nt':
        assert kwargs['creationflags'] == subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        assert kwargs['start_new_session'] is True
    assert managed_solver.send_signal.call_args_list == [
        mock.call(proc, force=False), mock.call(proc, force=True),
    ]


def test_solver_stop_escalates_timeout_and_closes_log(managed_solver):
    proc = mock.Mock(pid=12345)
    proc.poll.return_value = None
    proc.wait.side_effect = [subprocess.TimeoutExpired('mock-solver', 5), 0]
    log = mock.Mock()
    manager._proc, manager._log_file = proc, log
    manager.stop()
    assert managed_solver.send_signal.call_args_list == [
        mock.call(proc, force=False), mock.call(proc, force=True), mock.call(proc, force=True),
    ]
    log.close.assert_called_once()
    assert manager._proc is None and manager._stop_requested.is_set()


def test_solver_cancelled_start_generation_cannot_spawn(managed_solver, monkeypatch):
    monkeypatch.setenv('APP_ENABLE_SOLVER', '1')
    manager.stop()
    manager.start(_generation=0)
    managed_solver.probe.assert_not_called()
    managed_solver.spawn.assert_not_called()


def test_solver_signals_only_its_process_group_or_tree():
    proc = mock.Mock(pid=7654)
    proc.poll.return_value = None
    with mock.patch.object(manager.os, 'name', 'posix'), mock.patch.object(
        manager.os, 'killpg', create=True
    ) as killpg, mock.patch.object(manager.signal, 'SIGKILL', 9, create=True):
        manager._signal_owned_process(proc, force=True)
        killpg.assert_called_once_with(7654, signal.SIGKILL)
    with mock.patch.object(manager.os, 'name', 'nt'), mock.patch.object(
        manager.subprocess, 'run', return_value=SimpleNamespace(returncode=0)
    ) as run:
        manager._signal_owned_process(proc, force=True)
        assert run.call_args.args[0] == ['taskkill', '/PID', '7654', '/T', '/F']
    proc.kill.assert_not_called()
    proc.terminate.assert_not_called()


def test_reaped_parent_pid_is_not_signalled_without_owned_survivors(monkeypatch):
    proc = mock.Mock(pid=7654)
    proc.poll.return_value = 0
    monkeypatch.setattr(manager, '_owned_descendants', {})
    with mock.patch.object(manager.os, 'name', 'posix'), mock.patch.object(
        manager.os, 'killpg', create=True
    ) as killpg:
        manager._signal_owned_process(proc, force=True)
        killpg.assert_not_called()
    with mock.patch.object(manager.os, 'name', 'nt'), mock.patch.object(manager.subprocess, 'run') as run:
        manager._signal_owned_process(proc, force=True)
        run.assert_not_called()


def test_linux_descendant_snapshot_only_follows_owned_tree(monkeypatch):
    monkeypatch.setattr(manager.sys, 'platform', 'linux')
    monkeypatch.setattr(manager, '_owned_descendants', {})
    proc = mock.Mock(pid=100)
    proc.poll.return_value = None
    identities = {200: (100, 'start-driver', 100), 300: (200, 'start-browser', 300),
                  999: (9999, 'unrelated', 999)}
    def children(path, **_kwargs):
        parent = int(path.split('/')[2])
        return mock.mock_open(read_data={100: '200 999', 200: '300', 300: ''}[parent])()
    with mock.patch('builtins.open', side_effect=children), mock.patch.object(
        manager, '_linux_process_identity', side_effect=identities.get
    ):
        manager._remember_owned_descendants(proc)
    assert manager._owned_descendants == {200: ('start-driver', 100), 300: ('start-browser', 300)}


def test_linux_descendant_kill_checks_pid_identity_and_detached_group(monkeypatch):
    monkeypatch.setattr(manager, '_owned_descendants', {200: ('driver', 100), 300: ('browser', 300),
                                                       400: ('old-pid', 400)})
    identities = {200: (100, 'driver', 100), 300: (200, 'browser', 300),
                  400: (999, 'reused-pid', 400)}
    with mock.patch.object(manager, '_linux_process_identity', side_effect=identities.get), mock.patch.object(
        manager.os, 'killpg', create=True
    ) as killpg, mock.patch.object(manager.os, 'kill') as kill, mock.patch.object(
        manager.signal, 'SIGKILL', 9, create=True
    ):
        manager._signal_owned_descendants(force=True)
        killpg.assert_called_once_with(300, 9)
        kill.assert_called_once_with(200, 9)


@pytest.fixture(autouse=True)
def clean_results(monkeypatch):
    db_results.results_db.clear()
    monkeypatch.setenv('SOLVER_RESULT_TTL_SECONDS', '60')
    monkeypatch.setenv('SOLVER_RESULT_MAX_ENTRIES', '1000')
    yield
    db_results.results_db.clear()


@pytest.mark.parametrize('value', ['mock-token', 'CAPTCHA_FAIL'])
def test_terminal_results_keep_original_creation_and_expire(monkeypatch, value):
    clock = [1000.0]
    monkeypatch.setattr(db_results, 'time', SimpleNamespace(time=lambda: clock[0]))

    async def scenario():
        await db_results.save_result('id', 'turnstile', {'status': 'CAPTCHA_NOT_READY'})
        clock[0] = 1005.0
        await db_results.save_result('id', 'turnstile', {'value': value})
        result = await db_results.load_result('id')
        assert result == {'value': value, 'createTime': 1000.0}
        clock[0] = 1061.0
        assert await db_results.load_result('id') is None
        assert not db_results.results_db

    asyncio.run(scenario())


def test_legacy_missing_timestamp_is_normalized_then_cleaned(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(db_results, 'time', SimpleNamespace(time=lambda: clock[0]))
    db_results.results_db.update({'dict': {'value': 'token'}, 'string': 'CAPTCHA_NOT_READY'})

    async def scenario():
        assert await db_results.cleanup_old_results() == 0
        assert all(item['createTime'] == 1000 for item in db_results.results_db.values())
        clock[0] = 1061
        assert await db_results.cleanup_old_results() == 2
        assert not db_results.results_db

    asyncio.run(scenario())


def test_result_cache_has_an_entry_cap(monkeypatch):
    monkeypatch.setenv('SOLVER_RESULT_MAX_ENTRIES', '2')

    async def scenario():
        for index in range(5):
            await db_results.save_result(str(index), 'turnstile', {'value': 'mock-token'})
        assert len(db_results.results_db) == 2
        assert set(db_results.results_db) == {'3', '4'}

    asyncio.run(scenario())


def _server(**kwargs):
    return api_solver.TurnstileAPIServer(
        headless=True, useragent='offline-unit-test', debug=False,
        browser_type='camoufox', thread=kwargs.pop('thread', 1), proxy_support=False, **kwargs,
    )


def _browser(context=None):
    return SimpleNamespace(
        is_connected=mock.Mock(return_value=True),
        new_context=mock.AsyncMock(return_value=context), close=mock.AsyncMock(),
    )


def _lease(server, browser):
    server.browser_pool.put_nowait((1, browser, {'useragent': 'offline', 'sec_ch_ua': ''}))


def test_solver_cli_defaults_to_one_browser(monkeypatch):
    monkeypatch.setattr(sys, 'argv', ['mock-solver'])
    monkeypatch.delenv('SOLVER_THREADS', raising=False)
    assert api_solver.parse_args().thread == 1
    monkeypatch.setenv('SOLVER_THREADS', '2')
    assert api_solver.parse_args().thread == 2


def test_solver_queue_is_bounded_and_stopped_intake_is_rejected(monkeypatch):
    monkeypatch.setenv('SOLVER_QUEUE_LIMIT', '2')

    async def scenario():
        server = _server()
        blocked = asyncio.Event()
        server._solve_turnstile = mock.AsyncMock(side_effect=lambda **_kw: blocked.wait())
        # AsyncMock's callable return is not recursively awaited; use an async body.
        async def solve(**_kwargs):
            await blocked.wait()
        server._solve_turnstile.side_effect = solve
        for _ in range(3):
            async with server.app.test_request_context('/turnstile?url=https://offline.invalid&sitekey=mock'):
                response, status = await server.process_turnstile()
                assert status == 200 and (await response.get_json())['taskId']
        assert server._pending_tasks == 3 and len(server._solve_tasks) == 3
        async with server.app.test_request_context('/turnstile?url=https://offline.invalid&sitekey=mock'):
            response, status = await server.process_turnstile()
            assert status == 429 and (await response.get_json())['errorCode'] == 'ERROR_QUEUE_FULL'
        await server._shutdown()
        assert not server._solve_tasks and server._pending_tasks == 0
        async with server.app.test_request_context('/turnstile?url=https://offline.invalid&sitekey=mock'):
            _, status = await server.process_turnstile()
            assert status == 503

    asyncio.run(scenario())


def test_pool_wait_has_a_timeout():
    async def scenario():
        server = _server()
        server.queue_timeout = 0.01
        await server._solve_turnstile('queued', 'offline', 'mock')
        assert (await db_results.load_result('queued'))['errorCode'] == 'ERROR_QUEUE_TIMEOUT'
        assert server.browser_pool.empty()

    asyncio.run(scenario())


def test_context_creation_failure_retires_browser_without_leaking_pool_lease():
    async def scenario():
        server = _server()
        browser = _browser()
        browser.new_context.side_effect = RuntimeError('mock context failure')
        _lease(server, browser)
        await server._solve_turnstile('context-fail', 'offline', 'mock')
        browser.close.assert_awaited_once()
        assert server.browser_pool.empty()
        assert (await db_results.load_result('context-fail'))['value'] == 'CAPTCHA_FAIL'
        await asyncio.wait_for(server.browser_pool.join(), timeout=0.1)

    asyncio.run(scenario())


def test_page_setup_failure_always_closes_context_and_can_reuse_browser():
    async def scenario():
        server = _server()
        contexts = [SimpleNamespace(new_page=mock.AsyncMock(side_effect=RuntimeError('mock page failure')),
                                    close=mock.AsyncMock()) for _ in range(2)]
        browser = _browser()
        browser.new_context.side_effect = contexts
        _lease(server, browser)
        for index in range(2):
            await server._solve_turnstile(f'page-{index}', 'offline', 'mock')
            assert server.browser_pool.qsize() == 1
        for context in contexts:
            context.close.assert_awaited_once()
        assert browser.new_context.await_count == 2  # Per-request/account isolation.
        browser.close.assert_not_awaited()

    asyncio.run(scenario())


def test_solve_timeout_includes_page_setup_and_preserves_creation():
    async def scenario():
        server = _server()
        server.solve_timeout = 0.01
        never = asyncio.Event()
        async def new_page():
            await never.wait()
        context = SimpleNamespace(new_page=mock.AsyncMock(side_effect=new_page), close=mock.AsyncMock())
        browser = _browser(context)
        _lease(server, browser)
        await db_results.save_result('timeout', 'turnstile', {'status': 'CAPTCHA_NOT_READY'})
        created = db_results.results_db['timeout']['createTime']
        await server._solve_turnstile('timeout', 'offline', 'mock')
        result = await db_results.load_result('timeout')
        assert result['errorCode'] == 'ERROR_TASK_TIMEOUT' and result['createTime'] == created
        context.close.assert_awaited_once()
        assert server.browser_pool.qsize() == 1

    asyncio.run(scenario())


def test_solve_deadline_cannot_interrupt_final_context_cleanup():
    async def scenario():
        server = _server()
        server.solve_timeout = 0.01
        server.close_timeout = 1
        closed = asyncio.Event()
        async def close():
            asyncio.get_running_loop().call_later(0.03, closed.set)
            await closed.wait()
        context = SimpleNamespace(new_page=mock.AsyncMock(side_effect=RuntimeError('mock setup error')),
                                  close=mock.AsyncMock(side_effect=close))
        browser = _browser(context)
        _lease(server, browser)
        await server._solve_turnstile('cleanup-after-deadline', 'offline', 'mock')
        assert closed.is_set() and server.browser_pool.qsize() == 1
        browser.close.assert_not_awaited()

    asyncio.run(scenario())


def test_cancel_during_context_creation_retires_uncertain_browser():
    async def scenario():
        server = _server()
        entered, never = asyncio.Event(), asyncio.Event()
        async def new_context(**_kwargs):
            entered.set()
            await never.wait()
        browser = _browser()
        browser.new_context.side_effect = new_context
        _lease(server, browser)
        task = asyncio.create_task(server._solve_turnstile('cancel', 'offline', 'mock'))
        await asyncio.wait_for(entered.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        browser.close.assert_awaited_once()
        assert server.browser_pool.empty()
        assert (await db_results.load_result('cancel'))['errorCode'] == 'ERROR_SOLVER_STOPPED'

    asyncio.run(scenario())


def test_stuck_context_close_is_bounded_and_browser_is_not_reused():
    async def scenario():
        server = _server()
        server.close_timeout = 0.01
        never = asyncio.Event()
        async def close():
            await never.wait()
        context = SimpleNamespace(new_page=mock.AsyncMock(side_effect=RuntimeError('offline')),
                                  close=mock.AsyncMock(side_effect=close))
        browser = _browser(context)
        _lease(server, browser)
        await asyncio.wait_for(server._solve_turnstile('close-timeout', 'offline', 'mock'), timeout=1)
        browser.close.assert_awaited_once()
        assert server.browser_pool.empty()

    asyncio.run(scenario())


def test_shutdown_cancels_cleanup_and_tasks_and_closes_drivers():
    async def scenario():
        server = _server()
        browser = _browser()
        driver = SimpleNamespace(stop=mock.AsyncMock())
        server._browsers.append(browser)
        server._playwright = driver
        _lease(server, browser)
        server._cleanup_task = asyncio.create_task(server._periodic_cleanup())
        cleanup = server._cleanup_task
        never = asyncio.Event()
        task = asyncio.create_task(never.wait())
        server._solve_tasks.add(task)
        server._pending_tasks = 1
        task.add_done_callback(server._solve_task_done)
        await server._shutdown()
        assert task.cancelled() and cleanup.cancelled()
        browser.close.assert_awaited_once()
        driver.stop.assert_awaited_once()
        assert not server._solve_tasks and server.browser_pool.empty()
        await server._shutdown()  # Idempotent.
        browser.close.assert_awaited_once()

    asyncio.run(scenario())


def test_partial_startup_failure_closes_existing_resources():
    async def scenario():
        server = _server()
        browser = _browser()
        server._browsers.append(browser)
        server.display_welcome = mock.Mock()
        server._initialize_browser = mock.AsyncMock(side_effect=RuntimeError('mock init failure'))
        with pytest.raises(RuntimeError, match='mock init failure'):
            await server._startup()
        browser.close.assert_awaited_once()
        assert server._stopping and server._cleanup_task is None

    asyncio.run(scenario())


def test_camoufox_threads_own_distinct_managers_and_close_them():
    async def scenario():
        server = _server(thread=2)
        browsers = [_browser(), _browser()]
        managers = [SimpleNamespace(start=mock.AsyncMock(return_value=browser),
                                    __aexit__=mock.AsyncMock()) for browser in browsers]
        with mock.patch.object(api_solver, 'AsyncCamoufox', side_effect=managers) as factory, mock.patch.object(
            api_solver, '_preinstalled_camoufox_options', return_value={}
        ):
            await server._initialize_browser()
        assert factory.call_count == 2 and server.browser_pool.qsize() == 2
        await server._shutdown()
        for browser, owner in zip(browsers, managers):
            browser.close.assert_awaited_once()
            owner.__aexit__.assert_awaited_once_with(None, None, None)

    asyncio.run(scenario())


@pytest.mark.parametrize('name', ['docker-compose.yml', 'docker-compose.server.yml'])
def test_compose_preserves_resource_caps_key_volume_and_opt_in_solver(name):
    config = yaml.safe_load((ROOT / name).read_text(encoding='utf-8'))['services']['app']
    assert config['init'] is True and config['restart'] == 'unless-stopped'
    assert config['mem_limit'] == '${APP_MEM_LIMIT:-2g}'
    assert config['memswap_limit'] == '${APP_MEM_SWAP_LIMIT:-3g}'
    assert config['cpus'] == '${APP_CPU_LIMIT:-2.0}'
    assert config['logging']['options'] == {
        'max-size': '${APP_LOG_MAX_SIZE:-10m}', 'max-file': '${APP_LOG_MAX_FILES:-3}',
    }
    assert config['environment']['APP_ENABLE_SOLVER'] == '${APP_ENABLE_SOLVER:-0}'
    assert config['environment']['SOLVER_THREADS'] == '${SOLVER_THREADS:-1}'
    assert config['environment']['CREDENTIAL_ENCRYPTION_KEY_FILE'] == '/runtime/.secrets/credential_key'
    assert '${APP_RUNTIME_BIND:-./data}:/runtime' in config['volumes']
    if name == 'docker-compose.yml':
        assert config['ports'] == ['8000:8000', '127.0.0.1:8889:8889', '${CLIPROXYAPI_PORT_BIND:-8317}:8317']
        assert len(config['volumes']) == 3 and config['shm_size'] == '1gb'
    else:
        assert config['ports'] == ['${APP_PORT_BIND:-127.0.0.1:8000}:8000']


@pytest.mark.parametrize('name', ['Dockerfile', 'Dockerfile.server'])
def test_images_disable_solver_by_default_and_keep_node_runtime(name):
    text = (ROOT / name).read_text(encoding='utf-8')
    assert 'APP_ENABLE_SOLVER=0' in text and 'SOLVER_THREADS=1' in text
    assert 'node' in text
    if name.endswith('.server'):
        assert '/usr/local/bin/node /usr/local/bin/node' in text
        assert 'node --version' in text
        assert 'ENTRYPOINT ["/app/docker/entrypoint.server.sh"]' in text
        entrypoint = (ROOT / 'docker/entrypoint.server.sh').read_text(encoding='utf-8')
        assert 'node --version' in entrypoint and 'exit 1' in entrypoint


@pytest.mark.skipif(shutil.which('node') is None, reason='local Node is unavailable')
def test_sentinel_deadline_helper_clears_success_failure_and_timeout(tmp_path):
    source = (ROOT / 'platforms/chatgpt/protocol/openai_sentinel_quickjs.js').read_text(encoding='utf-8')
    helper = source[source.index('async function withTimeout('):source.index('// ─── Main')]
    harness = tmp_path / 'mock-timers.js'
    harness.write_text('''
const assert = require('node:assert/strict');
const timers = new Set();
let lastTimer;
function setTimeout(callback) { lastTimer = { callback }; timers.add(lastTimer); return lastTimer; }
function clearTimeout(timer) { timers.delete(timer); }
''' + helper + '''
(async () => {
  assert.equal(await withTimeout(Promise.resolve('ok'), 8000, 'timeout'), 'ok');
  assert.equal(timers.size, 0);
  await assert.rejects(withTimeout(Promise.reject(new Error('mock fail')), 8000, 'timeout'), /mock fail/);
  assert.equal(timers.size, 0);
  const pending = withTimeout(new Promise(() => {}), 8000, 'mock deadline');
  lastTimer.callback();
  await assert.rejects(pending, /mock deadline/);
  assert.equal(timers.size, 0);
})().catch(error => { console.error(error); process.exitCode = 1; });
''', encoding='utf-8')
    subprocess.run(['node', str(harness)], check=True, capture_output=True, text=True, timeout=3)
    assert 'withTimeout(context.SentinelSDK.init(flow)' in source
    assert 'withTimeout(context.SentinelSDK.token(flow)' in source
    assert 'withTimeout(context.SentinelSDK.sessionObserverToken(flow)' in source


@pytest.mark.skipif(shutil.which('node') is None, reason='local Node is unavailable')
def test_mock_sentinel_sdk_exits_after_output_and_keeps_accounts_isolated(tmp_path):
    source = (ROOT / 'platforms/chatgpt/protocol/openai_sentinel_quickjs.js').read_text(encoding='utf-8')
    mock_runner = tmp_path / 'mock-sentinel-runner.js'
    # Do not replay real SDK behavior or wait for pseudo-human events in a unit test.
    behavior_start = source.index('async function dispatchBehavior(')
    behavior_end = source.index('// Clear the losing deadline', behavior_start)
    mock_runner.write_text(source[:behavior_start] + 'async function dispatchBehavior() {}\n' + source[behavior_end:], encoding='utf-8')
    sdk = tmp_path / 'mock-sdk.js'
    sdk.write_text('''
var SentinelSDK = {
  init: async () => null,
  token: async () => 'mock-main-token',
  sessionObserverToken: async () => 'mock-observer-token'
};
globalThis.__debugP = { getRequirementsToken: async () => {
  const previous = localStorage.getItem('account');
  localStorage.setItem('account', document.cookie);
  return JSON.stringify({previous, cookie: document.cookie});
} };
'''.replace('var SentinelSDK =', 'var SentinelSDK='), encoding='utf-8')
    environment = dict(os.environ, OPENAI_SENTINEL_SDK_FILE=str(sdk))
    for account in ('mock-account-a', 'mock-account-b'):
        # An uncleared 8-second Promise.race timer would exceed this offline deadline.
        result = subprocess.run(
            ['node', str(ROOT / 'platforms/chatgpt/protocol/openai_sentinel_quickjs.js')],
            input=json.dumps({'action': 'requirements', 'device_id': account}),
            capture_output=True, text=True, env=environment, check=True, timeout=3,
        )
        proof = json.loads(json.loads(result.stdout)['request_p'])
        assert proof == {'previous': None, 'cookie': 'oai-' + 'did=' + account}
    solved = subprocess.run(
        ['node', str(mock_runner)], input=json.dumps({'action': 'solve'}),
        capture_output=True, text=True, env=environment, check=True, timeout=3,
    )
    assert json.loads(solved.stdout) == {'token': 'mock-' + 'main-token', 'so_token': 'mock-' + 'observer-token'}
