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

    function fileIdentity(file) {
        return [file.name || "", file.size || 0, file.lastModified || 0, file.type || ""].join("\u0000");
    }

    function mergeUniqueFiles(existing, incoming) {
        const merged = [];
        const seen = new Set();
        existing.concat(incoming).forEach(function (file) {
            const key = fileIdentity(file);
            if (seen.has(key)) return;
            seen.add(key);
            merged.push(file);
        });
        return merged;
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
        const duplicateCount = Number((meta && meta.duplicateCount) || 0);
        const appendedCount = Number((meta && meta.appendedCount) || 0);
        const previousCount = Number((meta && meta.previousCount) || 0);

        zone.classList.toggle("has-files", files.length > 0);
        zone.classList.remove("has-error", "is-uploading");
        clearNode(output);
        if (!files.length) {
            output.textContent = output.dataset.emptyText || "ファイル未選択";
            return;
        }

        const summary = makeElement("div", "file-dropzone-selection-summary");
        summary.appendChild(makeElement("span", "file-dropzone-selection-icon", "✓"));
        let summaryText;
        if (previousCount > 0 && appendedCount > 0) {
            summaryText = appendedCount + "件を追加選択しました（合計" + files.length + "件）";
        } else {
            summaryText = source === "drop"
                ? files.length + "件のファイルをドロップで受け付けました"
                : files.length + "件のファイルを選択しました";
        }
        summary.appendChild(makeElement("strong", "", summaryText));
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
            omittedCount ? "この欄で受け付けられる件数を超えたため、超過分を除外しました。" : ""
        );
        appendNotice(
            output,
            "file-dropzone-warning",
            duplicateCount ? "すでに選択済みの同じファイル " + duplicateCount + "件は重複追加しませんでした。" : ""
        );

        if (input.multiple) {
            const clearButton = makeElement("button", "file-dropzone-clear", "選択をクリア");
            clearButton.type = "button";
            clearButton.dataset.fileDropzoneClear = "";
            output.appendChild(clearButton);
        }
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
        let selectedFiles = Array.from(input.files || []);
        let preparedSelectionMeta = null;
        ensureDragPrompt(zone);
        outputNode(zone);

        function resetDragState() {
            dragDepth = 0;
            zone.classList.remove("is-dragover");
        }

        function clearSelection() {
            input.value = "";
            selectedFiles = [];
            preparedSelectionMeta = null;
            zone.dataset.selectionSource = "picker";
            renderEmpty(zone);
        }

        function limitFiles(files) {
            if (maxFiles <= 0 || files.length <= maxFiles) {
                return {files: files, omittedCount: 0};
            }
            return {files: files.slice(0, maxFiles), omittedCount: files.length - maxFiles};
        }

        function openPicker(event) {
            if (event && event.target.closest && event.target.closest("[data-file-dropzone-clear]")) return;
            if (event) event.preventDefault();
            if (!input.disabled && !zone.classList.contains("is-uploading")) {
                zone.dataset.selectionSource = "picker";
                input.click();
            }
        }

        trigger.addEventListener("click", openPicker);
        trigger.addEventListener("keydown", function (event) {
            if (event.key === "Enter" || event.key === " ") openPicker(event);
        });

        zone.addEventListener("click", function (event) {
            const clearButton = event.target.closest && event.target.closest("[data-file-dropzone-clear]");
            if (!clearButton || !zone.contains(clearButton)) return;
            event.preventDefault();
            event.stopPropagation();
            clearSelection();
        });

        zone.addEventListener("dragenter", function (event) {
            event.preventDefault();
            event.stopPropagation();
            if (input.disabled || zone.classList.contains("is-uploading")) return;
            dragDepth += 1;
            zone.classList.add("is-dragover");
        });
        zone.addEventListener("dragover", function (event) {
            event.preventDefault();
            event.stopPropagation();
            if (event.dataTransfer) event.dataTransfer.dropEffect = "copy";
            if (!input.disabled && !zone.classList.contains("is-uploading")) zone.classList.add("is-dragover");
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
            if (input.disabled || zone.classList.contains("is-uploading")) return;

            const candidates = Array.from((event.dataTransfer && event.dataTransfer.files) || []);
            const accepted = candidates.filter(function (file) { return fileIsAccepted(file, extensions); });
            const rejected = candidates.filter(function (file) { return !fileIsAccepted(file, extensions); });
            if (!accepted.length) {
                const rejectedNames = rejected.map(function (file) { return file.name; }).join(" / ");
                renderProblem(
                    zone,
                    rejectedNames
                        ? "対応していないファイル形式です: " + rejectedNames
                        : "ドロップされたファイルを読み取れませんでした。"
                );
                return;
            }

            const previousCount = selectedFiles.length;
            const merged = input.multiple ? mergeUniqueFiles(selectedFiles, accepted) : accepted.slice(0, 1);
            const limited = limitFiles(merged);
            const duplicateCount = input.multiple
                ? Math.max(accepted.length - (limited.files.length - previousCount), 0)
                : 0;
            if (!setFiles(input, limited.files)) {
                renderProblem(zone, "このブラウザではドロップを反映できません。クリックしてファイルを選択してください。");
                return;
            }
            selectedFiles = Array.from(input.files || []);
            zone.dataset.selectionSource = "drop";
            preparedSelectionMeta = {
                source: "drop",
                rejected: rejected,
                omittedCount: limited.omittedCount,
                duplicateCount: duplicateCount,
                previousCount: previousCount,
                appendedCount: Math.max(selectedFiles.length - previousCount, 0),
            };
            input.dispatchEvent(new Event("change", {bubbles: true}));
        });

        input.addEventListener("change", function () {
            let meta = preparedSelectionMeta;
            preparedSelectionMeta = null;

            if (!meta) {
                const source = zone.dataset.selectionSource || "picker";
                const incoming = Array.from(input.files || []);
                const previousCount = selectedFiles.length;
                const merged = input.multiple ? mergeUniqueFiles(selectedFiles, incoming) : incoming.slice(0, 1);
                const limited = limitFiles(merged);
                const duplicateCount = input.multiple
                    ? Math.max(incoming.length - (limited.files.length - previousCount), 0)
                    : 0;
                if (!setFiles(input, limited.files)) {
                    selectedFiles = incoming;
                } else {
                    selectedFiles = Array.from(input.files || []);
                }
                meta = {
                    source: source,
                    rejected: [],
                    omittedCount: limited.omittedCount,
                    duplicateCount: duplicateCount,
                    previousCount: previousCount,
                    appendedCount: Math.max(selectedFiles.length - previousCount, 0),
                };
            } else {
                selectedFiles = Array.from(input.files || []);
            }

            renderFiles(zone, input, meta);
            zone.dataset.selectionSource = "picker";
        }, true);

        zone.addEventListener("filedropzone:reset", clearSelection);
        zone.addEventListener("filedropzone:uploading", function (event) {
            const detail = event.detail || {};
            renderUploading(zone, detail.message);
        });

        const form = zone.closest("form");
        if (form) {
            form.addEventListener("reset", clearSelection);
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
