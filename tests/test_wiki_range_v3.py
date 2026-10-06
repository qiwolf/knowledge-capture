import pytest
import knowledge_capture.wiki as m
from test_wiki_hierarchy import fixture
from test_wiki_range import RangeModel


def payload(lines):
    return {'sources': [{'source_id': 's', 'version_id': 'v', 'lines': [{'number': i, 'text': line} for i, line in enumerate(lines, 1)]}]}


def cite(end):
    return {'source_id':'s', 'version_id':'v', 'start_line':1, 'end_line':end}


def covered(children):
    return {i for c in children for i in range(c['start_line'], c['end_line']+1)}


def test_22_line_example_split_without_loss():
    sent = payload(['代码示例'+str(i) for i in range(22)])
    children = m._materialize_range(cite(22), sent)
    assert [(c['start_line'],c['end_line']) for c in children] == [(1,20),(21,22)]
    assert covered(children) == set(range(1,23))


def test_80_and_81_boundaries():
    assert len(m._materialize_range(cite(80),payload(['abcd']*80))) == 4
    with pytest.raises(m.WikiError):
        m._materialize_range(cite(81),payload(['abcd']*81))


def test_short_tail_overlaps_left_without_losing_blank_line():
    children = m._materialize_range(cite(21), payload(['abcd']*20+['']))
    assert children[-1]['start_line'] == 20 and children[-1]['quote'] == 'abcd\n'
    assert covered(children) == set(range(1,22))


def test_blank_impossible_and_missing_and_wrong_version():
    with pytest.raises(m.WikiError):
        m._materialize_range(cite(4),payload(['']*4))
    sent = payload(['abcd']*22)
    sent['sources'][0]['lines'].pop(8)
    with pytest.raises(m.WikiError) as exc:
        m._materialize_range(cite(22),sent)
    assert exc.value.code == 'citation_outside_input'
    with pytest.raises(m.WikiError) as exc:
        m._materialize_range({**cite(22),'version_id':'wrong'},payload(['abcd']*22))
    assert exc.value.code == 'citation_outside_input'


def test_no_truncation_of_character_or_final_citation_limits():
    with pytest.raises(m.WikiError):
        m._materialize_range(cite(80),payload(['x'*60]*80))
    sent = payload(['abcd']*80)
    raw = {'summary':[{'text':'说明', 'evidence':[cite(80)]*7}], 'agreements':[], 'differences':[], 'questions':[]}
    result = m._decode_ranges(raw,sent,'range-v3')
    assert len(result['summary'][0]['evidence']) == 28
    with pytest.raises(m.WikiError) as exc:
        m.validate(result, {'s':{'markdown':'\n'.join(['abcd']*80),'metadata':{'version_id':'v'}}})
    assert exc.value.code == 'invalid_citation'


def test_default_build_v3_and_explicit_v2_cache_separation(tmp_path, monkeypatch):
    wiki, topic, _ = fixture(tmp_path,monkeypatch)
    monkeypatch.setattr(m,'MAX_INPUT_CHARS',5500)
    wiki.build(topic['id'],RangeModel(),protocol='range-v2')
    wiki.build(topic['id'],RangeModel())
    record = wiki.read(topic['id'])['record']
    assert record['protocol'] == 'range-v3'
    assert record['coverage']['citation_materialization'] == 'lossless_bounded_split'
    assert record['coverage']['cache_hits'] == 0
    assert all(c['cache_provenance']['schema'] == 3 for c in record['coverage']['leaf_chunks'])
    wiki.build(topic['id'],RangeModel())
    assert wiki.read(topic['id'])['record']['coverage']['cache_hits'] > 0


def test_twenty_blank_lines_rejected_and_blank_tail_requires_meaningful_overlap():
    with pytest.raises(m.WikiError):
        m._materialize_range(cite(20), payload(['']*20))
    children = m._materialize_range(cite(39), payload(['abcd']*20+['']*19))
    assert children[-1]['start_line'] == 20
    assert children[-1]['end_line'] == 39
    assert children[-1]['quote'].strip() == 'abcd'
    assert covered(children) == set(range(1,40))
    with pytest.raises(m.WikiError):
        m._materialize_range(cite(40), payload(['abcd']*20+['']*20))
