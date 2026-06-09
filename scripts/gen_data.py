#!/usr/bin/env python3
"""Generate test data for the federated query demo system.

Generates:
  - init/db_a.sql: MySQL init SQL for talent DB (~1000 rows)
  - init/db_b.sql: PostgreSQL init SQL for overseas DB (~700 rows)
  - data/salary.db: SQLite database for finance DB (~28800 rows)
  - data/salary_skewed.db: Skewed variant for adaptive demo
"""

import sqlite3
import random
import os
import sys

random.seed(42)

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
INIT_DIR = os.path.join(OUT_DIR, 'init')
DATA_DIR = os.path.join(OUT_DIR, 'data')
os.makedirs(INIT_DIR, exist_ok=True)
os.makedirs(DATA_DIR, exist_ok=True)

# ── ID card generation ──
def gen_id_card(i):
    """Generate a deterministic fake ID card number."""
    base = 32010619900100000 + i
    return str(base)

# ── Database A: Talent DB (MySQL) ~1000 rows ──
def gen_talent_db():
    names_pool = [
        "张伟", "李娜", "王强", "刘敏", "陈杰", "杨芳", "赵磊", "黄丽", "周军", "吴静",
        "徐明", "孙秀", "马超", "朱红", "胡勇", "郭艳", "何刚", "高霞", "林峰", "罗琳",
        "郑浩", "梁雪", "谢飞", "宋萍", "唐龙", "韩梅", "曹兵", "许蓉", "邓辉", "萧玉",
        "冯凯", "彭秀", "程亮", "蔡英", "潘勇", "袁芳", "于洋", "董洁", "余涛", "苏兰",
        "叶磊", "吕琴", "魏达", "蒋薇", "田猛", "杜娟", "丁锐", "沈蓉", "任刚", "姜玲",
    ]
    for i in range(50, 1050):
        name = names_pool[i % len(names_pool)]
        yield (i, name)

GENDER_MAP = {'M': '男', 'F': '女'}
FIELD_MAP = {'01': '物联网', '02': '人工智能', '03': '新材料', '04': '生物医药', '05': '量子计算'}
TITLE_MAP = {'11': '工程师', '12': '讲师', '13': '副教授', '14': '教授', '15': '研究员'}
ORG_MAP = {'U': '高校', 'R': '科研院所', 'E': '企业'}

def write_mysql_sql(path):
    rows = list(gen_talent_db())
    print(f"  Generating {len(rows)} talent records (MySQL)...")
    with open(path, 'w', encoding='utf-8') as f:
        f.write("-- Talent Database (MySQL)\n")
        f.write("CREATE DATABASE IF NOT EXISTS talent DEFAULT CHARACTER SET utf8mb4;\n")
        f.write("USE talent;\n\n")
        f.write("DROP TABLE IF EXISTS talent;\n")
        f.write("CREATE TABLE talent (\n")
        f.write("  id INT PRIMARY KEY AUTO_INCREMENT,\n")
        f.write("  id_card VARCHAR(18) NOT NULL,\n")
        f.write("  name VARCHAR(20) NOT NULL,\n")
        f.write("  gender_code CHAR(1) NOT NULL,\n")
        f.write("  birth_year INT NOT NULL,\n")
        f.write("  field_code CHAR(2) NOT NULL,\n")
        f.write("  title_code CHAR(2) NOT NULL,\n")
        f.write("  org_code CHAR(1) NOT NULL\n")
        f.write(") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;\n\n")
        f.write("INSERT INTO talent (id_card, name, gender_code, birth_year, field_code, title_code, org_code) VALUES\n")
        values = []
        for i, (idx, name) in enumerate(rows):
            id_card = gen_id_card(idx)
            gender_code = 'M' if i % 2 == 0 else 'F'
            birth_year = random.randint(1960, 2004)
            field_code = random.choice(list(FIELD_MAP.keys()))
            title_code = random.choice(list(TITLE_MAP.keys()))
            org_code = random.choice(list(ORG_MAP.keys()))
            values.append(f"('{id_card}','{name}','{gender_code}',{birth_year},'{field_code}','{title_code}','{org_code}')")
        f.write(',\n'.join(values) + ';\n')
    print(f"  -> {path}")

