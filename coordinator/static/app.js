// ═══════════════════════════════════════════════════
// FEDERATED QUERY SYSTEM — Frontend App (state + orchestration)
// ═══════════════════════════════════════════════════

const FUNC_LABELS = { avg: '平均', sum: '总', min: '最低', max: '最高', count: '人数' };

let currentQueryId = null;
let currentPlans = [];
let selectedPlanId = null;
let currentStageTimes = null;  // actual execution times (available after execute)
let currentStageSql = null;    // SQL statements

// ── Init ──
document.addEventListener('DOMContentLoaded', function() {
    // ready
});

// ═══════════════════════════════════════════════════════
// Step 1: Parse Query — parse only, no plan generation
// ═══════════════════════════════════════════════════════

async function submitQuery() {
    var query = document.getElementById('query-input').value.trim();
    if (!query) return;

    var btn = document.getElementById('submit-btn');
    btn.disabled = true;
    btn.textContent = '解析中...';

    // Reset state
    selectedPlanId = null;
    currentStageTimes = null;
    currentStageSql = null;
    currentPlans = [];
    document.getElementById('result-section').style.display = 'none';
    document.getElementById('atomic-section').style.display = 'none';
    document.getElementById('parse-sql').style.display = 'none';
    document.getElementById('plan-list').innerHTML = '';
    document.getElementById('plan-actions').innerHTML = '';
    document.getElementById('plan-count').textContent = '';

    try {
        var resp = await fetch('/api/query', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ query: query }),
        });
        if (!resp.ok) {
            var errMsg = 'HTTP ' + resp.status;
            try { var err = await resp.json(); errMsg = err.error || err.detail || errMsg; } catch (e) {}
            throw new Error(errMsg);
        }
        var data = await resp.json();
        if (data.error) { alert(data.error); return; }

        currentQueryId = data.query_id;

        document.getElementById('results-area').style.display = 'block';
        document.getElementById('parse-method').textContent =
            data.query_ast && data.query_ast.parsed_by ? '(via ' + data.query_ast.parsed_by + ')' : '';

        // 1. Render parse result (left side)
        renderParseResult(data.query_ast);

        // 2. Show placeholder with "生成对比方案" button (right side)
        document.getElementById('plans-section').style.display = 'none';
        document.getElementById('plans-placeholder').style.display = 'block';

    } catch (e) {
        alert('查询解析失败: ' + e.message);
    } finally {
        btn.disabled = false;
        btn.textContent = '解析查询';
    }
}

// ═══════════════════════════════════════════════════════
// Step 2: Generate Plans
// ═══════════════════════════════════════════════════════

async function generatePlans() {
    if (!currentQueryId) return;

    var btn = document.getElementById('generate-plans-btn');
    btn.disabled = true;
    btn.textContent = '生成方案中...';

    try {
        var resp = await fetch('/api/query/' + currentQueryId + '/generate-plans', {
            method: 'POST',
        });
        if (!resp.ok) {
            var errMsg = 'HTTP ' + resp.status;
            try { var err = await resp.json(); errMsg = err.error || err.detail || errMsg; } catch (e) {}
            throw new Error(errMsg);
        }
        var data = await resp.json();

        currentPlans = data.plans || [];

        // Hide placeholder, show plans section
        document.getElementById('plans-placeholder').style.display = 'none';
        document.getElementById('plans-section').style.display = 'block';

        // Render plans with new buttons (生成方案SQL + 执行所选方案)
        renderPlans(currentPlans);

        // Note: atomic breakdown is NOT shown here — it only appears after execution.
        // Plan selection will update the breakdown content when it becomes visible.
    } catch (e) {
        alert('方案生成失败: ' + e.message);
    } finally {
        btn.disabled = false;
        btn.textContent = '⚡ 生成对比方案';
    }
}

// ═══════════════════════════════════════════════════════
// Plan Selection
// ═══════════════════════════════════════════════════════

