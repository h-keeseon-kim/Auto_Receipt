(function () {
    "use strict";

    const root = document.querySelector("[data-statement-results]");
    if (!root || !root.dataset.statusUrl) return;
    let activeController = null;
    let requestSerial = 0;
    let pollTimer = null;
    const statusMessage = document.createElement("p");
    statusMessage.className = "muted small";
    statusMessage.setAttribute("role", "status");
    statusMessage.setAttribute("aria-live", "polite");
    root.before(statusMessage);

    function hasProcessingStatement() {
        return Boolean(root.querySelector("[data-statement-status='processing']"));
    }

    function schedulePoll(delay = 1800) {
        window.clearTimeout(pollTimer);
        pollTimer = null;
        if (!hasProcessingStatement()) return;
        pollTimer = window.setTimeout(() => {
            if (document.hidden) return schedulePoll(4000);
            loadResults(new URL(window.location.href), {poll: true});
        }, delay);
    }

    function updateStats(stats) {
        Object.entries(stats || {}).forEach(([key, value]) => {
            const element = document.querySelector(`[data-statement-stat="${key}"]`);
            if (!element) return;
            const count = Number(value);
            if (!Number.isFinite(count) || count < 0) return;
            element.textContent = String(count);
            element.classList.toggle("danger", key === "unresolved_count" && count > 0);
            element.classList.toggle("warning", (key === "review_count" || key === "unused_receipt_count") && count > 0);
        });
    }

    async function loadResults(pageUrl, {push = false, poll = false} = {}) {
        window.clearTimeout(pollTimer);
        if (activeController) activeController.abort();
        const serial = ++requestSerial;
        activeController = new AbortController();
        const statusUrl = new URL(root.dataset.statusUrl, window.location.href);
        // Never use the initial filter after navigation: rapid clicks and polling
        // must target the most recent month/filter pair.
        statusUrl.searchParams.set("month", pageUrl.searchParams.get("month") || statusUrl.searchParams.get("month") || "");
        const rawFilter = pageUrl.searchParams.get("result") || "all";
        const normalizedFilter = rawFilter === "inferred" ? "unmatched" :
            (["all", "unmatched", "needs_review"].includes(rawFilter) ? rawFilter : "all");
        statusUrl.searchParams.set("result", normalizedFilter);
        root.setAttribute("aria-busy", "true");
        if (!poll) statusMessage.textContent = "結果を切り替えています…";
        try {
            const response = await fetch(statusUrl.toString(), {
                headers: {"X-Requested-With": "XMLHttpRequest"},
                credentials: "same-origin",
                cache: "no-store",
                signal: activeController.signal
            });
            if (!response.ok || response.redirected) throw new Error("status request failed");
            const payload = await response.json();
            if (!payload.ok || typeof payload.html !== "string") throw new Error("invalid status payload");
            if (serial !== requestSerial) return;
            if (payload.month !== statusUrl.searchParams.get("month") ||
                payload.result_filter !== statusUrl.searchParams.get("result")) {
                throw new Error("response does not match requested filter");
            }
            const focused = document.activeElement;
            const restoreFocus = Boolean(focused && focused.matches && focused.matches("[data-statement-filter-link]"));
            root.innerHTML = payload.html;
            root.dataset.statusUrl = statusUrl.toString();
            updateStats(payload.stats);
            if (push) window.history.pushState({}, "", pageUrl.pathname + pageUrl.search + pageUrl.hash);
            if (restoreFocus) {
                const selected = root.querySelector("[data-statement-filter-link][aria-current='page']") ||
                    root.querySelector("[data-statement-filter-link]");
                if (selected) selected.focus({preventScroll: true});
            }
            statusMessage.textContent = poll ? "" : "表示を更新しました。";
            if (!payload.done) schedulePoll();
        } catch (error) {
            if (serial !== requestSerial || error.name === "AbortError") return;
            if (poll) {
                statusMessage.textContent = "更新状況を取得できませんでした。再試行します。";
                schedulePoll(4000);
            } else {
                // Normal navigation keeps server-side accessibility and expired
                // login handling. Never replace a failed request with stale HTML.
                window.location.assign(pageUrl.toString());
            }
        } finally {
            if (serial === requestSerial) {
                root.setAttribute("aria-busy", "false");
                activeController = null;
            }
        }
    }

    root.addEventListener("click", (event) => {
        const link = event.target.closest && event.target.closest("a[data-statement-filter-link]");
        if (!link || event.defaultPrevented || event.button !== 0 ||
            event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
        const target = new URL(link.href, window.location.href);
        if (target.origin !== window.location.origin || target.pathname !== window.location.pathname) return;
        event.preventDefault();
        loadResults(target, {push: true});
    });
    window.addEventListener("popstate", () => loadResults(new URL(window.location.href)));
    window.addEventListener("pagehide", () => {
        window.clearTimeout(pollTimer);
        if (activeController) activeController.abort();
    });
    schedulePoll(1200);
})();
