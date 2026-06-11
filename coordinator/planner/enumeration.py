"""
Query Planner — exhaustive and rule-based plan enumeration.

These two functions generate candidate execution-plan topologies without
any LLM involvement.  They can run even when no LLM API is configured.
"""

import itertools
import logging

from .validation import _validate_and_repair_plan

logger = logging.getLogger("planner.enumeration")


def _optimize_coordinator_count(plan: dict, query_ast: dict) -> dict:
    """Remove the aggregate stage when COUNT(person_token) — the coordinator
    can count tokens directly without sending them to any worker."""
    agg = query_ast.get('aggregation') or {}
    if not (agg.get('func') == 'count' and agg.get('field') == 'person_token'):
        return plan

    stages = plan.get('stages', [])
    # Find aggregate and compute stages
    agg_stage = next((s for s in stages if s['type'] == 'aggregate'), None)
    compute_stage = next((s for s in stages if s['type'] == 'compute'), None)
    if not agg_stage or not compute_stage:
        return plan

    # Reroute: compute stage now directly depends on what aggregate depended on
    compute_stage['depends_on'] = agg_stage.get('depends_on', [])
    # R inherits agg_func / agg_field for display; mark as coordinator-side count
    compute_stage['agg_func'] = agg.get('func')
    compute_stage['agg_field'] = agg.get('field')
    compute_stage['coordinator_count'] = True

    # Remove aggregate stage
    plan['stages'] = [s for s in stages if s['type'] != 'aggregate']
    plan['name'] = plan.get('name', '').replace('聚合节点计算统计值', '主控直接计数')
    plan['description'] = plan.get('description', '').replace(
        '由聚合节点计算统计值', '由主控直接统计Token数量'
    ).replace(
        '聚合节点本地完成求交和统计', '聚合节点本地完成求交，主控直接统计数量'
    ).replace(
        '聚合节点计算统计值', '主控直接统计Token数量'
    )

    logger.info(f"Optimized plan {plan.get('id')}: coordinator-side COUNT, removed aggregate stage")
    return plan


