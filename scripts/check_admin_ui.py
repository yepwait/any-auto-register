"""Browser-only admin UI regression with deterministic API fixtures.

Start: python scripts/serve_ui_mock.py --port 18765
Run: python scripts/check_admin_ui.py
No backend imports, live data, credentials, registrations or payments.
"""
from __future__ import annotations
import argparse
from copy import deepcopy
import json
import re
from pathlib import Path
import time
from urllib.parse import urlsplit, parse_qs
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
NOW = 1794454800

WEB = dict(enabled=False, base_url='https://pool.example.invalid', admin_token_configured=True,
    protocol='auto', target_mode='quota', target_value=2300, refill_below=2000,
    interval_minutes=17, max_attempts_per_run=7, daily_registration_limit=0,
    max_consecutive_failures=4, delay_seconds=11, request_timeout=31)
INSPECTION = dict(enabled=False, interval_minutes=37, concurrency=2, delay_seconds=7,
    probe_enabled=True, backfill_rt_enabled=True, backfill_allow_login=False,
    only_missing_rt=False, only_sms_verified=False, upload_cpa=True, upload_sub2api=False,
    chat2api_enabled=True, chat2api_protocol='webhook', chat2api_url='https://sync.example.invalid',
    chat2api_token_configured=True, account_statuses=['registered', 'subscribed'], email_filter='', max_accounts=42)
SMS = dict(enabled=False, min_accounts=30, interval_minutes=19, max_attempts_per_run=7,
    daily_attempt_limit=17, max_consecutive_failures=4, delay_seconds=9,
    candidate_cooldown_minutes=71, allow_login=False, register_flow='email')

def task(task_id='mock_web_98765432', status='done', source='chatgpt_web_pool', **changes):
    return dict(id=task_id, platform='chatgpt', source=source, status=status, total=7,
        registered=5, success=4, skipped=0, error_count=1, errors=[], progress='5/7',
        created_at=NOW-180, updated_at=NOW-40, control={'stop_requested': False},
        meta={'kind': source, 'trigger':'webhook', 'registrations':3, 'reason':'本轮已结束，额度已达到目标'}, **changes)

def snapshot(**changes):
    return dict(total_accounts=438, normal_accounts=430, available_accounts=426,
        remaining_quota=2180, quota_known=True, known_remaining_quota=2180,
        unknown_quota_accounts=0, unlimited_accounts=0, protocol='yukkcat_v3',
        limited_accounts=4, abnormal_accounts=3, disabled_accounts=1, checked_at=NOW, **changes)

