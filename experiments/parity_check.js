#!/usr/bin/env node
/* Parity check: docs/engine.js (browser simulator) vs the Python engine.
 *
 * Reads /tmp/fixture_1m.json and /tmp/py_reference.json written by
 * experiments/make_fixtures.py, reruns every case through the JavaScript
 * engine, and demands:
 *   - identical trade count, entry/exit times, exit reasons, bars held
 *   - matching prices, weights and returns to 1e-9
 *   - matching headline metrics to 1e-6
 *
 * Exit code 0 = parity, 1 = mismatch. Run: node experiments/parity_check.js
 */
"use strict";

const fs = require("fs");
const path = require("path");
const E = require(path.join(__dirname, "..", "docs", "engine.js"));

const fixture = JSON.parse(fs.readFileSync("/tmp/fixture_1m.json", "utf8"));
const reference = JSON.parse(fs.readFileSync("/tmp/py_reference.json", "utf8"));

const rows1m = fixture.map(k => ({ t: k[0], open: k[1], high: k[2], low: k[3],
                                   close: k[4], volume: k[5], taker_buy: k[6] }));

const PRICE_TOL = 1e-9, METRIC_TOL = 1e-6;
let failures = 0;

function fail(msg) { console.error("  MISMATCH: " + msg); failures += 1; }

function close(a, b, tol) { return Math.abs(a - b) <= tol * Math.max(1, Math.abs(a), Math.abs(b)); }

for (const ref of reference) {
  console.log(`case ${ref.name} (agg=${ref.agg}m)`);
  const p = Object.assign(E.defaultParams(), ref.params);
  const costs = Object.assign(E.defaultCosts(), ref.costs);
  const bars = E.aggregate(rows1m, ref.agg, true);
  if (bars.length !== ref.n_bars) {
    fail(`bar count: js ${bars.length} vs py ${ref.n_bars}`);
    continue;
  }
  const frame = E.addAll(bars, p);
  const score = E.computeScore(frame, p);
  const res = E.run(frame, p, costs, ref.agg, score);

  if (res.trades.length !== ref.trades.length) {
    fail(`trade count: js ${res.trades.length} vs py ${ref.trades.length}`);
    continue;
  }
  for (let i = 0; i < ref.trades.length; i++) {
    const [teIn, teOut, peIn, peOut, wPy, reasonPy, grossPy, netPy, wnetPy, heldPy] = ref.trades[i];
    const t = res.trades[i];
    const tag = `trade ${i}`;
    if (t.entry_time !== teIn || t.exit_time !== teOut)
      fail(`${tag} times: js ${t.entry_time}/${t.exit_time} vs py ${teIn}/${teOut}`);
    if (t.reason_out !== reasonPy) fail(`${tag} reason: js ${t.reason_out} vs py ${reasonPy}`);
    if (t.bars_held !== heldPy) fail(`${tag} held: js ${t.bars_held} vs py ${heldPy}`);
    if (!close(t.entry_price, peIn, PRICE_TOL)) fail(`${tag} entry px: js ${t.entry_price} vs py ${peIn}`);
    if (!close(t.exit_price, peOut, PRICE_TOL)) fail(`${tag} exit px: js ${t.exit_price} vs py ${peOut}`);
    if (!close(t.weight, wPy, METRIC_TOL)) fail(`${tag} weight: js ${t.weight} vs py ${wPy}`);
    if (!close(t.weighted_net, wnetPy, METRIC_TOL)) fail(`${tag} weighted net: js ${t.weighted_net} vs py ${wnetPy}`);
  }
  for (const [k, vPy] of Object.entries(ref.metrics)) {
    if (vPy === null) continue;
    const vJs = res.metrics[k];
    if (typeof vJs !== "number" || !close(vJs, vPy, METRIC_TOL))
      fail(`metric ${k}: js ${vJs} vs py ${vPy}`);
  }
  const eqJs = res.bar_returns.reduce((a, r) => a * (1 + r), 1.0);
  if (!close(eqJs, ref.equity_final, METRIC_TOL))
    fail(`final equity: js ${eqJs} vs py ${ref.equity_final}`);
}

if (failures) {
  console.error(`\nPARITY FAILED (${failures} mismatch(es))`);
  process.exit(1);
}
console.log("\nPARITY OK — the browser engine reproduces the Python engine exactly.");
