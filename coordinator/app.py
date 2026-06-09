"""
Coordinator — FastAPI application.

Routes:
  - POST /register          - Worker registration (system internal)
  - GET  /api/schema        - Get global schema view
  - POST /api/query         - Submit NL query, returns query ID
  - GET  /api/query/{id}/plans - Get candidate plans
  - POST /api/query/{id}/execute - Execute with recommended plan
  - POST /api/query/{id}/execute_with_plan/{pid} - Execute with specific plan
  - WS   /ws/{query_id}     - WebSocket for real-time events
  - GET  /                  - Frontend SPA
"""

import os
import sys
import json
import uuid
import time
import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import JSONResponse, HTMLResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from schema_manager import global_schema
from nl_parser import parse_query
from planner import generate_and_rank_plans
from planner.validation import repair_execution_plan
from scheduler import DAGScheduler

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] [%(levelname)s] %(message)s')
logger = logging.getLogger("coordinator")

# Worker URL mapping
WORKER_URLS_STR = os.environ.get('WORKER_URLS', 'http://worker_a:8001,http://worker_b:8002,http://worker_c:8003')
WORKER_URLS = {}
for entry in WORKER_URLS_STR.split(','):
    entry = entry.strip()
    if not entry:
        continue
    # Derive worker ID from URL (e.g., http://worker_a:8001 -> worker_a)
    host = entry.split('://')[1].split(':')[0] if '://' in entry else entry.split(':')[0]
    worker_id = host.replace('_', '_')  # worker_a format
    WORKER_URLS[worker_id] = entry

# In-memory query store
queries: dict[str, dict] = {}

# Active WebSocket connections
ws_connections: dict[str, list[WebSocket]] = {}

# ── Query TTL Eviction (Fix 2) ──
QUERY_TTL_SECONDS = int(os.environ.get('QUERY_TTL_SECONDS', '3600'))
QUERY_CLEANUP_INTERVAL = int(os.environ.get('QUERY_CLEANUP_INTERVAL', '300'))

# ── API Authentication (Fix 5) ──
API_TOKEN = os.environ.get('API_TOKEN', '')
AUTH_ENABLED = bool(API_TOKEN)

PUBLIC_PATHS = {'/', '/register', '/api/schema', '/health', '/docs', '/openapi.json'}
PUBLIC_PREFIXES = ('/static/',)


def _cleanup_expired_queries() -> list[str]:
    """Remove query entries that have exceeded their TTL. Returns expired qid list."""
    now = time.time()
    expired = [
        qid for qid, q in queries.items()
        if now - q.get('created_at', 0) > QUERY_TTL_SECONDS
    ]
    for qid in expired:
        del queries[qid]
        logger.info(f"TTL cleanup: evicted query {qid}")
    return expired


async def _cleanup_ws_connections(expired_qids: list[str] | None = None):
    """Close and remove WebSocket connections for queries that no longer exist."""
    if expired_qids is None:
        stale_qids = set(ws_connections.keys()) - set(queries.keys())
    else:
        stale_qids = set(expired_qids) & set(ws_connections.keys())

    for qid in stale_qids:
        ws_list = ws_connections.get(qid, [])
        for ws in ws_list:
            try:
                await ws.close(code=1000, reason="Query expired")
            except Exception:
                pass
        del ws_connections[qid]
        if ws_list:
            logger.info(f"WS cleanup: closed {len(ws_list)} connections for expired query {qid}")

    # Also clean up any remaining orphaned WS connections
    orphaned = set(ws_connections.keys()) - set(queries.keys())
    for qid in orphaned:
        for ws in ws_connections.get(qid, []):
            try:
                await ws.close(code=1000, reason="Orphaned connection")
            except Exception:
                pass
        del ws_connections[qid]


async def _cleanup_loop():
    """Periodic background task: evict expired queries and orphaned WebSocket connections."""
    while True:
        await asyncio.sleep(QUERY_CLEANUP_INTERVAL)
        try:
            expired = _cleanup_expired_queries()
            if expired:
                await _cleanup_ws_connections(expired)
            else:
                await _cleanup_ws_connections()
        except Exception as e:
            logger.error(f"Cleanup loop error: {e}")


# ── Auth middleware (Fix 5) — pure ASGI, never consumes body ──
from starlette.types import ASGIApp, Scope, Receive, Send


