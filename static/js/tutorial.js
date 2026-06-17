(function () {
    "use strict";

    const USER_STEPS = [
        {
            selector: "[data-tutorial-target='user-services-nav']",
            title: "まずは利用サービスを確認します",
            body: "ReceiptHubでは、最初の画面を利用サービスにしています。自分が使っているサービスを確認し、新しく使い始めたサービスもここから登録します。",
            hint: "管理者が登録したサービスマスターの中から選ぶため、サービス名の表記ゆれを防げます。",
            placement: "below",
        },
        {
            selector: "[data-tutorial-target='service-registration-button']",
            title: "新しい利用サービスを登録します",
            body: "新しく使い始めたサービスがある場合は「サービス利用登録」を押します。管理者側にもユーザーによる新規登録として記録されます。",
            hint: "同じサービスでも ChatGPT（サブスク）とChatGPT（従量課金 / API）のように種類別で管理します。",
            placement: "left",
        },
        {
            selector: "[data-tutorial-target='active-services-section']",
            title: "利用中サービスを確認します",
            body: "領収書アップロード時に選べるサービスは、ここに表示される利用中サービスです。使わなくなったサービスは利用停止できます。",
            hint: "利用停止時には、最後にアップロードすべき領収書月を選択します。",
            placement: "above",
            scrollBlock: "nearest",
        },
        {
            selector: "[data-tutorial-target='upload-nav']",
            title: "領収書をアップロードします",
            body: "アップロード画面では、提出月を選び、登録サービスを選択してから領収書ファイルを選びます。ファイル選択後は自動でアップロードされます。",
            hint: "アップロード後のAI確認は裏側で実行されます。ユーザー側には管理者用メモは表示されません。",
            placement: "below",
        },
        {
            selector: "[data-tutorial-target='history-nav']",
            title: "提出履歴を確認します",
            body: "提出履歴では、月ごとの提出状態と詳細を確認できます。提出後にファイルが間違っていた場合は、対象領収書のファイル修正もできます。",
            hint: "管理者から再提出依頼がある場合は、対象月のアップロード画面に表示されます。",
            placement: "below",
        },
        {
            selector: "[data-tutorial-target='tutorial-help-button']",
            title: "チュートリアルはいつでも再表示できます",
            body: "一度完了した後も、右上の「？」を押すとこのチュートリアルを再度確認できます。",
            hint: "これで基本操作の説明は完了です。",
            placement: "left",
        },
    ];

    const STAFF_STEPS = [
        {
            selector: "[data-tutorial-target='staff-history-nav']",
            title: "提出履歴で全体を確認します",
            body: "管理者は提出履歴を起点に、対象月の提出状況とアップロード済み領収書を確認します。提出状況はユーザー名順、領収書はアップロード日の新しい順で表示されます。",
            hint: "AIが確認できなかった項目はハイライトされ、人が確認しやすくなります。",
            placement: "below",
        },
        {
            selector: "[data-tutorial-target='staff-status-table']",
            title: "ユーザー別の提出状況を見ます",
            body: "この表では、ユーザーごとの提出ステータス、領収書数、保存中ファイル数、再提出待ちなどを確認できます。",
            hint: "件数が増えても縦横スクロールで確認できます。",
            placement: "above",
        },
        {
            selector: "[data-tutorial-target='staff-receipt-table']",
            title: "アップロード済み領収書を確認します",
            body: "領収書ごとに、AI確認チェック、管理者用メモ、ダウンロード、削除、再提出指示を確認できます。",
            hint: "問題が確定した領収書は再提出指示を出すと、ユーザー側からも該当項目が削除されます。",
            placement: "above",
        },
        {
            selector: "[data-tutorial-target='staff-services-nav']",
            title: "利用サービス管理を行います",
            body: "サービスマスターの登録、ユーザー別の登録状況、登録サービス一覧、新規登録/停止の確認を行います。",
            hint: "サービスマスターは件数が増えてもページ式で確認できます。",
            placement: "below",
        },
        {
            selector: "[data-tutorial-target='staff-user-create-nav']",
            title: "ユーザーを発行します",
            body: "新しい一般ユーザーは管理者がメールアドレス形式で発行します。初期パスワードはランダム生成され、初回ログイン時に変更が必須になります。",
            hint: "初期パスワードは作成直後の画面でのみ表示されます。",
            placement: "below",
        },
        {
            selector: "[data-tutorial-target='tutorial-help-button']",
            title: "チュートリアルはいつでも再表示できます",
            body: "右上の「？」を押すと、管理者向けチュートリアルを再度確認できます。",
            hint: "これで管理者向けの基本操作説明は完了です。",
            placement: "left",
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
        if (max < min) return min;
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
        const spotlight = document.createElement("div");
        spotlight.className = "tutorial-spotlight";
        spotlight.setAttribute("aria-hidden", "true");
        spotlight.hidden = true;
        root.insertBefore(spotlight, root.firstChild);

        let currentIndex = 0;
        let currentTarget = null;
        let resizeFrame = null;

        function clearHighlight() {
            if (currentTarget) currentTarget.classList.remove("tutorial-source-active");
            currentTarget = null;
            spotlight.hidden = true;
            root.classList.add("tutorial-no-target");
        }

        function positionSpotlight(target) {
            if (!target) {
                spotlight.hidden = true;
                root.classList.add("tutorial-no-target");
                return;
            }
            const targetRect = target.getBoundingClientRect();
            if (targetRect.width <= 0 || targetRect.height <= 0) {
                spotlight.hidden = true;
                root.classList.add("tutorial-no-target");
                return;
            }
            const margin = 8;
            const padding = target.dataset.tutorialPadding ? Number.parseInt(target.dataset.tutorialPadding, 10) : 10;
            const safePadding = Number.isFinite(padding) ? padding : 10;
            const left = clamp(targetRect.left - safePadding, margin, window.innerWidth - margin);
            const top = clamp(targetRect.top - safePadding, margin, window.innerHeight - margin);
            const right = clamp(targetRect.right + safePadding, margin, window.innerWidth - margin);
            const bottom = clamp(targetRect.bottom + safePadding, margin, window.innerHeight - margin);

            spotlight.style.left = `${left}px`;
            spotlight.style.top = `${top}px`;
            spotlight.style.width = `${Math.max(right - left, 24)}px`;
            spotlight.style.height = `${Math.max(bottom - top, 24)}px`;
            root.classList.remove("tutorial-no-target");
            spotlight.hidden = false;
        }

        function setCardPosition(left, top) {
            card.style.left = `${left}px`;
            card.style.top = `${top}px`;
            card.style.right = "";
            card.style.bottom = "";
            card.style.transform = "";
        }

        function overlapArea(cardCandidate, targetRect) {
            const overlapWidth = Math.max(0, Math.min(cardCandidate.left + cardCandidate.width, targetRect.right) - Math.max(cardCandidate.left, targetRect.left));
            const overlapHeight = Math.max(0, Math.min(cardCandidate.top + cardCandidate.height, targetRect.bottom) - Math.max(cardCandidate.top, targetRect.top));
            return overlapWidth * overlapHeight;
        }

        function positionCard(target, preferredPlacement) {
            if (!card) return;
            card.style.left = "";
            card.style.top = "";
            card.style.right = "";
            card.style.bottom = "";
            card.style.transform = "";

            const margin = 16;
            const viewportWidth = window.innerWidth;
            const viewportHeight = window.innerHeight;
            const cardRect = card.getBoundingClientRect();
            const cardWidth = Math.min(cardRect.width || 430, viewportWidth - margin * 2);
            const cardHeight = Math.min(cardRect.height || 320, viewportHeight - margin * 2);

            if (!target) {
                card.style.left = "50%";
                card.style.top = "50%";
                card.style.transform = "translate(-50%, -50%)";
                return;
            }

            const targetRect = target.getBoundingClientRect();
            const maxLeft = viewportWidth - cardWidth - margin;
            const maxTop = viewportHeight - cardHeight - margin;
            const middleLeft = clamp(targetRect.left + (targetRect.width - cardWidth) / 2, margin, maxLeft);
            const middleTop = clamp(targetRect.top + (targetRect.height - cardHeight) / 2, margin, maxTop);
            const sideTop = clamp(targetRect.top, margin, maxTop);
            const rightLeft = targetRect.right + margin;
            const leftLeft = targetRect.left - cardWidth - margin;

            const placementMap = {
                below: { left: middleLeft, top: targetRect.bottom + margin },
                above: { left: middleLeft, top: targetRect.top - cardHeight - margin },
                right: { left: rightLeft, top: sideTop },
                left: { left: leftLeft, top: sideTop },
                center: { left: middleLeft, top: middleTop },
            };

            let order;
            if (preferredPlacement && placementMap[preferredPlacement]) {
                order = [preferredPlacement, "below", "above", "right", "left", "center"];
            } else if (targetRect.top < 120) {
                order = ["below", "left", "right", "above", "center"];
            } else if (targetRect.left > viewportWidth * 0.55) {
                order = ["left", "below", "above", "right", "center"];
            } else if (targetRect.width > viewportWidth * 0.55 || targetRect.height > viewportHeight * 0.35) {
                order = ["above", "below", "right", "left", "center"];
            } else {
                order = ["right", "left", "below", "above", "center"];
            }

            const uniqueOrder = order.filter(function (placement, index) {
                return order.indexOf(placement) === index && placementMap[placement];
            });

            for (const placement of uniqueOrder) {
                const candidate = placementMap[placement];
                if (
                    candidate.left >= margin &&
                    candidate.top >= margin &&
                    candidate.left + cardWidth <= viewportWidth - margin &&
                    candidate.top + cardHeight <= viewportHeight - margin
                ) {
                    setCardPosition(candidate.left, candidate.top);
                    return;
                }
            }

            const rankedCandidates = uniqueOrder.map(function (placement, index) {
                const raw = placementMap[placement];
                const left = clamp(raw.left, margin, maxLeft);
                const top = clamp(raw.top, margin, maxTop);
                const candidateRect = { left: left, top: top, width: cardWidth, height: cardHeight };
                const clampPenalty = Math.abs(raw.left - left) + Math.abs(raw.top - top);
                const overlapPenalty = overlapArea(candidateRect, targetRect);
                return {
                    left: left,
                    top: top,
                    score: overlapPenalty + clampPenalty * 100 + index,
                };
            }).sort(function (a, b) { return a.score - b.score; });

            const fallback = rankedCandidates[0] || { left: margin, top: margin };
            setCardPosition(fallback.left, fallback.top);
        }

        function updatePositions() {
            if (!root.classList.contains("active")) return;
            const step = steps[currentIndex];
            const target = step && step.selector ? document.querySelector(step.selector) : null;
            currentTarget = target;
            positionSpotlight(target);
            positionCard(target, step ? step.placement : null);
        }

        function schedulePositionUpdate() {
            window.requestAnimationFrame(function () {
                updatePositions();
                window.requestAnimationFrame(updatePositions);
            });
        }

        function renderStep() {
            clearHighlight();
            const step = steps[currentIndex];
            if (!step) return;
            const target = step.selector ? document.querySelector(step.selector) : null;
            currentTarget = target;
            if (target) target.classList.add("tutorial-source-active");
            title.textContent = step.title;
            body.textContent = step.body;
            hint.textContent = step.hint || (target ? "" : "対象の機能がこのページにない場合は、上部メニューから該当ページへ移動してください。");
            count.textContent = `${currentIndex + 1} / ${steps.length}`;
            prevButton.disabled = currentIndex === 0;
            nextButton.textContent = currentIndex === steps.length - 1 ? "完了する" : "次へ";

            if (target && typeof target.scrollIntoView === "function") {
                target.scrollIntoView({ behavior: "auto", block: step.scrollBlock || "center", inline: "nearest" });
            }
            schedulePositionUpdate();
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
            if (resizeFrame) window.cancelAnimationFrame(resizeFrame);
            resizeFrame = window.requestAnimationFrame(updatePositions);
        });

        window.addEventListener("scroll", function () {
            if (!root.classList.contains("active") || !currentTarget) return;
            updatePositions();
        }, true);

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
