/* near-bot v2 engine — JavaScript port of src/near_bot/{data,indicators,score,backtest}.py
 *
 * Faithful port of the Python engine so the web simulator runs the SAME
 * strategy the bot trades: causal indicators, composite conviction score with
 * regime gates, hysteresis entry/exit at next bar's open, volatility-target
 * sizing, fees + slippage on every fill, exact per-bar mark-to-market
 * accounting.
 *
 * Parity with the Python implementation is enforced by
 * experiments/parity_check.js (identical trades on shared fixture data).
 * Works in the browser (window.NearBotEngine) and Node (module.exports).
 */
(function (root, factory) {
  if (typeof module !== "undefined" && module.exports) module.exports = factory();
  else root.NearBotEngine = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  const NaN_ = Number.NaN;

  // ---------------------------------------------------------------- defaults
  function defaultParams() {
    return {
      mom_fast: 36, mom_slow: 120, vol_window: 48,
      bo_window: 48, ofi_window: 24,
      w_mom: 0.50, w_bo: 0.30, w_ofi: 0.20,
      er_window: 48, er_min: 0.20, trend_window: 200,
      theta_in: 0.25, theta_out: 0.0,
      atr_period: 14, trail_atr: 3.0, max_hold_bars: 168,
      target_vol: 0.40, min_weight: 0.05,
    };
  }
  function defaultCosts() { return { taker_fee: 0.001, slippage: 0.0002 }; }
  function barsPerYear(barMinutes) { return 365.0 * 24.0 * 60.0 / barMinutes; }

  // ------------------------------------------------------------- aggregation
  // Mirrors data.aggregate(): bucket 1m bars by floor(open_time / N minutes).
  // rows: [{t(ms), open, high, low, close, volume, taker_buy}]
  function aggregate(rows1m, minutes, dropIncomplete = true) {
    const bucketMs = minutes * 60000;
    const out = [];
    let cur = null;
    for (const r of rows1m) {
      const b = Math.floor(r.t / bucketMs) * bucketMs;
      if (!cur || cur.t !== b) {
        if (cur) out.push(cur);
        cur = { t: b, open: r.open, high: r.high, low: r.low, close: r.close,
                volume: 0, taker_buy_volume: 0, _count: 0 };
      }
      cur.high = Math.max(cur.high, r.high);
      cur.low = Math.min(cur.low, r.low);
      cur.close = r.close;
      cur.volume += r.volume;
      cur.taker_buy_volume += r.taker_buy;
      cur._count += 1;
    }
    if (cur) out.push(cur);
    if (dropIncomplete && out.length && out[out.length - 1]._count < minutes) out.pop();
    return out;
  }

  // -------------------------------------------------------- rolling helpers
  // All match pandas semantics: NaN until `k` observations; std is ddof=1.
  function rollMean(x, k) {
    const n = x.length, out = new Float64Array(n).fill(NaN_);
    let sum = 0;
    for (let i = 0; i < n; i++) {
      sum += x[i];
      if (i >= k) sum -= x[i - k];
      if (i >= k - 1) out[i] = sum / k;
    }
    return out;
  }

  function rollSum(x, k) {
    const n = x.length, out = new Float64Array(n).fill(NaN_);
    let sum = 0;
    for (let i = 0; i < n; i++) {
      const v = x[i];
      if (!Number.isNaN(v)) sum += v;
      if (i >= k) { const old = x[i - k]; if (!Number.isNaN(old)) sum -= old; }
      if (i >= k - 1) out[i] = sum;
    }
    return out;
  }

  function rollStd(x, k) {
    const n = x.length, out = new Float64Array(n).fill(NaN_);
    let sum = 0, sumsq = 0;
    for (let i = 0; i < n; i++) {
      const v = x[i];
      if (!Number.isNaN(v)) { sum += v; sumsq += v * v; }
      if (i >= k) {
        const old = x[i - k];
        if (!Number.isNaN(old)) { sum -= old; sumsq -= old * old; }
      }
      if (i >= k - 1) {
        let varr = (sumsq - (sum * sum) / k) / (k - 1);  // ddof=1, like pandas
        if (varr < 0 && varr > -1e-18) varr = 0;
        out[i] = varr > 0 ? Math.sqrt(varr) : (varr === 0 ? 0 : NaN_);
      }
    }
    return out;
  }

  function rollMax(x, k) {
    const n = x.length, out = new Float64Array(n).fill(NaN_);
    for (let i = k - 1; i < n; i++) {
      let m = -Infinity;
      for (let j = i - k + 1; j <= i; j++) if (x[j] > m) m = x[j];
      out[i] = m;
    }
    return out;
  }

  function rollMin(x, k) {
    const n = x.length, out = new Float64Array(n).fill(NaN_);
    for (let i = k - 1; i < n; i++) {
      let m = Infinity;
      for (let j = i - k + 1; j <= i; j++) if (x[j] < m) m = x[j];
      out[i] = m;
    }
    return out;
  }

  // pandas ewm(alpha, adjust=False, min_periods=p).mean() for series whose
  // NaNs (if any) are leading-only.
  function ewmAlpha(x, alpha, minPeriods) {
    const n = x.length, out = new Float64Array(n).fill(NaN_);
    let state = null, count = 0;
    for (let i = 0; i < n; i++) {
      const v = x[i];
      if (Number.isNaN(v)) continue;
      state = state === null ? v : state + alpha * (v - state);
      count += 1;
      if (count >= minPeriods) out[i] = state;
    }
    return out;
  }

  // -------------------------------------------------------------- indicators
  function logReturns(close) {
    const n = close.length, out = new Float64Array(n).fill(NaN_);
    for (let i = 1; i < n; i++) out[i] = Math.log(close[i] / close[i - 1]);
    return out;
  }

  function realizedVol(close, win) { return rollStd(logReturns(close), win); }

  function tsmom(close, lookback, volWin) {
    const n = close.length, out = new Float64Array(n).fill(NaN_);
    const vol = realizedVol(close, volWin);
    const sq = Math.sqrt(lookback);
    for (let i = lookback; i < n; i++) {
      const v = vol[i];
      if (!Number.isNaN(v) && v !== 0) out[i] = (close[i] / close[i - lookback] - 1) / (v * sq);
    }
    return out;
  }

  function efficiencyRatio(close, win) {
    const n = close.length, out = new Float64Array(n).fill(NaN_);
    const absDiff = new Float64Array(n).fill(NaN_);
    for (let i = 1; i < n; i++) absDiff[i] = Math.abs(close[i] - close[i - 1]);
    const path = rollSum(absDiff, win);
    for (let i = win; i < n; i++) {
      const p = path[i];
      if (!Number.isNaN(p) && p !== 0) out[i] = Math.abs(close[i] - close[i - win]) / p;
    }
    return out;
  }

  function donchianPosition(high, low, close, win) {
    const n = close.length, out = new Float64Array(n).fill(NaN_);
    const hh = rollMax(high, win), ll = rollMin(low, win);
    for (let i = 0; i < n; i++) {
      const span = hh[i] - ll[i];
      if (!Number.isNaN(span) && span !== 0)
        out[i] = Math.max(-1, Math.min(1, 2 * (close[i] - ll[i]) / span - 1));
    }
    return out;
  }

  function ofiZscore(taker, volume, win) {
    const n = volume.length, out = new Float64Array(n).fill(NaN_);
    const ofi = new Float64Array(n).fill(NaN_);
    for (let i = 0; i < n; i++)
      if (volume[i] !== 0) ofi[i] = Math.max(-1, Math.min(1, 2 * Math.max(0, Math.min(1, taker[i] / volume[i])) - 1));
    const smooth = ewmAlpha(ofi, 2.0 / (win + 1.0), win);  // span convention
    const disp = rollStd(ofi, win);
    for (let i = 0; i < n; i++) {
      const d = disp[i];
      if (!Number.isNaN(smooth[i]) && !Number.isNaN(d) && d !== 0)
        out[i] = Math.max(-3, Math.min(3, smooth[i] / d));
    }
    return out;
  }

  function atr(high, low, close, period) {
    const n = close.length, tr = new Float64Array(n);
    for (let i = 0; i < n; i++) {
      const hl = high[i] - low[i];
      if (i === 0) { tr[i] = hl; continue; }
      const pc = close[i - 1];
      tr[i] = Math.max(hl, Math.abs(high[i] - pc), Math.abs(low[i] - pc));
    }
    return ewmAlpha(tr, 1.0 / period, period);
  }

  function addAll(bars, p) {
    const n = bars.length;
    const open = new Float64Array(n), high = new Float64Array(n),
          low = new Float64Array(n), close = new Float64Array(n),
          vol = new Float64Array(n), taker = new Float64Array(n);
    for (let i = 0; i < n; i++) {
      open[i] = bars[i].open; high[i] = bars[i].high; low[i] = bars[i].low;
      close[i] = bars[i].close; vol[i] = bars[i].volume; taker[i] = bars[i].taker_buy_volume;
    }
    return {
      n, t: bars.map(b => b.t), open, high, low, close,
      vol_bar: realizedVol(close, p.vol_window),
      mom_fast: tsmom(close, p.mom_fast, p.vol_window),
      mom_slow: tsmom(close, p.mom_slow, p.vol_window),
      breakout: donchianPosition(high, low, close, p.bo_window),
      ofi_z: ofiZscore(taker, vol, p.ofi_window),
      er: efficiencyRatio(close, p.er_window),
      trend_ma: rollMean(close, p.trend_window),
      atr: atr(high, low, close, p.atr_period),
    };
  }

  // ------------------------------------------------------------------- score
  function computeScore(f, p) {
    const n = f.n, score = new Float64Array(n).fill(NaN_);
    for (let i = 0; i < n; i++) {
      const warm = !(Number.isNaN(f.mom_fast[i]) || Number.isNaN(f.mom_slow[i]) ||
                     Number.isNaN(f.breakout[i]) || Number.isNaN(f.ofi_z[i]) ||
                     Number.isNaN(f.er[i]) || Number.isNaN(f.trend_ma[i]) ||
                     Number.isNaN(f.vol_bar[i]) || Number.isNaN(f.atr[i]));
      if (!warm) continue;
      const mom = 0.5 * Math.tanh(f.mom_fast[i]) + 0.5 * Math.tanh(f.mom_slow[i]);
      let s = p.w_mom * mom + p.w_bo * f.breakout[i] + p.w_ofi * (f.ofi_z[i] / 3.0);
      s = Math.max(-1, Math.min(1, s));
      const gated = f.close[i] > f.trend_ma[i] && f.er[i] >= p.er_min;
      score[i] = gated ? s : 0.0;
    }
    return score;
  }

  // ---------------------------------------------------------------- backtest
  function positionWeight(volBar, barMinutes, targetVol, minWeight, maxWeight = 1.0) {
    if (!Number.isFinite(volBar) || volBar <= 0) return 0.0;
    const annVol = volBar * Math.sqrt(barsPerYear(barMinutes));
    const w = Math.min(maxWeight, targetVol / annVol);
    return w >= minWeight ? w : 0.0;
  }

  function run(f, p, costs, barMinutes, score) {
    const n = f.n;
    score = score || computeScore(f, p);
    const fee = costs.taker_fee, slip = costs.slippage;
    const ret = new Float64Array(n), posArr = new Float64Array(n);
    const trades = [];
    let w = 0, entryBar = -1, peak = 0, fillIn = 0, lastCounted = -1;

    function closeTrade(exitBar, fillOut, reason, held) {
      const gross = fillOut / fillIn - 1.0;
      const net = gross - 2.0 * fee;
      trades.push({ entry_time: f.t[entryBar], exit_time: f.t[exitBar],
                    entry_price: fillIn, exit_price: fillOut, weight: w,
                    reason_out: reason, gross_pct: gross, net_pct: net,
                    weighted_net: net * w, bars_held: held });
    }

    let i = 1;
    while (i < n - 1) {
      if (w === 0) {
        const s = score[i];
        if (Number.isFinite(s) && s > p.theta_in) {
          const wNew = positionWeight(f.vol_bar[i], barMinutes, p.target_vol, p.min_weight);
          if (wNew > 0) {
            w = wNew;
            entryBar = i + 1;
            fillIn = f.open[entryBar] * (1.0 + slip);
            peak = f.close[entryBar];
            ret[entryBar] += w * (f.close[entryBar] / fillIn - 1.0) - w * fee;
            posArr[entryBar] = w;
            lastCounted = entryBar;
            i += 1;
            continue;
          }
        }
        i += 1;
        continue;
      }
      if (i > entryBar) {
        ret[i] += w * (f.close[i] / f.close[i - 1] - 1.0);
        lastCounted = i;
      }
      posArr[i] = w;
      if (f.close[i] > peak) peak = f.close[i];

      let exitReason = null;
      const s = score[i];
      if (Number.isFinite(s) && s < p.theta_out) exitReason = "score_fade";
      else if (f.close[i] < peak - p.trail_atr * f.atr[i]) exitReason = "trail_stop";
      else if (i - entryBar + 1 >= p.max_hold_bars) exitReason = "time_stop";

      if (exitReason !== null) {
        const exitBar = i + 1;
        const fillOut = f.open[exitBar] * (1.0 - slip);
        ret[exitBar] += w * (fillOut / f.close[i] - 1.0) - w * fee;
        closeTrade(exitBar, fillOut, exitReason, exitBar - entryBar);
        w = 0;
        i += 1;
        continue;
      }
      i += 1;
    }
    if (w > 0) {
      const exitBar = n - 1;
      const fillOut = f.close[exitBar] * (1.0 - slip);
      if (lastCounted === exitBar) ret[exitBar] -= w * (fee + slip);
      else ret[exitBar] += w * (fillOut / f.close[exitBar - 1] - 1.0) - w * fee;
      closeTrade(exitBar, fillOut, "end_of_data", exitBar - entryBar + 1);
    }
    return { trades, bar_returns: ret, position: posArr,
             metrics: computeMetrics(trades, ret, posArr, f.close, barMinutes) };
  }

  // ---------------------------------------------------------------- metrics
  function computeMetrics(trades, ret, posArr, close, barMinutes) {
    const bpy = barsPerYear(barMinutes), n = ret.length;
    const equity = new Float64Array(n);
    let acc = 1.0;
    for (let i = 0; i < n; i++) { acc *= 1.0 + ret[i]; equity[i] = acc; }
    let peakEq = -Infinity, maxDd = 0;
    for (let i = 0; i < n; i++) {
      if (equity[i] > peakEq) peakEq = equity[i];
      const dd = equity[i] / peakEq - 1.0;
      if (dd < maxDd) maxDd = dd;
    }
    let sum = 0, sumsq = 0, dsum = 0, dsq = 0, ndown = 0;
    for (let i = 0; i < n; i++) {
      sum += ret[i]; sumsq += ret[i] * ret[i];
      if (ret[i] < 0) { dsum += ret[i]; dsq += ret[i] * ret[i]; ndown += 1; }
    }
    const mu = sum / n;
    // population std (ddof=0) — matches numpy .std() used by the Python engine
    const sd = Math.sqrt(Math.max(0, (sumsq - (sum * sum) / n) / n));
    let sdDown = 0;
    if (ndown > 1) sdDown = Math.sqrt(Math.max(0, (dsq - (dsum * dsum) / ndown) / ndown));
    const annReturn = equity[n - 1] > 0
      ? Math.exp(Math.log(equity[n - 1]) * bpy / n) - 1.0 : -1.0;
    const exposure = posArr.reduce((c, v) => c + (v > 0 ? 1 : 0), 0) / n;

    const m = {
      total_return: equity[n - 1] - 1.0, ann_return: annReturn,
      ann_vol: sd * Math.sqrt(bpy),
      sharpe: sd > 0 ? mu / sd * Math.sqrt(bpy) : 0,
      sortino: sdDown > 0 ? mu / sdDown * Math.sqrt(bpy) : 0,
      max_drawdown: maxDd,
      calmar: maxDd < 0 ? annReturn / Math.abs(maxDd) : 0,
      exposure, buy_hold: close[n - 1] / close[0] - 1.0, n_bars: n,
      n_trades: trades.length,
    };
    if (!trades.length) return m;
    const nets = trades.map(t => t.net_pct), wnets = trades.map(t => t.weighted_net);
    const grossW = trades.map(t => t.gross_pct * t.weight);
    const wins = wnets.filter(x => x > 0), losses = wnets.filter(x => x <= 0);
    const mean = a => a.reduce((s, x) => s + x, 0) / a.length;
    const grossLoss = Math.abs(losses.reduce((s, x) => s + x, 0));
    Object.assign(m, {
      win_rate: wins.length / wnets.length,
      expectancy: mean(nets),
      expectancy_weighted: mean(wnets),
      expectancy_gross: mean(grossW),
      cost_drag: mean(grossW) - mean(wnets),
      profit_factor: grossLoss > 0 ? wins.reduce((s, x) => s + x, 0) / grossLoss : Infinity,
      turnover: 2.0 * trades.reduce((s, t) => s + t.weight, 0),
      avg_bars_held: mean(trades.map(t => t.bars_held)),
      avg_weight: mean(trades.map(t => t.weight)),
    });
    return m;
  }

  return { defaultParams, defaultCosts, barsPerYear, aggregate, addAll,
           computeScore, positionWeight, run, computeMetrics };
});
