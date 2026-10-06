from copy import deepcopy
import json
import pytest
import knowledge_capture.wiki as module
from knowledge_capture.wiki import WikiError
from test_wiki_hierarchy import fixture, LayerModel


class RangeModel(LayerModel):
    identity = {'provider': 'fixture', 'model': 'range-v2'}
    def complete_json(self, prompt, payload):
        if payload.get('stage') == 'reduce':
            self.calls.append(deepcopy(payload))
            ids = list(payload['evidence'])
            result = {'summary': [{'text': payload['evidence'][ids[-1]]['quote'], 'evidence_ids': [ids[-1]]}],
                      'agreements': [], 'differences': [], 'questions': []}
            if self.change:
                self.change(result, payload)
            return result
        result = super().complete_json(prompt, payload)
        for cite in module._citations(result):
            cite.pop('quote')
        return result


def test_default_range_build_cache_and_reduce_ids(tmp_path, monkeypatch):
    wiki, topic, _ = fixture(tmp_path, monkeypatch)
    # ID dictionary has additional overhead; keep multiple leaves and bounded reduction.
    monkeypatch.setattr(module, 'MAX_INPUT_CHARS', 5500)
    model = RangeModel()
    wiki.build(topic['id'], model, protocol='range-v2')
    record = wiki.read(topic['id'])['record']
    assert record['protocol'] == record['coverage']['protocol'] == 'range-v2'
    assert any(p.get('stage') == 'reduce' and p['evidence'] for p in model.calls)
    for p in model.calls:
        if p.get('stage') == 'reduce':
            assert all('evidence_ids' in entry and 'evidence' not in entry
                       for node in p['partials'] for entries in node['result'].values() for entry in entries)
    second = RangeModel()
    wiki.build(topic['id'], second, protocol='range-v2')
    assert all(p['stage'] == 'reduce' for p in second.calls)
    assert wiki.read(topic['id'])['record']['coverage']['cache_hits'] > 0
    assert all(json.loads(p.read_text())['provenance']['protocol'] == 'range-v2'
               for p in (tmp_path / '.cache/wiki/leaves').glob('*.json'))


def test_exact_full_lines_including_fences_and_spaces():
    payload = {'sources': [{'source_id': 's', 'version_id': 'v', 'lines': [
        {'number': 3, 'text': '```'}, {'number': 4, 'text': '  x = 1  '}, {'number': 5, 'text': '```'}]}]}
    cite = {'source_id': 's', 'version_id': 'v', 'start_line': 3, 'end_line': 5}
    assert module._range_citation(cite, payload)['quote'] == '```\n  x = 1  \n```'
    with pytest.raises(WikiError):
        module._range_citation({**cite, 'quote': '``` '}, payload)
    payload['sources'][0]['lines'].pop(1)
    with pytest.raises(WikiError) as exc:
        module._range_citation(cite, payload)
    assert exc.value.code == 'citation_outside_input'


@pytest.mark.parametrize('start,end', [(True, 1), (0, 1), (1, 21), (2, 1)])
def test_invalid_range_no_coercion(start, end):
    with pytest.raises(WikiError):
        module._range_citation({'source_id':'s','version_id':'v','start_line':start,'end_line':end}, {'sources':[]})


def test_reduce_unknown_id_rejected(tmp_path, monkeypatch):
    wiki, topic, _ = fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(module, 'MAX_INPUT_CHARS', 5500)
    def change(result, payload):
        if payload.get('stage') == 'reduce':
            result['summary'][0]['evidence_ids'] = ['e_invented']
    with pytest.raises(WikiError) as exc:
        wiki.build(topic['id'], RangeModel(change), protocol='range-v2')
    assert exc.value.code == 'citation_outside_input'
    assert wiki.list_pages() == []


def test_cache_partial_quote_rejected_even_if_substring_valid(tmp_path, monkeypatch):
    wiki, topic, _ = fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(module, 'MAX_INPUT_CHARS', 5500)
    wiki.build(topic['id'], RangeModel(), protocol='range-v2')
    for path in (tmp_path / '.cache/wiki/leaves').glob('*.json'):
        record = json.loads(path.read_text())
        record['result']['summary'][0]['evidence'][0]['quote'] = record['result']['summary'][0]['evidence'][0]['quote'][:6]
        record['result_hash'] = module.digest(module._canonical(record['result']))
        path.write_text(json.dumps(record))
    with pytest.raises(WikiError) as exc:
        wiki.build(topic['id'], RangeModel(), protocol='range-v2')
    assert exc.value.code == 'invalid_cache'


def test_single_default_range(tmp_path, monkeypatch):
    wiki, topic, _ = fixture(tmp_path, monkeypatch, count=1)
    monkeypatch.setattr(module, 'MAX_INPUT_CHARS', 60000)
    model = RangeModel()
    wiki.build(topic['id'], model, protocol='range-v2')
    assert len(model.calls) == 1
    assert wiki.read(topic['id'])['record']['protocol'] == 'range-v2'


@pytest.mark.parametrize('text', ['abc', 'x' * 4001])
def test_range_never_truncates_full_lines(text):
    payload = {'sources': [{'source_id': 's', 'version_id': 'v', 'lines': [{'number': 1, 'text': text}]}]}
    with pytest.raises(WikiError) as exc:
        module._range_citation({'source_id': 's', 'version_id': 'v', 'start_line': 1, 'end_line': 1}, payload)
    assert exc.value.code == 'invalid_citation'
