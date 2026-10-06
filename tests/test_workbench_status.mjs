import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const app=fs.readFileSync(new URL('../knowledge_capture/web/app.js',import.meta.url),'utf8');
const helper=app.slice(app.indexOf('function inferenceDetail('),app.indexOf('async function inboxView('));
function node(tag,text=''){return {tag,text,children:[],append(...children){this.children.push(...children);}};}
function append(parent,...children){parent.append(...children);return parent;}
const context={node,append,labels:{complete:'已完成',partial:'部分完成',failed:'失败',skipped:'未执行'}};
vm.runInNewContext(helper+'\nthis.outcome=analysisOutcome;this.details=detailList;this.jobOutcome=jobOutcome;',context);
const content=n=>(n.text||'')+n.children.map(content).join('');
test('manual analysis retains success but independently reports failed inference',()=>{
 for(const error of [{error_code:'invalid_citation',error:'SECRET raw model output'},{error:{code:'invalid_citation',message:'SECRET raw model output'}}]) {
  const result={status:'complete',interest_inference:{status:'failed',...error,reason:'SECRET'}};
  const message=context.outcome(result);
  assert.match(message,/AI 整理已保存；兴趣归纳：失败/);
  assert(!message.includes('整理已完成'));
  assert(!message.includes('SECRET'));
  const details=content(context.details({id:'job',result}));
  assert.match(details,/兴趣归纳失败.*引用校验/);
  assert(!details.includes('SECRET'));
 }
 assert.match(context.outcome({status:'complete',interest_inference:{status:'insufficient_sources'}}),/资料不足/);
 assert.match(context.outcome({status:'complete',interest_inference:{status:'skipped'}}),/未执行/);
});

for(const [stage,label] of [['wiki','Wiki 更新'],['alerts','背景关联']]) {
 test(`pipeline ${stage} failure preserves successful analysis and marks warning`,()=>{
  const result={status:'partial',capture:{status:'complete'},analysis:{status:'complete'},interest_inference:{status:'complete'},wiki:{status:'complete'},alerts:{status:'complete'}};
  result[stage]={status:'failed',error:{message:'SECRET raw model'}};
  const outcome=context.jobOutcome({status:'partial',result});
  assert.equal(outcome.warning,true);
  assert.match(outcome.text,/本次处理：部分完成/);
  assert.match(outcome.text,/AI 整理：已完成/);
  assert.match(outcome.text,/兴趣归纳：已完成/);
  assert(outcome.text.includes(label+'：失败'));
  assert(!outcome.text.includes('AI 整理部分完成'));
  assert(!outcome.text.includes('SECRET'));
 });
}
