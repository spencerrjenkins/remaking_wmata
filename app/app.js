// Helper to fetch and parse GeoJSON
async function fetchGeoJSON(url) {
    const resp = await fetch(url);
    return await resp.json();
}
const currentLinesLayerNames = [];
const catchmentCircles = [];
// Color palette for lines
const COLORS = [
    "#e6194b", // red
    "#3cb44b", // green
    "#ffe119", // yellow
    "#4363d8", // blue
    "#f58231", // orange
    "#911eb4", // purple
    "#46f0f0", // cyan
    "#f032e6", // magenta
    "#bcf60c", // lime
    "#fabebe", // pink
    "#008080", // teal
    "#e6beff", // lavender
    "#9a6324", // brown
    "#800000", // maroon
    "#aaffc3", // mint
    "#808000", // olive
    "#ffd8b1", // peach
    "#000075", // navy
    "#808080", // gray
    "#000000", // black
    "#a9a9a9", // dark gray
    "#ff4500", // orange red
    "#2e8b57", // sea green
    "#1e90ff", // dodger blue
    "#ff69b4", // hot pink
    "#7cfc00", // lawn green
    "#8a2be2", // blue violet
    "#00ced1"  // dark turquoise
];
const map = L.map('map').setView([38.9, -77.05], 10);
var Stadia_AlidadeSmooth = L.tileLayer('https://tiles.stadiamaps.com/tiles/alidade_smooth/{z}/{x}/{y}{r}.{ext}', {
    minZoom: 0,
    maxZoom: 20,
    attribution: '&copy; <a href="https://www.stadiamaps.com/" target="_blank">Stadia Maps</a> &copy; <a href="https://openmaptiles.org/" target="_blank">OpenMapTiles</a> &copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    ext: 'png'
}).addTo(map);

const lineMarkers = {};
const layers = {};
const layerToggles = document.getElementById('layer-toggles');
// Maintain order of layers for toggles
const layerOrder = [];
const lineLayerNames = [];
const selectedLineDetails = document.getElementById('selected-line-details');

// Utility: round coordinate for matching
function roundCoord(coord) {
    return [Number(coord[0].toFixed(6)), Number(coord[1].toFixed(6))].sort();
}

function getStationNameByCoord(coord, fallback = 'Unnamed station') {
    const key = roundCoord(coord).join(',');
    return stationNameByCoord[key] || fallback;
}

// Store which lines stop at each vertex
const vertexLineMap = {};
const vertexKDEMap = {};
const lineMetadataById = {};
let selectedLineId = null;

