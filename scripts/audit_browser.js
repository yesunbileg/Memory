// Loaded only by the dedicated headless Brave audit worker.
if (auditInput.action === "start") {
  const phases = {};
  const settling = new Set();
  function statsFor(phase) {
    return phases[phase] ??= {
      elapsedMs: 0, requests: 0, failed: 0, measured: 0, unavailable: 0, finished: 0,
      requestBody: 0, responseBody: 0, requestHeaders: 0, responseHeaders: 0,
      observedRequestBodyBytes: 0, observedFailedBodyBytes: 0, unknownRequestBodies: 0,
      saves: 0, saveBytes: 0, unknownSaveBodies: 0, unchangedMarkerSent: false,
      cdpRequests: 0, cdpFinished: 0, cdpFailed: 0, cdpRedirects: 0,
      cdpFinishedEncodedBytes: 0, cdpFailedPartialEncodedBytes: 0,
      cdpUnavailablePages: 0,
    };
  }
  const audit = {
    phase: "setup", phases, settling, phaseStart: Date.now(),
    finishPhase() {
      const now = Date.now();
      statsFor(this.phase).elapsedMs += now - this.phaseStart;
      this.phaseStart = now;
    },
    async watch(page) {
      const active = new Map();
      page.on("request", request => {
        const url = new URL(request.url());
        if (!["https:", "http:"].includes(url.protocol)) return;
        const stats = statsFor(audit.phase);
        let body;
        try { body = request.postDataBuffer(); } catch { body = null; }
        const count = body?.length ?? 0;
        stats.observedRequestBodyBytes += count;
        if (body === null && !["GET", "HEAD", "OPTIONS"].includes(request.method())) stats.unknownRequestBodies++;
        active.set(request, {stats, count});
        stats.requests++;
        if (url.hostname === "docs.google.com" && url.pathname.endsWith("/save")
            && request.method() === "POST") {
          stats.saves++;
          if (body == null) stats.unknownSaveBodies++;
          else {
            stats.saveBytes += count;
            stats.unchangedMarkerSent ||= body.includes(auditInput.marker);
          }
        }
      });
      page.on("requestfailed", request => {
        const item = active.get(request);
        if (item) {
          item.stats.failed++;
          item.stats.observedFailedBodyBytes += item.count;
        }
        active.delete(request);
      });
      page.on("requestfinished", request => {
        const item = active.get(request);
        if (!item) return;
        const {stats} = item;
        active.delete(request);
        stats.finished++;
        const task = request.sizes().then(sizes => {
          const keys = ["requestBody", "responseBody", "requestHeaders", "responseHeaders"];
          if (!keys.every(key => sizes[key + "Size"] >= 0)) {
            stats.unavailable++;
            return;
          }
          for (const key of keys) stats[key] += sizes[key + "Size"];
          stats.measured++;
        }).catch(() => { stats.unavailable++; });
        settling.add(task);
        task.finally(() => settling.delete(task));
      });
      try {
        const cdp = await context.newCDPSession(page);
        const transfers = new Map();
        cdp.on("Network.requestWillBeSent", e => {
          const previous = transfers.get(e.requestId);
          if (previous && e.redirectResponse) {
            previous.stats.cdpRedirects++;
            previous.stats.cdpFinished++;
            previous.stats.cdpFinishedEncodedBytes += Math.max(0, e.redirectResponse.encodedDataLength ?? 0);
          }
          if (!/^https?:/.test(e.request.url)) return;
          const stats = statsFor(audit.phase);
          stats.cdpRequests++;
          transfers.set(e.requestId, {stats, partial: 0});
        });
        cdp.on("Network.dataReceived", e => {
          const item = transfers.get(e.requestId);
          if (item) item.partial += Math.max(0, e.encodedDataLength ?? 0);
        });
        cdp.on("Network.loadingFinished", e => {
          const item = transfers.get(e.requestId);
          if (!item) return;
          item.stats.cdpFinished++;
          item.stats.cdpFinishedEncodedBytes += Math.max(0, e.encodedDataLength ?? 0);
          transfers.delete(e.requestId);
        });
        cdp.on("Network.loadingFailed", e => {
          const item = transfers.get(e.requestId);
          if (!item) return;
          item.stats.cdpFailed++;
          item.stats.cdpFailedPartialEncodedBytes += item.partial;
          transfers.delete(e.requestId);
        });
        await cdp.send("Network.enable");
      } catch {
        statsFor(audit.phase).cdpUnavailablePages++;
      }
    },
  };
  statsFor(audit.phase);
  audit.statsFor = statsFor;
  state.memoryDocsAudit = audit;
} else if (auditInput.action === "phase") {
  const audit = state.memoryDocsAudit;
  audit.finishPhase();
  audit.phase = auditInput.phase;
  audit.statsFor(audit.phase);
} else if (auditInput.action === "report") {
  const audit = state.memoryDocsAudit;
  await Promise.allSettled([...audit.settling]);
  audit.finishPhase();
  console.log("AI_MEMORY_DOCS_RESULT=" + JSON.stringify({status: "ok", metrics: audit.phases}));
  if (auditInput.stop) delete state.memoryDocsAudit;
}

if (auditInput.action !== "report") console.log('AI_MEMORY_DOCS_RESULT={"status":"ok"}');
