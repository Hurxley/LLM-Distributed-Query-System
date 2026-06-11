"""
Natural Language Query Parser — LLM-based with rule fallback.

Three-layer pipeline:
  1. LLM Layer: Semantic understanding + structured JSON extraction
  2. Anchor Layer: Field routing + value validation
  3. Validation Layer: Completeness + legality checks
"""

import json
import re
import logging
import os
from datetime import datetime
from typing import Optional

logger = logging.getLogger("nl_parser")


# ── Layer 1: LLM-Based Parsing ──

def _build_llm_prompt(user_query: str, schema_text: str) -> str:
    return f"""你是一个SQL查询解析器。根据以下全局数据视图，将用户的中文查询转换为结构化JSON。

{schema_text}

用户查询: {user_query}

请输出严格JSON格式（不要markdown代码块，只要纯JSON）:
{{
  "filters": [
    {{"field": "逻辑字段名", "op": "eq|neq|gt|gte|lt|lte|in", "value": "值"}}
  ],
  "aggregation": {{"field": "聚合字段", "func": "avg|sum|count|min|max"}}
}}

规则:
- field必须使用上面全局视图中的逻辑字段名
- "以上"/"及以上"用 gte，"以下"/"及以下"用 lte
- 年龄/岁 相关: op用lt/lte/gt/gte, value用数字
- "近N年"表示年份>= (2025-N+1)
- 时间范围如"2024年"用 pay_year=2024
- "或"/"或者"/"和"/"及"/"与"连接的多个值用 op="in", value用数组, 例如 "美国或德国留学" → {{"field":"study_country","op":"in","value":["美国","德国"]}}
- 没有筛选条件则 filters 为空数组[]
- 没有聚合目标则 aggregation 为 null"""


def parse_with_llm(user_query: str, schema_text: str) -> Optional[dict]:
    """Use LLM to parse the natural language query.

    Returns structured dict or None on failure.
    Tries OpenAI-compatible API, falls back gracefully.
    """
    api_base = os.environ.get('LLM_API_BASE', '')
    api_key = os.environ.get('LLM_API_KEY', '')
    model = os.environ.get('LLM_MODEL', '')

    if not api_base:
        logger.warning("No LLM_API_BASE configured, skipping LLM parsing")
        return None

    try:
        import httpx
        prompt = _build_llm_prompt(user_query, schema_text)

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
                        {"role": "system", "content": "你是一个精确的查询解析器。只回复JSON，不要任何解释。"},
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0.1,
                    "max_tokens": 500,
                }
            )

        if resp.status_code != 200:
            logger.warning(f"LLM API returned {resp.status_code}: {resp.text[:200]}")
            return None

        result = resp.json()
        content = result['choices'][0]['message']['content'].strip()

        # Strip markdown code fences if present
        content = re.sub(r'^```(?:json)?\s*', '', content)
        content = re.sub(r'\s*```$', '', content)

        parsed = json.loads(content)
        logger.info(f"LLM parsed: {json.dumps(parsed, ensure_ascii=False)}")
        return parsed

    except Exception as e:
        logger.error(f"LLM parsing failed: {e}")
        return None


# ── Layer 2: Anchor (Field Routing + Value Validation) ──

def anchor_and_validate(parsed: dict, global_schema) -> dict:
    """Route each filter to its worker, validate values against known domains."""

    filters = parsed.get('filters', [])
    routed_filters = []

    for f in filters:
        field_name = f['field']
        op = f.get('op', 'eq')
        value = f.get('value', '')

        # Resolve alias to logical field
        workers = global_schema.get_workers_for_field(field_name)
        logical = global_schema.get_field_by_alias(field_name)

        if logical and logical != field_name:
            field_name = logical
            workers = global_schema.get_workers_for_field(field_name)

        if not workers:
            logger.warning(f"Field '{f['field']}' not found in any worker")
            # Try fuzzy match
            field_name = _fuzzy_match_field(f['field'], global_schema)
            if field_name:
                workers = global_schema.get_workers_for_field(field_name)
            else:
                continue

        # Validate value(s)
        known_values = global_schema.get_field_values(field_name)
        if known_values and op == 'in' and isinstance(value, list):
            # Validate each element in the list
            mapped_values = []
            for v in value:
                if str(v) not in known_values:
                    mv = _fuzzy_match_value(str(v), known_values)
                    mapped_values.append(mv if mv else v)
                else:
                    mapped_values.append(v)
            value = mapped_values
        elif known_values and str(value) not in known_values:
            # Try to map via common patterns
            mapped_value = _fuzzy_match_value(str(value), known_values)
            if mapped_value:
                value = mapped_value

        routed_filters.append({
            'field': field_name,
            'op': op,
            'value': value,
            'workers': workers,
        })

    # Route aggregation
    agg = parsed.get('aggregation')
    routed_agg = None
    if agg:
        agg_field = agg.get('field', '')
        agg_func = agg.get('func', 'avg')
        # COUNT(person_token) is coordinator-side: data comes from the filter workers
        if agg_func == 'count' and agg_field == 'person_token':
            agg_workers = list({w for f in routed_filters for w in f.get('workers', [])})
        else:
            agg_workers = global_schema.get_workers_for_field(agg_field)
        routed_agg = {
            'field': agg_field,
            'func': agg_func,
            'workers': agg_workers,
        }

    return {
        'filters': routed_filters,
        'aggregation': routed_agg,
    }


