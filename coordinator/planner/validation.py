"""
Query Planner — plan validation and deduplication.
"""

import logging

logger = logging.getLogger("planner.validation")


def _validate_and_repair_plan(plan: dict, valid_workers: set, query_ast: dict) -> dict:
    """Validate and repair a plan structure to ensure all stage locations are valid.

    Rules:
    - filter/aggregate stages MUST have location in valid_workers (not 'coordinator')
    - intersect/compute stages MUST have location 'coordinator'
    - Remove filter stages that reference workers without predicates
    """
    agg_info = query_ast.get('aggregation') or {}
    agg_workers = agg_info.get('workers', [])
    # Never fall back to arbitrary worker for aggregate — only worker_c has salary data
    default_agg_worker = agg_workers[0] if agg_workers else 'worker_c'

    # Determine which workers actually have filter predicates
    filter_workers_with_predicates = set()
    for f in query_ast.get('filters', []):
        for w in f.get('workers', []):
            filter_workers_with_predicates.add(w)

    stages = plan.get('stages', [])
    repaired_stages = []
    removed_ids = set()

    for stage in stages:
        stype = stage.get('type', '')
        location = stage.get('location', '')

        if stype in ('filter', 'aggregate'):
            if location == 'coordinator' or location not in valid_workers:
                if stype == 'filter':
                    # Try to assign to a filter worker
                    if filter_workers_with_predicates:
                        new_location = next(iter(sorted(filter_workers_with_predicates)))
                    else:
                        logger.warning(f"Plan {plan.get('id')}: skipping filter stage {stage['id']} — no valid location")
                        removed_ids.add(stage['id'])
                        continue
                else:  # aggregate
                    new_location = default_agg_worker
                logger.warning(f"Plan {plan.get('id')}: repaired stage {stage['id']} location '{location}' -> '{new_location}'")
                stage = {**stage, 'location': new_location}

        elif stype == 'intersect':
            # Allow intersect at coordinator OR at a valid worker (push-down)
            if location != 'coordinator' and location not in valid_workers:
                logger.warning(f"Plan {plan.get('id')}: repaired intersect stage {stage['id']} location '{location}' -> 'coordinator'")
                stage = {**stage, 'location': 'coordinator'}
        elif stype == 'compute':
            if location != 'coordinator':
                logger.warning(f"Plan {plan.get('id')}: repaired compute stage {stage['id']} location '{location}' -> 'coordinator'")
                stage = {**stage, 'location': 'coordinator'}

        else:
            logger.warning(f"Plan {plan.get('id')}: unknown stage type '{stype}' for stage {stage['id']}, keeping as-is")

        repaired_stages.append(stage)

    # Update dependencies to remove references to deleted stages
    if removed_ids:
        for stage in repaired_stages:
            depends = stage.get('depends_on', [])
            stage['depends_on'] = [d for d in depends if d not in removed_ids]

    # Auto-insert intersect stage if 2+ filter stages on DIFFERENT workers but no intersect
    # Multiple filter stages on the SAME worker don't need a coordinator-side intersect —
    # the worker's SQL combines all predicates with AND, and the token set is already unified.
    filter_stages = [s for s in repaired_stages if s['type'] == 'filter']
    has_intersect = any(s['type'] == 'intersect' for s in repaired_stages)
    distinct_filter_locations = len({s.get('location') for s in filter_stages})
    if distinct_filter_locations >= 2 and not has_intersect:
        logger.warning(f"Plan {plan.get('id')}: auto-inserting missing intersect stage")
        filter_ids = [s['id'] for s in filter_stages]
        intersect_id = 'I'
        existing_ids = {s['id'] for s in repaired_stages}
        counter = 1
        while intersect_id in existing_ids:
            intersect_id = f'I{counter}'
            counter += 1
        intersect_stage = {
            'id': intersect_id,
            'type': 'intersect',
            'location': 'coordinator',
            'depends_on': list(filter_ids),
        }
        # Insert intersect before the first non-filter stage that depends on filters
        insert_idx = len(repaired_stages)
        for i, s in enumerate(repaired_stages):
            if s['type'] not in ('filter', 'intersect'):
                deps = s.get('depends_on', [])
                if any(d in filter_ids for d in deps):
                    # Replace filter dependencies with intersect
                    s['depends_on'] = [d for d in deps if d not in filter_ids] + [intersect_id]
                    insert_idx = i
                    break
        repaired_stages.insert(insert_idx, intersect_stage)

    plan['stages'] = repaired_stages
    return plan