def _enumerate_all_plans(query_ast: dict, workers_summary: dict, precheck_counts: dict) -> list[dict]:
    """Exhaustively enumerate ALL theoretically valid plan topologies.

    For N filter workers + 1 aggregate worker, generates all combinations of:
      - Filter topology: concurrent (1), serial (all permutations)
      - Intersect location: coordinator, each filter worker, aggregate worker

    Filters out theoretically impossible combinations (e.g., filter on worker
    with no predicates, aggregate on coordinator).
    """
    filters = query_ast.get('filters', [])
    agg = query_ast.get('aggregation') or {}

    # Determine filter workers that actually have predicates
    filter_workers = {}
    for f in filters:
        for w in f.get('workers', []):
            if w not in filter_workers:
                filter_workers[w] = []
            filter_workers[w].append(f)

    filter_wids = sorted(filter_workers.keys())
    agg_workers = agg.get('workers', [])

    # Determine aggregate worker
    if agg_workers:
        agg_wid = agg_workers[0]
    else:
        agg_wid = 'worker_c'

    plans = []
    plan_idx = 0

    def next_id():
        nonlocal plan_idx
        plan_idx += 1
        return f"P{plan_idx}"

    # ── Case 1: No filter workers → just aggregate → compute ──
    if len(filter_wids) == 0:
        plans.append({
            "id": next_id(),
            "name": "直接统计 — 无需筛选",
            "description": "没有筛选条件，直接在聚合节点计算统计值",
            "stages": [
                {"id": "C", "type": "aggregate", "location": agg_wid},
                {"id": "R", "type": "compute", "location": "coordinator", "depends_on": ["C"]},
            ]
        })
        return plans

    # ── Case 2: Single filter worker → no intersect needed ──
    if len(filter_wids) == 1:
        fw = filter_wids[0]
        wname = workers_summary.get(fw, {}).get('name', fw)
        plans.append({
            "id": next_id(),
            "name": f"单节点查询 — 仅查{wname}",
            "description": f"仅在{wname}中筛选，结果直接传入聚合节点计算统计数据",
            "stages": [
                {"id": "F1", "type": "filter", "location": fw},
                {"id": "C", "type": "aggregate", "location": agg_wid, "depends_on": ["F1"]},
                {"id": "R", "type": "compute", "location": "coordinator", "depends_on": ["C"]},
            ]
        })
        return plans

    # ── Case 3: 2+ filter workers → enumerate all topologies ──

    # All possible intersect locations
    intersect_locations = ['coordinator']
    for w in filter_wids:
        if w not in intersect_locations:
            intersect_locations.append(w)
    if agg_wid not in intersect_locations:
        intersect_locations.append(agg_wid)

    # ── 3a. Concurrent filters (all filters run in parallel) ──
    for iloc in intersect_locations:
        pid = next_id()
        stages = []
        fids = []
        for i, fw in enumerate(filter_wids):
            fid = f"F{i+1}"
            fids.append(fid)
            stages.append({
                "id": fid, "type": "filter", "location": fw,
                "concurrent_group": 1,
            })

        iid = "I"
        stages.append({
            "id": iid, "type": "intersect", "location": iloc,
            "depends_on": list(fids),
        })

        cid = "C"
        stages.append({
            "id": cid, "type": "aggregate", "location": agg_wid,
            "depends_on": [iid],
        })

        stages.append({
            "id": "R", "type": "compute", "location": "coordinator",
            "depends_on": [cid],
        })

        # Build descriptive name
        if iloc == 'coordinator':
            name = "并行查询 — 各数据源同时过滤，中心求交"
            desc = "所有数据源同时筛选，中心节点比对求交后，由聚合节点计算统计值"
        elif iloc == agg_wid:
            name = "数据下推 — 并行过滤，聚合节点本地求交"
            desc = "各数据源并发筛选后，将token直接发送到聚合节点本地完成求交和统计，省去中心往返"
        else:
            iloc_name = workers_summary.get(iloc, {}).get('name', iloc)
            name = f"并行查询 — 同时过滤，{iloc_name}本地求交"
            desc = f"所有数据源并发筛选，求交在{iloc_name}本地执行，再由聚合节点计算统计值"

        plans.append({
            "id": pid, "name": name, "description": desc,
            "stages": stages,
        })

    # ── 3b. Serial permutations (each permutation = different filter order) ──
    for perm in itertools.permutations(filter_wids):
        perm_list = list(perm)
        if len(perm_list) < 2:
            continue

        for iloc in intersect_locations:
            pid = next_id()
            stages = []
            fids = []
            for i, fw in enumerate(perm_list):
                fid = f"S{i+1}"
                fids.append(fid)
                deps = [fids[i-1]] if i > 0 else []
                stages.append({
                    "id": fid, "type": "filter", "location": fw,
                    "depends_on": list(deps),
                })

            iid = "I"
            stages.append({
                "id": iid, "type": "intersect", "location": iloc,
                "depends_on": list(fids),
            })

            cid = "C"
            stages.append({
                "id": cid, "type": "aggregate", "location": agg_wid,
                "depends_on": [iid],
            })

            stages.append({
                "id": "R", "type": "compute", "location": "coordinator",
                "depends_on": [cid],
            })

            # Build descriptive name
            chain_names = [workers_summary.get(w, {}).get('name', w) for w in perm_list]
            chain_str = "→".join(chain_names)

            if iloc == 'coordinator':
                name = f"串行查询 — {chain_str}，中心求交"
                desc = f"依次在{'、'.join(chain_names)}中筛选，利用上游结果缩小下游范围，中心求交后聚合"
            elif iloc == agg_wid:
                name = f"数据下推串行 — {chain_str}，聚合节点求交"
                desc = f"依次筛选，求交和聚合均在聚合节点本地完成，省去中心往返"
            else:
                iloc_name = workers_summary.get(iloc, {}).get('name', iloc)
                name = f"串行查询 — {chain_str}，{iloc_name}本地求交"
                desc = f"依次在{'、'.join(chain_names)}中筛选，求交在{iloc_name}本地执行"

            plans.append({
                "id": pid, "name": name, "description": desc,
                "stages": stages,
            })

    # Validate and repair all enumerated plans
    valid_worker_ids = set(workers_summary.keys())
    plans = [_validate_and_repair_plan(p, valid_worker_ids, query_ast) for p in plans]

    logger.info(f"Enumerated {len(plans)} candidate plan topologies "
                f"(filters={filter_wids}, agg={agg_wid}, intersect_locs={intersect_locations})")

    return plans


