<accessibility_remediation_report>

*Transit Network Viewer — pre-demo accessibility pass (static code review of `index.html`, `app.js`, `style.css`). Scoped to your top-6 request; the summary and open-questions sections below are kept to a few lines each because the studio hand-off format requires them. The paste-ready ticket table is in `<implementation_plan>`.*

<audit_summary>
6 findings: 1 × unlabeled controls (Category 1), 3 × un-announced dynamic updates (Category 2), 2 × color-only meaning (Category 3). Severity: 5 Serious, 1 Moderate, 0 Blockers. Per your scoping, keyboard operability was not audited beyond your own pass, and every fix below is attribute-level or contained to a single function — no update-flow rewiring, no visual redesign of the lines or chips.
</audit_summary>

<findings>

## Category 1 — Controls not identifiable via assistive technology

### F1 — Dataset and algorithm `<select>`s have no accessible name; layer-toggle group headers not programmatically associated — **Serious**

**Where:** `app.js` lines 520–533 and 536–554 (selects); `index.html` lines 25–26 and `app.js` lines 723–737 (group headers). **Reference:** WCAG 2.1 SC 4.1.2 (Name, Role, Value).

First, the good news on the checkboxes you asked about: static analysis indicates they **do** have accessible names — `createToggle()` wraps each input in a `<label>` with a text node (`app.js:789–790`):

```js
label.appendChild(checkbox);
label.appendChild(document.createTextNode(' ' + name));
```

A screen reader announces these as e.g. "Line 12, checkbox, checked." No change needed there. The genuinely unnamed controls in that panel are the two `<select>`s, which are created with no `<label>`, `aria-label`, or any other name source — a screen reader announces only "combo box, Normal Network" with no indication of what the control switches:

```js
const datasetSelect = document.createElement('select');
datasetSelect.id = 'dataset-select';
```

```js
const linesSourceSelect = document.createElement('select');
linesSourceSelect.id = 'lines-source-select';
```

**Fix (zero visual change — `aria-label` chosen over a visible `<label>` specifically to leave your layout untouched; a visible label would otherwise be the first choice):**

```js
const datasetSelect = document.createElement('select');
datasetSelect.id = 'dataset-select';
datasetSelect.setAttribute('aria-label', 'Dataset');
```

```js
const linesSourceSelect = document.createElement('select');
linesSourceSelect.id = 'lines-source-select';
linesSourceSelect.setAttribute('aria-label', 'Line-generation algorithm');
```

Second, the toggle panel's context is visual-only: the `<h3>Toggle Layers</h3>` is not associated with the `<form id="layer-toggles">`, and the `Group ${groupNum}` headers (`app.js:723–725`) are plain `<div>`s a screen reader reads as stray text between checkboxes. Fix in `index.html`:

```html
<!-- Before -->
<h3>Toggle Layers</h3>
<form id="layer-toggles"></form>

<!-- After -->
<h3 id="layer-toggles-heading">Toggle Layers</h3>
<form id="layer-toggles" aria-labelledby="layer-toggles-heading"></form>
```

And in `renderLayerToggles()` (`app.js:734–738`), wrap each group so the header is announced as group context (contained to this one function — no update-flow change):

```js
// Before
col.appendChild(groupHeader);
linesInCol += 1;
groupLines.forEach(n => {
    createToggle(n, map.hasLayer(layers[n]), col);
    linesInCol += 1;
});

// After
const groupWrap = document.createElement('div');
groupWrap.setAttribute('role', 'group');
groupHeader.id = `layer-group-header-${groupNum}`;
groupWrap.setAttribute('aria-labelledby', groupHeader.id);
groupWrap.appendChild(groupHeader);
linesInCol += 1;
groupLines.forEach(n => {
    createToggle(n, map.hasLayer(layers[n]), groupWrap);
    linesInCol += 1;
});
col.appendChild(groupWrap);
```

If even that touch is too much before Thursday, the group wrap alone can be deferred (checkbox names like "Line 12" are unique without it); the two `aria-label`s on the selects should not be deferred.

## Category 2 — Dynamic updates with no announcement

### F2 — Route-finder status prompts update silently — **Serious**

**Where:** `index.html` line 22; written to by `app.js` lines 850, 869, 902, 918, 992, 1189, 1218, 1292. **Reference:** WCAG 2.1 SC 4.1.3 (Status Messages); ARIA authoring practice: `aria-live="polite"` for non-urgent updates.

The entire route-finder conversation — "Click the starting location.", "Origin: X … Click the destination.", "Finding route..." — is written into a bare `<div>`:

