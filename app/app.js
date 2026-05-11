// =============================================================================
// Configuration
// =============================================================================

const COLORS = [
    "#e6194b", "#3cb44b", "#ffe119", "#4363d8", "#f58231", "#911eb4",
    "#46f0f0", "#f032e6", "#bcf60c", "#fabebe", "#008080", "#e6beff",
    "#9a6324", "#800000", "#aaffc3", "#808000", "#ffd8b1", "#000075",
    "#808080", "#000000", "#a9a9a9", "#ff4500", "#2e8b57", "#1e90ff",
    "#ff69b4", "#7cfc00", "#8a2be2", "#00ced1",
];

// =============================================================================
// Shared state
// =============================================================================

// Layer registry
const layers = {};
const lineMarkers = {};
const layerOrder = [];
const lineLayerNames = [];
const currentLinesLayerNames = [];
const catchmentCircles = [];

// Vertex-to-line lookups (populated when lines GeoJSON loads)
const vertexLineMap = {};
const vertexKDEMap = {};
const stationStatusByCoord = {};
const lineMetadataById = {};

// Lines graph used by the route finder
let linesGraph = null;
const nodeToLineIds = {};
const lineIdByNodePair = {};

// Route finder
let routeFinderState = 'idle'; // 'idle' | 'selectingStart' | 'selectingEnd' | 'results'
let routeStart = null;
let routeEnd = null;
let routeHighlightLayer = null;
let routeNodeMarkers = [];
let clickMarkerStart = null;
let clickToStationLineStart = null;
let clickMarkerEnd = null;
let clickToStationLineEnd = null;
let clickStartDist = 0;
let clickEndDist = 0;

// Currently selected line
let selectedLineId = null;
let currentLinesSource = 'lines_genetic';
let realWorldNetworkLayer = null;

// =============================================================================
// Map initialisation
// =============================================================================

const map = L.map('map').setView([38.9, -77.05], 10);

L.tileLayer('https://tiles.stadiamaps.com/tiles/alidade_smooth/{z}/{x}/{y}{r}.{ext}', {
    minZoom: 0,
    maxZoom: 20,
    attribution:
        '&copy; <a href="https://www.stadiamaps.com/" target="_blank">Stadia Maps</a> ' +
        '&copy; <a href="https://openmaptiles.org/" target="_blank">OpenMapTiles</a> ' +
        '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    ext: 'png',
}).addTo(map);

// =============================================================================
// Utility functions
// =============================================================================

async function fetchGeoJSON(url) {
    const resp = await fetch(url);
    return await resp.json();
}

function roundCoord(coord) {
    return [Number(coord[0].toFixed(6)), Number(coord[1].toFixed(6))].sort();
}

function escapeHtml(value) {
    return String(value ?? '')
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function toNumberOrNull(value) {
    const n = Number(value);
    return Number.isFinite(n) ? n : null;
}

function formatPercent(value) {
    const n = toNumberOrNull(value);
    return n === null ? 'N/A' : `${Math.round(n)}%`;
}

function formatMinutes(value) {
    const n = toNumberOrNull(value);
    return n === null ? 'N/A' : `${n.toFixed(0)} min`;
}

function formatDollars(value) {
    const n = toNumberOrNull(value);
    return n === null ? 'N/A' : `$${n.toFixed(1)}M`;
}

function formatDuration(minutes) {
    if (minutes >= 60) {
        const h = Math.floor(minutes / 60), m = minutes % 60;
        return `${h} hr${h > 1 ? 's' : ''}` + (m > 0 ? ` ${m} min` : '');
    }
    return `${minutes} min`;
}

// =============================================================================
// Transit operations panel
// =============================================================================

const selectedLineDetails = document.getElementById('selected-line-details');

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
    return { ok: 'ok', warn: 'warn', alert: 'alert' }[status] || 'muted';
}

function buildOperationalChips(meta) {
    const crowdingClass =
        meta.occupancyPct !== null && meta.occupancyPct >= 80 ? 'alert'
        : meta.occupancyPct !== null && meta.occupancyPct >= 60 ? 'warn'
        : 'ok';
    const delayClass =
        meta.delayMin !== null && meta.delayMin >= 10 ? 'alert'
        : meta.delayMin !== null && meta.delayMin >= 5 ? 'warn'
        : 'ok';
    const accessClass =
        meta.isAccessible === false ? 'warn'
        : meta.isAccessible === true ? 'ok'
        : 'muted';

    return [
        `<span class="chip muted">${escapeHtml(meta.routeKind)}</span>`,
        `<span class="chip muted">${escapeHtml(meta.serviceStatus)}</span>`,
        `<span class="chip ${statusChipClass(crowdingClass)}">Crowding ${escapeHtml(formatPercent(meta.occupancyPct))}</span>`,
        `<span class="chip ${statusChipClass(delayClass)}">Delay ${escapeHtml(formatMinutes(meta.delayMin))}</span>`,
        `<span class="chip ${statusChipClass(accessClass)}">${meta.isAccessible === false ? 'Not accessible' : meta.isAccessible === true ? 'Accessible' : 'Accessibility N/A'}</span>`,
        `<span class="chip muted">ROW ${escapeHtml(meta.rowType)}</span>`,
        meta.constructionCostMusd !== null ? `<span class="chip muted">Cost ${escapeHtml(formatDollars(meta.constructionCostMusd))}</span>` : '',
        meta.ridershipEstimate !== null ? `<span class="chip muted">Ridership ${escapeHtml(meta.ridershipEstimate.toFixed(0))}</span>` : '',
    ].join(' ');
}

