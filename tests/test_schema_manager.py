"""
Unit tests for coordinator/schema_manager.py — GlobalSchema.
"""

import pytest
from schema_manager import GlobalSchema


@pytest.fixture
def fresh_schema():
    """Return a fresh GlobalSchema instance for each test."""
    return GlobalSchema()


def make_worker(worker_id, worker_name, fields=None):
    """Helper to build a worker registration payload."""
    return {
        'worker_id': worker_id,
        'worker_name': worker_name,
        'baseline': {'row_count': 1000, 'scan_latency_ms': 200, 'token_lookup_us': 500},
        'fields': fields or [
            {'logical': 'person_token', 'alias': ['身份证'], 'secret': True, 'tokenize': 'hmac-sha256', 'type': 'token'},
            {'logical': 'gender', 'alias': ['性别'], 'type': 'enum', 'mapping': {'M': '男', 'F': '女'}, 'values': ['男', '女']},
        ],
    }


class TestRegisterWorker:
    def test_register_adds_worker(self, fresh_schema):
        fresh_schema.register_worker(make_worker('w1', 'Test1'))
        assert 'w1' in fresh_schema.workers
        assert fresh_schema.workers['w1']['worker_name'] == 'Test1'

    def test_register_multiple_workers(self, fresh_schema):
        fresh_schema.register_worker(make_worker('w1', 'Test1'))
        fresh_schema.register_worker(make_worker('w2', 'Test2'))
        assert len(fresh_schema.workers) == 2

    def test_register_indexes_fields(self, fresh_schema):
        fresh_schema.register_worker(make_worker('w1', 'Test1'))
        assert 'gender' in fresh_schema.field_index
        assert 'w1' in fresh_schema.field_index['gender']

    def test_register_indexes_aliases(self, fresh_schema):
        fresh_schema.register_worker(make_worker('w1', 'Test1'))
        assert '性别' in fresh_schema.alias_index
        assert fresh_schema.alias_index['性别'] == 'gender'

    def test_register_merges_values(self, fresh_schema):
        fresh_schema.register_worker(make_worker('w1', 'T1', [
            {'logical': 'color', 'type': 'enum', 'mapping': {'R': '红色'}, 'values': ['红色']},
        ]))
        fresh_schema.register_worker(make_worker('w2', 'T2', [
            {'logical': 'color', 'type': 'enum', 'mapping': {'B': '蓝色'}, 'values': ['蓝色']},
        ]))
        assert '红色' in fresh_schema.field_details['color']['values']
        assert '蓝色' in fresh_schema.field_details['color']['values']


class TestFieldLookup:
    def test_get_workers_for_field(self, fresh_schema):
        fresh_schema.register_worker(make_worker('w1', 'T1'))
        assert fresh_schema.get_workers_for_field('gender') == ['w1']

    def test_get_workers_via_alias(self, fresh_schema):
        fresh_schema.register_worker(make_worker('w1', 'T1'))
        assert fresh_schema.get_workers_for_field('性别') == ['w1']

    def test_field_not_found(self, fresh_schema):
        assert fresh_schema.get_workers_for_field('nonexistent') == []

    def test_get_field_by_alias(self, fresh_schema):
        fresh_schema.register_worker(make_worker('w1', 'T1'))
        assert fresh_schema.get_field_by_alias('性别') == 'gender'

    def test_alias_not_found_returns_none(self, fresh_schema):
        assert fresh_schema.get_field_by_alias('不存在的别名') is None


class TestFieldDetails:
    def test_get_field_type(self, fresh_schema):
        fresh_schema.register_worker(make_worker('w1', 'T1'))
        assert fresh_schema.get_field_type('person_token') == 'token'

    def test_get_field_values(self, fresh_schema):
        fresh_schema.register_worker(make_worker('w1', 'T1'))
        assert '男' in fresh_schema.get_field_values('gender')


class TestSummaries:
    def test_get_all_fields_summary(self, fresh_schema):
        fresh_schema.register_worker(make_worker('w1', 'T1'))
        summary = fresh_schema.get_all_fields_summary()
        assert any(f['logical'] == 'gender' for f in summary)
        assert any(f['logical'] == 'person_token' for f in summary)

    def test_get_workers_summary(self, fresh_schema):
        fresh_schema.register_worker(make_worker('w1', 'Test Worker'))
        summary = fresh_schema.get_workers_summary()
        assert 'w1' in summary
        assert summary['w1']['name'] == 'Test Worker'
        assert 'fields' in summary['w1']
        assert 'row_count' in summary['w1']

    def test_to_prompt_text(self, fresh_schema):
        fresh_schema.register_worker(make_worker('w1', 'Test Worker'))
        text = fresh_schema.to_prompt_text()
        assert 'Test Worker' in text
        assert 'w1' in text
        assert 'gender' in text