# ── Database B: Overseas DB (PostgreSQL) ~700 rows ──
def write_pg_sql(path, skewed=False):
    # Use a subset of talent population
    talent_rows = list(gen_talent_db())
    overseas_count = 700
    if skewed:
        # In skewed mode, one field dominates
        overseas_count = 700
    print(f"  Generating {overseas_count} overseas records (PostgreSQL)...")

    countries = ['美国', '英国', '德国', '日本', '澳大利亚', '无']
    award_levels = ['无', '市级', '省级', '国家级']

    with open(path, 'w', encoding='utf-8') as f:
        f.write("-- Overseas Experience Database (PostgreSQL)\n")
        f.write("DROP TABLE IF EXISTS overseas;\n")
        f.write("CREATE TABLE overseas (\n")
        f.write("  id SERIAL PRIMARY KEY,\n")
        f.write("  id_card VARCHAR(18) NOT NULL,\n")
        f.write("  has_overseas BOOLEAN NOT NULL,\n")
        f.write("  study_country VARCHAR(20) NOT NULL,\n")
        f.write("  max_award_level VARCHAR(10) NOT NULL,\n")
        f.write("  max_award_level_order INT NOT NULL,\n")
        f.write("  latest_award_year INT\n")
        f.write(");\n\n")
        f.write("INSERT INTO overseas (id_card, has_overseas, study_country, max_award_level, max_award_level_order, latest_award_year) VALUES\n")
        values = []
        for i in range(overseas_count):
            talent_idx = i * 1000 // overseas_count
            if talent_idx < len(talent_rows):
                _, _ = talent_rows[talent_idx]
            id_card = gen_id_card(i * 1000 // overseas_count + 50)

            has_overseas = random.choice([True] * 55 + [False] * 45)
            if has_overseas:
                study_country = random.choice(countries[:-1])  # excl '无'
            else:
                study_country = '无'

            award_level = random.choices(award_levels, weights=[25, 30, 25, 20])[0]
            award_order = award_levels.index(award_level)
            latest_award_year = random.randint(2010, 2025) if award_level != '无' else 'NULL'

            values.append(
                f"('{id_card}', {str(has_overseas).upper()}, '{study_country}', "
                f"'{award_level}', {award_order}, {latest_award_year})"
            )
        f.write(',\n'.join(values) + ';\n')
    print(f"  -> {path}")

# ── Database C: Finance DB (SQLite) ~28800 rows ──
def create_sqlite_db(path, skewed=False):
    talent_rows = list(gen_talent_db())
    # 800 people with financial data
    finance_people = 800
    print(f"  Generating finance records for {finance_people} people (SQLite)...")

    conn = sqlite3.connect(path)
    cur = conn.cursor()
    cur.execute("DROP TABLE IF EXISTS salary")
    cur.execute("""
        CREATE TABLE salary (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            id_card TEXT NOT NULL,
            pay_year INTEGER NOT NULL,
            pay_month INTEGER NOT NULL,
            monthly_income REAL NOT NULL,
            annual_bonus REAL NOT NULL,
            subsidy REAL NOT NULL
        )
    """)

    total = 0
    for i in range(finance_people):
        talent_idx = i * len(talent_rows) // finance_people
        id_card = gen_id_card(talent_idx + 50)

        # Each person has 4 years × 12 months = 48 records
        for year_offset in range(4):
            pay_year = 2021 + year_offset
            base_income = random.gauss(8000, 3000)
            base_income = max(3000, min(25000, base_income))

            if skewed and i < finance_people // 3:
                # Top earners in skewed mode
                base_income = random.gauss(18000, 4000)

            base_bonus = random.gauss(20000, 15000)
            base_bonus = max(0, min(80000, base_bonus))
            base_subsidy = random.gauss(500, 300)
            base_subsidy = max(0, min(2000, base_subsidy))

            for month in range(1, 13):
                monthly_income = round(base_income + random.gauss(0, 500), 2)
                monthly_income = max(2000, monthly_income)
                annual_bonus = round(base_bonus + random.gauss(0, 1000), 2) if month == 12 else 0.0
                subsidy = round(base_subsidy + random.gauss(0, 100), 2)
                subsidy = max(0, subsidy)

                cur.execute(
                    "INSERT INTO salary (id_card, pay_year, pay_month, monthly_income, annual_bonus, subsidy) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (id_card, pay_year, month, monthly_income, annual_bonus, subsidy)
                )
                total += 1

    conn.commit()

    # Verify
    cur.execute("SELECT COUNT(*), COUNT(DISTINCT id_card) FROM salary")
    row_count, person_count = cur.fetchone()
    print(f"  -> {path}: {row_count} rows, {person_count} unique persons")
    conn.close()
    return total

# ── Main ──
if __name__ == '__main__':
    print("=" * 50)
    print(" Generating Federated Query Test Data")
    print("=" * 50)

    print("\n[1/4] Generating Talent DB (MySQL)...")
    write_mysql_sql(os.path.join(INIT_DIR, 'db_a.sql'))

    print("\n[2/4] Generating Overseas DB (PostgreSQL)...")
    write_pg_sql(os.path.join(INIT_DIR, 'db_b.sql'))

    print("\n[3/4] Generating Finance DB (SQLite)...")
    create_sqlite_db(os.path.join(DATA_DIR, 'salary.db'))

    print("\n[4/4] Generating Skewed Finance DB (SQLite)...")
    create_sqlite_db(os.path.join(DATA_DIR, 'salary_skewed.db'), skewed=True)

    print("\n" + "=" * 50)
    print(" Data generation complete!")
    print("=" * 50)

    # Print summary
    print(f"\nFiles created:")
    for f in [
        os.path.join(INIT_DIR, 'db_a.sql'),
        os.path.join(INIT_DIR, 'db_b.sql'),
        os.path.join(DATA_DIR, 'salary.db'),
        os.path.join(DATA_DIR, 'salary_skewed.db'),
    ]:
        if os.path.exists(f):
            size = os.path.getsize(f)
            print(f"  {f} ({size:,} bytes)")
