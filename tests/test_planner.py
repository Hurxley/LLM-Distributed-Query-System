"""
Unit tests for coordinator/planner — plan generation, validation, cost model, description.
"""

import pytest
from planner.enumeration import _enumerate_all_plans, _generate_plans_rule_based
from planner.validation import _validate_and_repair_plan, _plan_topology_signature, _merge_plans
from planner.cost_model import compute_cost, rank_plans
from planner.description import _generate_friendly_description


# ── Shared test fixtures ──

@pytest.fixture
def workers_summary():
    return {
        'worker_a': {'name': '人才库', 'row_count': 1000, 'scan_ms': 180,
                     'fields': ['person_token', 'gender', 'research_field', 'title', 'org_type', 'age']},
        'worker_b': {'name': '海外库', 'row_count': 700, 'scan_ms': 160,
                     'fields': ['person_token', 'has_overseas', 'study_country', 'max_award_level']},
        'worker_c': {'name': '财务库', 'row_count': 28800, 'scan_ms': 500,
                     'fields': ['person_token', 'monthly_income', 'annual_bonus', 'subsidy']},
    }


@pytest.fixture
def precheck_counts():
    return {'worker_a': 100, 'worker_b': 50}


def make_query_ast(filters=None, agg_workers=None, agg_field='monthly_income', agg_func='avg'):
    """Build a query_ast with given filters and aggregation."""
    return {
        'filters': filters or [],
        'aggregation': {
            'field': agg_field,
            'func': agg_func,
            'workers': agg_workers or ['worker_c'],
        },
    }


def make_single_filter_ast():
    return make_query_ast(filters=[
        {'field': 'gender', 'op': 'eq', 'value': '女', 'workers': ['worker_a']},
    ])


def make_two_filter_ast():
    return make_query_ast(filters=[
        {'field': 'gender', 'op': 'eq', 'value': '女', 'workers': ['worker_a']},
        {'field': 'has_overseas', 'op': 'eq', 'value': 'true', 'workers': ['worker_b']},
    ])


# ── Enumerate Plans ──

class TestEnumeratePlans:
    def test_no_filters(self, workers_summary, precheck_counts):
        ast = make_query_ast()
        plans = _enumerate_all_plans(ast, workers_summary, precheck_counts)
        assert len(plans) == 1
        assert plans[0]['name'] == '直接统计 — 无需筛选'

    def test_single_filter(self, workers_summary, precheck_counts):
        ast = make_single_filter_ast()
        plans = _enumerate_all_plans(ast, workers_summary, precheck_counts)
        assert len(plans) == 1
        assert '单节点查询' in plans[0]['name']

    def test_two_filters_generates_multiple_plans(self, workers_summary, precheck_counts):
        ast = make_two_filter_ast()
        plans = _enumerate_all_plans(ast, workers_summary, precheck_counts)
        # Should have concurrent + serial permutations + pushdown variants
        assert len(plans) >= 4

    def test_all_plans_have_required_structure(self, workers_summary, precheck_counts):
        ast = make_two_filter_ast()
        plans = _enumerate_all_plans(ast, workers_summary, precheck_counts)
        for plan in plans:
            assert 'id' in plan
            assert 'name' in plan
            assert 'stages' in plan
            assert len(plan['stages']) >= 3
            # Every plan must have aggregate and compute stages
            stage_types = {s['type'] for s in plan['stages']}
            assert 'aggregate' in stage_types
            assert 'compute' in stage_types


# ── Plan Validation ──

