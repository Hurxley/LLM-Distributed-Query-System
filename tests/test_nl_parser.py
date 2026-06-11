"""
Unit tests for coordinator/nl_parser.py — rule-based NL query parsing.
"""

import pytest
from schema_manager import GlobalSchema
from nl_parser import parse_with_rules, anchor_and_validate, validate_parsed_query


@pytest.fixture
def sample_schema():
    """Create a GlobalSchema with workers that match the real deployment."""
    schema = GlobalSchema()
    schema.register_worker({
        'worker_id': 'worker_a',
        'worker_name': '人才库',
        'baseline': {'row_count': 1000, 'scan_latency_ms': 180},
        'fields': [
            {'logical': 'person_token', 'secret': True, 'tokenize': 'hmac-sha256', 'type': 'token'},
            {'logical': 'gender', 'alias': ['性别'], 'type': 'enum', 'mapping': {'M': '男', 'F': '女'},
             'values': ['男', '女']},
            {'logical': 'research_field', 'alias': ['研究方向', '方向', '领域'], 'type': 'enum',
             'mapping': {'01': '物联网', '02': '人工智能', '03': '新材料', '04': '生物医药', '05': '量子计算'},
             'values': ['物联网', '人工智能', '新材料', '生物医药', '量子计算']},
            {'logical': 'title', 'alias': ['职称'], 'type': 'enum',
             'mapping': {'11': '工程师', '12': '讲师', '13': '副教授', '14': '教授', '15': '研究员'},
             'values': ['工程师', '讲师', '副教授', '教授', '研究员']},
            {'logical': 'org_type', 'alias': ['单位类型'], 'type': 'enum',
             'mapping': {'U': '高校', 'R': '科研院所', 'E': '企业'},
             'values': ['高校', '科研院所', '企业']},
            {'logical': 'age', 'alias': ['年龄'], 'derived': True, 'derive_expr': '2025 - birth_year'},
        ],
    })
    schema.register_worker({
        'worker_id': 'worker_b',
        'worker_name': '海外库',
        'baseline': {'row_count': 700, 'scan_latency_ms': 160},
        'fields': [
            {'logical': 'person_token', 'secret': True, 'tokenize': 'hmac-sha256', 'type': 'token'},
            {'logical': 'overseas_experience', 'alias': ['海外经历', '有海外经历'], 'type': 'enum',
             'mapping': {'false': 'false', 'true': 'true'}, 'values': ['false', 'true']},
            {'logical': 'country_of_study', 'alias': ['留学国家'], 'type': 'enum',
             'mapping': {'美国': '美国', '英国': '英国', '德国': '德国', '日本': '日本', '澳大利亚': '澳大利亚', '无': '无'},
             'values': ['美国', '英国', '德国', '日本', '澳大利亚', '无']},
            {'logical': 'highest_award_level', 'alias': ['获奖级别'], 'type': 'enum',
             'mapping': {'无': '无', '市级': '市级', '省级': '省级', '国家级': '国家级'},
             'values': ['无', '市级', '省级', '国家级']},
        ],
    })
    schema.register_worker({
        'worker_id': 'worker_c',
        'worker_name': '财务库',
        'baseline': {'row_count': 28800, 'scan_latency_ms': 500},
        'fields': [
            {'logical': 'person_token', 'secret': True, 'tokenize': 'hmac-sha256', 'type': 'token'},
            {'logical': 'monthly_salary', 'alias': ['月收入', '月工资', '收入'], 'type': 'numeric'},
            {'logical': 'year_end_bonus', 'alias': ['年终奖', '奖金'], 'type': 'numeric'},
            {'logical': 'allowance', 'alias': ['补贴', '津贴'], 'type': 'numeric'},
        ],
    })
    return schema


