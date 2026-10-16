const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const events = {};
const timers = new Map();
const calls = [];
let time = 0, timerId = 0;
const plot = {data: [{name: "history"}, {name: "Live price"}]};
const context = {
    document: {
        readyState: "loading",
        addEventListener(name, fn) { (events[name] ||= []).push(fn); },
        getElementById() { return {querySelector: () => plot}; }
    },
    localStorage: {getItem: () => "[]"},
    setInterval() {},
    setTimeout(fn, delay) { const id = ++timerId; timers.set(id, {fn, due: time + delay}); return id; },
    clearTimeout(id) { timers.delete(id); },
    addEventListener() {},
    dash_clientside: {no_update: "no_update"},
    Plotly: {
        restyle(target, update, indices) { calls.push({target, update, indices}); },
        addTraces() { throw new Error("Unexpected replacement of existing live trace"); }
    }
};
context.window = context;
vm.runInNewContext(fs.readFileSync("assets/dashboard.js", "utf8"), context);
function emit(name) {
    const event = {target: {closest: () => plot}};
    for (const fn of events[name] || []) fn(event);
}
function advance(to) {
    time = to;
    for (const [id, task] of [...timers]) {
        if (task.due <= time) { timers.delete(id); task.fn(); }
    }
}
const selection = {symbol: "CL-CO", expression: "Z26-F27"};
function tick(value) {
    context.marketCurveMinuteMarkers.update({
        selection, zero_base: true, overlay: {x: [30], y: [value]}
    }, selection, ["yes"]);
}
tick(1);
assert.equal(calls.length, 1);
assert.equal(calls[0].indices[0], 1, "Only the live marker may be restyled");
emit("pointerdown");
tick(2);
tick(3);
assert.equal(calls.length, 1, "Live ticks must wait during a drag or legend click");
emit("pointerup");
advance(300);
plot.data[0].visible = "legendonly"; // Plotly finishes its delayed legend action.
tick(4);
advance(399);
assert.equal(calls.length, 1, "Do not redraw while Plotly resolves click versus double-click");
advance(400);
assert.equal(calls.length, 2);
assert.equal(calls[1].update.y[0][0], 4, "Apply the newest queued quote");
assert.equal(plot.data[0].visible, "legendonly");
context.marketCurveMinuteMarkers.update({
    selection, zero_base: true, overlay: {x: [30], y: [99]}
}, {symbol: "CL-CO", expression: "F27-G27"}, ["yes"]);
assert.equal(calls.length, 2, "Reject quotes for the previous cell");
console.log("Live marker interaction checks passed.");