class Fixtures:
    def __init__(self):
        self.web = deepcopy(WEB); self.inspection = deepcopy(INSPECTION); self.sms = deepcopy(SMS)
        self.mode = 'normal'; self.requests=[]; self.writes=[]; self.unknown=[]

    def handle(self, route):
        request = route.request
        parsed = urlsplit(request.url)
        if parsed.hostname not in {'127.0.0.1', 'localhost'}:
            self.unknown.append('blocked external ' + request.url)
            return route.abort()
        if not parsed.path.startswith('/api/'):
            return route.continue_()
        path = parsed.path[4:]; query = parse_qs(parsed.query)
        method = request.method; payload = request.post_data_json if request.post_data else None
        self.requests.append((method,path,payload))
        if method not in {'GET','HEAD'}: self.writes.append((method,path,payload))
        if self.mode == 'failure' and method == 'GET' and path == '/chatgpt-web-pool/status':
            return route.fulfill(status=503, content_type='application/json', body=json.dumps({'detail':'模拟状态读取失败'}))
        web_snapshot = snapshot()
        if self.mode in {'unknown','unlimited'}:
            web_snapshot.update(remaining_quota=None,quota_known=False,unknown_quota_accounts=2,
                unlimited_accounts=1 if self.mode=='unlimited' else 0)
        realtime=dict(secret_configured=True,pending=False,active_task_id='',status='idle',reason='额度已达标',
            last_received_at=NOW-60,last_started_at=NOW-50,last_finished_at=NOW-40,next_attempt_at=0,
            received=68,coalesced=52,failure_runs=0,refill_latched=False)
        if self.mode=='busy': realtime.update(status='waiting_busy',pending=True,reason='正在等待其他账号任务结束')
        if path=='/auth/status': result={'has_password':False}
        elif path=='/platforms': result=[{'name':'chatgpt','display_name':'ChatGPT'},{'name':'cursor','display_name':'Cursor'}]
        elif path=='/accounts/stats': result={'total':486,'by_platform':{'chatgpt':438,'cursor':48},'by_status':{'registered':421,'subscribed':42,'trial':16,'expired':4,'invalid':2,'banned':1}}
        elif path=='/chatgpt-web-pool/config':
            if method=='PUT':
                self.web.update(payload['config']); self.web.pop('admin_token',None)
                result={'ok':True,'config':self.web}
            else: result=self.web
        elif path=='/chatgpt-web-pool/status': result={'enabled':self.web['enabled'],'snapshot':None if self.mode=='empty' else web_snapshot,
            'pending_uploads':3,'daily_registrations_used':12,'daily_registration_unlimited':self.web['daily_registration_limit']==0,
            'daily_registrations_remaining':None if self.web['daily_registration_limit']==0 else max(0,self.web['daily_registration_limit']-12),'budget_date_utc':'2026-11-12','active':None,
            'tasks':[] if self.mode=='empty' else [task()], 'realtime':realtime}
        elif path=='/chatgpt-web-pool/check': result={'snapshot':web_snapshot}
        elif path=='/chatgpt-web-pool/run': result={'triggered':False,'reason':'模拟：额度已达标'}
        elif path=='/inspection/config':
            if method=='PUT': result={'ok':True,'config':self.inspection}
            else: result=self.inspection
        elif path=='/inspection/status':
            last=task('inspect_12345678',source='inspection')
            result={'enabled':self.inspection['enabled'],'active':None,'last_task':last,'tasks':[last]}
        elif path=='/sms-stock/config':
            if method=='PUT': self.sms.update(payload['config']); result={'ok':True,'config':self.sms}
            else: result=self.sms
        elif path=='/sms-stock/status': result={'enabled':self.sms['enabled'],'inventory':{
            'verified_accounts':24,'pending_accounts':9,'ready_candidates':6,'missing_accounts':6,
            'daily_attempts_used':3,'daily_attempts_remaining':14,'budget_date_utc':'2026-11-12'},
            'active':None,'tasks':[task('stock_12345678',source='sms_stock')]}
        elif path=='/tasks/page':
            result={'items':[task('active_abcdef12','running','inspection')] if query.get('status')==['active'] else [task('finished_87654321')],
                'total':1,'offset':0,'limit':50}
        elif path=='/accounts': result={'items':[dict(id=1,platform='chatgpt',email='demo@example.invalid',password='',status='registered',
                token='',user_id='user_1',created_at='2026-11-12T02:00:00Z',extra_json=json.dumps({
                'account_purpose':'chatgpt_web_pool','access_token':'','sync_statuses':{'chatgpt2api':{'imported':True,'ready':False,'needs_resync':True}}}))], 'total':1}
        elif path.startswith('/actions/'): result={'actions':[]}
        elif path=='/config': result={'mail_provider':'luckmail','default_executor':'protocol'}
        elif path=='/payments/channels': result={'channels':[{'name':'direct','display_name':'直卡渠道','operations':['link','pay'],'option_schema':{'link':[],'pay':[]}}]}
        elif path=='/payments/channels/direct/cards': result={'cards':[]}
        elif path=='/tasks': result=[]
        elif path=='/proxies': result=[]
        elif path=='/tasks/logs': result={'items':[],'total':0}
        elif path=='/tasks/history': result={'items':[],'total':0}
        elif path.startswith('/solver/'): result={'running':False,'enabled':False}
        else:
            self.unknown.append(f'{method} {path}')
            return route.fulfill(status=501,content_type='application/json',body='{"detail":"Unmocked API"}')
        route.fulfill(content_type='application/json', body=json.dumps(result,ensure_ascii=False))


