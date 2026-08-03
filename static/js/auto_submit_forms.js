(function () {
    "use strict";

    function initForm(form) {
        if (form.dataset.autoSubmitReady === "true") return;
        form.dataset.autoSubmitReady = "true";
        const control = form.querySelector("[data-auto-submit-control]")
            || form.querySelector("input[type='month']")
            || form.querySelector("select");
        if (!control) return;

        control.addEventListener("change", function () {
            if (!control.value || form.dataset.submitting === "true") return;
            form.dataset.submitting = "true";
            form.setAttribute("aria-busy", "true");
            if (typeof form.requestSubmit === "function") {
                form.requestSubmit();
            } else {
                form.submit();
            }
        });
    }

    function init() {
        document.querySelectorAll("[data-auto-submit-on-change]").forEach(initForm);
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
