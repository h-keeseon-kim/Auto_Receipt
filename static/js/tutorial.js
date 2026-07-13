(function () {
    "use strict";

    const STATE_KEY = "receipthub:tutorial-state:v3";
    const STATE_MAX_AGE_MS = 1000 * 60 * 60 * 2;

    const USER_STEPS = [
        {
            pageName: "user_services",
            selector: "[data-tutorial-target='user-services-page']",
            title: "利用サービスページから始めます",
            body: "ReceiptHubでは、まず自分が使っているサービスを確認します。チュートリアル中は、この説明に必要なページへ自動で移動します。",
            placement: "below",
        },
        {
            pageName: "user_services",
            selector: "[data-tutorial-target='service-registration-button']",
            title: "登録済みサービスはここから追加します",
            body: "管理者がサービスマスターに登録したサービスは「サービス利用登録」から選択し、すぐに利用中サービスへ追加できます。管理者承認は必要ありません。",
            placement: "left",
        },
        {
            pageName: "user_services",
            selector: "[data-tutorial-target='service-exception-button']",
            title: "一覧にない新規サービスだけ例外申請します",
            body: "サービス利用登録の一覧に存在しないサービスを使いたい場合だけ、サービス名、支払い方法、用途を記載して例外申請します。管理者の承認後にサービスマスターと利用サービスへ追加されます。",
            placement: "left",
        },
        {
            pageName: "user_services",
            selector: "[data-tutorial-target='service-exception-history-section']",
            title: "例外申請の状態を確認します",
            body: "サービスマスターにない新規サービスについて、確認待ち、承認済み、却下の状態と管理者コメントをここで確認できます。",
            placement: "above",
            scrollBlock: "nearest",
        },
        {
            pageName: "user_services",
            selector: "[data-tutorial-target='active-services-section']",
            title: "利用中サービスを確認します",
            body: "領収書アップロード時に選べるサービスは、ここに表示される利用中サービスです。ユーザー登録、管理者登録、例外申請承認のどの方法で追加されたかも確認できます。",
            placement: "above",
            scrollBlock: "nearest",
        },
        {
            pageName: "user_services",
            selector: "[data-tutorial-target='service-stop-button'], [data-tutorial-target='stopped-services-section']",
            title: "使わなくなったサービスを停止します",
            body: "使わなくなったサービスは「利用停止」から停止します。停止時には、最後にアップロードすべき領収書月を選択します。",
            placement: "above",
            scrollBlock: "nearest",
        },
        {
            pageName: "dashboard",
            selector: "[data-tutorial-target='upload-page']",
            title: "領収書アップロードページです。",
            body: "ここからは、利用サービスで登録したサービスを使って領収書をアップロードする画面を説明します。",
            placement: "below",
        },
        {
            pageName: "dashboard",
            selector: "[data-tutorial-target='upload-month-form']",
            title: "提出月を選びます",
            body: "まず対象となる提出月を選びます。月を変更して「表示」を押すと、その月の提出画面に切り替わります。",
            placement: "left",
        },
        {
            pageName: "dashboard",
            selector: "[data-tutorial-target='receipt-add-form']",
            title: "サービスを選んで領収書を追加します",
            body: "登録済みサービスまたは「その他」を選び、「＋ 領収書を追加」からファイルを選びます。同じサービスに複数の領収書を追加でき、一度に複数ファイルも選択できます。",
            placement: "above",
            scrollBlock: "center",
        },
        {
            pageName: "dashboard",
            selector: "[data-tutorial-target='uploaded-receipts-section']",
            title: "アップロード済み領収書を確認して提出します",
            body: "アップロードした領収書はここに追加されます。内容を確認し、月内の領収書が揃ったら「提出する」を押します。提出後も、間違えたファイルは修正できます。",
            placement: "above",
            scrollBlock: "nearest",
        },
        {
            pageName: "history",
            selector: "[data-tutorial-target='history-page']",
            title: "提出履歴ページです。",
            body: "ここでは、提出済み・下書きの月別状況を確認できます。",
            placement: "below",
        },
        {
            pageName: "history",
            selector: "[data-tutorial-target='history-table']",
            title: "月ごとの提出状況を確認します",
            body: "この一覧から、対象月のステータス、領収書数、提出日時を確認できます。詳細ボタンを押すと、その月の提出内容を確認できます。",
            placement: "above",
        },
        {
            selector: "[data-tutorial-target='tutorial-help-button']",
            title: "チュートリアルはいつでも再表示できます",
            body: "一度完了した後も、右上の「？」を押すとこのチュートリアルを再度確認できます。",
            placement: "left",
        },
    ];

    const STAFF_STEPS = [
        {
            pageName: "history",
            selector: "[data-tutorial-target='staff-history-nav']",
            title: "提出履歴で全体を確認します",
            body: "管理者は提出履歴を起点に、対象月の提出状況とアップロード済み領収書を確認します。提出状況はユーザー名順、領収書はアップロード日の新しい順で表示されます。",
            placement: "below",
        },
        {
            pageName: "history",
            selector: "[data-tutorial-target='staff-status-table']",
            title: "ユーザー別の提出状況を見ます",
            body: "この表では、ユーザーごとの提出ステータス、領収書数、保存中ファイル数、再提出待ちなどを確認できます。",
            placement: "above",
        },
        {
            pageName: "history",
            selector: "[data-tutorial-target='staff-receipt-table']",
            title: "アップロード済み領収書を確認します",
            body: "領収書ごとに、AI確認チェック、管理者用メモ、ダウンロード、削除、再提出指示を確認できます。",
            placement: "above",
        },
        {
            pageName: "staff_card_statements",
            selector: "[data-tutorial-target='staff-statements-nav']",
            title: "ご利用代金明細ページです",
            body: "会社全体のカード明細をアップロードし、対象月に全ユーザーが提出した領収書と一括照合します。",
            placement: "below",
        },
        {
            pageName: "staff_card_statements",
            selector: "[data-tutorial-target='staff-statement-upload-section']",
            title: "全社明細書をアップロードします",
            body: "対象月を選び、ユーザーを指定せずに会社共通のご利用代金明細書をアップロードします。AIが明細表の全行を抽出します。",
            placement: "above",
        },
        {
            pageName: "staff_card_statements",
            selector: "[data-tutorial-target='staff-statement-results']",
            title: "未提出の明細行を確認します",
            body: "黄色は一致する領収書が全ユーザーの提出物から見つからない行、薄橙色はユーザーまたはサービスの対応が曖昧な行です。",
            placement: "above",
        },
        {
            pageName: "staff_services",
            selector: "[data-tutorial-target='staff-services-nav']",
            title: "利用サービス管理を行います",
            body: "サービスマスターの登録、ユーザー別の登録状況、登録サービス一覧、新規登録/停止の確認を行います。",
            placement: "below",
        },
        {
            pageName: "staff_services",
            selector: "[data-tutorial-target='staff-service-catalog-section']",
            title: "サービスマスターを管理します",
            body: "ここでユーザーが選択できるサービスマスターを登録します。同じサービス名でも、サブスクや従量課金 / APIなど種別ごとに分けられます。",
            placement: "above",
            scrollBlock: "nearest",
        },
        {
            pageName: "staff_exception_requests",
            selector: "[data-tutorial-target='staff-exception-requests-nav']",
            title: "サービス例外申請を確認します",
            body: "サービスマスターに存在しない新規サービスについて、ユーザーから届いた例外申請を確認し、承認または却下します。確認待ち件数はメニューのバッジにも表示されます。",
            placement: "below",
        },
        {
            pageName: "staff_exception_requests",
            selector: "[data-tutorial-target='staff-exception-request-list']",
            title: "承認すると利用サービスへ追加されます",
            body: "承認すると新しいサービスマスターを作成し、対象ユーザーの利用サービスへ自動で追加します。却下時は理由を入力します。",
            placement: "above",
        },
        {
            pageName: "staff_user_create",
            selector: "[data-tutorial-target='staff-user-create-nav']",
            title: "ユーザーを管理します",
            body: "新しい一般ユーザーの発行、初期パスワードの生成、利用ステータスの管理を行います。",
            placement: "below",
        },
        {
            selector: "[data-tutorial-target='tutorial-help-button']",
            title: "チュートリアルはいつでも再表示できます",
            body: "右上の「？」を押すと、管理者向けチュートリアルを再度確認できます。",
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
        if (!url) return Promise.resolve();
        return fetch(url, {
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

    function readState() {
        try {
            const raw = window.sessionStorage.getItem(STATE_KEY);
            if (!raw) return null;
            const parsed = JSON.parse(raw);
            if (!parsed || parsed.active !== true) return null;
            if (!parsed.startedAt || Date.now() - parsed.startedAt > STATE_MAX_AGE_MS) {
                window.sessionStorage.removeItem(STATE_KEY);
                return null;
            }
            return parsed;
        } catch (error) {
            return null;
        }
    }

    function writeState(state) {
        try {
            window.sessionStorage.setItem(STATE_KEY, JSON.stringify(state));
        } catch (error) {
            // sessionStorage が使えない環境でも、現在ページ内の操作は継続する。
        }
    }

    function clearState() {
        try {
            window.sessionStorage.removeItem(STATE_KEY);
        } catch (error) {
            // noop
        }
    }

    function sameAbsoluteUrl(a, b) {
        try {
            const first = new URL(a, window.location.href);
            const second = new URL(b, window.location.href);
            return first.href === second.href;
        } catch (error) {
            return a === b;
        }
    }

    function initTutorial() {
        const root = document.querySelector("[data-tutorial-root]");
        if (!root) return;

        const role = document.body.dataset.tutorialRole === "staff" ? "staff" : "user";
        const steps = role === "staff" ? STAFF_STEPS : USER_STEPS;
        const pageUrls = {
            user_services: root.dataset.userServicesUrl,
            dashboard: root.dataset.uploadUrl,
            history: root.dataset.historyUrl,
            staff_card_statements: root.dataset.staffStatementsUrl,
            staff_services: root.dataset.staffServicesUrl,
            staff_exception_requests: root.dataset.staffExceptionRequestsUrl,
            staff_user_create: root.dataset.staffUserCreateUrl,
        };
        const card = root.querySelector(".tutorial-card");
        const title = root.querySelector("[data-tutorial-title]");
        const body = root.querySelector("[data-tutorial-body]");
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
        let tutorialReturnUrl = window.location.href;
        let tutorialStartedAt = Date.now();
        let closing = false;

        function currentUrlName() {
            return document.body.dataset.currentUrlName || "";
        }

        function statePayload(index) {
            return {
                active: true,
                role: role,
                index: index,
                returnUrl: tutorialReturnUrl,
                startedAt: tutorialStartedAt,
            };
        }

        function persistState(index) {
            writeState(statePayload(index));
        }

        function stepUrl(step) {
            return step && step.pageName ? pageUrls[step.pageName] : "";
        }

        function needsPageNavigation(step) {
            if (!step || !step.pageName) return false;
            return currentUrlName() !== step.pageName;
        }

        function navigateToStepPage(index) {
            const step = steps[index];
            if (!needsPageNavigation(step)) return false;
            const url = stepUrl(step);
            if (!url) return false;
            persistState(index);
            window.location.assign(url);
            return true;
        }

        function returnToStartPage() {
            if (!tutorialReturnUrl || sameAbsoluteUrl(tutorialReturnUrl, window.location.href)) return;
            window.location.assign(tutorialReturnUrl);
        }

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
            const preferred = preferredPlacement && placementMap[preferredPlacement] ? preferredPlacement : "below";
            const order = [preferred, "below", "above", "right", "left", "center"];
            const uniqueOrder = order.filter(function (placement, index, placements) {
                return placements.indexOf(placement) === index;
            });

            for (const placement of uniqueOrder) {
                const raw = placementMap[placement];
                const candidate = {
                    left: clamp(raw.left, margin, maxLeft),
                    top: clamp(raw.top, margin, maxTop),
                };
                const fitsPreferredSide =
                    (placement === "below" && targetRect.bottom + margin + cardHeight <= viewportHeight) ||
                    (placement === "above" && targetRect.top - margin - cardHeight >= 0) ||
                    (placement === "right" && targetRect.right + margin + cardWidth <= viewportWidth) ||
                    (placement === "left" && targetRect.left - margin - cardWidth >= 0) ||
                    placement === "center";
                if (
                    fitsPreferredSide &&
                    overlapArea({ left: candidate.left, top: candidate.top, width: cardWidth, height: cardHeight }, targetRect) === 0
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
            if (navigateToStepPage(currentIndex)) return;
            clearHighlight();
            const step = steps[currentIndex];
            if (!step) return;
            persistState(currentIndex);
            const target = step.selector ? document.querySelector(step.selector) : null;
            currentTarget = target;
            if (target) target.classList.add("tutorial-source-active");
            title.textContent = step.title;
            body.textContent = step.body;
            count.textContent = `${currentIndex + 1} / ${steps.length}`;
            prevButton.disabled = currentIndex === 0;
            nextButton.textContent = currentIndex === steps.length - 1 ? "完了する" : "次へ";

            if (target && typeof target.scrollIntoView === "function") {
                target.scrollIntoView({ behavior: "auto", block: step.scrollBlock || "center", inline: "nearest" });
            }
            schedulePositionUpdate();
        }

        function openTutorial(startAt, options) {
            const restoreOptions = options || {};
            currentIndex = typeof startAt === "number" ? startAt : 0;
            tutorialReturnUrl = restoreOptions.returnUrl || window.location.href;
            tutorialStartedAt = restoreOptions.startedAt || Date.now();
            closing = false;
            if (navigateToStepPage(currentIndex)) return;
            root.hidden = false;
            root.classList.add("active");
            document.body.classList.add("tutorial-open");
            renderStep();
            if (card) card.focus({ preventScroll: true });
        }

        function closeTutorial(options) {
            if (closing) return;
            closing = true;
            const save = !options || options.save !== false;
            clearHighlight();
            root.classList.remove("active");
            root.hidden = true;
            document.body.classList.remove("tutorial-open");
            clearState();
            const completion = save ? markCompleted(root) : Promise.resolve();
            completion.finally(returnToStartPage);
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

        const restoredState = readState();
        if (restoredState && restoredState.role === role) {
            window.setTimeout(function () {
                openTutorial(restoredState.index || 0, {
                    returnUrl: restoredState.returnUrl,
                    startedAt: restoredState.startedAt,
                });
            }, 120);
            return;
        }

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
