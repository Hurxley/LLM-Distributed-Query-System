# 联邦分布式查询系统 技术方案

## 1. 系统概述

### 1.1 业务背景

在跨机构数据协作场景中，不同数据库各自持有人员不同维度的信息（例如人才履历、海外经历、薪酬明细），各方出于隐私合规与数据安全要求不能直接暴露原始数据，但业务方需要对跨库关联后的人群进行统计查询（例如"物联网方向、有海外经历且获省级以上奖励的高校教授的平均月收入"）。

传统方案面临三个核心矛盾：
1. **数据不能出库**：各库只允许对外输出聚合标量，不允许导出明细行
2. **跨库需要对齐**：不同数据库使用相同的人员标识符（如身份证号），但不能以明文在网络中传输
3. **查询入口门槛高**：业务人员不懂 SQL，需要用自然语言发起查询

本系统针对上述矛盾，设计了一套完整的联邦分布式查询方案。

### 1.2 技术目标

| 目标 | 实现方式 |
|------|---------|
| 跨库隐私求交 | HMAC-SHA256 令牌盲化，同一人员在不同库生成相同 token |
| 数据最小暴露 | Worker 仅暴露 count / filter(token) / aggregate(scalar) 三个原子接口 |
| 自然语言入口 | LLM + 规则双层解析，将中文查询转为结构化查询 AST |
| 执行方案选优 | 穷举式拓扑枚举 + 代价模型 + LLM 补充，自动推荐最优方案 |
| 实时可观测 | DAG 调度器全程推送 WebSocket 事件，前端渲染阶段级进度 |
| 多数据库兼容 | 同一套 Worker 引擎同时支持 MySQL 8.0、PostgreSQL 15、SQLite 3 |

## 2. 系统架构

### 2.1 整体拓扑

```
                          ┌──────────────────────┐
                          │       浏览器          │
                          │   前端展示 + 交互     │
                          └──────┬───────┬───────┘
                                 │ 请求  │ 实时推送
                                 ▼       ▼
┌────────────────────────────────────────────────────────────────┐
│                     主控节点 (Coordinator)                     │
│                                                                │
│  ┌──────────────────────────────────────────────────────────┐ │
│  │                  令牌鉴权中间件                           │ │
│  │         请求头 Bearer Token 校验 + CORS 头注入           │ │
│  └────────────────────┬─────────────────────────────────────┘ │
│                       ▼                                        │
│  ┌──────────────────────────────────────────────────────────┐ │
│  │                  跨域中间件                               │ │
│  └────────────────────┬─────────────────────────────────────┘ │
│                       ▼                                        │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────────┐  │
│  │  Worker  │  │ 查询提交  │  │ 全局视图  │  │  实时推送    │  │
│  │  注册    │  │  NL解析  │  │ Schema   │  │  WebSocket   │  │
│  └────┬─────┘  └────┬─────┘  └──────────┘  └──────┬───────┘  │
│       │             │                              │          │
│       ▼             ▼                              ▼          │
│  ┌─────────┐  ┌──────────────────────────┐  ┌─────────────┐  │
│  │ 全局    │  │   查询处理管道           │  │ 连接管理    │  │
│  │ Schema  │  │                          │  │ + 清理循环  │  │
│  │ 管理    │  │ NL 解析 (LLM + 规则回退) │  └─────────────┘  │
│  │         │  │   → 字段锚定与校验       │                   │
│  │         │  │                          │                   │
│  │         │  │ 方案规划                 │                   │
│  │         │  │   → 预检查 (并行命中)    │                   │
│  │         │  │   → 穷举枚举 + LLM补充   │                   │
│  │         │  │   → 代价计算 + 排序      │                   │
│  │         │  │   → 去重 → top 4         │                   │
│  │         │  │                          │                   │
│  │         │  │ DAG 调度引擎             │                   │
│  │         │  │   → 拓扑执行             │                   │
│  │         │  │   → 事件推送             │                   │
│  └─────────┘  └──────────────────────────┘                   │
│                                                                │
│  ┌──────────────────────────────────────────────────────────┐ │
│  │             查询存储 (内存)                               │ │
│  │   • 查询 TTL 淘汰 (默认 3600s)                            │ │
│  │   • 后台清理循环 (默认 300s)                              │ │
│  └──────────────────────────────────────────────────────────┘ │
└────────┬──────────────────┬──────────────────┬────────────────┘
         │                  │                  │
    命中数查询          令牌筛选          聚合统计
    (命中行数)        (盲化token)        (仅返回标量)
         │                  │                  │
         ▼                  ▼                  ▼
┌──────────────┐  ┌──────────────┐  ┌──────────────┐
│  Worker A    │  │  Worker B    │  │  Worker C    │
│  端口 8001   │  │  端口 8002   │  │  端口 8003   │
│              │  │              │  │              │
│  启动注册    │  │  启动注册    │  │  启动注册    │
│              │  │              │  │              │
│ ┌──────────┐ │  │ ┌──────────┐ │  │ ┌──────────┐ │
│ │令牌盲化  │ │  │ │令牌盲化  │ │  │ │令牌盲化  │ │
│ │HMAC-SHA  │ │  │ │HMAC-SHA  │ │  │ │HMAC-SHA  │ │
│ │256       │ │  │ │256       │ │  │ │256       │ │
│ └──────────┘ │  │ └──────────┘ │  │ └──────────┘ │
│ ┌──────────┐ │  │ ┌──────────┐ │  │ ┌──────────┐ │
│ │SQL构建   │ │  │ │SQL构建   │ │  │ │SQL构建   │ │
│ │逻辑→物理 │ │  │ │逻辑→物理 │ │  │ │逻辑→物理 │ │
│ └──────────┘ │  │ └──────────┘ │  │ └──────────┘ │
│ ┌──────────┐ │  │ ┌──────────┐ │  │ ┌──────────┐ │
│ │出口过滤  │ │  │ │出口过滤  │ │  │ │出口过滤  │ │
│ │PII 扫描  │ │  │ │PII 扫描  │ │  │ │PII 扫描  │ │
│ └──────────┘ │  │ └──────────┘ │  │ └──────────┘ │
│ ┌──────────┐ │  │ ┌──────────┐ │  │ ┌──────────┐ │
│ │数据库    │ │  │ │数据库    │ │  │ │数据库    │ │
│ │连接管理  │ │  │ │连接管理  │ │  │ │连接管理  │ │
│ └──────────┘ │  │ └──────────┘ │  │ └──────────┘ │
└──────┬───────┘  └──────┬───────┘  └──────┬───────┘
       │                  │                  │
       ▼                  ▼                  ▼
┌──────────────┐  ┌──────────────┐  ┌──────────────┐
│   MySQL 8.0  │  │ PostgreSQL 15│  │   SQLite 3   │
│   人才履历库  │  │   海外经历库  │  │   薪酬数据库  │
│              │  │              │  │              │
│ • 研究方向   │  │ • 留学国家   │  │ • 月收入     │
│ • 职称/年龄  │  │ • 海外经历   │  │ • 年终奖     │
│ • 机构类型   │  │ • 获奖级别   │  │ • 补贴       │
│ • 性别       │  │ • 年份       │  │ • 工资年份   │
└──────────────┘  └──────────────┘  └──────────────┘
```