```html
<div id="route-finder-status"></div>
```

```js
routeFinderStatus.textContent = 'Click the starting location.';
```

Static analysis indicates none of this is ever announced: a screen reader user activates "Route finder" and hears nothing about what to do next, in both the marker-click flow and the map-click flow. Visual prominence doesn't substitute for programmatic announcement. Because the container is a persistent element that JS fills in place via `textContent`/`innerHTML`, this is the rare case where the *correct* live-region pattern is also a one-attribute diff:

```html
<!-- Before -->
<div id="route-finder-status"></div>

<!-- After -->
<div id="route-finder-status" role="status" aria-live="polite" aria-atomic="true"></div>
```

No JS changes required.

### F3 — "No route found." error is invisible to screen readers — **Serious**

**Where:** `app.js` line 923 (station-marker handler) **and** line 1223 (map-click handler — near-duplicate code paths; fix both). **Reference:** WCAG 2.1 SC 4.1.3; ARIA authoring practice: `role="alert"`/assertive is reserved for error-level updates such as "no route found between selected stations."

```js
if (path.length < 2) {
    routeFinderStatus.textContent = 'No route found.';
    return;
}
```

This is the failure outcome of the app's core task, conveyed as a silent text swap. Correct pattern: route it through a dedicated alert region that exists in the DOM from page load. Add to `index.html` under line 22:

```html
<div id="route-finder-error" role="alert"></div>
```

In `app.js` next to the other lookups (~line 826): `const routeFinderError = document.getElementById('route-finder-error');`, then at **both** failure sites:

```js
// After (lines 923 and 1223)
if (path.length < 2) {
    routeFinderStatus.textContent = '';
    routeFinderError.textContent = 'No route found.';
    return;
}
```

…and clear it on each new attempt by adding `routeFinderError.textContent = '';` beside each existing reset of `routeFinderStatus` (lines 850, 869, 918, 1088, 1218, 1395). **Diff-size note, since you asked for no restructuring:** this is the largest diff in the list (~10 one-line touches, no logic changes). The minimal-but-less-correct alternative is to do nothing beyond F2 and let "No route found." flow through the polite status region — it would be announced, but without error urgency and it can be pre-empted by other speech, which is exactly what the assertive/alert practice exists to prevent. Recommend the alert region; the fallback is there if Thursday wins.

### F4 — Route result summary renders silently (and is written twice) — **Serious**

**Where:** `index.html` line 23; `app.js` lines 1047 + 1059 (marker handler) **and** 1354 + 1366 (map-click handler — duplicate paths; fix both). **Reference:** WCAG 2.1 SC 4.1.3; ARIA authoring practice: `aria-live="polite"` for non-urgent updates.

```js
routeFinderResult.innerHTML =
    `<b>Route:</b> ${walkSummaryStart}${lineSequence.map(l => `Line ${l}`).join(' → ')}${walkDistanceStrEnd}` +
```

```js
routeFinderResult.innerHTML += `<br><button id="${showHideBtnId}">Show intermediate stations</button><div id="${intermediateListId}" style="display:none;"></div>`;
```

The route/transfers/travel-time payoff appears with no announcement. Fix: make the persistent container a polite live region, and merge the two back-to-back `innerHTML` writes into one so the region announces once instead of twice:

```html
<!-- index.html line 23 -->
<div id="route-finder-result" aria-live="polite"></div>
```

```js
// After — at both sites, delete the `+=` statement and append its string
// to the single assignment instead:
routeFinderResult.innerHTML =
    `<b>Route:</b> ${walkSummaryStart}${lineSequence.map(l => `Line ${l}`).join(' → ')}${walkDistanceStrEnd}` +
    /* ...existing lines unchanged... */
    (walkDistance > 0 ? `<br><b>Total walking time:</b> ${walkTimeStr}` : '') +
    `<br><button id="${showHideBtnId}">Show intermediate stations</button><div id="${intermediateListId}" style="display:none;"></div>`;
```

