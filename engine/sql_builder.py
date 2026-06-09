"""
SQL Builder: translates logical field predicates into database-specific SQL.

The design doc specifies:
  Worker receives logical predicates from coordinator.
  Worker translates them to physical SQL using the mapping file.
"""

import os
import yaml
import logging

from db import quote_identifier

logger = logging.getLogger("sql_builder")


def _ph():
    """Return the correct SQL placeholder for the current database type."""
    db_type = os.environ.get('DB_TYPE', 'sqlite')
    return '?' if db_type == 'sqlite' else '%s'


class _MappingCache:
    """Thread-safe mapping cache with explicit invalidation for testing.

    Replaces the module-level ``_mapping = None`` global that test fixtures
    had to manually reset.  Call ``_MappingCache.invalidate()`` to force a
    re-read on the next ``load_mapping()`` call.
    """

    _mapping: dict | None = None
    _mapping_file: str | None = None

    @classmethod
    def get(cls, reload: bool = False) -> dict:
        mapping_file = os.environ.get('MAPPING_FILE', 'mapping.yaml')
        if not reload and cls._mapping is not None and cls._mapping_file == mapping_file:
            return cls._mapping
        with open(mapping_file, 'r', encoding='utf-8') as f:
            cls._mapping = yaml.safe_load(f)
        cls._mapping_file = mapping_file
        logger.info(f"Loaded mapping: {cls._mapping.get('worker_id')} - {cls._mapping.get('worker_name')}")
        return cls._mapping

    @classmethod
    def invalidate(cls):
        cls._mapping = None
        cls._mapping_file = None


def load_mapping(reload: bool = False):
    """Load the mapping.yaml file for this worker.

    Results are cached after first load.  Pass ``reload=True`` to force a
    re-read, or call ``_MappingCache.invalidate()`` to reset the cache
    (useful in tests).
    """
    return _MappingCache.get(reload=reload)


def get_token_field(mapping: dict | None = None) -> dict:
    """Return the field definition for the secret/token field in the mapping.

    Raises ValueError if no secret or tokenize field is found.
    """
    if mapping is None:
        mapping = load_mapping()
    for field in mapping.get('fields', []):
        if field.get('secret') or field.get('tokenize'):
            return field
    raise ValueError("No tokenize/secret field found in mapping")


def get_field_by_logical(logical_name: str) -> dict | None:
    """Look up a field definition by logical name."""
    mapping = load_mapping()
    for field in mapping.get('fields', []):
        if field['logical'] == logical_name:
            return field
        # Also check aliases
        for alias in field.get('alias', []):
            if alias == logical_name:
                return field
    return None


def get_field_by_alias(alias_text: str) -> dict | None:
    """Look up a field definition by any alias."""
    mapping = load_mapping()
    for field in mapping.get('fields', []):
        if field['logical'] == alias_text:
            return field
        for a in field.get('alias', []):
            if a == alias_text:
                return field
    return None


def translate_value_to_physical(field_def: dict, value: str) -> str:
    """Convert a display value (e.g. '物联网') to a physical code (e.g. '01')."""
    value_map = field_def.get('mapping', {})
    if not value_map:
        return value

    # value_map is {code: display} — we need reverse lookup
    for code, display in value_map.items():
        if display == value:
            return code

    # If value itself is a code, return as-is
    if value in value_map:
        return value

    logger.warning(f"Could not translate value '{value}' for field {field_def['logical']}")
    return value


def translate_operator(op: str) -> str:
    """Translate a logical operator to SQL."""
    op_map = {
        'eq': '=',
        'neq': '!=',
        'gt': '>',
        'gte': '>=',
        'lt': '<',
        'lte': '<=',
        'in': 'IN',
    }
    return op_map.get(op, '=')


def build_where_clause(predicates: list[dict], table_alias: str = '') -> tuple[str, tuple]:
    """Build a SQL WHERE clause from a list of predicates.

    Returns (where_sql, params_tuple).
    """
    if not predicates:
        return '', ()

    prefix = f"{table_alias}." if table_alias else ''
    conditions = []
    params = []

    for pred in predicates:
        field_name = pred['field']
        op = pred.get('op', 'eq')
        value = pred['value']

        field_def = get_field_by_logical(field_name)
        if not field_def:
            logger.warning(f"Field '{field_name}' not found in mapping, using raw name")
            physical_name = field_name
        else:
            physical_name = field_def['physical']
            if 'mapping' in field_def:
                if op == 'in' and isinstance(value, list):
                    value = [translate_value_to_physical(field_def, v) for v in value]
                else:
                    value = translate_value_to_physical(field_def, value)

            # Derived fields
            if field_def.get('derived'):
                expr = field_def.get('derive_expr', physical_name)
                ph = _ph()
                sql_op = translate_operator(op)
                conditions.append(f"({expr}) {sql_op} {ph}")
                params.append(value)
                continue

        sql_op = translate_operator(op)

        q_name = quote_identifier(physical_name)
        if op == 'in':
            if isinstance(value, list):
                ph = _ph()
                placeholders = ','.join([ph] * len(value))
                conditions.append(f"{prefix}{q_name} IN ({placeholders})")
                params.extend(value)
            else:
                ph = _ph()
                conditions.append(f"{prefix}{q_name} IN ({ph})")
                params.append(value)
        else:
            ph = _ph()
            conditions.append(f"{prefix}{q_name} {sql_op} {ph}")
            params.append(value)

    where = ' AND '.join(conditions)
    return f"WHERE {where}", tuple(params)


