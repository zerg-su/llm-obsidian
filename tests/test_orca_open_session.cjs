'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {openSession} = require('../scripts/orca-open-session.cjs');
const target = {environment:'remote-id', workspace:'identity:workspace-1', workspaceId:'repo::/project',
  host:'local', runtimeId:'runtime-1', agent:'claude', sessionId:'session-123'};
const terminal = {handle:'term-1', worktreeId:target.workspaceId, executionHostId:'local',
  agentIdentity:'claude', connected:true, tabId:'tab-1'};
function fixture(t, changes={}) {
  const dir=fs.mkdtempSync(path.join(os.tmpdir(),'orca-open-test-'));
  t.after(()=>fs.rmSync(dir,{recursive:true,force:true}));
  const calls=[];
  const env={...target,...changes.target};
  const io={
    checkCompatible:async()=>{if(changes.unsupported) throw new Error('unsupported_api');},
    cli:async(args)=>{
      calls.push(args);
      const action=args.slice(0,2).join(' ');
      let result;
      if(action==='worktree show') result={worktree:{id:changes.workspace||env.workspaceId,hostId:changes.host||env.host,
        identity:{executionHostId:changes.identityHost||env.host},
        address:{environment:env.environment,worktree:env.workspace}}};
      if(action==='terminal list') result={terminals:changes.existing?[{...terminal,agentIdentity:env.agent}]:[],truncated:false};
      if(action==='terminal show') result={terminal:{...terminal,agentIdentity:env.agent,...changes.shown}};
      if(action==='terminal read') result={terminal:{source:'screen',text:'previous conversation'}};
      if(action==='terminal switch') result={focus:{navigated:changes.navigated!==false}};
      return {ok:true,result,_meta:{runtimeId:changes.runtime||env.runtimeId}};
    },
    tabs:async()=>({ok:true,result:{worktree:env.workspaceId,tabs:[{
      type:'terminal',terminal:'term-1',agentStatus:{worktreeId:env.workspaceId,tabId:'tab-1',
      agentType:env.agent,providerSession:{key:'session_id',id:env.sessionId}}
    }]},_meta:{runtimeId:env.runtimeId}}),
    ensure:async(params)=>{
      calls.push(['mutation',params]);
      if(changes.timeout) throw new Error('connection lost');
      return {ok:true,result:{terminal:{...terminal,agentIdentity:env.agent},
        resumeProviderSession:{key:'session_id',id:changes.wrongSession?'other':env.sessionId}},
        _meta:{runtimeId:env.runtimeId}};
    }
  };
  return {io,calls,env,dir};
}
test('opens exact Claude or Codex session and verifies navigation',async t=>{
  for(const agent of ['claude','codex']) {
    const f=fixture(t,{target:{agent}});
    const r=await openSession(f.env,f.io,f.dir);
    assert.equal(r.status,'opened');assert.equal(r.navigated,true);assert.equal(r.sessionId,target.sessionId);
    const m=f.calls.find(c=>c[0]==='mutation')[1];
    assert.deepEqual(m,{kind:'explicit',resumeOrigin:'saved',worktree:target.workspace,agent,
      providerSession:{key:'session_id',id:target.sessionId},presentation:'focused'});
    for(const c of f.calls.filter(c=>c[0]!=='mutation')) {
      assert.equal(c[c.indexOf('--environment')+1],target.environment);
    }
    assert.ok(f.calls.some(c=>c[1]==='read'&&c.includes('--screen')));
  }
});
test('already open is focused without a mutation, including repeat invocation',async t=>{
  const f=fixture(t,{existing:true});
  assert.equal((await openSession(f.env,f.io,f.dir)).status,'opened');
  assert.equal((await openSession(f.env,f.io,f.dir)).status,'opened');
  assert.equal(f.calls.filter(c=>c[0]==='mutation').length,0);
});
for(const [label,changes,blocker] of [
 ['wrong host',{host:'ssh:other'},'workspace_mismatch'],
 ['wrong workspace identity host',{identityHost:'ssh:other'},'workspace_mismatch'],
 ['wrong runtime',{runtime:'runtime-other'},'runtime_mismatch'],
 ['wrong workspace before mutation',{workspace:'other'},'workspace_mismatch'],
 ['unsupported API',{unsupported:true},'unsupported_api'],
 ['wrong workspace',{shown:{worktreeId:'other'}},'terminal_identity_mismatch'],
 ['wrong provider session',{wrongSession:true},'resume_identity_mismatch'],
 ['navigation refused',{navigated:false},'navigation_unverified']
]) test(label,async t=>{
 const f=fixture(t,changes); const r=await openSession(f.env,f.io,f.dir);
 assert.equal(r.status,'blocked');assert.match(r.reason,new RegExp(blocker));
 if(['wrong host','wrong workspace identity host','wrong runtime','wrong workspace before mutation','unsupported API'].includes(label)) assert.equal(f.calls.filter(c=>c[0]==='mutation').length,0);
});
test('lost mutation result is durable and cannot be blindly repeated',async t=>{
 const f=fixture(t,{timeout:true});
 assert.equal((await openSession(f.env,f.io,f.dir)).status,'unknown');
 assert.equal((await openSession(f.env,f.io,f.dir)).status,'unknown');
 assert.equal(f.calls.filter(c=>c[0]==='mutation').length,1);
});
test('unknown result can reconcile an exact existing session without repeating mutation',async t=>{
 const changes={timeout:true};const f=fixture(t,changes);
 assert.equal((await openSession(f.env,f.io,f.dir)).status,'unknown');
 const real=f.io.cli;f.io.cli=async a=>a[1]==='list'?{ok:true,result:{terminals:[terminal],truncated:false},_meta:{runtimeId:target.runtimeId}}:real(a);
 assert.equal((await openSession(f.env,f.io,f.dir)).status,'opened');
 assert.equal(f.calls.filter(c=>c[0]==='mutation').length,1);
});

