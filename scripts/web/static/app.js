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

  function workflowModeLabel(mode) {
    return mode === "manual_guard" ? "\u4eba\u5de5\u62a4\u822a\u6a21\u5f0f" :
      mode === "mature_automatic" ? "\u6210\u719f\u81ea\u52a8\u6a21\u5f0f" :
      mode || "\u672a\u77e5\u6a21\u5f0f";
  }

  function workflowModeImpact(mode) {
    return mode === "manual_guard"
      ? "\u7814\u7a76\u8ba1\u5212\u3001\u7814\u7a76\u7ed3\u679c\u3001\u5185\u5bb9\u8ba1\u5212\u3001\u521d\u7a3f\u3001\u5ba1\u6838\u548c\u6700\u7ec8\u7a3f\u90fd\u9700\u8981\u4eba\u5de5\u786e\u8ba4\u3002"
      : "\u7814\u7a76\u7ed3\u679c\u548c\u521d\u7a3f\u4f1a\u6309\u73b0\u6709\u89c4\u5219\u81ea\u52a8\u7ee7\u7eed\uff1b\u7814\u7a76\u8ba1\u5212\u3001\u5185\u5bb9\u8ba1\u5212\u3001\u5ba1\u6838\u548c\u6700\u7ec8\u7a3f\u4ecd\u9700\u4eba\u5de5\u786e\u8ba4\u3002";
  }

  function renderError(message) {
    document.querySelector("#loading-panel").hidden = true;
    document.querySelector("#status-panel").hidden = true;
    const panel = document.querySelector("#error-panel");
    panel.textContent = message;
    panel.hidden = false;
  }

  function domainActionControls(domain) {
    const label = escapeHtml(domain.domain_identity);
    const current = domain.current_cold_start || {};
    const mode = domain.workflow_mode || "";
    const buttons = [];
    if (current.status === "running") {
      buttons.push('<button data-web-action="stop" data-domain="' + label + '">停止当前 cold-start</button>');
    }
    if (current.status === "stopped" || current.status === "failed") {
      buttons.push('<button data-web-action="resume" data-domain="' + label + '">恢复当前 cold-start</button>');
    }
    if (mode === "manual_guard" || mode === "mature_automatic") {
      const target = mode === "manual_guard" ? "mature_automatic" : "manual_guard";
      buttons.push(
        '<div class="workflow-mode-controls">' +
        '<p><strong>当前工作模式：</strong>' + escapeHtml(workflowModeLabel(mode)) + '</p>' +
        '<label>目标模式<select data-mode-target>' +
        '<option value="' + escapeHtml(target) + '">' + escapeHtml(workflowModeLabel(target)) + '</option>' +
        '</select></label>' +
        '<p data-mode-impact><strong>切换影响：</strong>' + escapeHtml(workflowModeImpact(target)) + '</p>' +
        '<label>切换理由<textarea data-mode-reason rows="2" placeholder="请说明为什么切换"></textarea></label>' +
        '<button type="button" data-web-action="change-workflow-mode" data-domain="' + label + '">确认切换工作模式</button>' +
        '</div>'
      );
    }
    return buttons.join("");
  }

  function renderDomain(domain) {
    const waiting = domain.waiting_human
      ? (domain.waiting_for || []).map(function (item) {
          return escapeHtml(item.kind);
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
      '<article class="domain-card" data-domain-card="' + escapeHtml(domain.domain_identity) + '">',
      "<h3>" + escapeHtml(domain.name || domain.domain_identity) + "</h3>",
      '<div class="domain-details">',
      '<p><span>\u5de5\u4f5c\u6a21\u5f0f</span>' + escapeHtml(workflowModeLabel(domain.workflow_mode)) + "</p>",
      '<p><span>\u9886\u57DF\u6807\u8BC6</span>' + escapeHtml(domain.domain_identity) + "</p>",
      '<p><span>\u5F53\u524D activation</span>' + (domain.current_activation.exists ? "\u6709" : "\u65E0") + "</p>",
      '<p><span>\u5F53\u524D cold-start</span>' + escapeHtml(domain.current_cold_start.status) + "</p>",
      '<p><span>waiting_human</span>' + waiting + "</p>",
      '<p><span>\u5F53\u524D daily</span>' + escapeHtml(daily) + "</p>",
      '<p><span>\u5F53\u524D\u8FDB\u884C\u4E2D</span>' + escapeHtml(currentRuns) + "</p>",
      '<p><span>\u5141\u8BB8\u65B0 cold-start</span>' + yesNo(domain.can_start_new_cold_start) + "</p>",
      "</div>",
      '<div class="action-controls">' + domainActionControls(domain) + "</div>",
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
    renderActionChoices(status);
    document.querySelector("#loading-panel").hidden = true;
    document.querySelector("#error-panel").hidden = true;
    document.querySelector("#status-panel").hidden = false;
  }

  function experienceStatus(status) {
    if (status === "validation_ready") return "\u5f85\u771f\u5b9e\u9a8c\u8bc1";
    if (status === "promoted") return "\u5df2\u664b\u5347\u6b63\u5f0f\u7ecf\u9a8c";
    return {
      awaiting_human_decision: "等待人工决定",
      preparing: "准备中",
      accepted: "已接受",
      rejected: "已拒绝",
      no_proposal: "无可用提案",
      failed: "处理失败",
    }[status] || status || "未知";
  }

  function readableValue(value) {
    if (Array.isArray(value)) return value.join("；");
    if (value && typeof value === "object") return JSON.stringify(value);
    return String(value == null ? "" : value);
  }

  function renderExperienceCandidates(candidates) {
    const container = document.querySelector("#experience-candidates");
    if (!candidates.length) {
      container.innerHTML = '<p class="notice">当前没有候选经验。</p>';
      return;
    }
    container.innerHTML = candidates.map(function (item) {
      const candidate = item.candidate || {};
      const sources = (item.sources || []).map(function (source) {
        const quotes = (source.evidence_quotes || []).slice(0, 2).map(escapeHtml).join("；");
        return '<li><strong>' + escapeHtml(source.title || "来源") + '</strong>' +
          '<span>' + escapeHtml(source.account_name || "") + '</span>' +
          (quotes ? '<small>' + quotes + '</small>' : '') + '</li>';
      }).join("");
      const waiting = item.status === "awaiting_human_decision";
      const validationReady = item.status === "validation_ready";
      const validationActionMarkup = validationReady
        ? '<label>\u664b\u5347\u7406\u7531<textarea data-experience-reason rows="2" placeholder="\u8bf7\u8bf4\u660e\u4e3a\u4ec0\u4e48\u6839\u636e\u9a8c\u8bc1\u7ed3\u679c\u664b\u5347"></textarea></label>' +
          '<div class="experience-actions">' +
          '<button type="button" data-experience-action="promote_experience_candidate" data-candidate-id="' + escapeHtml(item.experience_candidate_id) + '">\u4eba\u5de5\u664b\u5347\u6b63\u5f0f\u7ecf\u9a8c</button>' +
          '</div>'
        : null;
      const actionMarkup = validationReady ? validationActionMarkup : waiting
        ? '<label>决定理由<textarea data-experience-reason rows="2" placeholder="请说明接受或拒绝的理由"></textarea></label>' +
          '<div class="experience-actions">' +
          '<button type="button" data-experience-action="accept_experience_candidate" data-candidate-id="' + escapeHtml(item.experience_candidate_id) + '">接受</button>' +
          '<button type="button" data-experience-action="reject_experience_candidate" data-candidate-id="' + escapeHtml(item.experience_candidate_id) + '">拒绝</button>' +
          '</div>'
        : '<p class="notice">当前状态不能进行人工确认。</p>';
      return '<article class="experience-card" data-candidate-card="' + escapeHtml(item.experience_candidate_id) + '">' +
        '<div class="experience-card-header"><h3>' + escapeHtml(candidate.summary || "候选经验") + '</h3>' +
        '<span class="experience-status">' + escapeHtml(experienceStatus(item.status)) + '</span></div>' +
        '<p><strong>适用条件：</strong>' + escapeHtml(readableValue(candidate.applicable_when)) + '</p>' +
        '<p><strong>方法：</strong>' + escapeHtml(readableValue(candidate.method)) + '</p>' +
        '<p><strong>边界：</strong>' + escapeHtml(readableValue(candidate.boundary)) + '</p>' +
        '<p><strong>依据来源：</strong>' + escapeHtml(item.source_count) + ' 条</p>' +
        '<details><summary>查看来源与证据摘要</summary><ul class="experience-sources">' + sources + '</ul></details>' +
        actionMarkup +
        '</article>';
    }).join("");
  }

  async function loadExperienceCandidates() {
    try {
      const response = await fetch("/api/experience-candidates", { method: "GET", cache: "no-store" });
      const payload = await response.json();
      if (!response.ok || !payload.ok) {
        document.querySelector("#experience-candidates").innerHTML = '<p class="notice error">' +
          escapeHtml(payload.error || "候选经验读取失败") + '</p>';
        return;
      }
      renderExperienceCandidates(payload.candidates || []);
    } catch (error) {
      document.querySelector("#experience-candidates").innerHTML = '<p class="notice error">无法连接 Creation Assistant Core</p>';
    }
  }

  function contentNodeLabel(node) {
    return {
      research_plan: "研究计划",
      deep_research: "实际研究结果",
      content_plan: "内容计划",
      formal_draft: "初稿",
      copy_optimization: "文案优化",
      de_ai_revision: "去模板化修订",
      review: "审核",
      user_final_confirmation: "最终稿确认",
    }[node] || node || "未知节点";
  }

  function renderContentTasks(tasks) {
    const container = document.querySelector("#content-tasks");
    if (!tasks.length) {
      container.innerHTML = '<p class="notice">当前没有正式内容生产任务。</p>';
      return;
    }
    container.innerHTML = tasks.map(function (task) {
      const artifact = task.current_artifact || {};
      const artifactPayload = artifact.payload || artifact;
      const artifactText = JSON.stringify(artifactPayload || {}, null, 2);
      const waiting = task.current_status === "awaiting_human_review";
      const finalConfirmation = task.current_node === "user_final_confirmation" && task.current_status === "approved";
      const controls = waiting
        ? '<label>本次确认理由或修改要求<textarea data-content-reason rows="2"></textarea></label>' +
          '<div class="experience-actions">' +
          '<button type="button" data-content-action="approve_content_node" data-task-id="' + escapeHtml(task.task_id) + '">确认并继续</button>' +
          '<button type="button" data-content-action="return_content_node" data-task-id="' + escapeHtml(task.task_id) + '">要求修改</button>' +
          '</div>'
        : finalConfirmation
          ? '<p class="notice">已到最终稿确认节点，仍需用户单独确认。</p>'
          : '<p class="notice">当前等待外部执行者完成：' + escapeHtml(contentNodeLabel(task.current_node)) + '</p>';
      return '<article class="experience-card content-task-card" data-content-task-card="' + escapeHtml(task.task_id) + '">' +
        '<div class="experience-card-header"><h3>' + escapeHtml(task.topic && task.topic.payload && task.topic.payload.title || "正式内容任务") + '</h3>' +
        '<span class="experience-status">' + escapeHtml(contentNodeLabel(task.current_node)) + ' / ' + escapeHtml(task.current_status) + '</span></div>' +
        '<details><summary>查看当前内容</summary><pre class="summary">' + escapeHtml(artifactText) + '</pre></details>' +
        controls +
        '</article>';
    }).join("");
  }

  async function loadContentTasks() {
    try {
      const response = await fetch("/api/content-tasks", { method: "GET", cache: "no-store" });
      const payload = await response.json();
      if (!response.ok || !payload.ok) {
        document.querySelector("#content-tasks").innerHTML = '<p class="notice error">' +
          escapeHtml(payload.error || "正式内容任务读取失败") + '</p>';
        return;
      }
      renderContentTasks(payload.tasks || []);
      renderPublicationTasks(payload.tasks || []);
    } catch (error) {
      document.querySelector("#content-tasks").innerHTML = '<p class="notice error">无法连接 Creation Assistant Core</p>';
    }
  }

  function renderPublicationTasks(tasks) {
    const container = document.querySelector("#publication-tasks");
    const actionable = (tasks || []).filter(function (task) {
      return task.current_node === "user_final_confirmation" && task.current_status === "approved";
    });
    if (!actionable.length) {
      container.innerHTML = '<p class="notice">当前没有可登记发布的内容。</p>';
      return;
    }
    container.innerHTML = actionable.map(function (task) {
      const title = task.topic && task.topic.payload && task.topic.payload.title || "正式内容";
      const finalConfirmed = (task.content_decisions || []).some(function (decision) {
        return decision.node === "user_final_confirmation" && decision.decision === "approved";
      });
      if (!finalConfirmed) {
        return '<article class="experience-card content-task-card" data-content-task-card="' + escapeHtml(task.task_id) + '">' +
          '<h3>' + escapeHtml(title) + '</h3>' +
          '<p class="notice">发布前需要先完成最终稿确认。</p>' +
          '<label>最终稿确认理由<textarea data-content-reason rows="2"></textarea></label>' +
          '<div class="experience-actions"><button type="button" data-content-action="approve_final_content" data-task-id="' + escapeHtml(task.task_id) + '">确认最终稿</button></div>' +
          '</article>';
      }
      return '<article class="experience-card content-task-card" data-publication-task-card="' + escapeHtml(task.task_id) + '">' +
        '<h3>' + escapeHtml(title) + '</h3>' +
        '<p class="notice">请先在系统外完成发布，再登记真实发布信息。</p>' +
        '<label>平台<input data-publication-field="platform" type="text"></label>' +
        '<label>作品链接<input data-publication-field="external_video_url" type="url"></label>' +
        '<label>真实发布时间<input data-publication-field="published_at" type="datetime-local"></label>' +
        '<label>发布内容<select data-publication-field="actual_content_status"><option value="same_as_approved">与确认稿一致</option><option value="different_from_approved">与确认稿有变化</option></select></label>' +
        '<label>变化说明（如有）<textarea data-publication-field="actual_content_note" rows="2"></textarea></label>' +
        '<div class="experience-actions"><button type="button" data-publication-action="register_publication" data-task-id="' + escapeHtml(task.task_id) + '">登记已发布内容</button></div>' +
        '</article>';
    }).join("");
  }

  function validationResultFields(publication) {
    return (publication.validation_usage || []).map(function (usage) {
      const candidate = usage.candidate || {};
      const summary = candidate.summary || usage.experience_candidate_id;
      return '<fieldset data-validation-candidate="' + escapeHtml(usage.experience_candidate_id) + '">' +
        '<legend>' + escapeHtml(summary) + '</legend>' +
        '<label>验证结果<select data-validation-field="result"><option value="supports">supports</option><option value="contradicts">contradicts</option><option value="inconclusive">inconclusive</option></select></label>' +
        '<label>实际表现摘要<textarea data-validation-field="performance_summary" rows="2"></textarea></label>' +
        '<label>复盘依据<textarea data-validation-field="review_basis" rows="2"></textarea></label>' +
        '</fieldset>';
    }).join("");
  }

  function renderPublications(publications) {
    const container = document.querySelector("#publications");
    if (!publications.length) {
      container.innerHTML = '<p class="notice">当前还没有发布登记。</p>';
      return;
    }
    container.innerHTML = publications.map(function (item) {
      const publication = item.publication || {};
      const observations = item.observations || [];
      const reviews = item.reviews || [];
      const review = reviews.length ? reviews[reviews.length - 1] : null;
      const observed = {};
      observations.forEach(function (observation) { observed[observation.point_code] = observation; });
      const pointForms = ["P0", "P1", "P2", "P3", "P4", "P5", "P6", "P7"].map(function (point) {
        const observation = observed[point];
        if (observation) {
          return '<li>' + escapeHtml(point) + '：' + escapeHtml(observation.observation_status) + '</li>';
        }
        return '<li><strong>' + escapeHtml(point) + '</strong>' +
          '<label>数据（JSON）<textarea data-observation-field="metrics" rows="1">{}</textarea></label>' +
          '<label>缺失说明（如适用）<input data-observation-field="missing_reason" type="text"></label>' +
          '<label>来源记录<input data-observation-field="source_ref" type="text"></label>' +
          '<label>观察时间<input data-observation-field="observed_at" type="datetime-local"></label>' +
          '<select data-observation-field="observation_status"><option value="recorded">已记录</option><option value="missing">缺失</option></select>' +
          '<button type="button" data-publication-action="record_publication_observation" data-publication-id="' + escapeHtml(publication.publication_id) + '" data-point-code="' + point + '">记录' + escapeHtml(point) + '</button></li>';
      }).join("");
      const complete = observations.length === 8 && ["P0", "P1", "P2", "P3", "P4", "P5", "P6", "P7"].every(function (point) { return observed[point]; });
      let reviewMarkup = '';
      if (complete && !review) {
        reviewMarkup = '<details><summary>准备 P7 复盘</summary>' +
          '<label>选题复盘<textarea data-p7-field="selection_assessment" rows="2"></textarea></label>' +
          '<label>叙事复盘<textarea data-p7-field="narrative_assessment" rows="2"></textarea></label>' +
          '<label>材料复盘<textarea data-p7-field="material_assessment" rows="2"></textarea></label>' +
          '<label>外部条件复盘<textarea data-p7-field="external_conditions_assessment" rows="2"></textarea></label>' +
          validationResultFields(item) +
          '<button type="button" data-publication-action="prepare_p7_review" data-publication-id="' + escapeHtml(publication.publication_id) + '">提交 P7 复盘</button></details>';
      } else if (review && review.status === "awaiting_user_confirmation") {
        reviewMarkup = '<label>P7 确认理由<textarea data-p7-reason rows="2"></textarea></label>' +
          '<button type="button" data-publication-action="confirm_p7_review" data-review-id="' + escapeHtml(review.review_id) + '" data-decision="confirmed">确认 P7 复盘</button>';
      } else if (review) {
        reviewMarkup = '<p class="notice">P7 复盘状态：' + escapeHtml(review.status) + '</p>';
      }
      return '<article class="experience-card publication-card" data-publication-card="' + escapeHtml(publication.publication_id) + '">' +
        '<h3>' + escapeHtml(publication.platform || "已发布内容") + '</h3>' +
        '<p>作品：' + escapeHtml(publication.external_video_url || "") + '</p>' +
        '<p>发布时间：' + escapeHtml(publication.published_at || "") + '</p>' +
        '<details><summary>P0-P7 观察记录</summary><ul>' + pointForms + '</ul></details>' +
        reviewMarkup +
        '</article>';
    }).join("");
  }

  async function loadPublications() {
    try {
      const response = await fetch("/api/publications", { method: "GET", cache: "no-store" });
      const payload = await response.json();
      if (!response.ok || !payload.ok) {
        document.querySelector("#publications").innerHTML = '<p class="notice error">' + escapeHtml(payload.error || "发布反馈读取失败") + '</p>';
        return;
      }
      renderPublications(payload.publications || []);
    } catch (error) {
      document.querySelector("#publications").innerHTML = '<p class="notice error">无法连接 Creation Assistant Core</p>';
    }
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

  function renderActionChoices(status) {
    const select = document.querySelector("#review-domain");
    const waiting = (status.domains || []).filter(function (domain) {
      return domain.waiting_human;
    });
    select.innerHTML = waiting.map(function (domain) {
      return '<option value="' + escapeHtml(domain.domain_identity) + '">' +
        escapeHtml(domain.name || domain.domain_identity) + "</option>";
    }).join("");
    if (!waiting.length) {
      select.innerHTML = '<option value="">当前没有等待人工确认的领域</option>';
    }
  }

  function showActionResult(payload) {
    const result = document.querySelector("#action-result");
    result.textContent = JSON.stringify({
      outcome: payload.outcome,
      core_result: payload.core_result,
      error: payload.error,
    }, null, 2);
    result.hidden = false;
  }

  async function postAction(action, values) {
    try {
      const response = await fetch("/api/action", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(Object.assign({ action: action }, values || {})),
      });
      const payload = await response.json();
      showActionResult(payload);
      if (payload.status) renderStatus(payload.status);
      if (action === "accept_experience_candidate" || action === "reject_experience_candidate" || action === "promote_experience_candidate") {
        loadExperienceCandidates();
      }
      if (action === "approve_content_node" || action === "return_content_node" || action === "approve_final_content") {
        loadContentTasks();
      }
      if (action === "register_publication" || action === "record_publication_observation" || action === "prepare_p7_review" || action === "confirm_p7_review") {
        loadPublications();
        loadContentTasks();
      }
    } catch (error) {
      showActionResult({ outcome: "failed", error: "Core动作调用失败" });
    }
  }

  function coldStartConfiguration() {
    const refs = document.querySelector("#cold-competitor-refs").value
      .split(/\r?\n/)
      .map(function (value) { return value.trim(); })
      .filter(Boolean);
    return {
      domain_name: document.querySelector("#cold-domain-name").value.trim(),
      owned_account: {
        display_name: "本地Web自营账号",
        external_account_ref: document.querySelector("#cold-owned-ref").value.trim(),
      },
      competitor_accounts: refs.map(function (value, index) {
        return { display_name: "本地Web对标" + (index + 1), external_account_ref: value };
      }),
    };
  }

  document.addEventListener("click", function (event) {
    const button = event.target.closest("button[data-web-action],button[data-experience-action],button[data-content-action],button[data-publication-action]");
    if (!button) return;
    if (button.dataset.experienceAction) {
      const card = button.closest("[data-candidate-card]");
      const reason = card ? card.querySelector("[data-experience-reason]").value.trim() : "";
      if (!reason) {
        showActionResult({ outcome: "rejected", error: "请填写决定理由" });
        return;
      }
      postAction(button.dataset.experienceAction, {
        experience_candidate_id: button.dataset.candidateId,
        reason: reason,
      });
      return;
    }
    if (button.dataset.contentAction) {
      const card = button.closest("[data-content-task-card],[data-publication-task-card]");
      const reason = card ? card.querySelector("[data-content-reason]").value.trim() : "";
      if (!reason) {
        showActionResult({ outcome: "rejected", error: "请填写确认理由或修改要求" });
        return;
      }
      if (button.dataset.contentAction === "approve_content_node") {
        postAction("approve_content_node", {
          task_id: button.dataset.taskId,
          reason: reason,
        });
      } else if (button.dataset.contentAction === "approve_final_content") {
        postAction("approve_final_content", {
          task_id: button.dataset.taskId,
          reason: reason,
        });
      } else {
        postAction("return_content_node", {
          task_id: button.dataset.taskId,
          requirements: reason,
        });
      }
      return;
    }
    if (button.dataset.publicationAction) {
      const action = button.dataset.publicationAction;
      if (action === "register_publication") {
        const card = button.closest("[data-publication-task-card]");
        const value = function (name) {
          const field = card ? card.querySelector('[data-publication-field="' + name + '"]') : null;
          return field ? field.value.trim() : "";
        };
        const values = {
          task_id: button.dataset.taskId,
          platform: value("platform"),
          external_video_url: value("external_video_url"),
          published_at: value("published_at"),
          actual_content_status: value("actual_content_status"),
          actual_content_note: value("actual_content_note"),
        };
        if (!values.platform || !values.external_video_url || !values.published_at) {
          showActionResult({ outcome: "rejected", error: "请填写平台、作品链接和真实发布时间" });
          return;
        }
        postAction(action, values);
        return;
      }
      if (action === "record_publication_observation") {
        const row = button.closest("li");
        let metrics;
        try {
          metrics = JSON.parse((row.querySelector('[data-observation-field="metrics"]') || {}).value || "{}");
        } catch (error) {
          showActionResult({ outcome: "rejected", error: "观察数据不是有效JSON" });
          return;
        }
        const value = function (name) {
          const field = row.querySelector('[data-observation-field="' + name + '"]');
          return field ? field.value.trim() : "";
        };
        postAction(action, {
          publication_id: button.dataset.publicationId,
          point_code: button.dataset.pointCode,
          observation_status: value("observation_status"),
          metrics: metrics,
          missing_reason: value("missing_reason"),
          source_ref: value("source_ref"),
          observed_at: value("observed_at"),
        });
        return;
      }
      if (action === "prepare_p7_review") {
        const card = button.closest("[data-publication-card]");
        const validationResults = [];
        let invalidValidation = false;
        card.querySelectorAll("[data-validation-candidate]").forEach(function (fieldset) {
          const value = function (name) {
            const field = fieldset.querySelector('[data-validation-field="' + name + '"]');
            return field ? field.value.trim() : "";
          };
          validationResults.push({
            experience_candidate_id: fieldset.dataset.validationCandidate,
            result: value("result"),
            performance_summary: value("performance_summary"),
            review_basis: value("review_basis"),
          });
          if (!value("performance_summary") || !value("review_basis")) invalidValidation = true;
        });
        if (invalidValidation) {
          showActionResult({ outcome: "rejected", error: "请补充每条验证候选的表现摘要和复盘依据" });
          return;
        }
        const value = function (name) {
          const field = card.querySelector('[data-p7-field="' + name + '"]');
          return field ? field.value.trim() : "";
        };
        postAction(action, {
          publication_id: button.dataset.publicationId,
          selection_assessment: value("selection_assessment"),
          narrative_assessment: value("narrative_assessment"),
          material_assessment: value("material_assessment"),
          external_conditions_assessment: value("external_conditions_assessment"),
          feedback_candidate: validationResults.length ? { validation_results: validationResults } : {},
        });
        return;
      }
      if (action === "confirm_p7_review") {
        const card = button.closest("[data-publication-card]");
        const reasonField = card ? card.querySelector("[data-p7-reason]") : null;
        const reason = reasonField ? reasonField.value.trim() : "";
        if (!reason) {
          showActionResult({ outcome: "rejected", error: "请填写P7确认理由" });
          return;
        }
        postAction(action, {
          review_id: button.dataset.reviewId,
          decision: button.dataset.decision,
          reason: reason,
        });
      }
      return;
    }
    const domain = button.dataset.domain;
    if (button.dataset.webAction === "change-workflow-mode") {
      const card = button.closest("[data-domain-card]");
      const targetField = card ? card.querySelector("[data-mode-target]") : null;
      const reasonField = card ? card.querySelector("[data-mode-reason]") : null;
      const target = targetField ? targetField.value : "";
      const reason = reasonField ? reasonField.value.trim() : "";
      if (!target || !reason) {
        showActionResult({ outcome: "rejected", error: "请填写切换理由" });
        return;
      }
      postAction("change_workflow_mode", {
        domain_label: domain,
        workflow_mode: target,
        reason: reason,
      });
      return;
    }
    if (button.dataset.webAction === "stop") {
      const reason = window.prompt("请说明停止原因");
      if (reason) postAction("cold_start_stop", { domain_label: domain, reason: reason });
    } else if (button.dataset.webAction === "resume") {
      postAction("cold_start_resume", { domain_label: domain });
    }
  });

  document.addEventListener("change", function (event) {
    const select = event.target.closest("select[data-mode-target]");
    if (!select) return;
    const card = select.closest("[data-domain-card]");
    const impact = card ? card.querySelector("[data-mode-impact]") : null;
    if (impact) {
      impact.innerHTML = "<strong>切换影响：</strong>" + escapeHtml(workflowModeImpact(select.value));
    }
  });

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelector("#cold-preview").addEventListener("click", function () {
      postAction("cold_start_preview", { configuration: coldStartConfiguration() });
    });
    document.querySelector("#cold-confirm").addEventListener("click", function () {
      postAction("cold_start_confirm");
    });
    document.querySelector("#review-submit").addEventListener("click", function () {
      let decisions;
      try {
        decisions = JSON.parse(document.querySelector("#review-decisions").value || "[]");
      } catch (error) {
        showActionResult({ outcome: "rejected", error: "决定列表不是有效JSON" });
        return;
      }
      postAction(document.querySelector("#review-kind").value, {
        domain_label: document.querySelector("#review-domain").value,
        decisions: decisions,
        reason: document.querySelector("#review-reason").value.trim(),
      });
    });
    document.querySelector("#daily-start").addEventListener("click", function () {
      const values = {
        domain_label: document.querySelector("#daily-domain").value.trim(),
      };
      const businessDate = document.querySelector("#daily-date").value.trim();
      if (businessDate) {
        values.business_date = businessDate;
      }
      postAction("daily_start", values);
    });
    loadStatus();
    loadExperienceCandidates();
    loadContentTasks();
    loadPublications();
  });
}());
