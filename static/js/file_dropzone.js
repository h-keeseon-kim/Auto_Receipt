(function () {
    "use strict";

    function acceptedExtensions(input) {
        return (input.accept || "")
            .split(",")
            .map(function (value) { return value.trim().toLowerCase(); })
            .filter(function (value) { return value.charAt(0) === "."; });
    }

    function fileIsAccepted(file, extensions) {
        if (!extensions.length) return true;
        const name = String(file.name || "").toLowerCase();
        return extensions.some(function (extension) { return name.endsWith(extension); });
    }

    function setFiles(input, files) {
        if (typeof DataTransfer === "undefined") return false;
        const transfer = new DataTransfer();
        files.forEach(function (file) { transfer.items.add(file); });
        input.files = transfer.files;
        return true;
    }

    function clearNode(node) {
        while (node.firstChild) node.removeChild(node.firstChild);
    }

    function makeElement(tagName, className, text) {
        const element = document.createElement(tagName);
        if (className) element.className = className;
        if (text !== undefined) element.textContent = text;
        return element;
    }

    function ensureDragPrompt(zone) {
        let prompt = zone.querySelector("[data-file-dropzone-drag-prompt]");
        if (prompt) return prompt;
        prompt = makeElement("div", "file-dropzone-drag-prompt");
        prompt.dataset.fileDropzoneDragPrompt = "";
        prompt.setAttribute("aria-hidden", "true");
        prompt.appendChild(makeElement("span", "file-dropzone-drag-icon", "↓"));
        prompt.appendChild(makeElement("strong", "", "ここでファイルを離してください"));
        prompt.appendChild(makeElement("span", "", "ドロップしたファイルをアップロード対象として受け付けます"));
        zone.appendChild(prompt);
        return prompt;
    }

    function outputNode(zone) {
        const output = zone.querySelector("[data-file-dropzone-files]");
        if (output) {
            output.setAttribute("role", "status");
            output.setAttribute("aria-live", "polite");
            output.setAttribute("aria-atomic", "true");
        }
        return output;
    }

    function renderEmpty(zone) {
        const output = outputNode(zone);
        zone.classList.remove("has-files", "has-error", "is-uploading");
        if (!output) return;
        clearNode(output);
        output.textContent = output.dataset.emptyText || "ファイル未選択";
    }

    function appendNotice(output, className, text) {
        if (!text) return;
        output.appendChild(makeElement("div", className, text));
    }

    function renderFiles(zone, input, meta) {
        const output = outputNode(zone);
        if (!output) return;
        const files = Array.from(input.files || []);
        const source = (meta && meta.source) || "picker";
        const rejected = (meta && meta.rejected) || [];
        const omittedCount = Number((meta && meta.omittedCount) || 0);

        zone.classList.toggle("has-files", files.length > 0);
        zone.classList.remove("has-error", "is-uploading");
        clearNode(output);
        if (!files.length) {
            output.textContent = output.dataset.emptyText || "ファイル未選択";
            return;
        }

        const summary = makeElement("div", "file-dropzone-selection-summary");
        summary.appendChild(makeElement("span", "file-dropzone-selection-icon", "✓"));
        summary.appendChild(
            makeElement(
                "strong",
                "",
                source === "drop"
                    ? files.length + "件のファイルをドロップで受け付けました"
                    : files.length + "件のファイルを選択しました"
            )
        );
        output.appendChild(summary);

        const list = makeElement("ul", "file-dropzone-file-list");
        files.slice(0, 4).forEach(function (file) {
            list.appendChild(makeElement("li", "", file.name));
        });
        if (files.length > 4) {
            list.appendChild(makeElement("li", "", "ほか " + (files.length - 4) + "件"));
        }
        output.appendChild(list);

        appendNotice(
            output,
            "file-dropzone-warning",
            rejected.length
                ? "対応していない形式のため除外: " + rejected.map(function (file) { return file.name; }).join(" / ")
                : ""
        );
        appendNotice(
            output,
            "file-dropzone-warning",
            omittedCount ? "この欄は1ファイルのみのため、先頭のファイルだけを受け付けました。" : ""
        );
    }

    function renderProblem(zone, message) {
        const output = outputNode(zone);
        zone.classList.remove("has-files", "is-uploading");
        zone.classList.add("has-error");
        if (!output) return;
        clearNode(output);
        const summary = makeElement("div", "file-dropzone-selection-summary error");
        summary.appendChild(makeElement("span", "file-dropzone-selection-icon", "!"));
        summary.appendChild(makeElement("strong", "", message));
        output.appendChild(summary);
    }

    function renderUploading(zone, message) {
        const output = outputNode(zone);
        zone.classList.remove("has-error");
        zone.classList.add("is-uploading");
        if (!output) return;
        clearNode(output);
        const summary = makeElement("div", "file-dropzone-selection-summary uploading");
        summary.appendChild(makeElement("span", "file-dropzone-spinner"));
        summary.appendChild(makeElement("strong", "", message || "ファイルをアップロード中です…"));
        output.appendChild(summary);
        output.appendChild(makeElement("div", "file-dropzone-upload-note", "画面が切り替わるまでそのままお待ちください。"));
    }

    function initZone(zone) {
        const input = zone.querySelector("[data-file-dropzone-input]") || zone.querySelector("input[type='file']");
        const trigger = zone.querySelector("[data-file-dropzone-trigger]") || zone;
        if (!input || zone.dataset.dropzoneReady === "true") return;
        zone.dataset.dropzoneReady = "true";
        const extensions = acceptedExtensions(input);
        const maxFiles = Number(zone.dataset.maxFiles || (input.multiple ? 0 : 1));
        let dragDepth = 0;
        let pendingRejected = [];
        let pendingOmittedCount = 0;
        ensureDragPrompt(zone);
        outputNode(zone);

        function resetDragState() {
            dragDepth = 0;
            zone.classList.remove("is-dragover");
        }

        function openPicker(event) {
            if (event) event.preventDefault();
            if (!input.disabled) {
                zone.dataset.selectionSource = "picker";
                input.click();
            }
        }

        trigger.addEventListener("click", openPicker);
        trigger.addEventListener("keydown", function (event) {
            if (event.key === "Enter" || event.key === " ") openPicker(event);
        });

        zone.addEventListener("dragenter", function (event) {
            event.preventDefault();
            event.stopPropagation();
            if (input.disabled) return;
            dragDepth += 1;
            zone.classList.add("is-dragover");
        });
        zone.addEventListener("dragover", function (event) {
            event.preventDefault();
            event.stopPropagation();
            if (event.dataTransfer) event.dataTransfer.dropEffect = "copy";
            if (!input.disabled) zone.classList.add("is-dragover");
        });
        zone.addEventListener("dragleave", function (event) {
            event.preventDefault();
            event.stopPropagation();
            dragDepth = Math.max(dragDepth - 1, 0);
            if (!dragDepth) zone.classList.remove("is-dragover");
        });
        zone.addEventListener("dragend", resetDragState);
        zone.addEventListener("drop", function (event) {
            event.preventDefault();
            event.stopPropagation();
            resetDragState();
            if (input.disabled) return;

            const candidates = Array.from((event.dataTransfer && event.dataTransfer.files) || []);
            let files = candidates.filter(function (file) { return fileIsAccepted(file, extensions); });
            pendingRejected = candidates.filter(function (file) { return !fileIsAccepted(file, extensions); });
            pendingOmittedCount = 0;
            if (maxFiles > 0 && files.length > maxFiles) {
                pendingOmittedCount = files.length - maxFiles;
                files = files.slice(0, maxFiles);
            }
            if (!files.length) {
                const rejectedNames = pendingRejected.map(function (file) { return file.name; }).join(" / ");
                pendingRejected = [];
                renderProblem(
                    zone,
                    rejectedNames
                        ? "対応していないファイル形式です: " + rejectedNames
                        : "ドロップされたファイルを読み取れませんでした。"
                );
                return;
            }
            zone.dataset.selectionSource = "drop";
            if (!setFiles(input, files)) {
                pendingRejected = [];
                pendingOmittedCount = 0;
                renderProblem(zone, "このブラウザではドロップを反映できません。クリックしてファイルを選択してください。");
                return;
            }
            input.dispatchEvent(new Event("change", {bubbles: true}));
        });

        input.addEventListener("change", function () {
            renderFiles(zone, input, {
                source: zone.dataset.selectionSource || "picker",
                rejected: pendingRejected,
                omittedCount: pendingOmittedCount,
            });
            pendingRejected = [];
            pendingOmittedCount = 0;
            zone.dataset.selectionSource = "picker";
        }, true);

        zone.addEventListener("filedropzone:uploading", function (event) {
            const detail = event.detail || {};
            renderUploading(zone, detail.message);
        });

        const form = zone.closest("form");
        if (form) {
            form.addEventListener("submit", function () {
                if ((input.files || []).length) renderUploading(zone);
            });
        }
        renderEmpty(zone);
    }

    function init() {
        document.querySelectorAll("[data-file-dropzone]").forEach(initZone);
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