### 2.2 数据流总览

一条查询"物联网方向、有海外经历的教授平均月收入"的完整生命周期：

```
用户输入(自然语言)                                          最终展示
    │                                                           ▲
    ▼                                                           │
┌─────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──┴──────┐
│ 1. 解析 │──▶│ 2. 枚举  │──▶│ 3. 代价  │──▶│ 4. 选择  │──▶│ 5. 执行 │
│ 查询    │   │ 方案拓扑 │   │ 排序     │   │ 提交执行 │   │ DAG调度 │
│ → AST   │   │          │   │ → top 4  │   │          │   │          │
└─────────┘   └──────────┘   └──────────┘   └──────────┘   └─────────┘
                                                          │
                                          ┌───────────────┼───────────────┐
                                          ▼               ▼               ▼
                                     Worker A         Worker B         Worker C
                                     命中数查询        命中数查询        (无筛选条件
                                     令牌筛选          令牌筛选          跳过)
                                          │               │
                                          ▼               ▼
                                     token 集合 A    token 集合 B
                                          │               │
                                          └───────┬───────┘
                                                  ▼
                                          主控节点 求交
                                          token A ∩ B
                                                  │
                                                  ▼
                                             Worker C
                                             聚合统计
                                             (标量结果)
                                                  │
                                                  ▼
                                          主控节点 汇总
                                          得出最终答案
```

### 2.3 核心设计原则

**原则一：Coordinator 零物理知识。** Coordinator 只接触逻辑字段名（如 `research_field`）和盲化 token（64 字符 HMAC 十六进制串），永远不接触物理表名、列名或原始数据。Worker 的 mapping.yaml 文件只存在于 Worker 容器内，Coordinator 无法访问。

**原则二：Worker 只暴露三个原子接口。** `/count` 返回命中行数标量，`/filter` 返回盲化 token 列表，`/aggregate` 返回五个聚合标量（sum/count/min/max/value）。任何接口都不返回原始行数据。

**原则三：执行方案自动生成，人来选择。** 系统穷举所有可行的执行拓扑，用代价模型自动排序并推荐最优方案，但保留用户手动选择的权利。前端展示每个方案的预估耗时、出口数据量和分步中文说明。

## 3. 隐私模型

### 3.1 HMAC 令牌盲化原理

系统跨库对齐人员的机制是**单向盲化哈希映射**：

```
同一人员 "110101199001011234" 在三个数据库中的行
         │                    │                    │
         ▼                    ▼                    ▼
    Worker A             Worker B             Worker C
    HMAC-SHA256          HMAC-SHA256          HMAC-SHA256
    (SALT, id)           (SALT, id)           (SALT, id)
         │                    │                    │
         ▼                    ▼                    ▼
  "a3f2c8...64hex"     "a3f2c8...64hex"     "a3f2c8...64hex"
  
  同一 SALT + 同一 id_card → 同一 token (确定性)
  无 SALT + 仅有 token → 无法反推 id_card (SHA256 抗原像性)
```

**关键安全属性：**
- **确定性：** 同一人员在所有数据库中产生相同 token，可跨库对齐
- **单向性：** HMAC-SHA256 的抗原像性保证从 token 无法恢复原始标识符
- **盐值保护：** SALT 通过环境变量注入，不出现在代码或配置文件中；盐值泄露则可被暴力枚举，因此 SALT 需妥善保管
- **无碰撞：** SHA256 输出空间 2^256，在人员标识符域内碰撞概率可忽略

**tokenizer.py 实现：**
```python
def tokenize(id_card: str) -> str:
    salt = get_salt()  # 从环境变量 SALT 读取
    return hmac.new(
        salt.encode('utf-8'),
        id_card.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()  # 64 字符十六进制字符串
```

### 3.2 数据库端 HMAC 下推

在 `/aggregate` 端点中，如果数据库支持内建哈希函数，HMAC 计算被下推到 SQL 层，避免将全部行拉回 Python 侧做匹配。

**MySQL 实现（`sql_builder.py:273-287`）：**
```sql
SELECT COALESCE(SUM(salary), 0) AS total_sum,
       COUNT(*) AS total_count,
       COALESCE(MIN(salary), 0) AS total_min,
       COALESCE(MAX(salary), 0) AS total_max,
       COUNT(CASE WHEN salary != 0 THEN 1 END) AS non_zero_count
FROM talent
WHERE SHA2(CONCAT(?, id_card), 256) IN (?, ?, ?, ...)
```

**PostgreSQL 实现（`sql_builder.py:289-303`）：**
```sql
SELECT COALESCE(SUM(salary), 0) AS total_sum,
       COUNT(*) AS total_count,
       COALESCE(MIN(salary), 0) AS total_min,
       COALESCE(MAX(salary), 0) AS total_max,
       COUNT(CASE WHEN salary != 0 THEN 1 END) AS non_zero_count
FROM overseas
WHERE encode(sha256((? || id_card)::bytea), 'hex') IN (?, ?, ?, ...)
```

**SQLite 回退（`sql_builder.py:305-309`）：**
SQLite 无内建 HMAC 函数，改为 `SELECT id_card, salary FROM salary` 拉取全部行，在 Python 侧逐行计算 token 并做集合匹配。

下推方案的实际收益：对于一个 30,000 行的薪酬表，下推前需要传输 30,000 行到 Python 侧做 HMAC 匹配（约 3MB 数据传输）。下推后，MySQL/PostgreSQL 在数据库引擎内完成匹配和聚合，只向 Worker 返回 1 行聚合标量（约 100 字节），数据传输量降低四个数量级。

### 3.3 Token 集合求交的两阶段设计