class TestValidateAndRepair:
    def test_fix_aggregate_on_coordinator(self, workers_summary):
        plan = {
            'id': 'P1', 'stages': [
                {'id': 'C', 'type': 'aggregate', 'location': 'coordinator'},
                {'id': 'R', 'type': 'compute', 'location': 'coordinator', 'depends_on': ['C']},
            ]
        }
        ast = make_single_filter_ast()
        repaired = _validate_and_repair_plan(plan, set(workers_summary.keys()), ast)
        agg = next(s for s in repaired['stages'] if s['type'] == 'aggregate')
        assert agg['location'] == 'worker_c'

    def test_auto_insert_intersect(self, workers_summary):
        """Two filter stages without intersect should get one auto-inserted."""
        plan = {
            'id': 'P1', 'stages': [
                {'id': 'F1', 'type': 'filter', 'location': 'worker_a'},
                {'id': 'F2', 'type': 'filter', 'location': 'worker_b'},
                {'id': 'C', 'type': 'aggregate', 'location': 'worker_c', 'depends_on': ['F1', 'F2']},
                {'id': 'R', 'type': 'compute', 'location': 'coordinator', 'depends_on': ['C']},
            ]
        }
        ast = make_two_filter_ast()
        repaired = _validate_and_repair_plan(plan, set(workers_summary.keys()), ast)
        stage_types = {s['type'] for s in repaired['stages']}
        assert 'intersect' in stage_types

    def test_skip_filter_no_predicates(self, workers_summary):
        """Filter stage for invalid worker gets location repaired to valid filter worker."""
        plan = {
            'id': 'P1', 'stages': [
                {'id': 'F1', 'type': 'filter', 'location': 'worker_x'},
                {'id': 'C', 'type': 'aggregate', 'location': 'worker_c', 'depends_on': ['F1']},
                {'id': 'R', 'type': 'compute', 'location': 'coordinator', 'depends_on': ['C']},
            ]
        }
        ast = make_single_filter_ast()
        repaired = _validate_and_repair_plan(plan, set(workers_summary.keys()), ast)
        filter_stages = [s for s in repaired['stages'] if s['type'] == 'filter']
        assert len(filter_stages) == 1
        # Location should be repaired to worker_a (the valid filter worker)
        assert filter_stages[0]['location'] == 'worker_a'


# ── Topology Signature & Dedup ──

class TestTopologySignature:
    def test_different_ids_same_topology(self):
        plan1 = {
            'id': 'P1', 'stages': [
                {'id': 'A', 'type': 'filter', 'location': 'worker_a'},
                {'id': 'B', 'type': 'aggregate', 'location': 'worker_c', 'depends_on': ['A']},
                {'id': 'R', 'type': 'compute', 'location': 'coordinator', 'depends_on': ['B']},
            ]
        }
        plan2 = {
            'id': 'P2', 'stages': [
                {'id': 'X', 'type': 'filter', 'location': 'worker_a'},
                {'id': 'Y', 'type': 'aggregate', 'location': 'worker_c', 'depends_on': ['X']},
                {'id': 'Z', 'type': 'compute', 'location': 'coordinator', 'depends_on': ['Y']},
            ]
        }
        assert _plan_topology_signature(plan1) == _plan_topology_signature(plan2)

    def test_different_topology_produces_different_sig(self):
        plan1 = {
            'id': 'P1', 'stages': [
                {'id': 'A', 'type': 'filter', 'location': 'worker_a'},
                {'id': 'R', 'type': 'compute', 'location': 'coordinator', 'depends_on': ['A']},
            ]
        }
        plan2 = {
            'id': 'P2', 'stages': [
                {'id': 'A', 'type': 'filter', 'location': 'worker_a'},
                {'id': 'B', 'type': 'aggregate', 'location': 'worker_c', 'depends_on': ['A']},
                {'id': 'R', 'type': 'compute', 'location': 'coordinator', 'depends_on': ['B']},
            ]
        }
        assert _plan_topology_signature(plan1) != _plan_topology_signature(plan2)