class AuthMiddleware:
    """Pure ASGI middleware that validates Bearer tokens from headers only.

    Injects early — checks scope headers before any body reading.
    Never wraps receive, so FastAPI body parsing is unaffected.
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope['type'] != 'http':
            # Pass through WebSocket and lifespan events
            await self.app(scope, receive, send)
            return

        path = scope.get('path', '')

        # Allow public paths without auth
        if path in PUBLIC_PATHS or any(path.startswith(p) for p in PUBLIC_PREFIXES):
            await self.app(scope, receive, send)
            return

        # If auth is disabled (no API_TOKEN set), allow all requests
        if not AUTH_ENABLED:
            await self.app(scope, receive, send)
            return

        # Read Authorization from scope headers (never touches body)
        headers = dict(scope.get('headers', []))
        auth_bytes = headers.get(b'authorization', b'')
        auth_header = auth_bytes.decode('utf-8', errors='ignore')

        # CORS headers must be present on 401 responses — AuthMiddleware runs
        # before CORSMiddleware in the stack, so we inject them manually.
        _cors_headers = [
            (b'content-type', b'application/json'),
            (b'access-control-allow-origin', b'*'),
            (b'access-control-allow-methods', b'*'),
            (b'access-control-allow-headers', b'*'),
        ]

        if not auth_header.startswith('Bearer '):
            response_body = b'{"error":"Missing or invalid Authorization header. Use: Bearer <token>"}'
            await send({
                'type': 'http.response.start',
                'status': 401,
                'headers': [
                    *_cors_headers,
                    (b'content-length', str(len(response_body)).encode()),
                ],
            })
            await send({
                'type': 'http.response.body',
                'body': response_body,
            })
            return

        token = auth_header[7:]
        if token != API_TOKEN:
            response_body = b'{"error":"Invalid API token"}'
            await send({
                'type': 'http.response.start',
                'status': 401,
                'headers': [
                    *_cors_headers,
                    (b'content-length', str(len(response_body)).encode()),
                ],
            })
            await send({
                'type': 'http.response.body',
                'body': response_body,
            })
            return

        await self.app(scope, receive, send)


async def ws_event_callback(event: str, data: dict):
    """Push an event to all WebSocket connections for this query."""
    qid = data.get('query_id', '')
    if qid in ws_connections:
        dead = []
        # Iterate over a snapshot to avoid skipping elements if the list
        # is mutated concurrently (e.g. by a disconnect during send).
        for ws in list(ws_connections[qid]):
            try:
                await ws.send_json({'event': event, **data})
            except Exception:
                dead.append(ws)
        for ws in dead:
            if ws in ws_connections.get(qid, []):
                ws_connections[qid].remove(ws)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(f"Coordinator starting, worker URLs: {WORKER_URLS}")
    cleanup_task = asyncio.create_task(_cleanup_loop())
    try:
        yield
    finally:
        cleanup_task.cancel()
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="Federated Query Coordinator", lifespan=lifespan)
app.add_middleware(AuthMiddleware)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# Static files
static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static')
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")


# ── Routes ──

@app.post("/register")
async def register_worker(req: dict):
    """Worker registration endpoint."""
    try:
        global_schema.register_worker(req)
        logger.info(f"Worker registered: {req.get('worker_id')} ({req.get('worker_name')})")
        return {"status": "registered"}
    except Exception as e:
        logger.error(f"Registration failed: {e}")
        return JSONResponse(status_code=400, content={"error": str(e)})


@app.get("/api/schema")
async def get_schema():
    """Get global schema view for frontend."""
    return {
        "workers": global_schema.get_workers_summary(),
        "fields": global_schema.get_all_fields_summary(),
    }


@app.post("/api/query")
async def submit_query(req: dict):
    """Submit a natural language query. Returns query ID."""
    user_query = req.get('query', '')
    if not user_query:
        return JSONResponse(status_code=400, content={"error": "Query is required"})

    query_id = str(uuid.uuid4())[:8]
    logger.info(f"Query [{query_id}]: {user_query}")

    # Parse query
    query_ast = parse_query(user_query, global_schema)

    # Generate and rank plans
    workers_summary = {wid: {
        'name': w['worker_name'],
        'row_count': w.get('baseline', {}).get('row_count', 1000),
        'scan_ms': w.get('baseline', {}).get('scan_latency_ms', 200),
    } for wid, w in global_schema.workers.items()}

    plans = await generate_and_rank_plans(query_ast, workers_summary, WORKER_URLS)

    # Store
    queries[query_id] = {
        'query_text': user_query,
        'query_ast': query_ast,
        'plans': plans,
        'status': 'parsed',
        'created_at': time.time(),
    }

    return {
        'query_id': query_id,
        'query_ast': {
            'filters': query_ast.get('filters', []),
            'aggregation': query_ast.get('aggregation'),
            'valid': query_ast.get('valid', True),
            'errors': query_ast.get('errors', []),
        },
        'plans': [{
            'id': p.get('id'),
            'name': p.get('name'),
            'friendly_name': p.get('friendly_name', p.get('name', '')),
            'description': p.get('description', ''),
            'friendly_description': p.get('friendly_description', p.get('description', '')),
            'steps': p.get('steps', []),
            'estimated_cost_ms': p.get('estimated_cost_ms', 0),
            'estimated_egress_bytes': p.get('estimated_egress_bytes', 0),
            'recommended': p.get('recommended', False),
            'stage_costs': p.get('stage_costs', {}),
        } for p in plans],
    }


@app.get("/api/query/{query_id}/plans")
async def get_plans(query_id: str):
    """Get detailed plan comparison for a query."""
    if query_id not in queries:
        return JSONResponse(status_code=404, content={"error": "Query not found"})

    q = queries[query_id]
    plans = q.get('plans', [])

    return {
        'query_id': query_id,
        'query_text': q['query_text'],
        'plans': [{
            'id': p.get('id'),
            'name': p.get('name'),
            'friendly_name': p.get('friendly_name', p.get('name', '')),
            'description': p.get('description', ''),
            'friendly_description': p.get('friendly_description', p.get('description', '')),
            'steps': p.get('steps', []),
            'estimated_cost_ms': p.get('estimated_cost_ms', 0),
            'estimated_egress_bytes': p.get('estimated_egress_bytes', 0),
            'recommended': p.get('recommended', False),
            'stages': p.get('stages', []),
            'stage_costs': p.get('stage_costs', {}),
        } for p in plans],
    }


@app.post("/api/query/{query_id}/execute")
async def execute_query(query_id: str):
    """Execute a query with the recommended plan."""
    if query_id not in queries:
        return JSONResponse(status_code=404, content={"error": "Query not found"})

    q = queries[query_id]
    plans = q.get('plans', [])

    if not plans:
        return JSONResponse(status_code=400, content={"error": "No plans available"})

    # Use recommended (first) plan
    return await _execute_plan(query_id, plans[0], q)


@app.post("/api/query/{query_id}/execute_with_plan/{plan_id}")
async def execute_query_with_plan(query_id: str, plan_id: str):
    """Execute a query with a specific plan."""
    if query_id not in queries:
        return JSONResponse(status_code=404, content={"error": "Query not found"})

    q = queries[query_id]
    plans = q.get('plans', [])

    plan = next((p for p in plans if p.get('id') == plan_id), None)
    if not plan:
        return JSONResponse(status_code=404, content={"error": f"Plan {plan_id} not found"})

    return await _execute_plan(query_id, plan, q)


async def _execute_plan(query_id: str, plan: dict, q: dict):
    """Execute a specific plan for a query."""
    query_ast = q['query_ast']

    # Populate plan stages with actual predicate and aggregation data,
    # fix invalid locations, remove empty filter stages, auto-insert
    # missing intersect stages — all handled by the shared repair function.
    plan = repair_execution_plan(plan, query_ast, set(WORKER_URLS.keys()))

    # Create scheduler with WebSocket callback
    async def event_cb(event, data):
        data['query_id'] = query_id
        await ws_event_callback(event, data)

    scheduler = DAGScheduler(WORKER_URLS, event_cb)
    scheduler.set_query_id(query_id)

    # Execute
    q['status'] = 'running'
    result = await scheduler.execute_plan(plan)
    q['status'] = 'completed'
    q['result'] = result

    # Send final result event
    await ws_event_callback('query_complete', {
        'query_id': query_id,
        'total_elapsed_ms': result.get('total_ms', 0),
        'result': result.get('result', {}),
        'estimated_ms': plan.get('estimated_cost_ms', 0),
        'stage_times': result.get('stage_times', {}),
        'stage_sql': result.get('stage_sql', {}),
    })

    actual_ms = max(result.get('total_ms', 1), 1)
    estimated_ms = max(plan.get('estimated_cost_ms', 1), 1)
    accuracy_pct = min(actual_ms, estimated_ms) / max(actual_ms, estimated_ms) * 100

    return {
        'query_id': query_id,
        'plan_used': plan.get('id'),
        'total_ms': result.get('total_ms', 0),
        'result': result.get('result', {}),
        'estimated_ms': plan.get('estimated_cost_ms', 0),
        'accuracy': f"{accuracy_pct:.1f}%",
        'stage_times': result.get('stage_times', {}),
        'stage_sql': result.get('stage_sql', {}),
        'stage_costs': plan.get('stage_costs', {}),
    }


@app.websocket("/ws/{query_id}")
async def websocket_endpoint(websocket: WebSocket, query_id: str):
    """WebSocket endpoint for real-time query execution events."""
    # WebSocket auth: check token query parameter
    if AUTH_ENABLED:
        token = websocket.query_params.get('token', '')
        if token != API_TOKEN:
            await websocket.close(code=4001, reason="Invalid or missing token")
            return

    await websocket.accept()

    if query_id not in ws_connections:
        ws_connections[query_id] = []
    ws_connections[query_id].append(websocket)

    try:
        while True:
            # Keep connection alive, wait for messages
            data = await websocket.receive_text()
            # Client can send heartbeat or control messages
            if data == 'ping':
                await websocket.send_json({'event': 'pong'})
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        if query_id in ws_connections:
            if websocket in ws_connections[query_id]:
                ws_connections[query_id].remove(websocket)
            if not ws_connections[query_id]:
                del ws_connections[query_id]


@app.get("/")
async def index():
    """Serve the frontend SPA."""
    index_path = os.path.join(static_dir, 'index.html')
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return HTMLResponse("<h1>Federated Query System</h1><p>Frontend not found.</p>")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
