"""
Test fixtures shared across all test modules.
"""

import os
import sys
import tempfile
import pytest
import yaml

# Ensure engine and coordinator directories are importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'engine'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'coordinator'))


SAMPLE_MAPPING = {
    'worker_id': 'worker_test',
    'worker_name': 'Test Worker',
    'table': 'test_table',
    'fields': [
        {
            'logical': 'gender',
            'physical': 'gender_code',
            'alias': ['性别', 'sex'],
            'mapping': {'M': '男', 'F': '女'},
        },
        {
            'logical': 'research_field',
            'physical': 'field_code',
            'alias': ['研究方向', '专业领域'],
            'mapping': {
                '01': '物联网',
                '02': '人工智能',
                '03': '新材料',
                '04': '生物医药',
                '05': '量子计算',
            },
        },
        {
            'logical': 'age',
            'physical': 'birth_year',
            'alias': ['年龄', '岁数'],
            'derived': True,
            'derive_expr': '(2025 - birth_year)',
        },
        {
            'logical': 'person_token',
            'physical': 'id_card',
            'secret': True,
            'tokenize': 'hmac-sha256',
        },
        {
            'logical': 'monthly_income',
            'physical': 'monthly_income',
            'alias': ['月收入', '月工资', '收入'],
        },
    ],
}


@pytest.fixture
def sample_mapping():
    """Return a copy of the sample mapping for testing."""
    import copy
    return copy.deepcopy(SAMPLE_MAPPING)


@pytest.fixture
def temp_mapping_file(monkeypatch, sample_mapping):
    """Create a temporary mapping.yaml and point MAPPING_FILE env var at it."""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False, encoding='utf-8') as f:
        yaml.dump(sample_mapping, f, allow_unicode=True)
        tmp_path = f.name

    monkeypatch.setenv('MAPPING_FILE', tmp_path)
    monkeypatch.setenv('SALT', 'test-salt-for-sql-builder')

    # Reset the mapping cache in sql_builder
    import sql_builder
    sql_builder._MappingCache.invalidate()

    yield tmp_path

    # Cleanup
    try:
        os.unlink(tmp_path)
    except Exception:
        pass
    sql_builder._MappingCache.invalidate()