调度器在 `_do_intersect()` 中实现了两种求交模式：

**中心求交（location = coordinator）：**
Coordinator 内存中计算所有上游 Worker 返回的 token 集合的交集（Python set intersection），然后将交集 token 列表发送给聚合 Worker。

**数据下推（location = Worker，如 worker_c）：**
Coordinator 不计算交集，而是将上游 token 做并集去重后，整体发送给目标 Worker。Worker 在 `/aggregate` 中通过 HMAC 匹配本地完成实际的求交。这样就省去了 Coordinator 和 Worker 之间一次额外的网络往返——intersect 和 aggregate 合并在同一个 Worker 调用中完成。

```python
# scheduler.py:207-243
if location == 'coordinator':
    # 中心求交：Python 集合交集
    intersection = token_sets[0]
    for ts in token_sets[1:]:
        intersection = intersection & ts
    tokens = list(intersection)
else:
    # 数据下推：并集去重，让 Worker 本地匹配
    all_tokens = []
    seen = set()
    for ts in token_sets:
        for t in ts:
            if t not in seen:
                seen.add(t)
                all_tokens.append(t)
```

### 3.4 出口过滤器

每个 Worker 在所有 API 响应返回前，强制通过 `egress_filter.py` 做 PII 扫描：

```python
PII_PATTERNS = [
    (r'\b\d{17}[\dXx]\b', 'ID Card Number'),    # 18 位身份证号
    (r'\b1[3-9]\d{9}\b', 'Phone Number'),         # 手机号
    (r'[\w\.-]+@[\w\.-]+\.\w+', 'Email Address'), # 邮箱
]
```

响应体被序列化为 JSON 字符串，逐一匹配 PII 正则。匹配到的字符串再与白名单 `SAFE_VALUES`（方向名称、职称、机构类型等已知枚举值）比对，白名单内的豁免。任何命中 PII 且不在白名单中的响应将被拦截，Worker 日志记录 CRITICAL 告警，向调用方返回 `PII_LEAK_BLOCKED` 错误。

## 4. 查询处理管道

### 4.1 自然语言解析（`nl_parser.py`，390 行）

解析管道采用**三层递进式架构**：

**Layer 1: LLM 语义解析（`parse_with_llm`）**

Coordinator 启动时从 `LLM_API_BASE`、`LLM_API_KEY`、`LLM_MODEL` 环境变量读取 LLM 配置。解析时首先将全局 Schema 视图（`GlobalSchema.to_prompt_text()` 生成）与用户查询拼接为 prompt：

```
你是一个SQL查询解析器。根据以下全局数据视图，将用户的中文查询转换为结构化JSON。

# 全局数据视图
## 人才库 (Worker: worker_a)
  行数: 50000
  - research_field (研究方向/领域): enum, 可选值: 物联网, 人工智能, 新材料, ...
  - title (职称/职位): enum, 可选值: 讲师, 副教授, 教授, 研究员, ...

## 海外经历库 (Worker: worker_b)
  ...

用户查询: 物联网方向、有海外经历的教授平均月收入

请输出严格JSON格式: {"filters": [...], "aggregation": {...}}
```

LLM 返回 JSON 后，调用方做 markdown 代码块剥离（`re.sub(r'^```(?:json)?\s*', ...)`）再解析。若未配置 LLM API 或调用失败，静默降级到规则解析。

**Layer 2: 锚定与校验（`anchor_and_validate`）**

将 LLM 返回的字段名路由到实际 Worker：通过 `alias_index` 解析同义词（"研究方向"→`research_field`），通过 `field_index` 锁定包含该字段的 Worker ID 列表，验证字段值是否在已知值域内（如"人工智能"是否在 research_field 的枚举值列表中），对不在值域内的值做子串模糊匹配。

**Layer 3: 规则回退（`parse_with_rules`）**

预置 30 条正则模式覆盖 10 类预定义查询模板。规则解析器按顺序匹配查询文本：

```python
patterns = [
    (r'(物联网|人工智能|新材料|生物医药|量子计算)方向', 'research_field', 'eq', None),
    (r'女性', 'gender', 'eq', lambda m: '女'),
    (r'教授', 'title', 'eq', None),
    (r'(\d+)岁以下', 'age', 'lt', lambda m: m.group(1)),
    (r'省级以上奖励', 'max_award_level', 'gte', lambda m: '省级'),
    (r'近(\d+)年', 'pay_year', 'gte', lambda m: str(datetime.now().year - int(m.group(1)) + 1)),
    ...
]
```

聚合函数通过独立的 agg_patterns 列表匹配（"平均月收入"→`avg+monthly_income`，"人数"→`count+person_token`）。多国家留学支持通过 `re.findall(r'([美英德日澳]国|澳大利亚)', user_query)` 提取所有国家名，自动组合为 `op: 'in'` 的列表值。

**最终输出结构（QueryAST）：**
```json
{
  "filters": [
    {"field": "research_field", "op": "eq", "value": "物联网", "workers": ["worker_a"]},
    {"field": "title",        "op": "eq", "value": "教授",   "workers": ["worker_a"]},
    {"field": "has_overseas",  "op": "eq", "value": "true",  "workers": ["worker_b"]}
  ],
  "aggregation": {"field": "monthly_income", "func": "avg", "workers": ["worker_c"]},
  "valid": true,
  "errors": [],
  "parsed_by": "llm"
}
```

### 4.2 执行方案枚举（`planner/enumeration.py`，403 行）

枚举器根据过滤 Worker 数量和聚合 Worker 位置，穷举所有理论可行的 DAG 拓扑：

**情形 1：无过滤条件。** 直接聚合，单方案：
```
aggregate(worker_c) → compute(coordinator)
```

**情形 2：单过滤 Worker。** 无需 intersect，三阶段线性：
```
filter(worker_a) → aggregate(worker_c) → compute(coordinator)
```

**情形 3：N≥2 个过滤 Worker。** 穷举全部拓扑组合：

| 维度 | 可选值 | 说明 |
|------|--------|------|
| 过滤拓扑 | 并行（1 种）+ 串行（N! 种排列） | 所有 filter_workers 的全排列 |
| 求交位置 | coordinator + 每个 filter Worker + 聚合 Worker | 最多 N+2 个位置 |

