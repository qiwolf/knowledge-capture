import json
import stat
import pytest
from knowledge_capture.engine_content import EngineContentNormalizer, ContentNormalizationError


class Model:
    identity = {'model':'fixture'}
    def __init__(self, choose=None): self.calls=[]; self.choose=choose
    def complete_json(self,prompt,payload):
        self.calls.append(payload)
        if self.choose: return self.choose(payload)
        units=payload['units']
        ref=lambda u: {'unit_id':u['id'],'start':0,'end':len(u['text'])}
        return {'classification':'article','title':ref(units[0]),'body':[ref(u) for u in units[1:]],'results':[],'images':[]}


def run(tmp_path,response,model=None,**kw):
    return EngineContentNormalizer(model or Model()).normalize(response,purpose='capture',archive_dir=tmp_path,**kw)


def test_nested_json_without_field_mapping_and_archive(tmp_path):
    raw={'unexpected':{'heading':'原始标题','nested':[{'content':'真实内容与条件，模型不能新增事实。'}]}}
    result=run(tmp_path,raw)
    assert result['title']=='原始标题' and result['text']=='真实内容与条件，模型不能新增事实。'
    path=result['provenance']['raw_path']
    assert json.loads(open(path).read())==raw
    assert stat.S_IMODE(__import__('os').stat(path).st_mode)==0o600
    assert result['provenance']['coverage']['all_units_sent']


@pytest.mark.parametrize('response',[{'job':{'status':'queued'}},{'error':'bad gateway'},'<html><input type="password"></html>','<html><div class="g-recaptcha">验证</div></html>','processing'])
def test_blocked_never_called_or_archived_as_body(tmp_path,response):
    model=Model()
    with pytest.raises(ContentNormalizationError): run(tmp_path,response,model)
    assert not model.calls
    assert list(tmp_path.rglob('response.txt'))
    assert not list(tmp_path.rglob('result.json'))


def test_no_invented_text_or_url(tmp_path):
    def choose(p): return {'classification':'article','title':{'unit_id':'u1','start':0,'end':4},'body':[{'unit_id':'invented','start':0,'end':10}],'results':[],'images':[]}
    with pytest.raises(ContentNormalizationError):run(tmp_path,{'title':'真实标题','body':'真实正文内容'},Model(choose))


def test_html_search_and_images_are_existing_candidates(tmp_path):
    def choose(p):
        title=next(u for u in p['units'] if u['text']=='实际标题')
        ref={'unit_id':title['id'],'start':0,'end':4}
        return {'classification':'search','title':None,'body':[],'images':[],'results':[{'title':ref,'url_id':p['links'][0]['id'],'description':None}]}
    result=EngineContentNormalizer(Model(choose)).normalize('<a href="/real">实际标题</a>',purpose='search',source_url='https://example.org/query',archive_dir=tmp_path)
    assert result['results']==[{'title':'实际标题','url':'https://example.org/real','description':''}]


def test_long_response_all_units_sent_no_silent_prefix(tmp_path):
    model=Model()
    result=run(tmp_path,{'title':'测试标题','body':'完整原文内容。'*35000},model)
    assert len(model.calls)>1
    all_ids={u['id'] for p in model.calls for u in p['units']}
    assert len(all_ids)==result['provenance']['coverage']['unit_count']
    assert result['provenance']['coverage']['chunk_count']==len(model.calls)
    assert '完整原文内容' in result['text']


def test_credentials_rejected_before_archive(tmp_path):
    with pytest.raises(ContentNormalizationError) as exc:run(tmp_path,{'access_token':'secret','body':'内容'})
    assert exc.value.code=='sensitive_response'
    assert not list(tmp_path.rglob('response.txt'))


def test_mutating_model_cannot_create_source(tmp_path):
    def choose(p):
        p['units'][1]['text']='篡改正文'
        return {'classification':'article','title':{'unit_id':'u1','start':0,'end':4},'body':[{'unit_id':'u2','start':0,'end':4}],'results':[],'images':[]}
    result=run(tmp_path,{'a':'真实标题','b':'原文内容'},Model(choose))
    assert result['text']=='原文内容'


def test_llm_rejects_unknown_video_without_transcript(tmp_path):
    def choose(p):return {'classification':'unknown','title':None,'body':[],'results':[],'images':[]}
    with pytest.raises(ContentNormalizationError):run(tmp_path,{'video':'https://example.org/video'},Model(choose))


