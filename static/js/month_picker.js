(function () {
    "use strict";

    const pickerSelector = "[data-month-picker]";

    function parseMonth(value) {
        const match = /^(\d{4})-(\d{2})/.exec(String(value || ""));
        if (!match) return null;
        const year = Number(match[1]);
        const month = Number(match[2]);
        if (!Number.isInteger(year) || month < 1 || month > 12) return null;
        return { year: year, month: month };
    }

    function monthValue(year, month) {
        return String(year).padStart(4, "0") + "-" + String(month).padStart(2, "0");
    }

    function monthLabel(year, month) {
        return year + "年" + String(month).padStart(2, "0") + "月";
    }

    function closePicker(root, options) {
        const popover = root.querySelector("[data-month-picker-popover]");
        const trigger = root.querySelector("[data-month-picker-trigger]");
        if (!popover || popover.hidden) return;
        popover.hidden = true;
        root.classList.remove("is-open");
        if (trigger) {
            trigger.setAttribute("aria-expanded", "false");
            if (options && options.restoreFocus) trigger.focus();
        }
    }

    function closeOtherPickers(current) {
        document.querySelectorAll(pickerSelector + ".is-open").forEach(function (root) {
            if (root !== current) closePicker(root);
        });
    }

    function submitPickerForm(root, input, trigger) {
        const form = root.closest("form");
        if (!form || form.dataset.submitting === "true") return;
        form.dataset.submitting = "true";
        form.setAttribute("aria-busy", "true");
        if (trigger) trigger.disabled = true;
        if (typeof form.requestSubmit === "function") {
            form.requestSubmit();
        } else {
            form.submit();
        }
    }

    function initPicker(root) {
        if (root.dataset.monthPickerReady === "true") return;
        root.dataset.monthPickerReady = "true";

        const input = root.querySelector("input[type='month']");
        const trigger = root.querySelector("[data-month-picker-trigger]");
        const display = root.querySelector("[data-month-picker-display]");
        const popover = root.querySelector("[data-month-picker-popover]");
        const yearLabel = root.querySelector("[data-month-picker-year]");
        const previousYearButton = root.querySelector("[data-month-picker-prev-year]");
        const nextYearButton = root.querySelector("[data-month-picker-next-year]");
        const monthButtons = Array.from(root.querySelectorAll("[data-month-picker-month]"));

        if (!input || !trigger || !display || !popover || !yearLabel || !monthButtons.length) return;
        root.classList.add("is-enhanced");

        let selected = parseMonth(input.value) || parseMonth(root.dataset.selectedMonth);
        if (!selected) {
            const now = new Date();
            selected = { year: now.getFullYear(), month: now.getMonth() + 1 };
            input.value = monthValue(selected.year, selected.month);
        }
        let visibleYear = selected.year;

        function render() {
            yearLabel.textContent = String(visibleYear);
            display.textContent = monthLabel(selected.year, selected.month);
            const selectedIsVisible = visibleYear === selected.year;
            monthButtons.forEach(function (button) {
                const month = Number(button.dataset.monthPickerMonth);
                const isSelected = selectedIsVisible && month === selected.month;
                button.classList.toggle("is-selected", isSelected);
                button.setAttribute("aria-selected", isSelected ? "true" : "false");
                button.setAttribute("tabindex", isSelected || (!selectedIsVisible && month === 1) ? "0" : "-1");
            });
        }

        function openPicker() {
            closeOtherPickers(root);
            visibleYear = selected.year;
            render();
            popover.hidden = false;
            root.classList.add("is-open");
            trigger.setAttribute("aria-expanded", "true");
            window.requestAnimationFrame(function () {
                const selectedButton = popover.querySelector(".is-selected");
                if (selectedButton) selectedButton.focus();
            });
        }

        trigger.addEventListener("click", function () {
            if (popover.hidden) {
                openPicker();
            } else {
                closePicker(root, { restoreFocus: true });
            }
        });

        if (previousYearButton) {
            previousYearButton.addEventListener("click", function () {
                visibleYear -= 1;
                render();
            });
        }
        if (nextYearButton) {
            nextYearButton.addEventListener("click", function () {
                visibleYear += 1;
                render();
            });
        }

        monthButtons.forEach(function (button) {
            button.addEventListener("click", function () {
                const month = Number(button.dataset.monthPickerMonth);
                const nextValue = monthValue(visibleYear, month);
                const changed = input.value !== nextValue;
                selected = { year: visibleYear, month: month };
                input.value = nextValue;
                root.dataset.selectedMonth = nextValue;
                render();
                closePicker(root);
                if (changed) submitPickerForm(root, input, trigger);
                else trigger.focus();
            });

            button.addEventListener("keydown", function (event) {
                const currentMonth = Number(button.dataset.monthPickerMonth);
                let nextMonth = currentMonth;
                let nextYear = visibleYear;
                if (event.key === "ArrowRight") nextMonth += 1;
                else if (event.key === "ArrowLeft") nextMonth -= 1;
                else if (event.key === "ArrowDown") nextMonth += 4;
                else if (event.key === "ArrowUp") nextMonth -= 4;
                else return;

                event.preventDefault();
                while (nextMonth < 1) {
                    nextMonth += 12;
                    nextYear -= 1;
                }
                while (nextMonth > 12) {
                    nextMonth -= 12;
                    nextYear += 1;
                }
                visibleYear = nextYear;
                render();
                const nextButton = root.querySelector('[data-month-picker-month="' + nextMonth + '"]');
                if (nextButton) nextButton.focus();
            });
        });

        root.addEventListener("keydown", function (event) {
            if (event.key === "Escape" && !popover.hidden) {
                event.preventDefault();
                closePicker(root, { restoreFocus: true });
            }
        });

        render();
    }

    function init() {
        document.querySelectorAll(pickerSelector).forEach(initPicker);
        document.addEventListener("click", function (event) {
            document.querySelectorAll(pickerSelector + ".is-open").forEach(function (root) {
                if (!root.contains(event.target)) closePicker(root);
            });
        });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