例如，2 个过滤 Worker（A, B）+ 1 个聚合 Worker（C），产生：
- **并行 + 中心求交：** filter(A) ∥ filter(B) → intersect(coordinator) → aggregate(C) → compute
- **并行 + 数据下推（A 求交）：** filter(A) ∥ filter(B) → intersect(A) → aggregate(C) → compute
- **并行 + 数据下推（B 求交）：** filter(A) ∥ filter(B) → intersect(B) → aggregate(C) → compute
- **并行 + 数据下推（C 求交）：** filter(A) ∥ filter(B) → intersect(C) → aggregate(C) → compute
- **串行 A→B + 中心求交：** filter(A) → filter(B) → intersect(coordinator) → aggregate(C) → compute
- **串行 B→A + 中心求交：** filter(B) → filter(A) → intersect(coordinator) → aggregate(C) → compute
- **串行 A→B + 数据下推（C 求交）：** filter(A) → filter(B) → intersect(C) → aggregate(C) → compute
- ...（共 4 种并行 + 4×2 种串行 = 12 个方案）

**方案校验与修复（`_validate_and_repair_plan`）：**
每个枚举出的原始方案经过以下修复步骤：
1. filter/aggregate stage 的 location 必须在 valid_workers 集合内，不在则修正
2. compute stage 强制 location='coordinator'
3. 移除对应 Worker 无谓词的 filter stage
4. 若存在 2+ 个不同 Worker 的 filter stage 但没有 intersect stage，自动插入
5. 修复后的依赖关系：移除指向已删除 stage 的 depends_on 引用

### 4.3 LLM 方案生成（`planner/generation.py`，87 行）

如果配置了 LLM API，在穷举枚举之后额外调用 LLM 生成执行方案。LLM 接收的 prompt（`planner/prompt.py` 构建）包含：查询 AST 摘要、各 Worker 的行数和基准延迟、预检查命中数、方案生成规则（5 条约束）。

LLM 返回的 JSON 包含 `plans` 数组，每个 plan 含 `stages` 列表。生成的方案经 `_validate_and_repair_plan` 校验后，通过拓扑签名去重（`_plan_topology_signature`）与穷举方案合并。LLM 返回方案不足 2 个时，自动补充规则生成方案。

LLM 的作用是跳出穷举空间的"创造性补充"——穷举覆盖了理论所有组合，但 LLM 可能基于语义理解跳过明显不合理的组合（如"命中数 50000 的 Worker 不应放在串行链的第一位"），生成的方案质量更高。

### 4.4 代价模型（`planner/cost_model.py`，206 行）

每个方案的 `compute_cost()` 接收预检查命中数（`precheck_counts`），为每个 stage 估算真实耗时：

**Filter 阶段：**
```
filter_db_ms = scan_ms × (hit_count / row_count) + 5  # 数据库按比例扫描
filter_network_ms = RTT + hit_count × TOKEN_TRANSFER_MS_PER_TOKEN  # 网络传输
```

**Intersect 阶段：**
```
intersect_ms = 3ms  # Python set intersection 极快
# 如果求交在 Worker 端（非 coordinator），加一次网络 RTT
```

**Aggregate 阶段：**
```
agg_db_ms = worker_scan_ms × 0.4  # 热缓存扫描（WARM_CACHE_FACTOR）
agg_hmac_ms = worker_row_count × 1μs / 1000  # 每行 HMAC 计算
agg_network_ms = RTT + token_count × 传输速率  # 非 colocated 时加 token 传输
# 当 intersect 与 aggregate 在同一 Worker（colocated），省去 token 传输
```

**Compute 阶段：**
```
compute_ms = 1ms  # Coordinator 内存汇总
```

**DAG 感知的总体耗时计算：**

代价模型区分并发组和顺序阶段：

```python
# 并发组：取组内最大耗时（并行执行）
for gid, sids in concurrent_groups.items():
    group_max = max(stage_costs[sid]['total_ms'] for sid in valid_sids)
    total_ms += group_max

# 顺序阶段：累加（串行执行）
for sid in sequential_stages:
    total_ms += stage_costs[sid]['total_ms']
```

排序后首位标记 `recommended: True`，预估精度通常在 60%-90% 范围内。

### 4.5 方案描述生成（`planner/description.py`，181 行）

`_generate_friendly_description()` 解析 DAG 结构，生成面向非技术用户的分步中文说明。它能识别四种拓扑模式：

| 拓扑 | 识别特征 | 示例名称 |
|------|---------|---------|
| 并行+中心求交 | filter 含 concurrent_group，intersect location=coordinator | "并行查询 — 2个数据源同时过滤" |
| 数据下推 | filter 含 concurrent_group，intersect location=Worker | "数据下推 — 薪酬库本地求交" |
| 串行 A→B | filter 通过 depends_on 串联 | "串行查询 — 先查人才库" |
| 直接聚合 | 无 filter stage | "直接统计方案" |

每个方案附带中文数字编号的分步骤描述：
```
第一步：同时在「人才库」和「海外库」中各自查找符合条件的人员，生成匿名标识
第二步：在中心节点比对各方匿名标识，取交集找出共同覆盖的人员
第三步：将中心节点求交后的共同人员列表传入「薪酬库」，计算所需的统计数据
第四步：在中心节点（主控）汇总各数据源统计结果，得出最终答案
```

### 4.6 方案编排主流程（`planner/orchestrate.py`，88 行）

`generate_and_rank_plans()` 是面向 Coordinator 的唯一入口：

```
1. run_precheck()         → 并行向所有过滤 Worker 发送 /count，获取命中数
2. _enumerate_all_plans() → 穷举所有理论可行拓扑
3. generate_plans_with_llm() → LLM 生成补充方案（可选），_merge_plans 去重
4. compute_cost() × N     → 为每个方案计算预估耗时
5. _generate_friendly_description() × N → 为每个方案生成中文说明
6. rank_plans()           → 按预估耗时排序，首位标记推荐
7. 按 friendly_description 去重 → 取 top 4 → 重编号 P1..P4
```

### 4.7 预检查（`planner/precheck.py`，47 行）

`run_precheck()` 在方案枚举之前执行。它解析 query_ast 的 filters，按 Worker 分组谓词，然后通过 `asyncio.gather` 并行向所有涉及的 Worker 发送 `/count` 请求：

```
Worker A: POST /count {"predicates": [{"field":"research_field","op":"eq","value":"物联网"},{"field":"title","op":"eq","value":"教授"}]}
Worker B: POST /count {"predicates": [{"field":"has_overseas","op":"eq","value":"true"}]}
```

