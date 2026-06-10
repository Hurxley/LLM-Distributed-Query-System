// ═══════════════════════════════════════════════════
// FEDERATED QUERY SYSTEM — DOM Rendering
// ═══════════════════════════════════════════════════

// ── Utils ──
function escapeHtml(text) {
    if (!text) return '';
    const div = document.createElement('div');
    div.textContent = String(text);
    return div.innerHTML;
}

function formatMs(ms) {
    if (ms == null || ms === '') return '--';
    var num = parseFloat(ms);
    if (isNaN(num)) return String(ms);
    if (num === 0) return '<0.1ms';
    if (num < 0.1) return '<0.1ms';
    if (num < 10) return num.toFixed(1) + 'ms';
    return Math.round(num) + 'ms';
}

function formatMsNum(ms) {
    if (ms == null || ms === '') return 0;
    var num = parseFloat(ms);
    return isNaN(num) ? 0 : Math.round(num);
}

// ── Parse Result ──
function renderParseResult(ast) {
    if (!ast) return;
    var html = '';
    var filters = ast.filters || [];
    if (filters.length > 0) {
        for (var i = 0; i < filters.length; i++) {
            var f = filters[i];
            var workers = (f.workers || []).join(', ');
            html += '<div class="filter-item">' +
                escapeHtml(f.field) + ' ' + escapeHtml(f.op || 'eq') + ' ' + escapeHtml(f.value) +
                '<em>数据源: ' + escapeHtml(workers) + '</em></div>';
        }
    } else {
        html += '<div class="filter-item">无筛选条件</div>';
    }
    if (ast.aggregation) {
        var a = ast.aggregation;
        var funcLabel = FUNC_LABELS[a.func] || a.func;
        html += '<div class="agg-item">' + escapeHtml(funcLabel) + ' ' + escapeHtml(a.field) +
            ' <em>数据源: ' + escapeHtml((a.workers || []).join(', ')) + '</em></div>';
    }
    if (ast.errors && ast.errors.length) {
        html += '<div class="errors">⚠ ' + ast.errors.map(escapeHtml).join('<br>') + '</div>';
    }
    document.getElementById('parse-result').innerHTML = html;
}

// ── Parse SQL (populated after execution) ──
function renderParseSQL(stageSql) {
    var container = document.getElementById('parse-sql');
    if (!stageSql || !Object.keys(stageSql).length) {
        container.style.display = 'none';
        return;
    }
    currentStageSql = stageSql;

    var html = '<div class="parse-sql-header">📝 各阶段生成的SQL语句</div>';
    var entries = Object.entries(stageSql);
    for (var i = 0; i < entries.length; i++) {
        var sid = entries[i][0];
        var info = entries[i][1];
        html += '<div class="sql-block">' +
            '<div class="sql-block-header">' +
                '<span class="sql-stage-label">' + escapeHtml(sid) + '</span>' +
                '<span class="sql-stage-location">' + escapeHtml(info.location) + ' · ' + escapeHtml(info.type) + '</span>' +
            '</div>' +
            '<pre class="sql-code">' + escapeHtml(info.sql || '') + '</pre>' +
        '</div>';
    }
    container.innerHTML = html;
    container.style.display = 'block';
}