def test_html_nested_inline_and_tail_keep_document_order_and_drop_script():
    normalizer = EngineContentNormalizer(None)
    units, links = normalizer._inventory('<div><p>甲<strong>乙<em>丙</em>丁</strong>戊</p>己<script>恶意脚本</script><p>庚</p></div>', 'https://example.org/a')
    assert ''.join(unit['text'] for unit in units) == '甲乙丙丁戊己庚'
    assert all('恶意脚本' not in unit['text'] for unit in units)


def test_markdown_destinations_have_correct_roles_and_balanced_urls():
    value = '![图](https://cdn.example/render?id=1)\n[来源](https://example.org/wiki/Test_(x))\n![相对图](<../pic.jpg>)'
    _, links = EngineContentNormalizer(None)._inventory(value, 'https://example.org/dir/article')
    assert [(link['url'],link['role']) for link in links] == [
        ('https://cdn.example/render?id=1','image'),
        ('https://example.org/wiki/Test_(x)','link'),
        ('https://example.org/pic.jpg','image')]


def test_final_metadata_url_drives_relative_lazy_image_resolution(tmp_path):
    def choose(payload):
        title = next(unit for unit in payload['units'] if unit['text'] == '真实标题')
        body = next(unit for unit in payload['units'] if unit['text'] == '真实正文')
        ref = lambda unit: {'unit_id':unit['id'],'start':0,'end':len(unit['text'])}
        return {'classification':'article','title':ref(title),'body':[ref(body)],'results':[],
                'images':[link['id'] for link in payload['links'] if link['role']=='image']}
    response = {'metadata': {'finalURL':'https://publisher.example/dir/article', 'sourceURL':'https://short.example/link'},
                'html':'<html><title>真实标题</title><p>真实正文</p><img data-src="pic.jpg"></html>'}
    result = run(tmp_path,response,Model(choose),source_url='https://short.example/link')
    assert result['final_url'] == 'https://publisher.example/dir/article'
    assert result['images'][0]['url'] == 'https://publisher.example/dir/pic.jpg'
    assert result['provenance']['source_url'] == 'https://short.example/link'


@pytest.mark.parametrize('response', [
    {'success':False,'title':'服务状态','body':'任务失败提示不能作为正文'},
    {'result':{'ok':False,'body':'任务失败'}},
    {'metadata':{'statusCode':403},'body':'访问拒绝'},
    {'status_code':202,'body':'已接受任务'},
])
def test_explicit_failure_flags_reject_before_model(tmp_path,response):
    model = Model()
    with pytest.raises(ContentNormalizationError):
        run(tmp_path,response,model)
    assert not model.calls


@pytest.mark.parametrize('response', [
    '<html><title>登录系统建设方案</title><body><p>这是介绍统一身份认证系统建设的正常中文文章。</p></body></html>',
    '<html><title>验证码组件技术分析</title><body><p>g-recaptcha 是示例组件名称，本文分析其机制。</p></body></html>',
    {'title':'系统错误状态说明','sections':[{'status':'error','description':'这表示业务任务失败，并非文章采集失败。'}]},
])
def test_ordinary_chinese_articles_about_login_or_errors_are_not_blocked(tmp_path,response):
    result = run(tmp_path,response)
    assert result['kind'] == 'article'
    assert result['text']


@pytest.mark.parametrize('response', [
    {'text':'{"success":false,"title":"服务状态","body":"尚未完成"}'},
    {'content':[{'type':'text','text':'{"status":"queued","message":"任务排队中"}'}]},
])
def test_json_as_transport_text_still_enforces_failure_state(tmp_path,response):
    model=Model()
    with pytest.raises(ContentNormalizationError):
        run(tmp_path,response,model)
    assert not model.calls


def test_semantic_review_boundary_is_metadata_not_technical_partial(tmp_path):
    result=run(tmp_path,{'title':'真实标题','body':'完整的正常正文'})
    assert result['warnings'] == []
    assert result['provenance']['semantic_verification'] is False
    assert result['provenance']['review_notes']
    assert result['provenance']['coverage']['selection_omission_possible'] is True


def test_final_url_metadata_inside_mcp_json_text_is_preserved():
    raw={'content':[{'type':'text','text':json.dumps({'metadata':{'finalURL':'https://publisher.example/real'}})}]}
    assert EngineContentNormalizer._final_url(raw,'https://short.example/a') == 'https://publisher.example/real'
