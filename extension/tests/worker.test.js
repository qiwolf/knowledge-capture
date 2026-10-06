import test from 'node:test';
import assert from 'node:assert/strict';
let listener, menuListener;
const state={endpoint:'http://127.0.0.1:8765',token:'local-secret'};
let allow=true, requests=[];
globalThis.chrome={
 runtime:{id:'extension-id',getURL:p=>'chrome-extension://extension-id/'+p,onInstalled:{addListener(){}},onMessage:{addListener(fn){listener=fn;}}},
 contextMenus:{onClicked:{addListener(fn){menuListener=fn;}}},
 permissions:{contains:async()=>allow},
 storage:{local:{get:async()=>({...state}),set:async data=>Object.assign(state,data)}},
 action:{setBadgeText:async()=>{},setBadgeBackgroundColor:async()=>{}}
};
globalThis.fetch=async(url,options)=>{requests.push({url,options}); return {ok:true,status:options.method==='POST'?202:200,json:async()=>({id:'task-1',status:options.method==='POST'?'queued':'complete'})};};
await import('../worker.js');
const sender={id:'extension-id',url:'chrome-extension://extension-id/popup.html'};
function message(data){return new Promise(resolve=>listener(data,sender,resolve));}
test('worker submits and persists only after accepted, then refreshes',async()=>{
 const response=await message({type:'collect',url:'https://example.org/article',note:'研究'});
 assert.equal(response.ok,true); assert.equal(response.job.status,'queued'); assert.equal(state.lastJob.id,'task-1');
 assert.equal(JSON.parse(requests[0].options.body).origin,'user');
 const refreshed=await message({type:'refresh'});assert.equal(refreshed.job.status,'complete');
});
test('worker checks granted backend permission before sending',async()=>{
 allow=false;const before=requests.length; const result=await message({type:'collect',url:'https://example.org',note:''});assert.equal(result.ok,false);assert.equal(requests.length,before);allow=true;
});
test('untrusted page sender cannot trigger worker',()=>{
 assert.equal(listener({type:'collect',url:'https://example.org'},{id:'extension-id',url:'https://example.org'},()=>assert.fail()),false);
});
test('right click failure persists actionable error',async()=>{
 menuListener({menuItemId:'collect-link',linkUrl:'javascript:alert(1)'});
 await new Promise(resolve=>setTimeout(resolve,0));assert.match(state.lastError,/只能采集/);
});
