"""
CJA Dimension Analysis Script v1.0
====================================

Analyzes CJA dimension data collected by the cja-dimension-analysis skill.
Performs cardinality classification, skew/distribution analysis, trend detection,
anomaly detection (z-score), data quality scoring, comparisons, and forecasting.
Generates both JSON results and comprehensive reports (markdown or HTML).

This script is called by the cja-dimension-analysis skill after data collection phases.

Usage:
    python3 cja_dimension_analysis.py <analysis_json> <data_view_name> <data_view_id>         [output_dir] [--format=markdown|html] [--keep-analyses=N]

Arguments:
    analysis_json    - Path to JSON file with dimension data collected by SKILL.md workflow
    data_view_name   - Name of the data view being analyzed
    data_view_id     - ID of the data view being analyzed
    output_dir       - (Optional) Directory for output files (default: current directory)
    --format=FORMAT  - (Optional) Output format: 'markdown' or 'html' (default: html)
    --keep-analyses=N - (Optional) Keep N recent analyses, delete older ones (default: 0 = keep all)

Output Files:
    - dimension_analysis_results_YYYY-MM-DD_HH-MM.json: Raw analysis data
    - dimension_analysis_report_YYYY-MM-DD_HH-MM.md or .html: Comprehensive report

Expected input JSON structure (collected by SKILL.md workflow):
{
  "analysis_metadata": {
    "data_view_name": "...",
    "data_view_id": "...",
    "date_range": "...",
    "analyses_run": ["cardinality", "distribution", "trends", "anomalies", "errors"],
    "generated_at": "ISO8601"
  },
  "dimensions": [
    {
      "id": "variables/page",
      "name": "Page",
      "cardinality": {"uniqueValueCount": 1500, "level": "HIGH"},
      "distribution": {"gini": 0.72, "skewLabel": "High skew", "topValues": [...]},
      "trends": {"period1": "...", "period2": "...", "changes": [...], "newValues": [], "disappearedValues": []},
      "anomalies": [...],
      "errors": {"errorPatterns": [...], "missingDataPct": 3.2}
    }
  ],
  "comparisons": [...],
  "summary": {}
}

Version: 1.0.0
Last Updated: March 13, 2026
"""
import json
import sys
import os
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from collections import defaultdict

def esc(s):
    """HTML-escape a value to prevent XSS in report output."""
    return str(s).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;') if s is not None else ''
CARDINALITY_LEVELS = [('VERY_HIGH', 10000), ('HIGH', 1000), ('MEDIUM', 100), ('LOW', 0)]
CARDINALITY_BADGES = {'LOW': ('🟢', 'Low', '< 100 unique values — fast queries'), 'MEDIUM': ('🟡', 'Medium', '100–1,000 unique values'), 'HIGH': ('🟠', 'High', '1,000–10,000 unique values — monitor query performance'), 'VERY_HIGH': ('🔴', 'Very High', '> 10,000 unique values — can slow queries')}

def classify_cardinality(count: int) -> str:
    for level, threshold in CARDINALITY_LEVELS:
        if count > threshold:
            return level
    return 'LOW'

def compute_gini(values: list[float]) -> float:
    """Compute Gini coefficient for a list of non-negative values."""
    if not values or sum(values) == 0:
        return 0.0
    sorted_vals = sorted(values)
    n = len(sorted_vals)
    cumulative = 0.0
    for i, v in enumerate(sorted_vals):
        cumulative += (2 * (i + 1) - n - 1) * v
    total = sum(sorted_vals)
    return cumulative / (n * total) if total else 0.0

def classify_skew(top1_pct: float, top5_pct: float, top10_pct: float) -> str:
    if top1_pct >= 50:
        return 'Extreme skew'
    if top1_pct >= 30:
        return 'High skew'
    if top10_pct < 50:
        return 'Long tail'
    return 'Moderate'

def enrich_distribution(dim_data: dict) -> dict:
    """Compute derived distribution metrics from raw topValues list."""
    dist = dim_data.get('distribution', {})
    top_values = dist.get('topValues', [])
    if not top_values:
        return dim_data
    metrics = [float(v.get('metric', 0)) for v in top_values]
    total = sum(metrics)
    if total == 0:
        return dim_data
    for i, v in enumerate(top_values):
        v['pct'] = round(metrics[i] / total * 100, 2)
        v['cumulative'] = round(sum(metrics[:i + 1]) / total * 100, 2)
    top1_pct = metrics[0] / total * 100 if metrics else 0
    top5_pct = sum(metrics[:5]) / total * 100 if len(metrics) >= 5 else sum(metrics) / total * 100
    top10_pct = sum(metrics[:10]) / total * 100 if len(metrics) >= 10 else sum(metrics) / total * 100
    dist['top1Pct'] = round(top1_pct, 2)
    dist['top5Pct'] = round(top5_pct, 2)
    dist['top10Pct'] = round(top10_pct, 2)
    dist['gini'] = round(compute_gini(metrics), 3)
    dist['skewLabel'] = classify_skew(top1_pct, top5_pct, top10_pct)
    dim_data['distribution'] = dist
    return dim_data

