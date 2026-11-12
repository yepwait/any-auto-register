"""Regression for viewport-bound list scrolling; all API traffic is mocked.
Run after frontend build with scripts/serve_ui_mock.py on 127.0.0.1:18765.
"""
from __future__ import annotations
import argparse
import json
import re
from pathlib import Path
from urllib.parse import urlsplit, parse_qs
from playwright.sync_api import sync_playwright, expect
from check_admin_ui import Fixtures, ROOT

class ListFixtures(Fixtures):
    def handle(self, route):
        request=route.request;url=urlsplit(request.url);query=parse_qs(url.query)
        path=url.path
        if url.hostname in {'localhost','127.0.0.1'} and request.method=='GET' and path in {'/api/accounts','/api/tasks/logs','/api/proxies'}:
            self.requests.append(('GET',path[4:],dict(query)))
            page=int(query.get('page',['1'])[0]);size=int(query.get('page_size',['50'])[0]);start=(page-1)*size
            if path=='/api/accounts':
                rows=[dict(id=n+1,platform='chatgpt',email=f'account-{n+1:03d}@example.invalid',password='',status='registered',token='',
                    user_id=f'user-{n+1}',created_at='2026-11-12T02:00:00Z',extra_json=json.dumps({'account_purpose':'chatgpt_web_pool',
                    'sms_verification':{'verified':n%3==0},'sync_statuses':{'chatgpt2api':{'imported':True,'ready':False,'needs_resync':True}}})) for n in range(start,min(start+size,137))]
                result={'items':rows,'total':137}
            elif path=='/api/tasks/logs':
                result={'items':[dict(id=n+1,platform='chatgpt',email=f'history-{n+1:03d}@example.invalid',created_at='2026-11-12T02:00:00Z',
                    status='failed' if n%2 else 'success',error='模拟失败：保留可读错误信息，不改变真实业务结果。'* (1+n%3) if n%2 else '') for n in range(start,min(start+size,137))],'total':137}
            else:
                result=[dict(id=n+1,url=f'http://127.0.0.1:{10000+n}',region='US',is_active=True,success_count=n,fail_count=0) for n in range(90)]
            return route.fulfill(content_type='application/json',body=json.dumps(result,ensure_ascii=False))
        return super().handle(route)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--url',default='http://127.0.0.1:18765');args=parser.parse_args()
    output=ROOT/'dist'/'list-scroll';output.mkdir(parents=True,exist_ok=True)
    report=[]
    with sync_playwright() as pw:
        browser=pw.chromium.launch(headless=True)
        context=browser.new_context(viewport={'width':1440,'height':900},locale='zh-CN',timezone_id='Asia/Shanghai')
        fixtures=ListFixtures();context.route('**/*',fixtures.handle)
        page=context.new_page();page.set_default_timeout(10000)
        errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
        def button(label):return page.get_by_role('button').filter(has_text=re.compile('^'+r'\s*'.join(re.escape(c) for c in label)+'$'))
        def open_list(path,title):
            page.goto(args.url+path,wait_until='networkidle')
            expect(page.get_by_role('heading',name=title,exact=True)).to_be_visible()
            expect(page.locator('.admin-table-viewport .ant-table-body')).to_be_visible()
            page.wait_for_function("document.querySelector('.admin-table-viewport .ant-table-body')?.clientHeight>50")
        def layout():
            return page.evaluate('''() => {
                const rect = selector => {const el=document.querySelector(selector);if(!el)return null;const r=el.getBoundingClientRect();return {top:r.top,bottom:r.bottom,height:r.height}};
                const body=document.querySelector('.admin-table-viewport .ant-table-body');
                const content=document.querySelector('.admin-shell-content');
                return {header:rect('.admin-page-header'),filters:rect('.admin-list-controls'),tableHead:rect('.ant-table-header'),pagination:rect('.ant-table-pagination'),
                  body:{...rect('.admin-table-viewport .ant-table-body'),height:body.clientHeight,scrollHeight:body.scrollHeight,top:body.scrollTop,left:body.scrollLeft,scrollWidth:body.scrollWidth,width:body.clientWidth},
                  content:{height:content.clientHeight,scrollHeight:content.scrollHeight,scrollTop:content.scrollTop},document:{height:document.documentElement.clientHeight,scrollHeight:document.documentElement.scrollHeight,scrollTop:document.scrollingElement.scrollTop}};
            }''')
        def verify_scroll(name,has_pagination=True):
            # Collapse uses a transition; measure only after the available slot
            # and ResizeObserver height have stopped changing, not mid-animation.
            page.wait_for_function('''() => {
              const el=document.querySelector('.admin-table-viewport'), body=el?.querySelector('.ant-table-body');
              if(!el||!body)return false;
              const r=el.getBoundingClientRect(),key=[r.top,r.height,body.clientHeight].map(n=>Math.round(n*10)).join(':');
              const state=window.__listStable || {key:'',count:0};
              state.count=state.key===key?state.count+1:0;state.key=key;window.__listStable=state;
              return state.count>5;
            }''')
            before=layout()
            assert before['body']['scrollHeight']>before['body']['height']+30,(name,'no row scroll',before)
            assert before['body']['height']>=75,(name,'list inaccessible',before)
            assert before['content']['scrollHeight']<=before['content']['height']+2,(name,'outer content scrolls',before)
            assert before['document']['scrollHeight']<=before['document']['height']+2,(name,'document scrolls',before)
            if has_pagination:
                assert before['pagination']['bottom']<=before['document']['height']+1,(name,'pagination outside viewport',before)
            body=page.locator('.admin-table-viewport .ant-table-body')
            body.hover();page.mouse.wheel(0,900)
            page.wait_for_function("document.querySelector('.admin-table-viewport .ant-table-body').scrollTop > 0")
            after=layout()
            assert after['body']['top']>0,(name,'rows did not move',after)
            assert abs(after['header']['top']-before['header']['top'])<1
            assert abs(after['tableHead']['top']-before['tableHead']['top'])<1
            if before['filters']:assert abs(after['filters']['top']-before['filters']['top'])<1
            if has_pagination:assert abs(after['pagination']['top']-before['pagination']['top'])<1
            assert after['content']['scrollTop']==0 and after['document']['scrollTop']==0
            page.mouse.wheel(0,50000)
            page.wait_for_function("() => {const el=document.querySelector('.admin-table-viewport .ant-table-body'); return el.scrollTop+el.clientHeight>=el.scrollHeight-3;}")
            assert layout()['content']['scrollTop']==0 and layout()['document']['scrollTop']==0
            report.append({'case':name,'body_height':after['body']['height'],'scroll_height':after['body']['scrollHeight'],'header_fixed':True,'pagination_fixed':has_pagination})
            print('PASS',name,flush=True)

        open_list('/accounts/chatgpt','ChatGPT 账号')
        verify_scroll('accounts-desktop')
        page.mouse.move(0,0);page.screenshot(path=str(output/'accounts-scrolled-desktop.png'))
        first=page.locator('.ant-table-tbody tr[data-row-key] .ant-checkbox-input').first
        first.check();expect(page.get_by_text('已选 1 个账号',exact=True)).to_be_visible()
        verify_scroll('accounts-selected-toolbar')
        before_height=layout()['body']['height']
        button('时间筛选').click();expect(page.get_by_placeholder('开始时间')).to_be_visible()
        verify_scroll('accounts-date-filters-expanded')
        assert layout()['body']['height']<=before_height
        body=page.locator('.ant-table-body');body.evaluate('(el)=>{el.scrollLeft=350}')
        page.wait_for_function("document.querySelector('.ant-table-body').scrollLeft>0")
        assert layout()['body']['left']>0
        report.append({'case':'accounts-horizontal-scroll','preserved':True})
        page.locator('.ant-pagination-next button').click()
        expect(page.get_by_text('account-051@example.invalid',exact=True)).to_be_attached()
        assert any(path=='/accounts' and query.get('page')==['2'] for method,path,query in fixtures.requests)
        report.append({'case':'accounts-server-pagination','page':2})
        page.locator('.ant-pagination-item-1').click()
        expect(page.get_by_text('account-001@example.invalid',exact=True)).to_be_attached()
        page.locator('.ant-pagination-options .ant-select').click()
        page.get_by_title('100 条/页',exact=True).click()
        page.wait_for_function("document.querySelectorAll('.ant-table-tbody tr[data-row-key]').length===100")
        verify_scroll('accounts-page-size-100')

        # Height recomputes for sidebar, wrapping toolbars, and viewport changes.
        for width,height in ((1366,768),(1024,620),(430,932),(390,844),(810,450)):
            page.set_viewport_size({'width':width,'height':height})
            open_list('/accounts/chatgpt','ChatGPT 账号')
            button('时间筛选').click()
            verify_scroll(f'accounts-{width}x{height}-expanded')
            if width==430:
                page.mouse.move(0,0);page.screenshot(path=str(output/'accounts-scrolled-mobile.png'))

        page.set_viewport_size({'width':1440,'height':900})
        open_list('/history','任务历史');verify_scroll('task-history-desktop')
        page.mouse.move(0,0);page.screenshot(path=str(output/'history-scrolled.png'))
        page.set_viewport_size({'width':430,'height':932})
        open_list('/history','任务历史');verify_scroll('task-history-mobile')
        page.set_viewport_size({'width':1440,'height':900})
        open_list('/proxies','代理管理');verify_scroll('proxy-list-desktop',False)
        page.locator('.admin-proxy-add .ant-collapse-header').click()
        expect(page.get_by_placeholder('地区标签 (如 US, SG)')).to_be_visible()
        verify_scroll('proxy-add-expanded',False)
        page.set_viewport_size({'width':430,'height':932})
        open_list('/proxies','代理管理');verify_scroll('proxy-list-mobile',False)
        # Non-list forms retain their independent content scroller, not document scrolling.
        page.goto(args.url+'/chatgpt-web-pool',wait_until='networkidle')
        expect(page.get_by_role('heading',name='Web 账号池',exact=True)).to_be_visible()
        assert page.locator('.admin-list-page').count()==0
        assert page.locator('.admin-shell-content').evaluate('(el)=>el.scrollHeight>el.clientHeight')
        report.append({'case':'non-list-form-scroll-preserved','passed':True})
        assert not errors,errors
        assert not fixtures.writes,fixtures.writes
        assert not fixtures.unknown,fixtures.unknown
        (output/'report.json').write_text(json.dumps({'checks':report,'browser_errors':errors,'mutation_requests':fixtures.writes},ensure_ascii=False,indent=2),encoding='utf-8')
        browser.close()
    print(str(len(report))+' list scroll checks passed',flush=True)
if __name__=='__main__':main()
