"""
Unit tests for engine/sql_builder.py — SQL building from logical predicates.

Covers:
  - WHERE clause building with various operators
  - Field lookup by logical name and alias
  - Value translation (logical → physical)
  - Count query building
  - Empty predicate handling
  - Placeholder generation per DB type
"""

import sys
import os
import pytest

# Add engine directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'engine'))

from sql_builder import (
    build_where_clause,
    build_count_query,
    get_field_by_logical,
    get_field_by_alias,
    translate_value_to_physical,
    translate_operator,
)


# ── Test data: simulate a minimal mapping ──

RESEARCH_FIELD_MAP = {
    '01': '物联网',
    '02': '人工智能',
    '03': '新材料',
}

GENDER_FIELD_DEF = {
    'logical': 'gender',
    'physical': 'gender_code',
    'alias': ['性别', 'sex'],
    'mapping': {'M': '男', 'F': '女'},
}

RESEARCH_FIELD_DEF = {
    'logical': 'research_field',
    'physical': 'field_code',
    'alias': ['研究方向', '专业领域'],
    'mapping': RESEARCH_FIELD_MAP,
}

AGE_FIELD_DEF = {
    'logical': 'age',
    'physical': 'birth_year',
    'alias': ['年龄', '岁数'],
    'derived': True,
    'derive_expr': '(2025 - birth_year)',
}


class TestTranslateOperator:
    def test_eq(self):
        assert translate_operator('eq') == '='

    def test_neq(self):
        assert translate_operator('neq') == '!='

    def test_gt(self):
        assert translate_operator('gt') == '>'

    def test_gte(self):
        assert translate_operator('gte') == '>='

    def test_lt(self):
        assert translate_operator('lt') == '<'

    def test_lte(self):
        assert translate_operator('lte') == '<='

    def test_in(self):
        assert translate_operator('in') == 'IN'

    def test_unknown_defaults_to_eq(self):
        assert translate_operator('xyz') == '='


class TestTranslateValueToPhysical:
    def test_direct_mapping(self):
        result = translate_value_to_physical(GENDER_FIELD_DEF, '男')
        assert result == 'M'

    def test_reverse_mapping(self):
        result = translate_value_to_physical(RESEARCH_FIELD_DEF, '人工智能')
        assert result == '02'

    def test_value_is_already_code(self):
        result = translate_value_to_physical(RESEARCH_FIELD_DEF, '02')
        assert result == '02'

    def test_unknown_value_returns_as_is(self):
        result = translate_value_to_physical(GENDER_FIELD_DEF, '未知')
        assert result == '未知'

    def test_no_mapping_returns_value(self):
        field_def = {'logical': 'name', 'physical': 'person_name'}
        result = translate_value_to_physical(field_def, '张三')
        assert result == '张三'


class TestBuildWhereClause:
    """WHERE clause tests using real mapping file (integration)."""

    def test_empty_predicates(self, temp_mapping_file):
        where, params = build_where_clause([])
        assert where == ''
        assert params == ()

    def test_single_eq_predicate(self, temp_mapping_file, monkeypatch):
        monkeypatch.setenv('DB_TYPE', 'sqlite')
        where, params = build_where_clause([{'field': 'gender', 'op': 'eq', 'value': '男'}])
        assert 'gender_code' in where
        assert '=' in where
        assert params == ('M',)  # value translated via mapping

    def test_multiple_predicates_and(self, temp_mapping_file, monkeypatch):
        monkeypatch.setenv('DB_TYPE', 'sqlite')
        preds = [
            {'field': 'gender', 'op': 'eq', 'value': '男'},
            {'field': 'age', 'op': 'gte', 'value': 35},
        ]
        where, params = build_where_clause(preds)
        assert 'AND' in where
        assert len(params) == 2

    def test_in_operator_single(self, temp_mapping_file, monkeypatch):
        monkeypatch.setenv('DB_TYPE', 'sqlite')
        preds = [{'field': 'research_field', 'op': 'in', 'value': ['物联网', '人工智能']}]
        where, params = build_where_clause(preds)
        assert 'IN' in where
        assert params == ('01', '02')

    def test_placeholder_sqlite(self, temp_mapping_file, monkeypatch):
        monkeypatch.setenv('DB_TYPE', 'sqlite')
        where, params = build_where_clause([{'field': 'gender', 'op': 'eq', 'value': '男'}])
        assert '?' in where

    def test_placeholder_postgresql(self, temp_mapping_file, monkeypatch):
        monkeypatch.setenv('DB_TYPE', 'postgresql')
        where, params = build_where_clause([{'field': 'gender', 'op': 'eq', 'value': '男'}])
        assert '%s' in where

    def test_derived_field_uses_expr(self, temp_mapping_file, monkeypatch):
        monkeypatch.setenv('DB_TYPE', 'sqlite')
        where, params = build_where_clause([{'field': 'age', 'op': 'gte', 'value': 35}])
        assert 'birth_year' in where
        assert 35 in params


