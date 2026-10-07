# Adding a new chart

All chart rendering is client-side in `index.html` (Tailwind and Chart.js from CDN); `server.py` serves the JSON endpoints.

## 1. Add the SQL endpoint in `server.py`

Add an entry to the `QUERY_DEFS` dict:

```python
"my-new-endpoint": {
    "sql": """
        SELECT date_trunc('day', er."createdAt")::date AS day,
               count(*) AS cnt
        FROM "ExecutedRule" er
        JOIN "EmailAccount" ea ON er."emailAccountId" = ea.id
        GROUP BY 1
        ORDER BY 1
    """,
    "has_account_join": True,   # True if query JOINs EmailAccount (enables account exclusion filter)
    "date_col": 'er."createdAt"',  # Column used for "since" date filter, or None
},
```

The server automatically handles filter injection (WHERE clauses for `exclude_accounts`, `since`, `skip_setup_hours`) — just write the base query without WHERE.

## 2. Add the chart container in `index.html`

Add a `<canvas>` inside the appropriate row section:

```html
<div class="bg-white border border-gray-200 rounded-xl p-5">
  <h2 class="text-sm font-semibold text-gray-900">Chart Title</h2>
  <p class="text-xs text-gray-500 mb-3">Brief description</p>
  <div class="h-64"><canvas id="chart-my-new"></canvas></div>
</div>
```

## 3. Add the render function in `index.html`

Follow the existing pattern — fetch data, destroy previous chart instance, create new Chart.js chart:

```javascript
async function renderMyNew() {
  const rows = await fetchData('my-new-endpoint');
  if (!rows.length) return;
  if (charts.myNew) charts.myNew.destroy();

  charts.myNew = new Chart(document.getElementById('chart-my-new'), {
    type: 'line',  // or 'bar', 'doughnut', 'bubble', etc.
    data: { labels: rows.map(r => r.day), datasets: [/* ... */] },
    options: {
      responsive: true, maintainAspectRatio: false,
      scales: { x: { grid: noGrid }, y: { grid: gridOpts, beginAtZero: true } },
    },
  });
}
```

## 4. Call it from `refreshAll()`

Add your render function to `refreshAll()` so it runs on load and every 5 minutes.

## Conventions

- Color palette: use the `C` object (e.g., `C.primary500`, `C.success500`, `C.warning500`, `C.error500`) for consistency
- Grid options: use `noGrid` for x-axis, `gridOpts` for y-axis
- Rolling averages: use the `rollingAvg(arr, window)` helper for smoothed overlays
- Chart instances: store in the `charts` object so they're properly destroyed on refresh
- `fetchData(endpoint)` handles filter params automatically — just pass the endpoint name