def repair_execution_plan(plan: dict, query_ast: dict, valid_worker_ids: set | None = None) -> dict:
    """Unified plan repair for execution phase.

    Merges logic from _validate_and_repair_plan() and _execute_plan()'s repair block.
    Handles: fixing stage locations, populating predicates/agg info, removing empty
    filter stages, auto-inserting missing intersect stages.

    Returns the repaired plan dict (mutated in place).
    """
    filters = query_ast.get('filters', [])
    agg = query_ast.get('aggregation', {}) or {}

    # Map filter predicates to stages by worker
    worker_filters = {}
    for f in filters:
        for w in f.get('workers', []):
            if w not in worker_filters:
                worker_filters[w] = []
            worker_filters[w].append({
                'field': f['field'],
                'op': f.get('op', 'eq'),
                'value': f['value'],
            })

    agg_func = agg.get('func', 'avg')
    agg_field = agg.get('field', 'monthly_income')
    agg_workers = agg.get('workers', [])
    default_agg_worker = agg_workers[0] if agg_workers else 'worker_c'

    # Build valid worker set for validation
    if valid_worker_ids is None:
        valid_worker_ids = set()
        for f in filters:
            for w in f.get('workers', []):
                valid_worker_ids.add(w)
        for w in agg_workers:
            valid_worker_ids.add(w)
        if not valid_worker_ids:
            valid_worker_ids.add('worker_c')

    stages = plan.get('stages', [])

    # Step 1: Populate predicates and agg info on stages
    for stage in stages:
        if stage['type'] == 'filter':
            location = stage['location']
            stage['predicates'] = worker_filters.get(location, [])
        elif stage['type'] == 'aggregate':
            stage['agg_field'] = agg_field
            stage['agg_func'] = agg_func
            if stage.get('location') == 'coordinator' or stage.get('location') not in valid_worker_ids:
                logger.warning(f"Plan {plan.get('id')}: fixing aggregate stage {stage['id']} location -> {default_agg_worker}")
                stage['location'] = default_agg_worker
        elif stage['type'] == 'compute':
            stage['agg_func'] = agg_func

    # Step 2: Remove filter stages with empty predicates
    removed_ids = set()
    repaired_stages = []
    for stage in stages:
        if stage['type'] == 'filter' and not stage.get('predicates', []):
            logger.warning(f"Plan {plan.get('id')}: removing empty filter stage {stage['id']} ({stage.get('location')})")
            removed_ids.add(stage['id'])
            continue
        repaired_stages.append(stage)

    # Step 2b: Merge duplicate filter stages on the same worker.
    # When the LLM emits one filter stage per condition on the same worker,
    # consolidating them into a single stage avoids redundant HTTP calls and
    # prevents a spurious coordinator-side intersect between identical token sets.
    seen_locations: dict[str, dict] = {}
    deduped_stages = []
    for stage in repaired_stages:
        if stage['type'] == 'filter':
            loc = stage['location']
            if loc in seen_locations:
                existing = seen_locations[loc]
                # Merge predicates (dedup by field+op+value tuple)
                existing_preds = {(p['field'], p['op'], str(p['value'])): p for p in existing.get('predicates', [])}
                for p in stage.get('predicates', []):
                    key = (p['field'], p['op'], str(p['value']))
                    if key not in existing_preds:
                        existing_preds[key] = p
                existing['predicates'] = list(existing_preds.values())
                logger.warning(f"Plan {plan.get('id')}: merged duplicate filter stage {stage['id']} into {existing['id']} (both on {loc})")
                removed_ids.add(stage['id'])
                continue
            seen_locations[loc] = stage
        deduped_stages.append(stage)
    repaired_stages = deduped_stages

    # Step 3: Update deps after removal
    if removed_ids:
        for stage in repaired_stages:
            depends = stage.get('depends_on', [])
            stage['depends_on'] = [d for d in depends if d not in removed_ids]

    # Step 4: Auto-insert intersect if 2+ filter locations but no intersect
    filter_stage_ids = [s['id'] for s in repaired_stages if s['type'] == 'filter']
    filter_worker_locations = set(s['location'] for s in repaired_stages if s['type'] == 'filter')
    has_intersect = any(s['type'] == 'intersect' for s in repaired_stages)

    if len(filter_worker_locations) >= 2 and not has_intersect:
        logger.warning(f"Plan {plan.get('id')}: missing intersect stage — auto-inserting")
        agg_stage = next((s for s in repaired_stages if s['type'] == 'aggregate'), None)
        intersect_id = 'I'
        existing_ids = {s['id'] for s in repaired_stages}
        counter = 1
        while intersect_id in existing_ids:
            intersect_id = f'I{counter}'
            counter += 1

        intersect_stage = {
            'id': intersect_id,
            'type': 'intersect',
            'location': 'coordinator',
            'depends_on': list(filter_stage_ids),
        }

        insert_pos = len(repaired_stages)
        if agg_stage:
            insert_pos = repaired_stages.index(agg_stage)
            agg_stage['depends_on'] = [d for d in agg_stage.get('depends_on', []) if d not in filter_stage_ids]
            agg_stage['depends_on'].append(intersect_id)

        repaired_stages.insert(insert_pos, intersect_stage)
        logger.info(f"Plan {plan.get('id')}: inserted intersect stage {intersect_id} before aggregate")

    plan['stages'] = repaired_stages
    return plan


def _plan_topology_signature(plan: dict) -> tuple:
    """Create a canonical topology signature for plan deduplication.

    Normalizes stage IDs so plans with identical structure but different
    stage IDs (e.g. from LLM vs enumeration) are recognized as duplicates.
    """
    stages = plan.get('stages', [])
    parts = []
    for s in stages:
        stype = s.get('type')
        loc = s.get('location', '')
        cg = s.get('concurrent_group', None)

        # Normalize depends_on: describe each dependency by (type, location)
        # instead of raw stage ID, so plans with different naming match
        deps = s.get('depends_on', [])
        dep_descs = []
        for dep_id in deps:
            for ref in stages:
                if ref.get('id') == dep_id:
                    dep_descs.append((ref.get('type'), ref.get('location')))
                    break
        normalized_deps = tuple(sorted(dep_descs))

        parts.append((stype, loc, cg, normalized_deps))
    return tuple(sorted(parts))


def _merge_plans(existing: list[dict], new_plans: list[dict]) -> list[dict]:
    """Merge new plans into existing list, deduplicating by topology signature."""
    seen = set()
    merged = []
    for p in existing:
        sig = _plan_topology_signature(p)
        if sig not in seen:
            seen.add(sig)
            merged.append(p)
    for p in new_plans:
        sig = _plan_topology_signature(p)
        if sig not in seen:
            seen.add(sig)
            merged.append(p)
            logger.info(f"Merged new plan topology: {p.get('id')} — {p.get('name', '?')}")
    return merged
