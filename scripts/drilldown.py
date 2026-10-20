#!/usr/bin/env python3
"""Multi-panel time-series drilldown: declarative spec -> data.json -> one self-contained HTML.

Commands:
  validate --spec SPEC
  fetch    --spec SPEC --out DATA            # Prometheus-compatible /api/v1/query_range
  render   --spec SPEC --data DATA [--extra JSON ...] [--out FILE.html]

Stdlib only. Inputs are read-only; outputs are written atomically.
Spec schema: skills/drilldown/references/spec.md.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import statistics
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

PANEL_TYPES = {"bar", "stacked-bar", "line"}
AGGS = {"max", "min", "sum", "mean", "median", "last", "p95"}
NAME_RX = re.compile(r"^[A-Za-z0-9._-]+$")
ID_RX = re.compile(r"^[A-Za-z0-9._:-]+$")
PARAM_RX = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
DURATION_RX = re.compile(r"^(\d+)(s|m|h|d)$")
UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
MAX_HTML_BYTES = 512 * 1024
MAX_SERIES_PER_PANEL = 8
DEFAULT_MAX_POINTS = 600

# Validated categorical palette (8 slots, fixed order), light / dark.
PALETTE_LIGHT = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
PALETTE_DARK = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"]


class SpecError(ValueError):
    pass


# ---------- spec ----------

def parse_time(value) -> int:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    if isinstance(value, str):
        try:
            return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
        except ValueError as exc:
            raise SpecError(f"range: bad time {value!r}") from exc
    raise SpecError(f"range: bad time {value!r}")


def parse_duration(value) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    match = DURATION_RX.match(str(value))
    if not match or int(match.group(1)) <= 0:
        raise SpecError(f"bad duration {value!r} (use e.g. 30s, 5m, 1h)")
    return int(match.group(1)) * UNITS[match.group(2)]


def load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SpecError(f"cannot read {path}: {exc}") from exc


def validate(spec: dict) -> dict:
    """Return a normalized copy; raise SpecError with a precise message."""
    if not isinstance(spec, dict):
        raise SpecError("spec must be a JSON object")
    name = spec.get("name")
    if not isinstance(name, str) or not NAME_RX.match(name):
        raise SpecError("name: required, letters/digits/._- only")
    rng = spec.get("range") or {}
    start, end = parse_time(rng.get("start")), parse_time(rng.get("end"))
    step = parse_duration(rng.get("step", "5m"))
    if end <= start:
        raise SpecError("range: end must be after start")
    source = spec.get("source") or {}
    url = source.get("url")
    if url is not None:
        parsed = urllib.parse.urlparse(str(url))
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise SpecError("source.url must be an http(s) URL")
    params = spec.get("params") or {}
    if not all(isinstance(k, str) and isinstance(v, str) for k, v in params.items()):
        raise SpecError("params: string keys and values only")

    panels = spec.get("panels")
    if not isinstance(panels, list) or not panels:
        raise SpecError("panels: at least one panel required")
    declared: set[str] = set()
    split_bases: set[str] = set()
    for pi, panel in enumerate(panels):
        where = f"panels[{pi}]"
        if panel.get("type") not in PANEL_TYPES:
            raise SpecError(f"{where}.type must be one of {sorted(PANEL_TYPES)}")
        series = panel.get("series")
        if not isinstance(series, list) or not series:
            raise SpecError(f"{where}.series: at least one series")
        if len(series) > MAX_SERIES_PER_PANEL:
            raise SpecError(f"{where}: more than {MAX_SERIES_PER_PANEL} series; fold into Other or split panels")
        for si, s in enumerate(series):
            sid = s.get("id")
            if not isinstance(sid, str) or not ID_RX.match(sid):
                raise SpecError(f"{where}.series[{si}].id: letters/digits/._:- only")
            if sid in declared:
                raise SpecError(f"duplicate series id {sid!r}")
            declared.add(sid)
            query = s.get("query")
            if s.get("source", "query") == "data":
                if query:
                    raise SpecError(f"series {sid!r}: source=data must not have a query")
            elif not isinstance(query, str) or not query.strip():
                raise SpecError(f"series {sid!r}: query required (or source=data)")
            else:
                missing = [m for m in PARAM_RX.findall(query) if m not in params]
                if missing:
                    raise SpecError(f"series {sid!r}: undefined params {missing}")
            if s.get("split_by"):
                split_bases.add(sid)
            if "bucket" in s:
                parse_duration(s["bucket"])

    def known(ref: str) -> bool:
        return ref in declared or any(ref.startswith(base + ":") for base in split_bases)

    for ti, tile in enumerate(spec.get("tiles") or []):
        if "value" in tile:
            continue
        ref = tile.get("series")
        if not isinstance(ref, str) or not known(ref):
            raise SpecError(f"tiles[{ti}]: unknown series {ref!r}")
        if tile.get("agg", "max") not in AGGS:
            raise SpecError(f"tiles[{ti}].agg must be one of {sorted(AGGS)}")
    table = spec.get("table")
    if table:
        parse_duration(table.get("bucket", "1h"))
        for ci, col in enumerate(table.get("columns") or []):
            if not known(col.get("series", "")):
                raise SpecError(f"table.columns[{ci}]: unknown series {col.get('series')!r}")
            if col.get("agg", "sum") not in AGGS:
                raise SpecError(f"table.columns[{ci}].agg must be one of {sorted(AGGS)}")
    out = dict(spec)
    out["_start"], out["_end"], out["_step"] = start, end, step
    return out


# ---------- fetch ----------

def substitute(query: str, params: dict) -> str:
    return PARAM_RX.sub(lambda m: params[m.group(1)], query)


def query_range(url: str, expr: str, start: int, end: int, step: int, token: str | None, timeout: int) -> list:
    qs = urllib.parse.urlencode({"query": expr, "start": start, "end": end, "step": step})
    request = urllib.request.Request(url.rstrip("/") + "/api/v1/query_range?" + qs)
    if token:
        request.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8", "replace"))
            message = body.get("error") or str(exc)
        except (ValueError, AttributeError):
            message = str(exc)
        raise RuntimeError(f"HTTP {exc.code}: {message}") from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise RuntimeError(str(exc)) from None
    if body.get("status") != "success":
        raise RuntimeError(str(body.get("error") or body)[:300])
    data = body.get("data") or {}
    if data.get("resultType") != "matrix":
        raise RuntimeError(f"expected matrix, got {data.get('resultType')}")
    return data.get("result") or []


def clean_points(values) -> list:
    """[[unix-seconds | ISO-8601, value], ...] -> sorted [[int, float]]; non-finite values dropped."""
    points = []
    for t, v in values:
        try:
            f = float(v)
            ts = int(float(t)) if not isinstance(t, str) or re.fullmatch(r"\d+(\.\d+)?", t) else parse_time(t)
        except (TypeError, ValueError, SpecError):
            continue
        if math.isfinite(f):
            points.append([ts, f])
    return sorted(points)


def fetch(spec: dict, out: Path, timeout: int) -> int:
    source = spec.get("source") or {}
    if source.get("type", "prometheus") != "prometheus" or not source.get("url"):
        raise SpecError("fetch needs source.type=prometheus and source.url")
    token = os.environ.get(source["bearer_env"]) if source.get("bearer_env") else None
    params = spec.get("params") or {}
    result = {"version": 1, "range": {"start": spec["_start"], "end": spec["_end"], "step": spec["_step"]},
              "series": {}, "errors": {}}
    for panel in spec["panels"]:
        for s in panel["series"]:
            if s.get("source", "query") == "data":
                continue
            sid = s["id"]
            expr = substitute(s["query"], params)
            try:
                rows = query_range(source["url"], expr, spec["_start"], spec["_end"],
                                   parse_duration(s.get("step", spec["_step"])), token, timeout)
            except RuntimeError as exc:
                result["errors"][sid] = str(exc)[:300]
                print(f"drilldown: {sid}: {result['errors'][sid]}", file=sys.stderr)
                continue
            split = s.get("split_by")
            if split:
                for row in rows:
                    key = str((row.get("metric") or {}).get(split, "none"))
                    result["series"][f"{sid}:{key}"] = {"label": key, "points": clean_points(row.get("values", []))}
                continue
            if len(rows) > 1:
                print(f"drilldown: {sid}: {len(rows)} series returned, summing (use split_by to keep them apart)",
                      file=sys.stderr)
            merged: dict[int, float] = {}
            for row in rows:
                for t, v in clean_points(row.get("values", [])):
                    merged[t] = merged.get(t, 0.0) + v
            result["series"][sid] = {"label": s.get("label", sid), "points": sorted([t, v] for t, v in merged.items())}
    atomic_write(out, json.dumps(result, ensure_ascii=False))
    print(f"drilldown: wrote {out} ({len(result['series'])} series, {len(result['errors'])} errors)")
    return 0 if result["series"] else 1


# ---------- render ----------

def agg(values: list[float], how: str) -> float | None:
    if not values:
        return None
    if how == "max":
        return max(values)
    if how == "min":
        return min(values)
    if how == "sum":
        return sum(values)
    if how == "mean":
        return statistics.fmean(values)
    if how == "median":
        return statistics.median(values)
    if how == "last":
        return values[-1]
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, math.ceil(0.95 * len(ordered)) - 1)]


def fmt(value: float | None, unit: str = "", decimals: int | None = None) -> str:
    if value is None:
        return "—"
    if decimals is None:
        a = abs(value)
        decimals = 0 if a >= 100 or float(value).is_integer() else 1 if a >= 10 else 2
    text = f"{value:,.{decimals}f}".replace(",", " ")
    return f"{text} {unit}".strip()


def downsample(points: list, max_points: int, how: str) -> list:
    if len(points) <= max_points:
        return points
    factor = math.ceil(len(points) / max_points)
    out = []
    for i in range(0, len(points), factor):
        chunk = points[i:i + factor]
        vals = [v for _, v in chunk]
        out.append([chunk[0][0], sum(vals) if how == "sum" else max(vals) if how == "max" else statistics.fmean(vals)])
    return out


def resolve_series(panel: dict, series_data: dict) -> list[tuple[dict, str, dict | None]]:
    """Expand split_by declarations into concrete data ids, in declared order."""
    rows = []
    for s in panel["series"]:
        if s.get("split_by"):
            prefix = s["id"] + ":"
            found = [k for k in series_data if k.startswith(prefix)]
            order = s.get("order") or []
            found.sort(key=lambda k: (order.index(k[len(prefix):]) if k[len(prefix):] in order else len(order), k))
            for k in found:
                rows.append((s, k, series_data[k]))
            if not found:
                rows.append((s, s["id"], None))
        else:
            rows.append((s, s["id"], series_data.get(s["id"])))
    return rows


def tz_of(spec: dict) -> tuple[timezone, str]:
    tz = spec.get("tz") or {}
    hours = float(tz.get("offset_hours", 0))
    return timezone(timedelta(hours=hours)), tz.get("label", "UTC" if hours == 0 else f"UTC{hours:+g}")


def build_payload(spec: dict, data: dict) -> tuple[dict, list[str]]:
    warnings: list[str] = []
    series_data = dict(data.get("series") or {})
    max_points = int(spec.get("max_points", DEFAULT_MAX_POINTS))
    tzinfo, tzlabel = tz_of(spec)
    slots: dict[str, int] = {}
    scaled: dict[str, list] = {}   # id -> scaled full-resolution points (for tiles/table)
    units: dict[str, str] = {}
    panels = []
    for panel in spec["panels"]:
        ptype = panel["type"]
        out_series = []
        for s, data_id, payload in resolve_series(panel, series_data):
            if payload is None:
                warnings.append(f"series {data_id!r} has no data" +
                                (f" ({data['errors'][data_id]})" if data_id in (data.get("errors") or {}) else ""))
                points = []
            else:
                scale = float(s.get("scale", panel.get("scale", 1)))
                points = [[int(t), float(v) * scale] for t, v in payload.get("points", [])]
            scaled[data_id] = points
            units[data_id] = s.get("unit", panel.get("unit", ""))
            color_key = s.get("color_key", data_id)
            if color_key not in slots:
                slots[color_key] = len(slots) % len(PALETTE_LIGHT)
            how = s.get("downsample", "max" if ptype == "line" else "sum")
            out_series.append({
                "id": data_id,
                "label": (payload or {}).get("label") or s.get("label") or data_id,
                "slot": slots[color_key],
                "bucket": parse_duration(s["bucket"]) if s.get("bucket") else None,
                "points": downsample(points, max_points, how),
            })
        panels.append({
            "title": panel.get("title", ""), "note": panel.get("note", ""), "type": ptype,
            "unit": panel.get("unit", ""), "height": int(panel.get("height", 150)),
            "max": panel.get("max"), "thresholds": panel.get("thresholds") or [],
            "annotate_max": bool(panel.get("annotate_max")), "series": out_series,
        })

    def local(t: int, pattern: str) -> str:
        return datetime.fromtimestamp(t, tzinfo).strftime(pattern)

    tiles = []
    for tile in spec.get("tiles") or []:
        if "value" in tile:
            tiles.append({"label": tile["label"], "value": str(tile["value"]), "note": tile.get("note", "")})
            continue
        pts = scaled.get(tile["series"], [])
        how = tile.get("agg", "max")
        value = agg([v for _, v in pts], how)
        note = tile.get("note", "")
        if tile.get("at_max") and pts:
            t_max = max(pts, key=lambda p: p[1])[0]
            note = (note + " · " if note else "") + f"at {local(t_max, '%d.%m %H:%M')} {tzlabel}"
        tiles.append({"label": tile["label"],
                      "value": fmt(value, tile.get("unit", units.get(tile["series"], "")), tile.get("decimals")),
                      "note": note})

    table = None
    if spec.get("table"):
        bucket = parse_duration(spec["table"].get("bucket", "1h"))
        offset = int(tzinfo.utcoffset(None).total_seconds())
        columns = spec["table"].get("columns") or []
        keys = sorted({(t + offset) // bucket for c in columns for t, _ in scaled.get(c["series"], [])})
        rows = []
        for key in keys:
            start_t = key * bucket - offset
            cells = []
            for c in columns:
                vals = [v for t, v in scaled.get(c["series"], []) if (t + offset) // bucket == key]
                value = agg(vals, c.get("agg", "sum"))
                cells.append(None if value is None else round(value, 2))
            rows.append({"label": local(start_t, "%d.%m %H:%M"), "cells": cells})
        table = {"columns": [c.get("label", c["series"]) for c in columns], "rows": rows}

    payload = {
        "title": spec.get("title", spec["name"]), "subtitle": spec.get("subtitle", ""),
        "start": spec["_start"], "end": spec["_end"], "step": spec["_step"],
        "tzOffset": int(tzinfo.utcoffset(None).total_seconds()), "tzLabel": tzlabel,
        "tiles": tiles, "panels": panels, "table": table,
        "palette": {"light": PALETTE_LIGHT, "dark": PALETTE_DARK},
    }
    return payload, warnings


HTML = r"""<!doctype html><html lang="__LANG__"><head><meta charset="utf-8">
<title>__TITLE__</title>
<style>
:root{--surface:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--muted:#898781;--grid:#e1e0d9;--axis:#c3c2b7}
@media (prefers-color-scheme:dark){:root{--surface:#1a1a19;--ink:#f0efec;--ink2:#c3c2b7;--muted:#898781;--grid:#2c2c2a;--axis:#383835}}
body{margin:0;padding:16px;background:var(--surface);color:var(--ink);font:13px/1.4 system-ui,-apple-system,"Segoe UI",sans-serif}
h1{font-size:15px;margin:0 0 4px}.sub{color:var(--ink2);margin:0 0 12px}
.tiles{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:14px}
.tile{border:1px solid var(--grid);border-radius:8px;padding:8px 12px;min-width:150px}
.tile b{display:block;font-size:20px;font-variant-numeric:tabular-nums}.tile span{color:var(--ink2);font-size:12px;white-space:pre-line}
.ptitle{font-weight:600;margin:10px 0 2px}.pnote{color:var(--muted);font-size:12px}
.legend{display:flex;gap:14px;flex-wrap:wrap;color:var(--ink2);font-size:12px;margin:2px 0}
.sw{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:4px;vertical-align:-1px}
svg{display:block;width:100%;overflow:visible}
.tip{position:fixed;pointer-events:none;background:var(--surface);border:1px solid var(--axis);border-radius:6px;padding:6px 8px;font-size:12px;box-shadow:0 2px 8px rgba(0,0,0,.15);display:none;font-variant-numeric:tabular-nums;z-index:9;max-width:320px}
table{border-collapse:collapse;font-variant-numeric:tabular-nums;font-size:12px;margin-top:6px}
td,th{padding:2px 8px;border-bottom:1px solid var(--grid);text-align:right}th{color:var(--ink2);font-weight:600}td:first-child,th:first-child{text-align:left}
details{margin-top:12px}summary{cursor:pointer;color:var(--ink2)}
</style></head><body>
<h1 id="title"></h1><p class="sub" id="sub"></p><div class="tiles" id="tiles"></div><div id="charts"></div>
<details id="tablebox" hidden><summary>Table</summary><table id="tbl"></table></details><div class="tip" id="tip"></div>
<script id="dd-data" type="application/json">__DATA__</script>
<script>
const D=JSON.parse(document.getElementById("dd-data").textContent);
const dark=window.matchMedia&&window.matchMedia("(prefers-color-scheme: dark)").matches;
const PAL=dark?D.palette.dark:D.palette.light;
const css=v=>getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const NS="http://www.w3.org/2000/svg",W=900,L=52,RP=12,SPAN=D.end+D.step-D.start;
const X=t=>L+(t-D.start)/SPAN*(W-L-RP);
const esc=s=>String(s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
function el(t,a,p){const e=document.createElementNS(NS,t);for(const k in a)e.setAttribute(k,a[k]);if(p)p.appendChild(e);return e}
function fmt(v,u){if(v==null||isNaN(v))return"—";const a=Math.abs(v);const d=a>=100||Number.isInteger(v)?0:a>=10?1:2;return v.toFixed(d)+(u?" "+u:"")}
function local(t,withDate){const d=new Date((t+D.tzOffset)*1000);const p=n=>String(n).padStart(2,"0");return(withDate?p(d.getUTCDate())+"."+p(d.getUTCMonth()+1)+" ":"")+p(d.getUTCHours())+":"+p(d.getUTCMinutes())}
function nice(m){if(!(m>0))return 1;const e=Math.pow(10,Math.floor(Math.log10(m)));for(const f of[1,2,2.5,5,10])if(f*e>=m)return f*e;return 10*e}
document.getElementById("title").textContent=D.title;
document.getElementById("sub").textContent=(D.subtitle?D.subtitle+" · ":"")+local(D.start,1)+" → "+local(D.end,1)+" "+D.tzLabel;
for(const t of D.tiles)document.getElementById("tiles").insertAdjacentHTML("beforeend",`<div class="tile"><b>${esc(t.value)}</b><span>${esc(t.label)}${t.note?"\n"+esc(t.note):""}</span></div>`);
const lookups=[];
for(const P of D.panels){
  const box=document.createElement("div");
  box.innerHTML=`<div class="ptitle">${esc(P.title)}</div>${P.note?`<div class="pnote">${esc(P.note)}</div>`:""}`+
    (P.series.length>1?`<div class="legend">${P.series.map(s=>`<span><i class="sw" style="background:${PAL[s.slot]}"></i>${esc(s.label)}</span>`).join("")}</div>`:"");
  document.getElementById("charts").appendChild(box);
  const h=P.height,svg=el("svg",{viewBox:`0 0 ${W} ${h}`,height:h});box.appendChild(svg);
  const maps=P.series.map(s=>new Map(s.points.map(p=>[p[0],p[1]])));
  let top=0;
  if(P.type==="stacked-bar"){const sums=new Map();P.series.forEach(s=>s.points.forEach(([t,v])=>sums.set(t,(sums.get(t)||0)+Math.max(v,0))));for(const v of sums.values())top=Math.max(top,v)}
  else P.series.forEach(s=>s.points.forEach(([,v])=>top=Math.max(top,v)));
  for(const th of P.thresholds)top=Math.max(top,th.value);
  const max=P.max!=null?P.max:nice(top*1.05),Y=v=>h-18-Math.max(0,Math.min(v,max))/max*(h-28);
  for(let i=0;i<=4;i++){const v=max*i/4,y=Y(v);el("line",{x1:L,x2:W-RP,y1:y,y2:y,stroke:css("--grid")},svg);
    el("text",{x:L-6,y:y+4,"text-anchor":"end","font-size":11,fill:css("--muted")},svg).textContent=fmt(v,i===4?P.unit:"")}
  el("line",{x1:L,x2:W-RP,y1:h-18,y2:h-18,stroke:css("--axis")},svg);
  const hours=SPAN/3600,every=[1,2,3,4,6,12,24,48].find(x=>hours/x<=8)||96;
  for(let t=Math.ceil((D.start+D.tzOffset)/(every*3600))*every*3600-D.tzOffset;t<=D.end;t+=every*3600)
    el("text",{x:X(t),y:h-4,"text-anchor":"middle","font-size":11,fill:css("--muted")},svg).textContent=local(t,every>=12||t===D.start);
  const bw=D.step/SPAN*(W-L-RP);
  if(P.type==="line"){P.series.forEach(s=>{let seg=[],prev=null;const flush=()=>{if(seg.length>1)el("polyline",{points:seg.join(" "),fill:"none",stroke:PAL[s.slot],"stroke-width":2,"stroke-linejoin":"round"},svg);seg=[]};
      for(const[t,v]of s.points){if(prev!=null&&t-prev>2*D.step)flush();seg.push(`${X(t)+bw/2},${Y(v)}`);prev=t}flush();
      const last=s.points[s.points.length-1];if(last&&P.series.length<=4)el("text",{x:W-RP-2,y:Y(last[1])-6,"text-anchor":"end","font-size":11,fill:css("--ink2")},svg).textContent=`${s.label} ${fmt(last[1],P.unit)}`})}
  else{const base=new Map();P.series.forEach(s=>{const w=s.bucket?s.bucket/SPAN*(W-L-RP):bw;for(const[t,v]of s.points){if(!(v>0))continue;
      const b=P.type==="stacked-bar"?(base.get(t)||0):0,y0=Y(b),y1=Y(b+v);
      el("rect",{x:X(t)+0.5,y:y1+(P.type==="stacked-bar"&&b>0?1:0),width:Math.max(w-1,1),height:Math.max(y0-y1-(P.type==="stacked-bar"&&b>0?1:0),0.5),fill:PAL[s.slot],rx:1},svg);
      if(P.type==="stacked-bar")base.set(t,b+v)}})}
  for(const th of P.thresholds){const y=Y(th.value);el("line",{x1:L,x2:W-RP,y1:y,y2:y,stroke:css("--ink2"),"stroke-dasharray":"4 4"},svg);
    el("text",{x:W-RP-2,y:y-4,"text-anchor":"end","font-size":11,fill:css("--ink2")},svg).textContent=th.label?`${th.label} ${fmt(th.value,P.unit)}`:fmt(th.value,P.unit)}
  if(P.annotate_max&&P.series[0]&&P.series[0].points.length){const m=P.series[0].points.reduce((a,p)=>p[1]>a[1]?p:a);
    el("text",{x:X(m[0])+6,y:Y(m[1])+4,"font-size":11,fill:css("--ink2")},svg).textContent=`${fmt(m[1],P.unit)} · ${local(m[0],1)}`}
  const cross=el("line",{y1:0,y2:h-18,stroke:css("--muted"),"stroke-dasharray":"3 3",visibility:"hidden"},svg);
  lookups.push({P,maps,svg,cross});
}
const tip=document.getElementById("tip");
function hover(t,ev){const x=X(t)+(D.step/SPAN*(W-L-RP))/2;let html=`<b>${local(t,1)} ${esc(D.tzLabel)}</b>`;
  for(const l of lookups){l.cross.setAttribute("x1",x);l.cross.setAttribute("x2",x);l.cross.setAttribute("visibility","visible");
    const parts=l.P.series.map((s,i)=>{let v=l.maps[i].get(t);if(v==null&&s.bucket){const k=[...l.maps[i].keys()].find(k=>k<=t&&t<k+s.bucket);if(k!=null)v=l.maps[i].get(k)}
      return v==null?null:`${esc(s.label)} ${fmt(v,l.P.unit)}`}).filter(Boolean);
    if(parts.length)html+=`<br><span style="color:var(--muted)">${esc(l.P.title)}:</span> ${parts.join(" · ")}`}
  tip.innerHTML=html;tip.style.display="block";tip.style.left=Math.min(ev.clientX+14,window.innerWidth-330)+"px";tip.style.top=(ev.clientY+14)+"px"}
for(const l of lookups){l.svg.addEventListener("mousemove",ev=>{const r=l.svg.getBoundingClientRect(),vx=(ev.clientX-r.left)*W/r.width;
    const t=D.start+Math.max(0,Math.min(Math.round(SPAN/D.step)-1,Math.floor((vx-L)/(W-L-RP)*SPAN/D.step)))*D.step;hover(t,ev)});
  l.svg.addEventListener("mouseleave",()=>{tip.style.display="none";lookups.forEach(k=>k.cross.setAttribute("visibility","hidden"))})}
if(D.table&&D.table.rows.length){const tb=document.getElementById("tbl");document.getElementById("tablebox").hidden=false;
  tb.insertAdjacentHTML("beforeend",`<tr><th>${esc(D.tzLabel)}</th>${D.table.columns.map(c=>`<th>${esc(c)}</th>`).join("")}</tr>`);
  for(const r of D.table.rows)tb.insertAdjacentHTML("beforeend",`<tr><td>${esc(r.label)}</td>${r.cells.map(c=>`<td>${c==null?"":c}</td>`).join("")}</tr>`)}
</script></body></html>"""


def default_out(spec: dict, payload_json: str) -> Path:
    digest = hashlib.sha1(payload_json.encode()).hexdigest()[:6]
    base = os.environ.get("ORCA_CHAT_SCRATCH_DIR")
    return Path(base or ".") / f"{spec['name']}-{digest}.html"


def render(spec: dict, data: dict, out: Path | None, lang: str) -> int:
    payload, warnings = build_payload(spec, data)
    for warning in warnings:
        print(f"drilldown: warning: {warning}", file=sys.stderr)
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    target = out or default_out(spec, raw)
    if not NAME_RX.match(target.name) or target.suffix != ".html":
        raise SpecError(f"output file name {target.name!r}: use letters/digits/._- and .html")
    title = payload["title"].replace("&", "&amp;").replace("<", "&lt;")
    html = HTML.replace("__DATA__", raw).replace("__TITLE__", title).replace("__LANG__", lang)
    size = len(html.encode("utf-8"))
    if size > MAX_HTML_BYTES:
        raise SpecError(f"HTML is {size} bytes (> {MAX_HTML_BYTES}); lower max_points or use a coarser step")
    atomic_write(target, html)
    print(f"drilldown: wrote {target} ({size} bytes)")
    if os.environ.get("ORCA_CHAT_SCRATCH_DIR") and target.parent.resolve() == Path(os.environ["ORCA_CHAT_SCRATCH_DIR"]).resolve():
        print(f'::orca-visual{{file="{target.name}" title="{payload["title"]}"}}')
    return 0


# ---------- io / cli ----------

def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def merge_extra(data: dict, extras: list[Path]) -> dict:
    merged = {"version": 1, "series": dict(data.get("series") or {}), "errors": dict(data.get("errors") or {})}
    for path in extras:
        extra = load_json(path)
        series = extra.get("series", extra) if isinstance(extra, dict) else None
        if not isinstance(series, dict):
            raise SpecError(f"{path}: expected {{id: {{label, points}}}}")
        for sid, value in series.items():
            if not isinstance(value, dict) or not isinstance(value.get("points"), list):
                raise SpecError(f"{path}: series {sid!r} needs a points list")
            merged["series"][sid] = {"label": value.get("label", sid), "points": clean_points(value["points"])}
            merged["errors"].pop(sid, None)
    return merged


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_val = sub.add_parser("validate")
    p_val.add_argument("--spec", type=Path, required=True)
    p_fetch = sub.add_parser("fetch")
    p_fetch.add_argument("--spec", type=Path, required=True)
    p_fetch.add_argument("--out", type=Path, required=True)
    p_fetch.add_argument("--timeout", type=int, default=60)
    p_render = sub.add_parser("render")
    p_render.add_argument("--spec", type=Path, required=True)
    p_render.add_argument("--data", type=Path, required=True)
    p_render.add_argument("--extra", type=Path, action="append", default=[])
    p_render.add_argument("--out", type=Path)
    p_render.add_argument("--lang", default="en")
    args = parser.parse_args(argv)
    try:
        spec = validate(load_json(args.spec))
        if args.cmd == "validate":
            print("drilldown: spec OK")
            return 0
        if args.cmd == "fetch":
            return fetch(spec, args.out, args.timeout)
        return render(spec, merge_extra(load_json(args.data), args.extra), args.out, args.lang)
    except SpecError as exc:
        print(f"drilldown: invalid spec: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