class TestBuildCountQuery:
    """Count query tests using real mapping file (integration)."""

    def test_count_with_predicates(self, temp_mapping_file, monkeypatch):
        monkeypatch.setenv('DB_TYPE', 'sqlite')
        sql, params = build_count_query([
            {'field': 'gender', 'op': 'eq', 'value': '女'},
        ])
        assert 'COUNT(*)' in sql
        assert 'test_table' in sql  # table name is quoted now
        assert 'gender_code' in sql  # column is quoted
        assert len(params) == 1

    def test_count_no_predicates(self, temp_mapping_file, monkeypatch):
        monkeypatch.setenv('DB_TYPE', 'sqlite')
        sql, params = build_count_query([])
        assert 'COUNT(*)' in sql
        assert 'WHERE' not in sql
        assert len(params) == 0


class TestMappingIntegration:
    """Tests that exercise the full mapping → SQL pipeline."""

    def test_gender_with_value_translation(self, temp_mapping_file, monkeypatch):
        monkeypatch.setenv('DB_TYPE', 'sqlite')
        sql, params = build_count_query([
            {'field': 'gender', 'op': 'eq', 'value': '男'},
        ])
        # Value should be translated '男' → 'M' via mapping
        assert params[0] == 'M'

    def test_research_field_with_value_translation(self, temp_mapping_file, monkeypatch):
        monkeypatch.setenv('DB_TYPE', 'sqlite')
        sql, params = build_count_query([
            {'field': 'research_field', 'op': 'eq', 'value': '人工智能'},
        ])
        assert params[0] == '02'

    def test_alias_resolution(self, temp_mapping_file, monkeypatch):
        monkeypatch.setenv('DB_TYPE', 'sqlite')
        sql, params = build_count_query([
            {'field': '性别', 'op': 'eq', 'value': '女'},
        ])
        # '性别' is an alias for 'gender', which maps to 'gender_code'
        assert 'gender_code' in sql
        assert params[0] == 'F'

    def test_multiple_filters_use_and(self, temp_mapping_file, monkeypatch):
        monkeypatch.setenv('DB_TYPE', 'sqlite')
        sql, params = build_count_query([
            {'field': 'gender', 'op': 'eq', 'value': '女'},
            {'field': 'research_field', 'op': 'eq', 'value': '物联网'},
        ])
        assert ' AND ' in sql
        assert len(params) == 2

    def test_in_operator_with_multiple_values(self, temp_mapping_file, monkeypatch):
        monkeypatch.setenv('DB_TYPE', 'sqlite')
        sql, params = build_count_query([
            {'field': 'research_field', 'op': 'in', 'value': ['人工智能', '新材料']},
        ])
        assert 'IN' in sql
        assert len(params) == 2  # two values
        assert params == ('02', '03')

    def test_derived_field_uses_expression(self, temp_mapping_file, monkeypatch):
        monkeypatch.setenv('DB_TYPE', 'sqlite')
        sql, params = build_count_query([
            {'field': 'age', 'op': 'gte', 'value': 35},
        ])
        # Derived field should wrap the derive_expr
        assert '2025 - birth_year' in sql or '(2025 - birth_year)' in sql

    def test_identifier_quoting_sqlite(self, temp_mapping_file, monkeypatch):
        monkeypatch.setenv('DB_TYPE', 'sqlite')
        sql, params = build_count_query([
            {'field': 'gender', 'op': 'eq', 'value': '男'},
        ])
        # SQLite uses double quotes for identifiers
        assert '"gender_code"' in sql
        assert '"test_table"' in sql

    def test_identifier_quoting_mysql(self, temp_mapping_file, monkeypatch):
        monkeypatch.setenv('DB_TYPE', 'mysql')
        sql, params = build_count_query([
            {'field': 'gender', 'op': 'eq', 'value': '男'},
        ])
        # MySQL uses backticks
        assert '`gender_code`' in sql
        assert '`test_table`' in sql

    def test_identifier_quoting_pg(self, temp_mapping_file, monkeypatch):
        monkeypatch.setenv('DB_TYPE', 'postgresql')
        sql, params = build_count_query([
            {'field': 'gender', 'op': 'eq', 'value': '男'},
        ])
        # PostgreSQL uses double quotes
        assert '"gender_code"' in sql
        assert '"test_table"' in sql