function renderTransitOperationsPanel(features) {
    const summaryPanel = document.getElementById('transit-ops-summary');
    const detailsPanel = document.getElementById('transit-line-details');
    if (!summaryPanel || !detailsPanel) return;

    const lines = (features || []).map(f => normalizeLineMetadata(f.properties || {}));
    const counts = {
        crowding: lines.filter(m => m.occupancyPct !== null).length,
        delay: lines.filter(m => m.delayMin !== null).length,
        accessible: lines.filter(m => m.isAccessible !== null).length,
        planned: lines.filter(m => m.serviceStatus === 'planned').length,
    };

    summaryPanel.innerHTML = [
        { label: 'Loaded lines', value: lines.length, note: 'Current network layers' },
        { label: 'Crowding data', value: counts.crowding, note: 'Lines with occupancy info' },
        { label: 'Delay data', value: counts.delay, note: 'Lines with delay tracking' },
        { label: 'Accessibility data', value: counts.accessible, note: 'Lines with access flags' },
    ].map(c => `
        <div class="metric-card">
            <span class="metric-label">${escapeHtml(c.label)}</span>
            <div class="metric-value">${escapeHtml(c.value)}</div>
            <div class="metric-note">${escapeHtml(c.note)}</div>
        </div>`).join('');

    detailsPanel.innerHTML = lines.length
        ? lines.sort((a, b) => (a.lineId ?? 0) - (b.lineId ?? 0)).map(meta => `
            <div class="line-detail-row">
                <div>
                    <div class="line-detail-title">${escapeHtml(meta.name)}</div>
                    <div class="line-detail-meta">${escapeHtml(meta.routeKind)} · ${escapeHtml(meta.serviceStatus)} · ${escapeHtml(meta.rowType)}</div>
                </div>
                <div>${buildOperationalChips(meta)}</div>
            </div>`).join('')
        : `<div class="line-detail-row"><div class="line-detail-meta">No transit lines are loaded yet.</div></div>`;

    if (counts.planned > 0) {
        detailsPanel.insertAdjacentHTML('beforeend',
            `<div class="line-detail-row"><div class="line-detail-meta">${counts.planned} lines are still marked as planned.</div></div>`);
    }

    if (selectedLineDetails && !selectedLineId) {
        selectedLineDetails.innerHTML =
            '<div class="line-detail-row"><div class="line-detail-meta">Click a line to inspect crowding, delay, accessibility, and cost fields.</div></div>';
    }
}

function renderSelectedLineDetails(props) {
    if (!selectedLineDetails || !props) return;
    const meta = normalizeLineMetadata(props);
    selectedLineId = meta.lineId;
    selectedLineDetails.innerHTML = `
        <div class="line-detail-row">
            <div>
                <div class="line-detail-title">${escapeHtml(meta.name)}</div>
                <div class="line-detail-meta">Selected line · ${escapeHtml(meta.routeKind)} · ${escapeHtml(meta.serviceStatus)}</div>
            </div>
            <div>${buildOperationalChips(meta)}</div>
        </div>`;
}

function summarizeRouteOperations(lineSequence) {
    const routeMeta = lineSequence.map(id => lineMetadataById[id]).filter(Boolean);
    if (!routeMeta.length) {
        return '<div class="metric-card"><span class="metric-label">Operations</span><div class="metric-value">No metadata</div><div class="metric-note">This route was generated without crowding or delay inputs.</div></div>';
    }
    const withCrowding = routeMeta.filter(m => m.occupancyPct !== null);
    const withDelays = routeMeta.filter(m => m.delayMin !== null);
    const withAccess = routeMeta.filter(m => m.isAccessible !== null);
    const avgCrowding = withCrowding.length ? withCrowding.reduce((s, m) => s + m.occupancyPct, 0) / withCrowding.length : null;
    const maxDelay = withDelays.length ? Math.max(...withDelays.map(m => m.delayMin)) : null;
    const accessibleCount = withAccess.filter(m => m.isAccessible !== false).length;
    return `
        <div class="metric-grid">
            <div class="metric-card"><span class="metric-label">Avg crowding</span><div class="metric-value">${escapeHtml(formatPercent(avgCrowding))}</div><div class="metric-note">Across route segments with data</div></div>
            <div class="metric-card"><span class="metric-label">Max delay</span><div class="metric-value">${escapeHtml(formatMinutes(maxDelay))}</div><div class="metric-note">Worst loaded line on this trip</div></div>
            <div class="metric-card"><span class="metric-label">Accessible segments</span><div class="metric-value">${escapeHtml(accessibleCount)}</div><div class="metric-note">Segments not marked inaccessible</div></div>
        </div>`;
}

// =============================================================================
// Static network layer (graph edges/nodes)
// =============================================================================

fetchGeoJSON('../data/output/network.geojson').then(data => {
    const networkLayer = L.geoJSON(data, {
        style: f => f.geometry.type === 'LineString'
            ? { color: '#222', weight: 1, opacity: 0.5, dashArray: '4 4' }
            : {},
        pointToLayer: (f, ll) => L.circleMarker(ll, { radius: 2, color: '#222', fillOpacity: 0.5 }),
    });
    layers['Network'] = networkLayer;
    layerOrder.push('Network');
});

// =============================================================================
// Line offset geometry helpers
// =============================================================================