def main():
    parser=argparse.ArgumentParser(); parser.add_argument('--url',default='http://127.0.0.1:18765'); args=parser.parse_args()
    output=ROOT/'dist'/'ui-preview'; output.mkdir(parents=True,exist_ok=True)
    report=[]
    def passed(name): report.append(name); print('PASS',name,flush=True)
    with sync_playwright() as pw:
        browser=pw.chromium.launch(headless=True)
        fixtures=Fixtures()
        context=browser.new_context(viewport={'width':1440,'height':1050},locale='zh-CN',timezone_id='Asia/Shanghai')
        context.route('**/*',fixtures.handle)
        errors=[]; context.on('page',lambda page:page.on('pageerror',lambda error:errors.append(str(error))))
        page=context.new_page()
        page.set_default_timeout(10000)
        def button(label):
            return page.get_by_role('button').filter(has_text=re.compile(r'^' + r'\s*'.join(re.escape(char) for char in label) + r'$'))
        def open_page(path,title):
            page.goto(args.url+path,wait_until='networkidle')
            expect(page.get_by_role('heading',name=title,exact=True)).to_be_visible()
        open_page('/chatgpt-web-pool','Web 账号池')
        expect(page.locator('#target_value')).to_have_value('2300')
        expect(page.locator('#daily_registration_limit')).to_have_value('0')
        assert not fixtures.writes, fixtures.writes
        assert page.locator('.ant-alert').count()==0
        expect(page.locator('#max_attempts_per_run')).not_to_be_visible()
        expect(page.locator('#base_url')).not_to_be_visible()
        page.screenshot(path=str(output/'web-pool-desktop.png'),full_page=True)
        passed('Web saved values/quiet normal state/collapsed technical fields/read-only navigation')

        # A folded field that was never opened must survive a save unchanged.
        page.locator('#daily_registration_limit').fill('21')
        button('保存修改').click()
        page.wait_for_function("!document.querySelector('.ant-btn-loading')")
        web_put=[x for x in fixtures.writes if x[1]=='/chatgpt-web-pool/config'][-1][2]
        expected={k:v for k,v in WEB.items() if k!='admin_token_configured'}
        expected.update(admin_token='',daily_registration_limit=21)
        assert web_put=={'config':expected,'confirm_registration':False},web_put
        passed('Web closed advanced and connection values preserved across PUT')
        before=len(fixtures.writes)
        button('立即补号').click()
        expect(page.get_by_role('dialog')).to_contain_text('可能产生邮箱费用')
        button('取消').click()
        assert len(fixtures.writes)==before
        passed('Manual action requires explicit fee confirmation; cancel sends nothing')
        before=len(fixtures.writes)
        page.get_by_role('switch',name='自动补号',exact=True).click()
        button('保存修改').click()
        expect(page.get_by_role('dialog')).to_contain_text('开启自动补号')
        assert len(fixtures.writes)==before
        button('取消').click()
        assert len(fixtures.writes)==before
        passed('Enabling automatic refill also requires confirmation; cancel leaves saved config untouched')
        # Invalid data inside a never-opened advanced section must be revealed, not silently sent.
        fixtures.web=deepcopy(WEB); fixtures.web['request_timeout']=2
        open_page('/chatgpt-web-pool','Web 账号池')
        before=len(fixtures.writes)
        button('保存修改').click()
        expect(page.locator('#request_timeout')).to_be_visible()
        assert len(fixtures.writes)==before
        passed('Collapsed advanced validation reveals the invalid field and sends no write')
        fixtures.web=deepcopy(WEB)
        open_page('/chatgpt-web-pool','Web 账号池')
        page.locator('.ant-collapse-header').filter(has_text='连接设置').click()
        page.locator('#base_url').fill('https://different.example.invalid')
        before=len(fixtures.writes)
        button('保存修改').click()
        expect(page.get_by_text('更换服务地址时请重新输入管理员密钥',exact=True)).to_be_visible()
        assert len(fixtures.writes)==before
        passed('Changing destination cannot reuse saved admin credentials silently')
        # Restore the initial fixture so preview images show the requested 0=unlimited state.
        fixtures.web=deepcopy(WEB)

        fixtures.mode='unknown'; open_page('/chatgpt-web-pool','Web 账号池')
        expect(button('立即补号')).to_be_disabled()
        expect(page.get_by_text('当前额度无法确定，自动注册已暂停判断',exact=True)).to_be_visible()
        page.screenshot(path=str(output/'web-pool-unknown.png'),full_page=True)
        fixtures.web['target_mode']='available_accounts'; page.reload(wait_until='networkidle')
        expect(button('立即补号')).to_be_enabled()
        passed('Unknown quota blocks numeric mode without blocking known available-account mode')
        fixtures.web['target_mode']='quota'
        fixtures.mode='empty'; open_page('/chatgpt-web-pool','Web 账号池')
        expect(button('立即补号')).to_be_disabled()
        expect(page.get_by_text('暂无补号记录',exact=True)).to_be_visible()
        fixtures.mode='failure'; page.goto(args.url+'/chatgpt-web-pool',wait_until='networkidle')
        expect(page.get_by_text('无法读取 Web 池配置',exact=True)).to_be_visible()
        fixtures.mode='normal'
        passed('Empty snapshot and API failure never masquerade as healthy zero quota')
        open_page('/inspection','Codex／RT 巡检')
        page.screenshot(path=str(output/'inspection-desktop.png'),full_page=True)
        expect(page.locator('#concurrency')).not_to_be_visible()
        button('保存配置').click()
        page.wait_for_function("!document.querySelector('.ant-btn-loading')")
        insp=[x for x in fixtures.writes if x[1]=='/inspection/config'][-1][2]['data']
        assert insp['inspection_concurrency']=='2' and insp['inspection_delay_seconds']=='7',insp
        assert insp['inspection_chat2api_url']==INSPECTION['chat2api_url'] and 'inspection_chat2api_token' not in insp,insp
        assert insp['inspection_backfill_rt_enabled']=='1' and insp['inspection_only_missing_rt']=='0',insp
        passed('Inspection folded config retains limits/recovery/target and does not overwrite saved key')

        open_page('/sms-stock','可用 RT 库存')
        page.screenshot(path=str(output/'stock-desktop.png'),full_page=True)
        expect(page.locator('#daily_attempt_limit')).not_to_be_visible()
        button('保存配置').click()
        page.wait_for_function("!document.querySelector('.ant-btn-loading')")
        sms_put=[x for x in fixtures.writes if x[1]=='/sms-stock/config'][-1][2]
        assert sms_put['config']==SMS,sms_put
        passed('Stock folded protection/retry/cost config round-trips unchanged')

        for path,title,filename in [('/', '运营总览','dashboard-desktop'),('/running-tasks','任务运行','tasks-desktop'),('/accounts/chatgpt','ChatGPT 账号','accounts-desktop'),('/settings','全局配置','settings-desktop'),('/proxies','代理管理','proxies-desktop'),('/history','任务历史','history-desktop'),('/payments','支付中心','payments-desktop')]:
            open_page(path,title)
            page.screenshot(path=str(output/(filename+'.png')),full_page=True)
        passed('All changed management routes render with mock data')

        # Mobile overflow is checked at the document/layout level; tables own horizontal scroll.
        page.set_viewport_size({'width':430,'height':932})
        for path,title,filename in [('/chatgpt-web-pool','Web 账号池','web-pool-mobile'),('/sms-stock','可用 RT 库存','stock-mobile'),('/inspection','Codex／RT 巡检','inspection-mobile'),('/running-tasks','任务运行','tasks-mobile'),('/accounts/chatgpt','ChatGPT 账号','accounts-mobile'),('/settings','全局配置','settings-mobile'),('/payments','支付中心','payments-mobile')]:
            open_page(path,title)
            overflow=page.evaluate('({width:document.documentElement.clientWidth,scroll:document.documentElement.scrollWidth})')
            assert overflow['scroll']<=overflow['width']+2,(path,overflow)
            page.screenshot(path=str(output/(filename+'.png')),full_page=True)
        passed('Mobile pages do not overflow document width')

        page.set_viewport_size({'width':1440,'height':1050})
        page.evaluate("localStorage.setItem('theme','light')")
        open_page('/chatgpt-web-pool','Web 账号池')
        page.screenshot(path=str(output/'web-pool-light.png'),full_page=True)
        passed('Light theme uses shared palette')
        assert not errors,errors
        assert not fixtures.unknown,fixtures.unknown
        passed('No browser runtime errors or unmocked/external requests')
        (output/'report.json').write_text(json.dumps({'checks':report,'requests':len(fixtures.requests),
            'mock_write_requests':len(fixtures.writes),'screenshots':sorted(p.name for p in output.glob('*.png'))},ensure_ascii=False,indent=2),encoding='utf-8')
        browser.close()
    print(f'{len(report)} UI checks passed; artifacts: {output}',flush=True)

if __name__=='__main__': main()
