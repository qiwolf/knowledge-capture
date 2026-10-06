import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {endpointOf,captureURL,statusText,apiRequest,jobDetails} from '../core.js';
test('only explicit safe backend origins',()=>{
 assert.equal(endpointOf('http://127.0.0.1:8765'),'http://127.0.0.1:8765');
 for(const value of ['http://remote.test','https://u:p@remote.test','https://remote.test/path','https://remote.test?token=x','file:///tmp/a']) assert.throws(()=>endpointOf(value));
 assert.equal(endpointOf('https://remote.test/'),'https://remote.test');
});
test('reject browser internals and URL credentials',()=>{
 for(const value of ['chrome://settings','javascript:alert(1)','https://u:p@test.org']) assert.throws(()=>captureURL(value));
 assert.equal(captureURL('https://example.org/a'),'https://example.org/a');
});
test('accepted is not completion and auth not in body', async()=>{
 let seen;
 const job=await apiRequest({token:'secret'},'POST','/api/captures',{url:'https://example.org',origin:'user'},async(url,options)=>{seen={url,options};return {ok:true,status:202,json:async()=>({id:'abc',status:'queued'})};});
 assert.equal(job.status,'queued'); assert.match(statusText(job),/等待/);
 assert.equal(seen.options.headers.Authorization,'Bearer secret'); assert.equal(seen.options.redirect,'error'); assert(!seen.options.body.includes('secret'));
});
test('ambiguous POST never retries',async()=>{
 let calls=0; await assert.rejects(apiRequest({token:'x'},'POST','/api/captures',{},async()=>{calls++;throw new Error('timeout');}),/勿立即重复/); assert.equal(calls,1);
});
test('reject malformed, failed authentication, and fake completed POST',async()=>{
 for(const response of [{ok:false,status:401},{ok:true,status:200,json:async()=>({id:'x',status:'complete'})},{ok:true,status:202,json:async()=>({status:'queued'})}]) await assert.rejects(apiRequest({token:'x'},'POST','/api/captures',{},async()=>response));
});
test('MV3 minimal permissions and no embedded scripts',()=>{
 const manifest=JSON.parse(fs.readFileSync(new URL('../manifest.json',import.meta.url)));
 assert.equal(manifest.manifest_version,3); assert.deepEqual(manifest.permissions,['activeTab','storage','contextMenus']); assert(!manifest.host_permissions); assert(!manifest.content_scripts);
 for(const file of ['popup.html','options.html']) {const html=fs.readFileSync(new URL('../'+file,import.meta.url),'utf8'); assert(!/\son\w+=/i.test(html)); assert(!/<script(?![^>]*\bsrc=)[^>]*>/i.test(html));}
});
test('202 accepts task that already advanced before response',async()=>{
 for(const status of ['running','complete','partial','failed']) {
  const job=await apiRequest({token:'x'},'POST','/api/captures',{},async()=>({ok:true,status:202,json:async()=>({id:'fast',status})}));
  assert.equal(job.status,status);
 }
});
test('safe job errors and warnings retained without copying arbitrary result',async()=>{
 const job=await apiRequest({token:'x'},'GET','/api/captures/x',undefined,async()=>({ok:true,status:200,json:async()=>({id:'x',status:'partial',error:{code:'denied',message:'无法采集',internal:'private'},result:{warnings:['图片缺失',{bad:true}],secret:'private'}})}));
 assert.deepEqual(job.error,{code:'denied',message:'无法采集'});
 assert.deepEqual(job.result,{warnings:['图片缺失']});
 const {jobDetails}=await import('../core.js'); assert.match(jobDetails(job),/图片缺失/);assert.match(jobDetails(job),/无法采集/);
});

test('preserve safe pipeline stage state without model content',async()=>{
 const fetcher=async()=>({ok:true,status:200,json:async()=>({id:'pipeline',status:'partial',result:{analysis:{status:'failed',secret:'private'},wiki:{status:'skipped',reason:'整理失败'},alerts:{status:'skipped'},privateBody:'private'}})});
 const job=await apiRequest({endpoint:'http://127.0.0.1:8765',token:'test'},'GET','/api/captures/pipeline',undefined,fetcher);
 assert.match(jobDetails(job),/AI 整理：失败/);
 assert.match(jobDetails(job),/Wiki 更新：未执行（整理失败）/);
 assert.equal(JSON.stringify(job).includes('private'),false);
});

test('inference is visible, sanitized, and prevents a false overall success',async()=>{
 for(const error of [{error_code:'invalid_citation',error:'SECRET raw model output'}, {error:{code:'invalid_citation',message:'SECRET raw model output'}}]) {
  const job=await apiRequest({token:'x'},'GET','/api/captures/x',undefined,async()=>({ok:true,status:200,json:async()=>({id:'x',status:'complete',result:{interest_inference:{status:'failed',...error,reason:'SECRET',model_json:'SECRET'}}})}));
  assert.match(jobDetails(job),/兴趣归纳：失败.*引用校验/);
  assert.match(statusText(job),/部分完成/);
  assert(!JSON.stringify(job).includes('SECRET'));
 }
 const job=await apiRequest({token:'x'},'GET','/api/captures/x',undefined,async()=>({ok:true,status:200,json:async()=>({id:'x',status:'complete',result:{interest_inference:{status:'insufficient_sources'}}})}));
 assert.match(jobDetails(job),/独立资料不足/);
});