test('local RuntimeClient explicitly clears inherited environment and pairing',async t=>{
 const dir=fs.mkdtempSync(path.join(os.tmpdir(),'orca-modules-test-'));
 t.after(()=>fs.rmSync(dir,{recursive:true,force:true}));
 const write=(p,s)=>{const f=path.join(dir,p);fs.mkdirSync(path.dirname(f),{recursive:true});fs.writeFileSync(f,s);};
 write('cli/runtime-client.js',`exports.RuntimeClient=class {constructor(...args){exports.args=args;}call(){return {ok:true,result:{appVersion:'test'},_meta:{runtimeId:'runtime-1'}};}};`);
 write('cli/cli-version.js',`exports.readOrcaCliVersion=()=> 'test';`);
 write('shared/rpc-contract/agent-session-params.js',`exports.ExplicitEnsure={safeParse:()=>({success:true})};`);
 write('shared/agent-launch/startup/agent-resume-argv.js',`exports.getAgentResumeArgv=(agent,p)=>agent==='codex'?['codex','resume',p.id]:['claude','--resume',p.id];`);
 const {productionTransport}=require('../scripts/orca-open-session.cjs');
 const io=productionTransport('/selected/orca',dir,{...target,environment:'local'});
 const args=require(path.join(dir,'cli/runtime-client.js')).args;
 assert.equal(args[2],null);assert.equal(args[3],null);assert.equal(args[4],'/selected/orca');
 await io.checkCompatible();
 productionTransport('/selected/orca',dir,target);
 assert.equal(require(path.join(dir,'cli/runtime-client.js')).args[3],target.environment);
});

