# drilldown spec, data, and extra formats

## spec.json

| Key | Required | Meaning |
|---|---|---|
| `name` | yes | Slug for files: letters, digits, `.`, `_`, `-` |
| `title`, `subtitle` | no | Page header |
| `tz` | no | `{"offset_hours": 3, "label": "MSK"}`; default UTC. Axis, tiles, and table use it |
| `range` | yes | `{"start", "end", "step"}`; times as ISO-8601 (`2026-10-19T07:00:00Z`) or unix seconds; step `30s`/`5m`/`1h` |
| `source` | for `fetch` | `{"type": "prometheus", "url": "https://host/prometheus", "bearer_env": "VAR"}`; `url` is the API prefix before `/api/v1` |
| `params` | no | String substitutions for `${name}` in queries, e.g. a label selector |
| `max_points` | no | Per-series cap before downsampling (default 600) |
| `tiles` | no | List of stat tiles (below) |
| `panels` | yes | List of panels (below) |
| `table` | no | Bucketed table (below) |

Panel:

| Key | Meaning |
|---|---|
| `title`, `note` | Header and one-line caption |
| `type` | `stacked-bar`, `bar`, or `line` |
| `unit`, `scale` | Display unit and multiplier (e.g. bytes→GiB `9.313225746154785e-10`); a series may override `scale`/`unit` |
| `height` | Pixels, default 150 |
| `max` | Fixed y-max; default is a rounded data max |
| `thresholds` | `[{"value": 10, "label": "timeout"}]` dashed lines |
| `annotate_max` | Label the max point of the first series |
| `series` | 1–8 series |

Series:

| Key | Meaning |
|---|---|
| `id` | Unique: letters, digits, `.`, `_`, `:`, `-` |
| `label` | Legend text (default: id or split label value) |
| `query` | PromQL/MetricsQL for `query_range`; `${param}` substitution |
| `split_by` | Label name: one query → one series per value, ids `<id>:<value>` |
| `order` | Order of split values (`["gen0","gen1","gen2"]`) |
| `source: "data"` | No query; values come from `data.json` or `--extra` |
| `bucket` | Bar width for coarse series (e.g. `1h` events on a `5m` axis) |
| `downsample` | `sum` (bar default), `max` (line default), or `mean` |
| `step` | Per-series query step override |

The same series id keeps the same color in every panel. Colors follow a fixed, validated
8-slot order; text never takes series colors.

Tile: `{"label", "series", "agg", "unit", "decimals", "at_max", "note"}` with `agg` in
`max, min, sum, mean, median, last, p95` over the full-resolution scaled values;
`at_max: true` appends the time of the maximum. Static tile: `{"label", "value", "note"}`.

Table: `{"bucket": "1h", "columns": [{"series", "agg", "label"}]}`; buckets align to the
spec time zone.

## data.json (written by `fetch`, or by the agent from MCP results)

```json
{"version": 1,
 "series": {"pause": {"label": "pause", "points": [[1760000000, 4.5], ["2026-10-20T05:50:00Z", 76.7]]}},
 "errors": {"heap": "HTTP 422: too many samples"}}
```

Split series are stored under `<id>:<value>`. Values must be raw (unscaled); the panel
`scale` applies at render time.

## extra JSON (`--extra`, repeatable)

`{"<series id>": {"label": "...", "points": [[time, value], ...]}}` — merged over
`data.json`; use it for events from other systems (error trackers, deploys, alerts).

## Example (generic .NET GC pod view)

```json
{"name": "api-gc", "title": "api-7d9 — GC", "tz": {"offset_hours": 0, "label": "UTC"},
 "range": {"start": "2026-01-10T00:00:00Z", "end": "2026-01-11T00:00:00Z", "step": "5m"},
 "source": {"type": "prometheus", "url": "http://prometheus:9090"},
 "params": {"sel": "pod=\"api-7d9\""},
 "tiles": [{"label": "max pause / 5 min", "series": "pause", "agg": "max", "unit": "s", "at_max": true}],
 "panels": [
  {"title": "GC by generation", "type": "stacked-bar",
   "series": [{"id": "gc", "query": "sum by (gen) (increase(dotnet_gc_collections_total{${sel}}[5m]))", "split_by": "gen"}]},
  {"title": "Pause", "type": "bar", "unit": "s", "annotate_max": true,
   "series": [{"id": "pause", "query": "increase(dotnet_gc_pause_seconds_total{${sel}}[5m])"}]}]}
```

Metric names differ by exporter; check them in the target system before writing queries.