function getOffsetLatLngs(latlngA, latlngB, offsetMeters, direction = 1) {
    const toRad = d => d * Math.PI / 180;
    const toDeg = r => r * 180 / Math.PI;
    const lat1 = toRad(latlngA[0]), lng1 = toRad(latlngA[1]);
    const lat2 = toRad(latlngB[0]), lng2 = toRad(latlngB[1]);
    const dLng = lng2 - lng1;
    const bearing = Math.atan2(
        Math.sin(dLng) * Math.cos(lat2),
        Math.cos(lat1) * Math.sin(lat2) - Math.sin(lat1) * Math.cos(lat2) * Math.cos(dLng)
    );
    const perpBearing = bearing + direction * Math.PI / 2;
    const R = 6378137;

    function offsetPoint(lat, lng, brg, dist) {
        const newLat = Math.asin(
            Math.sin(lat) * Math.cos(dist / R) + Math.cos(lat) * Math.sin(dist / R) * Math.cos(brg)
        );
        const newLng = lng + Math.atan2(
            Math.sin(brg) * Math.sin(dist / R) * Math.cos(lat),
            Math.cos(dist / R) - Math.sin(lat) * Math.sin(newLat)
        );
        return [toDeg(newLat), toDeg(newLng)];
    }

    const scale = Math.pow(2, 1 - map.getZoom());
    const scaledOffset = offsetMeters * scale;
    return [
        offsetPoint(lat1, lng1, perpBearing, scaledOffset),
        offsetPoint(lat2, lng2, perpBearing, scaledOffset),
    ];
}

function computeOffsetPolyline(coords, group, segmentToLines, groupMap) {
    const polylines = [];
    for (let j = 0; j < coords.length - 1; ++j) {
        const a = coords[j], b = coords[j + 1];
        const key = [a.join(','), b.join(',')].sort().join('|');
        const allLines = Array.from(segmentToLines[key] || []);

        const groupToLines = {};
        allLines.forEach(lid => {
            const g = groupMap[lid];
            if (!groupToLines[g]) groupToLines[g] = [];
            groupToLines[g].push(lid);
        });

        const groupIds = Object.keys(groupToLines).sort((a, b) => a - b);
        const myGroupIdx = groupIds.indexOf(String(group));
        const offset = groupIds.length > 1
            ? (myGroupIdx - (groupIds.length - 1) / 2) * 600_000
            : 0;

        const seg = getOffsetLatLngs([a[1], a[0]], [b[1], b[0]], offset, 1);
        if (seg?.[0] && seg?.[1]) polylines.push(seg);
    }
    return polylines;
}

function polylineFromSegments(segments) {
    if (!segments.length || !segments[0][0] || !segments[0][1]) return null;
    const latlngs = [segments[0][0]];
    for (const seg of segments) {
        if (seg[1]) latlngs.push(seg[1]);
    }
    return latlngs;
}

// =============================================================================
// Lines GeoJSON loading
// =============================================================================