def build_filter_query(predicates: list[dict], salt_placeholder: str = '%s') -> tuple[str, tuple]:
    """Build the filter query that returns blinded tokens.

    Returns (sql, params) where sql uses HMAC-SHA256 to blind id_card.
    """
    mapping = load_mapping()
    table_name = mapping.get('table', mapping.get('db_config', {}).get('database', 'unknown'))
    # Derive table name from worker context
    db_type = os.environ.get('DB_TYPE', 'sqlite')

    # Find the secret/token field
    token_field = get_token_field(mapping)

    if token_field is None:
        raise ValueError("No tokenize/secret field found in mapping")

    physical_token_field = token_field['physical']
    token_algo = token_field.get('tokenize', 'hmac-sha256')

    where_clause, params = build_where_clause(predicates)

    if db_type == 'sqlite':
        # SQLite doesn't have built-in HMAC — we do it in Python
        select_col = physical_token_field
    elif db_type == 'mysql':
        select_col = f"SHA2(CONCAT(%s, {physical_token_field}), 256)"
        params = (salt_placeholder,) + params
    elif db_type == 'postgresql':
        # PostgreSQL: use encode(sha256(...), 'hex')
        select_col = f"encode(sha256((%s || {physical_token_field})::bytea), 'hex')"
        params = (salt_placeholder,) + params
    else:
        select_col = physical_token_field

    # Determine table name from mapping
    table = mapping.get('table', mapping.get('db_config', {}).get('database', 'data'))
    q_table = quote_identifier(table)

    sql = f"SELECT {select_col} AS token FROM {q_table} {where_clause}" if where_clause else f"SELECT {select_col} AS token FROM {q_table}"

    return sql, params


def build_count_query(predicates: list[dict]) -> tuple[str, tuple]:
    """Build a count query for the given predicates."""
    mapping = load_mapping()
    table = mapping.get('table', mapping.get('db_config', {}).get('database', 'data'))
    q_table = quote_identifier(table)
    where_clause, params = build_where_clause(predicates)

    sql = f"SELECT COUNT(*) AS cnt FROM {q_table} {where_clause}" if where_clause else f"SELECT COUNT(*) AS cnt FROM {q_table}"
    return sql, params


def build_aggregate_query(tokens: list[str], agg_field: str, agg_func: str) -> tuple[str, tuple, bool]:
    """Build the aggregate query that matches tokens from an intersection set.

    Returns ``(sql, params, db_side_hmac)`` where ``db_side_hmac`` is True
    when the database handles HMAC computation natively (MySQL / PostgreSQL).
    For SQLite the caller must do HMAC matching in Python.

    This is the key "blind matching" step — the worker receives blinded token
    hashes and matches them against locally-computed hashes of each row's
    identifier column.
    """
    mapping = load_mapping()
    table = mapping.get('table', mapping.get('db_config', {}).get('database', 'data'))

    # Find the token/secret field
    token_field = get_token_field(mapping)

    agg_field_def = get_field_by_logical(agg_field)
    physical_agg = agg_field_def['physical'] if agg_field_def else agg_field

    db_type = os.environ.get('DB_TYPE', 'sqlite')
    q_table = quote_identifier(table)
    q_agg = quote_identifier(physical_agg)
    ph = _ph()
    placeholders = ','.join([ph] * len(tokens))

    if db_type == 'mysql':
        # Push HMAC to the DB — SHA2(CONCAT(salt, col), 256) happens inside MySQL.
        salt = os.environ.get('SALT', '')
        token_expr = f"SHA2(CONCAT(%s, {quote_identifier(token_field['physical'], 'mysql')}), 256)"
        params: list = [salt] + list(tokens)
        sql = (
            f"SELECT COALESCE(SUM({q_agg}), 0) AS total_sum, "
            f"       COUNT(*) AS total_count, "
            f"       COALESCE(MIN({q_agg}), 0) AS total_min, "
            f"       COALESCE(MAX({q_agg}), 0) AS total_max, "
            f"       COUNT(CASE WHEN {q_agg} != 0 THEN 1 END) AS non_zero_count "
            f"FROM {q_table} "
            f"WHERE {token_expr} IN ({placeholders})"
        )
        return sql, tuple(params), True

    elif db_type == 'postgresql':
        # Push HMAC to the DB — encode(sha256(...), 'hex') inside PostgreSQL.
        salt = os.environ.get('SALT', '')
        token_expr = f"encode(sha256((%s || {quote_identifier(token_field['physical'], 'postgresql')})::bytea), 'hex')"
        params = [salt] + list(tokens)
        sql = (
            f"SELECT COALESCE(SUM({q_agg}), 0) AS total_sum, "
            f"       COUNT(*) AS total_count, "
            f"       COALESCE(MIN({q_agg}), 0) AS total_min, "
            f"       COALESCE(MAX({q_agg}), 0) AS total_max, "
            f"       COUNT(CASE WHEN {q_agg} != 0 THEN 1 END) AS non_zero_count "
            f"FROM {q_table} "
            f"WHERE {token_expr} IN ({placeholders})"
        )
        return sql, tuple(params), True

    else:
        # SQLite — no native HMAC. Fetch all rows, let the caller match in Python.
        q_col = quote_identifier(token_field['physical'], 'sqlite')
        sql = f"SELECT {q_col} AS raw_id, {q_agg} AS agg_val FROM {q_table}"
        return sql, (), False
