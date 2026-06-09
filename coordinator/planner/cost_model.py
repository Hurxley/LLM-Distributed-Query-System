"""
Query Planner — cost model and plan ranking.
"""

import logging

logger = logging.getLogger("planner.cost_model")


def compute_cost(plan: dict, workers_summary: dict, precheck_counts: dict, network_rtt_ms: int = 20) -> dict:
    """Compute the estimated total elapsed time for a plan.

    Realistic cost model:
    - Filter: proportional scan (hit_count/row_count × scan_ms) + network RTT
    - Intersect: O(n) hash set intersection (~5ms for <100K tokens)
    - Aggregate: full table scan (scan_ms) + HMAC per row (~2us each) + network RTT
    - Network: RTT × 2 per round-trip + token data transfer (~0.005ms per token)
    """
    stages = plan.get('stages', [])
    stage_costs = {}
    total_ms = 0

    # Realistic per-row HMAC-SHA256 cost in Python (~1 microsecond on modern CPU)
    HMAC_US_PER_ROW = 1
    # Warm-cache scan is typically 2-3x faster than cold baseline
    WARM_CACHE_FACTOR = 0.4
    # Token size: 64 bytes (32-byte hex digest)
    TOKEN_BYTES = 64
    # LAN bandwidth: 100 Mbps = 12.5 MB/s
    TOKEN_TRANSFER_MS_PER_TOKEN = TOKEN_BYTES / (12.5 * 1024 * 1024 / 1000)  # ~0.005ms each

    for stage in stages:
        sid = stage['id']
        stype = stage['type']
        location = stage['location']

        if stype == 'filter':
            worker_info = workers_summary.get(location, {})
            scan_ms = worker_info.get('scan_ms', 200)
            row_count = worker_info.get('row_count', 1000)

            # Get actual hit count from precheck
            hit_count = row_count
            for key, count in precheck_counts.items():
                if location in key:
                    hit_count = count
                    break

            # DB cost: proportional to rows scanned (MySQL/PostgreSQL use indexes when available)
            if row_count > 0:
                filter_db_ms = int(scan_ms * (hit_count / row_count)) + 5
            else:
                filter_db_ms = scan_ms

            # Network: RTT + token data transfer
            filter_network_ms = network_rtt_ms + hit_count * TOKEN_TRANSFER_MS_PER_TOKEN
            stage_cost = int(round(filter_db_ms + filter_network_ms))
            stage_costs[sid] = {
                'filter_ms': int(round(filter_db_ms)),
                'network_ms': int(round(filter_network_ms)),
                'total_ms': stage_cost,
                'output_tokens': hit_count,
            }

        elif stype == 'intersect':
            depends = stage.get('depends_on', [])
            input_sizes = [stage_costs[d].get('output_tokens', 100) for d in depends if d in stage_costs]
            intersect_ms = 3  # hash set intersection is very fast

            # If intersect is not on coordinator, need to send result
            network_ms = 0
            if location != 'coordinator':
                # Need a round-trip to send tokens + receive result
                network_ms = network_rtt_ms

            # Estimate intersection output size
            if input_sizes:
                min_size = min(input_sizes)
                overlap_ratio = 0.5  # typical overlap between filter results
                output_tokens = int(min_size * overlap_ratio)
            else:
                output_tokens = 0

            stage_cost = int(round(intersect_ms + network_ms))
            stage_costs[sid] = {
                'intersect_ms': intersect_ms,
                'network_ms': network_ms,
                'total_ms': stage_cost,
                'output_tokens': output_tokens,
            }

        elif stype == 'aggregate':
            depends = stage.get('depends_on', [])
            token_count = 0
            intersect_colocated = False
            for d in depends:
                if d in stage_costs:
                    token_count = stage_costs[d].get('output_tokens', 0)
                # Check if preceding intersect stage is at the same location
                dep_stage = next((s for s in stages if s['id'] == d), None)
                if dep_stage and dep_stage['type'] == 'intersect' and dep_stage['location'] == location:
                    intersect_colocated = True

            worker_info = workers_summary.get(location, {})
            worker_scan_ms = worker_info.get('scan_ms', 500)
            worker_row_count = worker_info.get('row_count', 30000)

            # DB cost: full table scan (warm cache = faster than baseline)
            agg_db_ms = int(worker_scan_ms * WARM_CACHE_FACTOR)
            # HMAC computation: per-row HMAC-SHA256 to match tokens
            agg_hmac_ms = int(worker_row_count * HMAC_US_PER_ROW / 1000)
            # Network: result must return to coordinator regardless.
            # If intersect is colocated, tokens are already local — no token
            # transfer needed, but still pay RTT for request + result return.
            if intersect_colocated:
                agg_network_ms = network_rtt_ms
            else:
                agg_network_ms = network_rtt_ms + token_count * TOKEN_TRANSFER_MS_PER_TOKEN
            stage_cost = int(round(agg_db_ms + agg_hmac_ms + agg_network_ms))

            stage_costs[sid] = {
                'agg_db_ms': agg_db_ms,
                'agg_hmac_ms': agg_hmac_ms,
                'agg_network_ms': int(round(agg_network_ms)),
                'total_ms': stage_cost,
                'output_tokens': 1,  # aggregate returns scalar, not tokens
            }

        elif stype == 'compute':
            stage_costs[sid] = {
                'compute_ms': 1,
                'network_ms': 0,
                'total_ms': 1,
            }

    # Add human-readable labels to each stage cost
    for sid, costs in stage_costs.items():
        labels = []
        if 'filter_ms' in costs:
            labels.append(f"数据库扫描: {costs['filter_ms']}ms")
        if 'network_ms' in costs and costs['network_ms'] > 0:
            labels.append(f"网络传输(token): {costs['network_ms']}ms")
        if 'intersect_ms' in costs:
            labels.append(f"求交计算: {costs['intersect_ms']}ms")
        if 'agg_db_ms' in costs:
            labels.append(f"数据库全表扫描: {costs['agg_db_ms']}ms")
        if 'agg_hmac_ms' in costs:
            labels.append(f"HMAC身份匹配: {costs['agg_hmac_ms']}ms")
        if 'agg_network_ms' in costs and costs['agg_network_ms'] > 0:
            labels.append(f"网络往返(请求+结果回传): {costs['agg_network_ms']}ms")
        if 'compute_ms' in costs:
            labels.append(f"主控汇总: {costs['compute_ms']}ms")
        costs['breakdown_label'] = "，".join(labels)

    # Now compute actual total along DAG (simplified: sum all stages, account for concurrency)
    # Identify concurrent groups
    concurrent_groups = {}
    sequential_stages = []
    for stage in stages:
        sid = stage['id']
        if 'concurrent_group' in stage:
            gid = stage['concurrent_group']
            if gid not in concurrent_groups:
                concurrent_groups[gid] = []
            concurrent_groups[gid].append(sid)
        else:
            sequential_stages.append(sid)

    # For concurrent groups, take max stage cost
    total_ms = 0
    for gid, sids in concurrent_groups.items():
        valid_sids = [sid for sid in sids if sid in stage_costs]
        if valid_sids:
            group_max = max(stage_costs[sid]['total_ms'] for sid in valid_sids)
            total_ms += group_max

    # For sequential stages, sum them
    for sid in sequential_stages:
        if sid in stage_costs:
            total_ms += stage_costs[sid]['total_ms']

    # Estimate egress data size
    total_egress = 0
    for stage in stages:
        if stage.get('type') in ('filter', 'intersect'):
            sid = stage['id']
            tokens = stage_costs.get(sid, {}).get('output_tokens', 0)
            if stage['location'] != 'coordinator':
                total_egress += tokens * 64  # 64 bytes per hex token

    plan['estimated_cost_ms'] = int(round(total_ms))
    plan['estimated_egress_bytes'] = int(total_egress)
    plan['stage_costs'] = stage_costs

    return plan


def rank_plans(plans: list[dict]) -> list[dict]:
    """Sort plans by estimated total time, mark the best."""
    for plan in plans:
        if 'estimated_cost_ms' not in plan:
            plan['estimated_cost_ms'] = 9999
    plans.sort(key=lambda p: p['estimated_cost_ms'])
    if plans:
        plans[0]['recommended'] = True
    return plans