function loadLinesGeoJSON(source) {
    // Clear previous state
    Object.keys(vertexLineMap).forEach(k => delete vertexLineMap[k]);
    Object.keys(lineMetadataById).forEach(k => delete lineMetadataById[k]);
    Object.keys(stationStatusByCoord).forEach(k => delete stationStatusByCoord[k]);
    Object.keys(nodeToLineIds).forEach(k => delete nodeToLineIds[k]);
    Object.keys(lineIdByNodePair).forEach(k => delete lineIdByNodePair[k]);
    selectedLineId = null;
    linesGraph = null;

    catchmentCircles.forEach(c => { if (map.hasLayer(c)) map.removeLayer(c); });
    catchmentCircles.length = 0;

    currentLinesLayerNames.forEach(name => {
        if (layers[name]) map.removeLayer(layers[name]);
        if (lineMarkers[name]) lineMarkers[name].forEach(m => map.removeLayer(m));
        delete layers[name];
        delete lineMarkers[name];
    });
    currentLinesLayerNames.length = 0;

    fetchGeoJSON(`../data/output/${source}.geojson`).then(data => {
        const features = data.features || [];
        linesGraph = { nodes: {}, edges: {} };

        // Build vertex → line and segment → lines maps
        const segmentToLines = {};
        const groupMap = {};
        features.forEach(f => {
            const coords = f.geometry.coordinates;
            const lineId = f.properties.line_id;
            const kdeValues = f.properties.kde_values || [];
            const isStationArr = f.properties.is_station || [];

            lineMetadataById[lineId] = normalizeLineMetadata(f.properties);
            groupMap[lineId] = f.properties.group;

            coords.forEach((coord, idx) => {
                const key = roundCoord(coord).join(',');
                if (!vertexLineMap[key]) vertexLineMap[key] = [];
                vertexLineMap[key].push(lineId);
                if (!vertexKDEMap[key]) vertexKDEMap[key] = kdeValues[idx];
                stationStatusByCoord[key] = isStationArr.length > idx ? isStationArr[idx] : true;

                // Lines graph nodes
                const ll = [coord[1], coord[0]];
                linesGraph.nodes[key] = ll;
                if (!nodeToLineIds[key]) nodeToLineIds[key] = new Set();
                nodeToLineIds[key].add(lineId);
            });

            for (let i = 0; i < coords.length - 1; ++i) {
                const a = coords[i], b = coords[i + 1];
                const segKey = [a.join(','), b.join(',')].sort().join('|');
                if (!segmentToLines[segKey]) segmentToLines[segKey] = new Set();
                segmentToLines[segKey].add(lineId);

                const ka = [coords[i][1], coords[i][0]].join(',');
                const kb = [coords[i + 1][1], coords[i + 1][0]].join(',');
                const dist = L.latLng(...linesGraph.nodes[ka]).distanceTo(L.latLng(...linesGraph.nodes[kb]));
                if (!linesGraph.edges[ka]) linesGraph.edges[ka] = [];
                if (!linesGraph.edges[kb]) linesGraph.edges[kb] = [];
                linesGraph.edges[ka].push({ to: kb, dist, lineId });
                linesGraph.edges[kb].push({ to: ka, dist, lineId });

                const pairKey = [ka, kb].sort().join('|');
                if (!lineIdByNodePair[pairKey]) lineIdByNodePair[pairKey] = new Set();
                lineIdByNodePair[pairKey].add(lineId);
            }
        });

        // Draw lines
        features.forEach((f, i) => {
            const group = f.properties.group;
            const color = COLORS[group % COLORS.length];
            const name = `Line ${f.properties.line_id ?? i}`;
            const coords = f.geometry.coordinates;
            lineMarkers[name] = [];

            const segments = computeOffsetPolyline(coords, group, segmentToLines, groupMap);
            const latlngs = polylineFromSegments(segments);
            if (!latlngs) return;

            const totalDistance = (f.properties.segment_lengths || []).reduce((a, b) => a + b, 0);
            const nameList = f.properties.name_list || [];
            const firstStation = nameList.find(n => n) || 'Unnamed station';
            const lastStation = [...nameList].reverse().find(n => n) || 'Unnamed station';
            const stationCount = (f.properties.is_station || []).filter(Boolean).length;

            const poly = L.polyline(latlngs, { color, weight: 4, opacity: 1 }).addTo(map);
            poly._originalCoords = coords;
            poly._group = group;
            poly._segmentToLines = segmentToLines;
            poly._groupMap = groupMap;

            poly.bindTooltip(
                `<b>Line ${f.properties.line_id} (${firstStation.replace(/ \d+$/, '')} – ${lastStation.replace(/ \d+$/, '')})</b><br>` +
                `Total distance: ${(totalDistance / 1000).toFixed(2)} km<br>Stations: ${stationCount}`,
                { sticky: true, direction: 'top', offset: [0, -10] }
            );

            poly.on('mouseover', function () {
                this.setStyle({ weight: 7, opacity: 1 });
                document.querySelectorAll('.leaflet-interactive').forEach(el => {
                    if (el.getAttribute('stroke') !== this.options.color) {
                        el.setAttribute('opacity', '0.2');
                    }
                });
            });
            poly.on('mouseout', function () {
                this.setStyle({ weight: 4, opacity: 1 });
                document.querySelectorAll('.leaflet-interactive').forEach(el => {
                    el.setAttribute('opacity', '1');
                });
            });
            poly.on('click', () => renderSelectedLineDetails(f.properties));

            layers[name] = poly;
            currentLinesLayerNames.push(name);
            lineLayerNames.push(name);

            // Station markers and catchment circles
            const kdeValues = f.properties.kde_values || [];
            coords.forEach((coord, idx) => {
                const isStation = f.properties.is_station ? f.properties.is_station[idx] : true;
                if (!isStation) return;

                const key = roundCoord(coord).join(',');
                const kde = vertexKDEMap[key];
                const linesHere = vertexLineMap[key] || [];
                const stationName = nameList[idx] || 'Unnamed station';

                const marker = L.marker([coord[1], coord[0]], {
                    icon: L.icon({ iconUrl: 'assets/wmata.svg', iconSize: [14, 14], iconAnchor: [7, 7], popupAnchor: [0, -7] }),
                }).addTo(map);

                marker.bindTooltip(
                    `<b>${stationName}</b><br>KDE Score: ${kde?.toFixed(2) ?? 'N/A'}<br>Lines: ${linesHere.map(l => `Line ${l}`).join(', ')}`,
                    { direction: 'top', offset: [0, -10], sticky: false }
                );
                attachRouteFinderToMarker(marker, coord[1], coord[0], stationName);
                lineMarkers[name].push(marker);

                const circle = L.circle([coord[1], coord[0]], { radius: 700, color, fill: false, weight: 1, opacity: 0.3 });
                catchmentCircles.push(circle);
            });
        });

        // Sort layer names numerically
        currentLinesLayerNames.sort((a, b) => parseInt(a.replace('Line ', '')) - parseInt(b.replace('Line ', '')));
        layerOrder.length = 0;
        lineLayerNames.forEach(n => layerOrder.push(n));
        if (layers['Network']) layerOrder.push('Network');

        renderLayerToggles();
        renderTransitOperationsPanel(features);

        if (catchmentToggle.checked) {
            catchmentCircles.forEach(c => { if (!map.hasLayer(c)) map.addLayer(c); });
        }
    });
}

// =============================================================================
// Real-world transit network
// =============================================================================

const REAL_WORLD_SOURCES = [
    { url: '../data/real_transit/dcs/DC_Streetcar_Routes.geojson', color: 'brown', name: 'DC Streetcar' },
    {
        url: '../data/real_transit/marc/Maryland_Transit_-_MARC_Train_Lines.geojson',
        colorFn: props => props.Rail_Name?.includes('Brunswick') ? '#EFAD1D'
            : props.Rail_Name?.includes('Camden') ? '#F15828' : '#C71F3E',
        name: 'MARC Train',
    },
    {
        url: '../data/real_transit/wmata/Metro_Lines_Regional.geojson',
        colorFn: props => {
            const n = props.NAME || '';
            if (n.includes('orange')) return '#F9921D';
            if (n.includes('silver')) return '#A1A3A1';
            if (n.includes('red'))    return '#E41838';
            if (n.includes('yellow')) return '#FED201';
            if (n.includes('green'))  return '#01A850';
            return '#0077C1';
        },
        name: 'WMATA Metro',
    },
    {
        url: '../data/real_transit/vre/Virginia_Railway_Express_Routes.geojson',
        colorFn: props => props.RAILWAY_NM?.includes('Manassas') ? '#156DB4' : '#DD3534',
        name: 'VRE',
    },
    { url: '../data/real_transit/pl/PurpleLineAlignment.geojson', color: '#793390', name: 'Purple Line' },
];

