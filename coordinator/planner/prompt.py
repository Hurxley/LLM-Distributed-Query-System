"""
Query Planner — prompt construction for LLM plan generation.
"""

import json


def _build_plan_prompt(query_ast: dict, workers_summary: dict, precheck_counts: dict) -> str:
    """Build the LLM prompt for plan generation with 4 distinct topologies."""
    filters_text = json.dumps(query_ast.get('filters', []), ensure_ascii=False, indent=2)
    workers_text = json.dumps(workers_summary, ensure_ascii=False, indent=2)
    counts_text = json.dumps(precheck_counts, ensure_ascii=False, indent=2)
    agg_text = json.dumps(query_ast.get('aggregation'), ensure_ascii=False, indent=2)

    # Determine valid workers for filter/aggregate stages
    filter_worker_ids = set()
    for f in query_ast.get('filters', []):
        for w in f.get('workers', []):
            filter_worker_ids.add(w)
    filter_workers_str = ', '.join(sorted(filter_worker_ids)) if filter_worker_ids else '(无)'

    agg_info = query_ast.get('aggregation') or {}
    agg_workers = agg_info.get('workers', [])
    agg_worker_str = agg_workers[0] if agg_workers else 'worker_c'

    # Build list of filter worker IDs for topology examples
    filter_worker_list = sorted(filter_worker_ids)
    fw1 = filter_worker_list[0] if len(filter_worker_list) > 0 else 'worker_a'
    fw2 = filter_worker_list[1] if len(filter_worker_list) > 1 else fw1

    return f"""你是分布式查询优化器。必须生成恰好4个拓扑结构完全不同的候选执行方案。

查询结构:
- 筛选条件: {filters_text}
- 聚合目标: {agg_text}

Worker信息:
{workers_text}

命中数预查结果:
{counts_text}

网络RTT: 20ms

关键约束（必须严格遵守）:
- filter阶段只能在有筛选谓词的Worker上: {filter_workers_str}
- aggregate阶段必须在聚合数据所在的Worker: {agg_worker_str}
- intersect和compute阶段location必须是"coordinator"（数据下推方案除外）
- coordinator没有数据库，绝不能在coordinator上执行filter或aggregate
- 如果有且仅有一个Worker有筛选条件，无需intersect阶段
- 如果有2个或以上Worker有筛选条件，必须包含intersect阶段
- 不要在没有任何筛选条件的Worker上创建filter阶段

【核心要求】生成4个方案，每个方案拓扑结构必须不同:

方案P1「并行查询」: 所有filter Worker并发执行（concurrent_group:1）→ coordinator求交 → aggregate Worker汇总
方案P2「串行查询A→B」: filter {fw1}先执行 → filter {fw2}依赖{fw1}的结果再执行 → coordinator求交 → aggregate Worker汇总
方案P3「串行查询B→A」: filter {fw2}先执行 → filter {fw1}依赖{fw2}的结果再执行 → coordinator求交 → aggregate Worker汇总
方案P4「数据下推」: 所有filter Worker并发执行 → 求交(!!location={agg_worker_str}!!) → aggregate在{agg_worker_str}本地执行 → coordinator汇总

请输出严格JSON（不要markdown代码块）:
{{
  "plans": [
    {{
      "id": "P1",
      "name": "并行查询 — 各数据源同时过滤",
      "description": "所有数据源同时筛选，结果汇总到中心求交后，由财务库计算统计值",
      "stages": [
        {{"id": "A", "type": "filter", "location": "{fw1}", "concurrent_group": 1}},
        {{"id": "B", "type": "filter", "location": "{fw2}", "concurrent_group": 1}},
        {{"id": "I", "type": "intersect", "location": "coordinator", "depends_on": ["A", "B"]}},
        {{"id": "C", "type": "aggregate", "location": "{agg_worker_str}", "depends_on": ["I"]}},
        {{"id": "R", "type": "compute", "location": "coordinator", "depends_on": ["C"]}}
      ]
    }},
    {{
      "id": "P2",
      "name": "串行查询 — 先查{fw1}再查{fw2}",
      "description": "先筛选{fw1}，将其结果传给{fw2}继续筛选，利用先执行的命中数减少后续处理量",
      "stages": [
        {{"id": "A", "type": "filter", "location": "{fw1}"}},
        {{"id": "B", "type": "filter", "location": "{fw2}", "depends_on": ["A"]}},
        {{"id": "I", "type": "intersect", "location": "coordinator", "depends_on": ["A", "B"]}},
        {{"id": "C", "type": "aggregate", "location": "{agg_worker_str}", "depends_on": ["I"]}},
        {{"id": "R", "type": "compute", "location": "coordinator", "depends_on": ["C"]}}
      ]
    }},
    {{
      "id": "P3",
      "name": "串行查询 — 先查{fw2}再查{fw1}",
      "description": "先筛选{fw2}，将其结果传给{fw1}继续筛选，改变执行顺序可能影响总耗时",
      "stages": [
        {{"id": "A", "type": "filter", "location": "{fw2}"}},
        {{"id": "B", "type": "filter", "location": "{fw1}", "depends_on": ["A"]}},
        {{"id": "I", "type": "intersect", "location": "coordinator", "depends_on": ["A", "B"]}},
        {{"id": "C", "type": "aggregate", "location": "{agg_worker_str}", "depends_on": ["I"]}},
        {{"id": "R", "type": "compute", "location": "coordinator", "depends_on": ["C"]}}
      ]
    }},
    {{
      "id": "P4",
      "name": "数据下推 — 聚合节点本地求交",
      "description": "各数据源筛选后，将token直接发送到聚合Worker本地完成求交和统计，省去中心求交的网络往返",
      "stages": [
        {{"id": "A", "type": "filter", "location": "{fw1}", "concurrent_group": 1}},
        {{"id": "B", "type": "filter", "location": "{fw2}", "concurrent_group": 1}},
        {{"id": "I", "type": "intersect", "location": "{agg_worker_str}", "depends_on": ["A", "B"]}},
        {{"id": "C", "type": "aggregate", "location": "{agg_worker_str}", "depends_on": ["I"]}},
        {{"id": "R", "type": "compute", "location": "coordinator", "depends_on": ["C"]}}
      ]
    }}
  ]
}}"""
