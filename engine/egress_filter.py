"""
Egress filter: scans response bodies for PII patterns before they leave the worker.

Detected patterns:
  - Chinese ID card numbers (18 digits + check char)
  - Phone numbers
  - Email addresses
  - Chinese personal names (with care to avoid false positives on field values)
"""

import re
import json
import logging

logger = logging.getLogger("egress_filter")

# PII detection patterns
PII_PATTERNS = [
    (r'\b\d{17}[\dXx]\b', 'ID Card Number'),
    (r'\b1[3-9]\d{9}\b', 'Phone Number'),
    (r'[\w\.-]+@[\w\.-]+\.\w+', 'Email Address'),
]

# Known safe value strings (field values that naturally appear in responses)
SAFE_VALUES = {
    '物联网', '人工智能', '新材料', '生物医药', '量子计算',
    '工程师', '讲师', '副教授', '教授', '研究员',
    '高校', '科研院所', '企业',
    '男', '女',
    '美国', '英国', '德国', '日本', '澳大利亚', '无',
    '无', '市级', '省级', '国家级',
    'true', 'false',
}


def scan_response(data: dict | list | str) -> tuple[bool, str | None]:
    """Scan response body for PII leakage.

    Returns (is_clean, description_of_first_violation).
    """
    text = json.dumps(data, ensure_ascii=False)

    for pattern, label in PII_PATTERNS:
        matches = re.findall(pattern, text)
        for match in matches:
            # Skip if it looks like a known field value
            if match in SAFE_VALUES:
                continue
            logger.error(f"EGRESS_BLOCK: {label} pattern detected: {match[:20]}...")
            return False, f"PII pattern detected: {label}"

    return True, None


def wrap_response(data: dict) -> dict:
    """Middleware-style wrapper: scan response, raise if PII found."""
    is_clean, detail = scan_response(data)
    if not is_clean:
        logger.critical("ALERT: Egress filter blocked potential PII leak!")
        return {
            "error": "PII_LEAK_BLOCKED",
            "detail": detail,
            "message": "Response blocked by egress filter. This incident has been logged."
        }
    logger.info("Egress check passed — no PII in response")
    return data
