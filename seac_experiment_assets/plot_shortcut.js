// Middle-click anywhere in this dashboard to apply the current Plot settings.
(function () {
    function isPlotShortcut(event) {
        return event.button === 1 && event.target instanceof Element &&
            event.target.closest('.seac-builder');
    }

    document.addEventListener('mousedown', function (event) {
        if (!isPlotShortcut(event)) return;
        event.preventDefault(); // Suppress browser auto-scroll.
        event.stopPropagation();
    }, true);

    document.addEventListener('auxclick', function (event) {
        if (!isPlotShortcut(event)) return;
        event.preventDefault();
        event.stopPropagation();
        const button = document.getElementById('seac-plot');
        if (button && !button.disabled) button.click();
    }, true);
})();
