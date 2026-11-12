"""Verify the existing public site with GET-only browser traffic.
Short-lived panel token stays in process/browser memory, never logged or saved.
"""
from __future__ import annotations
import base64
import hashlib
import json
import re
from pathlib import Path
import subprocess
from urllib.parse import urlsplit
from playwright.sync_api import sync_playwright, expect

ROOT=Path(__file__).resolve().parents[1]
URL='https://sdk.yepoodai.cn'
RELEASE='ui-reliable-20261112T051329Z'
EXPECTED='54b94678613e7d47f18dab05a51b235893cc8843a677cfdc3a18b0d669367196'
OUT=ROOT/'dist'/RELEASE/'production-ui'
OUT.mkdir(parents=True,exist_ok=True)
# Use effective auth configuration read-only. Never call helpers that generate/persist secrets.
remote=r'''
import os,time,base64,json,hashlib,hmac
from core.config_store import config_store
if not config_store.get('auth_password_hash',''):print('')
else:
 key=os.getenv('APP_JWT_SECRET') or config_store.get('auth_jwt_secret','')
 assert key,'No effective signing secret'
 encode=lambda value:base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b'=').decode()
 head,body=encode({'alg':'HS256','typ':'JWT'}),encode({'sub':'admin','iat':int(time.time()),'exp':int(time.time())+300})
 sig=base64.urlsafe_b64encode(hmac.new(key.encode(),(head+'.'+body).encode(),hashlib.sha256).digest()).rstrip(b'=').decode()
 print(head+'.'+body+'.'+sig)
'''
profile=Path.home()/'.dsh-devops/ssh/configs/srv_5e286275b64d0ef6.conf'
result=subprocess.run(['ssh','-F',str(profile),'-o','BatchMode=yes','dsh-devops-target','docker exec -i any-auto-register python -'],
    input=remote.encode(),stdout=subprocess.PIPE,stderr=subprocess.PIPE)
if result.returncode:raise SystemExit('Could not obtain in-memory verification session; diagnostic suppressed')
token=result.stdout.decode().strip()
assert not token or (token.count('.')==2 and '\n' not in token),'Unexpected authentication response'
with sync_playwright() as pw:
 browser=pw.chromium.launch(headless=True)
 context=browser.new_context(viewport={'width':1440,'height':1050},locale='zh-CN',timezone_id='Asia/Shanghai')
 context.add_init_script("localStorage.setItem('auth_token',"+json.dumps(token)+");localStorage.setItem('theme','dark')")
 errors=[];blocked=[];responses=[];blocked_analytics=[];asset_checks=[];public_entry_sha256=None
 def boundary(route):
  request=route.request;parsed=urlsplit(request.url)
  if parsed.hostname=='static.cloudflareinsights.com':
   blocked_analytics.append('Cloudflare analytics script');return route.abort()
  if parsed.hostname!='sdk.yepoodai.cn' or request.method not in ('GET','HEAD','OPTIONS'):
   blocked.append(request.method+' '+parsed.path);return route.abort()
  return route.continue_()
 context.route('**/*',boundary)
 page=context.new_page();page.on('pageerror',lambda err:errors.append(str(err)))
 checks=[]
 for route,title,name in [('/chatgpt-web-pool','Web 账号池','web-pool-live'),('/', '运营总览','dashboard-live'),
     ('/sms-stock','可用 RT 库存','stock-live'),('/inspection','Codex／RT 巡检','inspection-live'),('/running-tasks','任务运行','tasks-live')]:
  response=page.goto(URL+route,wait_until='networkidle',timeout=60000)
  assert response and response.status==200,'Site route not healthy'
  expect(page.get_by_role('heading',name=title,exact=True)).to_be_visible()
  if route=='/chatgpt-web-pool':
   expect(page.locator('#max_attempts_per_run')).not_to_be_visible()
   expect(page.locator('#base_url')).not_to_be_visible()
   raw=response.body()
   public_entry_sha256=hashlib.sha256(raw).hexdigest()
   # Cloudflare may append its analytics script to HTML. Verify every local
   # entry reference and public JS/CSS bytes instead of rejecting that injection.
   origin=(ROOT/'static/index.html').read_bytes()
   assert hashlib.sha256(origin).hexdigest()==EXPECTED
   local_refs=set(re.findall(r'(?:src|href)="(/assets/[^\"]+)"',origin.decode()))
   public_refs=set(re.findall(r'(?:src|href)="(/assets/[^\"]+)"',raw.decode()))
   assert local_refs==public_refs,'Public page references a different build'
   for asset in sorted(local_refs):
    public=context.request.get(URL+asset,timeout=30000)
    assert public.status==200,'Missing entry asset '+asset
    expected=hashlib.sha256((ROOT/'static'/asset.removeprefix('/')).read_bytes()).hexdigest()
    assert hashlib.sha256(public.body()).hexdigest()==expected,'Public asset bytes differ '+asset
    asset_checks.append({'asset':asset,'sha256':expected,'http':200})
  page.mouse.move(0,0)
  page.screenshot(path=str(OUT/(name+'.png')),full_page=True)
  checks.append({'route':route,'http':response.status,'heading':title})
  print('PUBLIC_UI_OK',route,flush=True)
 headers={'Authorization':'Bearer '+token} if token else {}
 for path in ('/api/auth/status','/api/tasks/resources','/api/chatgpt-web-pool/config','/api/chatgpt-web-pool/status','/api/sms-stock/status','/api/inspection/status'):
  response=context.request.get(URL+path,headers=headers,timeout=30000)
  assert response.status==200,'Public API failed '+path
  data=response.json();row={'path':path,'http':response.status,'bytes':len(response.body())}
  if path.endswith('/chatgpt-web-pool/config'):
   assert 'admin_token' not in data
   row['policy']={k:data.get(k) for k in ('enabled','target_mode','target_value','refill_below','daily_registration_limit','protocol')}
  if path.endswith('/tasks/resources'):row['resources']=data
  responses.append(row)
 assert not errors,errors
 assert not blocked,blocked
 report={'release':RELEASE,'site':URL,'routes':checks,'get_apis':responses,'browser_errors':errors,'blocked_actions':blocked,
   'origin_static_index_sha256':EXPECTED,'public_entry_sha256':public_entry_sha256,'verified_assets':asset_checks,
   'external_analytics_blocked':len(blocked_analytics),'read_only':True}
 (OUT/'verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
 browser.close()
print('PUBLIC_BROWSER_VERIFICATION_COMPLETE no mutation requests',flush=True)
