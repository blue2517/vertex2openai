import json
import shutil
import subprocess

import pytest


def test_cookie_and_key_proxy_frontend_interactions():
    import main
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is needed for frontend interaction checks')
    source = '/* ---------- Channel' + main.DASHBOARD_HTML.split('/* ---------- Channel', 1)[1].split('/* ---------- Params', 1)[0]
    runner = r'''
const vm=require('node:vm'), assert=require('node:assert/strict');
let raw=''; process.stdin.setEncoding('utf8'); process.stdin.on('data',v=>raw+=v);
process.stdin.on('end', async()=>{
try {
  const elements={}, rows=[], selectors=[], keyRows=[], calls=[], replies=[];
  const el=()=>({value:'',textContent:'',innerHTML:'',disabled:false,checked:false,type:'password',dataset:{},focus(){},querySelectorAll:()=>[],classList:{toggle(){}}});
  const $=id=>elements[id]||(elements[id]=el());
  const context=vm.createContext({$,toast(){},confirm:()=>true,document:{querySelectorAll(selector){
    if(selector==='[data-cookie-i]') return rows;
    if(selector==='.credential-proxy') return selectors;
    if(selector==='#keys-list [data-i]') return keyRows;
    return [];
  }},fetch:async(...args)=>{calls.push(args); const r=replies.shift(); if(r instanceof Error) throw r; if(!r) throw Error('missing fixture response'); return r;}});
  vm.runInContext(JSON.parse(raw),context);
  const response=(data,ok=true)=>({ok,json:async()=>JSON.parse(JSON.stringify(data))});
  const evaluate=expr=>vm.runInContext(expr,context);
  await context.saveCookieCredentials();
  assert.equal(calls.length,0); // A failed initial read must not erase existing credentials.
  replies.push(new Error('offline'));
  await context.loadCookieCredentials();
  assert.equal($('cookies-result').textContent,'offline');
  const stored={id:'account-a',name:'<img src=x>',project_id:'project-a',enabled:true,proxy_mode:'pool',proxy_id:'exit-a',cookie_configured:true,cookie_masked:'configured (masked)'};
  replies.push(response({credentials:[stored]}));
  await context.loadCookieCredentials();
  assert.equal(calls[0][0],'/api/cookie-credentials');
  assert.equal($('cookies-count').textContent,'1 个');
  assert.equal($('cookies-list').innerHTML.includes('<img'),false);
  assert.equal($('cookies-list').innerHTML.includes('&lt;img'),true);
  assert.equal($('cookies-list').innerHTML.includes('代理已失效'),true);
  const fields={'.c-name':{value:'Account A'},'.c-cookie':{value:''},'.c-project':{value:'https://console.cloud.google.com/studio?project=project-a'},'.c-enabled':{checked:true},'.c-proxy':{value:'pool:exit-a'}};
  rows.push({dataset:{cookieI:'0'},querySelector:selector=>fields[selector]});
  const boundSelect={value:'pool:exit-a',innerHTML:''}; selectors.push(boundSelect);
  const status={mode:'direct',pool:[{id:'exit-a',name:'US <img>',url_masked:'http://***:***@HOST:8080',active:false}],bound_credentials:{'exit-a':1}};
  context.renderNetworkProxy(status);
  assert.equal(boundSelect.value,'pool:exit-a');
  assert.equal(boundSelect.innerHTML.includes('US &lt;img&gt;'),true);
  assert.equal($('proxy-pool-list').innerHTML.includes('已绑定 1 条凭证'),true);
  assert.equal($('proxy-pool-list').innerHTML.includes('data-locked="1"'),true);
  const submitted=context.collectCookies()[0];
  assert.equal(submitted.cookie,'');
  assert.equal(submitted.project_id,'project-a');
  assert.equal(submitted.proxy_id,'exit-a');
  replies.push(response({credentials:[stored],total:1,enabled:1}), response(status));
  await context.saveCookieCredentials();
  const saveCall=calls.find(c=>c[1]?.method==='POST');
  assert.equal(saveCall[0],'/api/cookie-credentials');
  assert.equal(JSON.parse(saveCall[1].body).credentials[0].cookie,'');
  assert.equal(evaluate('COOKIES[0].cookie'),undefined);
  assert.equal($('cookies-save').disabled,false);
  fields['.c-cookie'].value='[{"name":"SAPISID","value":"PRIVATE_NEW"},{"name":"SID","value":"PRIVATE_SID"}]';
  assert.equal(context.collectCookies()[0].cookie,'SAPISID=PRIVATE_NEW; SID=PRIVATE_SID');
  replies.push(response({error:'invalid project'},false));
  await context.saveCookieCredentials();
  assert.equal($('cookies-result').textContent,'invalid project');
  assert.equal($('cookies-save').disabled,false);
  assert.equal(fields['.c-cookie'].value.includes('PRIVATE_NEW'),true);
  evaluate('COOKIES[0].cookie="</textarea><img src=x>"');
  context.renderCookies();
  assert.equal($('cookies-list').innerHTML.includes('</textarea><img'),false);
  // Key selector uses the same binding schema and escapes user-controlled text.
  evaluate('KEYS=[{key:"<img>",project_id:"project-k",enabled:true,proxy_mode:"direct",_msg:"<img>"}]');
  context.renderKeys();
  assert.equal($('keys-list').innerHTML.includes('<img>'),false);
  assert.equal($('keys-list').innerHTML.includes('credential-proxy k-proxy'),true);
  const keyFields={'.k-key':{value:'KEY_A'},'.k-project':{value:'project-k'},'.k-enabled':{checked:true},'.k-proxy':{value:'pool:exit-a'}};
  keyRows.push({dataset:{i:'0'},querySelector:selector=>keyFields[selector]});
  assert.equal(context.collectKeys()[0].proxy_id,'exit-a');
  keyFields['.k-proxy'].value='direct';
  assert.equal(context.collectKeys()[0].proxy_mode,'direct');
  assert.equal(context.collectKeys()[0].proxy_id,'');
  process.stdout.write('Credential frontend checks passed\n');
} catch(error) {console.error(error); process.exitCode=1;}
});
'''
    result = subprocess.run([node, '-e', runner], input=json.dumps(source), text=True, encoding='utf-8', capture_output=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def test_entire_dashboard_javascript_syntax():
    import main
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is needed for syntax checks')
    source = main.DASHBOARD_HTML.split('<script>', 1)[1].split('</script>', 1)[0]
    runner = 'let s="";process.stdin.setEncoding("utf8");process.stdin.on("data",v=>s+=v);process.stdin.on("end",()=>new (require("node:vm").Script)(s));'
    result = subprocess.run([node, '-e', runner], input=source, text=True, encoding='utf-8', capture_output=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def test_dashboard_initial_event_handlers_only_reference_existing_elements():
    import main
    import re
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is needed for runtime checks')
    source = main.DASHBOARD_HTML.split('<script>', 1)[1].split('</script>', 1)[0].split('/* init */', 1)[0]
    ids = re.findall(r'\bid="([^"]+)"', main.DASHBOARD_HTML)
    runner = r'''
let raw='';process.stdin.setEncoding('utf8');process.stdin.on('data',v=>raw+=v);process.stdin.on('end',()=>{
  const {source,ids}=JSON.parse(raw), elements=Object.fromEntries(ids.map(id=>[id,{addEventListener(){}}]));
  require('node:vm').runInNewContext(source,{document:{getElementById:id=>elements[id]||null},EventSource:class{}});
});'''
    result = subprocess.run([node, '-e', runner], input=json.dumps({'source': source, 'ids': ids}), text=True, encoding='utf-8', capture_output=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