def detect_anomalies_zscore(time_series: list[dict], threshold: float=2.0) -> list[dict]:
    """Detect anomalies in a time series using z-score method."""
    if len(time_series) < 3:
        return []
    values = [float(p.get('metric', 0)) for p in time_series]
    mean = sum(values) / len(values)
    variance = sum(((v - mean) ** 2 for v in values)) / len(values)
    std = math.sqrt(variance) if variance > 0 else 0
    anomalies = []
    for i, (point, val) in enumerate(zip(time_series, values)):
        if std == 0:
            continue
        z = abs(val - mean) / std
        if z > threshold:
            atype = 'spike' if val > mean else 'drop'
            anomalies.append({'date': point.get('date', f'period_{i}'), 'value': val, 'mean': round(mean, 2), 'zScore': round(z, 2), 'type': atype})
    return anomalies

def linear_regression(x: list[float], y: list[float]) -> tuple[float, float, float]:
    """Simple linear regression. Returns (slope, intercept, r_squared)."""
    n = len(x)
    if n < 2:
        return (0.0, 0.0, 0.0)
    sum_x = sum(x)
    sum_y = sum(y)
    sum_xy = sum((xi * yi for xi, yi in zip(x, y)))
    sum_x2 = sum((xi ** 2 for xi in x))
    denom = n * sum_x2 - sum_x ** 2
    if denom == 0:
        return (0.0, sum_y / n, 0.0)
    slope = (n * sum_xy - sum_x * sum_y) / denom
    intercept = (sum_y - slope * sum_x) / n
    y_mean = sum_y / n
    ss_tot = sum(((yi - y_mean) ** 2 for yi in y))
    ss_res = sum(((yi - (slope * xi + intercept)) ** 2 for xi, yi in zip(x, y)))
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0
    return (round(slope, 4), round(intercept, 4), round(max(0, min(1, r2)), 4))

def classify_forecast(slope: float, r2: float, mean: float, threshold: float=0.7) -> dict:
    """Classify forecast trend and confidence."""
    if r2 >= 0.7:
        confidence = 'High'
    elif r2 >= 0.4:
        confidence = 'Medium'
    else:
        confidence = 'Low'
    if mean == 0:
        direction = 'Flat'
    elif slope > mean * 0.05:
        direction = 'Upward'
    elif slope < -mean * 0.05:
        direction = 'Downward'
    else:
        direction = 'Flat'
    return {'direction': direction, 'confidence': confidence}

def score_data_quality(error_data: dict) -> tuple[str, str]:
    """Return (severity, label) based on missing data percentage."""
    pct = error_data.get('missingDataPct', 0)
    if pct >= 20:
        return ('critical', f'⛔ Critical ({pct:.1f}% missing)')
    elif pct >= 5:
        return ('warning', f'⚠️ Warning ({pct:.1f}% missing)')
    else:
        return ('ok', f'✅ OK ({pct:.1f}% missing)')

def analyze(raw: dict) -> dict:
    """Run all analysis enrichments on the raw input data."""
    metadata = raw.get('analysis_metadata', {})
    dimensions = raw.get('dimensions', [])
    enriched_dims = []
    summary = {'totalDimensions': len(dimensions), 'cardinalityBreakdown': defaultdict(int), 'skewBreakdown': defaultdict(int), 'totalAnomalies': 0, 'criticalDataQualityCount': 0, 'warningDataQualityCount': 0, 'highCardinalityCount': 0}
    for dim in dimensions:
        card = dim.get('cardinality', {})
        count = card.get('uniqueValueCount', 0)
        card['level'] = classify_cardinality(count)
        card['badge'] = CARDINALITY_BADGES.get(card['level'], ('⚪', 'Unknown', ''))
        dim['cardinality'] = card
        level = card['level']
        summary['cardinalityBreakdown'][level] += 1
        if level in ('HIGH', 'VERY_HIGH'):
            summary['highCardinalityCount'] += 1
        dim = enrich_distribution(dim)
        skew_label = dim.get('distribution', {}).get('skewLabel', 'Unknown')
        summary['skewBreakdown'][skew_label] += 1
        anomalies = dim.get('anomalies', [])
        summary['totalAnomalies'] += len(anomalies)
        errors = dim.get('errors', {})
        severity, label = score_data_quality(errors)
        errors['severity'] = severity
        errors['severityLabel'] = label
        dim['errors'] = errors
        if severity == 'critical':
            summary['criticalDataQualityCount'] += 1
        elif severity == 'warning':
            summary['warningDataQualityCount'] += 1
        enriched_dims.append(dim)
    summary['cardinalityBreakdown'] = dict(summary['cardinalityBreakdown'])
    summary['skewBreakdown'] = dict(summary['skewBreakdown'])
    return {'analysis_metadata': metadata, 'dimensions': enriched_dims, 'comparisons': raw.get('comparisons', []), 'summary': summary}