The existing `document.getElementById(showHideBtnId).onclick = ...` line runs after the write either way, so it is unaffected. (The embedded button's label is read as part of the announcement — acceptable under SC 4.1.3.)

## Category 3 — Meaning conveyed by color alone

### F5 — Crowding/Delay chip severity (ok/warn/alert) exists only as background color — **Serious**

**Where:** `app.js` lines 124–125 (thresholds) and 130–131 (chip markup); classes styled in `style.css` lines 118–131. **Reference:** WCAG 2.1 SC 1.4.1 (Use of Color).

```js
chips.push(`<span class="chip ${statusChipClass(crowdingClass)}">Crowding ${escapeHtml(formatPercent(meta.occupancyPct))}</span>`);
chips.push(`<span class="chip ${statusChipClass(delayClass)}">Delay ${escapeHtml(formatMinutes(meta.delayMin))}</span>`);
```

The ops panel's headline promise — "network health at a glance" — is encoded solely in green/orange/red backgrounds. The visible text ("Crowding 65%", "Delay 7 min") does not carry the judgment, because the 60/80% and 5/10-min thresholds live only in code and color. Static analysis indicates a screen reader user hears no severity at all, and a color-blind user can't separate warn from ok. Note the Accessible/Not-accessible chip (line 132) already passes — its text differs by state. To be straight with you on "leave the look alone": SC 1.4.1 requires a *visible* non-color cue, so a zero-pixel fix isn't possible — the fix below is the smallest one (a small glyph inside the chip; colors, shape, and layout untouched) plus screen-reader-only severity text:

```js
// After — in buildOperationalChips (app.js:123), add once:
const STATUS_MARKS = { ok: '✓', warn: '▲', alert: '✖' };
const STATUS_WORDS = { ok: 'normal', warn: 'elevated', alert: 'critical' };
function chipHtml(statusKey, text) {
    const cls = statusChipClass(statusKey);
    const mark = STATUS_MARKS[cls] ? `<span aria-hidden="true">${STATUS_MARKS[cls]}&thinsp;</span>` : '';
    const word = STATUS_WORDS[cls] ? `<span class="visually-hidden"> — ${STATUS_WORDS[cls]}</span>` : '';
    return `<span class="chip ${cls}">${mark}${text}${word}</span>`;
}
// ...then replace lines 130–131:
chips.push(chipHtml(crowdingClass, `Crowding ${escapeHtml(formatPercent(meta.occupancyPct))}`));
chips.push(chipHtml(delayClass, `Delay ${escapeHtml(formatMinutes(meta.delayMin))}`));
```

```css
/* style.css — add */
.visually-hidden {
    position: absolute;
    width: 1px; height: 1px;
    margin: -1px; padding: 0;
    overflow: hidden;
    clip: rect(0 0 0 0);
    white-space: nowrap;
    border: 0;
}
```

### F6 — Transit lines are distinguished only by color on the map, with no text equivalent for what each line serves — **Moderate**

**Where:** map rendering `app.js` lines 349 and 397 (`const color = COLORS[group % COLORS.length];` … `L.polyline(latlngs, { color, weight: 4, opacity: 1 })`); remediation lands in `renderTransitOperationsPanel`, `app.js` lines 144 and 168. **Reference:** WCAG 2.1 SC 1.4.1 (Use of Color); map-interface practice: provide a non-visual equivalent (linked data table/list of stations and routes) for spatial information.

On the map, one route is told from another purely by palette color, and which stations a line serves is discoverable only by hover tooltips over a canvas/SVG layer a screen reader cannot perceive. We are explicitly **not** recommending making the Leaflet layer itself screen-reader operable — that's unrealistic for a rendered map and out of scope. Instead, supplement the text equivalent that already exists: the ops panel lists every line (name, kind, status) but omits the spatial facts. The per-station names are already in each feature (`name_list`, `is_station`), so extend the existing rows:

```js
// Before (app.js:144)
const lines = (features || []).map(feature => normalizeLineMetadata(feature.properties || {}));

// After
const lines = (features || []).map(feature => {
    const meta = normalizeLineMetadata(feature.properties || {});
    const names = ((feature.properties || {}).name_list || []).filter(Boolean);
    meta.stationCount = ((feature.properties || {}).is_station || []).filter(Boolean).length;
    meta.termini = names.length ? `${names[0]} – ${names[names.length - 1]}` : '';
    return meta;
});
```

```js
// Before (app.js:168)
<div class="line-detail-meta">${escapeHtml(meta.routeKind)} · ${escapeHtml(meta.serviceStatus)} · ${escapeHtml(meta.rowType)}</div>

// After
<div class="line-detail-meta">${escapeHtml(meta.routeKind)} · ${escapeHtml(meta.serviceStatus)} · ${escapeHtml(meta.rowType)}${meta.termini ? ` · ${escapeHtml(meta.termini)}` : ''} · ${meta.stationCount} stations</div>
```

Contained to one function; adds a few words to each existing row and changes nothing on the map itself. (The real-world overlay already passes on real-vs-generated: real lines are dashed `dashArray: '2 2'` vs. solid generated lines — a non-color cue — so that distinction was not flagged.)

</findings>

<implementation_plan>

Paste-ready ticket — one row per fix, ordered by severity (all diffs are attribute-level or contained to a single function; no rewiring, no visual redesign):

| # | Fix (ID) | Where | Severity | WCAG / ARIA | The change |
| --- | --- | --- | --- | --- | --- |
| 1 | Make route-finder status a live region (F2) | `index.html:22` | Serious | 4.1.3; polite live region | Change `<div id="route-finder-status"></div>` to `<div id="route-finder-status" role="status" aria-live="polite" aria-atomic="true"></div>`. No JS changes. |
| 2 | Announce "No route found." as an alert (F3) | `index.html` (add under line 22); `app.js:923` **and** `1223` (duplicate paths); clear beside resets at 850, 869, 918, 1088, 1218, 1395 | Serious | 4.1.3; `role="alert"` for error-level updates | Add `<div id="route-finder-error" role="alert"></div>` + `const routeFinderError = document.getElementById('route-finder-error');`. At both failure sites: `routeFinderStatus.textContent = ''; routeFinderError.textContent = 'No route found.';`. Add `routeFinderError.textContent = '';` beside each status reset. *Largest diff (~10 one-liners); minimal fallback = let it ride the polite region from row 1, at the cost of error urgency.* |
| 3 | Announce route results once (F4) | `index.html:23`; `app.js:1047+1059` **and** `1354+1366` (duplicate paths) | Serious | 4.1.3; polite live region | Change line 23 to `<div id="route-finder-result" aria-live="polite"></div>`; at both sites delete the `routeFinderResult.innerHTML += \`<br><button id="${showHideBtnId}">…\`` statement and append that string to the single `innerHTML =` assignment so the region gets one write. |
| 4 | Name the two `<select>`s + associate toggle-panel headings (F1) | `app.js:521, 537`; `index.html:25–26`; `app.js:734–738` | Serious | 4.1.2 | Add `datasetSelect.setAttribute('aria-label', 'Dataset');` and `linesSourceSelect.setAttribute('aria-label', 'Line-generation algorithm');` (zero visual change). Change heading/form to `<h3 id="layer-toggles-heading">Toggle Layers</h3>` + `<form id="layer-toggles" aria-labelledby="layer-toggles-heading">`. Optionally wrap each toggle group in `role="group"` + `aria-labelledby` per F1 snippet. Checkboxes themselves already have implicit labels — leave `createToggle` as is. |
| 5 | Add non-color severity cue to Crowding/Delay chips (F5) | `app.js:123–131`; `style.css` (append) | Serious | 1.4.1 | In `buildOperationalChips`, add `STATUS_MARKS`/`STATUS_WORDS` + `chipHtml()` helper per F5 snippet; replace the two `chips.push(...)` calls at lines 130–131. Add `.visually-hidden` rule to `style.css`. Colors/shape untouched; adds only a small ✓/▲/✖ glyph inside the chip (a fully invisible fix can't satisfy 1.4.1). |
| 6 | Text equivalent for map lines: termini + station count in ops rows (F6) | `app.js:144` and `168` (`renderTransitOperationsPanel`) | Moderate | 1.4.1; map non-visual-equivalent practice | Extend the `lines` mapping to capture `name_list` termini and `is_station` count, and append `· ${termini} · ${stationCount} stations` to the existing `line-detail-meta` template, per F6 snippet. Map rendering untouched. |

</implementation_plan>

<open_questions>

- **Not confirmable statically:** the `../data/**/*.geojson` files weren't provided, so F6 assumes `name_list`/`is_station` are populated the way the tooltip code (app.js:445–457) already assumes; the fallback branch in the snippet covers absent names. Announcement behavior should get one live screen-reader pass (VoiceOver/NVDA) before Thursday — everything above is static analysis, not tested behavior.
- **Descoped per your instruction (keyboard):** noting only for the record — static analysis shows line-inspection via `poly.on('click', ...)` (app.js:435) has no keyboard path, though the same data appears for all lines in the ops panel, and `renderLayerToggles()` rebuilds the toggle form after route events, which can drop focus mid-task. Say the word if you want these written up.
- **Third-party scope:** Leaflet's internal markup wasn't audited (not studio-controlled). One wrapper-level nicety if you have 30 seconds: the focusable map container has no name — `document.getElementById('map').setAttribute('aria-label', 'Transit network map');`.
- Two sections you asked to drop (summary, open questions) are retained in minimal form because the studio hand-off format requires all four; they're three lines each.
</open_questions>

</accessibility_remediation_report>
