#!/bin/bash
set -e

echo "========================================"
echo " 跨源安全统计原型系统 - 一键部署"
echo "========================================"
echo ""

# Determine script directory
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# 1. Generate random salt
echo "[1/5] 生成安全盐..."
export SALT=$(python3 -c "import secrets; print(secrets.token_hex(32))" 2>/dev/null || python -c "import secrets; print(secrets.token_hex(32))")
echo "  SALT=${SALT:0:16}... (已生成 32 字节随机盐)"

# 2. Generate test data
echo "[2/5] 生成测试数据..."
python3 scripts/gen_data.py 2>/dev/null || python scripts/gen_data.py
echo "  数据生成完成。"

# 3. Start all containers
echo "[3/5] 启动 Docker 容器..."
docker compose up -d --build 2>/dev/null || docker-compose up -d --build

# 4. Wait for readiness
echo "[4/5] 等待节点注册到主控..."
for i in $(seq 1 30); do
    count=$(curl -s http://localhost:8000/api/schema 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d.get('workers',{})))" 2>/dev/null || echo 0)
    if [ "$count" -ge 3 ]; then
        echo "  ✓ 全部 3 个节点已注册"
        break
    fi
    echo "  等待中... ($count/3) [$i/30]"
    sleep 2
done

# 5. Done
echo "[5/5] 系统就绪！"
echo ""
echo "  =========================================="
echo "   系统部署成功！"
echo "  =========================================="
echo ""
echo "  🌐 打开浏览器访问: http://localhost:8000"
echo ""
echo "  📊 查看全局视图:"
echo "     curl http://localhost:8000/api/schema"
echo ""
echo "  🧪 执行 Q1 查询:"
echo "     curl -X POST http://localhost:8000/api/query \\"
echo "       -H 'Content-Type: application/json' \\"
echo "       -d '{\"query\":\"物联网方向、有海外经历且获省级以上奖励的高校教授的平均月收入\"}'"
echo ""
echo "  📋 查看容器状态:"
echo "     docker ps --filter \"name=federated\""
echo ""
echo "  📋 查看 Worker 日志 (验证数据不出域):"
echo "     docker logs --tail 20 worker_a"
echo "     docker logs --tail 20 worker_b"
echo "     docker logs --tail 20 worker_c"
echo ""
echo "  🛑 停止系统:"
echo "     docker compose down"
echo ""
echo "  ⏱️  从 git clone 到首个查询出结果，目标: 3 分钟内完成。"
echo ""
