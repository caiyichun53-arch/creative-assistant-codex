(function () {
  "use strict";

  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function yesNo(value) {
    return value ? "\u662f" : "\u5426";
  }

  function renderError(message) {
    document.querySelector("#loading-panel").hidden = true;
    document.querySelector("#status-panel").hidden = true;
    const panel = document.querySelector("#error-panel");
    panel.textContent = message;
    panel.hidden = false;
  }

  function renderDomain(domain) {
    const waiting = domain.waiting_human
      ? (domain.waiting_for || []).map(function (item) {
          return escapeHtml(item.kind + "\uFF1A" + item.id);
        }).join("\u3001") || "\u7B49\u5F85\u4EBA\u5DE5\u786E\u8BA4"
      : "\u5426";
    const daily = domain.current_daily && domain.current_daily.exists
      ? domain.current_daily.lifecycle
      : "\u65E0";
    const currentRuns = (domain.in_progress_business_runs || []).length;
    const historical = domain.history && domain.history.unfinished_business_runs
      ? domain.history.unfinished_business_runs.total
      : 0;
    return [
      '<article class="domain-card">',
      "<h3>" + escapeHtml(domain.name || domain.domain_identity) + "</h3>",
      '<div class="domain-details">',
      '<p><span>\u9886\u57DF\u6807\u8BC6</span>' + escapeHtml(domain.domain_identity) + "</p>",
      '<p><span>\u5F53\u524D activation</span>' + (domain.current_activation.exists ? "\u6709" : "\u65E0") + "</p>",
      '<p><span>\u5F53\u524D cold-start</span>' + escapeHtml(domain.current_cold_start.status) + "</p>",
      '<p><span>waiting_human</span>' + waiting + "</p>",
      '<p><span>\u5F53\u524D daily</span>' + escapeHtml(daily) + "</p>",
      '<p><span>\u5F53\u524D\u8FDB\u884C\u4E2D</span>' + escapeHtml(currentRuns) + "</p>",
      '<p><span>\u5141\u8BB8\u65B0 cold-start</span>' + yesNo(domain.can_start_new_cold_start) + "</p>",
      "</div>",
      '<div class="historical">\u5386\u53F2\u672A\u5B8C\u6210：' + escapeHtml(historical) + ' \u6761；historical/non-current，\u4E0D\u5C5E\u4E8E\u5F53\u524D Business Run。</div>',
      "</article>",
    ].join("");
  }

  function renderStatus(status) {
    const system = status.system;
    const executor = system.executor;
    const schedule = system.daily_schedule;
    document.querySelector("#identity").textContent = system.runtime_identity;
    document.querySelector("#migration-protection").textContent = yesNo(system.migration_protection);
    document.querySelector("#database-readable").textContent = system.database.readable ? "\u53EF\u8BFB（\u53EA\u8BFB）" : "\u4E0D\u53EF\u8BFB";
    document.querySelector("#current-in-progress").textContent = status.business_runs.in_progress.length;
    document.querySelector("#historical-unfinished").textContent = status.history.unfinished_business_runs_total;
    document.querySelector("#project-environment").textContent = system.project_environment.status;
    document.querySelector("#daily-schedule").textContent = (schedule.enabled ? "\u5F00\u542F" : "\u5173\u95ED") + "，" + schedule.time;
    document.querySelector("#default-executor").textContent = system.default_executor || "\u672A\u914D\u7F6E";
    document.querySelector("#executor-config").textContent = executor.configured && executor.launch_configuration_valid
      ? "\u5DF2\u914D\u7F6E（\u672A\u542F\u52A8\u63A2\u6D3B）"
      : "\u672A\u5B8C\u6574\u914D\u7F6E";
    document.querySelector("#domains").innerHTML = status.domains.map(renderDomain).join("");
    document.querySelector("#human-summary").textContent = status.human_summary;
    document.querySelector("#loading-panel").hidden = true;
    document.querySelector("#error-panel").hidden = true;
    document.querySelector("#status-panel").hidden = false;
  }

  async function loadStatus() {
    try {
      const response = await fetch("/api/status", { method: "GET", cache: "no-store" });
      const payload = await response.json();
      if (!response.ok || !payload.ok) {
        renderError(payload.error || "\u72B6\u6001\u8BFB\u53D6\u5931\u8D25");
        return;
      }
      renderStatus(payload.status);
    } catch (error) {
      renderError("\u72B6\u6001\u8BFB\u53D6\u5931\u8D25：\u65E0\u6CD5\u8FDE\u63A5 Creation Assistant Core \u9875\u9762\u670D\u52A1");
    }
  }

  document.addEventListener("DOMContentLoaded", loadStatus);
}());