// ── Plan List ──
function renderPlans(plans) {
    document.getElementById('plan-count').textContent = '共 ' + plans.length + ' 个方案';

    var html = '';
    for (var i = 0; i < plans.length; i++) {
        var plan = plans[i];
        var kb = ((plan.estimated_egress_bytes || 0) / 1024).toFixed(1);
        var displayName = plan.friendly_name || plan.name || '方案';
        var displayDesc = plan.friendly_description || plan.description || '';

        html += '<div class="plan-block' +
            (plan.recommended ? ' recommended' : '') +
            '" onclick="selectPlan(\'' + plan.id + '\')" id="plan-' + plan.id + '">';

        // Header
        html += '<div class="plan-block-header">';
        html += '<span class="plan-block-name">' +
            escapeHtml(displayName) +
            (plan.recommended ? ' <span class="plan-rec-badge">推荐</span>' : '') +
        '</span>';
        html += '<span class="plan-block-meta">' +
            '<span class="cost">DAG预估 ' + formatMs(plan.estimated_cost_ms) + '</span>' +
            '<span class="egress">外传 ' + kb + 'KB</span>' +
        '</span>';
        html += '</div>';

        // Description (newline-separated steps)
        html += '<div class="plan-block-desc">' + escapeHtml(displayDesc).replace(/\n/g, '<br>') + '</div>';

        html += '</div>'; // plan-block
    }

    // Recommended note
    var rec = plans[0];
    html += '<div class="plan-notes">▶ 推荐方案：' +
        escapeHtml(rec ? (rec.friendly_name || rec.name) : 'N/A') +
        '，DAG预估 ' + formatMs(rec ? rec.estimated_cost_ms : null) + '</div>';

    document.getElementById('plan-list').innerHTML = html;
    document.getElementById('plan-actions').innerHTML =
        '<button class="primary" onclick="executeQuery()">▶ 执行推荐方案</button>' +
        '<button onclick="executeQueryWithPlan()">用所选方案执行</button>';
}

// ═══════════════════════════════════════════════════
// DEDICATED MODULE: Atomic Operation Breakdown
// ═══════════════════════════════════════════════════

function renderAtomicBreakdown(plan, stageTimes) {
    if (!plan) return;

    var costs = plan.stage_costs || {};
    var container = document.getElementById('atomic-breakdown');
    var hasActual = stageTimes && Object.keys(stageTimes).length > 0;

    if (!Object.keys(costs).length) {
        container.innerHTML = '<div class="muted">该方案暂无原子操作耗时数据</div>';
        return;
    }

    var totalEst = 0;
    var totalAct = 0;

    var html = '';
    html += '<table class="breakdown-table">';
    html += '<thead><tr>' +
        '<th class="bd-stage">阶段</th>' +
        '<th class="bd-ops">原子操作明细</th>' +
        '<th class="bd-est">预估耗时</th>' +
        (hasActual ? '<th class="bd-act">实际耗时</th>' : '') +
        (hasActual ? '<th class="bd-diff">偏差</th>' : '') +
    '</tr></thead><tbody>';

    var entries = Object.entries(costs);
    for (var i = 0; i < entries.length; i++) {
        var sid = entries[i][0];
        var ci = entries[i][1];
        var estMs = ci.total_ms || 0;
        totalEst += estMs;

        var actualMs = null;
        if (hasActual && stageTimes[sid] != null) {
            actualMs = stageTimes[sid];
            totalAct += actualMs;
        }

        var diffClass = '';
        var diffText = '';
        if (actualMs !== null) {
            var diff = Math.round((actualMs - estMs) * 10) / 10;
            if (diff <= 0) {
                diffClass = 'under';
                diffText = diff === 0 ? '±0' : (diff.toFixed(1) + 'ms');
            } else {
                diffClass = 'over';
                diffText = '+' + diff.toFixed(1) + 'ms';
            }
        }

        html += '<tr>' +
            '<td class="bd-stage"><span class="stage-badge">' + escapeHtml(sid) + '</span></td>' +
            '<td class="bd-ops">' + escapeHtml(ci.breakdown_label || '') + '</td>' +
            '<td class="bd-est">' + formatMs(estMs) + '</td>' +
            (hasActual ? '<td class="bd-act ' + (actualMs !== null ? (actualMs <= estMs ? 'under' : 'over') : '') + '">' + formatMs(actualMs) + '</td>' : '') +
            (hasActual ? '<td class="bd-diff ' + diffClass + '">' + (actualMs !== null ? diffText : '--') + '</td>' : '') +
        '</tr>';
    }

    // Totals row — show both simple sum and DAG-aware estimate
    var dagEst = plan.estimated_cost_ms || totalEst;
    var totalDiff = totalAct - totalEst;
    html += '<tr class="breakdown-total-row">' +
        '<td class="bd-stage"><strong>各阶段累加</strong></td>' +
        '<td class="bd-ops" style="color:var(--text-muted);font-size:0.85em;">（所有阶段依次执行的总和）</td>' +
        '<td class="bd-est"><strong>' + formatMs(totalEst) + '</strong></td>' +
        (hasActual ? '<td class="bd-act"><strong>' + formatMs(totalAct) + '</strong></td>' : '') +
        (hasActual ? '<td class="bd-diff ' + (totalAct <= totalEst ? 'under' : 'over') + '"><strong>' + (totalDiff >= 0 ? '+' : '') + formatMs(totalDiff) + '</strong></td>' : '') +
    '</tr>';

    // DAG-aware total row
    if (dagEst !== totalEst) {
        html += '<tr class="breakdown-dag-row">' +
            '<td class="bd-stage"><strong>DAG预估</strong></td>' +
            '<td class="bd-ops" style="color:var(--text-muted);font-size:0.85em;">（并行阶段取最大值，非简单累加）</td>' +
            '<td class="bd-est" style="color:var(--accent-green);font-weight:700;">' + formatMs(dagEst) + '</td>' +
            (hasActual ? '<td class="bd-act"></td>' : '') +
            (hasActual ? '<td class="bd-diff"></td>' : '') +
        '</tr>';
    }

    html += '</tbody></table>';

    // Legend
    if (hasActual) {
        html += '<div class="breakdown-legend">' +
            '<span class="legend-item"><span class="legend-dot under-dot"></span> 实际 ≤ 预估（优于预期）</span>' +
            '<span class="legend-item"><span class="legend-dot over-dot"></span> 实际 > 预估（慢于预期）</span>' +
        '</div>';
    } else {
        html += '<div class="breakdown-legend muted">执行后将展示实际耗时对比</div>';
    }

    container.innerHTML = html;
    document.getElementById('atomic-section').style.display = 'block';
}

