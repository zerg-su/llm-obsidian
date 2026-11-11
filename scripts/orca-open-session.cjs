#!/usr/bin/env node
'use strict';
// Internal Orca API bridge. No shell interpolation, prompts, launch settings or retries.
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const crypto = require('node:crypto');
const {spawnSync} = require('node:child_process');
const fail = code => {throw new Error(code);};
function validateTarget(t) {
  for(const key of ['environment','workspace','workspaceId','host','runtimeId','sessionId']) {
    if(typeof t[key]!=='string'||!t[key]||t[key]!==t[key].trim()||/[\x00-\x1f\x7f]/.test(t[key])) fail(`invalid_${key}`);
  }
  if(!['claude','codex'].includes(t.agent)||! /^(identity:|id:|folder:)/.test(t.workspace)) fail('invalid_target');
  if(t.sessionId.startsWith('-')||t.sessionId.length>512) fail('invalid_sessionId');
}
function envelope(r,t) {
  if(r?.ok!==true) fail(r?.error?.code||'invalid_response');
  if(r?._meta?.runtimeId!==t.runtimeId) fail('runtime_mismatch');
  return r.result;
}
function sameSession(p,t) {return p?.key==='session_id'&&p.id===t.sessionId;}
function sameTerminal(x,t) {
  return x?.worktreeId===t.workspaceId && x.executionHostId===t.host && x.agentIdentity===t.agent;
}
function atomicWrite(file,value) {
  const temp=`${file}.${crypto.randomUUID()}.tmp`;
  fs.writeFileSync(temp,JSON.stringify(value)+'\n',{mode:0o600,flag:'wx'});
  const fd=fs.openSync(temp,'r');try{fs.fsyncSync(fd);}finally{fs.closeSync(fd);}
  fs.renameSync(temp,file);
  const parent=fs.openSync(path.dirname(file),'r');try{fs.fsyncSync(parent);}finally{fs.closeSync(parent);}
}
async function openSession(t,io,receiptDir) {
  let lock,receiptFile;
  try {
    validateTarget(t);
    fs.mkdirSync(receiptDir,{recursive:true,mode:0o700});
    const key=crypto.createHash('sha256').update(JSON.stringify([t.environment,t.workspace,t.host,t.agent,t.sessionId])).digest('hex');
    receiptFile=path.join(receiptDir,key+'.json');lock=receiptFile+'.lock';
    try{fs.writeFileSync(lock,'opening\n',{flag:'wx',mode:0o600});}catch{lock=null;fail('operation_busy_or_stale_lock');}
    const cli=async args=>envelope(await io.cli([...args,'--environment',t.environment,'--json']),t);
    const w=(await cli(['worktree','show','--worktree',t.workspace])).worktree;
    if(w?.id!==t.workspaceId||w.hostId!==t.host||w.identity?.executionHostId!==t.host||
       w.address?.environment!==t.environment||w.address.worktree!==t.workspace) fail('workspace_mismatch');
    const listing=await cli(['terminal','list','--worktree',t.workspace,'--limit','10000']);
    if(listing.truncated||!Array.isArray(listing.terminals)) fail('inventory_incomplete');
    const tabState=envelope(await io.tabs(t.workspace),t);
    if(tabState.worktree!==t.workspaceId||!Array.isArray(tabState.tabs)) fail('tab_inventory_mismatch');
    for(const terminal of listing.terminals) {
      const bound=tabState.tabs.filter(tab=>tab.type==='terminal'&&tab.terminal===terminal.handle&&
        tab.agentStatus?.worktreeId===t.workspaceId&&tab.agentStatus?.agentType===t.agent&&
        tab.agentStatus?.tabId===terminal.tabId);
      if(bound.length===1) terminal.resumeProviderSession=bound[0].agentStatus.providerSession;
    }
    const matches=listing.terminals.filter(x=>sameTerminal(x,t)&&sameSession(x.resumeProviderSession,t)&&x.connected===true);
    if(matches.length>1) fail('multiple_open_sessions');
    let handle=matches[0]?.handle;
    let receipt=fs.existsSync(receiptFile)?JSON.parse(fs.readFileSync(receiptFile,'utf8')):null;
    // Reconcile known handles even when terminal.list omits additive resume metadata.
    if(!handle&&receipt?.handle) {
      const shown=listing.terminals.find(x=>x.handle===receipt.handle);
      if(sameTerminal(shown,t)&&sameSession(shown.resumeProviderSession,t)&&shown.connected===true) handle=shown.handle;
    }
    if(!handle) {
      if(receipt) return {status:'unknown',reason:'previous_mutation_requires_read_only_reconciliation',receipt:receiptFile};
      if(listing.terminals.some(x=>x.agentIdentity===t.agent&&!x.resumeProviderSession)) fail('open_session_identity_unavailable');
      await io.checkCompatible(t);
      const params={kind:'explicit',resumeOrigin:'saved',worktree:t.workspace,agent:t.agent,
        providerSession:{key:'session_id',id:t.sessionId},presentation:'focused'};
      // Write-ahead: a crash or lost reply never grants permission to reissue ensure.
      atomicWrite(receiptFile,{status:'unknown',target:t});
      let raw;
      try {raw=await io.ensure(params);} catch(e) {
        const code=e.response?.error?.code;
        return {status:code?'blocked':'unknown',reason:code||'mutation_outcome_unknown_do_not_retry',receipt:receiptFile};
      }
      const result=envelope(raw,t);
      if(result.agentResumeUnavailable||result.agentResumeRefused||result.refusedResume||
         (result.savedResumeDecision&&result.savedResumeDecision.verdict!=='admitted')) fail('saved_resume_refused');
      const resumed=result.resumeProviderSession||result.terminal?.resumeProviderSession;
      if(resumed&&!sameSession(resumed,t)) fail('resume_identity_mismatch');
      // ensure's launch envelope may contain only a handle; show/list owns identity.
      if(!result.terminal?.handle) fail('missing_terminal_handle');
      handle=result.terminal.handle;
      if(typeof handle!=='string'||!handle) fail('missing_terminal_handle');
      atomicWrite(receiptFile,{status:'bound',target:t,handle});
    }
    const shown=(await cli(['terminal','show','--terminal',handle])).terminal;
    if(!sameTerminal(shown,t)||shown.connected!==true) fail('terminal_identity_mismatch');
    let proved=false;
    for(let attempt=0;attempt<20;attempt++) {
      const verified=envelope(await io.tabs(t.workspace),t);
      if(verified.worktree!==t.workspaceId) fail('tab_inventory_mismatch');
      const proof=verified.tabs?.filter(tab=>tab.type==='terminal'&&tab.terminal===handle&&
        tab.agentStatus?.worktreeId===t.workspaceId&&tab.agentStatus?.agentType===t.agent&&
        sameSession(tab.agentStatus.providerSession,t));
      if(proof?.length===1) {proved=true;break;}
      if(io.pause) await io.pause(500);
    }
    if(!proved) fail('resume_identity_unverified');
    const screen=(await cli(['terminal','read','--terminal',handle,'--screen'])).terminal;
    if(screen?.source!=='screen') fail('screen_unavailable');
    const focus=(await cli(['terminal','switch','--terminal',handle])).focus;
    if(focus?.navigated!==true) fail('navigation_unverified');
    atomicWrite(receiptFile,{status:'opened',target:t,handle});
    return {status:'opened',agent:t.agent,sessionId:t.sessionId,environment:t.environment,
      workspace:t.workspace,surface:'terminal',handle,navigated:true,screen,receipt:receiptFile};
  } catch(e) {return {status:'blocked',reason:e.message, ...(receiptFile?{receipt:receiptFile}:{})};}
  finally {if(lock) fs.unlinkSync(lock);}
}
function executable(command) {
  const candidates=command.includes(path.sep)?[command]:(process.env.PATH||'').split(path.delimiter).map(d=>path.join(d,command));
  for(const p of candidates) {try{fs.accessSync(p,fs.constants.X_OK);return fs.realpathSync(p);}catch{}}
  fail('orca_cli_unavailable');
}
function resolveCli() {
  if(process.env.ORCA_CLI_COMMAND) return executable(process.env.ORCA_CLI_COMMAND);
  return executable(process.env.ORCA_DEV_REPO_ROOT?'orca-dev':process.platform==='linux'?'orca-ide':'orca');
}
function runtimeRoot(cli,override) {
  const roots=override?[path.resolve(override)]:[];
  for(let p=path.dirname(cli);;p=path.dirname(p)) {
    roots.push(path.join(p,'app.asar.unpacked','out'),path.join(p,'out'),p);
    if(path.dirname(p)===p) break;
  }
  for(const root of roots) if(fs.existsSync(path.join(root,'cli','runtime-client.js'))) return root;
  fail('unsupported_api_runtime_modules_not_found_use_runtime_root');
}
function jsonCommand(cli,args) {
  const r=spawnSync(cli,args,{encoding:'utf8',timeout:20000,maxBuffer:8*1024*1024});
  if(r.error) fail('cli_transport_unavailable');
  let parsed;try{parsed=JSON.parse(r.stdout);}catch{fail('cli_response_unreadable');}
  if(r.status!==0||parsed.ok===false) fail(parsed.error?.code||'cli_request_failed');
  return parsed;
}
function productionTransport(cli,root,t) {
  const {RuntimeClient}=require(path.join(root,'cli','runtime-client.js'));
  const {ExplicitEnsure}=require(path.join(root,'shared','rpc-contract','agent-session-params.js'));
  const {getAgentResumeArgv}=require(path.join(root,'shared','agent-launch','startup','agent-resume-argv.js'));
  const {readOrcaCliVersion}=require(path.join(root,'cli','cli-version.js'));
  const client=new RuntimeClient(undefined,15000,null,t.environment==='local'?null:t.environment,cli);
  return {
    cli:args=>jsonCommand(cli,args),
    checkCompatible:async()=>{
      if(typeof RuntimeClient!=='function'||typeof client.call!=='function'||!ExplicitEnsure?.safeParse) fail('unsupported_api');
      const params={kind:'explicit',resumeOrigin:'saved',worktree:t.workspace,agent:t.agent,providerSession:{key:'session_id',id:t.sessionId},presentation:'focused'};
      const argv=getAgentResumeArgv(t.agent,params.providerSession);
      const expected=t.agent==='codex'?['codex','resume',t.sessionId]:['claude','--resume',t.sessionId];
      if(!ExplicitEnsure.safeParse(params).success||JSON.stringify(argv)!==JSON.stringify(expected)) fail('unsupported_provider_session_format');
      const status=envelope(await client.call('status.get'),t);
      const version=readOrcaCliVersion(path.join(root,'cli'));
      if(!version||status.appVersion!==version) fail('unsupported_api_host_cli_version_mismatch');
    },
    pause:ms=>new Promise(resolve=>setTimeout(resolve,ms)),
    tabs:worktree=>client.call('session.tabs.list',{worktree}),
    ensure:params=>client.call('terminal.ensureAgentSession',params)
  };
}
async function catalogSessions(environment,sessionId,io) {
  if(!environment||!sessionId||/[\x00-\x1f\x7f]/.test(environment+sessionId)) fail('invalid_catalog_target');
  const raw=await io.list();
  if(raw?.ok!==true||!raw._meta?.runtimeId||!Array.isArray(raw.result?.sessions)) fail('unsupported_catalog_response');
  return {status:'candidates',environment,runtimeId:raw._meta.runtimeId,
    truncated:raw.result.truncated!==false,
    candidates:raw.result.sessions.filter(x=>x.sessionId===sessionId&&['claude','codex'].includes(x.agent)).map(x=>({
      agent:x.agent,sessionId:x.sessionId,title:x.title??null,date:x.updatedAt??x.createdAt??null,
      cwd:x.cwd??null,projectPath:x.projectPath??null,filePath:x.filePath??null,worktreeId:x.worktreeId??null
    }))};
}
function catalogTransport(cli,root,environment) {
  const {RuntimeClient}=require(path.join(root,'cli','runtime-client.js'));
  const {AiVaultListSessionsParams}=require(path.join(root,'shared','rpc-contract','ai-vault-params.js'));
  const {readOrcaCliVersion}=require(path.join(root,'cli','cli-version.js'));
  const client=new RuntimeClient(undefined,15000,null,environment==='local'?null:environment,cli);
  return {list:async()=>{
    const status=await client.call('status.get');
    if(status.ok!==true||status.result.appVersion!==readOrcaCliVersion(path.join(root,'cli'))||
       !status.result.capabilities?.includes('aiVault.v1')) fail('unsupported_catalog_api');
    const params=AiVaultListSessionsParams.parse({limit:2000});
    const result=await client.call('aiVault.listSessions',params);
    if(result._meta?.runtimeId!==status._meta?.runtimeId) fail('runtime_mismatch');
    return result;
  }};
}
async function main(argv) {
  const args={};for(let i=0;i<argv.length;i+=2) {
    if(!['--target','--runtime-root','--receipt-dir','--catalog','--session-id'].includes(argv[i])||!argv[i+1]) fail('usage_target_json_required');
    args[argv[i]]=argv[i+1];
  }
  if(args['--catalog']) {
    const cli=resolveCli(),root=runtimeRoot(cli,args['--runtime-root']);
    console.log(JSON.stringify(await catalogSessions(args['--catalog'],args['--session-id'],catalogTransport(cli,root,args['--catalog']))));
    return 0;
  }
  if(!args['--target']) fail('usage_target_json_required');
  const t=JSON.parse(fs.readFileSync(args['--target'],'utf8'));validateTarget(t);
  const cli=resolveCli();
  // Do not execute a future public resume command through a guessed argv contract.
  const commands=jsonCommand(cli,['agent-context','--json']).commands;
  if(!Array.isArray(commands)) fail('unsupported_cli_command_schema');
  if(commands.some(c=>/^(agent|session|chat) (resume|open)$/.test(c.command))) fail('documented_resume_available_load_guide');
  const io=productionTransport(cli,runtimeRoot(cli,args['--runtime-root']),t);
  const receiptDir=args['--receipt-dir']||path.join(process.env.XDG_CACHE_HOME||path.join(os.homedir(),'.cache'),'llm-obsidian','orca-open-session');
  const result=await openSession(t,io,receiptDir);console.log(JSON.stringify(result));
  return result.status==='opened'?0:1;
}
module.exports={openSession,validateTarget,resolveCli,runtimeRoot,productionTransport,catalogSessions,jsonCommand};
if(require.main===module) main(process.argv.slice(2)).then(code=>{process.exitCode=code;}).catch(e=>{
  console.log(JSON.stringify({status:'blocked',reason:e.code||e.message}));process.exitCode=1;
});