返回的命中数（例如 worker_a: 1200 人，worker_b: 8500 人）被传递给代价模型，使筛选阶段的预估能从"全表扫描"修正为"按实际命中比例扫描"。Worker 请求失败时使用 100 作为保守回退值。

## 5. DAG 调度引擎（`scheduler.py`，347 行）

### 5.1 执行模型

`DAGScheduler` 将执行方案建模为有向无环图，节点为四种原子操作，边由 `depends_on` 声明。调度器持有 `results: dict[str, dict]`（stage_id → Worker 返回值）和 `stage_times: dict[str, float]`（stage_id → 实际耗时毫秒数）。

```python
class DAGScheduler:
    def __init__(self, worker_urls: dict, event_callback: Optional[EventCallback] = None):
        self.worker_urls = worker_urls          # {"worker_a": "http://worker_a:8001", ...}
        self.event_callback = event_callback     # WebSocket 推送回调
        self.results = {}                        # stage_id → result
        self.stage_times = {}                    # stage_id → elapsed_ms
```

### 5.2 调度算法

```python
async def execute_plan(self, plan: dict) -> dict:
    stages = plan.get('stages', [])
    completed = set()

    while len(completed) < len(stages):
        # 扫描所有依赖已满足的 stage
        ready = [s for s in stages
                 if s['id'] not in completed
                 and all(d in completed for d in s.get('depends_on', []))]

        if not ready:
            logger.error("Deadlock detected!")
            break

        # 分组：并发组 vs 顺序组
        concurrent = [s for s in ready if 'concurrent_group' in s]
        sequential = [s for s in ready if 'concurrent_group' not in s]

        # 并发组：按 concurrent_group 值分组，每组内 asyncio.gather 并行执行
        for gid, group_stages in groups.items():
            await asyncio.gather(*[self._execute_stage(s) for s in group_stages])
            for s in group_stages:
                completed.add(s['id'])

        # 顺序组：逐个串行执行
        for s in sequential:
            await self._execute_stage(s)
            completed.add(s['id'])
```

关键设计点：
- 每轮迭代重新扫描就绪节点，支持动态 DAG（虽然当前固定拓扑用不上，但为扩展留了空间）
- 死锁检测：ready 为空且未全部完成时打印错误并退出
- 并发粒度：同一 `concurrent_group` 值的所有 stage 在同一 `gather` 中执行

### 5.3 四种原子操作的详细实现

**Filter（`_do_filter`，`scheduler.py:191-205`）：**

向目标 Worker 的 `/filter` 端点发送谓词列表，Worker 执行 `SELECT WHERE ...` 后返回匹配行的盲化 token。请求和响应示例：

```
POST http://worker_a:8001/filter
Body: {"predicates": [
  {"field":"research_field", "op":"eq", "value":"物联网"},
  {"field":"title", "op":"eq", "value":"教授"}
]}

Response: {
  "tokens": ["a3f2c8d1...", "b4e1a7f3...", ...],  // 1200 个 hex token
  "count": 1200,
  "sql": "SELECT person_token FROM talent WHERE research_field = '物联网' AND title = '教授'"
}
```

**Intersect（`_do_intersect`，`scheduler.py:207-243`）：**

收集上游所有 filter/intersect 阶段的 token 列表。两种模式：
- 中心求交（location=coordinator）：`intersection = set(tokens_A) & set(tokens_B)`，输出交集列表
- 数据下推（location=Worker）：`union = list(set(tokens_A) | set(tokens_B))`，输出并集列表，由 Worker 在 aggregate 阶段本地匹配

数据下推跳过 Coordinator 侧的集合交集计算，intersect 和 aggregate 合并为一次 Worker 调用。token 列表通过 HTTP JSON body 传输给 Worker，由 Worker 在 SQL WHERE 子句或 Python 循环中完成匹配。

**Aggregate（`_do_aggregate`，`scheduler.py:245-283`）：**

将 intersect 产生的 token 列表发送给聚合 Worker：

```
POST http://worker_c:8003/aggregate
Body: {
  "tokens": ["a3f2c8d1...", "b4e1a7f3...", ...],  // 交集后的 token 列表
  "agg_field": "monthly_income",
  "agg_func": "avg"
}

Response: {
  "sum": 12500000.0,
  "count": 850,
  "min": 5000.0,
  "max": 45000.0,
  "value": 14705.88,    // 预计算的 avg 值
  "func": "avg"
}
```

MySQL/PostgreSQL 的 HMAC 在 SQL 层完成，SQLite 在 Python 侧逐行匹配。

**Compute（`_do_compute`，`scheduler.py:284-337`）：**

在 Coordinator 内存中汇总多个 Worker 的部分聚合结果。对于 avg 函数，使用 Worker 预计算的 `value`（非零行正确均值）：

```python
if agg_func == 'avg':
    worker_values = [r.get('value') for d in depends
                     if (r := self.results.get(d)) and r.get('value') is not None]
    if worker_values:
        final_value = round(sum(worker_values) / len(worker_values), 2)
    else:
        final_value = round(total_sum / total_count, 2) if total_count > 0 else 0
```

### 5.4 WebSocket 实时事件流

调度器在每个状态转换点调用 `_push_event()`，将事件推送到所有订阅该 query_id 的 WebSocket 连接：

| 事件 | 触发时机 | 携带数据 |
|------|---------|---------|
| `execution_start` | 开始执行 | plan_id, plan_name, estimated_ms |
| `stage_start` | 单个 stage 开始 | stage_id, stage_type, location |
| `stage_complete` | 单个 stage 完成 | stage_id, elapsed_ms, result_summary |
| `stage_error` | 单个 stage 异常 | stage_id, error message |
| `execution_complete` | 全部 stage 完成 | total_ms, stage_times 汇总 |
| `query_complete` | 最终结果就绪 | total_elapsed_ms, result value, accuracy |

事件回调通过 `inspect.iscoroutinefunction` 自动检测同步/异步：

```python
async def _push_event(self, event: str, data: dict):
    if inspect.iscoroutinefunction(self.event_callback):
        await self.event_callback(event, payload)
    else:
        self.event_callback(event, payload)
```

### 5.5 WebSocket 连接管理

Coordinator 在 `ws_connections: dict[str, list[WebSocket]]` 中维护每个查询 ID 的活跃连接列表。写入时的快照迭代防止并发修改：

