const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

(async function () {
    const key = JSON.stringify(["CL", "Z26", "1MS"]);
    const handlers = {};
    const cells = [];
    let fetchCount = 0;
    let interval;
    const table = {id: "product-table-CL"};
    const root = {querySelectorAll: () => cells};
    const contracts = {querySelector: () => ({textContent: "Z26"})};
    function cell() {
        const children = [];
        return {
            attrs: {}, children,
            closest: () => table,
            getAttribute: name => name === "data-dash-row" ? "0" : "1MS",
            toggleAttribute(name, enabled) { this.attrs[name] = enabled; },
            querySelector: () => children[0] || null,
            appendChild(node) { children.push(node); node.remove = () => children.splice(children.indexOf(node), 1); }
        };
    }
    const context = {
        document: {
            readyState: "complete", body: {},
            addEventListener(name, handler) { (handlers[name] ||= []).push(handler); },
            getElementById: id => id === "product-tables" ? root : id === "contract-labels" ? contracts : null,
            createElement: () => ({style: {setProperty(name, value) { this[name] = value; }}, setAttribute() {}})
        },
        localStorage: {getItem: () => "[]", setItem() {}},
        MutationObserver: class {observe() {} disconnect() {}},
        requestAnimationFrame: fn => fn(),
        setInterval: fn => { interval = fn; },
        fetch: async () => { fetchCount++; return {ok: true, json: async () => ({[key]: .75})}; }
    };
    context.window = context;
    context.addEventListener = () => {};
    vm.runInNewContext(fs.readFileSync("assets/dashboard.js", "utf8"), context);
    const settle = () => new Promise(resolve => setImmediate(resolve));
    await interval();
    assert.equal(fetchCount, 0, "No requests before visible bookmarks");
    const td = cell();
    cells.push(td);
    const event = () => ({altKey: true, type: "click", target: {closest: () => td},
                         preventDefault() {}, stopImmediatePropagation() {}});
    handlers.click[0](event());
    await settle();
    assert.equal(fetchCount, 1);
    assert.equal(td.attrs["data-bookmarked"], true);
    assert.equal(td.children.length, 1);
    assert.equal(td.children[0].style["--range-position"], "25%");
    await interval();
    assert.equal(td.children.length, 1, "Refresh must not duplicate indicators");
    handlers.click[0](event());
    assert.equal(td.attrs["data-bookmarked"], false);
    assert.equal(td.children.length, 0);
    const before = fetchCount;
    await interval();
    assert.equal(fetchCount, before, "No requests after removing bookmark");
    console.log("Bookmark indicator DOM checks passed.");
})().catch(error => { console.error(error); process.exitCode = 1; });