function loadRealWorldNetwork() {
    if (realWorldNetworkLayer) {
        map.removeLayer(realWorldNetworkLayer);
        realWorldNetworkLayer = null;
    }
    Promise.all(REAL_WORLD_SOURCES.map(f => fetchGeoJSON(f.url).then(data => ({ ...f, data }))))
        .then(results => {
            const group = L.layerGroup();
            results.forEach(f => {
                const colorFn = f.colorFn || (() => f.color);
                group.addLayer(L.geoJSON(f.data, {
                    style: feature => ({ color: colorFn(feature.properties), weight: 2, opacity: 1, dashArray: '2 2' }),
                }));
            });
            realWorldNetworkLayer = group;
            const cb = document.getElementById('layer-toggle-RealWorldNetwork');
            if (cb?.checked) realWorldNetworkLayer.addTo(map);
        });
}

// =============================================================================
// Layer toggle UI
// =============================================================================

const layerToggles = document.getElementById('layer-toggles');

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

function createToggleAllLinesCheckbox(parent) {
    let label = document.getElementById('toggle-all-lines-label');
    const allChecked = () => currentLinesLayerNames.every(name => {
        const cb = document.getElementById(`layer-toggle-${name.replace(/\s/g, '-')}`);
        return cb?.checked;
    });

    if (!label) {
        label = document.createElement('label');
        label.id = 'toggle-all-lines-label';
        const checkbox = document.createElement('input');
        checkbox.type = 'checkbox';
        checkbox.id = 'toggle-all-lines';
        checkbox.checked = true;
        checkbox.onchange = () => {
            currentLinesLayerNames.forEach(name => {
                const cb = document.getElementById(`layer-toggle-${name.replace(/\s/g, '-')}`);
                if (cb && !cb.disabled) { cb.checked = checkbox.checked; cb.onchange(); }
            });
        };
        label.appendChild(checkbox);
        label.appendChild(document.createTextNode(' Toggle all lines'));
    } else {
        const checkbox = document.getElementById('toggle-all-lines');
        if (checkbox) checkbox.checked = allChecked();
    }
    (parent || layerToggles).appendChild(label);
}

function createRealWorldNetworkToggle(parent) {
    const id = 'layer-toggle-RealWorldNetwork';
    if (document.getElementById(id)) return;
    const label = document.createElement('label');
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
    (parent || layerToggles).appendChild(label);
}

function renderLayerToggles() {
    while (layerToggles.firstChild) layerToggles.removeChild(layerToggles.firstChild);

    // Group lines by group number
    const groupToLines = {};
    currentLinesLayerNames.forEach(name => {
        const poly = layers[name];
        if (!poly || poly._group === undefined) return;
        if (!groupToLines[poly._group]) groupToLines[poly._group] = [];
        groupToLines[poly._group].push(name);
    });

    const groupNums = Object.keys(groupToLines).map(Number).sort((a, b) => a - b);
    const columns = [];
    let col = document.createElement('div');
    col.className = 'layer-toggle-col';
    let linesInCol = 0;

    groupNums.forEach(gn => {
        const groupLines = groupToLines[gn];
        if (linesInCol + 1 + groupLines.length > 4) {
            columns.push(col);
            col = document.createElement('div');
            col.className = 'layer-toggle-col';
            linesInCol = 0;
        }
        const header = document.createElement('div');
        header.className = 'layer-toggle-group-header';
        header.textContent = `Group ${gn}`;
        col.appendChild(header);
        linesInCol += 1;
        groupLines.forEach(n => { createToggle(n, map.hasLayer(layers[n]), col); linesInCol += 1; });
    });
    if (col.childNodes.length > 0) columns.push(col);
    columns.forEach(c => layerToggles.appendChild(c));

    // Rightmost column: global controls
    const rightCol = document.createElement('div');
    rightCol.className = 'layer-toggle-col rightmost';
    createToggleAllLinesCheckbox(rightCol);
    if (layers['Network']) createToggle('Network', map.hasLayer(layers['Network']), rightCol);
    rightCol.appendChild(catchmentToggleLabel);
    createRealWorldNetworkToggle(rightCol);
    layerToggles.appendChild(rightCol);

    // Keep dropdown in controls div
    const dropdown = document.getElementById('lines-source-select');
    if (dropdown) {
        dropdown.parentNode?.removeChild(dropdown);
        document.getElementById('controls').appendChild(dropdown);
    }
}

// =============================================================================
// Catchment area toggle
// =============================================================================

const catchmentToggleLabel = document.createElement('label');
const catchmentToggle = document.createElement('input');
catchmentToggle.type = 'checkbox';
catchmentToggle.id = 'catchment-toggle';
catchmentToggle.checked = false;
catchmentToggle.onchange = () => {
    catchmentCircles.forEach(c => {
        if (catchmentToggle.checked) { if (!map.hasLayer(c)) map.addLayer(c); }
        else { if (map.hasLayer(c)) map.removeLayer(c); }
    });
};
catchmentToggleLabel.appendChild(catchmentToggle);
catchmentToggleLabel.appendChild(document.createTextNode(' Station catchment areas'));

// =============================================================================
// Source selector UI
// =============================================================================

