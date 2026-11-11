#!/usr/bin/env node
'use strict';
const fs=require('node:fs'),path=require('node:path');
const {resolveCli,runtimeRoot,jsonCommand}=require('./orca-open-session.cjs');
async function closeChat(t,io) {
  if(t.turn_end?.status!=='clean') return {status:'blocked',reason:'turn_end_not_clean',surface_closed:false};
  const read=async()=>{
    const r=await io.list();
    if(r.ok!==true||r._meta?.runtimeId!==t.runtimeId||r.result?.worktree!==t.workspaceId||!Array.isArray(r.result.tabs)) throw Error('chat_inventory_mismatch');
    return r.result.tabs;
  };
  try {
    await io.compatible();
    const before=await read(),matches=before.filter(x=>x.type==='agent-session'&&x.sessionId===t.sessionId);
    if(matches.length!==1) return {status:'manual',reason:'exact_chat_tab_unavailable',surface_closed:false};
    const selected=matches[0];
    let error;
    try {const r=await io.close({worktree:t.workspace,tabId:selected.id,reason:'user'});
      if(r.ok!==true||r._meta?.runtimeId!==t.runtimeId||r.result?.refused) error=r.result?.refusalReason||'close_refused';
    } catch {error='close_outcome_unknown';}
    const after=await read();
    if(after.some(x=>x.id===selected.id)) return {status:error?'unknown':'manual',reason:error||'chat_still_open',surface_closed:false};
    if(before.some(x=>x.id!==selected.id&&!after.some(y=>y.id===x.id))) return {status:'blocked',reason:'neighbor_inventory_changed',surface_closed:true};
    return {status:'closed',session_id:t.sessionId,surface_closed:true,reason:'exact_chat_tab_absent_neighbors_preserved'};
  } catch(e) {return {status:'manual',reason:e.code||e.message,surface_closed:false};}
}
async function main() {
  const input=JSON.parse(fs.readFileSync(0,'utf8'));
  if(input.turn_end?.status!=='clean') return {status:'blocked',reason:'turn_end_not_clean',surface_closed:false};
  if(!/^[A-Za-z0-9_-]{8,128}$/.test(input.sessionId||'')) throw Error('exact_chat_session_unavailable');
  if(!input.vault||!path.isAbsolute(input.vault)) throw Error('absolute_vault_required');
  if(!process.env.ORCA_ENVIRONMENT&&(process.env.ORCA_PAIRING_CODE||process.env.ORCA_REMOTE_PAIRING)) throw Error('explicit_environment_required');
  const environment=process.env.ORCA_ENVIRONMENT||'local';
  const cli=resolveCli(),root=runtimeRoot(cli);
  const w=jsonCommand(cli,['worktree','show','--environment',environment,'--worktree','path:'+input.vault,'--json']);
  const record=w.result.worktree,address=record.address;
  if(!address||record.path!==fs.realpathSync(input.vault)) throw Error('workspace_mismatch');
  const {RuntimeClient}=require(path.join(root,'cli/runtime-client.js'));
  const {CloseTab,WorktreeTabSelector}=require(path.join(root,'shared/rpc-contract/session-tabs-schemas-params.js'));
  const {readOrcaCliVersion}=require(path.join(root,'cli/cli-version.js'));
  const client=new RuntimeClient(undefined,15000,null,address.environment==='local'?null:address.environment,cli);
  return closeChat({...input,workspace:address.worktree,workspaceId:record.id,runtimeId:w._meta.runtimeId},{
    compatible:async()=>{
      if(!CloseTab.safeParse({worktree:address.worktree,tabId:'compatibility-only',reason:'user'}).success||!WorktreeTabSelector.safeParse({worktree:address.worktree}).success) throw Error('unsupported_close_schema');
      const status=await client.call('status.get');
      if(status._meta?.runtimeId!==w._meta.runtimeId||status.result.appVersion!==readOrcaCliVersion(path.join(root,'cli'))) throw Error('unsupported_close_runtime');
    },
    list:()=>client.call('session.tabs.list',{worktree:address.worktree}),
    close:params=>client.call('session.tabs.close',params)
  });
}
module.exports={closeChat};
if(require.main===module) main().then(r=>console.log(JSON.stringify(r))).catch(e=>{console.log(JSON.stringify({status:'manual',reason:e.code||e.message,surface_closed:false}));});
