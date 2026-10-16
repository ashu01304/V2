(function () {
    document.addEventListener("keydown", function (event) {
        if (event.key !== "Escape") {
            return;
        }
        var modal = document.getElementById("chart-modal");
        var closeButton = document.getElementById("close-modal");
        if (modal && modal.style.display !== "none" && closeButton) {
            event.preventDefault();
            closeButton.click();
        }
    }, true);
})();


(function () {
    var storageKey = "market-curves-cell-bookmarks-v1";
    var bookmarks;
    try {
        var saved = JSON.parse(localStorage.getItem(storageKey) || "[]");
        bookmarks = new Set(Array.isArray(saved) ? saved : []);
    } catch (error) {
        bookmarks = new Set();
    }

    function bookmarkKey(cell) {
        var productTable = cell.closest('[id^="product-table-"]');
        var contracts = document.getElementById("contract-labels");
        var row = cell.getAttribute("data-dash-row");
        var column = cell.getAttribute("data-dash-column");
        if (!productTable || !contracts || row === null || !column) {
            return null;
        }
        var contract = contracts.querySelector(
            'td[data-dash-row="' + row + '"][data-dash-column="Contract"]'
        );
        if (!contract) {
            return null;
        }
        return JSON.stringify([
            productTable.id.slice("product-table-".length),
            contract.textContent.trim(), column
        ]);
    }

    var rangePositions = {};
    var visibleKeys = [];
    var fetchingRanges = false;

    function paint(cell, key) {
        var bookmarked = bookmarks.has(key);
        cell.toggleAttribute("data-bookmarked", bookmarked);
        var indicator = cell.querySelector(".bookmark-range");
        var position = rangePositions[key];
        if (!bookmarked || !Number.isFinite(position)) {
            if (indicator) indicator.remove();
            return;
        }
        if (!indicator) {
            indicator = document.createElement("span");
            indicator.className = "bookmark-range";
            indicator.setAttribute("role", "img");
            indicator.setAttribute("aria-label", "Daily range: high at top, low at bottom; dot is current level");
            cell.appendChild(indicator);
        }
        indicator.style.setProperty("--range-position", (100 * (1 - Math.max(0, Math.min(1, position)))) + "%");
    }

    function repaintRanges() {
        var root = document.getElementById("product-tables");
        if (!root) return;
        var keys = [];
        root.querySelectorAll("td[data-dash-row][data-dash-column]").forEach(function(cell) {
            var key = bookmarkKey(cell);
            if (key) {
                paint(cell, key);
                if (bookmarks.has(key)) keys.push(key);
            }
        });
        var changed = JSON.stringify(keys) !== JSON.stringify(visibleKeys);
        visibleKeys = keys;
        if (changed && keys.length) refreshRanges();
    }

    async function refreshRanges() {
        if (fetchingRanges || !visibleKeys.length) return;
        fetchingRanges = true;
        try {
            var response = await fetch("/_bookmark-ranges", {
                method: "POST", headers: {"Content-Type": "application/json"},
                body: JSON.stringify({keys: visibleKeys})
            });
            rangePositions = response.ok ? await response.json() : {};
        } catch (error) {
            rangePositions = {};
        } finally {
            fetchingRanges = false;
            repaintRanges();
        }
    }

    // No requests occur before a bookmarked cell is visible.
    setInterval(refreshRanges, 10000);

    function handleAltClick(event) {
        if (!event.altKey || !event.target.closest) {
            return;
        }
        var cell = event.target.closest("td[data-dash-row][data-dash-column]");
        var key = cell && bookmarkKey(cell);
        if (!key) {
            return;
        }
        // Stop Dash selection handlers so bookmarking does not open the popup.
        event.preventDefault();
        event.stopImmediatePropagation();
        if (event.type !== "click") {
            return;
        }
        if (bookmarks.has(key)) {
            bookmarks.delete(key);
        } else {
            bookmarks.add(key);
        }
        repaintRanges();
        try {
            localStorage.setItem(storageKey, JSON.stringify(Array.from(bookmarks)));
        } catch (error) {
            // Keep bookmarks usable when browser storage is unavailable.
        }
    }

    ["pointerdown", "mousedown", "click", "dblclick"].forEach(function(type) {
        document.addEventListener(type, handleAltClick, true);
    });

    function watchTables() {
        var root = null;
        var scheduled = false;
        var observer = new MutationObserver(function() {
            var tables = document.getElementById("product-tables");
            if (!tables) {
                return;
            }
            if (root !== tables) {
                root = tables;
                observer.disconnect();
                observer.observe(root, {childList: true, subtree: true});
            }
            if (scheduled) {
                return;
            }
            scheduled = true;
            requestAnimationFrame(function() {
                scheduled = false;
                repaintRanges();
            });
        });
        observer.observe(document.body, {childList: true, subtree: true});
        // Also restore bookmarks when the tables already exist at script load.
        var tables = document.getElementById("product-tables");
        if (tables) {
            root = tables;
            observer.disconnect();
            observer.observe(root, {childList: true, subtree: true});
            repaintRanges();
        }
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", watchTables);
    } else {
        watchTables();
    }
})();

(function () {
    var pending = new Map();
    var interacting = false;
    var currentSelection = "";
    var interactionTimer = null;
    function selectionKey(selection) {
        return selection ? selection.symbol + ":" + selection.expression : "";
    }
    function flush() {
        if (interacting || !window.Plotly) return;
        pending.forEach(function(trace, id) {
            var graph = document.getElementById(id);
            var plot = graph && graph.querySelector(".js-plotly-plot");
            if (!plot || !plot.data || plot._dragging) return;
            pending.delete(id);
            var index = plot.data.findIndex(function(item) { return item.name === "Live price"; });
            if (index < 0) {
                if (trace.x && trace.x.length) window.Plotly.addTraces(plot, trace);
            } else {
                window.Plotly.restyle(plot, {
                    x: [trace.x || []], y: [trace.y || []],
                    customdata: [trace.customdata || []]
                }, [index]);
            }
        });
    }
    // Finish zooms and legend clicks before moving the live marker.
    document.addEventListener("pointerdown", function(event) {
        if (event.target.closest && event.target.closest(".js-plotly-plot")) {
            clearTimeout(interactionTimer);
            interacting = true;
        }
    }, true);
    function finishInteraction() {
        clearTimeout(interactionTimer);
        // Plotly waits 300 ms to distinguish legend clicks from double-clicks.
        // Restyling during that wait can discard the pending legend action.
        interactionTimer = setTimeout(function() {
            interacting = false;
            flush();
        }, 400);
    }
    document.addEventListener("pointerup", finishInteraction, true);
    document.addEventListener("pointercancel", finishInteraction, true);
    window.addEventListener("blur", finishInteraction);
    window.marketCurveMinuteMarkers = {
        update: function(data, selection, zeroBase) {
            var key = selectionKey(selection);
            if (key !== currentSelection) {
                pending.clear();
                currentSelection = key;
            }
            if (!data || !key || selectionKey(data.selection) !== key) {
                pending.clear();
                return window.dash_clientside.no_update;
            }
            if (data.candle) pending.set("popup-candle-chart", data.candle);
            if (data.overlay && data.zero_base === Boolean(zeroBase && zeroBase.length)) {
                pending.set("popup-overlay-chart", data.overlay);
            } else {
                pending.delete("popup-overlay-chart");
            }
            flush();
            return Date.now();
        }
    };
})();