const linesSourceSelect = document.createElement('select');
linesSourceSelect.id = 'lines-source-select';
[
    ['lines_iterative', 'Naive Algorithm with Iterative Improvement'],
    ['lines_genetic', 'Genetic Algorithm'],
    ['lines_naive', 'Naive Algorithm'],
].forEach(([value, label]) => {
    const opt = document.createElement('option');
    opt.value = value;
    opt.textContent = label;
    linesSourceSelect.appendChild(opt);
});
linesSourceSelect.value = currentLinesSource;
linesSourceSelect.onchange = function () {
    currentLinesSource = this.value;
    loadLinesGeoJSON(currentLinesSource);
};
document.getElementById('controls').appendChild(linesSourceSelect);

// Initial load
loadLinesGeoJSON(currentLinesSource);

// =============================================================================
// Route finder — core logic
// =============================================================================

function getVisibleLineIds() {
    return currentLinesLayerNames
        .filter(name => document.getElementById(`layer-toggle-${name.replace(/\s/g, '-')}`)?.checked)
        .map(name => parseInt(name.replace('Line ', '')));
}

function findNearestLineNodeVisible(lat, lng) {
    if (!linesGraph) return null;
    const visibleLineIds = new Set(getVisibleLineIds());
    let minDist = Infinity, minKey = null;
    for (const [key, ll] of Object.entries(linesGraph.nodes)) {
        const linesHere = nodeToLineIds[key] ? Array.from(nodeToLineIds[key]) : [];
        if (!linesHere.some(id => visibleLineIds.has(id))) continue;
        const d = Math.abs(ll[0] - lat) + Math.abs(ll[1] - lng);
        if (d < minDist) { minDist = d; minKey = key; }
    }
    return minKey;
}

function dijkstraLinesVisible(graph, start, end, visibleLineIds) {
    const Q = new Set(Object.keys(graph.nodes));
    const dist = {}, prev = {}, prevLine = {}, transfers = {};
    for (const v of Q) { dist[v] = Infinity; transfers[v] = Infinity; }
    dist[start] = 0;
    transfers[start] = 0;
    prevLine[start] = null;

    while (Q.size) {
        let u = null, minT = Infinity, minD = Infinity;
        for (const v of Q) {
            if (transfers[v] < minT || (transfers[v] === minT && dist[v] < minD)) {
                minT = transfers[v]; minD = dist[v]; u = v;
            }
        }
        if (!u || u === end) break;
        Q.delete(u);
        for (const e of (graph.edges[u] || [])) {
            if (!visibleLineIds.has(e.lineId)) continue;
            const newT = (prevLine[u] === null || prevLine[u] === e.lineId) ? transfers[u] : transfers[u] + 1;
            const alt = dist[u] + e.dist;
            if (newT < transfers[e.to] || (newT === transfers[e.to] && alt < dist[e.to])) {
                dist[e.to] = alt; prev[e.to] = u; prevLine[e.to] = e.lineId; transfers[e.to] = newT;
            }
        }
    }

    const path = [];
    let u = end;
    while (u !== undefined) { path.unshift(u); u = prev[u]; }
    return path[0] === start ? path : [];
}

function buildLineSequence(path) {
    const lineSequence = [];
    let currentLine = null;

    for (let i = 0; i < path.length - 1; ++i) {
        const pairKey = [path[i], path[i + 1]].sort().join('|');
        const lines = lineIdByNodePair[pairKey] ? Array.from(lineIdByNodePair[pairKey]) : [];
        if (!lines.length) continue;

        // Pick the line that can be continued the longest from here
        const best = lines.reduce((acc, candidate) => {
            let run = 1;
            for (let j = i + 1; j < path.length - 1; ++j) {
                const nk = [path[j], path[j + 1]].sort().join('|');
                if (!(lineIdByNodePair[nk] ? Array.from(lineIdByNodePair[nk]) : []).includes(candidate)) break;
                run++;
            }
            return acc === null || run > acc.run || (run === acc.run && candidate < acc.line)
                ? { line: candidate, run } : acc;
        }, null);

        if (!best) continue;

        if (lineSequence.length === 0) {
            currentLine = best.line;
            lineSequence.push(currentLine);
        } else if (!lines.includes(currentLine)) {
            currentLine = best.line;
            lineSequence.push(currentLine);
        }
        // else: continue on same line — no push
    }
    return lineSequence;
}