class TestMergePlans:
    def test_dedup_removes_duplicates(self):
        plan1 = {
            'id': 'P1', 'stages': [
                {'id': 'A', 'type': 'filter', 'location': 'worker_a'},
                {'id': 'R', 'type': 'compute', 'location': 'coordinator', 'depends_on': ['A']},
            ]
        }
        plan2 = {
            'id': 'P2', 'stages': [
                {'id': 'X', 'type': 'filter', 'location': 'worker_a'},
                {'id': 'Z', 'type': 'compute', 'location': 'coordinator', 'depends_on': ['X']},
            ]
        }
        merged = _merge_plans([plan1], [plan2])
        assert len(merged) == 1


# ── Cost Model ──

class TestComputeCost:
    def test_plan_gets_estimated_cost(self, workers_summary, precheck_counts):
        ast = make_single_filter_ast()
        plans = _enumerate_all_plans(ast, workers_summary, precheck_counts)
        plan = compute_cost(plans[0], workers_summary, precheck_counts)
        assert 'estimated_cost_ms' in plan
        assert plan['estimated_cost_ms'] > 0

    def test_stage_costs_filled(self, workers_summary, precheck_counts):
        ast = make_two_filter_ast()
        plans = _enumerate_all_plans(ast, workers_summary, precheck_counts)
        plan = compute_cost(plans[0], workers_summary, precheck_counts)
        assert 'stage_costs' in plan
        for stage in plan['stages']:
            assert stage['id'] in plan['stage_costs']


class TestRankPlans:
    def test_first_plan_recommended(self, workers_summary, precheck_counts):
        ast = make_two_filter_ast()
        plans = _enumerate_all_plans(ast, workers_summary, precheck_counts)
        for p in plans:
            compute_cost(p, workers_summary, precheck_counts)
        ranked = rank_plans(plans)
        assert ranked[0]['recommended'] is True

    def test_sorted_by_cost(self, workers_summary, precheck_counts):
        ast = make_two_filter_ast()
        plans = _enumerate_all_plans(ast, workers_summary, precheck_counts)
        for p in plans:
            compute_cost(p, workers_summary, precheck_counts)
        ranked = rank_plans(plans)
        costs = [p['estimated_cost_ms'] for p in ranked]
        assert costs == sorted(costs)


# ── Friendly Description ──

class TestFriendlyDescription:
    def test_concurrent_plan_description(self, workers_summary, precheck_counts):
        ast = make_two_filter_ast()
        plans = _enumerate_all_plans(ast, workers_summary, precheck_counts)
        concurrent = next((p for p in plans if '并行查询' in p.get('name', '')), plans[0])
        friendly = _generate_friendly_description(concurrent, workers_summary)
        assert friendly['friendly_name']
        assert len(friendly['steps']) >= 3

    def test_single_filter_description(self, workers_summary, precheck_counts):
        ast = make_single_filter_ast()
        plans = _enumerate_all_plans(ast, workers_summary, precheck_counts)
        friendly = _generate_friendly_description(plans[0], workers_summary)
        assert friendly['friendly_name']
        assert len(friendly['steps']) >= 3
        # Single filter + no intersect should use "筛选结果传入" pattern
        full_text = friendly['friendly_description']
        assert '筛选' in full_text


# ── Rule-Based Generation ──

class TestRuleBasedGeneration:
    def test_no_workers(self, workers_summary, precheck_counts):
        ast = {'filters': [], 'aggregation': None}
        plans = _generate_plans_rule_based(ast, workers_summary, precheck_counts)
        assert len(plans) == 1

    def test_one_filter_worker(self, workers_summary, precheck_counts):
        ast = make_single_filter_ast()
        plans = _generate_plans_rule_based(ast, workers_summary, precheck_counts)
        assert len(plans) >= 1

    def test_two_filter_workers(self, workers_summary, precheck_counts):
        ast = make_two_filter_ast()
        plans = _generate_plans_rule_based(ast, workers_summary, precheck_counts)
        assert len(plans) >= 4
        plan_ids = {p['id'] for p in plans}
        assert 'P1' in plan_ids
        assert 'P4' in plan_ids  # push-down plan
