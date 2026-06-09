"""
DAG Scheduler — executes a plan by dispatching stages to workers,
managing dependencies, and pushing WebSocket events.

Key behaviors:
- Concurrent stages execute in parallel (gather)
- Dependent stages wait for predecessors
- WebSocket events pushed for each stage transition
"""

import asyncio
import inspect
import logging
import time
import json
from typing import Optional, Callable

import httpx

logger = logging.getLogger("scheduler")

# WebSocket event callback type
EventCallback = Callable[[str, dict], None]


class DAGScheduler:
    """Executes a query plan DAG with WebSocket event streaming."""

    def __init__(self, worker_urls: dict[str, str], event_callback: Optional[EventCallback] = None):
        self.worker_urls = worker_urls
        self.event_callback = event_callback or (lambda e, d: None)
        self.results = {}       # stage_id -> result data
        self.stage_times = {}   # stage_id -> elapsed_ms
        self.query_id = ""

    def set_query_id(self, qid: str):
        self.query_id = qid

    async def _push_event(self, event: str, data: dict):
        """Send a WebSocket event."""
        data['timestamp'] = int(time.time() * 1000)
        payload = {'event': event, **data}
        if inspect.iscoroutinefunction(self.event_callback):
            await self.event_callback(event, payload)
        else:
            self.event_callback(event, payload)

    async def execute_plan(self, plan: dict) -> dict:
        """Execute a plan DAG and return the final result."""
        stages = plan.get('stages', [])
        completed = set()
        stage_objects = {s['id']: s for s in stages}
        total_start = time.time()

        await self._push_event('execution_start', {
            'plan_id': plan.get('id', 'P1'),
            'plan_name': plan.get('name', ''),
            'estimated_ms': plan.get('estimated_cost_ms', 0),
        })

        while len(completed) < len(stages):
            # Find stages ready to execute (all dependencies met)
            ready = []
            for stage in stages:
                sid = stage['id']
                if sid in completed:
                    continue
                depends = stage.get('depends_on', [])
                if all(d in completed for d in depends):
                    ready.append(stage)

            if not ready:
                logger.error(f"Deadlock detected! Completed: {completed}, remaining: {[s['id'] for s in stages if s['id'] not in completed]}")
                break

            # Group by concurrent_group (if specified)
            concurrent = [s for s in ready if 'concurrent_group' in s]
            sequential = [s for s in ready if 'concurrent_group' not in s]

            # Execute all concurrent stages in parallel first
            if concurrent:
                groups = {}
                for s in concurrent:
                    gid = s['concurrent_group']
                    if gid not in groups:
                        groups[gid] = []
                    groups[gid].append(s)

                for gid, group_stages in groups.items():
                    tasks = [self._execute_stage(s) for s in group_stages]
                    await asyncio.gather(*tasks)
                    for s in group_stages:
                        completed.add(s['id'])

            # Then execute sequential stages one by one
            for s in sequential:
                await self._execute_stage(s)
                completed.add(s['id'])

        total_ms = round((time.time() - total_start) * 1000, 1)

        # Find the compute/aggregate stage result for final output (search by type, not hardcoded ID)
        final_result = {}
        for stage in stages:
            if stage['type'] == 'compute':
                sid = stage['id']
                if sid in self.results:
                    final_result = self.results[sid]
                    break
        # Fallback: try aggregate stage if no compute result found
        if not final_result:
            for stage in stages:
                if stage['type'] == 'aggregate':
                    sid = stage['id']
                    if sid in self.results:
                        final_result = self.results[sid]
                        break

        # Build SQL log from stage results
        stage_sql = {}
        for sid, result in self.results.items():
            if 'sql' in result:
                so = stage_objects.get(sid, {})
                stage_sql[sid] = {
                    'sql': result.get('display_sql', '') or result.get('sql', ''),
                    'location': so.get('location', ''),
                    'type': so.get('type', ''),
                }

        await self._push_event('execution_complete', {
            'total_ms': total_ms,
            'stage_times': self.stage_times,
        })

        return {
            'success': True,
            'total_ms': total_ms,
            'result': final_result,
            'stage_times': self.stage_times,
            'stage_sql': stage_sql,
        }

    async def _execute_stage(self, stage: dict) -> dict:
        """Execute a single stage."""
        sid = stage['id']
        stype = stage['type']
        location = stage['location']

        await self._push_event('stage_start', {
            'stage_id': sid,
            'stage_type': stype,
            'location': location,
        })

        start = time.time()

        try:
            if stype == 'filter':
                result = await self._do_filter(stage)
            elif stype == 'intersect':
                result = await self._do_intersect(stage)
            elif stype == 'aggregate':
                result = await self._do_aggregate(stage)
            elif stype == 'compute':
                result = self._do_compute(stage)
            else:
                result = {'error': f'Unknown stage type: {stype}'}

        except Exception as e:
            logger.error(f"Stage {sid} ({stype}) failed: {e}")
            result = {'error': str(e)}
            await self._push_event('stage_error', {
                'stage_id': sid,
                'error': str(e),
            })

        elapsed_ms = round((time.time() - start) * 1000, 1)
        self.results[sid] = result
        self.stage_times[sid] = elapsed_ms

        await self._push_event('stage_complete', {
            'stage_id': sid,
            'stage_type': stype,
            'location': location,
            'elapsed_ms': elapsed_ms,
            'result_summary': self._summarize_result(result),
        })

        return result

    async def _do_filter(self, stage: dict) -> dict:
        """Execute a filter stage on a worker."""
        location = stage['location']
        url = self.worker_urls.get(location)
        if not url:
            raise ValueError(f"Unknown worker: {location}")

        # Collect predicates for this worker
        predicates = stage.get('predicates', [])
        # If no explicit predicates, use intersection tokens from upstream if this is push-down
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(f"{url}/filter", json={"predicates": predicates})
            resp.raise_for_status()
            data = resp.json()
            return {'tokens': data.get('tokens', []), 'count': data.get('count', 0), 'sql': data.get('sql', ''), 'display_sql': data.get('display_sql', ''), 'params': data.get('params', [])}

    async def _do_intersect(self, stage: dict) -> dict:
        """Compute token intersection."""
        location = stage['location']
        depends = stage.get('depends_on', [])

        # Collect token sets from upstream stages
        token_sets = []
        for d in depends:
            if d in self.results:
                tokens = self.results[d].get('tokens', [])
                token_sets.append(set(tokens))

        if not token_sets:
            return {'tokens': [], 'count': 0}

        if location == 'coordinator':
            # Center-side intersection: compute set intersection of all upstream token sets
            intersection = token_sets[0]
            for ts in token_sets[1:]:
                intersection = intersection & ts
            tokens = list(intersection)
            return {'tokens': tokens, 'count': len(tokens)}
        else:
            # Push-down: union all upstream tokens and pass them to the worker.
            # The worker's /aggregate endpoint already does local matching against its
            # own data — this is the "intersection" for push-down topologies.
            # We skip coordinator-side set intersection to avoid an unnecessary round-trip.
            all_tokens: list[str] = []
            seen: set[str] = set()
            for ts in token_sets:
                for t in ts:
                    if t not in seen:
                        seen.add(t)
                        all_tokens.append(t)
            logger.info(f"Push-down intersect @ {location}: unioned {len(all_tokens)} unique tokens "
                        f"from {len(token_sets)} upstream sets (coordinator intersection skipped)")
            return {'tokens': all_tokens, 'count': len(all_tokens)}

    async def _do_aggregate(self, stage: dict) -> dict:
        """Execute an aggregate stage on a worker."""
        location = stage['location']
        url = self.worker_urls.get(location)
        if not url:
            raise ValueError(f"Unknown worker: {location}")

        depends = stage.get('depends_on', [])
        # Collect intersection tokens from upstream
        all_tokens = []
        for d in depends:
            if d in self.results:
                all_tokens.extend(self.results[d].get('tokens', []))

        agg_field = stage.get('agg_field', 'monthly_income')
        agg_func = stage.get('agg_func', 'avg')

        logger.info(f"Scheduler _do_aggregate: sending {len(all_tokens)} tokens to {location}, field={agg_field}, func={agg_func}")

        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(f"{url}/aggregate", json={
                "tokens": all_tokens,
                "agg_field": agg_field,
                "agg_func": agg_func,
            })
            resp.raise_for_status()
            data = resp.json()
            logger.info(f"Scheduler _do_aggregate result: count={data.get('count', 0)}, value={data.get('value', 0)}")
            return {
                'sum': data.get('sum', 0),
                'count': data.get('count', 0),
                'min': data.get('min'),
                'max': data.get('max'),
                'value': data.get('value', 0),
                'func': data.get('func', agg_func),
                'sql': data.get('sql', ''),
                'display_sql': data.get('display_sql', ''),
            }

    def _do_compute(self, stage: dict) -> dict:
        """Compute final result from aggregate stage output."""
        depends = stage.get('depends_on', [])
        total_sum = 0
        total_count = 0
        total_min = None
        total_max = None
        agg_func = stage.get('agg_func', 'avg')

        for d in depends:
            if d in self.results:
                r = self.results[d]
                total_sum += r.get('sum', 0)
                total_count += r.get('count', 0)
                r_min = r.get('min')
                r_max = r.get('max')
                if r_min is not None:
                    if total_min is None or r_min < total_min:
                        total_min = r_min
                if r_max is not None:
                    if total_max is None or r_max > total_max:
                        total_max = r_max

        if agg_func == 'count':
            final_value = total_count
        elif agg_func == 'sum':
            final_value = total_sum
        elif agg_func == 'min':
            final_value = total_min if total_min is not None else 0
        elif agg_func == 'max':
            final_value = total_max if total_max is not None else 0
        elif agg_func == 'avg':
            # Use worker's pre-computed value (handles sparse/non-zero avg correctly)
            # Workers compute avg with non_zero_count for fields like annual_bonus.
            # Fall back to sum/count only if no worker value is available.
            worker_values = [
                r.get('value') for d in depends
                if (r := self.results.get(d)) and r.get('value') is not None
            ]
            if worker_values:
                final_value = round(sum(worker_values) / len(worker_values), 2)
            else:
                final_value = round(total_sum / total_count, 2) if total_count > 0 else 0
        else:
            final_value = total_sum

        return {
            'sum': total_sum,
            'count': total_count,
            'min': total_min if total_min is not None else 0,
            'max': total_max if total_max is not None else 0,
            'value': final_value,
            'func': agg_func,
        }

    def _summarize_result(self, result: dict) -> str:
        """Create a human-readable summary of a stage result."""
        if 'tokens' in result:
            return f"{result.get('count', 0)} tokens"
        if 'sum' in result and 'count' in result:
            return f"sum={result['sum']:.0f}, count={result['count']}"
        if 'avg' in result:
            return f"avg={result['avg']:.2f}"
        return "OK"