// Compute and render the route result; called from both the marker-click and
// map-click handlers after a path has been found.
function executeRoute(path, transitDistance) {
    if (path.length < 2) {
        routeFinderStatus.textContent = 'No route found.';
        return;
    }

    // Draw route highlight
    const routeCoords = path.map(node => linesGraph.nodes[node]);
    if (routeHighlightLayer) map.removeLayer(routeHighlightLayer);
    routeHighlightLayer = L.polyline(routeCoords, { color: 'blue', weight: 8, opacity: 0.7 }).addTo(map);

    const lineSequence = buildLineSequence(path);

    let transfers = 0;
    for (let i = 1; i < lineSequence.length; ++i) {
        if (lineSequence[i] !== lineSequence[i - 1]) transfers++;
    }

    let stationCount = 0;
    for (const node of path) {
        const coord = linesGraph.nodes[node];
        if (!coord) continue;
        const key = roundCoord(coord).join(',');
        if (stationStatusByCoord[key] === undefined || stationStatusByCoord[key]) stationCount++;
    }

    // Unified distance (always includes any walk legs)
    const walkDist = clickStartDist + clickEndDist;
    const totalDistanceKm = (transitDistance + walkDist) / 1000;
    const travelTimeMinutes = Math.ceil(
        (totalDistanceKm / 80) * 60              // 80 km/h rail
        + Math.max(0, stationCount - 2) * 0.4    // dwell per intermediate station
        + transfers * 6                           // transfer penalty
        + walkDist / 100                          // 100 m/min walk speed
    );
    const travelTimeStr = formatDuration(travelTimeMinutes);

    // Walk summaries
    let walkSummaryStart = '', walkDistanceStrEnd = '', walkTimeStr = '';
    if (clickStartDist > 0) walkSummaryStart = `Walk (${(clickStartDist / 1000).toFixed(2)} km) → `;
    if (clickEndDist > 0)   walkDistanceStrEnd = ` → Walk (${(clickEndDist / 1000).toFixed(2)} km)`;
    if (walkDist > 0)       walkTimeStr = formatDuration(Math.ceil(walkDist / 100));

    // Intermediate station names
    const intermediateStations = [];
    for (let i = 1; i < path.length - 1; ++i) {
        const coord = linesGraph.nodes[path[i]];
        if (!coord) continue;
        let name = 'Unnamed station';
        outer: for (const markers of Object.values(lineMarkers)) {
            for (const m of markers) {
                if (m.getLatLng().lat === coord[0] && m.getLatLng().lng === coord[1]) {
                    const tt = m.getTooltip();
                    if (tt?._content) {
                        const match = tt._content.match(/<b>(.*?)<\/b>/);
                        if (match) { name = match[1]; break outer; }
                    }
                }
            }
        }
        intermediateStations.push(name);
    }

    routeFinderResult.innerHTML =
        `<b>Route:</b> ${walkSummaryStart}${lineSequence.map(l => `Line ${l}`).join(' → ')}${walkDistanceStrEnd}` +
        `<br><b>Stations:</b> ${stationCount - 2}` +
        `<br><b>Total distance:</b> ${totalDistanceKm.toFixed(2)} km` +
        `<br><b>Transfers:</b> ${transfers}` +
        `<br><b>Estimated travel time:</b> ${travelTimeStr}` +
        summarizeRouteOperations(lineSequence) +
        (walkDist > 0 ? `<br><b>Total walking distance:</b> ${(walkDist / 1000).toFixed(2)} km` : '') +
        (walkDist > 0 ? `<br><b>Total walking time:</b> ${walkTimeStr}` : '') +
        `<br><button id="show-hide-intermediate-btn">Show intermediate stations</button>` +
        `<div id="intermediate-stations-list" style="display:none;"></div>`;

    document.getElementById('show-hide-intermediate-btn').onclick = function () {
        const div = document.getElementById('intermediate-stations-list');
        const hidden = div.style.display === 'none';
        div.style.display = hidden ? 'block' : 'none';
        div.innerHTML = hidden
            ? '<ol>' + intermediateStations.map(s => `<li>${escapeHtml(s)}</li>`).join('') + '</ol>'
            : '';
        this.textContent = hidden ? 'Hide intermediate stations' : 'Show intermediate stations';
    };

    if (!document.getElementById('route-finder-exit-btn')) {
        const exitBtn = document.createElement('button');
        exitBtn.id = 'route-finder-exit-btn';
        exitBtn.textContent = 'Exit Route Finder';
        exitBtn.style.marginLeft = '10px';
        routeFinderBtn.parentNode.insertBefore(exitBtn, routeFinderBtn.nextSibling);
        exitBtn.onclick = exitRouteFinder;
    }

    routeFinderBtn.textContent = 'clear';
    routeFinderState = 'results';
    renderLayerToggles();
}

function exitRouteFinder() {
    routeFinderState = 'idle';
    routeStart = routeEnd = null;
    if (routeHighlightLayer) { map.removeLayer(routeHighlightLayer); routeHighlightLayer = null; }
    routeNodeMarkers.forEach(m => map.removeLayer(m));
    routeNodeMarkers = [];
    routeFinderStatus.textContent = '';
    routeFinderResult.textContent = '';
    routeFinderBtn.textContent = 'Route finder';
    document.getElementById('route-finder-exit-btn')?.remove();
    renderLayerToggles();
    clearRouteSelectionVisuals();
}

// =============================================================================
// Route finder — UI and event wiring
// =============================================================================

const routeFinderBtn = document.getElementById('route-finder-btn');
const routeFinderStatus = document.getElementById('route-finder-status');
const routeFinderResult = document.getElementById('route-finder-result');

function clearRouteSelectionVisuals() {
    if (clickMarkerStart) { map.removeLayer(clickMarkerStart); clickMarkerStart = null; }
    if (clickToStationLineStart) { map.removeLayer(clickToStationLineStart); clickToStationLineStart = null; }
    if (clickMarkerEnd) { map.removeLayer(clickMarkerEnd); clickMarkerEnd = null; }
    if (clickToStationLineEnd) { map.removeLayer(clickToStationLineEnd); clickToStationLineEnd = null; }
    clickStartDist = 0;
    clickEndDist = 0;
}

function startRouteFinder() {
    routeFinderState = 'selectingStart';
    routeStart = routeEnd = null;
    if (routeHighlightLayer) { map.removeLayer(routeHighlightLayer); routeHighlightLayer = null; }
    routeNodeMarkers.forEach(m => map.removeLayer(m));
    routeNodeMarkers = [];
    routeFinderStatus.textContent = 'Click the starting location.';
    routeFinderResult.textContent = '';
    routeFinderBtn.textContent = 'clear';
    routeFinderBtn.disabled = true;
    document.getElementById('route-finder-exit-btn')?.remove();
    clearRouteSelectionVisuals();
}

routeFinderBtn.onclick = startRouteFinder;