function selectPlan(planId) {
    selectedPlanId = planId;
    // Highlight selected plan
    var all = document.querySelectorAll('.plan-block');
    for (var i = 0; i < all.length; i++) {
        all[i].classList.remove('selected');
    }
    var el = document.getElementById('plan-' + planId);
    if (el) el.classList.add('selected');

    // Update atomic breakdown to show selected plan
    var plan = null;
    for (var j = 0; j < currentPlans.length; j++) {
        if (currentPlans[j].id === planId) {
            plan = currentPlans[j];
            break;
        }
    }
    if (plan) {
        // Only update the breakdown if it's already visible (i.e., after execution).
        // Before execution, the breakdown stays hidden.
        var atomicSection = document.getElementById('atomic-section');
        if (atomicSection.style.display === 'block') {
            document.getElementById('atomic-plan-name').textContent =
                '— ' + escapeHtml(plan.friendly_name || plan.name);
            renderAtomicBreakdown(plan, currentStageTimes);
        }
    }
}

// ═══════════════════════════════════════════════════════
// Step 3a: Generate SQL only (no execution)
// ═══════════════════════════════════════════════════════

async function generateSQL() {
    if (!currentQueryId || !selectedPlanId) {
        alert('请先在方案列表中点击选择一个方案');
        return;
    }

    var btn = document.getElementById('sql-btn');
    if (btn) {
        btn.disabled = true;
        btn.textContent = '生成SQL中...';
    }

    try {
        var resp = await fetch('/api/query/' + currentQueryId + '/sql/' + selectedPlanId, {
            method: 'POST',
        });
        if (!resp.ok) {
            var errMsg = 'HTTP ' + resp.status;
            try { var err = await resp.json(); errMsg = err.error || err.detail || errMsg; } catch (e) {}
            throw new Error(errMsg);
        }
        var data = await resp.json();

        // Render SQL in the parse section
        renderParseSQL(data.stage_sql);
        document.getElementById('parse-sql').style.display = 'block';

    } catch (e) {
        alert('SQL生成失败: ' + e.message);
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.textContent = '📝 生成方案SQL';
        }
    }
}

// ═══════════════════════════════════════════════════════
// Step 3b: Execute selected plan (shows SQL + results)
// ═══════════════════════════════════════════════════════

function executeQuery() {
    // Execute recommended plan
    selectedPlanId = null;
    if (!currentQueryId) return;
    doExecute('/api/query/' + currentQueryId + '/execute');
}

async function executeQueryWithPlan() {
    if (!currentQueryId || !selectedPlanId) {
        alert('请先在方案列表中点击选择一个方案');
        return;
    }
    await doExecute('/api/query/' + currentQueryId + '/execute_with_plan/' + selectedPlanId);
}

async function doExecute(url) {
    try {
        var resp = await fetch(url, { method: 'POST' });
        if (!resp.ok) {
            var errMsg = 'HTTP ' + resp.status;
            try { var err = await resp.json(); errMsg = err.error || err.detail || errMsg; } catch (e) {}
            throw new Error(errMsg);
        }
        var data = await resp.json();

        // 1. Show final result (big number + metadata)
        renderFinalResult(data);
        document.getElementById('result-section').style.display = 'block';

        // 2. Show SQL in parse section (from execution)
        renderParseSQL(data.stage_sql);

        // 3. Update atomic breakdown with actual times
        currentStageTimes = data.stage_times || {};
        var executedPlan = findPlanById(data.plan_used);
        if (executedPlan) {
            renderAtomicBreakdown(executedPlan, currentStageTimes);
            document.getElementById('atomic-plan-name').textContent =
                '— ' + escapeHtml(executedPlan.friendly_name || executedPlan.name);
            document.getElementById('atomic-section').style.display = 'block';
        }
    } catch (e) {
        alert('执行失败: ' + e.message);
    }
}

function findPlanById(planId) {
    for (var i = 0; i < currentPlans.length; i++) {
        if (currentPlans[i].id === planId) return currentPlans[i];
    }
    return null;
}
