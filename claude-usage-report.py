#!/usr/bin/env python3
"""
Local Claude Code token usage report.
Reads ccusage JSON output, renders a zero-dependency static HTML dashboard.
No network uploads. No third-party Python libraries.

Usage:
    python3 ~/.claude/scripts/usage-report.py                  # 生成一次
    python3 ~/.claude/scripts/usage-report.py --watch          # 每 30s 重新生成（守护模式）
    python3 ~/.claude/scripts/usage-report.py --watch 10       # 每 10s 刷新
    open ~/Desktop/token-usage/index.html
"""

import argparse
import json
import os
import subprocess
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from html import escape
from pathlib import Path

OUTPUT_DIR = Path.home() / "Desktop" / "token-usage"
OUTPUT_FILE = OUTPUT_DIR / "index.html"
PROJECTS_DIR = Path.home() / ".claude" / "projects"

# Hardcoded Anthropic pricing (USD per 1M tokens). Used only for today's
# intra-day estimated-cost line. Daily/weekly/monthly totals come from ccusage
# (authoritative). Update here if Anthropic changes list prices.
PRICING = {
    # model-family-prefix : (input, output, cache_read, cache_write_5m)
    "claude-opus-4":    (15.00, 75.00, 1.50, 18.75),
    "claude-sonnet-4":  ( 3.00, 15.00, 0.30,  3.75),
    "claude-haiku-4":   ( 1.00,  5.00, 0.10,  1.25),
}
UNKNOWN_MODEL_PRICING = (3.00, 15.00, 0.30, 3.75)  # fall back to sonnet


def run_ccusage(mode: str) -> dict:
    try:
        result = subprocess.run(
            ["ccusage", mode, "--json", "--offline"],
            capture_output=True, text=True, check=True, timeout=60,
        )
    except FileNotFoundError:
        sys.exit("ccusage not found. Install with: npm install -g ccusage")
    except subprocess.CalledProcessError as e:
        sys.exit(f"ccusage {mode} failed: {e.stderr[:500]}")
    return json.loads(result.stdout)