// Attach route-finder click to a station marker
function attachRouteFinderToMarker(marker, lat, lng, stationName) {
    marker.on('click', e => {
        if (routeFinderState === 'selectingStart') {
            routeStart = findNearestLineNodeVisible(lat, lng);
            if (!routeStart) return;
            routeFinderState = 'selectingEnd';
            routeFinderBtn.dataset.originStation = stationName || '';
            routeFinderStatus.innerHTML = `<b>Origin:</b> ${escapeHtml(stationName || '')}<br>Click the destination.`;
            routeNodeMarkers.push(L.circleMarker([lat, lng], { radius: 12, color: 'green', fillOpacity: 0.7 }).addTo(map));
            routeFinderBtn.textContent = 'clear';
            routeFinderBtn.disabled = false;
            renderLayerToggles();

        } else if (routeFinderState === 'selectingEnd') {
            routeEnd = findNearestLineNodeVisible(lat, lng);
            if (!routeEnd || routeEnd === routeStart) return;

            routeFinderBtn.dataset.destinationStation = stationName || '';
            routeNodeMarkers.push(L.circleMarker([lat, lng], { radius: 12, color: 'red', fillOpacity: 0.7 }).addTo(map));

            routeFinderStatus.innerHTML =
                `<b>Origin:</b> ${escapeHtml(routeFinderBtn.dataset.originStation || '')}<br>` +
                `<b>Destination:</b> ${escapeHtml(stationName || '')}`;

            routeFinderState = 'idle';
            const visibleLineIds = new Set(getVisibleLineIds());
            const path = dijkstraLinesVisible(linesGraph, routeStart, routeEnd, visibleLineIds);
            const transitDistance = path.length >= 2
                ? path.slice(0, -1).reduce((sum, node, i) => {
                    const edge = (linesGraph.edges[node] || []).find(e => e.to === path[i + 1]);
                    return sum + (edge ? edge.dist : 0);
                }, 0)
                : 0;
            executeRoute(path, transitDistance);
        }
    });
}

// Map click handler for selecting start/end by clicking anywhere
map.on('click', function (e) {
    if (routeFinderState === 'selectingStart') {
        clearRouteSelectionVisuals();
        const { lat, lng } = e.latlng;
        const nearestKey = findNearestLineNodeVisible(lat, lng);
        if (!nearestKey) return;

        const nearestCoord = linesGraph.nodes[nearestKey];
        clickMarkerStart = L.circleMarker([lat, lng], { radius: 10, color: 'blue', fillOpacity: 0.5 }).addTo(map);
        clickToStationLineStart = L.polyline([[lat, lng], nearestCoord], { color: 'blue', weight: 3, dashArray: '6 6', opacity: 0.7 }).addTo(map);
        clickStartDist = map.distance([lat, lng], nearestCoord);

        routeStart = nearestKey;
        routeFinderState = 'selectingEnd';
        routeFinderStatus.textContent = 'Click the destination.';
        routeNodeMarkers.push(L.circleMarker(nearestCoord, { radius: 12, color: 'green', fillOpacity: 0.7 }).addTo(map));
        routeFinderBtn.textContent = 'clear';
        routeFinderBtn.disabled = false;
        renderLayerToggles();

    } else if (routeFinderState === 'selectingEnd') {
        if (clickMarkerEnd) { map.removeLayer(clickMarkerEnd); clickMarkerEnd = null; }
        if (clickToStationLineEnd) { map.removeLayer(clickToStationLineEnd); clickToStationLineEnd = null; }

        const { lat, lng } = e.latlng;
        const nearestKey = findNearestLineNodeVisible(lat, lng);
        if (!nearestKey || nearestKey === routeStart) return;

        const nearestCoord = linesGraph.nodes[nearestKey];
        clickMarkerEnd = L.circleMarker([lat, lng], { radius: 10, color: 'blue', fillOpacity: 0.5 }).addTo(map);
        clickToStationLineEnd = L.polyline([[lat, lng], nearestCoord], { color: 'blue', weight: 3, dashArray: '6 6', opacity: 0.7 }).addTo(map);
        clickEndDist = map.distance([lat, lng], nearestCoord);

        routeEnd = nearestKey;
        routeNodeMarkers.push(L.circleMarker(nearestCoord, { radius: 12, color: 'red', fillOpacity: 0.7 }).addTo(map));

        routeFinderStatus.innerHTML =
            `<b>Origin:</b> ${escapeHtml(routeFinderBtn.dataset.originStation || '')}<br>` +
            `<b>Destination:</b> ${escapeHtml(routeFinderBtn.dataset.destinationStation || '')}`;

        routeFinderState = 'idle';
        const visibleLineIds = new Set(getVisibleLineIds());
        const path = dijkstraLinesVisible(linesGraph, routeStart, routeEnd, visibleLineIds);
        const transitDistance = path.length >= 2
            ? path.slice(0, -1).reduce((sum, node, i) => {
                const edge = (linesGraph.edges[node] || []).find(e => e.to === path[i + 1]);
                return sum + (edge ? edge.dist : 0);
            }, 0)
            : 0;
        executeRoute(path, transitDistance);
    }
});

// =============================================================================
// Zoom handler — update line offsets on zoom (registered once)
// =============================================================================

map.on('zoomend', () => {
    currentLinesLayerNames.forEach(name => {
        const poly = layers[name];
        if (!poly?._originalCoords || poly._group === undefined) return;
        const latlngs = polylineFromSegments(
            computeOffsetPolyline(
                poly._originalCoords,
                poly._group,
                poly._segmentToLines,
                poly._groupMap,
            )
        );
        if (latlngs) poly.setLatLngs(latlngs);
    });
});