test('exact-ID catalog stays on its connection and returns metadata without prompts',async()=>{
 const {catalogSessions}=require('../scripts/orca-open-session.cjs');let calls=0;
 const result=await catalogSessions('arbitrary-remote-id','exact-id',{list:async()=>{
  calls++;return {ok:true,result:{sessions:[{agent:'codex',sessionId:'exact-id',title:'Prior work',prompt:'PRIVATE'},
    {agent:'claude',sessionId:'other-id'}],truncated:false},_meta:{runtimeId:'remote-runtime'}};
 }});
 assert.equal(calls,1);assert.equal(result.environment,'arbitrary-remote-id');assert.equal(result.runtimeId,'remote-runtime');
 assert.equal(result.candidates.length,1);assert.equal(result.candidates[0].sessionId,'exact-id');
 assert.ok(!JSON.stringify(result).includes('PRIVATE'));
});
test('concurrent open attempts never issue two mutations',async t=>{
 const f=fixture(t);const results=await Promise.all([openSession(f.env,f.io,f.dir),openSession(f.env,f.io,f.dir)]);
 assert.equal(f.calls.filter(c=>c[0]==='mutation').length,1);
 assert.equal(results.filter(r=>r.status==='opened').length,1);
 assert.ok(results.some(r=>r.reason==='operation_busy_or_stale_lock'));
});

test('incomplete terminal inventory cannot authorize creation',async t=>{
 const f=fixture(t);const cli=f.io.cli;
 f.io.cli=async args=>{const r=await cli(args);if(args[1]==='list')r.result.truncated=true;return r;};
 assert.equal((await openSession(f.env,f.io,f.dir)).reason,'inventory_incomplete');
 assert.equal(f.calls.filter(c=>c[0]==='mutation').length,0);
});
test('known unsupported RPC is reported exactly and never retried',async t=>{
 const f=fixture(t);let count=0;
 f.io.ensure=async()=>{count++;throw {response:{error:{code:'method_not_found'}}};};
 assert.equal((await openSession(f.env,f.io,f.dir)).reason,'method_not_found');
 assert.equal((await openSession(f.env,f.io,f.dir)).status,'unknown');assert.equal(count,1);
});
test('invalid shortened or unsafe target cannot open anything',async t=>{
 const f=fixture(t);f.env.workspace='active';
 assert.equal((await openSession(f.env,f.io,f.dir)).reason,'invalid_target');assert.equal(f.calls.length,0);
 f.env.workspace=target.workspace;f.env.sessionId='bad\nsession';
 assert.equal((await openSession(f.env,f.io,f.dir)).reason,'invalid_sessionId');assert.equal(f.calls.length,0);
});

test('host refusal for a conversation owned by chat never reports success',async t=>{
 const f=fixture(t);const ensure=f.io.ensure;
 f.io.ensure=async p=>{const r=await ensure(p);r.result.agentResumeRefused='chat-writing';return r;};
 assert.equal((await openSession(f.env,f.io,f.dir)).reason,'saved_resume_refused');
 assert.ok(!f.calls.some(c=>c[1]==='switch'));
});

test('real terminal show omits provider identity; tab status proves repeat without duplication',async t=>{
 const f=fixture(t,{existing:true});
 assert.equal((await openSession(f.env,f.io,f.dir)).status,'opened');
 assert.equal((await openSession(f.env,f.io,f.dir)).status,'opened');
 assert.equal(f.calls.filter(c=>c[0]==='mutation').length,0);
});
test('wrong provider proof in tab status blocks navigation',async t=>{
 const f=fixture(t);const tabs=f.io.tabs;
 f.io.tabs=async()=>{const r=await tabs();r.result.tabs[0].agentStatus.providerSession.id='other';return r;};
 assert.equal((await openSession(f.env,f.io,f.dir)).reason,'resume_identity_unverified');
});

test('launch envelope may omit resume facts while live tabs prove exact identity',async t=>{
 const f=fixture(t);const ensure=f.io.ensure;
 f.io.ensure=async p=>{const r=await ensure(p);delete r.result.resumeProviderSession;r.result.terminal={handle:'term-1'};return r;};
 assert.equal((await openSession(f.env,f.io,f.dir)).status,'opened');
});
