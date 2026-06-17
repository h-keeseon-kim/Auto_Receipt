(function () {
    "use strict";

    function getCsrfToken(form) {
        const input = form.querySelector("input[name='csrfmiddlewaretoken']");
        if (input && input.value) return input.value;
        const match = document.cookie.match(/(?:^|; )csrftoken=([^;]+)/);
        return match ? decodeURIComponent(match[1]) : "";
    }

    function updateSummary(stats) {
        if (!stats) return;
        Object.keys(stats).forEach(function (key) {
            const element = document.querySelector("[data-ai-summary='" + key + "']");
            if (element) element.textContent = stats[key];
        });
    }

    function replaceReceiptTable(html) {
        const body = document.querySelector("[data-ai-receipt-table-body]");
        if (!body || typeof html !== "string") return;
        body.innerHTML = html;
    }

    function setMessage(form, message, isWorking) {
        const messageElement = form.querySelector("[data-ai-process-message]");
        if (!messageElement || !message) return;
        messageElement.textContent = message;
        messageElement.classList.toggle("processing-text", Boolean(isWorking));
    }

    function setButtonState(form, disabled) {
        const button = form.querySelector("[data-ai-process-button]");
        if (!button) return;
        button.disabled = Boolean(disabled);
        button.textContent = disabled ? "AIで情報を抽出中" : "AIでファイル名を修正・検査";
    }

    function pollStatus(form) {
        const statusUrl = form.dataset.statusUrl;
        if (!statusUrl) return;
        fetch(statusUrl, {
            method: "GET",
            headers: {"X-Requested-With": "XMLHttpRequest"},
            credentials: "same-origin",
        })
            .then(function (response) { return response.json(); })
            .then(function (payload) {
                if (!payload || !payload.ok) throw new Error("Invalid AI processing status response");
                updateSummary(payload.stats);
                replaceReceiptTable(payload.receipts_html);
                if (Number(payload.processing_count || 0) > 0) {
                    setButtonState(form, true);
                    setMessage(form, "AIで情報を抽出中です。完了した領収書から順番に反映されます。", true);
                    window.setTimeout(function () { pollStatus(form); }, 2500);
                } else {
                    setButtonState(form, Number(payload.processable_count || 0) === 0);
                    setMessage(form, "AI処理が完了しました。処理済み項目は次回ボタン押下時にスキップされます。", false);
                }
            })
            .catch(function () {
                setButtonState(form, false);
                setMessage(form, "AI処理状況の取得に失敗しました。ページを再読み込みしてください。", false);
            });
    }

    function init() {
        const form = document.querySelector("[data-ai-process-form]");
        if (!form) return;

        form.addEventListener("submit", function (event) {
            event.preventDefault();
            setButtonState(form, true);
            setMessage(form, "AIで情報を抽出中です。", true);

            fetch(form.action, {
                method: "POST",
                body: new FormData(form),
                headers: {
                    "X-CSRFToken": getCsrfToken(form),
                    "X-Requested-With": "XMLHttpRequest",
                },
                credentials: "same-origin",
            })
                .then(function (response) { return response.json(); })
                .then(function (payload) {
                    if (!payload || !payload.ok) throw new Error("Invalid AI processing response");
                    updateSummary(payload.stats);
                    setMessage(form, payload.message, Number(payload.started_count || 0) > 0);
                    if (Number(payload.started_count || 0) > 0 || (payload.stats && Number(payload.stats.ai_processing_count || 0) > 0)) {
                        pollStatus(form);
                    } else {
                        setButtonState(form, false);
                    }
                })
                .catch(function () {
                    setButtonState(form, false);
                    setMessage(form, "AI処理の開始に失敗しました。時間をおいて再度実行してください。", false);
                });
        });

        const currentProcessing = Number((document.querySelector("[data-ai-summary='ai_processing_count']") || {}).textContent || 0);
        if (currentProcessing > 0) {
            setButtonState(form, true);
            pollStatus(form);
        }
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