```python
for ws in list(ws_connections[qid]):  # 快照，防止 disconnect 并发修改
    try:
        await ws.send_json({'event': event, **data})
    except Exception:
        dead.append(ws)  # 收集断开的连接，循环结束后统一移除
```

后台清理循环（`asyncio.create_task(_cleanup_loop())` 在 lifespan 中启动）每 300s 执行：
1. TTL 淘汰：`time.time() - query['created_at'] > 3600` 的查询被移除
2. 孤立 WebSocket 清理：无对应查询的 WS 连接被 `close(code=1000)` 并移除

## 6. 全局 Schema 管理（`schema_manager.py`，145 行）

`GlobalSchema` 单例维护三个索引：

```python
class GlobalSchema:
    workers: dict[str, dict]            # worker_id → {name, baseline, fields}
    field_index: dict[str, list[str]]   # "research_field" → ["worker_a"]
    alias_index: dict[str, str]         # "研究方向" → "research_field"
    field_details: dict[str, dict]      # "research_field" → {type, values, secret}
```

### 6.1 Worker 注册流程

Worker 在 lifespan 启动阶段加载 mapping.yaml，提取逻辑字段元数据（不含物理列名），构建注册载荷：

```json
{
  "worker_id": "worker_a",
  "worker_name": "人才库",
  "fields": [
    {
      "logical": "research_field",
      "alias": ["研究方向", "领域"],
      "type": "enum",
      "values": ["物联网", "人工智能", "新材料", "生物医药", "量子计算"]
    },
    {
      "logical": "person_token",
      "type": "token",
      "secret": true
    }
  ],
  "baseline": {
    "row_count": 50000,
    "scan_latency_ms": 200
  }
}
```

Coordinator 的 `/register` 端点接收后，将字段元数据合并到 GlobalSchema。同名字段的 values 在多个 Worker 注册时自动做集合合并。

### 6.2 LLM Prompt 生成

`to_prompt_text()` 将 Schema 渲染为 LLM 可理解的自然语言描述：

```
# 全局数据视图

## 人才库 (Worker: worker_a)
  行数: 50000
  - research_field (研究方向/领域): enum, 可选值: 物联网, 人工智能, 新材料, ...
  - title (职称/职位): enum, 可选值: 讲师, 副教授, 教授, ...
  - person_token (人员标识): token (盲化标识符)

## 海外经历库 (Worker: worker_b)
  ...
```

该文本被同时用于 NL 解析 prompt 和方案生成 prompt。

## 7. 鉴权与会话安全

### 7.1 Bearer Token 鉴权中间件

`AuthMiddleware` 以纯 ASGI 中间件形式实现（`app.py:127-205`），在 FastAPI 路由处理之前从 HTTP scope headers 中读取 `Authorization` 头：

```python
class AuthMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope['type'] != 'http':
            await self.app(scope, receive, send)  # WebSocket/lifespan 放行
            return

        path = scope.get('path', '')
        if path in PUBLIC_PATHS or any(path.startswith(p) for p in PUBLIC_PREFIXES):
            await self.app(scope, receive, send)  # 公开路径放行
            return

        if not AUTH_ENABLED:
            await self.app(scope, receive, send)  # 未配置 API_TOKEN 时放行
            return

        # 从 scope headers 读取 Bearer token（不消费 body）
        auth_header = dict(scope.get('headers', [])).get(b'authorization', b'').decode()
        if not auth_header.startswith('Bearer ') or auth_header[7:] != API_TOKEN:
            # 返回 401 + 手动 CORS 头注入
            await send({'type': 'http.response.start', 'status': 401, 'headers': [...]})
            return

        await self.app(scope, receive, send)
```

**中间件顺序：** `AuthMiddleware` 在 `CORSMiddleware` 之前注册（`app.add_middleware(AuthMiddleware)` 先于 `app.add_middleware(CORSMiddleware, ...)`）。因此 AuthMiddleware 拦截请求并返回 401 时，CORS 头尚未被 CORSMiddleware 添加。为此 AuthMiddleware 在 401 响应中手动注入 `access-control-allow-*` 头，确保来自浏览器的跨域请求能正确读取 401 错误信息。

**公开路径：** `{'/', '/register', '/api/schema', '/health', '/docs', '/openapi.json'}` 以及以 `/static/` 为前缀的路径无需鉴权。

**WebSocket 鉴权：** WebSocket 端点在 `websocket_endpoint` 函数内检查查询参数 `?token=xxx`，不通过 ASGI 中间件（因为 WebSocket scope type 不是 'http'）。

### 7.2 查询生命周期管理

```
创建 → parsed → running → completed → (3600s TTL) → 清理
```

- `submit_query()` 中 `created_at = time.time()` 记录创建时间
- `_execute_plan()` 中更新 `status = 'running'` 和 `'completed'`
- `_cleanup_expired_queries()` 在后台循环中定期扫描超时条目
- WebSocket 连接随查询过期一同清理

## 8. Worker 引擎设计

### 8.1 数据库适配层（`db.py`，152 行）

`get_connection()` 根据 `DB_TYPE` 环境变量创建三种数据库连接：

```python
if db_type == 'sqlite':
    uri = f"file:{db_path}?mode=ro"
    _connection = sqlite3.connect(uri, uri=True)
    _connection.row_factory = sqlite3.Row
    # 重试 10 次（Docker volume 挂载可能有延迟）

elif db_type == 'mysql':
    _connection = mysql.connector.connect(host=..., port=..., database=..., ...)

elif db_type == 'postgresql':
    _connection = psycopg2.connect(host=..., port=..., dbname=..., ...)
    _connection.cursor_factory = psycopg2.extras.RealDictCursor
```

SQLite 使用 URI 模式 `file:path?mode=ro` 以只读方式打开，避免在 Docker Windows 卷挂载上产生 WAL/shm 文件。连接失败时重试 10 次（每次间隔 2s）。

`quote_identifier()` 自动为 MySQL 添加反引号，为 PostgreSQL/SQLite 添加双引号，防止标识符注入。

`execute_query()` 和 `execute_scalar()` 封装了三种数据库的游标差异（SQLite 的 `sqlite3.Row`、MySQL 的 `dictionary=True`、PostgreSQL 的 `RealDictCursor`），统一返回 dict 列表或标量。

### 8.2 SQL 构建器（`sql_builder.py`，309 行）

`build_where_clause(predicates)` 将逻辑谓词转换为物理 SQL 条件：