def _fuzzy_match_field(alias: str, global_schema) -> Optional[str]:
    """Fuzzy match a field name."""
    all_fields = list(global_schema.field_index.keys())
    # Direct substring match
    for field in all_fields:
        if alias in field or field in alias:
            return field
    # Alias substring match
    for logical, details in global_schema.field_details.items():
        # Check in alias_index values
        for als, log in global_schema.alias_index.items():
            if alias in als or als in alias:
                return log
    return None


def _fuzzy_match_value(value: str, known_values: list[str]) -> Optional[str]:
    """Fuzzy match a value against known domain values."""
    for kv in known_values:
        if value in kv or kv in value:
            return kv
    return None


# ── Layer 3: Validation ──

def validate_parsed_query(routed: dict) -> tuple[bool, list[str]]:
    """Validate that the parsed query is complete and legal."""
    errors = []

    filters = routed.get('filters', [])
    agg = routed.get('aggregation')

    for f in filters:
        if not f.get('workers'):
            errors.append(f"字段 '{f['field']}' 无法路由到任何数据源")
        if f.get('op') not in ('eq', 'neq', 'gt', 'gte', 'lt', 'lte', 'in'):
            errors.append(f"不支持的运算符: {f.get('op')} (字段: {f['field']})")

    if agg and not agg.get('workers'):
        # COUNT(person_token) is coordinator-side — no workers needed
        if not (agg.get('func') == 'count' and agg.get('field') == 'person_token'):
            errors.append(f"聚合字段 '{agg['field']}' 无法路由到任何数据源")

    return len(errors) == 0, errors


# ── Rule-based fallback parser ──

