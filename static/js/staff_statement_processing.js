(function () {
    "use strict";

    const root = document.querySelector("[data-statement-results]");
    if (!root) return;
    const statusUrl = root.dataset.statusUrl;
    if (!statusUrl) return;

    function hasProcessingStatement() {
        return Boolean(root.querySelector("[data-statement-status='processing']"));
    }

    async function refresh() {
        if (!hasProcessingStatement()) return;
        try {
            const response = await fetch(statusUrl, {
                headers: {"X-Requested-With": "XMLHttpRequest"},
                credentials: "same-origin"
            });
            if (!response.ok) throw new Error("status request failed");
            const payload = await response.json();
            if (!payload.ok) throw new Error("invalid status payload");
            root.innerHTML = payload.html;
            if (!payload.done) window.setTimeout(refresh, 1800);
        } catch (_error) {
            window.setTimeout(refresh, 4000);
        }
    }

    if (hasProcessingStatement()) window.setTimeout(refresh, 1200);
})();
