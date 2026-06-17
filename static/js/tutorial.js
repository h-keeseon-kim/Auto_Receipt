(function () {
    "use strict";

    const USER_STEPS = [
        {
            selector: "[data-tutorial-target='user-services-nav']",
            title: "まずは利用サービスを確認します",
            body: "ReceiptHubでは、最初の画面を利用サービスにしています。自分が使っているサービスを確認し、新しく使い始めたサービスもここから登録します。",
            hint: "管理者が登録したサービスマスターの中から選ぶため、サービス名の表記ゆれを防げます。",
        },
        {
            selector: "[data-tutorial-target='service-registration-button']",
            title: "新しい利用サービスを登録します",
            body: "新しく使い始めたサービスがある場合は「サービス利用登録」を押します。管理者側にもユーザーによる新規登録として記録されます。",
            hint: "同じサービスでも ChatGPT（サブスク）とChatGPT（従量課金 / API）のように種類別で管理します。",
        },
        {
            selector: "[data-tutorial-target='active-services-section']",
            title: "利用中サービスを確認します",
            body: "領収書アップロード時に選べるサービスは、ここに表示される利用中サービスです。使わなくなったサービスは利用停止できます。",
            hint: "利用停止時には、最後にアップロードすべき領収書月を選択します。",
        },
        {
            selector: "[data-tutorial-target='upload-nav']",
            title: "領収書をアップロードします",
            body: "アップロード画面では、提出月を選び、登録サービスを選択してから領収書ファイルを選びます。ファイル選択後は自動でアップロードされます。",
            hint: "アップロード後のAI確認は裏側で実行されます。ユーザー側には管理者用メモは表示されません。",
        },
        {
            selector: "[data-tutorial-target='history-nav']",
            title: "提出履歴を確認します",
            body: "提出履歴では、月ごとの提出状態と詳細を確認できます。提出後にファイルが間違っていた場合は、対象領収書のファイル修正もできます。",
            hint: "管理者から再提出依頼がある場合は、対象月のアップロード画面に表示されます。",
        },
        {
            selector: "[data-tutorial-target='tutorial-help-button']",
            title: "チュートリアルはいつでも再表示できます",
            body: "一度完了した後も、右上の「？」を押すとこのチュートリアルを再度確認できます。",
            hint: "これで基本操作の説明は完了です。",
        },
    ];

    const STAFF_STEPS = [
        {
            selector: "[data-tutorial-target='staff-history-nav']",
            title: "提出履歴で全体を確認します",
            body: "管理者は提出履歴を起点に、対象月の提出状況とアップロード済み領収書を確認します。提出状況はユーザー名順、領収書はアップロード日の新しい順で表示されます。",
            hint: "AIが確認できなかった項目はハイライトされ、人が確認しやすくなります。",
        },
        {
            selector: "[data-tutorial-target='staff-status-table']",
            title: "ユーザー別の提出状況を見ます",
            body: "この表では、ユーザーごとの提出ステータス、領収書数、保存中ファイル数、再提出待ちなどを確認できます。",
            hint: "件数が増えても縦横スクロールで確認できます。",
        },
        {
            selector: "[data-tutorial-target='staff-receipt-table']",
            title: "アップロード済み領収書を確認します",
            body: "領収書ごとに、AI確認チェック、管理者用メモ、ダウンロード、削除、再提出指示を確認できます。",
            hint: "問題が確定した領収書は再提出指示を出すと、ユーザー側からも該当項目が削除されます。",
        },
        {
            selector: "[data-tutorial-target='staff-services-nav']",
            title: "利用サービス管理を行います",
            body: "サービスマスターの登録、ユーザー別の登録状況、登録サービス一覧、新規登録/停止の確認を行います。",
            hint: "サービスマスターは件数が増えてもページ式で確認できます。",
        },
        {
            selector: "[data-tutorial-target='staff-user-create-nav']",
            title: "ユーザーを発行します",
            body: "新しい一般ユーザーは管理者がメールアドレス形式で発行します。初期パスワードはランダム生成され、初回ログイン時に変更が必須になります。",
            hint: "初期パスワードは作成直後の画面でのみ表示されます。",
        },
        {
            selector: "[data-tutorial-target='tutorial-help-button']",
            title: "チュートリアルはいつでも再表示できます",
            body: "右上の「？」を押すと、管理者向けチュートリアルを再度確認できます。",
            hint: "これで管理者向けの基本操作説明は完了です。",
        },
    ];

    function getCsrfToken(root) {
        const input = root.querySelector("input[name='csrfmiddlewaretoken']");
        if (input && input.value) return input.value;
        const match = document.cookie.match(/(?:^|; )csrftoken=([^;]+)/);
        return match ? decodeURIComponent(match[1]) : "";
    }

    function markCompleted(root) {
        const url = root.dataset.completeUrl;
        if (!url) return;
        fetch(url, {
            method: "POST",
            headers: {
                "X-CSRFToken": getCsrfToken(root),
                "X-Requested-With": "XMLHttpRequest",
            },
            credentials: "same-origin",
        }).catch(function () {
            // チュートリアル完了の保存に失敗しても、画面操作は妨げない。
        });
    }

    function clamp(value, min, max) {
        return Math.min(Math.max(value, min), max);
    }

    function initTutorial() {
        const root = document.querySelector("[data-tutorial-root]");
        if (!root) return;

        const role = document.body.dataset.tutorialRole === "staff" ? "staff" : "user";
        const steps = role === "staff" ? STAFF_STEPS : USER_STEPS;
        const card = root.querySelector(".tutorial-card");
        const title = root.querySelector("[data-tutorial-title]");
        const body = root.querySelector("[data-tutorial-body]");
        const hint = root.querySelector("[data-tutorial-hint]");
        const count = root.querySelector("[data-tutorial-step-count]");
        const prevButton = root.querySelector("[data-tutorial-prev]");
        const nextButton = root.querySelector("[data-tutorial-next]");
        const skipButton = root.querySelector("[data-tutorial-skip]");
        const openButtons = document.querySelectorAll("[data-tutorial-open]");
        const closeButtons = root.querySelectorAll("[data-tutorial-close]");
        let currentIndex = 0;
        let highlighted = null;

        function clearHighlight() {
            if (highlighted) {
                highlighted.classList.remove("tutorial-highlight");
                highlighted = null;
            }
        }

        function positionCard(target) {
            if (!card) return;
            card.style.left = "";
            card.style.top = "";
            card.style.right = "";
            card.style.bottom = "";
            card.style.transform = "";

            const cardRect = card.getBoundingClientRect();
            const margin = 16;
            if (!target) {
                card.style.left = "50%";
                card.style.top = "50%";
                card.style.transform = "translate(-50%, -50%)";
                return;
            }

            const targetRect = target.getBoundingClientRect();
            let top = targetRect.bottom + margin;
            if (top + cardRect.height > window.innerHeight - margin) {
                top = targetRect.top - cardRect.height - margin;
            }
            if (top < margin) top = margin;
            const left = clamp(targetRect.left, margin, window.innerWidth - cardRect.width - margin);
            card.style.left = `${left}px`;
            card.style.top = `${top}px`;
        }

        function renderStep() {
            clearHighlight();
            const step = steps[currentIndex];
            if (!step) return;
            const target = step.selector ? document.querySelector(step.selector) : null;
            if (target) {
                target.classList.add("tutorial-highlight");
                highlighted = target;
                target.scrollIntoView({ behavior: "smooth", block: "center", inline: "nearest" });
            }
            title.textContent = step.title;
            body.textContent = step.body;
            hint.textContent = step.hint || (target ? "" : "対象の機能がこのページにない場合は、上部メニューから該当ページへ移動してください。");
            count.textContent = `${currentIndex + 1} / ${steps.length}`;
            prevButton.disabled = currentIndex === 0;
            nextButton.textContent = currentIndex === steps.length - 1 ? "完了する" : "次へ";
            window.setTimeout(function () { positionCard(target); }, 80);
        }

        function openTutorial(startAt) {
            currentIndex = typeof startAt === "number" ? startAt : 0;
            root.hidden = false;
            root.classList.add("active");
            document.body.classList.add("tutorial-open");
            renderStep();
            if (card) card.focus({ preventScroll: true });
        }

        function closeTutorial(options) {
            const save = !options || options.save !== false;
            clearHighlight();
            root.classList.remove("active");
            root.hidden = true;
            document.body.classList.remove("tutorial-open");
            if (save) markCompleted(root);
        }

        nextButton.addEventListener("click", function () {
            if (currentIndex >= steps.length - 1) {
                closeTutorial({ save: true });
                return;
            }
            currentIndex += 1;
            renderStep();
        });

        prevButton.addEventListener("click", function () {
            if (currentIndex === 0) return;
            currentIndex -= 1;
            renderStep();
        });

        skipButton.addEventListener("click", function () {
            closeTutorial({ save: true });
        });

        closeButtons.forEach(function (button) {
            button.addEventListener("click", function () {
                closeTutorial({ save: true });
            });
        });

        openButtons.forEach(function (button) {
            button.addEventListener("click", function () {
                openTutorial(0);
            });
        });

        window.addEventListener("resize", function () {
            if (!root.classList.contains("active")) return;
            renderStep();
        });

        document.addEventListener("keydown", function (event) {
            if (!root.classList.contains("active")) return;
            if (event.key === "Escape") closeTutorial({ save: true });
            if (event.key === "ArrowRight") nextButton.click();
            if (event.key === "ArrowLeft") prevButton.click();
        });

        if (root.dataset.autoStart === "true") {
            window.setTimeout(function () { openTutorial(0); }, 350);
        }
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", initTutorial);
    } else {
        initTutorial();
    }
})();
