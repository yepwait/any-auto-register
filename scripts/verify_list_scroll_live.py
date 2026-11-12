"""GET-only verification of fixed-list scrolling at the existing public site.
Panel token is short lived and remains in process/browser memory, never logged.
"""
from pathlib import Path
import hashlib,json,re,subprocess,tarfile
from urllib.parse import urlsplit
from playwright.sync_api import sync_playwright,expect

ROOT=Path(__file__).resolve().parents[1]
RELEASE='list-scroll-20261112T062323Z'
SITE='https://sdk.yepoodai.cn'
OUT=ROOT/'dist'/RELEASE/'production';OUT.mkdir(parents=True,exist_ok=True)
remote=r'''
import time,os,base64,json,hashlib,hmac
from core.config_store import config_store
if not config_store.get('auth_password_hash',''):print('')
else:
 key=os.getenv('APP_JWT_SECRET') or config_store.get('auth_jwt_secret','');assert key
 encode=lambda v:base64.urlsafe_b64encode(json.dumps(v).encode()).rstrip(b'=').decode()
 h,p=encode({'alg':'HS256','typ':'JWT'}),encode({'sub':'admin','iat':int(time.time()),'exp':int(time.time())+300})
 s=base64.urlsafe_b64encode(hmac.new(key.encode(),(h+'.'+p).encode(),hashlib.sha256).digest()).rstrip(b'=').decode()
 print(h+'.'+p+'.'+s)
'''
profile=Path.home()/'.dsh-devops/ssh/configs/srv_5e286275b64d0ef6.conf'
p=subprocess.run(['ssh','-F',str(profile),'-o','BatchMode=yes','dsh-devops-target','docker exec -i any-auto-register python -'],
 input=remote.encode(),stdout=subprocess.PIPE,stderr=subprocess.PIPE)
assert p.returncode==0,'Cannot establish read-only session'
token=p.stdout.decode().strip();assert not token or (token.count('.')==2 and '\n' not in token)
with tarfile.open(ROOT/'dist'/(RELEASE+'.tar.gz')) as pack:
 manifest=json.load(pack.extractfile('manifest.json'))
 index=pack.extractfile('payload/static/index.html').read()
refs=set(re.findall(r'(?:src|href)="(/assets/[^\"]+)"',index.decode()))
report={'release':RELEASE,'site':SITE,'checks':[],'get_only':True}
with sync_playwright() as pw:
 browser=pw.chromium.launch(headless=True)
 ctx=browser.new_context(viewport={'width':1366,'height':768},locale='zh-CN',timezone_id='Asia/Shanghai')
 ctx.add_init_script("localStorage.setItem('auth_token',"+json.dumps(token)+");localStorage.setItem('theme','dark')")
 mutations=[];unknown=[];errors=[]
 def boundary(route):
  r=route.request;u=urlsplit(r.url)
  if u.hostname=='static.cloudflareinsights.com':return route.abort()
  if u.hostname!='sdk.yepoodai.cn':unknown.append(u.hostname);return route.abort()
  if r.method not in ('GET','HEAD','OPTIONS'):mutations.append(r.method+' '+u.path);return route.abort()
  return route.continue_()
 ctx.route('**/*',boundary)
 page=ctx.new_page();page.on('pageerror',lambda error:errors.append(str(error)))
 response=page.goto(SITE+'/accounts/chatgpt',wait_until='networkidle',timeout=60000)
 assert response and response.status==200
 expect(page.get_by_role('heading',name='ChatGPT 账号',exact=True)).to_be_visible()
 public_refs=set(re.findall(r'(?:src|href)="(/assets/[^\"]+)"',response.text()))
 assert refs==public_refs,'Public entry uses a different static build'
 verified=[]
 for ref in sorted(refs):
  r=ctx.request.get(SITE+ref,timeout=30000);assert r.status==200
  assert hashlib.sha256(r.body()).hexdigest()==manifest['files']['static'+ref]
  verified.append(ref)
 report['entry_assets_verified']=verified
 def geometry():
  return page.evaluate('''() => {
   const box=selector=>{const e=document.querySelector(selector);if(!e)return null;const r=e.getBoundingClientRect();return {top:r.top,bottom:r.bottom,height:r.height}};
   const body=document.querySelector('.admin-table-viewport .ant-table-body');const content=document.querySelector('.admin-shell-content');
   return {header:box('.admin-page-header'),filters:box('.admin-list-controls'),tableHeader:box('.ant-table-header'),pager:box('.ant-table-pagination'),
    body:{...box('.admin-table-viewport .ant-table-body'),clientHeight:body.clientHeight,scrollHeight:body.scrollHeight,scrollTop:body.scrollTop},
    contentScrollTop:content.scrollTop,contentScrollHeight:content.scrollHeight,contentHeight:content.clientHeight,documentScrollTop:document.scrollingElement.scrollTop,viewHeight:innerHeight};
  }''')
 def check(name):
  page.wait_for_function("document.querySelector('.admin-table-viewport .ant-table-body')?.clientHeight>50")
  before=geometry();assert before['pager'] and before['pager']['bottom']<=before['viewHeight']+2
  assert before['contentScrollHeight']<=before['contentHeight']+2
  body=page.locator('.admin-table-viewport .ant-table-body')
  if before['body']['scrollHeight']>before['body']['clientHeight']+10:
   body.hover();page.mouse.wheel(0,1000)
   page.wait_for_function("document.querySelector('.admin-table-viewport .ant-table-body').scrollTop>0")
   after=geometry()
   for key in ('header','filters','tableHeader','pager'):
    if before[key]:assert abs(before[key]['top']-after[key]['top'])<1,(name,key)
   assert after['contentScrollTop']==0 and after['documentScrollTop']==0
   moved=True
  else:moved=False
  report['checks'].append({'case':name,'rows_scroll':moved,'body_height':before['body']['clientHeight'],
   'body_scroll_height':before['body']['scrollHeight'],'filters_and_header_fixed':True,'pagination_visible':True})
  print('PUBLIC_LIST_SCROLL_OK',name,flush=True)
 check('accounts-desktop')
 page.set_viewport_size({'width':430,'height':932})
 page.reload(wait_until='networkidle');check('accounts-mobile')
 page.set_viewport_size({'width':1366,'height':768})
 page.goto(SITE+'/history',wait_until='networkidle',timeout=60000)
 expect(page.get_by_role('heading',name='任务历史',exact=True)).to_be_visible();check('history-desktop')
 assert not mutations,mutations
 assert not unknown,unknown
 assert not errors,errors
 report.update(browser_errors=errors,mutation_requests=mutations)
 (OUT/'verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
 browser.close()
print('LIVE_LIST_SCROLL_VERIFIED_NO_MUTATION',flush=True)
