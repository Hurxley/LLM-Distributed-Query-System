"""
End-to-end integration tests for the coordinator API, auth middleware,
DAG scheduler, WebSocket, and plan repair.

These tests import coordinator modules via the sys.path hook that conftest.py
already configures.  Worker HTTP calls are mocked via httpx transport patching.
"""

import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import httpx

# conftest.py already inserts coordinator/ into sys.path — the coordinator
# modules are imported as top-level names, NOT as a package.
import app as coord_app
from scheduler import DAGScheduler


# ── Fixtures ──

@pytest.fixture(autouse=True)
def clear_coordinator_state():
    """Reset in-memory state between tests."""
    coord_app.queries.clear()
    coord_app.ws_connections.clear()
    coord_app.global_schema.workers.clear()
    coord_app.global_schema.field_index.clear()
    coord_app.global_schema.alias_index.clear()
    coord_app.AUTH_ENABLED = False
    yield
    coord_app.queries.clear()
    coord_app.ws_connections.clear()
    coord_app.global_schema.workers.clear()
    coord_app.global_schema.field_index.clear()
    coord_app.global_schema.alias_index.clear()
    coord_app.AUTH_ENABLED = False


@pytest.fixture
def registered_workers():
    """Register the three standard workers in global_schema."""
    gs = coord_app.global_schema

    workers = [
        {
            'worker_id': 'worker_a', 'worker_name': '人才库',
            'fields': [
                {'logical': 'research_field', 'alias': ['研究方向'], 'type': 'enum',
                 'values': ['物联网', '人工智能', '新材料', '生物医药']},
                {'logical': 'age', 'alias': ['年龄'], 'type': 'text', 'values': ['numeric']},
                {'logical': 'title', 'alias': ['职称'], 'type': 'enum',
                 'values': ['教授', '副教授', '讲师', '工程师']},
                {'logical': 'person_token', 'alias': [], 'secret': True, 'type': 'token'},
            ],
            'baseline': {'row_count': 100000, 'scan_latency_ms': 200},
        },
        {
            'worker_id': 'worker_b', 'worker_name': '海外经历库',
            'fields': [
                {'logical': 'study_country', 'alias': ['留学国家'], 'type': 'enum',
                 'values': ['美国', '英国', '德国']},
                {'logical': 'has_overseas', 'alias': ['是否有海外经历'], 'type': 'text',
                 'values': ['是', '否']},
                {'logical': 'award_level', 'alias': ['奖励级别'], 'type': 'enum',
                 'values': ['国家级', '省级', '市级']},
                {'logical': 'person_token', 'alias': [], 'secret': True, 'type': 'token'},
            ],
            'baseline': {'row_count': 150000, 'scan_latency_ms': 180},
        },
        {
            'worker_id': 'worker_c', 'worker_name': '财务库',
            'fields': [
                {'logical': 'monthly_income', 'alias': ['月收入'], 'type': 'text',
                 'values': ['numeric']},
                {'logical': 'annual_bonus', 'alias': ['年终奖'], 'type': 'text',
                 'values': ['numeric']},
                {'logical': 'total_subsidy', 'alias': ['总补贴'], 'type': 'text',
                 'values': ['numeric']},
                {'logical': 'pay_year', 'alias': ['发放年份'], 'type': 'text',
                 'values': ['numeric']},
                {'logical': 'person_token', 'alias': [], 'secret': True, 'type': 'token'},
            ],
            'baseline': {'row_count': 200000, 'scan_latency_ms': 150},
        },
    ]
    for w in workers:
        gs.register_worker(w)
    return workers


