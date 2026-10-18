import json
import shutil
import subprocess

import pytest


def test_proxy_frontend_interactions():
    """执行真实内联 JS，使用最小 DOM/Fetch 模拟覆盖表单与异步状态。"""
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is needed for frontend interaction checks')
    import main

    source = main.DASHBOARD_HTML.split('/* ---------- 出网代理', 1)[1]
    source = '/* ---------- 出网代理' + source.split('/* ---------- Express Key', 1)[0]
    runner = r'''
const vm = require('node:vm');
const assert = require('node:assert/strict');
let raw = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', chunk => raw += chunk);
process.stdin.on('end', async () => {
  try {
    const source = JSON.parse(raw);
    const elements = {};
    const element=()=>({value:'',textContent:'',innerHTML:'',disabled:true,checked:false,type:'password',className:'',dataset:{},querySelectorAll:()=>[],focus(){},classList:{toggle(name, value){this[name]=value;}}});
    for (const id of ['proxy-mode','proxy-url','proxy-show','proxy-test','proxy-save','proxy-result','proxy-effective','proxy-status','proxy-custom']) elements[id]=element();
    elements['proxy-test'].textContent = 'test'; elements['proxy-save'].textContent = 'save';
    const calls = [], replies = [];
    const context = vm.createContext({
      $: id => elements[id]||(elements[id]=element()), toast: () => {}, confirm:()=>true,
      fetch: async (...args) => { calls.push(args); const next=replies.shift(); if(next instanceof Error) throw next; return next; }
    });
    vm.runInContext(source, context);
    const status = {mode:'environment',effective_url_masked:'socks5://***:***@ENV:1080',environment_url_masked:'socks5://***:***@ENV:1080',custom_url_masked:''};
    const reply = (value,ok=true) => ({ok,json:async()=>value});
    replies.push(reply(status));
    await context.loadNetworkProxy();
    assert.equal(calls[0][0],'/api/proxy');
    assert.equal(elements['proxy-url'].value,'');
    assert.equal(elements['proxy-mode'].disabled,false);
    const entry={id:'node-1',name:'<img src=x onerror=alert(1)>',url_masked:'socks5://***:***@HOST:1080',active:false};
    const poolStatus={...status,mode:'environment',enabled:true,current_in_pool:true,active_id:entry.id,pool:[entry]};
    context.renderNetworkProxy(poolStatus);
    assert.equal(elements['proxy-pool-list'].innerHTML.includes('<img'),false);
    assert.equal(elements['proxy-pool-list'].innerHTML.includes('&lt;img'),true);
    assert.equal(elements['proxy-pool-current'].disabled,true);
    replies.push(reply({ok:true,message:'connected',http_status:204,elapsed_ms:3}));
    await context.testProxyEntry(entry.id);
    assert.equal(calls.at(-1)[0],'/api/proxy/test');
    assert.equal(JSON.parse(calls.at(-1)[1].body).proxy_id,entry.id);
    assert.equal(elements['proxy-mode'].value,'environment');
    const activeStatus={...poolStatus,mode:'pool',pool:[{...entry,active:true}]};
    replies.push(reply(activeStatus));
    await context.activateProxyEntry(entry.id);
    assert.equal(calls.at(-1)[0],'/api/proxy');
    assert.equal(elements['proxy-mode'].value,'pool');
    assert.equal(elements['proxy-pool-select'].value,entry.id);
    assert.equal(elements['proxy-pool-choice'].classList.hidden,false);
    context.editProxyEntry(entry.id);
    assert.equal(elements['proxy-entry-url'].value,'');
    elements['proxy-entry-name'].value='renamed';
    replies.push(reply({...activeStatus,pool:[{...entry,name:'renamed',active:true}]}));
    await context.saveProxyEntry();
    assert.equal(calls.at(-1)[0],'/api/proxy/pool/node-1');
    assert.equal(calls.at(-1)[1].method,'PUT');
    assert.equal(JSON.parse(calls.at(-1)[1].body).proxy_url,null);
    assert.equal(elements['proxy-entry-name'].value,'');
    assert.equal(elements['proxy-entry-url'].value,'');
    elements['proxy-entry-name'].value='new'; elements['proxy-entry-url'].value='socks5://USER:SECRET@SECOND:1080';
    replies.push(reply(poolStatus));
    await context.saveProxyEntry();
    assert.equal(calls.at(-1)[0],'/api/proxy/pool');
    assert.equal(calls.at(-1)[1].method,'POST');
    assert.equal(elements['proxy-entry-url'].value,'');
    replies.push(reply({...status,pool:[]}));
    await context.deleteProxyEntry(entry.id);
    assert.equal(calls.at(-1)[1].method,'DELETE');
    assert.equal(calls.at(-1)[1].body,undefined);
    assert.equal(elements['proxy-url'].disabled,true);
    elements['proxy-mode'].value='custom'; context.proxyModeChanged();
    assert.equal(elements['proxy-custom'].classList.hidden,false);
    assert.equal(elements['proxy-url'].disabled,false);
    elements['proxy-url'].value='socks5://USER:PRIVATE_PASSWORD@HOST:1080';
    replies.push(reply({ok:true,http_status:204,message:'connected',elapsed_ms:12,proxy_url_masked:'socks5://***:***@HOST:1080'}));
    const probeRun = context.testNetworkProxy();
    assert.equal(elements['proxy-url'].disabled,true);
    assert.equal(elements['proxy-mode'].disabled,true);
    assert.equal(elements['proxy-save'].disabled,true);
    await probeRun;
    assert.equal(calls.at(-1)[0],'/api/proxy/test');
    assert.equal(JSON.parse(calls.at(-1)[1].body).proxy_url,elements['proxy-url'].value);
    assert.equal(elements['proxy-result'].textContent.includes('PRIVATE_PASSWORD'),false);
    assert.equal(elements['proxy-url'].value.includes('PRIVATE_PASSWORD'),true);
    replies.push(reply({...status,mode:'custom',custom_url_masked:'socks5://***:***@HOST:1080',effective_url_masked:'socks5://***:***@HOST:1080'}));
    await context.saveNetworkProxy();
    assert.equal(calls.at(-1)[0],'/api/proxy');
    assert.equal(elements['proxy-url'].value,'');
    assert.equal(elements['proxy-url'].type,'password');
    assert.equal(elements['proxy-test'].disabled,false);
    elements['proxy-mode'].value='direct'; context.proxyModeChanged();
    elements['proxy-url'].value='socks5://UNSAVED:PRIVATE_PASSWORD@HOST:1080';
    assert.equal(elements['proxy-url'].disabled,true);
    replies.push(reply({detail:'invalid config'},false));
    await context.saveNetworkProxy();
    assert.equal(JSON.parse(calls.at(-1)[1].body).mode,'direct');
    assert.equal(JSON.parse(calls.at(-1)[1].body).proxy_url,null);
    assert.equal(elements['proxy-result'].textContent,'invalid config');
    assert.equal(elements['proxy-save'].disabled,false);
    assert.equal(elements['proxy-url'].disabled,true);
    replies.push(new Error('fetch failed'));
    await context.testNetworkProxy();
    assert.equal(elements['proxy-result'].textContent,'fetch failed');
    assert.equal(elements['proxy-mode'].disabled,false);
    process.stdout.write('Frontend interaction checks passed\n');
  } catch (error) { console.error(error); process.exitCode=1; }
});
'''
    result = subprocess.run([node, '-e', runner], input=json.dumps(source), text=True, encoding='utf-8', capture_output=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