def iso_week_key(d: date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def build_aggregates(daily_rows: list[dict]):
    today = date.today()
    this_week = iso_week_key(today)
    this_month = today.strftime("%Y-%m")

    today_usage = {"cost": 0.0, "tokens": 0}
    week_usage = {"cost": 0.0, "tokens": 0}
    month_usage = {"cost": 0.0, "tokens": 0}

    by_week = defaultdict(lambda: {"cost": 0.0, "tokens": 0})
    by_month = defaultdict(lambda: {"cost": 0.0, "tokens": 0})
    by_model = defaultdict(lambda: {"cost": 0.0, "tokens": 0})

    for row in daily_rows:
        try:
            d = date.fromisoformat(row["date"])
        except Exception:
            continue
        cost = float(row.get("totalCost", 0) or 0)
        tokens = int(row.get("totalTokens", 0) or 0)
        wk = iso_week_key(d)
        mo = d.strftime("%Y-%m")
        by_week[wk]["cost"] += cost
        by_week[wk]["tokens"] += tokens
        by_month[mo]["cost"] += cost
        by_month[mo]["tokens"] += tokens
        if row["date"] == today.isoformat():
            today_usage["cost"] += cost
            today_usage["tokens"] += tokens
        if wk == this_week:
            week_usage["cost"] += cost
            week_usage["tokens"] += tokens
        if mo == this_month:
            month_usage["cost"] += cost
            month_usage["tokens"] += tokens
        for mb in row.get("modelBreakdowns", []) or []:
            name = mb.get("modelName", "unknown")
            by_model[name]["cost"] += float(mb.get("cost", 0) or 0)
            by_model[name]["tokens"] += int(
                (mb.get("inputTokens") or 0)
                + (mb.get("outputTokens") or 0)
                + (mb.get("cacheCreationTokens") or 0)
                + (mb.get("cacheReadTokens") or 0)
            )
    return {
        "today": today_usage,
        "week": week_usage,
        "month": month_usage,
        "by_week": dict(by_week),
        "by_month": dict(by_month),
        "by_model": dict(by_model),
    }


def price_for(model: str):
    if not model:
        return UNKNOWN_MODEL_PRICING
    for prefix, p in PRICING.items():
        if model.startswith(prefix):
            return p
    return UNKNOWN_MODEL_PRICING


def estimate_cost(model: str, u: dict) -> float:
    if not u:
        return 0.0
    inp_p, out_p, cr_p, cw_p = price_for(model)
    return (
        (u.get("input_tokens", 0) or 0)               * inp_p / 1_000_000
      + (u.get("output_tokens", 0) or 0)              * out_p / 1_000_000
      + (u.get("cache_read_input_tokens", 0) or 0)    * cr_p  / 1_000_000
      + (u.get("cache_creation_input_tokens", 0) or 0)* cw_p  / 1_000_000
    )


def load_intraday(bucket_min: int = 15) -> dict:
    """Scan today's Claude Code jsonl messages, bucket into N-minute slots.

    Uses local time for bucketing. Returns {buckets: [{t_label, tokens, cost_bucket, cost_cum}, ...], stats}.
    """
    if not PROJECTS_DIR.exists():
        return {"buckets": [], "total_tokens": 0, "total_cost": 0.0, "bucket_min": bucket_min}

    now = datetime.now()
    day_start = datetime(now.year, now.month, now.day)
    today_ymd = now.strftime("%Y-%m-%d")
    cutoff_ts = day_start.timestamp()

    num_buckets = (24 * 60) // bucket_min
    tokens = [0] * num_buckets
    seen = set()  # dedup by message.id

    for jsonl in PROJECTS_DIR.rglob("*.jsonl"):
        try:
            if jsonl.stat().st_mtime < cutoff_ts:
                continue
        except OSError:
            continue
        try:
            with jsonl.open("r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line or '"timestamp"' not in line:
                        continue
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue
                    ts = d.get("timestamp")
                    if not ts or today_ymd not in ts:
                        continue
                    try:
                        dt_utc = datetime.strptime(ts.replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S.%f%z")
                    except Exception:
                        try:
                            dt_utc = datetime.strptime(ts.replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S%z")
                        except Exception:
                            continue
                    dt_local = dt_utc.astimezone()
                    if dt_local.strftime("%Y-%m-%d") != today_ymd:
                        continue
                    if d.get("isSidechain"):
                        continue
                    if d.get("type") != "assistant":
                        continue
                    msg = d.get("message") or {}
                    usage = msg.get("usage") or {}
                    if not usage:
                        continue
                    # dedup by message.id (API-returned stable id). Falls back
                    # to requestId+uuid to be safe.
                    mid = msg.get("id") or f"{d.get('requestId','')}:{d.get('uuid','')}"
                    if mid in seen:
                        continue
                    seen.add(mid)
                    tok = (
                        (usage.get("input_tokens", 0) or 0)
                      + (usage.get("output_tokens", 0) or 0)
                      + (usage.get("cache_read_input_tokens", 0) or 0)
                      + (usage.get("cache_creation_input_tokens", 0) or 0)
                    )
                    idx = (dt_local.hour * 60 + dt_local.minute) // bucket_min
                    if 0 <= idx < num_buckets:
                        tokens[idx] += tok
        except OSError:
            continue

    buckets = []
    cum_tok = 0
    for i in range(num_buckets):
        mm = i * bucket_min
        label = f"{mm//60:02d}:{mm%60:02d}"
        cum_tok += tokens[i]
        buckets.append({
            "t_label": label,
            "minute": mm,
            "tokens": tokens[i],
            "cum_tokens": cum_tok,
        })

    return {
        "buckets": buckets,
        "total_tokens": cum_tok,
        "bucket_min": bucket_min,
        "now_minute": now.hour * 60 + now.minute,
    }


def svg_intraday_chart(intra: dict, width: int = 940, height: int = 320) -> str:
    buckets = intra.get("buckets") or []
    if not buckets or intra.get("total_tokens", 0) == 0:
        return '<div class="empty">今天还没有用量数据。</div>'
    pad_l, pad_r, pad_t, pad_b = 64, 56, 16, 44
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    n = len(buckets)
    xw = plot_w / n
    max_cum = max(b["cum_tokens"] for b in buckets) or 1
    max_tok = max(b["tokens"] for b in buckets) or 1
    now_min = intra.get("now_minute", 24 * 60)

    parts = [f'<svg viewBox="0 0 {width} {height}" class="chart">']
    # gridlines + left-axis labels (cumulative tokens)
    for i in range(5):
        y = pad_t + plot_h * i / 4
        v = max_cum * (1 - i / 4)
        parts.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width-pad_r}" y2="{y:.1f}" stroke="#e5e7eb" stroke-width="1"/>')
        parts.append(f'<text x="{pad_l-6}" y="{y+3:.1f}" text-anchor="end" font-size="10" fill="#4f46e5">{fmt_tokens(int(v))}</text>')
        tv = max_tok * (1 - i / 4)
        parts.append(f'<text x="{width-pad_r+6}" y="{y+3:.1f}" font-size="10" fill="#10b981">{fmt_tokens(int(tv))}</text>')
    # hour tick marks
    for h in range(0, 25, 2):
        idx = h * 60 / intra["bucket_min"]
        x = pad_l + idx * xw
        if x > width - pad_r + 1:
            continue
        parts.append(f'<line x1="{x:.1f}" y1="{pad_t+plot_h}" x2="{x:.1f}" y2="{pad_t+plot_h+4}" stroke="#9ca3af"/>')
        parts.append(f'<text x="{x:.1f}" y="{pad_t+plot_h+18}" text-anchor="middle" font-size="10" fill="#6b7280">{h:02d}:00</text>')

    # per-bucket token bars (right axis, green, background)
    for i, b in enumerate(buckets):
        if b["tokens"] <= 0:
            continue
        x = pad_l + i * xw
        bh = (b["tokens"] / max_tok) * plot_h
        by = pad_t + plot_h - bh
        tip = f'{b["t_label"]} · 本档 {fmt_tokens(b["tokens"])} · 累计 {fmt_tokens(b["cum_tokens"])}'
        parts.append(
            f'<rect x="{x:.1f}" y="{by:.1f}" width="{xw-0.5:.1f}" height="{bh:.1f}" '
            f'fill="#10b981" fill-opacity="0.28" class="hv" data-tip="{escape(tip)}"/>'
        )

    # cumulative tokens line (left axis, indigo, on top) — truncate at now
    pts = []
    for i, b in enumerate(buckets):
        if b["minute"] > now_min:
            break
        x = pad_l + i * xw + xw / 2
        y = pad_t + plot_h - (b["cum_tokens"] / max_cum) * plot_h
        pts.append((x, y, b))
    if pts:
        area = f"M {pts[0][0]:.1f} {pad_t+plot_h:.1f} " + " ".join(f"L {x:.1f} {y:.1f}" for x, y, _ in pts) + f" L {pts[-1][0]:.1f} {pad_t+plot_h:.1f} Z"
        parts.append(f'<path d="{area}" fill="#6366f1" fill-opacity="0.08"/>')
        line = f"M {pts[0][0]:.1f} {pts[0][1]:.1f} " + " ".join(f"L {x:.1f} {y:.1f}" for x, y, _ in pts[1:])
        parts.append(f'<path d="{line}" fill="none" stroke="#6366f1" stroke-width="2"/>')
        lx, ly, lb = pts[-1]
        parts.append(f'<circle cx="{lx:.1f}" cy="{ly:.1f}" r="4" fill="#6366f1"/>')
        parts.append(f'<text x="{lx-6:.1f}" y="{ly-8:.1f}" text-anchor="end" font-size="11" fill="#4f46e5" font-weight="600">{fmt_tokens(lb["cum_tokens"])}</text>')
    # "now" marker
    now_x = pad_l + (now_min / intra["bucket_min"]) * xw
    if pad_l <= now_x <= width - pad_r:
        parts.append(f'<line x1="{now_x:.1f}" y1="{pad_t}" x2="{now_x:.1f}" y2="{pad_t+plot_h}" stroke="#ef4444" stroke-width="1" stroke-dasharray="3,3"/>')
    parts.append(f'<text x="{pad_l-52}" y="{pad_t-4}" font-size="10" fill="#4f46e5" font-weight="600">累计 tokens</text>')
    parts.append(f'<text x="{width-pad_r+6}" y="{pad_t-4}" font-size="10" fill="#10b981" font-weight="600">每 {intra["bucket_min"]}m</text>')
    parts.append("</svg>")
    return "".join(parts)


def fmt_tokens(n: int) -> str:
    if n >= 1_000_000_000:
        return f"{n/1_000_000_000:.2f}B"
    if n >= 1_000_000:
        return f"{n/1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)


def _tip_attr(label: str, value_str: str) -> str:
    return f'data-tip="{escape(label)}: {escape(value_str)}"'


def svg_bar_chart(pairs, width=820, height=240, bar_color="#6366f1", value_fmt=lambda v: f"${v:.2f}"):
    if not pairs:
        return '<div class="empty">无数据</div>'
    pad_l, pad_r, pad_t, pad_b = 48, 16, 16, 44
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    max_v = max((v for _, v in pairs), default=0) or 1
    n = len(pairs)
    bw = plot_w / n * 0.72
    gap = plot_w / n * 0.28
    parts = [f'<svg viewBox="0 0 {width} {height}" class="chart">']
    # y-axis gridlines
    for i in range(4):
        y = pad_t + plot_h * i / 3
        v = max_v * (1 - i / 3)
        parts.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width-pad_r}" y2="{y:.1f}" stroke="#e5e7eb" stroke-width="1"/>')
        parts.append(f'<text x="{pad_l-6}" y="{y+3:.1f}" text-anchor="end" font-size="10" fill="#6b7280">{value_fmt(v)}</text>')
    for idx, (label, v) in enumerate(pairs):
        x = pad_l + idx * (bw + gap) + gap / 2
        h = (v / max_v) * plot_h if max_v else 0
        y = pad_t + plot_h - h
        parts.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{bw:.1f}" height="{h:.1f}" '
            f'fill="{bar_color}" rx="2" class="hv" {_tip_attr(label, value_fmt(v))}/>'
        )
        # rotated label
        lx = x + bw / 2
        ly = height - pad_b + 12
        parts.append(
            f'<text x="{lx:.1f}" y="{ly:.1f}" text-anchor="end" font-size="10" '
            f'fill="#6b7280" transform="rotate(-45 {lx:.1f} {ly:.1f})">{escape(label)}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


def svg_hbar(pairs, width=820, row_h=26, bar_color="#10b981", value_fmt=lambda v: f"${v:.2f}"):
    """Horizontal bars with right-aligned value label. Label stays inside the
    bar when the bar is wide enough, flips to outside for short bars. Bar is
    scaled to full plot width so nothing overflows the SVG regardless of the
    absolute magnitude of `v`."""
    if not pairs:
        return '<div class="empty">无数据</div>'
    pad_l, pad_r = 260, 8
    height = row_h * len(pairs) + 12
    plot_w = width - pad_l - pad_r
    max_v = max((v for _, v in pairs), default=0) or 1
    parts = [f'<svg viewBox="0 0 {width} {height}" class="chart">']
    for i, (label, v) in enumerate(pairs):
        y = 6 + i * row_h
        cy = y + row_h / 2 + 3
        bw = (v / max_v) * plot_w
        txt = value_fmt(v)
        # ~7 px per char as a safe estimate for 11px sans-serif
        est_text_w = len(txt) * 7 + 8
        parts.append(f'<text x="{pad_l-8}" y="{cy:.1f}" text-anchor="end" font-size="12" fill="#374151">{escape(label)}</text>')
        parts.append(f'<rect x="{pad_l}" y="{y+4:.1f}" width="{bw:.1f}" height="{row_h-8}" fill="{bar_color}" rx="2" class="hv" {_tip_attr(label, txt)}/>')
        if bw >= est_text_w:
            parts.append(f'<text x="{pad_l+bw-4:.1f}" y="{cy:.1f}" text-anchor="end" font-size="11" fill="#ffffff" font-weight="600">{txt}</text>')
        else:
            parts.append(f'<text x="{pad_l+bw+6:.1f}" y="{cy:.1f}" font-size="11" fill="#374151">{txt}</text>')
    parts.append("</svg>")
    return "".join(parts)


def render_html(daily: dict, agg: dict, intra: dict, refresh_seconds: int = 0) -> str:
    totals = daily.get("totals", {}) or {}
    daily_rows = sorted(daily.get("daily", []) or [], key=lambda r: r["date"])
    last_n = daily_rows[-60:]

    # intraday chart (today)
    intraday_svg = svg_intraday_chart(intra)

    # trend (daily cost)
    trend_pairs = [(r["date"][5:], float(r.get("totalCost", 0) or 0)) for r in last_n]
    trend_svg = svg_bar_chart(trend_pairs, bar_color="#6366f1", value_fmt=lambda v: f"${v:.2f}")

    # weekly cost (last 16 weeks)
    weeks = sorted(agg["by_week"].items())[-16:]
    week_pairs = [(wk, v["cost"]) for wk, v in weeks]
    week_svg = svg_bar_chart(week_pairs, bar_color="#0ea5e9", value_fmt=lambda v: f"${v:.2f}")

    # monthly cost
    months = sorted(agg["by_month"].items())
    month_pairs = [(mo, v["cost"]) for mo, v in months]
    month_svg = svg_bar_chart(month_pairs, bar_color="#8b5cf6", value_fmt=lambda v: f"${v:.2f}")

    # model breakdown (by cost, top 10)
    model_items = sorted(agg["by_model"].items(), key=lambda kv: kv[1]["cost"], reverse=True)[:10]
    model_pairs = [(m, v["cost"]) for m, v in model_items]
    model_svg = svg_hbar(model_pairs, bar_color="#10b981", value_fmt=lambda v: f"${v:.2f}")

    # detail table rows (all days, newest first)
    rows_html = []
    for r in reversed(daily_rows):
        models = ", ".join(sorted(set(r.get("modelsUsed", []) or []))) or "-"
        rows_html.append(
            "<tr>"
            f"<td>{escape(r['date'])}</td>"
            f"<td class='muted'>{escape(models)}</td>"
            f"<td class='num'>{fmt_tokens(int(r.get('inputTokens',0) or 0))}</td>"
            f"<td class='num'>{fmt_tokens(int(r.get('outputTokens',0) or 0))}</td>"
            f"<td class='num'>{fmt_tokens(int(r.get('cacheCreationTokens',0) or 0))}</td>"
            f"<td class='num'>{fmt_tokens(int(r.get('cacheReadTokens',0) or 0))}</td>"
            f"<td class='num'>{fmt_tokens(int(r.get('totalTokens',0) or 0))}</td>"
            f"<td class='num cost'>${float(r.get('totalCost',0) or 0):.2f}</td>"
            "</tr>"
        )

    generated = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    total_cost = float(totals.get("totalCost", 0) or 0)
    total_tokens = int(totals.get("totalTokens", 0) or 0)
    day_count = len(daily_rows)

    return f"""<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
{('<meta http-equiv="refresh" content="' + str(refresh_seconds) + '">') if refresh_seconds else ''}
<title>Claude Code Token 用量</title>
<style>
  :root {{ --fg:#111827; --muted:#6b7280; --bg:#f9fafb; --card:#fff; --border:#e5e7eb; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; font:14px/1.5 -apple-system,BlinkMacSystemFont,"Helvetica Neue",Arial,sans-serif; color:var(--fg); background:var(--bg); }}
  header {{ padding:24px 32px; background:linear-gradient(135deg,#6366f1,#8b5cf6); color:#fff; }}
  header h1 {{ margin:0; font-size:22px; font-weight:600; }}
  header .sub {{ opacity:.85; font-size:13px; margin-top:4px; }}
  main {{ padding:24px 32px; max-width:1280px; margin:0 auto; }}
  .cards {{ display:grid; grid-template-columns:repeat(4,1fr); gap:16px; margin-bottom:24px; }}
  .card {{ background:var(--card); border:1px solid var(--border); border-radius:10px; padding:18px 20px; }}
  .card .label {{ color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.5px; }}
  .card .value {{ font-size:26px; font-weight:600; margin-top:6px; }}
  .card .sub {{ color:var(--muted); font-size:12px; margin-top:4px; }}
  .panel {{ background:var(--card); border:1px solid var(--border); border-radius:10px; padding:20px; margin-bottom:20px; }}
  .panel h2 {{ margin:0 0 12px; font-size:15px; font-weight:600; color:var(--fg); }}
  .chart {{ width:100%; height:auto; }}
  table {{ width:100%; border-collapse:collapse; font-size:13px; }}
  th,td {{ padding:8px 10px; text-align:left; border-bottom:1px solid var(--border); }}
  th {{ background:#f3f4f6; font-weight:600; color:var(--muted); text-transform:uppercase; font-size:11px; letter-spacing:.5px; cursor:pointer; user-select:none; }}
  th:hover {{ background:#e5e7eb; }}
  td.num {{ text-align:right; font-variant-numeric:tabular-nums; }}
  td.cost {{ font-weight:600; color:#4f46e5; }}
  td.muted {{ color:var(--muted); font-size:12px; }}
  footer {{ padding:16px 32px; color:var(--muted); font-size:12px; text-align:center; }}
  .empty {{ padding:40px; text-align:center; color:var(--muted); }}
  .hv {{ cursor:default; transition:filter .1s; }}
  .hv:hover {{ filter:brightness(1.15); }}
  #tip {{ position:fixed; display:none; background:rgba(17,24,39,.96); color:#fff;
         padding:6px 10px; border-radius:6px; font-size:12px; pointer-events:none;
         z-index:100; white-space:nowrap; box-shadow:0 4px 14px rgba(0,0,0,.25); }}
</style>
</head>
<body>
<div id="tip"></div>
<header>
  <h1>Claude Code Token 用量面板</h1>
  <div class="sub">纯本地 · 无上传 · 数据源 ccusage + ~/.claude/projects · 生成于 {generated}{(' · 每 ' + str(refresh_seconds) + 's 自动刷新') if refresh_seconds else ''}</div>
</header>
<main>
  <div class="cards">
    <div class="card"><div class="label">今日</div><div class="value">${agg['today']['cost']:.2f}</div><div class="sub">{fmt_tokens(agg['today']['tokens'])} tokens</div></div>
    <div class="card"><div class="label">本周</div><div class="value">${agg['week']['cost']:.2f}</div><div class="sub">{fmt_tokens(agg['week']['tokens'])} tokens</div></div>
    <div class="card"><div class="label">本月</div><div class="value">${agg['month']['cost']:.2f}</div><div class="sub">{fmt_tokens(agg['month']['tokens'])} tokens</div></div>
    <div class="card"><div class="label">累计 ({day_count} 天)</div><div class="value">${total_cost:.2f}</div><div class="sub">{fmt_tokens(total_tokens)} tokens</div></div>
  </div>

  <div class="panel"><h2>今日实时 Token 走势（{intra['bucket_min']} 分钟一档 · 紫线=累计 tokens · 绿柱=每档 tokens · 红虚线=当前时刻 · 今日合计 {fmt_tokens(intra['total_tokens'])}）</h2>{intraday_svg}</div>
  <div class="panel"><h2>每日费用（最近 {len(last_n)} 天）</h2>{trend_svg}</div>
  <div class="panel"><h2>每周费用（最近 {len(week_pairs)} 周）</h2>{week_svg}</div>
  <div class="panel"><h2>每月费用</h2>{month_svg}</div>
  <div class="panel"><h2>按模型费用 Top 10</h2>{model_svg}</div>

  <div class="panel">
    <h2>明细</h2>
    <table id="detail">
      <thead><tr>
        <th data-k="str">日期</th><th>模型</th>
        <th data-k="num" class="num">Input</th>
        <th data-k="num" class="num">Output</th>
        <th data-k="num" class="num">Cache Create</th>
        <th data-k="num" class="num">Cache Read</th>
        <th data-k="num" class="num">Total Tokens</th>
        <th data-k="num" class="num">Cost</th>
      </tr></thead>
      <tbody>
        {"".join(rows_html) or '<tr><td colspan="8" class="empty">无数据</td></tr>'}
      </tbody>
    </table>
  </div>
</main>
<footer>ccusage --offline · Python stdlib only · 断网也能跑</footer>
<script>
  // chart hover tooltip (shared across all charts)
  (function() {{
    const tip = document.getElementById('tip');
    document.addEventListener('mouseover', e => {{
      const t = e.target.closest('[data-tip]');
      if (!t) return;
      tip.textContent = t.getAttribute('data-tip');
      tip.style.display = 'block';
    }});
    document.addEventListener('mousemove', e => {{
      if (tip.style.display !== 'block') return;
      const pad = 14;
      let x = e.clientX + pad, y = e.clientY + pad;
      const r = tip.getBoundingClientRect();
      if (x + r.width > window.innerWidth - 8) x = e.clientX - r.width - pad;
      if (y + r.height > window.innerHeight - 8) y = e.clientY - r.height - pad;
      tip.style.left = x + 'px';
      tip.style.top = y + 'px';
    }});
    document.addEventListener('mouseout', e => {{
      const t = e.target.closest('[data-tip]');
      if (!t) return;
      if (e.relatedTarget && e.relatedTarget.closest && e.relatedTarget.closest('[data-tip]')) return;
      tip.style.display = 'none';
    }});
  }})();

  // minimal column sort
  const table = document.getElementById('detail');
  table.querySelectorAll('th[data-k]').forEach((th, idx) => {{
    let asc = true;
    th.addEventListener('click', () => {{
      const tbody = table.tBodies[0];
      const rows = Array.from(tbody.rows);
      const kind = th.dataset.k;
      rows.sort((a, b) => {{
        const av = a.cells[idx].textContent.trim();
        const bv = b.cells[idx].textContent.trim();
        if (kind === 'num') {{
          const parse = s => parseFloat(s.replace(/[^\\d.-]/g,'')) || 0;
          return (parse(av) - parse(bv)) * (asc ? 1 : -1);
        }}
        return av.localeCompare(bv) * (asc ? 1 : -1);
      }});
      asc = !asc;
      rows.forEach(r => tbody.appendChild(r));
    }});
  }});
</script>
</body>
</html>
"""


def generate_once(refresh_seconds: int):
    daily = run_ccusage("daily")
    agg = build_aggregates(daily.get("daily", []) or [])
    intra = load_intraday(bucket_min=15)
    html = render_html(daily, agg, intra, refresh_seconds=refresh_seconds)
    OUTPUT_FILE.write_text(html, encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--watch", nargs="?", const=30, type=int, default=None,
                        help="守护模式，每 N 秒（默认 30）重新生成 HTML，页面带 meta refresh")
    parser.add_argument("--refresh", type=int, default=0,
                        help="一次性模式下也在 HTML 里注入 meta refresh（用于 launchd/cron 外部调度）")
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    if args.watch is None:
        generate_once(refresh_seconds=max(0, args.refresh))
        print(f"Wrote {OUTPUT_FILE}")
        print(f"Open with: open {OUTPUT_FILE}")
        return

    interval = max(5, int(args.watch))
    print(f"Watching · 每 {interval}s 重新生成 · Ctrl+C 退出")
    print(f"Open with: open {OUTPUT_FILE}")
    try:
        while True:
            t0 = time.time()
            try:
                generate_once(refresh_seconds=interval)
                print(f"  [{datetime.now().strftime('%H:%M:%S')}] refreshed ({time.time()-t0:.1f}s)")
            except SystemExit as e:
                print(f"  [{datetime.now().strftime('%H:%M:%S')}] error: {e}")
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nstopped.")


if __name__ == "__main__":
    main()
