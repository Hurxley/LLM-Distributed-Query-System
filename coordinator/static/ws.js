// ═══════════════════════════════════════════════════
// FEDERATED QUERY SYSTEM — WebSocket & Timeline
// ═══════════════════════════════════════════════════

// ── WebSocket ──
function connectWebSocket(queryId) {
    if (ws) { try { ws.close(); } catch (e) {} }
    var protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    var wsUrl = protocol + '//' + location.host + '/ws/' + queryId;
    ws = new WebSocket(wsUrl);
    ws.onmessage = function(event) {
        try { handleWSEvent(JSON.parse(event.data)); } catch (e) {}
    };
    ws.onerror = function() {};
    ws.onclose = function() {};
}

function handleWSEvent(data) {
    var event = data.event;
    if (event === 'stage_start') {
        addTimelineEntry('→ ' + data.location + ' 开始 ' + data.stage_type);
        updateStageCard(data.stage_id, data.stage_type, data.location, 'running', 0, data.estimated_ms || 120);
    } else if (event === 'stage_complete') {
        addTimelineEntry('✓ ' + data.location + ' ' + data.stage_type + ' 完成 (' + formatMs(data.elapsed_ms) + ') ' + (data.result_summary || ''));
        updateStageCard(data.stage_id, data.stage_type, data.location, 'done', data.elapsed_ms, data.elapsed_ms);
    } else if (event === 'stage_error') {
        addTimelineEntry('✗ ' + data.stage_id + ' 错误: ' + data.error);
        updateStageCard(data.stage_id, 'error', '', 'error', 0, 0);
    } else if (event === 'execution_start') {
        addTimelineEntry('▶ 执行计划: ' + data.plan_name + ' (预估 ' + formatMs(data.estimated_ms) + ')');
    } else if (event === 'execution_complete') {
        addTimelineEntry('✓ 执行完成 — 总耗时 ' + formatMs(data.total_ms));
        updateProgress(100);
    } else if (event === 'query_complete') {
        updateProgress(100);
    }
}

// ── Timeline ──
function addTimelineEntry(text) {
    var timeline = document.getElementById('event-timeline');
    var time = new Date().toLocaleTimeString('zh-CN', { hour12: false });
    var div = document.createElement('div');
    div.className = 'timeline-entry';
    div.textContent = '[' + time + '] ' + text;
    timeline.appendChild(div);
    timeline.scrollTop = timeline.scrollHeight;
}

function updateStageCard(sid, stype, location, status, elapsed, estimated) {
    var container = document.getElementById('stage-cards');
    var card = document.getElementById('stage-' + sid);
    if (!card) {
        card = document.createElement('div');
        card.id = 'stage-' + sid;
        card.className = 'stage-card';
        container.appendChild(card);
    }
    var pct = estimated > 0 ? Math.min(100, Math.round(elapsed / estimated * 100)) : 0;
    var icon = status === 'done' ? '✔' : status === 'running' ? '⟳' : status === 'error' ? '✗' : '○';
    card.className = 'stage-card ' + status;
    card.innerHTML =
        '<div class="stage-header">' +
            '<span class="stage-icon">' + icon + '</span>' +
            '<span class="stage-name">' + escapeHtml(location) + '<br><small>' + escapeHtml(stype) + '</small></span>' +
            '<span class="stage-time">' + formatMs(elapsed) + '<br><small>/' + formatMs(estimated) + '</small></span>' +
        '</div>' +
        '<div class="stage-progress"><div class="stage-progress-fill" style="width:' + pct + '%"></div></div>';
}

function updateProgress(pct) {
    document.getElementById('progress-bar').style.width = pct + '%';
}
