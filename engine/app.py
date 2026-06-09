"""
Worker Engine - FastAPI application.

Exposes 4 endpoints:
  - GET  /health     - Health check
  - POST /count      - Hit count pre-check (returns scalar)
  - POST /filter     - Filter and return blinded tokens
  - POST /aggregate  - Aggregate computation (returns scalar)
"""

import os
import sys
import time
import json
import asyncio
import logging
from contextlib import asynccontextmanager

import yaml
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

# Add engine directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tokenizer import tokenize, get_salt
from db import get_connection, get_db_type, execute_query, execute_scalar, quote_identifier
from sql_builder import (
    load_mapping,
    get_field_by_logical,
    get_token_field,
    translate_value_to_physical,
    build_where_clause,
    build_filter_query,
    build_count_query,
    build_aggregate_query,
)
from egress_filter import wrap_response

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] [%(levelname)s] %(message)s')
logger = logging.getLogger("worker")

# ── Globals ──
worker_id = None
worker_name = None
coordinator_url = None
mapping = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: register with coordinator. Shutdown: cleanup."""
    global worker_id, worker_name, coordinator_url, mapping

    mapping = load_mapping()
    worker_id = mapping['worker_id']
    worker_name = mapping['worker_name']
    coordinator_url = os.environ.get('COORDINATOR_URL', 'http://coordinator:8000')

    # Self-check database connectivity
    retries = 5
    while retries > 0:
        try:
            conn = get_connection()
            logger.info(f"Database connection OK ({get_db_type()})")
            break
        except Exception as e:
            retries -= 1
            logger.warning(f"DB connection retry ({retries} left): {e}")
            await asyncio.sleep(3)

    # Baseline test
    salt = get_salt()
    try:
        db_type = get_db_type()
        table = mapping.get('table', mapping.get('db_config', {}).get('database', 'data'))
        q_table = quote_identifier(table, db_type)
        start = time.time()
        row_count = execute_scalar(f"SELECT COUNT(*) FROM {q_table}")
        scan_ms = int((time.time() - start) * 1000)
        logger.info(f"Baseline: {row_count} rows, full scan {scan_ms}ms")
    except Exception as e:
        logger.warning(f"Baseline test failed: {e}")
        row_count = 0
        scan_ms = 100

    # Build registration payload (only logical fields!)
    fields_payload = []
    for field in mapping.get('fields', []):
        entry = {
            'logical': field['logical'],
            'alias': field.get('alias', []),
            'secret': field.get('secret', False),
        }
        if 'mapping' in field:
            entry['type'] = 'enum'
            entry['values'] = list(field['mapping'].values())
        elif field.get('derived'):
            entry['type'] = 'derived'
            entry['values'] = ['numeric']
        elif field.get('secret'):
            entry['type'] = 'token'
        else:
            entry['type'] = 'text'
        fields_payload.append(entry)

    register_body = {
        'worker_id': worker_id,
        'worker_name': worker_name,
        'fields': fields_payload,
        'baseline': {
            'row_count': row_count,
            'scan_latency_ms': scan_ms,
            'token_lookup_us': 500,
        }
    }

    # Register with coordinator (retry)
    for attempt in range(30):
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(f"{coordinator_url}/register", json=register_body)
                if resp.status_code == 200:
                    logger.info(f"Registered with coordinator as '{worker_id}'")
                    break
        except Exception as e:
            logger.debug(f"Registration attempt {attempt+1}/30: {e}")
        await asyncio.sleep(2)
    else:
        logger.error("Failed to register with coordinator after 30 attempts!")

    yield


app = FastAPI(
    title=f"Worker Engine",
    lifespan=lifespan,
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ── Egress filter: checked manually in each endpoint's return ──
def egress_check(data: dict) -> dict:
    """Run egress filter check before returning response."""
    filtered = wrap_response(data)
    if "error" in filtered and filtered["error"] == "PII_LEAK_BLOCKED":
        logger.critical("ALERT: Egress filter blocked potential PII leak!")
        logger.critical(f"Detail: {filtered.get('detail', 'unknown')}")
    return filtered


def _should_debug_sql() -> bool:
    """Check if the DEBUG_SQL flag is enabled.

    When enabled, physical SQL is included in API responses for debugging.
    Default OFF — physical schema should not leave the worker.
    """
    return os.environ.get('DEBUG_SQL', '').lower() in ('true', '1', 'yes')


def _build_logical_display_sql(predicates: list[dict], stype: str = 'filter', agg_field: str = None) -> str:
    """Build a simulated SQL statement using only logical field names from mapping.yaml.

    Uses logical field names (e.g. person_token, research_field) and table names
    from the mapping file — never exposes real physical column names.
    """
    mapping = load_mapping()
    table_name = mapping.get('table', mapping.get('db_config', {}).get('database', 'unknown'))

    # Find the secret token field — use its logical name
    try:
        tf = get_token_field(mapping)
        token_display = tf.get('logical', 'person_token')
    except ValueError:
        token_display = 'person_token'

    if stype == 'filter':
        where_parts = []
        for pred in predicates:
            field_name = pred.get('field', '?')
            op = pred.get('op', 'eq')
            value = pred.get('value', '?')

            op_map = {'eq': '=', 'neq': '!=', 'gt': '>', 'gte': '>=', 'lt': '<', 'lte': '<=', 'in': 'IN'}
            sql_op = op_map.get(op, '=')

            if isinstance(value, str):
                where_parts.append(f"{field_name} {sql_op} '{value}'")
            else:
                where_parts.append(f"{field_name} {sql_op} {value}")

        where_str = ' AND '.join(where_parts)
        if where_str:
            return f"SELECT {token_display} FROM {table_name} WHERE {where_str}"
        else:
            return f"SELECT {token_display} FROM {table_name}"

    elif stype == 'aggregate':
        return f"SELECT {token_display}, {agg_field or '?'} FROM {table_name}"

    return ''


# ── Endpoints ──

@app.get("/health")
async def health():
    return {"status": "ok", "worker_id": worker_id}


@app.post("/count")
async def count(req: dict):
    """Return hit count for given predicates (scalar only, no token output)."""
    predicates = req.get('predicates', [])
    logger.info(f"POST /count - predicates: {json.dumps(predicates, ensure_ascii=False)}")

    sql, params = build_count_query(predicates)
    logger.info(f"SQL: {sql} | params: {params}")

    try:
        count_val = execute_scalar(sql, params)
        return {"count": count_val or 0}
    except Exception as e:
        logger.error(f"Count query failed: {e}")
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/filter")
async def filter_endpoint(req: dict):
    """Filter and return blinded tokens (HMAC-SHA256 hashes)."""
    predicates = req.get('predicates', [])
    logger.info(f"POST /filter - predicates: {json.dumps(predicates, ensure_ascii=False)}")

    db_type = get_db_type()
    mapping = load_mapping()

    # Find the secret token field
    token_field = get_token_field()

    table = mapping.get('table', mapping.get('db_config', {}).get('database', 'data'))

    # Build WHERE clause
    where_clause, where_params = build_where_clause(predicates)

    q_col = quote_identifier(token_field['physical'], db_type)
    q_table = quote_identifier(table, db_type)
    sql = f"SELECT {q_col} AS raw_id FROM {q_table} {where_clause}" if where_clause else f"SELECT {q_col} AS raw_id FROM {q_table}"

    logger.info(f"SQL: {sql} | params: {where_params}")

    try:
        rows = execute_query(sql, where_params)
    except Exception as e:
        logger.error(f"Filter query failed: {e}")
        return JSONResponse(status_code=500, content={"error": str(e)})

    # Tokenize each raw ID client-side
    tokens = []
    for row in rows:
        raw_id = str(row.get('raw_id', row.get(token_field['physical'], '')))
        if raw_id:
            try:
                t = tokenize(raw_id)
                tokens.append(t)
            except Exception as e:
                logger.error(f"Tokenization error: {e}")

    # Build logical display SQL (all logical field names, no real column names)
    display_sql = _build_logical_display_sql(predicates, 'filter')

    logger.info(f"Response: {len(tokens)} tokens")
    response = {"tokens": tokens, "count": len(tokens), "sql": display_sql}
    if _should_debug_sql():
        response["display_sql"] = display_sql
        response["physical_sql"] = sql
        response["params"] = list(where_params)
    return egress_check(response)


@app.post("/aggregate")
async def aggregate(req: dict):
    """Aggregate computation using token list for blind matching.

    Receives tokens from coordinator (intersection result).
    Matches against local data via HMAC.  For MySQL / PostgreSQL the HMAC
    is pushed to the database; for SQLite it is computed row-by-row in Python.
    Returns ONLY scalar (sum, count) — never row-level data.
    """
    tokens = req.get('tokens', [])
    agg_field = req.get('agg_field', 'monthly_income')
    agg_func = req.get('agg_func', 'avg')

    logger.info(f"POST /aggregate - {len(tokens)} tokens, field={agg_field}, func={agg_func}")

    db_type = get_db_type()
    mapping = load_mapping()

    # Build the aggregate query.  For MySQL / PostgreSQL this pushes HMAC
    # computation into the database so we only transfer matching rows.
    sql, params, db_side_hmac = build_aggregate_query(tokens, agg_field, agg_func)

    # Build logical display SQL (all logical field names, no real column names)
    display_sql = _build_logical_display_sql([], 'aggregate', agg_field=agg_field)

    try:
        rows = execute_query(sql, params)
    except Exception as e:
        logger.error(f"Aggregate query failed: {e}")
        return JSONResponse(status_code=500, content={"error": str(e)})

    if db_side_hmac:
        # MySQL / PostgreSQL — the DB already computed the aggregates on
        # matching rows.  No Python-side HMAC loop needed.
        row = rows[0] if rows else {}
        total_sum = float(row.get('total_sum', 0) or 0)
        total_count = int(row.get('total_count', 0) or 0)
        non_zero_count = int(row.get('non_zero_count', 0) or 0)
        total_min = float(row.get('total_min', 0) or 0)
        total_max = float(row.get('total_max', 0) or 0)
        matched = total_count
    else:
        # SQLite — no native HMAC.  Match tokens row-by-row in Python.
        token_set = set(tokens) if tokens else None

        total_sum = 0.0
        total_count = 0
        non_zero_count = 0
        total_min = None
        total_max = None
        matched = 0

        for row in rows:
            raw_id = str(row.get('raw_id', ''))
            agg_val = row.get('agg_val', 0)
            if agg_val is None:
                agg_val = 0
            agg_val = float(agg_val)

            try:
                t = tokenize(raw_id)
            except Exception:
                continue

            if token_set is None or t in token_set:
                total_sum += agg_val
                total_count += 1
                if agg_val != 0:
                    non_zero_count += 1
                if total_min is None or agg_val < total_min:
                    total_min = agg_val
                if total_max is None or agg_val > total_max:
                    total_max = agg_val
                matched += 1

        if total_min is None:
            total_min = 0.0
        if total_max is None:
            total_max = 0.0

    # Compute the requested aggregation
    if agg_func == 'count':
        result_val = total_count
    elif agg_func == 'sum':
        result_val = total_sum
    elif agg_func == 'min':
        result_val = total_min
    elif agg_func == 'max':
        result_val = total_max
    elif agg_func == 'avg':
        # Use non_zero_count for sparse fields like annual_bonus (only non-zero in Dec).
        # Falls back to total_count when all values are genuinely zero.
        divisor = non_zero_count if non_zero_count > 0 else total_count
        result_val = total_sum / divisor if divisor > 0 else 0
    else:
        result_val = total_sum  # default to sum

    logger.info(f"Response: func={agg_func}, value={result_val}, sum={total_sum:.2f}, count={total_count}, matched={matched}, db_hmac={db_side_hmac}")
    response = {
        "sum": total_sum,
        "count": total_count,
        "min": total_min,
        "max": total_max,
        "value": result_val,
        "func": agg_func,
        "sql": display_sql,
    }
    if _should_debug_sql():
        response["display_sql"] = display_sql
        response["physical_sql"] = sql
    return egress_check(response)
