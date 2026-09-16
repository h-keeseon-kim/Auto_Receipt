(function () {
    "use strict";

    function csrfToken(form) {
        const input = form.querySelector("input[name='csrfmiddlewaretoken']");
        return input ? input.value : "";
    }

    function init() {
        const panel = document.querySelector("[data-receipt-review-panel]");
        if (!panel) return;
        let polling = false;

        function contentIsProcessing() {
            const content = panel.querySelector("[data-receipt-review-content]");
            return Boolean(content && content.dataset.processing === "true");
        }

        function schedulePoll() {
            if (polling) return;
            polling = true;
            window.setTimeout(poll, 1800);
        }

        function poll() {
            fetch(panel.dataset.statusUrl, {
                headers: {"X-Requested-With": "XMLHttpRequest"},
                credentials: "same-origin",
            })
                .then(function (response) { return response.json(); })
                .then(function (payload) {
                    if (!payload || !payload.ok) throw new Error("invalid response");
                    if (payload.deleted && payload.redirect_url) {
                        window.location.assign(payload.redirect_url);
                        return;
                    }
                    panel.innerHTML = payload.html;
                    polling = false;
                    if (payload.processing) schedulePoll();
                })
                .catch(function () {
                    polling = false;
                    const message = panel.querySelector("[data-single-receipt-ai-message]");
                    if (message) message.textContent = "AI処理状況を取得できませんでした。ページを再読み込みしてください。";
                });
        }

        panel.addEventListener("submit", function (event) {
            const form = event.target.closest("[data-single-receipt-ai-form]");
            if (!form) return;
            event.preventDefault();
            const button = form.querySelector("button[type='submit']");
            const message = form.querySelector("[data-single-receipt-ai-message]");
            if (button) {
                button.disabled = true;
                button.textContent = "AIで情報を抽出中";
            }
            if (message) message.textContent = "AIで情報を抽出中です。";
            fetch(form.action, {
                method: "POST",
                body: new FormData(form),
                headers: {
                    "X-CSRFToken": csrfToken(form),
                    "X-Requested-With": "XMLHttpRequest",
                },
                credentials: "same-origin",
            })
                .then(function (response) { return response.json(); })
                .then(function (payload) {
                    if (!payload || !payload.ok) throw new Error("invalid response");
                    if (message) message.textContent = payload.message;
                    schedulePoll();
                })
                .catch(function () {
                    if (button) {
                        button.disabled = false;
                        button.textContent = "AIでファイル名を修正・検査";
                    }
                    if (message) message.textContent = "AI処理を開始できませんでした。";
                });
        });

        if (contentIsProcessing()) schedulePoll();
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