def generate_markdown(data: dict, data_view_name: str) -> str:
    now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    meta = data.get('analysis_metadata', {})
    dims = data.get('dimensions', [])
    summary = data.get('summary', {})
    lines = [f'# Dimension Analysis Report', f'**Data View**: {data_view_name}', f'**Date Range**: {meta.get('date_range', 'N/A')}', f'**Generated**: {now}', f'**Dimensions Analyzed**: {summary.get('totalDimensions', 0)}', '', '---', '', '## Summary', '', f'| Metric | Value |', f'|--------|-------|', f'| Total Dimensions | {summary.get('totalDimensions', 0)} |', f'| High / Very High Cardinality | {summary.get('highCardinalityCount', 0)} |', f'| Total Anomalies Detected | {summary.get('totalAnomalies', 0)} |', f'| Critical Data Quality Issues | {summary.get('criticalDataQualityCount', 0)} |', f'| Warning Data Quality Issues | {summary.get('warningDataQualityCount', 0)} |', '', '### Cardinality Breakdown', '']
    card_bd = summary.get('cardinalityBreakdown', {})
    for level in ['LOW', 'MEDIUM', 'HIGH', 'VERY_HIGH']:
        badge_info = CARDINALITY_BADGES.get(level, ('⚪', level, ''))
        lines.append(f'- {badge_info[0]} **{badge_info[1]}**: {card_bd.get(level, 0)} dimensions — {badge_info[2]}')
    lines.append('')
    for dim in dims:
        name = dim.get('name', dim.get('id', 'Unknown'))
        dim_id = dim.get('id', '')
        lines += [f'---', f'', f'## {name}', f'**ID**: `{dim_id}`', '']
        card = dim.get('cardinality', {})
        badge = card.get('badge', ('⚪', 'Unknown', ''))
        lines += [f'### Cardinality', f'- **Unique Values**: {card.get('uniqueValueCount', 'N/A'):,}', f'- **Level**: {badge[0]} {badge[1]} — {badge[2]}', '']
        dist = dim.get('distribution', {})
        if dist:
            lines += [f'### Distribution & Skew', f'- **Skew**: {dist.get('skewLabel', 'N/A')}', f'- **Gini Coefficient**: {dist.get('gini', 'N/A')}', f'- **Top 1 value share**: {dist.get('top1Pct', 0):.1f}%', f'- **Top 5 value share**: {dist.get('top5Pct', 0):.1f}%', f'- **Top 10 value share**: {dist.get('top10Pct', 0):.1f}%', '']
            top_values = dist.get('topValues', [])[:10]
            if top_values:
                lines += ['| Rank | Value | Metric | % of Total | Cumulative % |', '|------|-------|--------|------------|--------------|']
                for i, v in enumerate(top_values):
                    lines.append(f'| {i + 1} | {v.get('value', 'N/A')} | {v.get('metric', 0):,.0f} | {v.get('pct', 0):.1f}% | {v.get('cumulative', 0):.1f}% |')
                lines.append('')
        trends = dim.get('trends', {})
        if trends:
            new_vals = trends.get('newValues', [])
            gone_vals = trends.get('disappearedValues', [])
            changes = trends.get('changes', [])
            lines += [f'### Trends', f'- **Period 1**: {trends.get('period1', 'N/A')}', f'- **Period 2**: {trends.get('period2', 'N/A')}', f'- **New values**: {len(new_vals)}', f'- **Disappeared values**: {len(gone_vals)}', '']
            growing = [c for c in changes if c.get('badge') == '🟢 Growing']
            declining = [c for c in changes if c.get('badge') == '🔴 Declining']
            if growing:
                lines.append(f'**Top Growing Values**: {', '.join((c.get('value', '') for c in growing[:5]))}')
            if declining:
                lines.append(f'**Top Declining Values**: {', '.join((c.get('value', '') for c in declining[:5]))}')
            lines.append('')
        anomalies = dim.get('anomalies', [])
        if anomalies:
            lines += [f'### Anomalies ({len(anomalies)} detected)', '', '| Type | Value/Date | Z-Score | Magnitude |', '|------|-----------|---------|-----------|']
            for a in anomalies[:10]:
                lines.append(f'| {a.get('type', 'N/A')} | {a.get('date', a.get('value', 'N/A'))} | {a.get('zScore', 'N/A')} | {a.get('value', 'N/A')} |')
            lines.append('')
        errors = dim.get('errors', {})
        if errors:
            lines += [f'### Data Quality', f'- **Status**: {errors.get('severityLabel', 'N/A')}', f'- **Missing data**: {errors.get('missingDataPct', 0):.1f}%']
            patterns = errors.get('errorPatterns', [])
            if patterns:
                lines.append('')
                lines.append('| Error Pattern | Count | % of Total |')
                lines.append('|--------------|-------|------------|')
                for p in patterns:
                    lines.append(f'| `{p.get('pattern', 'N/A')}` | {p.get('count', 0):,} | {p.get('pct', 0):.1f}% |')
            lines.append('')
        forecasts = dim.get('forecasts', [])
        if forecasts:
            lines += [f'### Forecasts', '', '| Value | Direction | Confidence | Trend |', '|-------|-----------|------------|-------|']
            for f in forecasts[:5]:
                lines.append(f'| {f.get('value', 'N/A')} | {f.get('direction', 'N/A')} | {f.get('confidence', 'N/A')} | slope={f.get('slope', 0):.4f} |')
            lines.append('')
    lines += ['---', '', '## Recommendations', '']
    recs = []
    for dim in dims:
        name = dim.get('name', dim.get('id', 'Unknown'))
        card = dim.get('cardinality', {})
        if card.get('level') in ('HIGH', 'VERY_HIGH'):
            recs.append(f'🟠 **{name}** — High cardinality ({card.get('uniqueValueCount', 0):,} values). Consider whether all values are necessary; high cardinality can slow queries.')
        errors = dim.get('errors', {})
        if errors.get('severity') == 'critical':
            recs.append(f'⛔ **{name}** — Critical data quality ({errors.get('missingDataPct', 0):.1f}% missing). Review data collection for this dimension.')
        elif errors.get('severity') == 'warning':
            recs.append(f'⚠️ **{name}** — Data quality warning ({errors.get('missingDataPct', 0):.1f}% missing).')
        if len(dim.get('anomalies', [])) > 0:
            recs.append(f'🔍 **{name}** — {len(dim.get('anomalies', []))} anomalies detected. Review for data spikes or drops.')
    if recs:
        for r in recs:
            lines.append(f'- {r}')
    else:
        lines.append('No critical recommendations — dimensions look healthy.')
    lines += ['', '---', '', '*Generated by cja-dimension-analysis skill*']
    return '\n'.join(lines)

