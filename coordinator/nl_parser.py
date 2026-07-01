"""
Natural Language Query Parser — LLM-based with rule fallback.

Three-layer pipeline:
  1. LLM Layer: Semantic understanding + structured JSON extraction
  2. Anchor Layer: Field routing + value validation (embedding semantic match)
  3. Validation Layer: Completeness + legality checks
"""

import json
import math
import re
import logging
import os
from datetime import datetime
from typing import Optional

logger = logging.getLogger("nl_parser")


# ── Embedding-based semantic matching ──

# Cache: field_name -> {value_string: embedding_vector}
_embedding_cache: dict[str, dict[str, list[float]]] = {}


def _get_embeddings_batch(texts: list[str]) -> Optional[list[list[float]]]:
    """Get embedding vectors from the LLM API in one batch.

    Returns None if the embedding endpoint is unavailable.
    """
    api_base = os.environ.get('LLM_API_BASE', '')
    api_key = os.environ.get('LLM_API_KEY', '')
    if not api_base:
        return None
    try:
        import httpx
        with httpx.Client(timeout=15) as client:
            resp = client.post(
                f"{api_base}/embeddings",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": os.environ.get('LLM_MODEL', 'qwen2.5:7b'),
                    "input": texts,
                },
            )
            if resp.status_code != 200:
                logger.debug(f"Embedding API returned {resp.status_code}, falling back to bigram")
                return None
            data = resp.json()
            return [d['embedding'] for d in data['data']]
    except Exception as e:
        logger.debug(f"Embedding API unavailable: {e}, falling back to bigram")
        return None


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two embedding vectors (no numpy dependency)."""
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    return dot / (norm_a * norm_b) if norm_a and norm_b else 0.0


def _char_bigram_similarity(a: str, b: str) -> float:
    """Character bigram Jaccard similarity — lightweight fallback.

    Works well for Chinese text: "获奖情况" vs "获奖等级" share "获奖" bigram.
    """
    def bigrams(s: str) -> set[str]:
        return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) >= 2 else {s}

    ba = bigrams(a)
    bb = bigrams(b)
    if not ba or not bb:
        # Single-char fallback: unigram overlap
        return len(set(a) & set(b)) / max(len(set(a) | set(b)), 1)
    return len(ba & bb) / len(ba | bb)


def _build_embedding_cache(global_schema) -> None:
    """Pre-compute embedding vectors for all known values in the schema.

    Called once on first use; results are cached in ``_embedding_cache``.
    """
    # Collect fields that have known values and are not yet cached
    needed: dict[str, list[str]] = {}
    for logical, details in global_schema.field_details.items():
        values = details.get('values', [])
        if values and logical not in _embedding_cache:
            needed[logical] = values

    if not needed:
        return

    # Flatten for one batch API call
    all_texts: list[str] = []
    field_spans: dict[str, tuple[int, int]] = {}
    idx = 0
    for field, values in needed.items():
        field_spans[field] = (idx, idx + len(values))
        all_texts.extend(values)
        idx += len(values)

    logger.info(f"Building embedding cache for {len(needed)} fields, {len(all_texts)} values")

    embeddings = _get_embeddings_batch(all_texts)

    for field, (start, end) in field_spans.items():
        if embeddings:
            _embedding_cache[field] = {
                v: emb for v, emb in zip(needed[field], embeddings[start:end])
            }
            logger.debug(f"  {field}: {len(_embedding_cache[field])} vectors cached (embedding)")
        else:
            # Embedding API unavailable — store empty dict as sentinel
            # so we don't retry; bigram fallback will be used instead
            _embedding_cache[field] = {}
            logger.debug(f"  {field}: embedding unavailable, will use bigram fallback")


def _semantic_match_value(value: str, known_values: list[str], field_name: str) -> Optional[str]:
    """Match a value against known domain values using embedding + bigram fallback.

    Returns the best-matching known value, or None if no match exceeds threshold.
    """
    if not known_values:
        return None
    if value in known_values:
        return value

    # ── Tier 1: embedding cosine similarity ──
    cached = _embedding_cache.get(field_name)
    if cached:  # non-empty dict means embeddings are available
        emb_result = _get_embeddings_batch([value])
        if emb_result:
            query_vec = emb_result[0]
            best = max(known_values, key=lambda kv: _cosine_similarity(query_vec, cached.get(kv, [])))
            best_sim = _cosine_similarity(query_vec, cached.get(best, []))
            if best_sim > 0.65:
                logger.info(f"Embedding match: '{value}' → '{best}' (cos={best_sim:.3f})")
                return best

    # ── Tier 2: character bigram Jaccard ──
    best = max(known_values, key=lambda kv: _char_bigram_similarity(value, kv))
    best_sim = _char_bigram_similarity(value, best)
    if best_sim > 0.2:
        logger.info(f"Bigram match: '{value}' → '{best}' (sim={best_sim:.3f})")
        return best

    return None


def _semantic_match_field(name: str, global_schema) -> Optional[str]:
    """Semantic match a field name/alias against known fields.

    Uses the same embedding + bigram fallback strategy as value matching.
    """
    # Collect all candidate (display_name, logical_field) pairs
    candidates: list[tuple[str, str]] = []
    for logical in global_schema.field_index:
        candidates.append((logical, logical))
    for alias, logical in global_schema.alias_index.items():
        candidates.append((alias, logical))

    if not candidates:
        return None

    display_names = [c[0] for c in candidates]

    # ── Tier 1: embedding ──
    emb_result = _get_embeddings_batch([name] + display_names)
    if emb_result:
        query_vec = emb_result[0]
        field_vecs = emb_result[1:]
        best_idx = max(
            range(len(field_vecs)),
            key=lambda i: _cosine_similarity(query_vec, field_vecs[i]),
        )
        best_sim = _cosine_similarity(query_vec, field_vecs[best_idx])
        if best_sim > 0.65:
            matched_field = candidates[best_idx][1]
            logger.info(f"Embedding field match: '{name}' → '{matched_field}' (cos={best_sim:.3f})")
            return matched_field

    # ── Tier 2: character bigram ──
    best = max(candidates, key=lambda c: _char_bigram_similarity(name, c[0]))
    best_sim = _char_bigram_similarity(name, best[0])
    if best_sim > 0.2:
        logger.info(f"Bigram field match: '{name}' → '{best[1]}' (sim={best_sim:.3f})")
        return best[1]

    return None


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
- value必须严格从对应字段的"可选值"列表中选取，不要自创或改写用户输入
- 如果用户表达的值不在可选值列表中，选择语义最接近的可选值（例："美利坚"→"美国"、"AI"→"人工智能"、"德意志"→"德国"）
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
    """Route each filter to its worker, validate values against known domains.

    Uses embedding-based semantic matching (with character bigram fallback)
    to correct values and field names that don't exactly match the schema.
    """
    # Ensure embedding cache is built (first call only)
    _build_embedding_cache(global_schema)

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
            logger.warning(f"Field '{f['field']}' not found in any worker, trying semantic match")
            field_name = _semantic_match_field(f['field'], global_schema)
            if field_name:
                workers = global_schema.get_workers_for_field(field_name)
            else:
                continue

        # Validate value(s) with semantic matching
        known_values = global_schema.get_field_values(field_name)
        if known_values and op == 'in' and isinstance(value, list):
            # Validate each element in the list
            mapped_values = []
            for v in value:
                if str(v) not in known_values:
                    mv = _semantic_match_value(str(v), known_values, field_name)
                    mapped_values.append(mv if mv else v)
                else:
                    mapped_values.append(v)
            value = mapped_values
        elif known_values and str(value) not in known_values:
            mapped_value = _semantic_match_value(str(value), known_values, field_name)
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
    elif routed_filters:
        # No aggregation was specified but filters are present —
        # default to COUNT(person_token) as the safest fallback.
        # AVG(monthly_salary) is actively misleading when the user
        # only asked "how many" (or didn't specify any aggregation).
        agg_workers = list({w for f in routed_filters for w in f.get('workers', [])})
        routed_agg = {
            'field': 'person_token',
            'func': 'count',
            'workers': agg_workers,
        }

    return {
        'filters': routed_filters,
        'aggregation': routed_agg,
    }


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

# Salary-related terms used across multiple aggregation regex patterns.
# Must stay in sync with monthly_salary aliases in mapping.yaml.
_SALARY_TERMS = r'月收入|月工资|收入|工资|月薪|薪水|年终奖|奖金|补贴|津贴'
_SALARY_NON_SUBSIDY = r'月收入|月工资|收入|工资|月薪|薪水|年终奖|奖金'


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
        # Gender — "女性"/"男性" (explicit), then standalone "女"/"男"
        # before a title/profession (e.g. "女研究员", "男教授")
        (r'女性', 'gender', 'eq', lambda m: '女'),
        (r'男性', 'gender', 'eq', lambda m: '男'),
        (r'女(?:研究员|教授|工程师|讲师|博士|性)', 'gender', 'eq', lambda m: '女'),
        (r'男(?:研究员|教授|工程师|讲师|博士|性)', 'gender', 'eq', lambda m: '男'),
        # Org type
        (r'高校', 'org_type', 'eq', None),
        (r'科研院所', 'org_type', 'eq', None),
        (r'企业', 'org_type', 'eq', None),
        # Title — longer/specific patterns first, negative lookbehind to avoid
        # substring false matches (e.g. "副教授" incorrectly matching "教授")
        (r'工程师及以上', 'title', 'gte', lambda m: '工程师'),
        (r'副教授', 'title', 'eq', None),
        (r'(?<!副)教授', 'title', 'eq', None),
        (r'讲师', 'title', 'eq', None),
        (r'(?<!高级)(?<!助理)工程师', 'title', 'eq', None),
        (r'研究员', 'title', 'eq', None),
        # Overseas — catches "海外经历", "有海外经历", and "留学经历"
        # (e.g. "有美利坚留学经历", "德国留学经历")
        (r'(?:有)?(?:海外|留学)经历', 'overseas_experience', 'eq', lambda m: 'true'),
        (r'无(?:海外|留学)经历', 'overseas_experience', 'eq', lambda m: 'false'),
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

    # Multi-country extraction: supports "美国或德国留学", "美国和英国留学经历",
    # "有美利坚留学经历" etc.  Also maps common aliases to canonical country names.
    if '留学' in user_query:
        _COUNTRY_ALIASES = {
            '美利坚': '美国', '英格兰': '英国', '德意志': '德国',
            '澳洲': '澳大利亚',
        }
        # Match standard X国 patterns + known aliases
        country_matches = re.findall(
            r'(美利坚|英格兰|德意志|澳洲|[美英德日澳]国|澳大利亚)', user_query
        )
        if country_matches:
            # Remove duplicates while preserving order, map aliases to canonical
            seen = set()
            countries = []
            for c in country_matches:
                canonical = _COUNTRY_ALIASES.get(c, c)
                if canonical not in seen:
                    seen.add(canonical)
                    countries.append(canonical)
            if len(countries) == 1:
                filters.append({'field': 'country_of_study', 'op': 'eq', 'value': countries[0]})
            else:
                filters.append({'field': 'country_of_study', 'op': 'in', 'value': countries})

    # Determine aggregation
    agg = None
    # Use module-level _SALARY_TERMS / _SALARY_NON_SUBSIDY to stay in sync
    # with mapping.yaml aliases and avoid omissions (e.g. "工资" was missing).
    agg_patterns = [
        (fr'平均(?:的)?({_SALARY_TERMS})', 'avg'),
        (fr'({_SALARY_TERMS})(?:的)?平均', 'avg'),
        (fr'({_SALARY_TERMS})(?:的)?最高', 'max'),
        (fr'最高(?:的)?({_SALARY_TERMS})', 'max'),
        (fr'({_SALARY_TERMS})(?:的)?最低', 'min'),
        (fr'最低(?:的)?({_SALARY_TERMS})', 'min'),
        (r'总(?:的)?(补贴|津贴)(?:金额)?', 'sum'),
        (fr'总(?:的)?({_SALARY_NON_SUBSIDY})(?:金额)?', 'sum'),
        (r'(总工资|总收入|工资|收入)支出', 'sum'),
        (r'总额', 'sum'),
        (r'(?:人员|的)?(人数|数量)', 'count'),
        (r'多少(?:个)?人', 'count'),
        (r'有多少', 'count'),
        (r'计数', 'count'),
    ]

    agg_field_map = {
        '月收入': 'monthly_salary', '月工资': 'monthly_salary', '收入': 'monthly_salary',
        '工资': 'monthly_salary', '月薪': 'monthly_salary', '薪水': 'monthly_salary',
        '年终奖': 'year_end_bonus', '奖金': 'year_end_bonus',
        '补贴': 'allowance', '津贴': 'allowance',
        '总工资': 'monthly_salary', '总收入': 'monthly_salary',
        '人数': 'person_token', '数量': 'person_token',
    }

    for regex, func in agg_patterns:
        match = re.search(regex, user_query)
        if match:
            field_text = match.group(1) if match.lastindex and match.lastindex >= 1 else '数量'
            field = agg_field_map.get(field_text, 'monthly_salary')
            agg = {'field': field, 'func': func}
            break

    # Clean up conflicts: deduplicate exact duplicates, merge same-field eq's
    # into 'in', and keep broader operators over narrower ones.
    #
    # Operator specificity: in > gte/lte > gt/lt > eq > neq
    OP_PRIORITY = {'in': 4, 'gte': 3, 'lte': 3, 'gt': 2, 'lt': 2, 'eq': 1, 'neq': 0}

    seen_fields: dict[str, dict] = {}   # field -> canonical filter entry
    deduped: list[dict] = []

    for f in filters:
        key = f['field']
        if key in seen_fields:
            existing = seen_fields[key]

            # ── Same operator ──
            if f['op'] == existing['op']:
                if f['value'] == existing['value']:
                    continue  # exact duplicate → skip
                # Different values under same op → merge into 'in'
                if existing['op'] == 'eq':
                    existing['op'] = 'in'
                    existing['value'] = [existing['value'], f['value']]
                elif existing['op'] == 'in' and isinstance(existing['value'], list):
                    if f['value'] not in existing['value']:
                        existing['value'].append(f['value'])
                # For range ops (gte/lte/gt/lt), keep the broader bound:
                # gte: lower value = wider range; lte: higher value = wider range
                elif existing['op'] in ('gte', 'gt'):
                    if f['value'] < existing['value']:
                        deduped.remove(existing)
                        deduped.append(f)
                        seen_fields[key] = f
                elif existing['op'] in ('lte', 'lt'):
                    if f['value'] > existing['value']:
                        deduped.remove(existing)
                        deduped.append(f)
                        seen_fields[key] = f
                continue

            # ── Different operators → keep the one with higher priority ──
            f_pri = OP_PRIORITY.get(f['op'], 0)
            ex_pri = OP_PRIORITY.get(existing['op'], 0)
            if f_pri > ex_pri:
                deduped.remove(existing)
                deduped.append(f)
                seen_fields[key] = f
            # f_pri <= ex_pri → keep existing, discard f
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
