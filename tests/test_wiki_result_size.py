import pytest
import knowledge_capture.wiki as m


def run_response(count, text='有证据的结论'):
    line = '原文内容' * 800
    documents = {'s': {'metadata': {'version_id': 'v'}, 'markdown': line}}
    payload = {'topic':'主题', 'sources':[{'source_id':'s','version_id':'v','title':'来源','lines':[{'number':1,'text':line}]}]}
    citation = {'source_id':'s','version_id':'v','start_line':1,'end_line':1}
    raw = {'summary':[{'text':text,'evidence':[dict(citation)]} for _ in range(count)],'agreements':[],'differences':[],'questions':[]}
    class Model:
        identity = {'model':'size-fixture'}
        def complete_json(self, prompt, payload):
            return raw
    return raw, lambda: m.synthesize(Model(),payload,documents,protocol='range-v3')


def test_raw_small_materialized_24_to_60k_accepted_without_truncation():
    raw, run = run_response(10)
    assert m._json_size(raw) < 24000
    answer, coverage = run()
    assert 24000 < m._json_size(answer) <= 60000
    assert len(m._citations(answer)) == 10
    assert all(len(c['quote']) == 3200 for c in m._citations(answer))


def test_raw_over_24k_rejected_before_expansion():
    raw, run = run_response(10, '说明'*1250)
    assert m._json_size(raw) > 24000
    with pytest.raises(m.WikiError) as exc:
        run()
    assert exc.value.code == 'intermediate_too_large'
    assert '模型原始JSON' in str(exc.value)


def test_materialized_over_60k_rejected_without_truncation():
    raw, run = run_response(20)
    assert m._json_size(raw) < 24000
    with pytest.raises(m.WikiError) as exc:
        run()
    assert exc.value.code == 'intermediate_too_large'
    assert '原文引用展开结果' in str(exc.value)


def test_quote_v1_preserves_24k_materialized_limit():
    with pytest.raises(m.WikiError):
        m._check_result_size({'value':'x'*25000},'quote-v1',materialized=True)
