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
        input.dispatchEvent(new Event("change", {bubbles: true}));
        return true;
    }

    function renderFiles(zone, input) {
        const output = zone.querySelector("[data-file-dropzone-files]");
        if (!output) return;
        const files = Array.from(input.files || []);
        zone.classList.toggle("has-files", files.length > 0);
        if (!files.length) {
            output.textContent = output.dataset.emptyText || "ファイル未選択";
            return;
        }
        if (files.length === 1) {
            output.textContent = files[0].name;
            return;
        }
        output.textContent = files.length + "件: " + files.map(function (file) { return file.name; }).join(" / ");
    }

    function initZone(zone) {
        const input = zone.querySelector("[data-file-dropzone-input]") || zone.querySelector("input[type='file']");
        const trigger = zone.querySelector("[data-file-dropzone-trigger]") || zone;
        if (!input || zone.dataset.dropzoneReady === "true") return;
        zone.dataset.dropzoneReady = "true";
        const extensions = acceptedExtensions(input);
        const maxFiles = Number(zone.dataset.maxFiles || (input.multiple ? 0 : 1));

        function openPicker(event) {
            if (event) event.preventDefault();
            if (!input.disabled) input.click();
        }

        trigger.addEventListener("click", openPicker);
        trigger.addEventListener("keydown", function (event) {
            if (event.key === "Enter" || event.key === " ") openPicker(event);
        });

        ["dragenter", "dragover"].forEach(function (eventName) {
            zone.addEventListener(eventName, function (event) {
                event.preventDefault();
                event.stopPropagation();
                if (!input.disabled) zone.classList.add("is-dragover");
            });
        });
        ["dragleave", "dragend"].forEach(function (eventName) {
            zone.addEventListener(eventName, function (event) {
                event.preventDefault();
                event.stopPropagation();
                zone.classList.remove("is-dragover");
            });
        });
        zone.addEventListener("drop", function (event) {
            event.preventDefault();
            event.stopPropagation();
            zone.classList.remove("is-dragover");
            if (input.disabled) return;
            let files = Array.from((event.dataTransfer && event.dataTransfer.files) || []);
            const rejected = files.filter(function (file) { return !fileIsAccepted(file, extensions); });
            files = files.filter(function (file) { return fileIsAccepted(file, extensions); });
            if (maxFiles > 0) files = files.slice(0, maxFiles);
            const output = zone.querySelector("[data-file-dropzone-files]");
            if (rejected.length && output) {
                output.textContent = "対応していない形式を除外しました: " + rejected.map(function (file) { return file.name; }).join(" / ");
            }
            if (files.length && !setFiles(input, files) && output) {
                output.textContent = "このブラウザではドロップを反映できません。クリックしてファイルを選択してください。";
            }
        });
        input.addEventListener("change", function () { renderFiles(zone, input); });
        renderFiles(zone, input);
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
