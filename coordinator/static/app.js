// ═══════════════════════════════════════════════════
// FEDERATED QUERY SYSTEM — Frontend App (state + orchestration)
// ═══════════════════════════════════════════════════
// Rendering functions live in render.js; WebSocket in ws.js.

const FUNC_LABELS = { avg: '平均', sum: '总', min: '最低', max: '最高', count: '人数' };

let currentQueryId = null;
let currentPlans = [];
let selectedPlanId = null;
let currentStageTimes = null;  // actual execution times (available after execute)
let currentStageSql = null;    // SQL statements (available after execute)
let ws = null;

// ── Init ──
document.addEventListener('DOMContentLoaded', function() {
    // ready
});

// ── Submit Query ──
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
    document.getElementById('status-section').style.display = 'none';
    document.getElementById('result-section').style.display = 'none';
    document.getElementById('atomic-section').style.display = 'none';
    document.getElementById('parse-sql').style.display = 'none';

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
        currentPlans = data.plans || [];

        document.getElementById('results-area').style.display = 'block';
        document.getElementById('parse-method').textContent =
            data.query_ast && data.query_ast.parsed_by ? '(via ' + data.query_ast.parsed_by + ')' : '';

        // 1. Render parse result (filters + aggregation)
        renderParseResult(data.query_ast);

        // 2. Render plan list (with steps — always visible)
        renderPlans(data.plans);

        // 3. Render atomic breakdown module (dedicated, shows recommended plan estimates)
        if (currentPlans.length > 0) {
            var displayPlan = currentPlans[0];  // recommended
            renderAtomicBreakdown(displayPlan, null);
            document.getElementById('atomic-section').style.display = 'block';
        }
    } catch (e) {
        alert('查询解析失败: ' + e.message);
    } finally {
        btn.disabled = false;
        btn.textContent = '解析查询';
    }
}

// ── Plan Selection ──
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
        document.getElementById('atomic-plan-name').textContent = '— ' + escapeHtml(plan.friendly_name || plan.name);
        renderAtomicBreakdown(plan, currentStageTimes);
        document.getElementById('atomic-section').style.display = 'block';
    }
}

// ── Execute ──
async function executeQuery() {
    if (!currentQueryId) return;
    selectedPlanId = null;
    await doExecute('/api/query/' + currentQueryId + '/execute');
}

async function executeQueryWithPlan() {
    if (!currentQueryId || !selectedPlanId) {
        alert('请先在方案列表中点击选择一个方案');
        return;
    }
    await doExecute('/api/query/' + currentQueryId + '/execute_with_plan/' + selectedPlanId);
}

async function doExecute(url) {
    document.getElementById('status-section').style.display = 'block';
    document.getElementById('result-section').style.display = 'none';
    document.getElementById('parse-sql').style.display = 'none';
    document.getElementById('stage-cards').innerHTML = '';
    document.getElementById('event-timeline').innerHTML = '';
    document.getElementById('progress-bar').style.width = '0%';

    connectWebSocket(currentQueryId);

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

        // 2. Show SQL in parse section
        renderParseSQL(data.stage_sql);

        // 3. Update atomic breakdown with actual times
        currentStageTimes = data.stage_times || {};
        var executedPlan = findPlanById(data.plan_used);
        if (executedPlan) {
            renderAtomicBreakdown(executedPlan, currentStageTimes);
            document.getElementById('atomic-plan-name').textContent = '— ' + escapeHtml(executedPlan.friendly_name || executedPlan.name);
        }
    } catch (e) {
        addTimelineEntry('✗ 执行失败: ' + e.message);
        alert('执行失败: ' + e.message);
    }
}

function findPlanById(planId) {
    for (var i = 0; i < currentPlans.length; i++) {
        if (currentPlans[i].id === planId) return currentPlans[i];
    }
    return null;
}
