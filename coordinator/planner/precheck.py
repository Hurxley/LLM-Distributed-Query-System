"""
Query Planner — precheck hit counts before plan generation.
"""

import asyncio
import logging

import httpx

logger = logging.getLogger("planner.precheck")


async def run_precheck(query_ast: dict, worker_urls: dict) -> dict:
    """Run /count pre-check on all filter workers in parallel."""
    filters = query_ast.get('filters', [])
    # Group predicates by worker
    worker_predicates = {}
    for f in filters:
        for w in f.get('workers', []):
            if w not in worker_predicates:
                worker_predicates[w] = []
            worker_predicates[w].append({
                'field': f['field'],
                'op': f.get('op', 'eq'),
                'value': f['value'],
            })

    counts = {}

    async def count_worker(wid, predicates):
        url = worker_urls.get(wid)
        if not url:
            return
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(f"{url}/count", json={"predicates": predicates})
                if resp.status_code == 200:
                    data = resp.json()
                    counts[wid] = data.get('count', 0)
        except Exception as e:
            logger.warning(f"Precheck failed for {wid}: {e}")
            counts[wid] = 100  # fallback estimate

    tasks = [count_worker(wid, preds) for wid, preds in worker_predicates.items()]
    await asyncio.gather(*tasks)

    return counts
