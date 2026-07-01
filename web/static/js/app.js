/**
 * SatTrack dashboard — CesiumJS geospatial globe (Esri World Imagery).
 */
/* global Cesium */

function setEntityPosition(entity, cartesian) {
  if (!entity._posProp) {
    entity._posProp = new Cesium.ConstantPositionProperty(cartesian);
    entity.position = entity._posProp;
  } else {
    entity._posProp.setValue(cartesian);
  }
}

function setPolylinePositions(entity, cartesians) {
  if (!entity._lineProp) {
    entity._lineProp = new Cesium.ConstantProperty(cartesians);
    entity.polyline.positions = entity._lineProp;
  } else {
    entity._lineProp.setValue(cartesians);
  }
}

function hexColor(hex) {
  return Cesium.Color.fromCssColorString(hex || "#94a3b8");
}

function formatLocal(iso) {
  if (!iso) return "—";
  return new Date(iso).toLocaleString(undefined, {
    weekday: "short",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function formatUtc(iso) {
  if (!iso) return "—";
  return new Date(iso).toISOString().slice(11, 19) + " UTC";
}

function mmss(seconds) {
  seconds = Math.max(0, Math.floor(seconds));
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

function aosCountdown(aosIso) {
  const sec = (new Date(aosIso) - Date.now()) / 1000;
  return sec > 0 ? mmss(sec) : "now";
}

const PLANE_ICON_CACHE = new Map();

/** Top-down aircraft glyph — canvas so Cesium always gets a valid texture. */
function planeIconDataUrl(hex) {
  const key = hex || "#fbbf24";
  if (PLANE_ICON_CACHE.has(key)) return PLANE_ICON_CACHE.get(key);

  const size = 72;
  const canvas = document.createElement("canvas");
  canvas.width = size;
  canvas.height = size;
  const ctx = canvas.getContext("2d");
  const cx = size / 2;
  const cy = size / 2;

  ctx.clearRect(0, 0, size, size);

  // Soft glow halo.
  const glow = ctx.createRadialGradient(cx, cy, 4, cx, cy, 30);
  glow.addColorStop(0, hex + "55");
  glow.addColorStop(1, hex + "00");
  ctx.fillStyle = glow;
  ctx.beginPath();
  ctx.arc(cx, cy, 30, 0, Math.PI * 2);
  ctx.fill();

  ctx.save();
  ctx.translate(cx, cy);
  ctx.shadowColor = hex;
  ctx.shadowBlur = 10;

  // Fuselage + wings (nose points up = north in icon space).
  ctx.beginPath();
  ctx.moveTo(0, -26);       // nose
  ctx.lineTo(-5, -8);
  ctx.lineTo(-24, 4);       // left wing tip
  ctx.lineTo(-7, 6);
  ctx.lineTo(-9, 22);       // left tail
  ctx.lineTo(0, 16);        // tail notch
  ctx.lineTo(9, 22);        // right tail
  ctx.lineTo(7, 6);
  ctx.lineTo(24, 4);        // right wing tip
  ctx.lineTo(5, -8);
  ctx.closePath();

  ctx.fillStyle = hex;
  ctx.fill();
  ctx.shadowBlur = 0;
  ctx.strokeStyle = "rgba(255,255,255,0.95)";
  ctx.lineWidth = 2.2;
  ctx.lineJoin = "round";
  ctx.stroke();

  // Cockpit highlight.
  ctx.beginPath();
  ctx.ellipse(0, -12, 3.2, 5.5, 0, 0, Math.PI * 2);
  ctx.fillStyle = "rgba(255,255,255,0.55)";
  ctx.fill();

  ctx.restore();

  const url = canvas.toDataURL("image/png");
  PLANE_ICON_CACHE.set(key, url);
  return url;
}

function altColorHex(altM) {
  const ft = (altM || 0) * 3.28084;
  if (ft < 5000) return "#fbbf24";
  if (ft < 18000) return "#fb923c";
  if (ft < 35000) return "#f472b6";
  return "#e2e8f0";
}

function headingRotation(headingDeg) {
  if (headingDeg == null || Number.isNaN(headingDeg)) return 0;
  // Icon nose points up; Cesium billboard rotation is clockwise from +X.
  return Cesium.Math.toRadians(90 - headingDeg);
}

async function installEarthImagery(viewer) {
  const layers = viewer.imageryLayers;
  layers.removeAll();

  const providers = [
    {
      name: "Esri World Imagery",
      create: () =>
        new Cesium.WebMapTileServiceImageryProvider({
          url: "https://services.arcgisonline.com/arcgis/rest/services/World_Imagery/MapServer/WMTS",
          layer: "World_Imagery",
          style: "default",
          format: "image/jpeg",
          tileMatrixSetID: "GoogleMapsCompatible",
          maximumLevel: 19,
          credit: "Esri, Maxar, Earthstar Geographics",
        }),
    },
    {
      name: "Esri World Imagery (REST)",
      create: async () =>
        Cesium.ArcGisMapServerImageryProvider.fromUrl(
          "https://services.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer",
          { enablePickFeatures: false },
        ),
    },
    {
      name: "Esri World Topo",
      create: () =>
        new Cesium.WebMapTileServiceImageryProvider({
          url: "https://services.arcgisonline.com/arcgis/rest/services/World_Topo_Map/MapServer/WMTS",
          layer: "World_Topo_Map",
          style: "default",
          format: "image/jpeg",
          tileMatrixSetID: "GoogleMapsCompatible",
          maximumLevel: 19,
          credit: "Esri World Topo",
        }),
    },
    {
      name: "OpenStreetMap",
      create: () =>
        new Cesium.UrlTemplateImageryProvider({
          url: "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
          credit: "© OpenStreetMap contributors",
          maximumLevel: 19,
        }),
    },
    {
      name: "Natural Earth II",
      create: async () =>
        Cesium.TileMapServiceImageryProvider.fromUrl(
          `${window.CESIUM_BASE_URL}Assets/Textures/NaturalEarthII`,
        ),
    },
  ];

  for (const entry of providers) {
    try {
      const provider = await entry.create();
      if (provider.readyPromise) {
        await provider.readyPromise;
      }
      layers.addImageryProvider(provider);
      console.info("[SatTrack] Earth basemap loaded:", entry.name);
      return entry.name;
    } catch (err) {
      console.warn("[SatTrack] Basemap unavailable:", entry.name, err);
    }
  }

  console.error("[SatTrack] Could not load any Earth imagery layer");
  return null;
}

class CesiumGlobe {
  constructor(containerId, { onEntityPick, onFollowChange } = {}) {
    this.containerId = containerId;
    this.onEntityPick = onEntityPick;
    this.onFollowChange = onFollowChange;
    this.viewer = null;
    this.ready = this._init();
    this.followNorad = null;
    this.followIcao = null;
    this.userNavigated = false;
    this.lastData = null;
    this.satEntities = new Map();
    this.satEntitiesByNorad = new Map();
    this.trackEntities = new Map();
    this.aircraftEntities = new Map();
    this.aircraftTrails = new Map();
    this.lookLine = null;
    this.observerEntity = null;
    this.tracksVersion = -1;
    this.liveFollowNorad = null;
  }

  async _init() {
    // baseLayer: false — skip Cesium Ion (needs token; otherwise solid blue globe).
    this.viewer = new Cesium.Viewer(this.containerId, {
      baseLayer: false,
      animation: false,
      timeline: false,
      fullscreenButton: false,
      vrButton: false,
      geocoder: false,
      homeButton: false,
      infoBox: false,
      sceneModePicker: false,
      navigationHelpButton: false,
      baseLayerPicker: false,
      selectionIndicator: true,
      shouldAnimate: false,
      requestRenderMode: true,
      maximumRenderTimeChange: Infinity,
    });

    const basemap = await installEarthImagery(this.viewer);
    if (!basemap) {
      const status = document.getElementById("status-message");
      if (status) {
        status.textContent =
          "Earth map tiles blocked — allow HTTPS to services.arcgisonline.com and cdn.jsdelivr.net";
      }
    }

    const scene = this.viewer.scene;
    scene.globe.enableLighting = false;
    scene.globe.depthTestAgainstTerrain = false;
    scene.globe.showGroundAtmosphere = true;
    scene.globe.baseColor = Cesium.Color.fromCssColorString("#2a4a6a");
    scene.skyAtmosphere.show = true;
    scene.backgroundColor = Cesium.Color.fromCssColorString("#070b14");
    scene.moon.show = true;
    scene.fog.enabled = false;

    // Fixed Earth — no auto-spin.
    this.viewer.clock.shouldAnimate = false;

    this.viewer.cesiumWidget.creditContainer.classList.add("cesium-credit-compact");

    const handler = this.viewer.screenSpaceEventHandler;
    handler.setInputAction(() => {
      if (this.liveFollowNorad != null) return;
      if (this.followNorad != null || this.followIcao != null) {
        this.userNavigated = true;
        this.viewer.trackedEntity = undefined;
        const hint = document.getElementById("follow-hint");
        if (hint) {
          hint.textContent = "Free view — click a satellite or aircraft to follow";
        }
      }
    }, Cesium.ScreenSpaceEventType.LEFT_DOWN);
    handler.setInputAction(() => {
      if (this.liveFollowNorad != null) return;
      if (this.followNorad != null || this.followIcao != null) {
        this.userNavigated = true;
      }
    }, Cesium.ScreenSpaceEventType.WHEEL);
    handler.setInputAction((click) => {
      const picked = this.viewer.scene.pick(click.position);
      if (!Cesium.defined(picked) || !picked.id?.id) return;
      const entityId = picked.id.id;
      if (entityId.startsWith("adsb-") && !entityId.includes("trail")) {
        this.onEntityPick?.({ type: "aircraft", id: entityId.slice(5) });
        return;
      }
      const satMatch = entityId.match(/^sat-(\d+)-/);
      if (satMatch) {
        this.onEntityPick?.({ type: "satellite", id: parseInt(satMatch[1], 10) });
      }
    }, Cesium.ScreenSpaceEventType.LEFT_CLICK);

    this.lookLine = this.viewer.entities.add({
      id: "look-line",
      show: false,
      polyline: {
        positions: [],
        width: 2,
        material: new Cesium.PolylineDashMaterialProperty({
          color: Cesium.Color.fromCssColorString("#22d3ee"),
          dashLength: 12,
        }),
        arcType: Cesium.ArcType.NONE,
      },
    });

    return this.viewer;
  }

  async _ensureReady() {
    await this.ready;
  }

  _observerPosition(observer) {
    return Cesium.Cartesian3.fromDegrees(
      observer.lon,
      observer.lat,
      observer.elev_m || 180,
    );
  }

  _upsertObserver(observer) {
    if (!observer) return;
    const pos = this._observerPosition(observer);
    const label = observer.name || "Plano, TX";

    if (!this.observerEntity) {
      this.observerEntity = this.viewer.entities.add({
        id: "ground-station",
        position: pos,
        point: {
          pixelSize: 14,
          color: Cesium.Color.fromCssColorString("#4ade80"),
          outlineColor: Cesium.Color.BLACK,
          outlineWidth: 2,
          disableDepthTestDistance: Number.POSITIVE_INFINITY,
        },
        ellipse: {
          semiMajorAxis: 95000,
          semiMinorAxis: 95000,
          material: Cesium.Color.fromCssColorString("#4ade80").withAlpha(0.12),
          outline: true,
          outlineColor: Cesium.Color.fromCssColorString("#4ade80").withAlpha(0.85),
          outlineWidth: 2,
          height: 0,
        },
        label: {
          text: label,
          font: "600 13px JetBrains Mono, monospace",
          fillColor: Cesium.Color.fromCssColorString("#4ade80"),
          outlineColor: Cesium.Color.BLACK,
          outlineWidth: 3,
          style: Cesium.LabelStyle.FILL_AND_OUTLINE,
          pixelOffset: new Cesium.Cartesian2(0, -22),
          disableDepthTestDistance: Number.POSITIVE_INFINITY,
        },
      });
    } else {
      setEntityPosition(this.observerEntity, pos);
      this.observerEntity.label.text = label;
    }

    const obsLabel = document.getElementById("observer-label");
    if (obsLabel) obsLabel.textContent = label;
  }

  _trackPositions(points) {
    const flat = [];
    for (const p of points) {
      flat.push(p.lon, p.lat, (p.alt_km || 0) * 1000);
    }
    return Cesium.Cartesian3.fromDegreesArrayHeights(flat);
  }

  async update(data) {
    await this._ensureReady();
    this.lastData = data;

    const { observer, satellites, tracks, watcher } = data;
    this._upsertObserver(observer);

    const targetName =
      watcher?.active_pass?.satellite ||
      watcher?.next_pass?.satellite ||
      data.next_pass?.satellite;
    const targetNorad =
      watcher?.active_pass?.norad_id ??
      watcher?.next_pass?.norad_id ??
      data.next_pass?.norad_id ??
      satellites?.find((s) => s.name === targetName)?.norad_id ??
      null;
    const isLivePass =
      watcher?.phase === "recording" || watcher?.phase === "decoding";

    const seenSats = new Set();
    const seenTracks = new Set();

    for (const sat of satellites || []) {
      seenSats.add(sat.name);
      const color = hexColor(sat.color);
      const isTarget = sat.norad_id === targetNorad;
      const isSelected = sat.norad_id === this.followNorad;
      const emphasis = isSelected || (this.followNorad == null && isTarget);
      const altM = (sat.alt_km || 400) * 1000;
      const pos = Cesium.Cartesian3.fromDegrees(sat.lon, sat.lat, altM);

      let ent = this.satEntities.get(sat.name);
      if (!ent) {
        ent = this.viewer.entities.add({
          id: `sat-${sat.norad_id}-${sat.name}`,
          name: sat.name,
          position: pos,
          point: {
            pixelSize: 11,
            color,
            outlineColor: Cesium.Color.WHITE.withAlpha(0.9),
            outlineWidth: 1,
            disableDepthTestDistance: Number.POSITIVE_INFINITY,
          },
          label: {
            text: sat.name,
            font: "12px JetBrains Mono, monospace",
            fillColor: color,
            outlineColor: Cesium.Color.BLACK,
            outlineWidth: 2,
            style: Cesium.LabelStyle.FILL_AND_OUTLINE,
            pixelOffset: new Cesium.Cartesian2(0, -16),
            show: false,
            disableDepthTestDistance: Number.POSITIVE_INFINITY,
          },
        });
        this.satEntities.set(sat.name, ent);
        this.satEntitiesByNorad.set(sat.norad_id, ent);
      }

      setEntityPosition(ent, pos);
      const isLiveTarget = isTarget && isLivePass;
      ent.point.pixelSize = isSelected ? 18 : isLiveTarget ? 16 : isTarget ? 14 : 10;
      ent.point.color = isLiveTarget ? Cesium.Color.fromCssColorString("#fb923c") : color;
      ent.point.outlineWidth = isSelected ? 3 : 1;
      ent.point.outlineColor = isSelected
        ? Cesium.Color.WHITE
        : Cesium.Color.BLACK.withAlpha(0.8);
      ent.label.show = isSelected || isTarget;
      ent.label.text = `${sat.name}\nel ${sat.elevation_deg.toFixed(1)}° az ${sat.azimuth_deg.toFixed(0)}°`;

      const trackPoints = tracks?.[sat.name];
      if (trackPoints?.length > 1 && data.tracks_version !== this.tracksVersion) {
        seenTracks.add(sat.name);
        const positions = this._trackPositions(trackPoints);
        let track = this.trackEntities.get(sat.name);
        if (!track) {
          track = this.viewer.entities.add({
            id: `track-${sat.name}`,
            polyline: {
              positions,
              width: emphasis ? 3 : 1.5,
              material: color.withAlpha(emphasis ? 0.9 : 0.28),
              arcType: Cesium.ArcType.NONE,
            },
          });
          this.trackEntities.set(sat.name, track);
        } else {
          setPolylinePositions(track, positions);
          track.polyline.width = emphasis ? 3 : 1.5;
          track.polyline.material = color.withAlpha(emphasis ? 0.9 : 0.28);
        }
        track.show = true;
      } else if (trackPoints?.length > 1) {
        seenTracks.add(sat.name);
      }
    }

    if (data.tracks_version != null && data.tracks_version !== this.tracksVersion) {
      this.tracksVersion = data.tracks_version;
    }

    for (const [name, ent] of this.satEntities) {
      if (!seenSats.has(name)) ent.show = false;
    }
    for (const [name, ent] of this.trackEntities) {
      if (!seenTracks.has(name)) ent.show = false;
    }

    const lineSat = this.followNorad != null
      ? satellites?.find((s) => s.norad_id === this.followNorad)
      : satellites?.find((s) => s.norad_id === targetNorad);

    if (lineSat && observer) {
      this.lookLine.show = true;
      const isLive = watcher?.phase === "recording" || watcher?.phase === "decoding";
      if (this._lookLineLive !== isLive) {
        this._lookLineLive = isLive;
        const lineColor = isLive ? "#fb923c" : "#22d3ee";
        this.lookLine.polyline.material = new Cesium.PolylineDashMaterialProperty({
          color: Cesium.Color.fromCssColorString(lineColor),
          dashLength: isLive ? 8 : 12,
        });
        this.lookLine.polyline.width = isLive ? 3 : 2;
      }
      setPolylinePositions(this.lookLine, [
        this._observerPosition(observer),
        Cesium.Cartesian3.fromDegrees(lineSat.lon, lineSat.lat, (lineSat.alt_km || 400) * 1000),
      ]);
    } else {
      this.lookLine.show = false;
    }

    const label = document.getElementById("reticle-label");
    const isLive = watcher?.phase === "recording" || watcher?.phase === "decoding";
    label.classList.toggle("visible", !!lineSat);
    label.classList.toggle("recording", isLive && !!lineSat);
    if (lineSat) {
      const prefix = isLive
        ? watcher?.phase === "decoding"
          ? "DEC · "
          : "REC · "
        : "";
      label.textContent = `${prefix}${lineSat.name} · el ${lineSat.elevation_deg.toFixed(1)}° · az ${lineSat.azimuth_deg.toFixed(0)}°`;
    }

    if (this.liveFollowNorad != null) {
      const liveEnt = this.satEntitiesByNorad.get(this.liveFollowNorad);
      if (liveEnt) {
        this.followNorad = this.liveFollowNorad;
        this.followIcao = null;
        this.viewer.trackedEntity = liveEnt;
        this.viewer.selectedEntity = liveEnt;
      }
    } else if (this.followNorad != null && !this.userNavigated) {
      const followEnt = this.satEntitiesByNorad.get(this.followNorad);
      if (followEnt) this.viewer.trackedEntity = followEnt;
    } else if (this.followIcao != null && !this.userNavigated) {
      const followEnt = this.aircraftEntities.get(this.followIcao);
      if (followEnt?.show) this.viewer.trackedEntity = followEnt;
    }

    this._updateAircraft(data.adsb);
    this.viewer.scene.requestRender();
  }

  _updateAircraft(adsb) {
    const list = adsb?.aircraft || [];
    const seen = new Set();

    for (const ac of list) {
      seen.add(ac.icao);
      const altM = ac.alt_m || 0;
      const pos = Cesium.Cartesian3.fromDegrees(ac.lon, ac.lat, Math.max(altM, 50));
      const hex = altColorHex(altM);
      const color = Cesium.Color.fromCssColorString(hex);
      const icon = planeIconDataUrl(hex);
      const label = ac.callsign || ac.icao;
      const sub = [
        ac.alt_ft != null ? `${Math.round(ac.alt_ft).toLocaleString()} ft` : null,
        ac.speed_kts != null ? `${Math.round(ac.speed_kts)} kts` : null,
        ac.heading != null ? `hdg ${Math.round(ac.heading)}°` : null,
      ].filter(Boolean).join(" · ");
      const rot = headingRotation(ac.heading);
      const isSelected = ac.icao === this.followIcao;

      let ent = this.aircraftEntities.get(ac.icao);
      if (!ent) {
        ent = this.viewer.entities.add({
          id: `adsb-${ac.icao}`,
          name: label,
          position: pos,
          billboard: {
            image: icon,
            scale: 1.15,
            color: Cesium.Color.WHITE,
            rotation: rot,
            verticalOrigin: Cesium.VerticalOrigin.CENTER,
            horizontalOrigin: Cesium.HorizontalOrigin.CENTER,
            disableDepthTestDistance: Number.POSITIVE_INFINITY,
            scaleByDistance: new Cesium.NearFarScalar(5e4, 1.35, 2.5e7, 0.45),
            translucencyByDistance: new Cesium.NearFarScalar(5e4, 1.0, 2.5e7, 0.75),
          },
          label: {
            text: label,
            font: "600 11px JetBrains Mono, monospace",
            fillColor: color,
            outlineColor: Cesium.Color.BLACK,
            outlineWidth: 3,
            style: Cesium.LabelStyle.FILL_AND_OUTLINE,
            pixelOffset: new Cesium.Cartesian2(0, -22),
            disableDepthTestDistance: Number.POSITIVE_INFINITY,
            scaleByDistance: new Cesium.NearFarScalar(5e4, 1.0, 2.5e7, 0.0),
          },
          description: sub || ac.icao,
        });
        this.aircraftEntities.set(ac.icao, ent);
      }

      setEntityPosition(ent, pos);
      ent.show = true;
      if (ent._iconHex !== hex) {
        ent._iconHex = hex;
        ent.billboard.image = icon;
      }
      ent.billboard.scale = isSelected ? 1.65 : 1.15;
      ent.billboard.rotation = rot;
      ent.label.text = sub ? `${label}\n${sub}` : label;
      ent.label.fillColor = color;
      ent.label.outlineWidth = isSelected ? 4 : 3;

      // Short heading tick — subtle vector showing direction of travel.
      if (ac.heading != null) {
        const trailLenM = Math.min(18000, Math.max(4000, (ac.speed_m_s || 120) * 30));
        const rad = Cesium.Math.toRadians(ac.heading);
        const dLat = (trailLenM / 6378137) * (180 / Math.PI) * Math.cos(rad);
        const dLon = (trailLenM / 6378137) * (180 / Math.PI) * Math.sin(rad) / Math.cos(ac.lat * Math.PI / 180);
        const tail = Cesium.Cartesian3.fromDegrees(ac.lon - dLon, ac.lat - dLat, Math.max(altM, 50));
        let trail = this.aircraftTrails.get(ac.icao);
        if (!trail) {
          trail = this.viewer.entities.add({
            id: `adsb-trail-${ac.icao}`,
            polyline: {
              positions: [tail, pos],
              width: isSelected ? 3.5 : 2,
              material: color.withAlpha(isSelected ? 0.85 : 0.55),
              arcType: Cesium.ArcType.NONE,
            },
          });
          this.aircraftTrails.set(ac.icao, trail);
        } else {
          setPolylinePositions(trail, [tail, pos]);
          trail.polyline.width = isSelected ? 3.5 : 2;
          trail.polyline.material = color.withAlpha(isSelected ? 0.85 : 0.55);
          trail.show = true;
        }
      }
    }

    for (const [icao, ent] of this.aircraftEntities) {
      if (!seen.has(icao)) ent.show = false;
    }
    for (const [icao, trail] of this.aircraftTrails) {
      if (!seen.has(icao)) trail.show = false;
    }
  }

  _showFollowHint(name) {
    const hint = document.getElementById("follow-hint");
    if (!hint) return;
    hint.textContent = `Following ${name} — drag globe to free view`;
    hint.classList.add("visible");
  }

  _notifyFollowChange() {
    this.onFollowChange?.({
      norad: this.followNorad,
      icao: this.followIcao,
    });
  }

  setLiveFollow(norad) {
    this.liveFollowNorad = norad;
    if (norad != null) {
      this.userNavigated = false;
      const sat = this.lastData?.satellites?.find((s) => s.norad_id === norad);
      if (sat) this._showFollowHint(sat.name);
    }
  }

  clearFollow() {
    this.followNorad = null;
    this.followIcao = null;
    this.userNavigated = false;
    this.viewer.trackedEntity = undefined;
    this.viewer.selectedEntity = undefined;
    const hint = document.getElementById("follow-hint");
    if (hint) {
      hint.classList.remove("visible");
      hint.textContent = "Following — drag globe to free view";
    }
    this._notifyFollowChange();
    if (this.lastData) return this.update(this.lastData);
  }

  async selectSatellite(norad, { follow = true } = {}) {
    await this._ensureReady();
    this.followNorad = norad;
    this.followIcao = null;
    this.userNavigated = !follow;

    const sat = this.lastData?.satellites?.find((s) => s.norad_id === norad);
    if (!sat) {
      this._notifyFollowChange();
      return norad;
    }

    const ent = this.satEntitiesByNorad.get(norad) || this.satEntities.get(sat.name);
    if (!ent) {
      this._notifyFollowChange();
      return norad;
    }

    if (follow) {
      this.viewer.trackedEntity = ent;
      await this.viewer.flyTo(ent, {
        duration: 1.2,
        offset: new Cesium.HeadingPitchRange(
          Cesium.Math.toRadians(25),
          Cesium.Math.toRadians(-35),
          (sat.alt_km || 400) * 1000 * 4.5,
        ),
      });
      this.userNavigated = false;
      this._showFollowHint(sat.name);
    }

    this.viewer.selectedEntity = ent;
    if (this.lastData) await this.update(this.lastData);
    this._notifyFollowChange();
    return norad;
  }

  async selectAircraft(icao, { follow = true } = {}) {
    await this._ensureReady();
    this.followNorad = null;
    this.followIcao = icao;
    this.userNavigated = !follow;

    const ac = this.lastData?.adsb?.aircraft?.find((a) => a.icao === icao);
    const ent = this.aircraftEntities.get(icao);
    if (!ent) {
      this._notifyFollowChange();
      return icao;
    }

    const label = ac?.callsign || ac?.icao || icao;

    if (follow && ac) {
      const altM = Math.max(ac.alt_m || 5000, 50);
      const range = Math.min(120_000, Math.max(12_000, altM * 14));
      this.viewer.trackedEntity = ent;
      await this.viewer.flyTo(ent, {
        duration: 1.2,
        offset: new Cesium.HeadingPitchRange(
          Cesium.Math.toRadians((ac.heading ?? 0) + 90),
          Cesium.Math.toRadians(-38),
          range,
        ),
      });
      this.userNavigated = false;
      this._showFollowHint(label);
    }

    this.viewer.selectedEntity = ent;
    if (this.lastData) await this.update(this.lastData);
    this._notifyFollowChange();
    return icao;
  }

  getFollowNorad() {
    return this.followNorad;
  }

  getFollowIcao() {
    return this.followIcao;
  }

  _homeView(observer) {
    const center = this._observerPosition(observer);
    return {
      sphere: new Cesium.BoundingSphere(center, 800),
      offset: new Cesium.HeadingPitchRange(
        0,
        Cesium.Math.toRadians(-42),
        185_000,
      ),
    };
  }

  async _flyHome(observer, duration = 1.4) {
    const { sphere, offset } = this._homeView(observer);
    await this.viewer.camera.flyToBoundingSphere(sphere, { offset, duration });
  }

  async goHome(observer) {
    await this._ensureReady();
    if (!observer) return;
    this.followNorad = null;
    this.followIcao = null;
    this.userNavigated = false;
    this.viewer.trackedEntity = undefined;
    this.viewer.selectedEntity = undefined;
    const hint = document.getElementById("follow-hint");
    if (hint) hint.classList.remove("visible");
    this._notifyFollowChange();
    await this._flyHome(observer, 1.4);
    if (this.lastData) await this.update(this.lastData);
  }

  async flyToObserver(observer) {
    await this._ensureReady();
    if (!observer) return;
    await this._flyHome(observer, 0.5);
  }
}

class Dashboard {
  constructor() {
    this.globe = new CesiumGlobe("cesiumContainer", {
      onEntityPick: ({ type, id }) => {
        if (type === "satellite") this._selectSat(id);
        else if (type === "aircraft") this._selectAircraft(id);
      },
      onFollowChange: () => this._syncFollowUi(),
    });
    this.globe.ready.then(() => {
      document.getElementById("globe-home-btn")?.addEventListener("click", () => {
        const obs = this.globe.lastData?.observer;
        if (obs) this.globe.goHome(obs);
      });
      this.connect();
    });
    this.ws = null;
    this.reconnectTimer = null;
    this._renderTick = 0;
    this._renderPending = false;
    this._pendingData = null;
    this._autoFocusKey = null;
    this._recordingFollowNorad = null;
    this._startClock();

    document.getElementById("next-pass")?.addEventListener("click", (ev) => {
      const card = ev.target.closest(".pass-card[data-norad]");
      if (!card?.dataset.norad) return;
      const norad = parseInt(card.dataset.norad, 10);
      if (Number.isNaN(norad)) return;
      this._selectSat(norad);
    });
  }

  connect() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    this.ws = new WebSocket(`${proto}://${location.host}/ws`);
    this.ws.onopen = () => this._setConn(true);
    this.ws.onclose = () => {
      this._setConn(false);
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = setTimeout(() => this.connect(), 3000);
    };
    this.ws.onmessage = (ev) => {
      try {
        this.render(JSON.parse(ev.data));
      } catch (e) {
        console.warn("bad payload", e);
      }
    };
  }

  render(data) {
    this._pendingData = data;
    if (this._renderPending) return;
    this._renderPending = true;
    requestAnimationFrame(() => {
      this._renderPending = false;
      const payload = this._pendingData;
      if (payload) void this._renderFrame(payload);
    });
  }

  _setConn(ok) {
    const pill = document.getElementById("conn-pill");
    pill.textContent = ok ? "live" : "reconnecting";
    pill.className = `pill ${ok ? "connected" : "disconnected"}`;
  }

  _startClock() {
    const tick = () => {
      document.getElementById("utc-clock").textContent = formatUtc(new Date().toISOString());
    };
    tick();
    setInterval(tick, 1000);
  }

  async _renderFrame(data) {
    this._renderTick += 1;
    const slow = this._renderTick === 1 || this._renderTick % 5 === 0;
    const { observer, watcher, schedule, satellites, telemetry, config, next_pass, adsb } = data;

    const phase = watcher?.phase || "offline";
    const phasePill = document.getElementById("phase-pill");
    phasePill.textContent = phase;
    phasePill.className = `pill phase ${phase}`;

    document.getElementById("status-message").textContent =
      watcher?.message || "Watcher not running — showing predicted schedule only";

    this._renderRecordingState(watcher);
    this._renderNextPass(next_pass, watcher, schedule);

    const isLive = watcher?.phase === "recording" || watcher?.phase === "decoding";
    if (isLive) {
      this._renderMetrics(watcher);
    }

    if (slow) {
      document.getElementById("observer-name").textContent =
        `${observer?.name || config?.observer?.name || "Ground station"} · ${config?.backend || watcher?.backend || "—"}`;
      this._renderMetrics(watcher);
      this._renderSchedule(schedule, watcher);
      this._renderSatellites(satellites);
      this._renderAdsb(adsb, config);
      this._renderTelemetry(telemetry);
      this._renderLegend(satellites, adsb);
    }

    await this.globe.update(data);
    this._syncRecordingFocus(watcher, satellites);

    if (!this._initialView && observer) {
      this._initialView = true;
      await this.globe.flyToObserver(observer);
    }
  }

  _passNorad(pass, satellites) {
    if (!pass) return null;
    if (pass.norad_id) return pass.norad_id;
    return satellites?.find((s) => s.name === pass.satellite)?.norad_id ?? null;
  }

  _renderRecordingState(watcher) {
    const statusCard = document.getElementById("status-card");
    const progressWrap = document.getElementById("recording-progress-wrap");
    const progressLabel = document.getElementById("recording-progress-label");
    const progressBar = document.getElementById("recording-progress-bar");
    const pane = document.getElementById("recording-pane");
    const phase = watcher?.phase;
    const isRecording = phase === "recording" && watcher?.recording;
    const isDecoding = phase === "decoding";
    const isLive = isRecording || isDecoding;
    const pass = watcher?.active_pass;

    statusCard?.classList.toggle("recording", isRecording);
    statusCard?.classList.toggle("decoding", isDecoding);

    if (pane) {
      if (isLive && pass) {
        pane.hidden = false;
        pane.classList.toggle("decoding", isDecoding);

        const titleEl = document.getElementById("recording-pane-title");
        const statusEl = document.getElementById("recording-pane-status");
        const satEl = document.getElementById("recording-pane-sat");
        const elapsedEl = document.getElementById("recording-pane-elapsed");
        const remainingEl = document.getElementById("recording-pane-remaining");
        const barEl = document.getElementById("recording-pane-bar");
        const metaEl = document.getElementById("recording-pane-meta");

        if (titleEl) titleEl.textContent = isDecoding ? "Decoding" : "Recording";
        if (satEl) satEl.textContent = pass.satellite;

        if (statusEl) {
          statusEl.classList.remove("disconnected", "processing");
          if (isDecoding) {
            statusEl.textContent = "processing";
            statusEl.classList.add("processing");
          } else if (watcher?.dry_run) {
            statusEl.textContent = "simulated";
            statusEl.classList.add("processing");
          } else {
            statusEl.textContent = "listening";
          }
        }

        if (isRecording && elapsedEl && remainingEl && barEl) {
          const elapsed = watcher.recording.elapsed_s;
          const remaining = watcher.recording.ends_in_s;
          elapsedEl.textContent = mmss(elapsed);
          remainingEl.textContent = mmss(remaining);
          const total = Math.max(1, elapsed + remaining);
          barEl.style.width = `${Math.min(100, (elapsed / total) * 100)}%`;
          barEl.style.animation = "none";
        } else if (barEl) {
          if (elapsedEl) elapsedEl.textContent = "—";
          if (remainingEl) remainingEl.textContent = "—";
          barEl.style.width = "35%";
          barEl.style.animation = "";
        }

        if (metaEl) {
          const bits = [
            `${pass.freq_mhz} MHz`,
            pass.decoder,
            watcher?.backend ? `SDR · ${watcher.backend}` : null,
          ].filter(Boolean);
          metaEl.textContent = bits.join(" · ");
        }
      } else {
        pane.hidden = true;
        pane.classList.remove("decoding");
      }
    }

    if (progressWrap && progressLabel && progressBar) {
      if (isRecording) {
        progressWrap.hidden = false;
        const elapsed = watcher.recording.elapsed_s;
        const remaining = watcher.recording.ends_in_s;
        const total = Math.max(1, elapsed + remaining);
        progressLabel.textContent = `Capture progress · ${Math.round((elapsed / total) * 100)}%`;
        progressBar.style.width = `${Math.min(100, (elapsed / total) * 100)}%`;
        progressBar.style.animation = "none";
      } else if (isDecoding) {
        progressWrap.hidden = false;
        progressLabel.textContent = "Decoder running";
        progressBar.style.width = "35%";
        progressBar.style.animation = "";
      } else {
        progressWrap.hidden = true;
        progressBar.style.width = "0%";
      }
    }
  }

  _syncRecordingFocus(watcher, satellites) {
    const phase = watcher?.phase;
    const isLive = phase === "recording" || phase === "decoding";

    if (!isLive) {
      this._autoFocusKey = null;
      this._recordingFollowNorad = null;
      this.globe.setLiveFollow(null);
      return;
    }

    const pass = watcher?.active_pass;
    if (!pass) return;

    const norad = this._passNorad(pass, satellites);
    if (!norad) return;

    const key = `${norad}:${pass.aos}`;
    this._recordingFollowNorad = norad;
    this.globe.setLiveFollow(norad);

    if (this._autoFocusKey !== key) {
      this._autoFocusKey = key;
      void this.globe.selectSatellite(norad, { follow: true });
      this._syncFollowUi();
    }
  }

  _renderMetrics(watcher) {
    const el = document.getElementById("status-metrics");
    if (!watcher) {
      el.innerHTML = "";
      return;
    }
    const items = [
      ["Queued", watcher.queued_passes ?? "—"],
      ["Backend", watcher.backend ?? "—"],
    ];
    if (watcher.recording) {
      items.push(["Elapsed", mmss(watcher.recording.elapsed_s)]);
      items.push(["Ends in", mmss(watcher.recording.ends_in_s)]);
    }
    el.innerHTML = items
      .map(
        ([label, value]) =>
          `<div class="metric"><div class="label">${label}</div><div class="value">${value}</div></div>`,
      )
      .join("");
  }

  _resolveNextPass(nextPass, watcher, schedule) {
    const phase = watcher?.phase;
    const active = watcher?.active_pass;
    const livePhase =
      phase === "recording" || phase === "decoding" || phase === "waiting";

    if (livePhase && active) {
      return active;
    }

    let p = nextPass || watcher?.next_pass;
    if (!p && schedule?.length) {
      const now = Date.now();
      p = schedule
        .filter((s) => new Date(s.los).getTime() > now)
        .sort((a, b) => new Date(a.aos) - new Date(b.aos))[0];
    }

    return p || nextPass;
  }

  _renderNextPass(nextPass, watcher, schedule) {
    const el = document.getElementById("next-pass");
    const cardWrap = document.getElementById("next-pass-card");
    const p = this._resolveNextPass(nextPass, watcher, schedule);
    if (!p) {
      cardWrap?.classList.remove("connected");
      el.innerHTML = '<p class="empty">No upcoming passes in horizon</p>';
      return;
    }
    const norad = this._passNorad(p, this.globe.lastData?.satellites);
    const follow = this.globe.getFollowNorad();
    const selected = norad != null && follow === norad;
    const phase = watcher?.phase;
    const isConnected =
      phase === "recording" && watcher?.active_pass?.satellite === p.satellite;
    const isDecoding =
      phase === "decoding" && watcher?.active_pass?.satellite === p.satellite;
    cardWrap?.classList.toggle("connected", isConnected);
    const now = Date.now();
    const overhead = new Date(p.aos).getTime() <= now && new Date(p.los).getTime() > now;
    const timingLine = overhead
      ? `Overhead now · LOS <span class="highlight">${formatLocal(p.los)}</span>`
      : `AOS <span class="highlight">${formatLocal(p.aos)}</span> (${aosCountdown(p.aos)})`;
    const statusBadge = isConnected
      ? '<span class="connected-badge">CONNECTED</span>'
      : isDecoding
        ? " ◉ DECODING"
        : overhead
          ? " ◉ OVERHEAD"
          : "";
    const cls = [
      "pass-card",
      "selectable",
      selected ? "selected" : "",
      isConnected ? "connected" : "",
      isDecoding ? "decoding" : "",
    ]
      .filter(Boolean)
      .join(" ");
    el.innerHTML = `
      <div class="${cls}" data-norad="${norad ?? ""}" data-sat="${p.satellite}" role="button" tabindex="0">
        <div class="name">${p.satellite}${statusBadge}</div>
        <div>${timingLine}</div>
        <div>Max el <span class="highlight">${p.max_elevation_deg}°</span> · ${p.duration_s}s · ${p.freq_mhz} MHz</div>
        <div>${p.decoder} · ${p.direction}</div>
        <div class="focus-hint">Click to focus on globe</div>
      </div>`;
  }

  _renderSchedule(schedule, watcher) {
    const el = document.getElementById("schedule-list");
    if (!schedule?.length) {
      el.innerHTML = '<p class="empty">No schedule data</p>';
      return;
    }
    const activeName = watcher?.active_pass?.satellite;
    const follow = this.globe.getFollowNorad();
    el.innerHTML = schedule
      .slice(0, 12)
      .map((p) => {
        const sat = this.globe.lastData?.satellites?.find((s) => s.name === p.satellite);
        const cls = [
          "schedule-item",
          p.status,
          p.satellite === activeName ? "active" : "",
          sat && sat.norad_id === follow ? "selected" : "",
        ].join(" ");
        return `
        <div class="${cls}" data-norad="${sat?.norad_id ?? ""}" data-sat="${p.satellite}">
          <span class="el-badge">${p.max_elevation_deg}°</span>
          <span>${p.satellite}<br><small>${formatLocal(p.aos)}${p.window ? ` · ${p.window}` : ""}</small></span>
          <span class="sat-meta">${p.status}</span>
        </div>`;
      })
      .join("");

    el.querySelectorAll(".schedule-item[data-norad]").forEach((node) => {
      if (!node.dataset.norad) return;
      node.addEventListener("click", () => {
        this._selectSat(parseInt(node.dataset.norad, 10));
      });
    });
  }

  _selectSat(norad) {
    if (norad !== this._recordingFollowNorad) {
      this.globe.setLiveFollow(null);
    }
    this.globe.selectSatellite(norad, { follow: true });
    this._syncFollowUi();
  }

  _selectAircraft(icao) {
    this.globe.selectAircraft(icao, { follow: true });
    this._syncFollowUi();
  }

  _syncFollowUi() {
    const norad = this.globe.getFollowNorad();
    const icao = this.globe.getFollowIcao();
    document
      .querySelectorAll(".sat-item, .schedule-item, .pass-card.selectable, .aircraft-item")
      .forEach((n) => n.classList.remove("selected"));
    if (norad != null) {
      document.querySelectorAll(`[data-norad="${norad}"]`).forEach((n) => n.classList.add("selected"));
    }
    if (icao) {
      document.querySelectorAll(`[data-icao="${icao}"]`).forEach((n) => n.classList.add("selected"));
    }
  }

  _renderSatellites(sats) {
    const el = document.getElementById("sat-list");
    if (!sats?.length) {
      el.innerHTML = '<p class="empty">No satellites</p>';
      return;
    }
    const follow = this.globe.getFollowNorad();
    el.innerHTML = sats
      .map(
        (s) => `
      <div class="sat-item ${s.norad_id === follow ? "selected" : ""}" data-norad="${s.norad_id}">
        <span class="sat-dot" style="color:${s.color}"></span>
        <span>${s.name}<br><small>${s.decoder} · ${s.freq_mhz} MHz</small></span>
        <span class="sat-meta">
          <span class="el-badge ${s.visible ? "above" : ""}">${s.elevation_deg.toFixed(1)}° el</span>
          ${s.alt_km} km
        </span>
      </div>`,
      )
      .join("");

    el.querySelectorAll(".sat-item").forEach((node) => {
      node.addEventListener("click", () => {
        this._selectSat(parseInt(node.dataset.norad, 10));
      });
    });
  }

  _renderAdsb(adsb, config) {
    const pill = document.getElementById("adsb-pill");
    const statusEl = document.getElementById("adsb-status");
    const listEl = document.getElementById("aircraft-list");
    const link = document.getElementById("kismet-map-link");

    const enabled = adsb?.enabled ?? config?.kismet?.enabled;
    if (!enabled) {
      pill.textContent = "adsb off";
      pill.className = "pill adsb-pill offline";
      statusEl.textContent = "Kismet ADS-B integration disabled in config.json";
      listEl.innerHTML = "";
      if (link) link.style.display = "none";
      return;
    }

    if (link) {
      link.href = adsb?.gui_url || config?.kismet?.gui_url || "#";
      link.style.display = adsb?.gui_url ? "inline" : "none";
    }

    const svc = adsb?.service_up;
    const svcNote = svc === false ? " (kismet service stopped — SDR capture?)" : svc === true ? "" : "";

    if (adsb?.online && adsb.count > 0) {
      pill.textContent = `adsb ${adsb.count}`;
      pill.className = "pill adsb-pill live";
      statusEl.textContent = `Live from Kismet · ${adsb.count} aircraft${adsb.raw_devices ? ` (${adsb.raw_devices} in map feed)` : ""}${svcNote}`;
    } else if (adsb?.online && adsb.raw_devices > 0) {
      pill.textContent = `adsb 0`;
      pill.className = "pill adsb-pill live";
      statusEl.textContent = `Kismet returned ${adsb.raw_devices} devices but none with a position lock${svcNote}`;
    } else if (adsb?.online) {
      pill.textContent = "adsb 0";
      pill.className = "pill adsb-pill live";
      statusEl.textContent = `Kismet connected — no aircraft in range${svcNote}`;
    } else {
      pill.textContent = "adsb down";
      pill.className = "pill adsb-pill offline";
      statusEl.textContent = adsb?.auth_required
        ? "Kismet requires login on :2501 — set kismet.password or KISMET_PASSWORD in config/env"
        : adsb?.error
          ? `Kismet unreachable: ${adsb.error}${svcNote}`
          : `Waiting for Kismet ADS-B feed${svcNote}`;
    }

    const planes = adsb?.aircraft || [];
    const followIcao = this.globe.getFollowIcao();
    if (!planes.length) {
      listEl.innerHTML = '<p class="empty">No aircraft tracked</p>';
      return;
    }

    listEl.innerHTML = planes
      .slice(0, 16)
      .map(
        (ac) => `
      <div class="aircraft-item ${ac.icao === followIcao ? "selected" : ""}" data-icao="${ac.icao}">
        <span class="callsign">${ac.callsign || ac.icao}</span>
        <span>${ac.model || ac.operator || ac.icao}<br><small>${Math.round(ac.alt_ft || 0).toLocaleString()} ft · ${ac.speed_kts != null ? Math.round(ac.speed_kts) + " kts" : "—"}</small></span>
        <span class="sat-meta">${ac.heading != null ? Math.round(ac.heading) + "°" : "—"}</span>
      </div>`,
      )
      .join("");

    listEl.querySelectorAll(".aircraft-item").forEach((node) => {
      node.addEventListener("click", () => {
        this._selectAircraft(node.dataset.icao);
      });
    });
  }

  _renderTelemetry(telemetry) {
    const statsEl = document.getElementById("telemetry-stats");
    const capEl = document.getElementById("capture-list");
    const stats = telemetry?.stats || {};
    statsEl.innerHTML = Object.entries(stats)
      .map(
        ([k, v]) =>
          `<div class="metric"><div class="label">${k.replace(/_/g, " ")}</div><div class="value">${v ?? "—"}</div></div>`,
      )
      .join("");

    const caps = telemetry?.recent_captures || [];
    if (!caps.length) {
      capEl.innerHTML = '<p class="empty">No captures yet</p>';
      return;
    }
    capEl.innerHTML = caps
      .map(
        (c) => `
      <div class="capture-item">
        <span class="${c.ok ? "ok" : "fail"}">${c.ok ? "✓" : "✗"}</span>
        <span>${c.satellite}<br><small>${formatLocal(c.aos)}</small></span>
        <span class="sat-meta">score ${c.quality_score ?? "—"}</span>
      </div>`,
      )
      .join("");
  }

  _renderLegend(sats, adsb) {
    const el = document.getElementById("legend");
    const satBits = (sats || [])
      .map(
        (s) =>
          `<span class="legend-item"><span class="legend-dot" style="background:${s.color}"></span>${s.name.split(" ")[0]}</span>`,
      )
      .join("");
    const adsbBit =
      adsb?.count > 0
        ? `<span class="legend-item"><span class="legend-dot" style="background:#fbbf24"></span>ADS-B (${adsb.count})</span>`
        : `<span class="legend-item"><span class="legend-dot" style="background:#fbbf24"></span>ADS-B</span>`;
    el.innerHTML = satBits + adsbBit;
  }
}

new Dashboard();