def parse_with_rules(user_query: str, global_schema) -> Optional[dict]:
    """Rule-based fallback when LLM is unavailable.

    Handles the 10 predefined query patterns.
    """
    logger.info("Using rule-based parser (fallback)")
    filters = []

    # Patterns: (regex, field, op, value_transform)
    patterns = [
        # Research field
        (r'(物联网|人工智能|新材料|生物医药|量子计算)方向', 'research_field', 'eq', None),
        (r'(物联网|人工智能|新材料|生物医药|量子计算)领域', 'research_field', 'eq', None),
        # Gender
        (r'女性', 'gender', 'eq', lambda m: '女'),
        (r'男性', 'gender', 'eq', lambda m: '男'),
        # Org type
        (r'高校', 'org_type', 'eq', None),
        (r'科研院所', 'org_type', 'eq', None),
        (r'企业', 'org_type', 'eq', None),
        # Title
        (r'教授', 'title', 'eq', None),
        (r'副教授', 'title', 'eq', None),
        (r'讲师', 'title', 'eq', None),
        (r'工程师及以上', 'title', 'gte', lambda m: '工程师'),
        (r'工程师', 'title', 'eq', None),
        (r'研究员', 'title', 'eq', None),
        # Overseas
        (r'(?:有)?海外经历', 'overseas_experience', 'eq', lambda m: 'true'),
        (r'无海外经历', 'overseas_experience', 'eq', lambda m: 'false'),
        # Country — handled separately below (supports "美国或德国留学")
        # Award level
        (r'省级以上奖励', 'highest_award_level', 'gte', lambda m: '省级'),
        (r'省级奖励', 'highest_award_level', 'eq', lambda m: '省级'),
        (r'国家级奖励', 'highest_award_level', 'eq', lambda m: '国家级'),
        (r'市级奖励', 'highest_award_level', 'eq', lambda m: '市级'),
        (r'获奖', 'overseas_experience', 'neq', lambda m: ''),
        # Age
        (r'(\d+)岁以下', 'age', 'lt', lambda m: m.group(1)),
        (r'(\d+)岁以上', 'age', 'gt', lambda m: m.group(1)),
        # Year
        (r'(\d{4})年', 'fiscal_year', 'eq', None),
        (r'近(\d+)年', 'fiscal_year', 'gte', lambda m: str(datetime.now().year - int(m.group(1)) + 1)),
    ]

    for regex, field, op, transform in patterns:
        match = re.search(regex, user_query)
        if match:
            if transform is not None:
                value = transform(match)
            else:
                # Use capture group 1 if present, otherwise use the full match
                try:
                    value = match.group(1)
                except IndexError:
                    value = match.group(0)
            if value:
                filters.append({'field': field, 'op': op, 'value': value})

    # Multi-country extraction: supports "美国或德国留学", "美国和英国留学经历" etc.
    # The pattern match above only catches a single country directly before/after "留学".
    # Here we find ALL country names in the query when "留学" is present.
    if '留学' in user_query:
        country_matches = re.findall(r'([美英德日澳]国|澳大利亚)', user_query)
        if country_matches:
            # Remove duplicates while preserving order
            seen = set()
            countries = []
            for c in country_matches:
                if c not in seen:
                    seen.add(c)
                    countries.append(c)
            if len(countries) == 1:
                filters.append({'field': 'country_of_study', 'op': 'eq', 'value': countries[0]})
            else:
                filters.append({'field': 'country_of_study', 'op': 'in', 'value': countries})

    # Determine aggregation
    agg = None
    agg_patterns = [
        (r'平均(?:的)?(月收入|月工资|收入|年终奖|奖金|补贴|津贴)', 'avg'),
        (r'(月收入|月工资|收入|年终奖|奖金|补贴|津贴)(?:的)?平均', 'avg'),
        (r'(月收入|月工资|收入|年终奖|奖金|补贴|津贴)(?:的)?最高', 'max'),
        (r'最高(?:的)?(月收入|月工资|收入|年终奖|奖金|补贴|津贴)', 'max'),
        (r'(月收入|月工资|收入|年终奖|奖金|补贴|津贴)(?:的)?最低', 'min'),
        (r'最低(?:的)?(月收入|月工资|收入|年终奖|奖金|补贴|津贴)', 'min'),
        (r'总(?:的)?(补贴|津贴)(?:金额)?', 'sum'),
        (r'总(?:的)?(月收入|月工资|收入|年终奖|奖金)(?:金额)?', 'sum'),
        (r'(总工资|总收入|工资|收入)支出', 'sum'),
        (r'总额', 'sum'),
        (r'(?:人员|的)?(人数|数量)', 'count'),
        (r'多少(?:个)?人', 'count'),
        (r'计数', 'count'),
    ]

    agg_field_map = {
        '月收入': 'monthly_salary', '月工资': 'monthly_salary', '收入': 'monthly_salary',
        '年终奖': 'year_end_bonus', '奖金': 'year_end_bonus',
        '补贴': 'allowance', '津贴': 'allowance',
        '总工资': 'monthly_salary', '总收入': 'monthly_salary', '工资': 'monthly_salary',
        '人数': 'person_token', '数量': 'person_token',
    }

    for regex, func in agg_patterns:
        match = re.search(regex, user_query)
        if match:
            field_text = match.group(1) if match.lastindex and match.lastindex >= 1 else '数量'
            field = agg_field_map.get(field_text, 'monthly_salary')
            agg = {'field': field, 'func': func}
            break

    # Clean up conflicts
    seen_fields = {}
    deduped = []
    for f in filters:
        key = f['field']
        if key in seen_fields:
            # Keep more specific (gte over eq, etc.)
            existing = seen_fields[key]
            if f['op'] == 'gte' and existing['op'] == 'eq':
                deduped.remove(existing)
                deduped.append(f)
                seen_fields[key] = f
        else:
            deduped.append(f)
            seen_fields[key] = f

    parsed = {
        'filters': deduped,
        'aggregation': agg,
    }

    logger.info(f"Rule parser result: {json.dumps(parsed, ensure_ascii=False)}")
    return parsed


# ── Main entry point ──

def parse_query(user_query: str, global_schema) -> dict:
    """Parse a natural language query into a structured QueryAST.

    Returns {'filters': [...], 'aggregation': {...}}
    """
    schema_text = global_schema.to_prompt_text()

    # Try LLM first
    parsed = parse_with_llm(user_query, schema_text)

    # Fall back to rules
    if parsed is None:
        parsed = parse_with_rules(user_query, global_schema)

    # Anchor and validate
    routed = anchor_and_validate(parsed, global_schema)
    is_valid, errors = validate_parsed_query(routed)

    if not is_valid:
        logger.warning(f"Validation errors: {errors}")

    # Build final AST
    return {
        'filters': routed['filters'],
        'aggregation': routed['aggregation'],
        'valid': is_valid,
        'errors': errors,
        'parsed_by': 'llm' if parsed else 'rules',
    }