def generate_html(data: dict, data_view_name: str, data_view_id: str) -> str:
    now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    meta = data.get('analysis_metadata', {})
    dims = data.get('dimensions', [])
    summary = data.get('summary', {})
    dims_json = json.dumps(dims, default=str)
    summary_json = json.dumps(summary, default=str)
    card_bd = summary.get('cardinalityBreakdown', {})
    card_labels = json.dumps(list(card_bd.keys()))
    card_values = json.dumps(list(card_bd.values()))
    skew_bd = summary.get('skewBreakdown', {})
    skew_labels = json.dumps(list(skew_bd.keys()))
    skew_values = json.dumps(list(skew_bd.values()))
    nav_items = '<a href="#summary" class="active">Summary</a>' + ''.join((f'<a href="#dim-{i}">{esc(d.get('name', d.get('id', '')))}</a>' for i, d in enumerate(dims)))
    dim_sections = ''
    for i, dim in enumerate(dims):
        name = dim.get('name', dim.get('id', 'Unknown'))
        dim_id = dim.get('id', '')
        card = dim.get('cardinality', {})
        card_count = card.get('uniqueValueCount', 0)
        card_level = card.get('level', 'UNKNOWN')
        badge_info = CARDINALITY_BADGES.get(card_level, ('⚪', card_level, ''))
        card_color = {'LOW': 'var(--accent-green)', 'MEDIUM': 'var(--accent-yellow)', 'HIGH': 'var(--accent-red)', 'VERY_HIGH': 'var(--accent-red)'}.get(card_level, 'var(--ink-muted)')
        dist = dim.get('distribution', {})
        top_values = dist.get('topValues', [])[:10]
        tv_labels = json.dumps([v.get('value', 'N/A') for v in top_values])
        tv_values = json.dumps([v.get('metric', 0) for v in top_values])
        errors = dim.get('errors', {})
        err_severity_color = {'ok': 'var(--accent-green)', 'warning': 'var(--accent-yellow)', 'critical': 'var(--accent-red)'}.get(errors.get('severity', 'ok'), 'var(--ink-muted)')
        anomalies = dim.get('anomalies', [])
        anomaly_rows = ''.join((f'<tr><td>{esc(a.get('type', 'N/A'))}</td><td>{esc(a.get('date', a.get('value', 'N/A')))}</td><td>{esc(a.get('zScore', 'N/A'))}</td></tr>' for a in anomalies[:5]))
        dist_table_rows = ''.join((f'<tr><td>{j + 1}</td><td>{esc(v.get('value', 'N/A'))}</td><td>{v.get('metric', 0):,.0f}</td><td>{v.get('pct', 0):.1f}%</td><td>{v.get('cumulative', 0):.1f}%</td></tr>' for j, v in enumerate(top_values)))
        forecasts = dim.get('forecasts', [])
        forecast_rows = ''.join((f'<tr><td>{esc(f.get('value', 'N/A'))}</td><td>{esc(f.get('direction', 'N/A'))}</td><td>{esc(f.get('confidence', 'N/A'))}</td></tr>' for f in forecasts[:5]))
        dim_sections += f"""\n        <section id="dim-{i}" class="dim-section">\n          <div class="dim-header">\n            <h2>{esc(name)}</h2>\n            <code class="dim-id">{esc(dim_id)}</code>\n          </div>\n          <div class="cards-row">\n            <div class="card">\n              <div class="card-label">Cardinality</div>\n              <div class="card-value" style="color:{card_color}">{badge_info[0]} {badge_info[1]}</div>\n              <div class="card-sub">{card_count:,} unique values</div>\n            </div>\n            <div class="card">\n              <div class="card-label">Skew</div>\n              <div class="card-value">{dist.get('skewLabel', 'N/A')}</div>\n              <div class="card-sub">Gini: {dist.get('gini', 'N/A')}</div>\n            </div>\n            <div class="card">\n              <div class="card-label">Data Quality</div>\n              <div class="card-value" style="color:{err_severity_color}">{errors.get('severityLabel', 'N/A')}</div>\n              <div class="card-sub">{errors.get('missingDataPct', 0):.1f}% missing</div>\n            </div>\n            <div class="card">\n              <div class="card-label">Anomalies</div>\n              <div class="card-value">{len(anomalies)}</div>\n              <div class="card-sub">detected</div>\n            </div>\n          </div>\n\n          <details open class="dim-detail">\n            <summary>Top Values Distribution</summary>\n            <canvas id="chart-{i}" width="600" height="200"></canvas>\n            <script>\n              (function(){{\n                var ctx = document.getElementById('chart-{i}').getContext('2d');\n                new Chart(ctx, {{\n                  type: 'bar',\n                  data: {{\n                    labels: {tv_labels},\n                    datasets: [{{ label: 'Metric', data: {tv_values},\n                      backgroundColor: 'rgba(200,49,47,0.7)', borderColor: 'rgba(200,49,47,1)',\n                      borderWidth: 1 }}]\n                  }},\n                  options: {{ responsive: true, plugins: {{ legend: {{ display: false }} }},\n                    scales: {{ y: {{ beginAtZero: true }} }} }}\n                }});\n              }})();\n            </script>\n            <table class="data-table">\n              <tr><th>#</th><th>Value</th><th>Metric</th><th>% of Total</th><th>Cumulative</th></tr>\n              {dist_table_rows}\n            </table>\n          </details>\n\n          {('<details class="dim-detail"><summary>Anomalies (' + str(len(anomalies)) + ')</summary><table class="data-table"><tr><th>Type</th><th>Date/Value</th><th>Z-Score</th></tr>' + anomaly_rows + '</table></details>' if anomalies else '')}\n\n          {('<details class="dim-detail"><summary>Forecasts (' + str(len(forecasts)) + ' values)</summary><table class="data-table"><tr><th>Value</th><th>Direction</th><th>Confidence</th></tr>' + forecast_rows + '</table></details>' if forecasts else '')}\n        </section>\n        """
    return f"""<!DOCTYPE html>\n<html lang="en">\n<head>\n  <meta charset="UTF-8">\n  <meta name="viewport" content="width=device-width, initial-scale=1.0">\n  <title>Dimension Analysis &mdash; {esc(data_view_name)}</title>\n  <link rel="preconnect" href="https://fonts.googleapis.com">\n  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>\n  <link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700;900&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">\n  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>\n  <style>\n    * {{ box-sizing: border-box; margin: 0; padding: 0; }}\n    :root {{\n      --bg: #f5f4f1;\n      --surface: #ffffff;\n      --ink: #1a1a1a;\n      --ink-muted: #6b6b6b;\n      --border: #e5e2dc;\n      --header-bg: #0e0e10;\n      --header-warm: #3a1010;\n      --accent-red: #c8312f;\n      --accent-red-bright: #ff6b68;\n      --accent-red-soft: #fdecea;\n      --accent-green: #1f7a4d;\n      --accent-yellow: #d4a017;\n      /* legacy aliases preserved for unchanged markup */\n      --card-bg: var(--surface);\n      --text: var(--ink);\n      --text-muted: var(--ink-muted);\n    }}\n    body {{ font-family: "Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;\n           background: var(--bg); color: var(--ink); line-height: 1.5;\n           -webkit-font-smoothing: antialiased; }}\n    a {{ scroll-behavior: smooth; }}\n\n    /* === Header === */\n    header {{\n      background: linear-gradient(120deg, var(--header-bg) 0%, #1a0d0d 55%, var(--header-warm) 100%);\n      color: #fff; padding: 56px 56px 44px; position: relative; overflow: hidden;\n    }}\n    header::after {{ content: ""; position: absolute; right: -140px; top: -140px;\n                    width: 460px; height: 460px;\n                    background: radial-gradient(circle, rgba(200,49,47,.35) 0%, transparent 70%);\n                    pointer-events: none; }}\n    .header-inner {{ max-width: 1080px; margin: 0 auto; position: relative; z-index: 1; }}\n    .eyebrow {{ display: inline-flex; align-items: center; gap: 8px;\n               padding: 6px 14px; border: 1px solid rgba(255,107,104,.55);\n               border-radius: 999px; color: var(--accent-red-bright);\n               font-size: 11px; font-weight: 600; letter-spacing: 1.2px;\n               text-transform: uppercase; margin-bottom: 24px;\n               background: rgba(200,49,47,.10); }}\n    .eyebrow::before {{ content: ""; width: 6px; height: 6px;\n                       background: var(--accent-red-bright); border-radius: 50%; }}\n    header h1 {{ font-family: "Playfair Display", Georgia, serif;\n                font-size: 56px; font-weight: 700; letter-spacing: -1.5px;\n                line-height: 1.05; margin-bottom: 14px; color: #fff; }}\n    header .lede {{ font-size: 16px; max-width: 560px;\n                   color: rgba(255,255,255,.80); margin-bottom: 24px;\n                   line-height: 1.55; }}\n    header .meta {{ display: flex; flex-wrap: wrap; gap: 22px;\n                   font-size: 13px; color: rgba(255,255,255,.60); }}\n    header .meta span {{ display: inline-flex; align-items: center; gap: 6px; }}\n    header .meta .icon {{ opacity: .8; }}\n\n    /* === Sticky nav === */\n    nav {{ position: sticky; top: 0; background: var(--surface);\n           border-bottom: 1px solid var(--border);\n           display: flex; gap: 28px; padding: 0 56px;\n           z-index: 100; flex-wrap: wrap; }}\n    nav a {{ display: block; padding: 16px 0; font-size: 14px;\n             color: var(--ink); text-decoration: none;\n             border-bottom: 2px solid transparent;\n             transition: border-color .15s ease; }}\n    nav a:hover {{ border-bottom-color: var(--accent-red); }}\n    nav a.active {{ border-bottom-color: var(--accent-red); }}\n    @media print {{ nav {{ display: none; position: static; }} }}\n\n    main {{ padding: 36px 56px 60px; max-width: 1080px; margin: 0 auto; }}\n\n    /* === Section label & headings === */\n    .section-label {{ font-size: 11px; font-weight: 700;\n                     text-transform: uppercase; letter-spacing: 1.4px;\n                     color: var(--ink-muted); margin-bottom: 14px;\n                     padding-bottom: 10px; border-bottom: 1px solid var(--border); }}\n    main section > h2 {{ font-family: "Playfair Display", Georgia, serif;\n                        font-size: 24px; font-weight: 700; margin-bottom: 18px;\n                        padding-bottom: 12px; border-bottom: 1px solid var(--border); }}\n\n    /* === Summary KPI tiles === */\n    .summary-cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));\n                     gap: 14px; margin-bottom: 36px; }}\n    .summary-cards .card {{\n      background: var(--surface); border: 0; border-top: 3px solid #b9b6ae;\n      border-radius: 8px; padding: 22px 22px 20px; text-align: left;\n      box-shadow: 0 1px 3px rgba(0,0,0,.05);\n    }}\n    .summary-cards .card-label {{ font-size: 11px; font-weight: 700;\n                                 text-transform: uppercase; letter-spacing: 1px;\n                                 color: var(--ink-muted); margin-bottom: 10px; }}\n    .summary-cards .card-value {{ font-family: "Playfair Display", Georgia, serif;\n                                 font-weight: 700; font-size: 38px;\n                                 line-height: 1; color: var(--ink); margin-bottom: 8px; }}\n    .summary-cards .card-sub {{ font-size: 12px; color: var(--ink-muted); }}\n\n    /* === Drill-down dimension section card === */\n    .dim-section {{ background: var(--surface); border: 1px solid var(--border);\n                   border-radius: 8px; padding: 28px 32px;\n                   margin-bottom: 22px;\n                   box-shadow: 0 1px 3px rgba(0,0,0,.04); }}\n    .dim-header {{ display: flex; align-items: center; gap: 1rem; margin-bottom: 1rem;\n                  padding-bottom: 12px; border-bottom: 1px solid var(--border); }}\n    .dim-header h2 {{ font-family: "Playfair Display", Georgia, serif;\n                     font-size: 22px; font-weight: 700; color: var(--ink); }}\n    .dim-id {{ font-size: 12px; color: var(--ink-muted); background: var(--bg);\n              padding: 4px 10px; border-radius: 4px; font-family: monospace; }}\n\n    /* Inline per-dimension stat cards */\n    .cards-row {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));\n                 gap: 12px; margin: 16px 0 20px; }}\n    .cards-row .card {{\n      background: var(--bg); border: 1px solid var(--border);\n      border-radius: 8px; padding: 14px 16px; text-align: left;\n    }}\n    .cards-row .card-label {{ font-size: 11px; font-weight: 700;\n                             text-transform: uppercase; letter-spacing: .8px;\n                             color: var(--ink-muted); margin-bottom: 8px; }}\n    .cards-row .card-value {{ font-family: "Playfair Display", Georgia, serif;\n                             font-size: 22px; font-weight: 700;\n                             color: var(--ink); line-height: 1.1; }}\n    .cards-row .card-sub {{ font-size: 11px; color: var(--ink-muted); margin-top: 6px; }}\n\n    /* === Collapsible details === */\n    details.dim-detail {{ margin-top: 16px; border: 1px solid var(--border); border-radius: 6px; }}\n    details.dim-detail summary {{\n      padding: 12px 18px; cursor: pointer; font-weight: 600;\n      font-size: 14px; color: var(--ink);\n      background: #faf8f4; border-radius: 6px;\n      list-style: none; user-select: none;\n    }}\n    details.dim-detail summary::-webkit-details-marker {{ display: none; }}\n    details.dim-detail summary::after {{ content: " ▾"; color: var(--ink-muted); float: right; }}\n    details.dim-detail[open] summary {{ border-bottom: 1px solid var(--border);\n                                       border-radius: 6px 6px 0 0; }}\n    details.dim-detail[open] summary::after {{ content: " ▴"; }}\n    details.dim-detail > *:not(summary) {{ padding: 18px; }}\n\n    /* === Data tables === */\n    .data-table {{ width: 100%; border-collapse: collapse; font-size: 13px; margin-top: 12px; }}\n    .data-table th {{\n      background: #faf8f4; padding: 12px 22px;\n      text-align: left; font-weight: 600;\n      text-transform: uppercase; letter-spacing: .6px;\n      font-size: 11px; color: var(--ink-muted);\n      border-bottom: 1px solid var(--border);\n    }}\n    .data-table td {{ padding: 12px 22px; border-bottom: 1px solid #f4f1eb; }}\n    .data-table tr:last-child td {{ border-bottom: none; }}\n    .data-table tr:hover td {{ background: #faf8f4; }}\n\n    /* === Badges === */\n    .badge {{ display: inline-block; padding: 3px 9px; border-radius: 4px;\n             font-size: 11px; font-weight: 600; }}\n    .badge.green  {{ background: #ebf5ef; color: var(--accent-green); }}\n    .badge.red    {{ background: var(--accent-red-soft); color: var(--accent-red); }}\n    .badge.yellow {{ background: #fef6e3; color: #b67a08; }}\n    .badge.grey   {{ background: #f1efea; color: var(--ink-muted); }}\n\n    footer {{ text-align: center; padding: 32px 24px;\n             color: var(--ink-muted); font-size: 12px;\n             border-top: 1px solid var(--border); }}\n\n    @media print {{\n      header {{ padding: 36px 32px 28px; }}\n      header h1 {{ font-size: 42px; }}\n      .summary-cards {{ page-break-inside: avoid; }}\n      .dim-section, .summary-cards .card {{ box-shadow: none; border: 1px solid var(--border); }}\n    }}\n  </style>\n</head>\n<body>\n  <header>\n    <div class="header-inner">\n      <div class="eyebrow">Dimension Analysis</div>\n      <h1>{esc(data_view_name)} Dimension Analysis</h1>\n      <p class="lede">Cardinality, skew, anomaly, and data-quality review across the selected dimensions.</p>\n      <div class="meta">\n        <span><span class="icon">&#128197;</span> {esc(meta.get('date_range', 'N/A'))}</span>\n        <span><span class="icon">&#128202;</span> {esc(data_view_name)}</span>\n        <span><span class="icon">&#128340;</span> Generated {now}</span>\n      </div>\n    </div>\n  </header>\n  <nav>{nav_items}</nav>\n  <main>\n      <section id="summary">\n        <div class="section-label">Executive Summary</div>\n        <div class="summary-cards">\n          <div class="card">\n            <div class="card-label">Dimensions</div>\n            <div class="card-value">{summary.get('totalDimensions', 0)}</div>\n            <div class="card-sub">analyzed</div>\n          </div>\n          <div class="card">\n            <div class="card-label">High Cardinality</div>\n            <div class="card-value" style="color:var(--accent-yellow)">{summary.get('highCardinalityCount', 0)}</div>\n            <div class="card-sub">HIGH or VERY HIGH</div>\n          </div>\n          <div class="card">\n            <div class="card-label">Anomalies</div>\n            <div class="card-value" style="color:var(--accent-red)">{summary.get('totalAnomalies', 0)}</div>\n            <div class="card-sub">detected</div>\n          </div>\n          <div class="card">\n            <div class="card-label">Critical DQ</div>\n            <div class="card-value" style="color:var(--accent-red)">{summary.get('criticalDataQualityCount', 0)}</div>\n            <div class="card-sub">data quality issues</div>\n          </div>\n        </div>\n        <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:8px">\n          <div style="background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:18px 22px">\n            <div class="section-label" style="margin-bottom:8px;padding-bottom:6px">Cardinality Distribution</div>\n            <canvas id="chart-cardinality" width="300" height="200"></canvas>\n          </div>\n          <div style="background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:18px 22px">\n            <div class="section-label" style="margin-bottom:8px;padding-bottom:6px">Skew Distribution</div>\n            <canvas id="chart-skew" width="300" height="200"></canvas>\n          </div>\n        </div>\n        <script>\n          new Chart(document.getElementById('chart-cardinality'), {{\n            type: 'doughnut',\n            data: {{\n              labels: {card_labels},\n              datasets: [{{ data: {card_values},\n                backgroundColor: ['#1f7a4d','#d4a017','#c8312f','#7a1a18'] }}]\n            }},\n            options: {{ responsive: true, plugins: {{ legend: {{ position: 'bottom' }} }} }}\n          }});\n          new Chart(document.getElementById('chart-skew'), {{\n            type: 'doughnut',\n            data: {{\n              labels: {skew_labels},\n              datasets: [{{ data: {skew_values},\n                backgroundColor: ['#1f7a4d','#5a8a6a','#b9b6ae','#d4a017','#c8312f'] }}]\n            }},\n            options: {{ responsive: true, plugins: {{ legend: {{ position: 'bottom' }} }} }}\n          }});\n        </script>\n      </section>\n      {dim_sections}\n    </main>\n  <footer>Dimension Analysis &mdash; {esc(data_view_name)} &mdash; Generated {now}</footer>\n</body>\n</html>"""