1. 通过 `get_field_by_logical()` 查找逻辑字段对应的物理字段定义
2. 如果字段有 `mapping`（枚举映射），通过 `translate_value_to_physical()` 将展示值（"物联网"）转为物理代码（"01"）
3. 如果字段有 `derived`，使用 `derive_expr`（如 `YEAR(NOW()) - birth_year`）作为 SQL 表达式
4. 通过 `_ph()` 返回正确的占位符（SQLite 用 `?`，MySQL/PostgreSQL 用 `%s`）

`build_filter_query()` 生成筛选 SQL，根据数据库类型内联 HMAC 表达式：
- SQLite：`SELECT id_card FROM salary WHERE ...`（Python 侧做 HMAC）
- MySQL：`SELECT SHA2(CONCAT(?, id_card), 256) AS token FROM talent WHERE ...`
- PostgreSQL：`SELECT encode(sha256((? || id_card)::bytea), 'hex') AS token FROM overseas WHERE ...`

占位符 `?` 对应的 SALT 值在 `build_filter_query()` 中被插入参数元组的首位置。

`_MappingCache` 是映射文件的线程安全缓存，通过类级别 `_mapping` 和 `_mapping_file` 变量实现。`invalidate()` 方法供测试使用，强制下次 `load_mapping()` 重新从磁盘读取。

### 8.3 Worker 启动流程

Worker 通过 FastAPI lifespan 在启动时依次执行：

```
1. load_mapping()          → 读取 mapping.yaml
2. get_connection()        → 连接数据库，最多重试 5 次 (3s 间隔)
3. 基准测试                → SELECT COUNT(*)，记录行数和全表扫描延迟
4. 构建注册载荷            → 从 mapping 提取逻辑字段元数据（不含物理列名）
5. POST /register          → 向 Coordinator 注册，最多重试 30 次 (2s 间隔)
```

Worker 注册后，Coordinator 的 GlobalSchema 立即更新，后续查询解析可以路由到该 Worker 的字段。

### 8.4 逻辑 SQL 展示

`_build_logical_display_sql()` 构建一个"模拟 SQL"，用逻辑字段名和逻辑表名替换真实的物理名称，在前端展示查询语义而不暴露物理 schema：

```sql
-- 前端看到的（逻辑 SQL）
SELECT person_token FROM talent WHERE research_field = '物联网' AND title = '教授'

-- Worker 实际执行的（物理 SQL，debug 模式下才暴露）
SELECT SHA2(CONCAT(?, id_card), 256) AS token FROM `t_personnel` WHERE `research_area` = '01' AND `job_title` = '03'
```

## 9. 前端设计

前端为原生 HTML/CSS/JS，无框架依赖，拆分为三个模块：

| 文件 | 职责 | 行数 |
|------|------|------|
| `app.js` | 状态管理 + 流程编排：submitQuery → selectPlan → executeQuery → doExecute | 209 |
| `render.js` | DOM 渲染：renderSchema, renderParseResult, renderPlans, renderAtomicBreakdown, renderFinalResult | 296 |
| `ws.js` | WebSocket 通信：connectWebSocket, handleWSEvent, addTimelineEntry, updateStageCard, updateProgress | 74 |

**页面布局（`index.html`）：**
```
┌─────────────────────────────────────┐
│ Header: 跨源安全统计原型系统         │
├─────────────────────────────────────┤
│ Schema 卡片（可折叠）               │
│  Worker 列表 + 字段 + 数据量         │
├─────────────────────────────────────┤
│ 查询输入框 + 预置查询按钮           │
├──────────────────┬──────────────────┤
│ 解析结果（左列） │ 方案对比（右列）  │
│ filter list      │ 4 个方案卡片     │
│ aggregation      │ DAG 预估耗时     │
│                  │ 出口数据量       │
├──────────────────┴──────────────────┤
│ 原子操作耗时分解表（独立模块）      │
│ 阶段 | 明细 | 预估 | 实际 | 偏差    │
├─────────────────────────────────────┤
│ 实时执行状态                        │
│ 进度条 + 阶段卡片 + 事件时间线      │
├─────────────────────────────────────┤
│ 最终结果（大数字 + 元数据）         │
└─────────────────────────────────────┘
```

**预置查询（10 个）：** 覆盖基础、多维、时间、聚合四类查询模板，点击即可填充到输入框。

**时序对比表：** 执行完成后，前端在同一张表中并排展示每个阶段的预估耗时和实际耗时，并计算偏差。绿色表示实际值低于预估（快于预期），红色表示实际值高于预估（慢于预期）。

## 10. 部署架构

### 10.1 服务编排（`docker-compose.yml`）

```
                   ┌──────────────────┐
                   │   主控节点        │
                   │   端口 8000       │
                   │   鉴权令牌        │
                   │   LLM 配置       │
                   └────────┬─────────┘
                            │
          ┌─────────────────┼─────────────────┐
          ▼                 ▼                 ▼
┌──────────────────┐ ┌──────────────┐ ┌──────────────┐
│   Worker A        │ │  Worker B    │ │  Worker C    │
│   端口 8001       │ │  端口 8002   │ │  端口 8003   │
│   统一 SALT       │ │  统一 SALT   │ │  统一 SALT   │
│   MySQL 连接      │ │  PostgreSQL  │ │  SQLite      │
│   mapping.yaml    │ │  连接        │ │  本地文件    │
│   (只读挂载)      │ │  mapping.yaml│ │  mapping.yaml│
│                   │ │  (只读挂载)  │ │  (只读挂载)  │
└────────┬──────────┘ └──────┬───────┘ └──────────────┘
         │                   │
         ▼                   ▼
┌──────────────────┐ ┌──────────────┐
│   MySQL 8.0      │ │ PostgreSQL 15│
│   人才履历库      │ │  海外经历库  │
│   健康检查        │ │  健康检查    │
│   初始化脚本      │ │  初始化脚本  │
└──────────────────┘ └──────────────┘
```

### 10.2 安全配置

| 配置项 | 注入方式 | 说明 |
|--------|---------|------|
| SALT | `${SALT}` 环境变量 | 所有 Worker 必须相同，保证跨库 token 一致 |
| API_TOKEN | `${API_TOKEN:-}` | 为空则关闭鉴权，开发环境兼容 |
| LLM_API_KEY | `${LLM_API_KEY:-}` | 为空则跳过 LLM 解析和方案生成 |
| DB_PASSWORD | `${MYSQL_ROOT_PASSWORD:-root}` 等 | 各数据库密码独立管理 |
| mapping.yaml | Docker volume `:ro` 只读挂载 | 物理 schema 不出 Worker 容器 |

