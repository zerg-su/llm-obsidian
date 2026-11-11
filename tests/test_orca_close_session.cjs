'use strict';
const {test}=require('node:test'),assert=require('node:assert/strict');
const {closeChat}=require('../scripts/orca-close-session.cjs');
const target={turn_end:{status:'clean'},runtimeId:'runtime',workspace:'identity:fixture',workspaceId:'workspace',sessionId:'codex_test_session'};
function fixture(options={}) {
 let tabs=[{id:'exact-tab',type:'agent-session',sessionId:target.sessionId},{id:'neighbor',type:'agent-session',sessionId:'another-session'}],calls=[];
 return {calls,io:{compatible:async()=>{},list:async()=>({ok:true,_meta:{runtimeId:options.wrongRuntime?'other':'runtime'},result:{worktree:'workspace',tabs}}),
 close:async p=>{calls.push(p);if(!options.refuse)tabs=tabs.filter(t=>t.id!==p.tabId);if(options.lost)throw Error('lost');return {ok:true,_meta:{runtimeId:'runtime'},result:{refused:!!options.refuse}};}}};
}
test('closes exact chat and proves neighbor remains',async()=>{const f=fixture();const r=await closeChat(target,f.io);assert.equal(r.status,'closed');assert.equal(r.surface_closed,true);assert.deepEqual(f.calls,[{worktree:'identity:fixture',tabId:'exact-tab',reason:'user'}]);});
test('blocked save sends no close',async()=>{const f=fixture();assert.equal((await closeChat({...target,turn_end:{status:'blocked'}},f.io)).status,'blocked');assert.equal(f.calls.length,0);});
test('wrong runtime sends no close',async()=>{const f=fixture({wrongRuntime:true});assert.equal((await closeChat(target,f.io)).status,'manual');assert.equal(f.calls.length,0);});
test('missing exact session cannot close neighbor',async()=>{const f=fixture();assert.equal((await closeChat({...target,sessionId:'missing'},f.io)).status,'manual');assert.equal(f.calls.length,0);});
test('lost reply reconciles absence without retry',async()=>{const f=fixture({lost:true});assert.equal((await closeChat(target,f.io)).status,'closed');assert.equal(f.calls.length,1);});
test('refusal leaves chat open and reports unknown without retry',async()=>{const f=fixture({refuse:true});assert.equal((await closeChat(target,f.io)).status,'unknown');assert.equal(f.calls.length,1);});
