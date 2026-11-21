/*
 * 场地坐标映射 / 合规矢量底图（M3，2026-11-08）
 * ------------------------------------------------------------------
 * 与 server/field_map.py 同构：逻辑坐标 (x,y) → 场地米制 (east,north) → 可选 GCJ-02。
 * 默认底图是本地自绘矢量场地：不加载任何在线瓦片、不画国界/行政区划，
 * 断网也能演示。需要真实地理底图时用 /map 页（腾讯位置服务合规代理模式）。
 *
 * 用法：
 *   FieldMap.load(api("/field"));           // 失败也不影响原界面
 *   if (!FieldMap.drawBase(ctx, 800, 600)) { ...原网格兜底... }
 *   FieldMap.describe(400, 300);            // "东 10.00 m · 北 -7.50 m · 巡检主通道"
 */
(function (global) {
  "use strict";

  var M_PER_DEG_LAT = 110540.0, M_PER_DEG_LNG = 111320.0;
  var ZONE_STYLE = {
    parking:    { fill: "rgba(47,123,217,.10)", line: "#9dc0ea", text: "#3a6ea8" },
    lane:       { fill: "rgba(28,155,108,.09)", line: "#9ad3bb", text: "#237d59" },
    dock:       { fill: "rgba(139,92,199,.10)", line: "#c3a9e4", text: "#7451a8" },
    restricted: { fill: "rgba(214,69,69,.13)",  line: "#e29a9a", text: "#b53b3b" }
  };

  var F = {
    ready: false,
    cfg: null,
    error: null,
    width: 800, height: 600,
    mpu: 1.0, northDeg: 0,
    geofenceEnabled: false, geofenceMargin: 0, polygon: null,
    zones: [], waypoints: [],
    geoReady: false, originLng: 0, originLat: 0, rotationDeg: 0, datum: "GCJ-02"
  };

  function num(v, d) { var n = Number(v); return isFinite(n) ? n : d; }

  function apply(cfg) {
    cfg = cfg || {};
    F.cfg = cfg;
    var lg = cfg.logical || {}, sc = cfg.scale || {}, gf = cfg.geofence || {},
        geo = cfg.geo || {};
    F.width = num(lg.width, 800);
    F.height = num(lg.height, 600);
    F.northDeg = num(lg.north_deg, 0);
    F.mpu = num(sc.meters_per_unit, 1.0) || 1.0;
    F.geofenceEnabled = !!gf.enabled;
    F.geofenceMargin = num(gf.margin, 0);
    F.polygon = gf.polygon || [[0, 0], [F.width, 0], [F.width, F.height], [0, F.height]];
    F.zones = cfg.zones || [];
    F.waypoints = cfg.waypoints || [];
    F.geoReady = !!(geo.enabled && geo.calibrated && geo.origin_lng !== null && geo.origin_lat !== null);
    F.originLng = num(geo.origin_lng, 0);
    F.originLat = num(geo.origin_lat, 0);
    F.rotationDeg = num(geo.rotation_deg, 0);
    F.datum = geo.datum || "GCJ-02";
    F.ready = true;
  }

  F.load = function (url, opts) {
    return fetch(url, opts || { cache: "no-store" }).then(function (r) {
      if (!r.ok) throw new Error("GET /field -> " + r.status);
      return r.json();
    }).then(function (j) {
      apply(j.ok ? (j.field || {}) : j);
      return F;
    }).catch(function (e) {
      F.ready = false;
      F.error = e.message || String(e);
      return F;                       // 旧后端没有 /field：调用方退回原网格，界面不退化
    });
  };

  // -------------------------------------------------------------- 坐标换算
  F.toMeters = function (x, y) {
    if (x === null || y === null || x === undefined || y === undefined) return null;
    return { east: Number(x) * F.mpu, north: -Number(y) * F.mpu };
  };

  F.toGeo = function (x, y) {
    var m = F.toMeters(x, y);
    if (!m || !F.geoReady) return null;
    var r = F.rotationDeg * Math.PI / 180;
    var e = m.east * Math.cos(r) - m.north * Math.sin(r);
    var n = m.east * Math.sin(r) + m.north * Math.cos(r);
    var lat = F.originLat;
    var lng = F.originLng + e / (M_PER_DEG_LNG * Math.max(Math.cos(lat * Math.PI / 180), 1e-6));
    lat = lat + n / M_PER_DEG_LAT;
    return { lng: +lng.toFixed(7), lat: +lat.toFixed(7), datum: F.datum };
  };

  function inPolygon(x, y, poly) {
    var inside = false;
    for (var i = 0, j = poly.length - 1; i < poly.length; j = i++) {
      var xi = poly[i][0], yi = poly[i][1], xj = poly[j][0], yj = poly[j][1];
      if ((yi > y) !== (yj > y) && x < (xj - xi) * (y - yi) / (yj - yi) + xi) inside = !inside;
    }
    return inside;
  }

  function distSeg(px, py, ax, ay, bx, by) {
    var dx = bx - ax, dy = by - ay;
    if (!dx && !dy) return Math.hypot(px - ax, py - ay);
    var t = Math.max(0, Math.min(1, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)));
    return Math.hypot(px - (ax + t * dx), py - (ay + t * dy));
  }

  F.inside = function (x, y) {
    if (!F.ready || !F.geofenceEnabled || x === null || y === null || x === undefined) return true;
    x = Number(x); y = Number(y);
    if (!inPolygon(x, y, F.polygon)) return false;
    if (F.geofenceMargin <= 0) return true;
    for (var i = 0; i < F.polygon.length; i++) {
      var a = F.polygon[i], b = F.polygon[(i + 1) % F.polygon.length];
      if (distSeg(x, y, a[0], a[1], b[0], b[1]) < F.geofenceMargin) return false;
    }
    return true;
  };

  F.zoneAt = function (x, y) {
    if (!F.ready || x === null || y === null || x === undefined) return null;
    var hit = null;
    for (var i = 0; i < F.zones.length; i++) {
      var z = F.zones[i], pts = z.points || [];
      if (pts.length >= 3 && inPolygon(Number(x), Number(y), pts)) {
        if (z.type === "restricted") return z;
        if (!hit) hit = z;
      }
    }
    return hit;
  };

  F.describe = function (x, y) {
    var m = F.toMeters(x, y);
    if (!m) return "坐标 --";
    var s = "东 " + m.east.toFixed(2) + " m · 北 " + m.north.toFixed(2) + " m";
    var z = F.zoneAt(x, y);
    if (z) s += " · " + (z.name || z.id);
    if (!F.inside(x, y)) s += " · ⚠ 越界";
    return s;
  };

  // -------------------------------------------------------------- 合规矢量底图
  function poly(ctx, pts, close) {
    ctx.beginPath();
    ctx.moveTo(pts[0][0], pts[0][1]);
    for (var i = 1; i < pts.length; i++) ctx.lineTo(pts[i][0], pts[i][1]);
    if (close !== false) ctx.closePath();
  }

  F.drawBase = function (ctx, w, h) {
    if (!F.ready) return false;
    w = w || F.width; h = h || F.height;
    var sx = w / F.width, sy = h / F.height;
    ctx.save();
    ctx.scale(sx, sy);

    // 1) 底色
    ctx.fillStyle = "#f7fafd";
    ctx.fillRect(0, 0, F.width, F.height);

    // 2) 米制网格：每 1 m 细线，每 5 m 中线（由 meters_per_unit 反推像素间距）
    var step1 = 1 / F.mpu;
    if (step1 >= 8 && step1 <= F.width) {
      ctx.lineWidth = 1;
      for (var gx = 0; gx <= F.width + 0.001; gx += step1) {
        var major = Math.round(gx / step1) % 5 === 0;
        ctx.strokeStyle = major ? "#d6e2ef" : "#eaf1f8";
        ctx.beginPath(); ctx.moveTo(gx, 0); ctx.lineTo(gx, F.height); ctx.stroke();
      }
      for (var gy = 0; gy <= F.height + 0.001; gy += step1) {
        var majorY = Math.round(gy / step1) % 5 === 0;
        ctx.strokeStyle = majorY ? "#d6e2ef" : "#eaf1f8";
        ctx.beginPath(); ctx.moveTo(0, gy); ctx.lineTo(F.width, gy); ctx.stroke();
      }
      // 米制刻度
      ctx.fillStyle = "#9fb0c2";
      ctx.font = "10px sans-serif";
      ctx.textAlign = "left";
      for (var mx = 0; mx <= F.width + 0.001; mx += step1 * 5) {
        ctx.fillText((mx * F.mpu).toFixed(0) + "m", mx + 3, 12);
      }
      for (var my = 0; my <= F.height + 0.001; my += step1 * 5) {
        if (my > 20) ctx.fillText((-my * F.mpu).toFixed(0) + "m", 3, my - 3);
      }
    }

    // 3) 场地分区（本地矢量，不含任何在线地图要素）
    for (var i = 0; i < F.zones.length; i++) {
      var z = F.zones[i], pts = z.points || [];
      if (pts.length < 3) continue;
      var st = ZONE_STYLE[z.type] || ZONE_STYLE.lane;
      poly(ctx, pts, true);
      ctx.fillStyle = st.fill; ctx.fill();
      ctx.setLineDash(z.type === "restricted" ? [6, 4] : [3, 5]);
      ctx.lineWidth = 1.5; ctx.strokeStyle = st.line; ctx.stroke();
      ctx.setLineDash([]);
      var cx = 0, cy = 0;
      for (var k = 0; k < pts.length; k++) { cx += pts[k][0]; cy += pts[k][1]; }
      cx /= pts.length; cy /= pts.length;
      ctx.fillStyle = st.text;
      ctx.font = "11px 'Microsoft YaHei', sans-serif";
      ctx.textAlign = "center"; ctx.textBaseline = "middle";
      ctx.fillText((z.type === "restricted" ? "⛔ " : "") + (z.name || z.id || ""), cx, cy);
    }

    // 4) 安全边界（含内缩余量示意）
    if (F.geofenceEnabled && F.polygon && F.polygon.length >= 3) {
      poly(ctx, F.polygon, true);
      ctx.setLineDash([8, 5]);
      ctx.lineWidth = 2; ctx.strokeStyle = "#e0a03a"; ctx.stroke();
      ctx.setLineDash([]);
    }

    // 5) 巡检点
    for (var p = 0; p < F.waypoints.length; p++) {
      var wp = F.waypoints[p];
      ctx.beginPath(); ctx.arc(wp.x, wp.y, 4, 0, Math.PI * 2);
      ctx.fillStyle = "#2f7bd9"; ctx.fill();
      ctx.strokeStyle = "#fff"; ctx.lineWidth = 1.5; ctx.stroke();
      ctx.fillStyle = "#5a7fa8";
      ctx.font = "10px sans-serif"; ctx.textAlign = "left"; ctx.textBaseline = "middle";
      ctx.fillText(wp.id || wp.name || "", wp.x + 7, wp.y);
    }

    // 6) 比例尺 + 合规声明
    var barMeters = 5, barPx = barMeters / F.mpu;
    if (barPx > F.width * 0.6) { barMeters = 1; barPx = 1 / F.mpu; }
    var bx = F.width - barPx - 14, by = F.height - 18;
    ctx.strokeStyle = "#5a6b7f"; ctx.lineWidth = 2;
    ctx.beginPath(); ctx.moveTo(bx, by); ctx.lineTo(bx + barPx, by);
    ctx.moveTo(bx, by - 4); ctx.lineTo(bx, by + 4);
    ctx.moveTo(bx + barPx, by - 4); ctx.lineTo(bx + barPx, by + 4);
    ctx.stroke();
    ctx.fillStyle = "#5a6b7f"; ctx.font = "10px sans-serif";
    ctx.textAlign = "center"; ctx.textBaseline = "bottom";
    ctx.fillText(barMeters + " m", bx + barPx / 2, by - 5);

    var comp = (F.cfg && F.cfg.compliance) || {};
    var label = "本地矢量场地 · 1 单位 = " + F.mpu + " m · " +
                (comp.datum || "GCJ-02") + (F.geoReady ? "（已校准）" : "（未校准，不输出经纬度）");
    ctx.fillStyle = "#8496aa"; ctx.font = "11px 'Microsoft YaHei', sans-serif";
    ctx.textAlign = "left"; ctx.textBaseline = "top";
    ctx.fillText(label, 10, 8);

    ctx.restore();
    return true;
  };

  global.FieldMap = F;
})(typeof window !== "undefined" ? window : globalThis);