## 11. 测试体系

### 11.1 测试策略

| 测试层级 | 文件 | 用例数 | 覆盖目标 |
|---------|------|--------|---------|
| 端到端 | `tests/test_e2e.py` | 18 | 完整 API 流程：解析→方案生成→执行→鉴权→WebSocket |
| SQL 构建 | `tests/test_sql_builder.py` | 31 | 谓词转译、HMAC SQL 生成、聚合查询、占位符适配 |
| NL 解析 | `tests/test_nl_parser.py` | 23 | 规则解析、LLM 解析、字段锚定、值验证 |
| 规划器 | `tests/test_planner.py` | 19 | 枚举、校验、拓扑签名、去重、代价模型、描述生成 |
| Schema | `tests/test_schema_manager.py` | 15 | Worker 注册、字段路由、别名解析、值合并 |
| 令牌 | `tests/test_tokenizer.py` | 11 | HMAC 一致性、碰撞检测、SALT 缺失处理 |

### 11.2 端到端测试设计

`test_e2e.py` 通过 `MockWorkerTransport` 模拟 Worker HTTP 响应，无需启动真实 Worker：

```python
class MockWorkerTransport(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request):
        # 根据 URL 路径分发到对应的模拟响应
        if "/count" in str(request.url):
            return make_resp(200, {"count": 100})
        elif "/filter" in str(request.url):
            return make_resp(200, {"tokens": ["mock_token_1", ...], "count": 100})
        elif "/aggregate" in str(request.url):
            return make_resp(200, {"sum": 10000, "count": 10, "value": 1000, ...})
```

`TestClient` 使用 `transport` 参数注入 Mock，所有 Coordinator 对外发起的 httpx 请求都被拦截。

### 11.3 CI 流程

```yaml
# .github/workflows/test.yml
test:
  runs-on: ubuntu-latest
  steps:
    - actions/checkout@v4
    - setup-python@v5 (python-version: '3.11')
    - pip install coordinator/requirements.txt + engine/requirements.txt + pytest
    - pytest tests/ -v --tb=short

build:
  runs-on: ubuntu-latest
  steps:
    - checkout + docker/setup-buildx-action
    - docker build coordinator image
    - docker build engine image
```

## 12. 技术栈总览

| 层次 | 组件 | 版本 |
|------|------|------|
| Coordinator 框架 | FastAPI | 0.115.6 |
| Worker 框架 | FastAPI | 0.115.6 |
| ASGI 服务器 | Uvicorn | 0.34.0 |
| 异步 HTTP 客户端 | httpx | 0.28.1 |
| 同步 HTTP 客户端 | httpx.Client | 0.28.1 |
| 配置格式 | YAML (PyYAML) | 6.0.2 |
| 关系型数据库 | MySQL 8.0, PostgreSQL 15, SQLite 3 | — |
| MySQL 驱动 | mysql-connector-python | 9.1.0 |
| PostgreSQL 驱动 | psycopg2-binary | 2.9.10 |
| LLM 集成 | OpenAI 兼容 API (DeepSeek / Qwen) | — |
| 容器化 | Docker + Docker Compose | v3.8 |
| 前端 | 原生 HTML/CSS/JS，无框架 | — |
| 测试框架 | pytest | latest |
| CI/CD | GitHub Actions | — |

## 13. 目录结构

```
LLM_Distributed_Query_System/
├── coordinator/                  # Coordinator 服务
│   ├── app.py                    # FastAPI 应用（路由、鉴权、WebSocket、TTL清理）480 行
│   ├── nl_parser.py              # NL 解析器（LLM + 规则双层）390 行
│   ├── schema_manager.py         # 全局 Schema 管理器 145 行
│   ├── scheduler.py              # DAG 调度引擎 347 行
│   ├── planner/                  # 查询规划器（9 模块分包）
│   │   ├── __init__.py           # 导出 generate_and_rank_plans
│   │   ├── orchestrate.py        # 编排主流程 88 行
│   │   ├── enumeration.py        # 穷举拓扑枚举 403 行
│   │   ├── generation.py         # LLM 方案生成 86 行
│   │   ├── validation.py         # 方案校验与修复 285 行
│   │   ├── cost_model.py         # 代价模型 206 行
│   │   ├── description.py        # 中文描述生成 181 行
│   │   ├── precheck.py           # 预检查 47 行
│   │   ├── prompt.py             # LLM prompt 构建 113 行
│   ├── static/                   # 前端（无框架 SPA）
│   │   ├── index.html            # 页面结构 116 行
│   │   ├── app.js                # 状态 + 流程编排 209 行
│   │   ├── render.js             # DOM 渲染 295 行
│   │   ├── ws.js                 # WebSocket 客户端 73 行
│   │   └── style.css             # 样式表
│   ├── Dockerfile                # Coordinator 镜像
│   └── requirements.txt          # Python 依赖
├── engine/                       # Worker 引擎（所有 Worker 共用）
│   ├── app.py                    # FastAPI 应用（4 端点 + 出口过滤）392 行
│   ├── sql_builder.py            # SQL 构建器（三库适配 + HMAC 下推）309 行
│   ├── tokenizer.py              # HMAC-SHA256 令牌盲化 29 行
│   ├── db.py                     # 数据库连接管理（三库适配）152 行
│   ├── egress_filter.py          # PII 出口过滤器 66 行
│   ├── Dockerfile                # Worker 镜像
│   └── requirements.txt          # Python 依赖
├── engines/                      # 各 Worker 实例配置
│   ├── worker_a/mapping.yaml     # Worker A 逻辑→物理映射
│   ├── worker_b/mapping.yaml     # Worker B 逻辑→物理映射
│   └── worker_c/mapping.yaml     # Worker C 逻辑→物理映射
├── tests/                        # 测试
│   ├── test_e2e.py               # 端到端测试（Mock Transport）556 行
│   ├── test_planner.py           # 规划器测试 272 行
│   ├── test_nl_parser.py         # NL 解析器测试
│   ├── test_schema_manager.py    # Schema 管理器测试
│   └── conftest.py               # pytest 配置
├── init/                         # 数据库初始化 SQL
│   ├── db_a.sql
│   └── db_b.sql
├── docker-compose.yml            # 6 服务编排 124 行
├── .github/workflows/test.yml    # CI 配置
├── .env.example                  # 环境变量模板
└── .gitignore
```