def _generate_plans_rule_based(query_ast: dict, workers_summary: dict, precheck_counts: dict) -> list[dict]:
    """Rule-based plan generation as fallback.

    Generates a fixed set of plan topologies based on which workers are involved.
    Used when LLM is unavailable, and also called by ``generate_plans_with_llm``
    as a supplement when the LLM returns too few plans.
    """
    filters = query_ast.get('filters', [])
    agg = query_ast.get('aggregation') or {}

    # Determine which workers are involved
    filter_workers = {}
    for f in filters:
        for w in f.get('workers', []):
            if w not in filter_workers:
                filter_workers[w] = []
            filter_workers[w].append(f)

    agg_workers = agg.get('workers', [])
    worker_ids = sorted(set(list(filter_workers.keys()) + agg_workers))

    plans = []

    if len(worker_ids) == 0:
        plans.append({
            "id": "P1", "name": "单步查询",
            "description": "无需筛选，直接聚合",
            "stages": [
                {"id": "C", "type": "aggregate", "location": agg_workers[0] if agg_workers else "coordinator", "concurrent_group": 1},
                {"id": "R", "type": "compute", "location": "coordinator", "depends_on": ["C"]},
            ]
        })
        return plans

    # Separate filter-only workers from aggregate worker
    filter_only = [w for w in worker_ids if w in filter_workers and w not in agg_workers]
    agg_only = [w for w in worker_ids if w in agg_workers and w not in filter_workers]
    both = [w for w in worker_ids if w in filter_workers and w in agg_workers]

    filter_wids = sorted(set(filter_only + both))

    # Plan 1: Concurrent filters + center intersect + aggregate
    stages = []
    concurrent_groups = 1
    for i, wid in enumerate(filter_wids):
        stages.append({
            "id": f"S{i+1}",
            "type": "filter",
            "location": wid,
            "concurrent_group": 1,
        })

    if len(filter_wids) >= 2:
        # Intersection needed
        stages.append({
            "id": "I",
            "type": "intersect",
            "location": "coordinator",
            "depends_on": [f"S{i+1}" for i in range(len(filter_wids))],
        })
        intersect_id = "I"
    elif len(filter_wids) == 1:
        intersect_id = f"S1"
    else:
        intersect_id = None

    if agg_workers:
        agg_id = "C"
        depends = [intersect_id] if intersect_id else []
        stages.append({
            "id": agg_id,
            "type": "aggregate",
            "location": agg_workers[0],
            "depends_on": depends,
        })
    else:
        agg_id = None

    stages.append({
        "id": "R",
        "type": "compute",
        "location": "coordinator",
        "depends_on": [agg_id] if agg_id else ([intersect_id] if intersect_id else []),
    })

    plans.append({
        "id": "P1", "name": "并行查询 — 各数据源同时过滤",
        "description": "所有数据源同时筛选，结果汇总到中心求交后，由财务库计算统计值",
        "stages": stages,
    })

    # Plan 2: Serial A->B + center intersect (if 2+ filter workers)
    if len(filter_wids) >= 2:
        serial_stages = []
        for i, wid in enumerate(filter_wids):
            depends = [f"SS{i}"] if i > 0 else []
            serial_stages.append({
                "id": f"SS{i+1}",
                "type": "filter",
                "location": wid,
                "depends_on": depends,
            })
        serial_stages.append({
            "id": "SI",
            "type": "intersect",
            "location": "coordinator",
            "depends_on": [f"SS{i+1}" for i in range(len(filter_wids))],
        })
        if agg_workers:
            serial_stages.append({
                "id": "SC",
                "type": "aggregate",
                "location": agg_workers[0],
                "depends_on": ["SI"],
            })
        serial_stages.append({
            "id": "SR",
            "type": "compute",
            "location": "coordinator",
            "depends_on": ["SC"] if agg_workers else ["SI"],
        })
        plans.append({
            "id": "P2", "name": f"串行查询 — 先查{filter_wids[0]}再查{filter_wids[1]}",
            "description": f"先筛选{filter_wids[0]}，将其结果传给{filter_wids[1]}继续筛选，缩小后续处理范围",
            "stages": serial_stages,
        })

        # Plan 3: Reverse serial
        rev_filter_wids = list(reversed(filter_wids))
        rev_stages = []
        for i, wid in enumerate(rev_filter_wids):
            depends = [f"RS{i}"] if i > 0 else []
            rev_stages.append({
                "id": f"RS{i+1}",
                "type": "filter",
                "location": wid,
                "depends_on": depends,
            })
        rev_stages.append({
            "id": "RI",
            "type": "intersect",
            "location": "coordinator",
            "depends_on": [f"RS{i+1}" for i in range(len(rev_filter_wids))],
        })
        if agg_workers:
            rev_stages.append({
                "id": "RC",
                "type": "aggregate",
                "location": agg_workers[0],
                "depends_on": ["RI"],
            })
        rev_stages.append({
            "id": "RR",
            "type": "compute",
            "location": "coordinator",
            "depends_on": ["RC"] if agg_workers else ["RI"],
        })
        plans.append({
            "id": "P3", "name": f"串行查询 — 先查{filter_wids[1]}再查{filter_wids[0]}",
            "description": f"先筛选{filter_wids[1]}，将其结果传给{filter_wids[0]}继续筛选，改变执行顺序可能影响总耗时",
            "stages": rev_stages,
        })

    # Plan 4: Push-down (smallest filter result pushed to aggregate worker)
    if len(filter_wids) >= 1 and agg_workers:
        push_stages = []
        for i, wid in enumerate(filter_wids):
            push_stages.append({
                "id": f"PD{i+1}",
                "type": "filter",
                "location": wid,
                "concurrent_group": 1,
            })
        push_stages.append({
            "id": "PDI",
            "type": "intersect",
            "location": agg_workers[0],
            "depends_on": [f"PD{i+1}" for i in range(len(filter_wids))],
        })
        push_stages.append({
            "id": "PDC",
            "type": "aggregate",
            "location": agg_workers[0],
            "depends_on": ["PDI"],
        })
        push_stages.append({
            "id": "PDR",
            "type": "compute",
            "location": "coordinator",
            "depends_on": ["PDC"],
        })
        plans.append({
            "id": "P4", "name": "数据下推 — 聚合节点本地求交",
            "description": "各数据源筛选后，将token直接发送到聚合Worker本地完成求交和统计，省去中心求交的网络往返",
            "stages": push_stages,
        })

    return plans
