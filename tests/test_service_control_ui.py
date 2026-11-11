import json
import shutil
import subprocess

import pytest


def test_restart_frontend_interactions():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is needed for frontend interaction checks")
    import main

    source = main.DASHBOARD_HTML.split("/* ---------- Service restart ---------- */", 1)[1].split(
        "/* ---------- End service restart ---------- */", 1
    )[0]
    assert 'id="restart-service" onclick="restartService()"' in main.DASHBOARD_HTML
    runner = r"""
const vm = require('node:vm');
const assert = require('node:assert/strict');
let raw = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', chunk => raw += chunk);
process.stdin.on('end', async () => {
  const source = JSON.parse(raw);
  const reply = (data, status=200) => ({ok: status<400, status, json: async()=>data});
  const old = {instance_id:'old', restarting:false, error:null};
  function setup(replies, accepted=true) {
    let now=0, reloads=0;
    const calls=[];
    const elements={
      'restart-service': {disabled:false, textContent:'重启'},
      'restart-status': {textContent:'', classList:{remove(){}}}
    };
    const context=vm.createContext({
      $: id=>elements[id], confirm:()=>accepted,
      location:{reload(){reloads++;}}, AbortController,
      Date:{now:()=>now},
      setTimeout(callback, ms){
        if(ms===1500) {now+=ms; queueMicrotask(callback);}
        return 1;
      }, clearTimeout(){},
      fetch:async (url, options)=>{
        calls.push([url,options]);
        assert.equal(options.cache,'no-store');
        assert.ok(options.signal);
        const next=replies.length ? replies.shift() : reply(old);
        if(next instanceof Error) throw next;
        return next;
      }
    });
    vm.runInContext(source, context);
    return {context,calls,elements,get reloads(){return reloads;}};
  }
  let test=setup([],false);
  await test.context.restartService();
  assert.equal(test.calls.length,0);

  // Do not treat the old process or a brief outage as successful recovery.
  test=setup([reply(old),reply(old,202),reply(old),new Error('offline'),reply({instance_id:'new'})]);
  const pending=test.context.restartService();
  assert.equal(test.elements['restart-service'].disabled,true);
  await test.context.restartService(); // duplicate click
  await pending;
  assert.equal(test.reloads,1);
  assert.equal(test.calls.length,5);
  assert.equal(test.calls[1][1].method,'POST');
  assert.equal(test.calls[1][1].headers['X-Requested-With'],'service-restart');

  // A lost POST response still polls, without ever reissuing the restart.
  test=setup([reply(old),new Error('connection reset'),reply({instance_id:'new'})]);
  await test.context.restartService();
  assert.equal(test.reloads,1);
  assert.equal(test.calls.filter(c=>c[1].method==='POST').length,1);

  for(const code of [401,403,503]) {
    test=setup([reply(old),reply({detail:'server explanation'},code)]);
    await test.context.restartService();
    assert.equal(test.reloads,0);
    assert.equal(test.calls.length,2);
    assert.equal(test.elements['restart-service'].disabled,false);
    assert.ok(test.elements['restart-status'].textContent.includes(code===401?'登录已过期':'server explanation'));
  }
  test=setup([reply(old),reply(old,202),reply({...old,error:'systemd submission failed'})]);
  await test.context.restartService();
  assert.equal(test.reloads,0);
  assert.equal(test.elements['restart-status'].textContent,'systemd submission failed');

  test=setup([reply(old),reply(old,202)]);
  await test.context.restartService();
  assert.equal(test.reloads,0);
  assert.ok(test.elements['restart-status'].textContent.includes('等待超时'));
  assert.equal(test.elements['restart-service'].disabled,false);

  test=setup([new Error('offline')]);
  await test.context.restartService();
  assert.equal(test.calls.length,1);
  assert.equal(test.reloads,0);
  test=setup([reply({})]);
  await test.context.restartService();
  assert.equal(test.calls.length,1);
  assert.ok(test.elements['restart-status'].textContent.includes('实例标识缺失'));
});
"""
    result = subprocess.run(
        [node, "-e", runner], input=json.dumps(source), text=True,
        encoding="utf-8", capture_output=True, timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
