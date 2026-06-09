"""
Query Planner — beginner-friendly description generation.
"""

import logging

logger = logging.getLogger("planner.description")


def _generate_friendly_description(plan: dict, workers_summary: dict) -> dict:
    """Generate beginner-friendly name and step-by-step description from plan stages.

    Detects 4 distinct topologies:
    - Concurrent: all filters use concurrent_group, intersect at coordinator
    - Serial A→B: filters chained via depends_on, intersect at coordinator
    - Serial B→A: filters chained in reverse order, intersect at coordinator
    - Push-down: intersect happens at aggregate worker (not coordinator)
    """
    stages = plan.get('stages', [])

    filter_stages = [s for s in stages if s['type'] == 'filter']
    intersect_stage = next((s for s in stages if s['type'] == 'intersect'), None)
    agg_stage = next((s for s in stages if s['type'] == 'aggregate'), None)

    # Distinct filter worker locations (dedup preserving order)
    # Topology decisions should be based on HOW MANY DIFFERENT WORKERS,
    # not how many filter stages the LLM happened to emit.
    filter_locations = list(dict.fromkeys([s['location'] for s in filter_stages]))
    n_filter_workers = len(filter_locations)

    # Determine topology
    concurrent = any('concurrent_group' in s for s in filter_stages)
    intersect_at_coordinator = intersect_stage and intersect_stage.get('location') == 'coordinator'
    agg_worker = agg_stage.get('location', '') if agg_stage else ''

    # Get worker names for display
    def worker_display(wid):
        info = workers_summary.get(wid, {})
        return info.get('name', wid)

    # Build distinct friendly_name based on topology (by distinct worker count).
    # The generated name always wins — plan.get('name') from the generator may be
    # misleading when the LLM outputs a structure that doesn't match its label.
    plan_given_name = plan.get('name') or ''
    if n_filter_workers == 0:
        friendly_name = "直接统计方案"
    elif n_filter_workers == 1:
        wname = worker_display(filter_locations[0])
        friendly_name = f"单节点查询 — {wname}统一筛选"
    elif concurrent and intersect_at_coordinator:
        friendly_name = f"并行查询 — {n_filter_workers}个数据源同时过滤"
    elif concurrent and not intersect_at_coordinator:
        # Push-down: intersect on a worker
        push_target = worker_display(intersect_stage['location']) if intersect_stage else '?'
        friendly_name = f"数据下推 — {push_target}本地求交"
    elif not concurrent and n_filter_workers >= 2:
        # Serial: determine order from depends_on chain
        first_filter = None
        for f in filter_stages:
            if not f.get('depends_on'):
                first_filter = f
                break
        if first_filter:
            first_name = worker_display(first_filter['location'])
            friendly_name = f"串行查询 — 先查{first_name}"
        else:
            friendly_name = "串行查询方案"
    else:
        friendly_name = "未分类方案"

    # Fall back to plan-given name only when we couldn't determine topology
    # (e.g., plan has no recognizable stages at all)
    if not friendly_name and plan_given_name:
        friendly_name = plan_given_name

    # Build step-by-step description with Chinese numerals, differentiated by topology
    CN_NUM = ['一', '二', '三', '四', '五', '六', '七', '八', '九', '十']
    steps = []
    step_num = 0

    def cn(n):
        return CN_NUM[n] if n < len(CN_NUM) else str(n + 1)

    # Determine the serial order (if applicable)
    serial_chain = []
    if not concurrent and n_filter_workers >= 1:
        remaining = {s['id']: s for s in filter_stages}
        while remaining:
            nxt = next((s for s in remaining.values() if not s.get('depends_on') or all(d not in remaining for d in s.get('depends_on', []))), None)
            if nxt is None:
                nxt = next(iter(remaining.values()))
            serial_chain.append(nxt)
            del remaining[nxt['id']]

    # True intersection is only meaningful when filter results come from
    # different workers. When all filters hit the same worker, the SQL
    # already combines predicates — no coordinator-side intersect needed.
    real_intersect = n_filter_workers >= 2

    # ── Filter stage(s) ──
    if concurrent:
        # Group all concurrent filters into one step
        filter_names = [f"「{worker_display(f['location'])}」" for f in filter_stages]
        unique_names = list(dict.fromkeys(filter_names))  # dedup preserve order
        if len(unique_names) == 1:
            # Single worker — "同时在" would be misleading, just say "在"
            steps.append(f"第{cn(step_num)}步：在{unique_names[0]}中查找符合条件的人员，生成匿名标识")
        else:
            # Multiple workers genuinely running in parallel
            joined = " 和 ".join(unique_names)
            steps.append(f"第{cn(step_num)}步：同时在{joined}中各自查找符合条件的人员，生成匿名标识")
        step_num += 1
    else:
        # Serial: each filter is a separate step showing the chain.
        # Skip consecutive same-worker steps — "人才库→人才库" is meaningless.
        prev_wname = None
        for i, f in enumerate(serial_chain):
            wname = worker_display(f['location'])
            if wname == prev_wname:
                continue  # same worker twice in a row → skip redundant step
            prev_wname = wname
            if n_filter_workers == 1:
                # Only one worker total — "先" would imply a sequence that doesn't exist
                steps.append(f"第{cn(step_num)}步：在「{wname}」中查找符合条件的人员，生成匿名标识")
            elif i == 0:
                steps.append(f"第{cn(step_num)}步：先在「{wname}」中查找符合条件的人员，生成匿名标识")
            else:
                prev_name = worker_display(serial_chain[i-1]['location'])
                steps.append(f"第{cn(step_num)}步：将「{prev_name}」的匿名标识传入「{wname}」，继续筛选符合条件的人员")
            step_num += 1

    # ── Intersect stage (only meaningful when data comes from 2+ workers) ──
    if intersect_stage and real_intersect:
        if intersect_at_coordinator:
            if concurrent:
                steps.append(f"第{cn(step_num)}步：在中心节点比对各方匿名标识，取交集找出共同覆盖的人员")
            else:
                steps.append(f"第{cn(step_num)}步：在中心节点比对先后筛选出的匿名标识，取交集找出共同覆盖的人员")
        else:
            wname = worker_display(intersect_stage['location'])
            steps.append(f"第{cn(step_num)}步：将匿名标识汇总到「{wname}」本地进行比对求交，省去中心往返（数据下推）")
        step_num += 1

    # ── Aggregate stage ──
    if agg_stage:
        wname = worker_display(agg_stage['location'])
        intersect_at_agg = (real_intersect and intersect_stage and
                            intersect_stage.get('location') == agg_stage.get('location'))
        if intersect_at_agg:
            # Intersect and aggregate at same worker — truly local
            steps.append(f"第{cn(step_num)}步：直接在「{wname}」本地用求交结果完成统计计算")
        elif real_intersect and intersect_stage and not intersect_at_coordinator:
            # Intersect at some other worker, need to transfer result
            int_wname = worker_display(intersect_stage['location'])
            steps.append(f"第{cn(step_num)}步：将「{int_wname}」的求交结果传入「{wname}」，计算所需的统计数据")
        elif not real_intersect or not intersect_stage:
            # No cross-worker intersection — filter results flow directly to aggregate
            if not filter_stages:
                steps.append(f"第{cn(step_num)}步：直接在「{wname}」本地完成统计计算（无需筛选）")
            elif agg_stage['location'] in filter_locations:
                steps.append(f"第{cn(step_num)}步：直接在「{wname}」本地根据筛选结果完成统计计算")
            else:
                filter_wname = worker_display(filter_stages[0]['location'])
                steps.append(f"第{cn(step_num)}步：将「{filter_wname}」的筛选结果传入「{wname}」，计算所需的统计数据")
        else:
            # Intersect at coordinator — data flows from coordinator to aggregate worker
            steps.append(f"第{cn(step_num)}步：将中心节点求交后的共同人员列表传入「{wname}」，计算所需的统计数据")
        step_num += 1

    # ── Compute stage (always at coordinator) ──
    steps.append(f"第{cn(step_num)}步：在中心节点（主控）汇总各数据源统计结果，得出最终答案")
    step_num += 1

    # Build newline-separated friendly description
    friendly_description = "\n".join(steps)

    return {
        'friendly_name': friendly_name,
        'friendly_description': friendly_description,
        'steps': steps,
    }