class TestRuleBasedParsing:
    """Test parse_with_rules() — deterministic, no LLM needed."""

    def test_gender_filter(self, sample_schema):
        result = parse_with_rules("女性的平均月收入", sample_schema)
        filters = result['filters']
        assert any(f['field'] == 'gender' and f['value'] == '女' for f in filters)

    def test_research_field(self, sample_schema):
        result = parse_with_rules("人工智能方向的教授", sample_schema)
        filters = result['filters']
        assert any(f['field'] == 'research_field' and f['value'] == '人工智能' for f in filters)

    def test_org_type(self, sample_schema):
        """科研院所的讲师 — no overlapping substring issue with 副教授."""
        result = parse_with_rules("科研院所的讲师", sample_schema)
        filters = result['filters']
        assert any(f['field'] == 'org_type' and f['value'] == '科研院所' for f in filters)
        assert any(f['field'] == 'title' and f['value'] == '讲师' for f in filters)

    def test_title_gte(self, sample_schema):
        result = parse_with_rules("工程师及以上的平均月收入", sample_schema)
        filters = result['filters']
        assert any(f['field'] == 'title' and f['op'] == 'gte' and f['value'] == '工程师' for f in filters)

    def test_overseas_experience(self, sample_schema):
        result = parse_with_rules("有海外经历的教授", sample_schema)
        filters = result['filters']
        assert any(f['field'] == 'overseas_experience' and f['value'] == 'true' for f in filters)

    def test_award_level(self, sample_schema):
        result = parse_with_rules("省级以上奖励的教授", sample_schema)
        filters = result['filters']
        assert any(f['field'] == 'highest_award_level' and f['op'] == 'gte' and f['value'] == '省级' for f in filters)

    def test_age_filter(self, sample_schema):
        result = parse_with_rules("35岁以下的平均月收入", sample_schema)
        filters = result['filters']
        assert any(f['field'] == 'age' and f['op'] == 'lt' and f['value'] == '35' for f in filters)

    def test_year_filter(self, sample_schema):
        result = parse_with_rules("2024年的平均月收入", sample_schema)
        filters = result['filters']
        assert any(f['field'] == 'fiscal_year' and f['value'] == '2024' for f in filters)

    def test_multi_country_or(self, sample_schema):
        """美国或德国留学 should produce IN filter with both countries."""
        result = parse_with_rules("美国或德国留学的教授", sample_schema)
        filters = result['filters']
        country_filter = next((f for f in filters if f['field'] == 'country_of_study'), None)
        assert country_filter is not None
        assert country_filter['op'] == 'in'
        assert '美国' in country_filter['value']
        assert '德国' in country_filter['value']

    def test_single_country(self, sample_schema):
        """美国留学 should produce EQ filter."""
        result = parse_with_rules("美国留学的教授", sample_schema)
        filters = result['filters']
        country_filter = next((f for f in filters if f['field'] == 'country_of_study'), None)
        assert country_filter is not None
        assert country_filter['op'] == 'eq'
        assert country_filter['value'] == '美国'

    def test_avg_aggregation(self, sample_schema):
        result = parse_with_rules("平均月收入", sample_schema)
        assert result['aggregation']['func'] == 'avg'
        assert result['aggregation']['field'] == 'monthly_salary'

    def test_max_aggregation(self, sample_schema):
        result = parse_with_rules("最高月收入", sample_schema)
        assert result['aggregation']['func'] == 'max'

    def test_count_aggregation(self, sample_schema):
        result = parse_with_rules("教授的人数", sample_schema)
        assert result['aggregation']['func'] == 'count'

    def test_sum_aggregation(self, sample_schema):
        result = parse_with_rules("总补贴金额", sample_schema)
        assert result['aggregation']['func'] == 'sum'
        assert result['aggregation']['field'] == 'allowance'

    def test_annual_bonus_avg(self, sample_schema):
        result = parse_with_rules("平均年终奖", sample_schema)
        assert result['aggregation']['func'] == 'avg'
        assert result['aggregation']['field'] == 'year_end_bonus'

    def test_no_filter_query(self, sample_schema):
        result = parse_with_rules("平均月收入", sample_schema)
        assert result['filters'] == []


class TestAnchorAndValidate:
    """Test anchor_and_validate() and validate_parsed_query()."""

    def test_field_routed_to_correct_workers(self, sample_schema):
        parsed = {
            'filters': [{'field': 'gender', 'op': 'eq', 'value': '女'}],
            'aggregation': {'field': 'monthly_salary', 'func': 'avg'},
        }
        routed = anchor_and_validate(parsed, sample_schema)
        assert 'worker_a' in routed['filters'][0]['workers']
        assert 'worker_c' in routed['aggregation']['workers']

    def test_alias_resolution(self, sample_schema):
        parsed = {
            'filters': [{'field': '性别', 'op': 'eq', 'value': '女'}],
            'aggregation': None,
        }
        routed = anchor_and_validate(parsed, sample_schema)
        assert routed['filters'][0]['field'] == 'gender'

    def test_fuzzy_value_match(self, sample_schema):
        """Fuzzy matching should map '材料' -> '新材料'."""
        parsed = {
            'filters': [{'field': 'research_field', 'op': 'eq', 'value': '材料'}],
            'aggregation': None,
        }
        routed = anchor_and_validate(parsed, sample_schema)
        assert routed['filters'][0]['value'] == '新材料'

    def test_validate_valid_query(self, sample_schema):
        routed = {
            'filters': [{'field': 'gender', 'op': 'eq', 'value': '女', 'workers': ['worker_a']}],
            'aggregation': {'field': 'monthly_salary', 'func': 'avg', 'workers': ['worker_c']},
        }
        is_valid, errors = validate_parsed_query(routed)
        assert is_valid
        assert len(errors) == 0

    def test_validate_missing_workers(self):
        routed = {
            'filters': [{'field': 'unknown_field', 'op': 'eq', 'value': 'x', 'workers': []}],
            'aggregation': None,
        }
        is_valid, errors = validate_parsed_query(routed)
        assert not is_valid
        assert any('无法路由' in e for e in errors)

    def test_validate_invalid_op(self):
        routed = {
            'filters': [{'field': 'gender', 'op': 'invalid_op', 'value': 'x', 'workers': ['w1']}],
            'aggregation': None,
        }
        is_valid, errors = validate_parsed_query(routed)
        assert not is_valid
        assert any('不支持的运算符' in e for e in errors)

    def test_in_operator_list_value(self, sample_schema):
        parsed = {
            'filters': [{'field': 'country_of_study', 'op': 'in', 'value': ['美国', '德国']}],
            'aggregation': None,
        }
        routed = anchor_and_validate(parsed, sample_schema)
        assert len(routed['filters'][0]['value']) == 2
        assert 'worker_b' in routed['filters'][0]['workers']