@pytest.fixture
def test_client(registered_workers):
    """Create an httpx AsyncClient backed by the real coordinator ASGI app."""
    transport = httpx.ASGITransport(app=coord_app.app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


# ── Mock worker HTTP transport ──

class MockWorkerTransport(httpx.AsyncBaseTransport):
    """Mock HTTP transport that simulates worker responses."""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        body = {}

        try:
            body = json.loads(request.content) if request.content else {}
        except Exception:
            pass

        make_resp = lambda status, **kw: httpx.Response(status, request=request, **kw)

        if 'worker_a:8001' in url:
            if '/filter' in url:
                return make_resp(200, json={
                    'tokens': ['a1', 'a2', 'a3'],
                    'count': 3,
                    'sql': 'SELECT person_token FROM talent WHERE ...',
                })
            elif '/count' in url:
                return make_resp(200, json={'count': 5000})
            elif '/aggregate' in url:
                return make_resp(200, json={
                    'sum': 0, 'count': 0, 'min': 0, 'max': 0, 'value': 0,
                    'func': body.get('agg_func', 'avg'),
                    'sql': 'SELECT ... FROM salary',
                })

        elif 'worker_b:8002' in url:
            if '/filter' in url:
                return make_resp(200, json={
                    'tokens': ['b1', 'b2', 'a1', 'a2'],
                    'count': 4,
                    'sql': 'SELECT person_token FROM overseas WHERE ...',
                })
            elif '/count' in url:
                return make_resp(200, json={'count': 3000})

        elif 'worker_c:8003' in url:
            if '/aggregate' in url:
                return make_resp(200, json={
                    'sum': 150000.0, 'count': 100, 'min': 8000.0, 'max': 25000.0,
                    'value': 15000.0,
                    'func': body.get('agg_func', 'avg'),
                    'sql': 'SELECT ... FROM salary',
                })

        return make_resp(404, json={'error': 'not found'})


# ── Tests ──

class TestAPIEndpoints:
    """Test the coordinator REST API endpoints (async)."""

    @pytest.mark.asyncio
    async def test_health_via_static(self, test_client):
        """Static files and index are served at /."""
        resp = await test_client.get('/')
        assert resp.status_code in (200, 307)

    @pytest.mark.asyncio
    async def test_schema_endpoint(self, test_client, registered_workers):
        """GET /api/schema returns registered workers."""
        resp = await test_client.get('/api/schema')
        assert resp.status_code == 200
        data = resp.json()
        assert 'workers' in data
        assert 'worker_a' in data['workers']
        assert data['workers']['worker_a']['name'] == '人才库'

    @pytest.mark.asyncio
    async def test_submit_query_single_filter(self, test_client, registered_workers):
        """POST /api/query returns query_id and plans."""
        resp = await test_client.post('/api/query', json={
            'query': '人工智能方向的副教授的平均月收入',
        })
        assert resp.status_code == 200
        data = resp.json()
        assert 'query_id' in data
        assert len(data['query_id']) == 8
        assert 'plans' in data
        assert len(data['plans']) >= 1
        for plan in data['plans']:
            assert 'friendly_name' in plan
            assert 'friendly_description' in plan
            assert 'estimated_cost_ms' in plan

    @pytest.mark.asyncio
    async def test_submit_query_multi_filter(self, test_client, registered_workers):
        """Multi-condition query generates multiple plans."""
        resp = await test_client.post('/api/query', json={
            'query': '物联网方向、有海外经历的教授的平均月收入',
        })
        assert resp.status_code == 200
        data = resp.json()
        assert len(data['plans']) >= 1

    @pytest.mark.asyncio
    async def test_get_plans_endpoint(self, test_client, registered_workers):
        """GET /api/query/{id}/plans returns detailed plans."""
        submit_resp = await test_client.post('/api/query', json={
            'query': '高校副教授的平均月收入',
        })
        qid = submit_resp.json()['query_id']

        resp = await test_client.get(f'/api/query/{qid}/plans')
        assert resp.status_code == 200
        data = resp.json()
        assert data['query_id'] == qid
        assert 'stages' in data['plans'][0]

    @pytest.mark.asyncio
    async def test_query_not_found(self, test_client):
        """404 when query ID doesn't exist."""
        resp = await test_client.get('/api/query/nonexist/plans')
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_submit_empty_query(self, test_client):
        """400 when query text is empty."""
        resp = await test_client.post('/api/query', json={'query': ''})
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_plan_deduplication(self, test_client, registered_workers):
        """Plans returned should have unique friendly_descriptions."""
        resp = await test_client.post('/api/query', json={
            'query': '生物医药方向、35岁以下且有海外经历的人员数量',
        })
        data = resp.json()
        descs = [p['friendly_description'] for p in data['plans']]
        assert len(descs) == len(set(descs)), f"Duplicate descriptions found: {descs}"


class TestScheduler:
    """Test DAG scheduler with mocked worker HTTP transport (async)."""

    WORKER_URLS = {
        'worker_a': 'http://worker_a:8001',
        'worker_b': 'http://worker_b:8002',
        'worker_c': 'http://worker_c:8003',
    }

    @pytest.mark.asyncio
    async def test_scheduler_executes_concurrent_plan(self, registered_workers):
        """DAG scheduler executes a concurrent plan and collects results."""
        plan = {
            'id': 'P1', 'name': '并行测试',
            'stages': [
                {'id': 'F1', 'type': 'filter', 'location': 'worker_a',
                 'concurrent_group': 1,
                 'predicates': [{'field': 'research_field', 'op': 'eq', 'value': '人工智能'}]},
                {'id': 'F2', 'type': 'filter', 'location': 'worker_b',
                 'concurrent_group': 1,
                 'predicates': [{'field': 'has_overseas', 'op': 'eq', 'value': '是'}]},
                {'id': 'I', 'type': 'intersect', 'location': 'coordinator',
                 'depends_on': ['F1', 'F2']},
                {'id': 'C', 'type': 'aggregate', 'location': 'worker_c',
                 'depends_on': ['I'], 'agg_field': 'monthly_income', 'agg_func': 'avg'},
                {'id': 'R', 'type': 'compute', 'location': 'coordinator',
                 'depends_on': ['C'], 'agg_func': 'avg'},
            ],
        }

        mock_transport = MockWorkerTransport()

        async def mock_post(url, **kwargs):
            req = httpx.Request('POST', url, **kwargs)
            return await mock_transport.handle_async_request(req)

        with patch('httpx.AsyncClient') as mock_client:
            mock_client.return_value.__aenter__.return_value.post = mock_post
            mock_client.return_value.__aexit__ = AsyncMock(return_value=None)

            scheduler = DAGScheduler(self.WORKER_URLS)
            scheduler.set_query_id('test-qid')
            result = await scheduler.execute_plan(plan)

        assert result['success'] is True
        assert len(result['stage_times']) == 5
        for sid in ['F1', 'F2', 'I', 'C', 'R']:
            assert sid in result['stage_times'], f"Missing stage {sid}"

    @pytest.mark.asyncio
    async def test_scheduler_serial_chain(self, registered_workers):
        """DAG scheduler respects depends_on ordering in serial chains."""
        plan = {
            'id': 'P2', 'name': '串行测试',
            'stages': [
                {'id': 'S1', 'type': 'filter', 'location': 'worker_a',
                 'predicates': [{'field': 'research_field', 'op': 'eq', 'value': '物联网'}]},
                {'id': 'C', 'type': 'aggregate', 'location': 'worker_c',
                 'depends_on': ['S1'], 'agg_field': 'monthly_income', 'agg_func': 'avg'},
                {'id': 'R', 'type': 'compute', 'location': 'coordinator',
                 'depends_on': ['C'], 'agg_func': 'avg'},
            ],
        }

        mock_transport = MockWorkerTransport()

        async def mock_post(url, **kwargs):
            req = httpx.Request('POST', url, **kwargs)
            return await mock_transport.handle_async_request(req)

        with patch('httpx.AsyncClient') as mock_client:
            mock_client.return_value.__aenter__.return_value.post = mock_post
            mock_client.return_value.__aexit__ = AsyncMock(return_value=None)

            scheduler = DAGScheduler(self.WORKER_URLS)
            scheduler.set_query_id('test-serial')
            result = await scheduler.execute_plan(plan)

        assert result['success'] is True
        assert len(result['stage_times']) == 3

    @pytest.mark.asyncio
    async def test_pushdown_intersect(self, registered_workers):
        """Push-down intersect unions tokens and skips coordinator intersection."""
        plan = {
            'id': 'P3', 'name': '数据下推测试',
            'stages': [
                {'id': 'F1', 'type': 'filter', 'location': 'worker_a',
                 'concurrent_group': 1,
                 'predicates': [{'field': 'research_field', 'op': 'eq', 'value': '新材料'}]},
                {'id': 'F2', 'type': 'filter', 'location': 'worker_b',
                 'concurrent_group': 1,
                 'predicates': [{'field': 'award_level', 'op': 'eq', 'value': '国家级'}]},
                {'id': 'I', 'type': 'intersect', 'location': 'worker_c',
                 'depends_on': ['F1', 'F2']},
                {'id': 'C', 'type': 'aggregate', 'location': 'worker_c',
                 'depends_on': ['I'], 'agg_field': 'annual_bonus', 'agg_func': 'avg'},
                {'id': 'R', 'type': 'compute', 'location': 'coordinator',
                 'depends_on': ['C'], 'agg_func': 'avg'},
            ],
        }

        mock_transport = MockWorkerTransport()

        async def mock_post(url, **kwargs):
            req = httpx.Request('POST', url, **kwargs)
            return await mock_transport.handle_async_request(req)

        with patch('httpx.AsyncClient') as mock_client:
            mock_client.return_value.__aenter__.return_value.post = mock_post
            mock_client.return_value.__aexit__ = AsyncMock(return_value=None)

            scheduler = DAGScheduler(self.WORKER_URLS)
            scheduler.set_query_id('test-pushdown')
            result = await scheduler.execute_plan(plan)

        assert result['success'] is True
        # Push-down union should have 5 unique tokens (3 from a + 4 from b - 2 overlap)
        intersect_tokens = scheduler.results.get('I', {}).get('tokens', [])
        assert len(intersect_tokens) == 5, \
            f"Push-down union should have 5 unique tokens, got {len(intersect_tokens)}"


class TestAuthMiddleware:
    """Test API authentication middleware (async)."""

    @pytest.mark.asyncio
    async def test_public_paths_no_auth_needed(self, registered_workers):
        """Public paths are accessible without auth; protected paths need token."""
        coord_app.AUTH_ENABLED = True
        coord_app.API_TOKEN = 'test-token'

        try:
            transport = httpx.ASGITransport(app=coord_app.app)
            client = httpx.AsyncClient(transport=transport, base_url="http://test")

            # Public paths — no token needed
            resp = await client.get('/')
            assert resp.status_code in (200, 307)

            resp = await client.get('/api/schema')
            assert resp.status_code == 200

            # Protected path — 401 without token
            resp = await client.post('/api/query', json={'query': 'test'})
            assert resp.status_code == 401

            # Protected path — works with correct token
            resp = await client.post(
                '/api/query', json={'query': 'test'},
                headers={'Authorization': 'Bearer test-token'},
            )
            assert resp.status_code == 200

            # Wrong token → 401
            resp = await client.post(
                '/api/query', json={'query': 'test'},
                headers={'Authorization': 'Bearer wrong-token'},
            )
            assert resp.status_code == 401

            await client.aclose()
        finally:
            coord_app.AUTH_ENABLED = False

    @pytest.mark.asyncio
    async def test_401_has_cors_headers(self, registered_workers):
        """401 responses from AuthMiddleware include CORS headers."""
        coord_app.AUTH_ENABLED = True
        coord_app.API_TOKEN = 'test-token'

        try:
            transport = httpx.ASGITransport(app=coord_app.app)
            client = httpx.AsyncClient(transport=transport, base_url="http://test")

            resp = await client.post('/api/query', json={'query': 'test'})
            assert resp.status_code == 401
            assert 'access-control-allow-origin' in resp.headers, \
                "401 response missing CORS headers"

            await client.aclose()
        finally:
            coord_app.AUTH_ENABLED = False

    @pytest.mark.asyncio
    async def test_auth_disabled_allows_all(self, registered_workers):
        """When API_TOKEN is empty, all endpoints are accessible."""
        coord_app.AUTH_ENABLED = False

        transport = httpx.ASGITransport(app=coord_app.app)
        client = httpx.AsyncClient(transport=transport, base_url="http://test")

        resp = await client.post('/api/query', json={'query': 'test'})
        assert resp.status_code == 200

        await client.aclose()


class TestWebSocketFlow:
    """Test WebSocket cleanup (async)."""

    @pytest.mark.asyncio
    async def test_ws_cleanup_orphaned(self, registered_workers):
        """Orphaned WebSocket connections are cleaned up."""
        coord_app.ws_connections['orphaned-qid'] = []
        coord_app.queries.clear()

        await coord_app._cleanup_ws_connections()

        assert 'orphaned-qid' not in coord_app.ws_connections, \
            "Orphaned WS connection should be removed after cleanup"


class TestRepairExecutionPlan:
    """Test the shared repair_execution_plan function."""

    def test_repair_fills_predicates(self, registered_workers):
        """repair_execution_plan populates predicates and agg info."""
        from planner.validation import repair_execution_plan

        query_ast = {
            'filters': [
                {'field': 'research_field', 'op': 'eq', 'value': '人工智能',
                 'workers': ['worker_a']},
            ],
            'aggregation': {
                'field': 'monthly_income', 'func': 'avg', 'workers': ['worker_c'],
            },
        }

        plan = {
            'id': 'P1',
            'stages': [
                {'id': 'F1', 'type': 'filter', 'location': 'worker_a'},
                {'id': 'C', 'type': 'aggregate', 'location': 'worker_c',
                 'depends_on': ['F1']},
                {'id': 'R', 'type': 'compute', 'location': 'coordinator',
                 'depends_on': ['C']},
            ],
        }

        repaired = repair_execution_plan(plan, query_ast, {'worker_a', 'worker_b', 'worker_c'})

        assert repaired['stages'][0]['predicates'][0]['field'] == 'research_field'
        agg_stage = repaired['stages'][1]
        assert agg_stage['agg_field'] == 'monthly_income'
        assert agg_stage['agg_func'] == 'avg'

    def test_repair_merges_duplicate_filters(self, registered_workers):
        """Duplicate filter stages on the same worker are merged."""
        from planner.validation import repair_execution_plan

        query_ast = {
            'filters': [
                {'field': 'research_field', 'op': 'eq', 'value': '人工智能',
                 'workers': ['worker_a']},
                {'field': 'age', 'op': 'lt', 'value': '40',
                 'workers': ['worker_a']},
            ],
            'aggregation': {
                'field': 'annual_bonus', 'func': 'avg', 'workers': ['worker_c'],
            },
        }

        plan = {
            'id': 'P1',
            'stages': [
                {'id': 'F1', 'type': 'filter', 'location': 'worker_a'},
                {'id': 'F2', 'type': 'filter', 'location': 'worker_a'},
                {'id': 'C', 'type': 'aggregate', 'location': 'worker_c',
                 'depends_on': ['F1', 'F2']},
                {'id': 'R', 'type': 'compute', 'location': 'coordinator',
                 'depends_on': ['C']},
            ],
        }

        repaired = repair_execution_plan(plan, query_ast, {'worker_a', 'worker_b', 'worker_c'})

        filter_stages = [s for s in repaired['stages'] if s['type'] == 'filter']
        assert len(filter_stages) == 1, \
            f"Expected 1 merged filter stage, got {len(filter_stages)}"
        assert len(filter_stages[0]['predicates']) == 2

    def test_repair_auto_inserts_intersect(self, registered_workers):
        """Missing intersect is auto-inserted when 2+ filter workers exist."""
        from planner.validation import repair_execution_plan

        query_ast = {
            'filters': [
                {'field': 'research_field', 'op': 'eq', 'value': '人工智能',
                 'workers': ['worker_a']},
                {'field': 'award_level', 'op': 'eq', 'value': '国家级',
                 'workers': ['worker_b']},
            ],
            'aggregation': {
                'field': 'monthly_income', 'func': 'avg', 'workers': ['worker_c'],
            },
        }

        plan = {
            'id': 'P1',
            'stages': [
                {'id': 'F1', 'type': 'filter', 'location': 'worker_a'},
                {'id': 'F2', 'type': 'filter', 'location': 'worker_b'},
                {'id': 'C', 'type': 'aggregate', 'location': 'worker_c',
                 'depends_on': ['F1', 'F2']},
                {'id': 'R', 'type': 'compute', 'location': 'coordinator',
                 'depends_on': ['C']},
            ],
        }

        repaired = repair_execution_plan(plan, query_ast, {'worker_a', 'worker_b', 'worker_c'})

        intersect_stages = [s for s in repaired['stages'] if s['type'] == 'intersect']
        assert len(intersect_stages) == 1, \
            "Should auto-insert one intersect stage for 2+ filter workers"
