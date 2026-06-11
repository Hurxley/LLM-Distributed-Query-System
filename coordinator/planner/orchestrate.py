"""
Query Planner — orchestration: precheck → enumerate → cost → rank → top 4.
"""

import logging
import os

from .precheck import run_precheck
from .enumeration import _enumerate_all_plans
from .enumeration import _optimize_coordinator_count
from .generation import generate_plans_with_llm
from .validation import _merge_plans
from .cost_model import compute_cost, rank_plans
from .description import _generate_friendly_description

logger = logging.getLogger("planner.orchestrate")


async def generate_and_rank_plans(
    query_ast: dict,
    workers_summary: dict,
    worker_urls: dict,
) -> list[dict]:
    """Full pipeline: precheck -> enumerate ALL plans -> compute costs -> rank -> top 4.

    Generates ALL theoretically valid plan topologies via exhaustive enumeration,
    optionally supplements with LLM-generated plans, computes costs for every plan,
    ranks them, and returns the top 4.
    """

    # Step 1: Precheck
    precheck_counts = await run_precheck(query_ast, worker_urls)
    logger.info(f"Precheck results: {precheck_counts}")

    # Step 2: Generate ALL plans via exhaustive enumeration
    all_plans = _enumerate_all_plans(query_ast, workers_summary, precheck_counts)
    logger.info(f"Exhaustive enumeration: {len(all_plans)} candidate plans")

    # Step 2b: Optimize COUNT(person_token) — can be coordinator-side, no DB needed
    all_plans = [_optimize_coordinator_count(p, query_ast) for p in all_plans]

    # Step 2c: Optionally supplement with LLM-generated plans
    api_base = os.environ.get('LLM_API_BASE', '')
    if api_base:
        try:
            llm_plans = generate_plans_with_llm(query_ast, workers_summary, precheck_counts)
            if llm_plans:
                before = len(all_plans)
                all_plans = _merge_plans(all_plans, llm_plans)
                logger.info(f"LLM contributed {len(all_plans) - before} new plan(s) "
                           f"(had {before}, now {len(all_plans)})")
        except Exception as e:
            logger.warning(f"LLM generation failed, using exhaustive only: {e}")

    # Step 3: Compute costs for every plan
    for plan in all_plans:
        compute_cost(plan, workers_summary, precheck_counts)

    # Step 3.5: Generate beginner-friendly descriptions
    for plan in all_plans:
        friendly = _generate_friendly_description(plan, workers_summary)
        plan['friendly_name'] = friendly['friendly_name']
        plan['friendly_description'] = friendly['friendly_description']
        plan['steps'] = friendly['steps']

    # Step 4: Rank all plans by estimated cost
    all_plans = rank_plans(all_plans)

    # Step 4.5: Deduplicate by friendly_description.
    # Plans with identical descriptions are the same plan to the user —
    # keep only the cheapest (first-ranked) for each unique description.
    seen_desc = set()
    unique_plans = []
    for plan in all_plans:
        desc = plan.get('friendly_description', '')
        if desc not in seen_desc:
            seen_desc.add(desc)
            unique_plans.append(plan)
        else:
            logger.info(f"Dropped duplicate plan {plan.get('id')} — same description as cheaper plan")
    all_plans = unique_plans

    # Step 5: Renumber top plans P1..Pn for clean frontend display
    for i, plan in enumerate(all_plans):
        plan['id'] = f"P{i+1}"

    # Step 6: Return top 4
    top4 = all_plans[:4]
    top4_summary = [(p['id'], p.get('friendly_name', p.get('name','?')), f"{p['estimated_cost_ms']}ms") for p in top4]
    logger.info(f"Top 4 of {len(all_plans)} plans: {top4_summary}")

    return top4