function escapeHtml(value) {
    return String(value ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

function toNumberOrNull(value) {
    const num = Number(value);
    return Number.isFinite(num) ? num : null;
}

function formatPercent(value) {
    const num = toNumberOrNull(value);
    return num === null ? 'N/A' : `${Math.round(num)}%`;
}

function formatMinutes(value) {
    const num = toNumberOrNull(value);
    return num === null ? 'N/A' : `${num.toFixed(0)} min`;
}

function formatDollars(value) {
    const num = toNumberOrNull(value);
    return num === null ? 'N/A' : `$${num.toFixed(1)}M`;
}

function normalizeLineMetadata(props = {}) {
    return {
        lineId: props.line_id,
        name: props.name || `Line ${props.line_id}`,
        routeKind: props.route_kind || 'generated',
        serviceStatus: props.service_status || 'planned',
        occupancyPct: toNumberOrNull(props.occupancy_pct),
        delayMin: toNumberOrNull(props.delay_min),
        accessibilityScore: toNumberOrNull(props.accessibility_score),
        isAccessible: props.is_accessible,
        rowType: props.row_type || 'unspecified',
        constructionCostMusd: toNumberOrNull(props.construction_cost_musd),
        ridershipEstimate: toNumberOrNull(props.ridership_estimate),
    };
}

function statusChipClass(status) {
    if (status === 'ok') return 'ok';
    if (status === 'warn') return 'warn';
    if (status === 'alert') return 'alert';
    return 'muted';
}

const STATUS_MARKS = { ok: '✓', warn: '▲', alert: '✖' };
const STATUS_WORDS = { ok: 'normal', warn: 'elevated', alert: 'critical' };
function chipHtml(statusKey, text) {
    const cls = statusChipClass(statusKey);
    const mark = STATUS_MARKS[cls] ? `<span aria-hidden="true">${STATUS_MARKS[cls]}&thinsp;</span>` : '';
    const word = STATUS_WORDS[cls] ? `<span class="visually-hidden"> — ${STATUS_WORDS[cls]}</span>` : '';
    return `<span class="chip ${cls}">${mark}${text}${word}</span>`;
}

function buildOperationalChips(meta) {
    const crowdingClass = meta.occupancyPct !== null && meta.occupancyPct >= 80 ? 'alert' : meta.occupancyPct !== null && meta.occupancyPct >= 60 ? 'warn' : 'ok';
    const delayClass = meta.delayMin !== null && meta.delayMin >= 10 ? 'alert' : meta.delayMin !== null && meta.delayMin >= 5 ? 'warn' : 'ok';
    const accessibilityClass = meta.isAccessible === false ? 'warn' : meta.isAccessible === true ? 'ok' : 'muted';
    const chips = [];
    chips.push(`<span class="chip muted">${escapeHtml(meta.routeKind)}</span>`);
    chips.push(`<span class="chip muted">${escapeHtml(meta.serviceStatus)}</span>`);
    chips.push(chipHtml(crowdingClass, `Crowding ${escapeHtml(formatPercent(meta.occupancyPct))}`));
    chips.push(chipHtml(delayClass, `Delay ${escapeHtml(formatMinutes(meta.delayMin))}`));
    chips.push(`<span class="chip ${statusChipClass(accessibilityClass)}">${meta.isAccessible === false ? 'Not accessible' : meta.isAccessible === true ? 'Accessible' : 'Accessibility N/A'}</span>`);
    chips.push(`<span class="chip muted">ROW ${escapeHtml(meta.rowType)}</span>`);
    if (meta.constructionCostMusd !== null) chips.push(`<span class="chip muted">Cost ${escapeHtml(formatDollars(meta.constructionCostMusd))}</span>`);
    if (meta.ridershipEstimate !== null) chips.push(`<span class="chip muted">Ridership ${escapeHtml(meta.ridershipEstimate.toFixed(0))}</span>`);
    return chips.join(' ');
}

function renderTransitOperationsPanel(features) {
    const summaryPanel = document.getElementById('transit-ops-summary');
    const detailsPanel = document.getElementById('transit-line-details');
    if (!summaryPanel || !detailsPanel) return;

    const lines = (features || []).map(feature => {
        const meta = normalizeLineMetadata(feature.properties || {});
        const names = ((feature.properties || {}).name_list || []).filter(Boolean);
        meta.stationCount = ((feature.properties || {}).is_station || []).filter(Boolean).length;
        meta.termini = names.length ? `${names[0]} – ${names[names.length - 1]}` : '';
        return meta;
    });
    const crowdingCount = lines.filter(meta => meta.occupancyPct !== null).length;
    const delayCount = lines.filter(meta => meta.delayMin !== null).length;
    const accessibleCount = lines.filter(meta => meta.isAccessible !== null).length;
    const plannedCount = lines.filter(meta => meta.serviceStatus === 'planned').length;

    summaryPanel.innerHTML = [
        { label: 'Loaded lines', value: lines.length, note: 'Current network layers' },
        { label: 'Crowding data', value: crowdingCount, note: 'Lines with occupancy info' },
        { label: 'Delay data', value: delayCount, note: 'Lines with delay tracking' },
        { label: 'Accessibility data', value: accessibleCount, note: 'Lines with access flags' },
    ].map(card => `
        <div class="metric-card">
            <span class="metric-label">${escapeHtml(card.label)}</span>
            <div class="metric-value">${escapeHtml(card.value)}</div>
            <div class="metric-note">${escapeHtml(card.note)}</div>
        </div>
    `).join('');

    const sortedLines = lines.sort((a, b) => (a.lineId ?? 0) - (b.lineId ?? 0));
    detailsPanel.innerHTML = sortedLines.length ? sortedLines.map(meta => `
        <div class="line-detail-row">
            <div>
                <div class="line-detail-title">${escapeHtml(meta.name)}</div>
                <div class="line-detail-meta">${escapeHtml(meta.routeKind)} · ${escapeHtml(meta.serviceStatus)} · ${escapeHtml(meta.rowType)}${meta.termini ? ` · ${escapeHtml(meta.termini)}` : ''} · ${meta.stationCount} stations</div>
            </div>
            <div>${buildOperationalChips(meta)}</div>
        </div>
    `).join('') : `<div class="line-detail-row"><div class="line-detail-meta">No transit lines are loaded yet.</div></div>`;

    if (plannedCount > 0) {
        detailsPanel.insertAdjacentHTML('beforeend', `<div class="line-detail-row"><div class="line-detail-meta">${plannedCount} lines are still marked as planned.</div></div>`);
    }

    if (selectedLineDetails && !selectedLineId) {
        selectedLineDetails.innerHTML = '<div class="line-detail-row"><div class="line-detail-meta">Click a line to inspect crowding, delay, accessibility, and cost fields.</div></div>';
    }
}

function renderSelectedLineDetails(props) {
    const detailsPanel = selectedLineDetails;
    if (!detailsPanel || !props) return;
    const meta = normalizeLineMetadata(props);
    selectedLineId = meta.lineId;
    detailsPanel.innerHTML = `
        <div class="line-detail-row">
            <div>
                <div class="line-detail-title">${escapeHtml(meta.name)}</div>
                <div class="line-detail-meta">Selected line · ${escapeHtml(meta.routeKind)} · ${escapeHtml(meta.serviceStatus)}</div>
            </div>
            <div>${buildOperationalChips(meta)}</div>
        </div>
    `;
}

function summarizeRouteOperations(lineSequence) {
    const routeMeta = lineSequence.map(lineId => lineMetadataById[lineId]).filter(Boolean);
    if (!routeMeta.length) {
        return '<div class="metric-card"><span class="metric-label">Operations</span><div class="metric-value">No metadata</div><div class="metric-note">This route was generated without crowding or delay inputs.</div></div>';
    }
    const withCrowding = routeMeta.filter(meta => meta.occupancyPct !== null);
    const withDelays = routeMeta.filter(meta => meta.delayMin !== null);
    const withAccess = routeMeta.filter(meta => meta.isAccessible !== null);
    const avgCrowding = withCrowding.length ? withCrowding.reduce((sum, meta) => sum + meta.occupancyPct, 0) / withCrowding.length : null;
    const maxDelay = withDelays.length ? Math.max(...withDelays.map(meta => meta.delayMin)) : null;
    const accessible = withAccess.filter(meta => meta.isAccessible !== false).length;

    return `
        <div class="metric-grid">
            <div class="metric-card"><span class="metric-label">Avg crowding</span><div class="metric-value">${escapeHtml(formatPercent(avgCrowding))}</div><div class="metric-note">Across route segments with data</div></div>
            <div class="metric-card"><span class="metric-label">Max delay</span><div class="metric-value">${escapeHtml(formatMinutes(maxDelay))}</div><div class="metric-note">Worst loaded line on this trip</div></div>
            <div class="metric-card"><span class="metric-label">Accessible segments</span><div class="metric-value">${escapeHtml(accessible)}</div><div class="metric-note">Segments not marked inaccessible</div></div>
        </div>
    `;
}

// Dataset mode: 'output' for normal network, 'output_subway' for Subway restaurants
let datasetMode = 'output';

function loadNetworkGeoJSON(outputDir) {
    if (layers['Network']) {
        if (map.hasLayer(layers['Network'])) map.removeLayer(layers['Network']);
        delete layers['Network'];
        const idx = layerOrder.indexOf('Network');
        if (idx !== -1) layerOrder.splice(idx, 1);
    }
    fetchGeoJSON(`../data/${outputDir}/network.geojson`).then(data => {
        const networkLayer = L.geoJSON(data, {
            style: feature => {
                if (feature.geometry.type === 'LineString') {
                    return { color: '#222', weight: 1, opacity: 0.5, dashArray: '4 4' };
                }
                return {};
            },
            pointToLayer: (feature, latlng) => L.circleMarker(latlng, { radius: 2, color: '#222', fillOpacity: 0.5 })
        });
        layers['Network'] = networkLayer;
        // Do not add to map by default
        layerOrder.push('Network');
    });
}

// Load network on page load
loadNetworkGeoJSON(datasetMode);

// Add support for toggling
let currentLinesSource = 'lines_genetic';

function getOffsetLatLngs(latlngA, latlngB, offsetMeters, map, direction = 1) {
    // Compute perpendicular offset for a segment from latlngA to latlngB
    // direction: +1 for right, -1 for left (relative to A->B)
    const toRad = deg => deg * Math.PI / 180;
    const toDeg = rad => rad * 180 / Math.PI;
    // Convert to radians
    const lat1 = toRad(latlngA[0]), lng1 = toRad(latlngA[1]);
    const lat2 = toRad(latlngB[0]), lng2 = toRad(latlngB[1]);
    // Calculate bearing
    const dLng = lng2 - lng1;
    const y = Math.sin(dLng) * Math.cos(lat2);
    const x = Math.cos(lat1) * Math.sin(lat2) - Math.sin(lat1) * Math.cos(lat2) * Math.cos(dLng);
    const bearing = Math.atan2(y, x);
    // Perpendicular bearing (right-hand rule)
    const perpBearing = bearing + direction * Math.PI / 2;
    // Offset both points
    function offsetPoint(lat, lng, bearing, distMeters) {
        const R = 6378137; // Earth radius in meters
        const newLat = Math.asin(Math.sin(lat) * Math.cos(distMeters / R) + Math.cos(lat) * Math.sin(distMeters / R) * Math.cos(bearing));
        const newLng = lng + Math.atan2(Math.sin(bearing) * Math.sin(distMeters / R) * Math.cos(lat), Math.cos(distMeters / R) - Math.sin(lat) * Math.sin(newLat));
        return [toDeg(newLat), toDeg(newLng)];
    }
    // Scale offset by zoom: higher zoom = smaller offset in meters
    let zoom = map.getZoom ? map.getZoom() : 10;
    // More aggressive scaling: increase exponent
    const scale = Math.pow(2, 1 - zoom);
    const scaledOffset = offsetMeters * scale;
    return [
        offsetPoint(lat1, lng1, perpBearing, scaledOffset),
        offsetPoint(lat2, lng2, perpBearing, scaledOffset)
    ];
}

function loadLinesGeoJSON(source) {
    // Remove existing line layers and markers
    for (var member in vertexLineMap) delete vertexLineMap[member];
    for (var member in lineMetadataById) delete lineMetadataById[member];
    selectedLineId = null;
    // Remove all old catchment circles from the map before clearing
    catchmentCircles.forEach(circle => { if (map.hasLayer(circle)) map.removeLayer(circle); });
    currentLinesLayerNames.forEach(name => {
        if (layers[name]) map.removeLayer(layers[name]);
        if (lineMarkers[name]) lineMarkers[name].forEach(m => map.removeLayer(m));
        delete layers[name];
        delete lineMarkers[name];
    });
    catchmentCircles.length = 0;
    currentLinesLayerNames.length = 0;
    // Load the selected lines file
    // Load lines and add tooltips, vertex markers, and circles
    fetchGeoJSON(`../data/${datasetMode}/${source}.geojson`).then(data => {
        // First, build a map of vertex => [line_ids], and vertex => kde
        (data.features || []).forEach((feature, i) => {
            const coords = feature.geometry.coordinates;
            const kdeValues = feature.properties.kde_values || [];
            lineMetadataById[feature.properties.line_id] = normalizeLineMetadata(feature.properties);
            coords.forEach((coord, idx) => {
                const key = roundCoord(coord).join(',');
                if (!vertexLineMap[key]) vertexLineMap[key] = [];
                vertexLineMap[key].push(feature.properties.line_id);
                if (!vertexKDEMap[key]) vertexKDEMap[key] = kdeValues[idx];
            });
        });
        // Populate stationStatusByCoord from is_station attributes
        (data.features || []).forEach((feature, i) => {
            const coords = feature.geometry.coordinates;
            const isStationArr = feature.properties.is_station || [];
            const nameList = feature.properties.name_list || [];
            coords.forEach((coord, idx) => {
                const key = roundCoord(coord).join(',');
                if (isStationArr.length > idx) {
                    stationStatusByCoord[key] = isStationArr[idx];
                } else {
                    stationStatusByCoord[key] = true; // fallback: treat as station
                }
                if (nameList[idx]) {
                    stationNameByCoord[key] = nameList[idx];
                }
            });
        });
        // Build segment-to-lines map (direction-agnostic)
        const segmentToLines = {};
        (data.features || []).forEach((feature) => {
            const coords = feature.geometry.coordinates;
            const lineId = feature.properties.line_id;
            for (let i = 0; i < coords.length - 1; ++i) {
                const a = coords[i], b = coords[i + 1];
                // Use sorted key so [A,B] and [B,A] are the same segment
                const key = [a.join(','), b.join(',')].sort().join('|');
                if (!segmentToLines[key]) segmentToLines[key] = new Set();
                segmentToLines[key].add(lineId);
            }
        });
        const numStationsArray = new Array(data.features.length);
        // Draw lines as offset polylines for overlapping segments
        (data.features || []).forEach((feature, i) => {
            const group = feature.properties.group;
            const color = COLORS[group % COLORS.length];
            const name = `Line ${feature.properties.line_id ?? i}`;
            lineMarkers[name] = [];
            const coords = feature.geometry.coordinates;
            let polylines = [];
            const groupMap = {};
            for (let j = 0; j < coords.length - 1; ++j) {
                const a = coords[j], b = coords[j + 1];
                // Use direction-agnostic key
                const key = [a.join(','), b.join(',')].sort().join('|');
                // Only consider lines in different groups for separation
                const allLines = Array.from(segmentToLines[key] || []);
                // Get group for each line on this segment

                (data.features || []).forEach(f => {
                    if (allLines.includes(f.properties.line_id)) {
                        groupMap[f.properties.line_id] = f.properties.group;
                    }
                });
                // Only offset between groups
                // Get unique groups and their line_ids
                const groupToLines = {};
                allLines.forEach(lid => {
                    const g = groupMap[lid];
                    if (!groupToLines[g]) groupToLines[g] = [];
                    groupToLines[g].push(lid);
                });
                // Sort groups by group id (for consistent order)
                const groupIds = Object.keys(groupToLines).sort((a, b) => a - b);
                // Find this line's group index
                const myGroupIdx = groupIds.indexOf(String(group));
                // Offset: only if there are multiple groups
                let offset = 0;
                if (groupIds.length > 1) {
                    // Offset by group, not by line
                    offset = (myGroupIdx - (groupIds.length - 1) / 2) * 600000;
                }
                const seg = getOffsetLatLngs([a[1], a[0]], [b[1], b[0]], offset, map, 1);
                if (seg && seg[0] && seg[1]) {
                    polylines.push(seg);
                }
            }
            // Only draw if we have valid segments
            if (polylines.length > 0 && polylines[0][0] && polylines[0][1]) {
                let latlngs = [polylines[0][0]];
                for (let j = 0; j < polylines.length; ++j) {
                    if (polylines[j][1]) latlngs.push(polylines[j][1]);
                }
                const poly = L.polyline(latlngs, { color, weight: 4, opacity: 1 }).addTo(map);
                layers[name] = poly;
                lineLayerNames.push(name);
                // Store original data for offset recalculation
                poly._originalCoords = coords;
                poly._group = group;
                poly._segmentToLines = segmentToLines;
                poly._groupMap = groupMap;
                // Restore tooltip and click/hover interactivity
                const totalDistance = (feature.properties.segment_lengths || []).reduce((a, b) => a + b, 0);
                numStationsArray[i] = feature.properties.is_station.filter(Boolean).length;
                const nameList = feature.properties.name_list || [];
                const firstStationName = nameList.find(n => n) || 'Unnamed station';
                const lastStationName = [...nameList].reverse().find(n => n) || 'Unnamed station';
                poly.bindTooltip(
                    `<b>Line ${feature.properties.line_id} (${firstStationName.replace(/ \d+$/, '')} - ${lastStationName.replace(/ \d+$/, '')})</b><br>Total distance: ${(totalDistance / 1000).toFixed(2)} km<br>Stations: ${numStationsArray[i]}`,
                    {
                        sticky: true,
                        direction: 'top',
                        offset: [0, -10]
                    }
                );
                poly.on('mouseover', function (e) {
                    this.setStyle({ weight: 7, opacity: 1 });
                    var list = document.querySelector("#map > div.leaflet-pane.leaflet-map-pane > div.leaflet-pane.leaflet-overlay-pane > svg > g").getElementsByClassName("leaflet-interactive");
                    for (let item of list) {
                        if (item.getAttribute("stroke") != this.options.color)
                            item.setAttribute("opacity", 0.2);
                    }
                });
                poly.on('mouseout', function (e) {
                    this.setStyle({ weight: 4, opacity: 1 });
                    var list = document.querySelector("#map > div.leaflet-pane.leaflet-map-pane > div.leaflet-pane.leaflet-overlay-pane > svg > g").getElementsByClassName("leaflet-interactive");
                    for (let item of list) {
                        if (item.getAttribute("stroke") != this.options.color)
                            item.setAttribute("opacity", 1);
                    }
                });
                poly.on('click', function (e) {
                    renderSelectedLineDetails(feature.properties);
                });
            }
            // Add vertex markers, popups, and circles
            const kdeValues = feature.properties.kde_values || [];
            coords.forEach((coord, idx) => {
                const key = roundCoord(coord).join(',');
                const kde = vertexKDEMap[key];
                const linesHere = vertexLineMap[key] || [];
                const isStation = feature.properties.is_station ? feature.properties.is_station[idx] : true;
                const stationName = feature.properties.name_list && feature.properties.name_list[idx] ? feature.properties.name_list[idx] : 'Unnamed station';
                if (!isStation) return; // Only add marker if node is a station
                const icon = L.icon({
                    iconUrl: 'assets/wmata.svg',
                    iconSize: [14, 14],
                    iconAnchor: [7, 7],
                    popupAnchor: [0, -7],
                });
                const marker = L.marker([coord[1], coord[0]], { icon }).addTo(map);
                attachRouteFinderToMarker(marker, coord[1], coord[0], stationName);
                const tooltip = `<b>${stationName}</b><br>KDE Score: ${kde?.toFixed(2) ?? 'N/A'}<br>Lines: ${linesHere.map(l => `Line ${l}`).join(', ')}`;
                marker.bindTooltip(tooltip, { direction: 'top', offset: [0, -10], sticky: false });
                lineMarkers[name].push(marker);
                // Circle of 700m
                const circle = L.circle([coord[1], coord[0]], {
                    radius: 700,
                    color: color,
                    fill: false,
                    weight: 1,
                    opacity: 0.3
                });
                catchmentCircles.push(circle);
                // Do not add to map by default
            });
            currentLinesLayerNames.push(name);
        });
        // After all lines are loaded, sort lineLayerNames numerically
        currentLinesLayerNames.sort((a, b) => {
            const numA = parseInt(a.replace('Line ', ''));
            const numB = parseInt(b.replace('Line ', ''));
            return numA - numB;
        });
        // Update layerOrder: all lines in order, then Network last
        layerOrder.length = 0;
        lineLayerNames.forEach(n => layerOrder.push(n));
        if (layers['Network']) layerOrder.push('Network');
        // Re-render toggles
        renderLayerToggles();
        renderTransitOperationsPanel(data.features || []);
        linesGraph = { nodes: {}, edges: {} };
        (data.features || []).forEach(f => {
            const coords = f.geometry.coordinates;
            const lineId = f.properties.line_id;
            for (let i = 0; i < coords.length; ++i) {
                const latlng = [coords[i][1], coords[i][0]];
                const key = latlng.join(',');
                linesGraph.nodes[key] = latlng;
                if (!nodeToLineIds[key]) nodeToLineIds[key] = new Set();
                nodeToLineIds[key].add(lineId);
            }
            for (let i = 0; i < coords.length - 1; ++i) {
                const a = [coords[i][1], coords[i][0]].join(',');
                const b = [coords[i + 1][1], coords[i + 1][0]].join(',');
                const dist = L.latLng(coords[i][1], coords[i][0]).distanceTo(L.latLng(coords[i + 1][1], coords[i + 1][0]));
                if (!linesGraph.edges[a]) linesGraph.edges[a] = [];
                if (!linesGraph.edges[b]) linesGraph.edges[b] = [];
                linesGraph.edges[a].push({ to: b, dist, lineId });
                linesGraph.edges[b].push({ to: a, dist, lineId });
                // For description
                const pairKey = [a, b].sort().join('|');
                if (!lineIdByNodePair[pairKey]) lineIdByNodePair[pairKey] = new Set();
                lineIdByNodePair[pairKey].add(lineId);
            }
        });
        // --- Ensure catchment circles are shown if toggle is checked ---
        if (catchmentToggle.checked) {
            catchmentCircles.forEach(circle => {
                if (!map.hasLayer(circle)) map.addLayer(circle);
            });
        }
    });
}

// Dataset selector: switch between normal network and Subway restaurants network
const datasetSelect = document.createElement('select');
datasetSelect.id = 'dataset-select';
datasetSelect.setAttribute('aria-label', 'Dataset');
[['output', 'Normal Network'], ['output_subway', 'Subway Restaurants']].forEach(([val, label]) => {
    const opt = document.createElement('option');
    opt.value = val;
    opt.textContent = label;
    datasetSelect.appendChild(opt);
});
datasetSelect.onchange = function () {
    datasetMode = this.value;
    loadNetworkGeoJSON(datasetMode);
    loadLinesGeoJSON(currentLinesSource);
};
document.getElementById('controls').appendChild(datasetSelect);

// Add UI for toggling
const linesSourceSelect = document.createElement('select');
linesSourceSelect.id = 'lines-source-select';
linesSourceSelect.setAttribute('aria-label', 'Line-generation algorithm');
['lines_iterative', 'lines_genetic', 'lines_naive', 'lines_aco'].forEach(src => {
    const opt = document.createElement('option');
    opt.value = src;
    opt.textContent = src
        .replace('lines', '')
        .replace('_', ' ')
        .replace('naive', 'Naive Algorithm')
        .replace('iterative', 'Naive Algorithm with Iterative Improvement')
        .replace('genetic', 'Genetic Algorithm')
        .replace('aco', 'Ant Colony Optimization');
    linesSourceSelect.appendChild(opt);
});
linesSourceSelect.onchange = function () {
    currentLinesSource = this.value;
    loadLinesGeoJSON(currentLinesSource);
};
document.getElementById('controls').appendChild(linesSourceSelect);

// On page load, load default lines
loadLinesGeoJSON(currentLinesSource);

// Store is_station info for each node globally
const stationStatusByCoord = {};
const stationNameByCoord = {};

// --- Real-World Transit Network Layer ---
let realWorldNetworkLayer = null;
function loadRealWorldNetwork() {
    // Remove previous layer if present
    if (realWorldNetworkLayer) {
        map.removeLayer(realWorldNetworkLayer);
        realWorldNetworkLayer = null;
    }
    // List of real-world network GeoJSONs and color logic
    const files = [
        {
            url: '../data/real_transit/dcs/DC_Streetcar_Routes.geojson',
            color: 'brown',
            name: 'DC Streetcar'
        },
        {
            url: '../data/real_transit/marc/Maryland_Transit_-_MARC_Train_Lines.geojson',
            colorFn: function (props) {
                if (props.Rail_Name && props.Rail_Name.includes('Brunswick')) return '#EFAD1D';
                if (props.Rail_Name && props.Rail_Name.includes('Camden')) return '#F15828';
                return '#C71F3E';
            },
            name: 'MARC Train'
        },
        {
            url: '../data/real_transit/wmata/Metro_Lines_Regional.geojson',
            colorFn: function (props) {
                if (props.NAME && props.NAME.includes('orange')) return '#F9921D';
                if (props.NAME && props.NAME.includes('silver')) return '#A1A3A1';
                if (props.NAME && props.NAME.includes('red')) return '#E41838';
                if (props.NAME && props.NAME.includes('yellow')) return '#FED201';
                if (props.NAME && props.NAME.includes('green')) return '#01A850';
                return '#0077C1';
            },
            name: 'WMATA Metro'
        },
        {
            url: '../data/real_transit/vre/Virginia_Railway_Express_Routes.geojson',
            colorFn: function (props) {
                if (props.RAILWAY_NM && props.RAILWAY_NM.includes('Manassas')) return '#156DB4';
                return '#DD3534';
            },
            name: 'VRE'
        },
        {
            url: '../data/real_transit/pl/PurpleLineAlignment.geojson',
            color: '#793390',
            name: 'Purple Line'
        }
    ];
    // Load all files and add to map as a single layer group
    Promise.all(files.map(f => fetchGeoJSON(f.url).then(data => ({ ...f, data }))))

        .then(layers => {
            const group = L.layerGroup();
            layers.forEach(f => {
                const colorFn = f.colorFn || (() => f.color);
                const geoLayer = L.geoJSON(f.data, {
                    style: feature => ({
                        color: colorFn(feature.properties),
                        weight: 2, // Set weight to 2 for real-world network
                        opacity: 1,
                        dashArray: '2 2',
                    })
                });
                group.addLayer(geoLayer);
            });
            realWorldNetworkLayer = group;
            // Add to map if toggled on
            const cb = document.getElementById('layer-toggle-RealWorldNetwork');
            if (cb && cb.checked) realWorldNetworkLayer.addTo(map);
        });
}
// Add checkbox for real-world network
function createRealWorldNetworkToggle() {
    const id = 'layer-toggle-RealWorldNetwork';
    let label = document.getElementById('realworld-toggle-label');
    if (!label) {
        label = document.createElement('label');
        label.id = 'realworld-toggle-label';
        const checkbox = document.createElement('input');
        checkbox.type = 'checkbox';
        checkbox.id = id;
        checkbox.checked = false;
        checkbox.onchange = () => {
            if (checkbox.checked) {
                if (!realWorldNetworkLayer) loadRealWorldNetwork();
                else realWorldNetworkLayer.addTo(map);
            } else {
                if (realWorldNetworkLayer) map.removeLayer(realWorldNetworkLayer);
            }
        };
        label.appendChild(checkbox);
        label.appendChild(document.createTextNode(' Real-world transit network'));
        layerToggles.appendChild(label);
    }
}
// Add toggle all lines checkbox
function createToggleAllLinesCheckbox(parent) {
    let label = document.getElementById('toggle-all-lines-label');
    if (!label) {
        label = document.createElement('label');
        label.id = 'toggle-all-lines-label';
        const checkbox = document.createElement('input');
        checkbox.type = 'checkbox';
        checkbox.id = 'toggle-all-lines';
        checkbox.checked = currentLinesLayerNames.every(name => {
            const cb = document.getElementById(`layer-toggle-${name.replace(/\s/g, '-')}`);
            return cb && cb.checked;
        });
        checkbox.onchange = () => {
            const checked = checkbox.checked;
            currentLinesLayerNames.forEach(name => {
                const cb = document.getElementById(`layer-toggle-${name.replace(/\s/g, '-')}`);
                if (cb && !cb.disabled) {
                    cb.checked = checked;
                    cb.onchange();
                }
            });
        };
        label.appendChild(checkbox);
        label.appendChild(document.createTextNode(' Toggle all lines'));
        (parent || layerToggles).appendChild(label);
        checkbox.checked = true;
    } else {
        // Update checked state
        const checkbox = document.getElementById('toggle-all-lines');
        if (checkbox) {
            checkbox.checked = currentLinesLayerNames.every(name => {
                const cb = document.getElementById(`layer-toggle-${name.replace(/\s/g, '-')}`);
                return cb && cb.checked;
            });
        }
        (parent || layerToggles).appendChild(label);
    }
}

function renderLayerToggles() {
    // Clear toggles
    while (layerToggles.firstChild) layerToggles.removeChild(layerToggles.firstChild);

    // --- Group lines by group number ---
    // Build a map: groupNum -> [lineName, ...]
    const groupToLines = {};
    currentLinesLayerNames.forEach(name => {
        const poly = layers[name];
        if (!poly || poly._group === undefined) return;
        const group = poly._group;
        if (!groupToLines[group]) groupToLines[group] = [];
        groupToLines[group].push(name);
    });
    // Sort group numbers
    const groupNums = Object.keys(groupToLines).map(Number).sort((a, b) => a - b);
    // --- Create columns: at most 4 lines per column (including group headers) ---
    const columns = [];
    let col = document.createElement('div');
    col.className = 'layer-toggle-col';
    let linesInCol = 0;
    for (let i = 0; i < groupNums.length; ++i) {
        const groupNum = groupNums[i];
        const groupHeader = document.createElement('div');
        groupHeader.className = 'layer-toggle-group-header';
        groupHeader.textContent = `Group ${groupNum}`;
        const groupLines = groupToLines[groupNum];
        // If adding this group would exceed 4 lines in the column, start a new column
        if (linesInCol + 1 + groupLines.length > 4) {
            columns.push(col);
            col = document.createElement('div');
            col.className = 'layer-toggle-col';
            linesInCol = 0;
        }
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
    }
    if (col.childNodes.length > 0) columns.push(col);
    // Add all group columns to the toggles container
    columns.forEach(col => layerToggles.appendChild(col));

    // --- Rightmost column for global toggles and dropdown ---
    const rightCol = document.createElement('div');
    rightCol.className = 'layer-toggle-col rightmost';
    // Toggle all lines
    createToggleAllLinesCheckbox(rightCol);
    // Network toggle
    if (layers['Network']) createToggle('Network', map.hasLayer(layers['Network']), rightCol);
    // Catchment area toggle
    rightCol.appendChild(catchmentToggleLabel);
    // Real-world network toggle
    createRealWorldNetworkToggle(rightCol);
    layerToggles.appendChild(rightCol);

    // --- Style columns with flex ---
    layerToggles.style.display = 'flex';
    layerToggles.style.flexDirection = 'row';
    layerToggles.style.gap = '24px';

    // --- Ensure dropdown is always in controls div ---
    const controlsDiv = document.getElementById('controls');
    const dropdown = document.getElementById('lines-source-select');
    if (dropdown) {
        // Remove from any parent and append to controls
        if (dropdown.parentNode) dropdown.parentNode.removeChild(dropdown);
        controlsDiv.appendChild(dropdown);
    }
}

function createToggle(name, checked, parent) {
    const id = `layer-toggle-${name.replace(/\s/g, '-')}`;
    const label = document.createElement('label');
    const checkbox = document.createElement('input');
    checkbox.type = 'checkbox';
    checkbox.id = id;
    checkbox.checked = checked;
    checkbox.onchange = () => {
        if (checkbox.checked) {
            layers[name].addTo(map);
            if (lineMarkers[name]) lineMarkers[name].forEach(m => map.addLayer(m));
        } else {
            map.removeLayer(layers[name]);
            if (lineMarkers[name]) lineMarkers[name].forEach(m => map.removeLayer(m));
        }
    };
    label.appendChild(checkbox);
    label.appendChild(document.createTextNode(' ' + name));
    (parent || layerToggles).appendChild(label);
}

// Create Station catchment area toggle (define before any function uses it)
const catchmentToggleLabel = document.createElement('label');
const catchmentToggle = document.createElement('input');
catchmentToggle.type = 'checkbox';
catchmentToggle.id = 'catchment-toggle';
catchmentToggle.checked = false;
catchmentToggle.onchange = () => {
    catchmentCircles.forEach(circle => {
        if (catchmentToggle.checked) {
            if (!map.hasLayer(circle)) map.addLayer(circle);
        } else {
            if (map.hasLayer(circle)) map.removeLayer(circle);
        }
    });
};
catchmentToggleLabel.appendChild(catchmentToggle);
catchmentToggleLabel.appendChild(document.createTextNode(' Station catchment areas'));
// On page load, ensure circles are not shown
catchmentCircles.forEach(circle => { if (map.hasLayer(circle)) map.removeLayer(circle); });

// --- Route Finder Logic ---
let routeFinderState = 'idle'; // 'idle', 'selectingStart', 'selectingEnd'
let routeStart = null;
let routeEnd = null;
let routeHighlightLayer = null;
let routeNodeMarkers = [];
let linesGraph = null;
let nodeIdByLatLng = {};
let latLngByNodeId = {};
let lineIdByNodePair = {};
let nodeToLineIds = {};

const routeFinderBtn = document.getElementById('route-finder-btn');
const routeFinderStatus = document.getElementById('route-finder-status');
const routeFinderError = document.getElementById('route-finder-error');
const routeFinderResult = document.getElementById('route-finder-result');


// Helper: get currently visible line IDs
function getVisibleLineIds() {
    return currentLinesLayerNames.filter(name => {
        const checkbox = document.getElementById(`layer-toggle-${name.replace(/\s/g, '-')}`);
        return checkbox && checkbox.checked;
    }).map(name => parseInt(name.replace('Line ', '')));
}

routeFinderBtn.onclick = () => {
    if (routeFinderState === 'selectingStart' || routeFinderState === 'selectingEnd') {
        // Clear/reset
        routeFinderState = 'selectingStart';
        routeStart = null;
        routeEnd = null;
        routeFinderBtn.dataset.originStation = '';
        routeFinderBtn.dataset.destinationStation = '';
        if (routeHighlightLayer) { map.removeLayer(routeHighlightLayer); routeHighlightLayer = null; }
        routeNodeMarkers.forEach(m => map.removeLayer(m));
        routeNodeMarkers = [];
        routeFinderStatus.textContent = 'Click the starting location.';
        routeFinderError.textContent = '';
        routeFinderResult.textContent = '';
        routeFinderBtn.textContent = 'clear';
        routeFinderBtn.disabled = true; // Disable clear until origin is selected
        // Remove Exit button if present
        const exitBtn = document.getElementById('route-finder-exit-btn');
        if (exitBtn) exitBtn.remove();
        clearRouteSelectionVisuals(); // <-- ensure blue marker/line are removed
        return;
    }
    // Start route finder
    routeFinderState = 'selectingStart';
    routeStart = null;
    routeEnd = null;
    routeFinderBtn.dataset.originStation = '';
    routeFinderBtn.dataset.destinationStation = '';
    if (routeHighlightLayer) { map.removeLayer(routeHighlightLayer); routeHighlightLayer = null; }
    routeNodeMarkers.forEach(m => map.removeLayer(m));
    routeNodeMarkers = [];
    routeFinderStatus.textContent = 'Click the starting location.';
    routeFinderError.textContent = '';
    routeFinderResult.textContent = '';
    routeFinderBtn.textContent = 'clear';
    routeFinderBtn.disabled = true; // Disable clear until origin is selected
    // Remove Exit button if present
    const exitBtn = document.getElementById('route-finder-exit-btn');
    if (exitBtn) exitBtn.remove();
    clearRouteSelectionVisuals(); // <-- ensure blue marker/line are removed
};

// Helper: find nearest node in lines graph, but only for visible lines
function findNearestLineNodeVisible(lat, lng) {
    const visibleLineIds = new Set(getVisibleLineIds());
    let minDist = Infinity, minKey = null;
    for (const [key, ll] of Object.entries(linesGraph.nodes || {})) {
        // Only consider nodes on visible lines
        const linesHere = nodeToLineIds[key] ? Array.from(nodeToLineIds[key]) : [];
        if (!linesHere.some(lid => visibleLineIds.has(lid))) continue;
        const d = Math.abs(ll[0] - lat) + Math.abs(ll[1] - lng);
        if (d < minDist) { minDist = d; minKey = key; }
    }
    return minKey;
}

// Attach click handlers to station markers after they are created
function attachRouteFinderToMarker(marker, lat, lng, stationName) {
    marker.on('click', (e) => {
        if (routeFinderState === 'selectingStart') {
            routeStart = findNearestLineNodeVisible(lat, lng);
            if (!routeStart) return;
            routeFinderState = 'selectingEnd';
            // Save station name for origin
            routeFinderBtn.dataset.originStation = stationName || '';
            routeFinderStatus.innerHTML = `<b>Origin:</b> ${stationName || ''}<br>Click the destination.`;
            // Highlight start marker
            const m = L.circleMarker([lat, lng], { radius: 12, color: 'green', fillOpacity: 0.7 }).addTo(map);
            routeNodeMarkers.push(m);
            routeFinderBtn.textContent = 'clear';
            routeFinderBtn.disabled = false; // Enable clear after origin is selected
            renderLayerToggles();
        } else if (routeFinderState === 'selectingEnd') {
            routeEnd = findNearestLineNodeVisible(lat, lng);
            if (!routeEnd || routeEnd === routeStart) return;
            routeFinderState = 'idle';
            // Save station name for destination
            routeFinderBtn.dataset.destinationStation = stationName || '';
            // Highlight end marker
            const m = L.circleMarker([lat, lng], { radius: 12, color: 'red', fillOpacity: 0.7 }).addTo(map);
            routeNodeMarkers.push(m);
            routeFinderStatus.textContent = 'Finding route...';
            routeFinderError.textContent = '';
            // Only consider visible lines
            const visibleLineIds = new Set(getVisibleLineIds());
            const path = dijkstraLinesVisible(linesGraph, routeStart, routeEnd, visibleLineIds);
            if (path.length < 2) {
                routeFinderStatus.textContent = '';
                routeFinderError.textContent = 'No route found.';
                return;
            }
            // Draw route
            const routeCoords = [];
            let totalRouteDistance = 0;
            for (let i = 0; i < path.length - 1; ++i) {
                const from = path[i], to = path[i + 1];
                routeCoords.push(linesGraph.nodes[from]);
                // Calculate distance for this segment
                const edge = (linesGraph.edges[from] || []).find(e => e.to === to);
                if (edge) totalRouteDistance += edge.dist;
            }
            routeCoords.push(linesGraph.nodes[path[path.length - 1]]);
            if (routeHighlightLayer) map.removeLayer(routeHighlightLayer);
            routeHighlightLayer = L.polyline(routeCoords, { color: 'blue', weight: 8, opacity: 0.7 }).addTo(map);
            // Show description with lines used and total distance
            let lineSequence = [];
            let currentLine = null;
            for (let i = 0; i < path.length - 1; ++i) {
                const a = path[i], b = path[i + 1];
                const pairKey = [a, b].sort().join('|');
                const lines = lineIdByNodePair[pairKey] ? Array.from(lineIdByNodePair[pairKey]) : [];
                if (lines.length === 0) continue;
                if (lineSequence.length === 0) {
                    // Start with the line that can be continued the longest
                    let bestLine = null, bestRun = -1;
                    for (const candidate of lines) {
                        let run = 1;
                        for (let j = i + 1; j < path.length - 1; ++j) {
                            const nextA = path[j], nextB = path[j + 1];
                            const nextKey = [nextA, nextB].sort().join('|');
                            const nextLines = lineIdByNodePair[nextKey] ? Array.from(lineIdByNodePair[nextKey]) : [];
                            if (!nextLines.includes(candidate)) break;
                            run++;
                        }
                        if (run > bestRun || (run === bestRun && (bestLine === null || candidate < bestLine))) {
                            bestRun = run;
                            bestLine = candidate;
                        }
                    }
                    currentLine = bestLine;
                    lineSequence.push(currentLine);
                } else {
                    // Continue on the current line if possible
                    if (lines.includes(currentLine)) {
                        continue;
                    } else {
                        // Choose the line that can be continued the longest from here
                        let bestLine = null, bestRun = -1;
                        for (const candidate of lines) {
                            let run = 1;
                            for (let j = i + 1; j < path.length - 1; ++j) {
                                const nextA = path[j], nextB = path[j + 1];
                                const nextKey = [nextA, nextB].sort().join('|');
                                const nextLines = lineIdByNodePair[nextKey] ? Array.from(lineIdByNodePair[nextKey]) : [];
                                if (!nextLines.includes(candidate)) break;
                                run++;
                            }
                            if (run > bestRun || (run === bestRun && (bestLine === null || candidate < bestLine))) {
                                bestRun = run;
                                bestLine = candidate;
                            }
                        }
                        currentLine = bestLine;
                        lineSequence.push(currentLine);
                    }
                }
            }
            routeFinderStatus.innerHTML = `<b>Origin:</b> ${routeFinderBtn.dataset.originStation || ''}<br><b>Destination:</b> ${routeFinderBtn.dataset.destinationStation || ''}`;
            // Calculate travel time: 80 km/h + 0.4 min per station + 6 min per transfer
            const speed_kmh = 80;
            const totalDistanceKm = totalRouteDistance / 1000;
            let transfers = 0;
            for (let i = 1; i < lineSequence.length; ++i) {
                if (lineSequence[i] !== lineSequence[i - 1]) transfers++;
            }
            const travelTimeHours = totalDistanceKm / speed_kmh;
            // Count number of stations in the path
            let stationCount = 0;
            for (let i = 0; i < path.length; ++i) {
                const node = path[i];
                const coord = linesGraph.nodes[node];
                if (!coord) continue;
                const key = roundCoord(coord).join(',');
                if (stationStatusByCoord[key] === undefined || stationStatusByCoord[key]) stationCount++;
            }
            // For travel time, replace (path.length - 2) * 0.4 with (stationCount - 2) * 0.4
            let travelTimeMinutes = travelTimeHours * 60 + Math.max(0, stationCount - 2) * 0.4 + transfers * 6;
            travelTimeMinutes = Math.ceil(travelTimeMinutes); // round up to next whole minute
            let travelTimeStr = `${travelTimeMinutes} min`;
            if (travelTimeMinutes >= 60) {
                const hours = Math.floor(travelTimeMinutes / 60);
                const minutes = travelTimeMinutes % 60;
                travelTimeStr = `${hours} hr${hours > 1 ? 's' : ''}` + (minutes > 0 ? ` ${minutes} min` : '');
            }
            // Add walk to route summary if needed
            let walkSummaryStart = '';
            let walkSummaryEnd = '';
            let walkDistance = clickStartDist + clickEndDist;
            let walkDistanceStrStart = '';
            let walkDistanceStrEnd = '';
            let walkTimeStr = '';
            if (clickStartDist > 0) {
                walkDistanceStrStart = `${(clickStartDist / 1000).toFixed(2)} km`;
                walkSummaryStart = `Walk (${walkDistanceStrStart}) → `;
            }
            if (clickEndDist > 0) {
                walkDistanceStrEnd = ` → Walk (${(clickEndDist / 1000).toFixed(2)} km)`;
            }
            if (walkDistance > 0) {
                const walkTime = Math.ceil(walkDistance / 100); // 100 meters per minute
                walkTimeStr = `${walkTime} min`;
            }
            const operationsSummary = summarizeRouteOperations(lineSequence);
            // Build list of intermediate station names
            let intermediateStations = [];
            for (let i = 1; i < path.length - 1; ++i) {
                const node = path[i];
                const coord = linesGraph.nodes[node];
                if (!coord) continue;
                const stationName = getStationNameByCoord(coord);
                intermediateStations.push(stationName);
            }
            // Add show/hide button for intermediate stations
            let intermediateListId = 'intermediate-stations-list';
            let showHideBtnId = 'show-hide-intermediate-btn';
            routeFinderResult.innerHTML =
                `<b>Route:</b> ${walkSummaryStart}${lineSequence.map(l => `Line ${l}`).join(' → ')}${walkDistanceStrEnd}` +
                `<br><b>Stations:</b> ${stationCount - 2}` +
                `<br><b>Total distance:</b> ${totalDistanceKm.toFixed(2)} km` +
                `<br><b>Transfers:</b> ${transfers}` +
                `<br><b>Estimated travel time:</b> ${travelTimeStr}` +
                operationsSummary +
                (walkDistance > 0 ? `<br><b>Total walking distance:</b> ${((clickStartDist + clickEndDist) / 1000).toFixed(2)} km` : '') +
                (walkDistance > 0 ? `<br><b>Total walking time:</b> ${walkTimeStr}` : '') +
                `<br><button id="${showHideBtnId}">Show intermediate stations</button><div id="${intermediateListId}" style="display:none;"></div>`;
            document.getElementById(showHideBtnId).onclick = function () {
                const div = document.getElementById(intermediateListId);
                if (div.style.display === 'none') {
                    div.style.display = 'block';
                    div.innerHTML = '<ol>' + intermediateStations.map(s => `<li>${s}</li>`).join('') + '</ol>';
                    this.textContent = 'Hide intermediate stations';
                } else {
                    div.style.display = 'none';
                    this.textContent = 'Show intermediate stations';
                }
            };
            // Add Exit Route Finder button
            let exitBtn = document.getElementById('route-finder-exit-btn');
            if (!exitBtn) {
                exitBtn = document.createElement('button');
                exitBtn.id = 'route-finder-exit-btn';
                exitBtn.textContent = 'Exit Route Finder';
                exitBtn.style.marginLeft = '10px';
                routeFinderBtn.parentNode.insertBefore(exitBtn, routeFinderBtn.nextSibling);
                exitBtn.onclick = () => {
                    routeFinderState = 'idle';
                    routeStart = null;
                    routeEnd = null;
                    routeFinderBtn.dataset.originStation = '';
                    routeFinderBtn.dataset.destinationStation = '';
                    if (routeHighlightLayer) { map.removeLayer(routeHighlightLayer); routeHighlightLayer = null; }
                    routeNodeMarkers.forEach(m => map.removeLayer(m));
                    routeNodeMarkers = [];
                    routeFinderStatus.textContent = '';
                    routeFinderError.textContent = '';
                    routeFinderResult.textContent = '';
                    routeFinderBtn.textContent = 'Route Finder';
                    exitBtn.remove();
                    renderLayerToggles();
                    clearRouteSelectionVisuals(); // <-- ensure blue marker/line are removed
                };
            }
            routeFinderBtn.textContent = 'clear';
            routeFinderState = 'results';
            renderLayerToggles();
        }
    });
}

// Dijkstra's algorithm for shortest path on lines graph, prioritizing fewer transfers
function dijkstraLinesVisible(graph, start, end, visibleLineIds) {
    // Each state: {node, cost, prev, line, transfers}
    const Q = new Set(Object.keys(graph.nodes));
    const dist = {}, prev = {}, prevLine = {}, transfers = {};
    for (const v of Q) {
        dist[v] = Infinity;
        transfers[v] = Infinity;
    }
    dist[start] = 0;
    transfers[start] = 0;
    prevLine[start] = null;
    while (Q.size) {
        // Find node with min (transfers, dist)
        let u = null, minTransfers = Infinity, minDist = Infinity;
        for (const v of Q) {
            if (
                transfers[v] < minTransfers ||
                (transfers[v] === minTransfers && dist[v] < minDist)
            ) {
                minTransfers = transfers[v];
                minDist = dist[v];
                u = v;
            }
        }
        if (u === null || u === end) break;
        Q.delete(u);
        for (const e of (graph.edges[u] || [])) {
            if (!visibleLineIds.has(e.lineId)) continue;
            const newTransfers = (prevLine[u] === null || prevLine[u] === e.lineId) ? transfers[u] : transfers[u] + 1;
            const alt = dist[u] + e.dist;
            if (
                newTransfers < transfers[e.to] ||
                (newTransfers === transfers[e.to] && alt < dist[e.to])
            ) {
                dist[e.to] = alt;
                prev[e.to] = u;
                prevLine[e.to] = e.lineId;
                transfers[e.to] = newTransfers;
            }
        }
    }
    // Reconstruct path
    const path = [];
    let u = end;
    while (u !== undefined) { path.unshift(u); u = prev[u]; }
    if (path[0] != start) return [];
    return path;
}

let clickMarkerStart = null;
let clickToStationLineStart = null;
let clickMarkerEnd = null;
let clickToStationLineEnd = null;
let clickStartDist = 0;
let clickEndDist = 0;

function clearRouteSelectionVisuals() {
    if (clickMarkerStart) { map.removeLayer(clickMarkerStart); clickMarkerStart = null; }
    if (clickToStationLineStart) { map.removeLayer(clickToStationLineStart); clickToStationLineStart = null; }
    if (clickMarkerEnd) { map.removeLayer(clickMarkerEnd); clickMarkerEnd = null; }
    if (clickToStationLineEnd) { map.removeLayer(clickToStationLineEnd); clickToStationLineEnd = null; }
    clickStartDist = 0;
    clickEndDist = 0;
}

map.on('click', function (e) {
    if (routeFinderState === 'selectingStart') {
        clearRouteSelectionVisuals();
        const lat = e.latlng.lat;
        const lng = e.latlng.lng;
        // Find nearest station using rbush
        const nearestKey = findNearestLineNodeVisible(lat, lng);
        if (!nearestKey) return;
        const nearestCoord = linesGraph.nodes[nearestKey];
        // Place marker at click location
        clickMarkerStart = L.circleMarker([lat, lng], { radius: 10, color: 'blue', fillOpacity: 0.5 }).addTo(map);
        // Draw blue dotted line
        clickToStationLineStart = L.polyline([[lat, lng], [nearestCoord[0], nearestCoord[1]]], { color: 'blue', weight: 3, dashArray: '6 6', opacity: 0.7 }).addTo(map);
        // Save distance in meters
        clickStartDist = map.distance([lat, lng], [nearestCoord[0], nearestCoord[1]]);
        // Save as start
        routeStart = nearestKey;
        routeFinderState = 'selectingEnd';
        const originName = getStationNameByCoord(nearestCoord);
        routeFinderBtn.dataset.originStation = originName;
        routeFinderStatus.innerHTML = `<b>Origin:</b> ${originName}<br>Click the destination.`;
        const m = L.circleMarker([nearestCoord[0], nearestCoord[1]], { radius: 12, color: 'green', fillOpacity: 0.7 }).addTo(map);
        routeNodeMarkers.push(m);
        routeFinderBtn.textContent = 'clear';
        routeFinderBtn.disabled = false;
        renderLayerToggles();
    } else if (routeFinderState === 'selectingEnd') {
        // Remove previous destination visuals if any
        if (clickMarkerEnd) { map.removeLayer(clickMarkerEnd); clickMarkerEnd = null; }
        if (clickToStationLineEnd) { map.removeLayer(clickToStationLineEnd); clickToStationLineEnd = null; }
        const lat = e.latlng.lat;
        const lng = e.latlng.lng;
        // Find nearest station using rbush
        const nearestKey = findNearestLineNodeVisible(lat, lng);
        if (!nearestKey || nearestKey === routeStart) return;
        const nearestCoord = linesGraph.nodes[nearestKey];
        // Place marker at click location
        clickMarkerEnd = L.circleMarker([lat, lng], { radius: 10, color: 'blue', fillOpacity: 0.5 }).addTo(map);
        // Draw blue dotted line from click to nearest station
        clickToStationLineEnd = L.polyline([[lat, lng], [nearestCoord[0], nearestCoord[1]]], { color: 'blue', weight: 3, dashArray: '6 6', opacity: 0.7 }).addTo(map);
        // Save distance in meters
        clickEndDist = map.distance([lat, lng], [nearestCoord[0], nearestCoord[1]]);
        // Save as end
        routeEnd = nearestKey;
        const destinationName = getStationNameByCoord(nearestCoord);
        routeFinderBtn.dataset.destinationStation = destinationName;
        routeFinderState = 'idle';
        const m = L.circleMarker([nearestCoord[0], nearestCoord[1]], { radius: 12, color: 'red', fillOpacity: 0.7 }).addTo(map);
        routeNodeMarkers.push(m);
        routeFinderStatus.textContent = 'Finding route...';
        routeFinderError.textContent = '';
        // Only consider visible lines
        const visibleLineIds = new Set(getVisibleLineIds());
        const path = dijkstraLinesVisible(linesGraph, routeStart, routeEnd, visibleLineIds);
        if (path.length < 2) {
            routeFinderStatus.textContent = '';
            routeFinderError.textContent = 'No route found.';
            return;
        }
        // Draw route
        const routeCoords = [];
        let totalRouteDistance = 0;
        for (let i = 0; i < path.length - 1; ++i) {
            const from = path[i], to = path[i + 1];
            routeCoords.push(linesGraph.nodes[from]);
            // Calculate distance for this segment
            const edge = (linesGraph.edges[from] || []).find(e => e.to === to);
            if (edge) totalRouteDistance += edge.dist;
        }
        routeCoords.push(linesGraph.nodes[path[path.length - 1]]);
        if (routeHighlightLayer) map.removeLayer(routeHighlightLayer);
        routeHighlightLayer = L.polyline(routeCoords, { color: 'blue', weight: 8, opacity: 0.7 }).addTo(map);
        // Show description with lines used and total distance
        let lineSequence = [];
        let currentLine = null;
        for (let i = 0; i < path.length - 1; ++i) {
            const a = path[i], b = path[i + 1];
            const pairKey = [a, b].sort().join('|');
            const lines = lineIdByNodePair[pairKey] ? Array.from(lineIdByNodePair[pairKey]) : [];
            if (lines.length === 0) continue;
            if (lineSequence.length === 0) {
                // Start with the line that can be continued the longest
                let bestLine = null, bestRun = -1;
                for (const candidate of lines) {
                    let run = 1;
                    for (let j = i + 1; j < path.length - 1; ++j) {
                        const nextA = path[j], nextB = path[j + 1];
                        const nextKey = [nextA, nextB].sort().join('|');
                        const nextLines = lineIdByNodePair[nextKey] ? Array.from(lineIdByNodePair[nextKey]) : [];
                        if (!nextLines.includes(candidate)) break;
                        run++;
                    }
                    if (run > bestRun || (run === bestRun && (bestLine === null || candidate < bestLine))) {
                        bestRun = run;
                        bestLine = candidate;
                    }
                }
                currentLine = bestLine;
                lineSequence.push(currentLine);
            } else {
                // Continue on the current line if possible
                if (lines.includes(currentLine)) {
                    continue;
                } else {
                    // Choose the line that can be continued the longest from here
                    let bestLine = null, bestRun = -1;
                    for (const candidate of lines) {
                        let run = 1;
                        for (let j = i + 1; j < path.length - 1; ++j) {
                            const nextA = path[j], nextB = path[j + 1];
                            const nextKey = [nextA, nextB].sort().join('|');
                            const nextLines = lineIdByNodePair[nextKey] ? Array.from(lineIdByNodePair[nextKey]) : [];
                            if (!nextLines.includes(candidate)) break;
                            run++;
                        }
                        if (run > bestRun || (run === bestRun && (bestLine === null || candidate < bestLine))) {
                            bestRun = run;
                            bestLine = candidate;
                        }
                    }
                    currentLine = bestLine;
                    lineSequence.push(currentLine);
                }
            }
        }
        routeFinderStatus.innerHTML = `<b>Origin:</b> ${routeFinderBtn.dataset.originStation || ''}<br><b>Destination:</b> ${routeFinderBtn.dataset.destinationStation || ''}`;
        // Calculate travel time: 80 km/h + 0.4 min per station + 6 min per transfer
        // Add clickStartDist and clickEndDist to totalRouteDistance
        const speed_kmh = 80;
        const totalDistanceKm = (totalRouteDistance + clickStartDist + clickEndDist) / 1000;
        let transfers = 0;
        for (let i = 1; i < lineSequence.length; ++i) {
            if (lineSequence[i] !== lineSequence[i - 1]) transfers++;
        }
        const travelTimeHours = totalDistanceKm / speed_kmh;
        // Count number of stations in the path
        let stationCount = 0;
        for (let i = 0; i < path.length; ++i) {
            const node = path[i];
            const coord = linesGraph.nodes[node];
            if (!coord) continue;
            const key = roundCoord(coord).join(',');
            if (stationStatusByCoord[key] === undefined || stationStatusByCoord[key]) stationCount++;
        }
        // For travel time, replace (path.length - 2) * 0.4 with (stationCount - 2) * 0.4
        // Add time for clickStartDist and clickEndDist at 100 meters per minute
        let travelTimeMinutes = travelTimeHours * 60 + Math.max(0, stationCount - 2) * 0.4 + transfers * 6 + (clickStartDist + clickEndDist) / 100;
        travelTimeMinutes = Math.ceil(travelTimeMinutes); // round up to next whole minute
        let travelTimeStr = `${travelTimeMinutes} min`;
        if (travelTimeMinutes >= 60) {
            const hours = Math.floor(travelTimeMinutes / 60);
            const minutes = travelTimeMinutes % 60;
            travelTimeStr = `${hours} hr${hours > 1 ? 's' : ''}` + (minutes > 0 ? ` ${minutes} min` : '');
        }
        // Add walk to route summary if needed
        let walkSummaryStart = '';
        let walkSummaryEnd = '';
        let walkDistance = clickStartDist + clickEndDist;
        let walkDistanceStrStart = '';
        let walkDistanceStrEnd = '';
        let walkTimeStr = '';
        if (clickStartDist > 0) {
            walkDistanceStrStart = `${(clickStartDist / 1000).toFixed(2)} km`;
            walkSummaryStart = `Walk (${walkDistanceStrStart}) → `;
        }
        if (clickEndDist > 0) {
            walkDistanceStrEnd = ` → Walk (${(clickEndDist / 1000).toFixed(2)} km)`;
        }
        if (walkDistance > 0) {
            const walkTime = Math.ceil(walkDistance / 100); // 100 meters per minute
            walkTimeStr = `${walkTime} min`;
            if (walkTime >= 60) {
                hours = Math.floor(walkTime / 60);
                minutes = walkTime % 60;
                walkTimeStr = `${hours} hr${hours > 1 ? 's' : ''}` + (minutes > 0 ? ` ${minutes} min` : '');
            }
        }
        const operationsSummary = summarizeRouteOperations(lineSequence);
        // Build list of intermediate station names
        let intermediateStations = [];
        for (let i = 1; i < path.length - 1; ++i) {
            const node = path[i];
            const coord = linesGraph.nodes[node];
            if (!coord) continue;
            const stationName = getStationNameByCoord(coord);
            intermediateStations.push(stationName);
        }
        // Add show/hide button for intermediate stations
        let intermediateListId = 'intermediate-stations-list';
        let showHideBtnId = 'show-hide-intermediate-btn';
        routeFinderResult.innerHTML =
            `<b>Route:</b> ${walkSummaryStart}${lineSequence.map(l => `Line ${l}`).join(' → ')}${walkDistanceStrEnd}` +
            `<br><b>Stations:</b> ${stationCount - 2}` +
            `<br><b>Total distance:</b> ${totalDistanceKm.toFixed(2)} km` +
            `<br><b>Transfers:</b> ${transfers}` +
            `<br><b>Estimated travel time:</b> ${travelTimeStr}` +
            operationsSummary +
            (walkDistance > 0 ? `<br><b>Total walking distance:</b> ${((clickStartDist + clickEndDist) / 1000).toFixed(2)} km` : '') +
            (walkDistance > 0 ? `<br><b>Total walking time:</b> ${walkTimeStr}` : '') +
            `<br><button id="${showHideBtnId}">Show intermediate stations</button><div id="${intermediateListId}" style="display:none;"></div>`;
        document.getElementById(showHideBtnId).onclick = function () {
            const div = document.getElementById(intermediateListId);
            if (div.style.display === 'none') {
                div.style.display = 'block';
                div.innerHTML = '<ol>' + intermediateStations.map(s => `<li>${s}</li>`).join('') + '</ol>';
                this.textContent = 'Hide intermediate stations';
            } else {
                div.style.display = 'none';
                this.textContent = 'Show intermediate stations';
            }
        };
        // Add Exit Route Finder button
        let exitBtn = document.getElementById('route-finder-exit-btn');
        if (!exitBtn) {
            exitBtn = document.createElement('button');
            exitBtn.id = 'route-finder-exit-btn';
            exitBtn.textContent = 'Exit Route Finder';
            exitBtn.style.marginLeft = '10px';
            routeFinderBtn.parentNode.insertBefore(exitBtn, routeFinderBtn.nextSibling);
            exitBtn.onclick = () => {
                routeFinderState = 'idle';
                routeStart = null;
                routeEnd = null;
                routeFinderBtn.dataset.originStation = '';
                routeFinderBtn.dataset.destinationStation = '';
                if (routeHighlightLayer) { map.removeLayer(routeHighlightLayer); routeHighlightLayer = null; }
                routeNodeMarkers.forEach(m => map.removeLayer(m));
                routeNodeMarkers = [];
                routeFinderStatus.textContent = '';
                routeFinderError.textContent = '';
                routeFinderResult.textContent = '';
                routeFinderBtn.textContent = 'Route Finder';
                exitBtn.remove();
                renderLayerToggles();
                clearRouteSelectionVisuals();
            };
        }
        routeFinderBtn.textContent = 'clear';
        routeFinderState = 'results';
        renderLayerToggles();
        // Do not clearRouteSelectionVisuals here, keep destination visuals
    }
});

// --- Update line offsets on zoom without reloading everything ---
function updateLineOffsetsOnZoom() {
    // For each visible line, recalculate its offset geometry and update the polyline
    currentLinesLayerNames.forEach(name => {
        const poly = layers[name];
        if (!poly) return;
        // Find the corresponding feature (line) for this polyline
        // We'll need to store the original coordinates and group info for each line
        // Store these as a property on the polyline when first created
        if (!poly._originalCoords || !poly._group) return;
        const coords = poly._originalCoords;
        const group = poly._group;
        // Recompute offset segments
        let polylines = [];
        for (let j = 0; j < coords.length - 1; ++j) {
            const a = coords[j], b = coords[j + 1];
            // Use direction-agnostic key
            const key = [a.join(','), b.join(',')].sort().join('|');
            // Only consider lines in different groups for separation
            const allLines = poly._segmentToLines[key] ? Array.from(poly._segmentToLines[key]) : [];
            // Get group for each line on this segment
            const groupMap = poly._groupMap || {};
            // Only offset between groups
            // Get unique groups and their line_ids
            const groupToLines = {};
            allLines.forEach(lid => {
                const g = groupMap[lid];
                if (!groupToLines[g]) groupToLines[g] = [];
                groupToLines[g].push(lid);
            });
            // Sort groups by group id (for consistent order)
            const groupIds = Object.keys(groupToLines).sort((a, b) => a - b);
            // Find this line's group index
            const myGroupIdx = groupIds.indexOf(String(group));
            // Offset: only if there are multiple groups
            let offset = 0;
            if (groupIds.length > 1) {
                // Offset by group, not by line
                offset = (myGroupIdx - (groupIds.length - 1) / 2) * 600000;
            }
            const seg = getOffsetLatLngs([a[1], a[0]], [b[1], b[0]], offset, map, 1);
            if (seg && seg[0] && seg[1]) {
                polylines.push(seg);
            }
        }
        // Only update if we have valid segments
        if (polylines.length > 0 && polylines[0][0] && polylines[0][1]) {
            let latlngs = [polylines[0][0]];
            for (let j = 0; j < polylines.length; ++j) {
                if (polylines[j][1]) latlngs.push(polylines[j][1]);
            }
            poly.setLatLngs(latlngs);
        }
    });
}

// --- Replace reload on zoom with offset update ---
map.on('zoomend', () => {
    updateLineOffsetsOnZoom();
});

// Redraw lines on zoom to keep offsets visually appropriate
map.on('zoomend', () => {
    updateLineOffsetsOnZoom();
});

// Add minimal CSS for columns if not present
(function ensureLayerToggleColCSS() {
    if (!document.getElementById('layer-toggle-col-style')) {
        const style = document.createElement('style');
        style.id = 'layer-toggle-col-style';
        style.textContent = `
            #layer-toggles { display: flex; flex-direction: row; gap: 24px; flex-wrap: wrap; max-width: 100%; overflow-x: auto; box-sizing: border-box; }
            .layer-toggle-col { display: flex; flex-direction: column; gap: 8px; min-width: 120px; max-width: 180px; }
            .layer-toggle-group-header { font-weight: bold; margin-top: 8px; margin-bottom: 4px; }
            .layer-toggle-col.rightmost { min-width: 180px; }
            #controls { overflow-x: auto; overflow-y: visible; max-width: 100vw; box-sizing: border-box; }
        `;
        document.head.appendChild(style);
    }
})();