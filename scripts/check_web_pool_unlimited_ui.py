"""Offline browser regression for 0=unlimited and saved-vs-task policy UI."""
from __future__ import annotations
from copy import deepcopy
import argparse,json,re
from urllib.parse import urlsplit
from playwright.sync_api import sync_playwright,expect
from check_admin_ui import Fixtures,ROOT,WEB,NOW,task

class UnlimitedFixtures(Fixtures):
    def __init__(self):
        super().__init__()
        self.web.update(enabled=True,max_attempts_per_run=5,max_consecutive_failures=3)
        self.detail_limit=5
        self.blocked=False
        self.detail_outcome=None
        self.detail_registered=0
        self.event_stream=False

    def handle(self,route):
        path=urlsplit(route.request.url).path
        if path=='/api/chatgpt-web-pool/status':
            # Reuse the complete base fixture then add task policy/outcome.
            class Capture:
                request=route.request
                def fulfill(inner,**kwargs):
                    data=json.loads(kwargs['body'])
                    row=data['tasks'][0]
                    row['meta'].update(settings={'max_attempts_per_run':self.detail_limit,'max_consecutive_failures':3,'daily_registration_limit':0})
                    row['status']='done' if self.blocked else 'failed'
                    if self.blocked:
                        row['registered']=0;row['success']=0;row['error_count']=0
                        row['meta'].update(outcome='quota_unknown',reason='额度未知，等待远端确认')
                    return route.fulfill(**{**kwargs,'body':json.dumps(data,ensure_ascii=False)})
            return super().handle(Capture())
        if path=='/api/tasks/mock_web_98765432':
            self.requests.append(('GET',path[4:],None))
            row=task()
            row.update(logs=['[04:28:58] 历史任务按当时配置执行'],log_count=1,log_offset=0,next_since=1,registered=self.detail_registered)
            row['total']=self.detail_limit
            row['meta'].update(settings={'max_attempts_per_run':self.detail_limit,'max_consecutive_failures':0 if self.detail_limit==0 else 3,'daily_registration_limit':0},
                               attempt_limit_unlimited=self.detail_limit==0)
            if self.detail_outcome:row['meta']['outcome']=self.detail_outcome
            if self.event_stream:row['status']='running'
            return route.fulfill(content_type='application/json',body=json.dumps(row,ensure_ascii=False))
        if path=='/api/tasks/mock_web_98765432/logs/stream':
            self.requests.append(('GET',path[4:],None))
            return route.fulfill(content_type='text/event-stream',body='data: '+json.dumps({'done':True,'status':'done','registered':7,'success':3,'total':0,'log_count':1})+'\n\n')
        return super().handle(route)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--url',default='http://127.0.0.1:18765');args=parser.parse_args()
    output=ROOT/'dist'/'web-unlimited-ui';output.mkdir(parents=True,exist_ok=True)
    checks=[]
    def passed(label):checks.append(label);print('PASS',label,flush=True)
    with sync_playwright() as pw:
        browser=pw.chromium.launch(headless=True)
        fixtures=UnlimitedFixtures()
        context=browser.new_context(viewport={'width':1440,'height':1080},locale='zh-CN',timezone_id='Asia/Shanghai')
        context.route('**/*',fixtures.handle)
        page=context.new_page();page.set_default_timeout(10000)
        errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
        def button(label):return page.get_by_role('button').filter(has_text=re.compile('^'+r'\s*'.join(re.escape(c) for c in label)+'$'))
        def open_page():
            page.goto(args.url+'/chatgpt-web-pool',wait_until='networkidle')
            expect(page.get_by_role('heading',name='Web 账号池',exact=True)).to_be_visible()
        def open_log():
            page.get_by_role('button',name='查看日志',exact=True).first.click()
            expect(page.get_by_role('dialog')).to_be_visible()
        open_page()
        page.locator('.ant-collapse-header').filter(has_text='高级设置').click()
        expect(page.locator('#max_attempts_per_run')).to_have_value('5')
        expect(page.locator('#max_consecutive_failures')).to_have_value('3')
        page.locator('#max_attempts_per_run').fill('0')
        page.locator('#max_consecutive_failures').fill('0')
        expect(page.get_by_text('有未保存修改：尚未用于后台任务',exact=True)).to_be_visible()
        assert fixtures.web['max_attempts_per_run']==5 and fixtures.web['max_consecutive_failures']==3
        saved_rows=page.locator('.admin-state-row')
        expect(saved_rows.filter(has_text='已保存每轮上限')).to_contain_text('5 次')
        expect(saved_rows.filter(has_text='已保存连续失败上限')).to_contain_text('3 次')
        assert not fixtures.writes
        passed('Zero accepted in both controls without silently changing saved policy')
        page.mouse.move(0,0);page.screenshot(path=str(output/'editing-zero-saved-policy.png'),full_page=True)

        button('保存修改').click()
        dialog=page.get_by_role('dialog')
        expect(dialog).to_contain_text('每轮与连续失败均不限')
        expect(dialog).to_contain_text('可能持续注册并重复产生邮箱费用')
        assert not fixtures.writes
        button('取消').click()
        assert not fixtures.writes
        passed('All-unlimited fee confirmation is explicit and cancel does not write')
        button('保存修改').click();button('确认并保存').click()
        expect(saved_rows.filter(has_text='已保存每轮上限')).to_contain_text('不限')
        writes=[entry for entry in fixtures.writes if entry[1]=='/chatgpt-web-pool/config']
        assert len(writes)==1 and writes[0][2]['confirm_registration'] is True
        values=writes[0][2]['config']
        assert values['max_attempts_per_run']==0 and values['max_consecutive_failures']==0 and values['daily_registration_limit']==0
        assert values['admin_token']=='' and values['target_value']==2300
        passed('Confirmed PUT round-trips 0/0/0 and preserves connection/goal fields')
        open_log()
        expect(page.get_by_role('dialog')).to_contain_text('本任务尝试上限：5')
        expect(page.get_by_role('dialog')).to_contain_text('配置按创建时生效，不随当前表单变化')
        expect(page.get_by_role('dialog')).to_contain_text('本任务每轮 5，连续失败 3，每日 不限')
        page.screenshot(path=str(output/'historical-task-policy.png'))
        passed('Historical log shows task-time 5/3, not newly saved unlimited policy')
        page.locator('.ant-drawer-close').click()

        fixtures.detail_limit=0;fixtures.detail_registered=7
        open_log()
        expect(page.get_by_role('dialog')).to_contain_text('本任务尝试上限：不限')
        expect(page.get_by_role('dialog')).to_contain_text('已尝试：7')
        expect(page.get_by_role('dialog')).not_to_contain_text('本任务尝试上限：7')
        passed('Unlimited task log never turns growing attempt count into a fake finite cap')
        page.locator('.ant-drawer-close').click()
        fixtures.event_stream=True
        open_log()
        expect(page.get_by_role('dialog')).to_contain_text('本任务尝试上限：不限')
        expect(page.get_by_role('dialog')).to_contain_text('已尝试：7')
        passed('SSE completion preserves unlimited limit and actual attempt count')
        page.locator('.ant-drawer-close').click()
        fixtures.event_stream=False
        fixtures.mode='unknown';fixtures.blocked=True;fixtures.detail_outcome='quota_unknown';fixtures.detail_registered=0
        open_page()
        expect(page.get_by_text('额度未知，待确认',exact=True)).to_be_visible()
        expect(button('立即补号')).to_be_disabled()
        expect(page.get_by_text('未执行：额度未知',exact=True).first).to_be_visible()
        expect(page.get_by_text('当前额度无法确定，自动注册已暂停判断',exact=True)).to_be_visible()
        page.screenshot(path=str(output/'quota-blocked-not-failed.png'),full_page=True)
        open_log()
        expect(page.get_by_role('dialog')).to_contain_text('额度未知，本任务未继续注册')
        expect(page.get_by_role('dialog')).not_to_contain_text('任务失败')
        passed('Unknown quota has a separate blocked outcome, not a registration failure')
        page.locator('.ant-drawer-close').click()
        page.set_viewport_size({'width':430,'height':932})
        open_page()
        overflow=page.evaluate('document.documentElement.scrollWidth>document.documentElement.clientWidth+2')
        assert not overflow
        page.screenshot(path=str(output/'quota-blocked-mobile.png'),full_page=True)
        passed('Mobile status and zero controls remain accessible without document overflow')
        assert not errors,errors
        assert not fixtures.unknown,fixtures.unknown
        assert not any(path.endswith('/run') for _,path,_ in fixtures.writes)
        (output/'report.json').write_text(json.dumps({'checks':checks,'mock_write_requests':len(fixtures.writes),'browser_errors':errors},ensure_ascii=False,indent=2),encoding='utf-8')
        browser.close()
    print(str(len(checks))+' unlimited-policy UI checks passed',flush=True)
if __name__=='__main__':main()