def cleanup_old_files(output_dir: Path, prefix: str, keep: int):
    if keep == 0:
        return
    files = sorted(output_dir.glob(f'{prefix}*'), key=lambda p: p.stat().st_mtime, reverse=True)
    for old_file in files[keep:]:
        try:
            old_file.unlink()
            print(f'  🗑️  Removed old file: {old_file.name}')
        except OSError:
            pass

def main():
    if len(sys.argv) < 4:
        print(__doc__)
        sys.exit(1)
    analysis_json_path = sys.argv[1]
    data_view_name = sys.argv[2]
    data_view_id = sys.argv[3]
    output_dir = Path('.')
    fmt = 'html'
    keep_analyses = 0
    for arg in sys.argv[4:]:
        if arg.startswith('--format='):
            fmt = arg.split('=', 1)[1].lower()
        elif arg.startswith('--keep-analyses='):
            keep_analyses = int(arg.split('=', 1)[1])
        elif not arg.startswith('--'):
            output_dir = Path(arg)
    output_dir.mkdir(parents=True, exist_ok=True)
    print(f'\n🔍 CJA Dimension Analysis')
    print(f'   Data View: {data_view_name}')
    print(f'   Input:     {analysis_json_path}')
    print(f'   Output:    {output_dir} ({fmt})')
    print()
    print('📂 Loading analysis data...')
    with open(analysis_json_path) as f:
        raw = json.load(f)
    print('⚙️  Running analysis pipeline...')
    enriched = analyze(raw)
    ts = datetime.now().strftime('%Y-%m-%d_%H-%M')
    json_path = output_dir / f'dimension_analysis_results_{ts}.json'
    with open(json_path, 'w') as f:
        json.dump(enriched, f, indent=2, default=str)
    print(f'✅ Analysis JSON saved: {json_path} ({json_path.stat().st_size:,} bytes)')
    print(f'📝 Generating {fmt.upper()} report...')
    if fmt == 'markdown':
        report_content = generate_markdown(enriched, data_view_name)
        report_ext = 'md'
        report_prefix = 'dimension_analysis_report_'
    else:
        report_content = generate_html(enriched, data_view_name, data_view_id)
        report_ext = 'html'
        report_prefix = 'dimension_analysis_report_'
    report_path = output_dir / f'{report_prefix}{ts}.{report_ext}'
    with open(report_path, 'w') as f:
        f.write(report_content)
    print(f'✅ Report saved: {report_path} ({report_path.stat().st_size:,} bytes)')
    if keep_analyses > 0:
        print(f'\n🧹 Cleaning up old analysis files (keeping {keep_analyses} most recent)...')
        cleanup_old_files(output_dir, 'dimension_analysis_results_', keep_analyses)
        cleanup_old_files(output_dir, report_prefix, keep_analyses)
    try:
        input_path = Path(analysis_json_path)
        if input_path.exists():
            input_path.unlink()
            print(f'🧹 Cleaned up input file: {input_path.name}')
    except Exception as e:
        print(f'⚠️  Warning: Could not clean up input file: {e}')
    summary = enriched.get('summary', {})
    dims = enriched.get('dimensions', [])
    print(f'\n{'=' * 50}')
    print(f'📊 Analysis complete — {data_view_name}')
    print(f'   Dimensions analyzed:   {summary.get('totalDimensions', 0)}')
    print(f'   High cardinality:      {summary.get('highCardinalityCount', 0)}')
    print(f'   Total anomalies:       {summary.get('totalAnomalies', 0)}')
    print(f'   Critical DQ issues:    {summary.get('criticalDataQualityCount', 0)}')
    print(f'   Warning DQ issues:     {summary.get('warningDataQualityCount', 0)}')
    print(f'{'=' * 50}\n')
    return 0
if __name__ == '__main__':
    sys.exit(main())