// ═══════════════════════════════════════════════════
// Final Result
// ═══════════════════════════════════════════════════

function renderFinalResult(data) {
    var result = data.result || {};
    var count = result.count || 0;
    var value = result.value != null ? result.value : (result.avg != null ? result.avg : 0);
    var func = result.func || 'avg';
    var accuracy = data.accuracy || 'N/A';
    var funcLabel = FUNC_LABELS[func] || '统计';

    var html = '';

    // Big result number
    if (count === 0 && func !== 'count') {
        html += '<div class="result-big" style="color:var(--accent-amber);">未找到匹配数据</div>';
    } else if (func === 'count') {
        html += '<div class="result-big">共 ' + count.toLocaleString() + ' 人</div>';
    } else if (func === 'sum') {
        html += '<div class="result-big">' + count.toLocaleString() + ' 人，' + funcLabel + '值 ' +
            Number(value).toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2}) + ' 元</div>';
    } else {
        html += '<div class="result-big">' + count.toLocaleString() + ' 人，' + funcLabel + '值 ' +
            Number(value).toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2}) + ' 元</div>';
    }

    // Metadata row
    html += '<div class="result-meta">' +
        '<span>实测 ' + formatMs(data.total_ms) + '</span>' +
        '<span>预估 ' + formatMs(data.estimated_ms) + '</span>' +
        '<span>准确度 ' + accuracy + '</span>' +
        '<span>聚合: ' + funcLabel + '</span>' +
    '</div>';

    // Stage times summary (compact)
    if (data.stage_times && Object.keys(data.stage_times).length) {
        html += '<div class="stage-times">各阶段耗时: ';
        var entries = Object.entries(data.stage_times);
        for (var i = 0; i < entries.length; i++) {
            html += '<span>' + escapeHtml(entries[i][0]) + ': ' + formatMs(entries[i][1]) + '</span>';
            if (i < entries.length - 1) html += ' · ';
        }
        html += '</div>';
    }

    document.getElementById('final-result').innerHTML = html;
    updateProgress(100);
}
