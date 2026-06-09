"""
Global Schema Manager for the Coordinator.

Maintains a global view of all Workers' logical fields.
Receives registrations from Workers and builds a unified schema
that the NL parser and planner can use for query routing.
"""

import logging
from typing import Optional

logger = logging.getLogger("schema_manager")


class GlobalSchema:
    """Global schema of all registered workers."""

    def __init__(self):
        self.workers: dict[str, dict] = {}          # worker_id -> worker info
        self.field_index: dict[str, list[str]] = {}  # logical_field -> [worker_ids]
        self.alias_index: dict[str, str] = {}        # alias -> logical_field
        self.field_details: dict[str, dict] = {}     # logical_field -> metadata

    def register_worker(self, worker_data: dict):
        """Register a worker's metadata (only logical fields!)."""
        worker_id = worker_data['worker_id']
        worker_name = worker_data['worker_name']

        self.workers[worker_id] = {
            'worker_id': worker_id,
            'worker_name': worker_name,
            'baseline': worker_data.get('baseline', {}),
            'fields': {},
        }

        for field in worker_data.get('fields', []):
            logical = field['logical']
            self.workers[worker_id]['fields'][logical] = field

            # Index: logical field -> workers
            if logical not in self.field_index:
                self.field_index[logical] = []
            self.field_index[logical].append(worker_id)

            # Index: aliases -> logical field
            for alias in field.get('alias', []):
                self.alias_index[alias] = logical

            # Store field details
            if logical not in self.field_details:
                self.field_details[logical] = {
                    'type': field.get('type', 'text'),
                    'values': field.get('values', []),
                    'secret': field.get('secret', False),
                }
            else:
                # Merge values from multiple workers
                existing = self.field_details[logical]
                existing['values'] = list(set(existing.get('values', []) + field.get('values', [])))

        logger.info(f"Registered worker '{worker_id}' ({worker_name}) with {len(worker_data.get('fields', []))} fields")
        logger.info(f"Total workers: {len(self.workers)}, total fields: {len(self.field_index)}")

    def get_workers_for_field(self, field_name: str) -> list[str]:
        """Get the worker IDs that contain a given logical field."""
        # Try direct logical name
        if field_name in self.field_index:
            return self.field_index[field_name]
        # Try alias resolution
        if field_name in self.alias_index:
            logical = self.alias_index[field_name]
            return self.field_index.get(logical, [])
        return []

    def get_field_by_alias(self, alias: str) -> Optional[str]:
        """Resolve an alias to its logical field name."""
        if alias in self.field_index:
            return alias
        return self.alias_index.get(alias)

    def get_field_type(self, field_name: str) -> str:
        """Get the type of a logical field."""
        logical = self.alias_index.get(field_name, field_name)
        return self.field_details.get(logical, {}).get('type', 'unknown')

    def get_field_values(self, field_name: str) -> list:
        """Get the known values for a field."""
        logical = self.alias_index.get(field_name, field_name)
        return self.field_details.get(logical, {}).get('values', [])

    def get_all_fields_summary(self) -> list[dict]:
        """Return a summary of all fields for the frontend."""
        summary = []
        for logical, field_def in self.field_details.items():
            workers = self.field_index.get(logical, [])
            # Worker names
            worker_names = []
            for wid in workers:
                if wid in self.workers:
                    worker_names.append(self.workers[wid]['worker_name'])
            summary.append({
                'logical': logical,
                'type': field_def.get('type', 'text'),
                'values': field_def.get('values', []),
                'secret': field_def.get('secret', False),
                'workers': worker_names,
            })
        return summary

    def get_workers_summary(self) -> dict:
        """Return summary of all workers for the LLM prompt and frontend."""
        return {
            wid: {
                'name': w['worker_name'],
                'fields': list(w['fields'].keys()),
                'row_count': w.get('baseline', {}).get('row_count', 0),
                'scan_ms': w.get('baseline', {}).get('scan_latency_ms', 100),
            }
            for wid, w in self.workers.items()
        }

    def to_prompt_text(self) -> str:
        """Generate a text description of the global schema for LLM prompts."""
        lines = ["# 全局数据视图 (Global Schema)\n"]
        lines.append("以下是所有数据源的逻辑字段及其值域：\n")
        for wid, worker in self.workers.items():
            lines.append(f"## {worker['worker_name']} (Worker: {wid})")
            lines.append(f"  行数: {worker.get('baseline', {}).get('row_count', 'N/A')}")
            for logical, field in worker['fields'].items():
                aliases = field.get('alias', [])
                alias_str = "/".join(aliases) if aliases else ""
                type_str = field.get('type', 'text')
                values = field.get('values', [])
                if type_str == 'enum' and values:
                    lines.append(f"  - {logical} ({alias_str}): {type_str}, 可选值: {', '.join(values)}")
                elif field.get('secret'):
                    lines.append(f"  - {logical} ({alias_str}): token (盲化标识符)")
                else:
                    lines.append(f"  - {logical} ({alias_str}): {type_str}")
            lines.append("")
        return "\n".join(lines)


# Singleton
global_schema = GlobalSchema()
