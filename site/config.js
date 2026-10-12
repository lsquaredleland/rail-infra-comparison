// Tunable numbers of the map. Edit here; reload the page. No build step.
//
// "Scale" is the zoom of the map in pixels per metre. About how much ground a ~880 px wide map shows:
//   0.0012 = 730 km   0.002 = 440 km   0.004 = 220 km   0.005 = 176 km   0.008 = 110 km   0.012 = 73 km
//   0.02 = 44 km      0.04 = 22 km     0.07 = 12.6 km    0.1 = 8.8 km     0.5 = 1.8 km
// A station is drawn once the scale is at or above its dotMinScale, and named once it is at or above labelMinScale.
window.RA_CONFIG = {
  zoom: { min: 0.0008, max: 2 },            // limits of the zoom
  stations: {
    // Ranks as shown in the legend. Dot radius is max(dotMinRadius, base radius x dotFactor), in pixels.
    ranks: [
      { name: 'single-line stop',          dotMinScale: 0.008, labelMinScale: 0.04,   dotMinRadius: 1.7, dotFactor: 0.62, labelFont: '500 11px' },
      { name: 'interchange (2-3 lines)',   dotMinScale: 0.004, labelMinScale: 0.02,   dotMinRadius: 3.2, dotFactor: 1.15, labelFont: '600 11.5px' },
      { name: 'major interchange',         dotMinScale: 0.002, labelMinScale: 0.009,  dotMinRadius: 4.2, dotFactor: 1.55, labelFont: '700 12px' },
      { name: 'high-speed stop',           dotMinScale: 0.02,  labelMinScale: 0.04,   dotMinRadius: 4.2, dotFactor: 1.5,  labelFont: '700 12px' },   // stops along a high-speed line
      { name: 'main high-speed station',   dotMinScale: 0,     labelMinScale: 0.0012, dotMinRadius: 5.6, dotFactor: 2.0,  labelFont: '700 13px' },
      { name: 'main station',              dotMinScale: 0,     labelMinScale: 0.0016, dotMinRadius: 5.2, dotFactor: 1.85, labelFont: '700 12.5px' },
    ],
    // Light-rail, tram and cable-car stops of rank 0 and 1 are not drawn at all (no dot, no name, no tooltip) below this scale.
    tramStopMinScale: 0.07,
    // Base dot radius grows from minRadius at growFromScale to maxRadius: r = clamp(minRadius + (scale - growFromScale) x growPerScale)
    dot: { minRadius: 2.2, maxRadius: 4.6, growFromScale: 0.006, growPerScale: 60 },
    outlineMinScale: 0.04,                  // station outlines (footprints) are drawn from this scale
    hitRadiusPx: 14,                        // how close the pointer must be to a dot to show its tooltip
  },
};
