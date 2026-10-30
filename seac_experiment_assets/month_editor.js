// Both controls respond to the wheel under the pointer, without taking focus.
document.addEventListener("wheel", function (event) {
    if (!event.deltaY) return;
    const popup = document.getElementById("seac-month-editor");
    if (!popup || !popup.offsetHeight) return;
    const structure = event.target.closest("#seac-structure");
    if (structure) {
        event.preventDefault();
        const options = ["outright", "spread", "fly", "defly"];
        const current = Math.max(0, options.indexOf(window.seacMonthStructure));
        const next = Math.max(0, Math.min(options.length - 1, current + (event.deltaY < 0 ? 1 : -1)));
        window.seacMonthStructure = options[next];
        window.dash_clientside.set_props("seac-structure", {value: options[next]});
        return;
    }
    const count = event.target.closest("#seac-leg-count, .dash-input-container:has(#seac-leg-count)");
    if (!count) return;
    const input = document.getElementById("seac-leg-count");
    event.preventDefault();
    const current = Number(input.value || 0);
    if (!Number.isFinite(current)) return;
    const value = current + (event.deltaY < 0 ? 1 : -1);
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
    setter.call(input, String(value));
    input.dispatchEvent(new Event("input", {bubbles: true}));
    window.dash_clientside.set_props("seac-leg-count", {value: value});
}, {passive: false});

// Anchor the compact editor to its month, including within the scrolling strip.
(function () {
    let anchor;
    function positionPopup() {
        if (!anchor || !anchor.isConnected) return;
        const rect = anchor.getBoundingClientRect();
        const popup = document.getElementById("seac-month-editor");
        const height = popup && popup.offsetHeight || 90;
        const width = Math.min(160, window.innerWidth - 16);
        const left = Math.max(8, Math.min(rect.left, window.innerWidth - width - 8));
        const top = rect.bottom + height + 6 <= window.innerHeight
            ? rect.bottom + 6 : Math.max(8, rect.top - height - 6);
        document.documentElement.style.setProperty("--month-popup-left", left + "px");
        document.documentElement.style.setProperty("--month-popup-top", top + "px");
    }
    document.addEventListener("click", function (event) {
        const cell = event.target.closest(".seac-month-cell");
        if (cell) {
            anchor = cell;
            positionPopup();
            return;
        }
        const popup = document.getElementById("seac-month-editor");
        if (popup && popup.offsetHeight && !popup.contains(event.target)
            && !event.target.closest('[role="option"], [role="listbox"]')) {
            document.getElementById("seac-close-month").click();
        }
    });
    document.addEventListener("keydown", function (event) {
        if (event.key === "Escape") {
            const close = document.getElementById("seac-close-month");
            if (close) close.click();
        }
    });
    window.addEventListener("resize", positionPopup);
    document.addEventListener("scroll", positionPopup, true);
})();

// Move a structure between months; the server retains its count and type.
(function () {
    let source = null;
    function clearTarget() {
        document.querySelectorAll('.seac-drop-target').forEach(cell => cell.classList.remove('seac-drop-target'));
    }
    document.addEventListener('dragstart', function (event) {
        const value = event.target.closest('.seac-month-value[draggable="true"]');
        if (!value) return;
        source = JSON.parse(value.id).code;
        event.dataTransfer.setData('text/plain', source);
        event.dataTransfer.effectAllowed = 'move';
    });
    document.addEventListener('dragover', function (event) {
        const cell = event.target.closest('.seac-month-cell');
        if (!source || !cell) return;
        event.preventDefault();
        event.dataTransfer.dropEffect = 'move';
        clearTarget();
        cell.classList.add('seac-drop-target');
    });
    document.addEventListener('drop', function (event) {
        const cell = event.target.closest('.seac-month-cell');
        if (!source || !cell) return;
        event.preventDefault();
        const target = JSON.parse(cell.querySelector('.seac-month-value').id).code;
        if (target !== source) {
            window.dash_clientside.set_props('seac-month-move', {
                data: {source: source, target: target, revision: Date.now()}
            });
        }
        source = null;
        clearTarget();
    });
    document.addEventListener('dragend', function () { source = null; clearTarget(); });
})();
