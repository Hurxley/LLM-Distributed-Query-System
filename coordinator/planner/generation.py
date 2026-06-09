"""
Query Planner — LLM-based plan generation.

When an LLM API is configured this module generates candidate execution plans
via the language model.  It falls back to rule-based enumeration (imported from
``.enumeration``) when the LLM is unavailable or returns too few plans.
"""

import json
import logging
import os
import re

import httpx

from .prompt import _build_plan_prompt
from .validation import _validate_and_repair_plan
from .enumeration import _generate_plans_rule_based

logger = logging.getLogger("planner.generation")


def generate_plans_with_llm(query_ast: dict, workers_summary: dict, precheck_counts: dict) -> list[dict]:
    """Use LLM to generate candidate execution plans.

    Falls back to ``_generate_plans_rule_based`` when no LLM API is configured,
    the API call fails, or the model returns too few plans.
    """
    api_base = os.environ.get('LLM_API_BASE', '')
    api_key = os.environ.get('LLM_API_KEY', '')
    model = os.environ.get('LLM_MODEL', '')

    if not api_base:
        logger.warning("No LLM_API_BASE configured, using rule-based plan generation")
        return _generate_plans_rule_based(query_ast, workers_summary, precheck_counts)

    try:
        prompt = _build_plan_prompt(query_ast, workers_summary, precheck_counts)

        with httpx.Client(timeout=30) as client:
            resp = client.post(
                f"{api_base}/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model or "qwen2.5:7b",
                    "messages": [
                        {"role": "system", "content": "你是分布式查询优化器。只回复JSON，不要任何解释。"},
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0.1,
                    "max_tokens": 1500,
                }
            )

        if resp.status_code != 200:
            logger.warning(f"LLM API returned {resp.status_code}")
            return _generate_plans_rule_based(query_ast, workers_summary, precheck_counts)

        result = resp.json()
        content = result['choices'][0]['message']['content'].strip()

        content = re.sub(r'^```(?:json)?\s*', '', content)
        content = re.sub(r'\s*```$', '', content)

        data = json.loads(content)
        plans = data.get('plans', [])

        if len(plans) < 2:
            logger.warning("LLM returned too few plans, supplementing with rule-based")
            rule_plans = _generate_plans_rule_based(query_ast, workers_summary, precheck_counts)
            plans = plans + rule_plans[:(5 - len(plans))]

        logger.info(f"LLM generated {len(plans)} plans")

        # Validate and repair plan structures
        valid_worker_ids = set(workers_summary.keys())
        plans = [_validate_and_repair_plan(p, valid_worker_ids, query_ast) for p in plans]

        return plans

    except Exception as e:
        logger.error(f"LLM plan generation failed: {e}")
        return _generate_plans_rule_based(query_ast, workers_summary, precheck_counts)
