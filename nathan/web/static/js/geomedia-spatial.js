// A conservative horizon box, followed by exact camera-frustum clipping in the worker.
export function viewportBox({ lat, lng, altitude }) {
  const rad = Math.PI / 180;
  const angle = Math.acos(1 / (1 + Math.max(0.000001, altitude))) / rad;
  const south = Math.max(-90, lat - angle), north = Math.min(90, lat + angle);
  if (south === -90 || north === 90) return [-180, south, 180, north];
  const span = Math.asin(Math.min(1, Math.sin(angle * rad) / Math.cos(lat * rad))) / rad;
  const wrap = (x) => ((x + 180) % 360 + 360) % 360 - 180;
  return [wrap(lng - span), south, wrap(lng + span), north];
}

export function inBox([lng, lat], [west, south, east, north]) {
  return lat >= south && lat <= north && (west <= east ? lng >= west && lng <= east : lng >= west || lng <= east);
}

export function inFrustum([lng, lat], { matrix: m, camera: c, radius: r }) {
  const rad = Math.PI / 180, phi = lat * rad, theta = lng * rad;
  const x = r * Math.cos(phi) * Math.sin(theta), y = r * Math.sin(phi), z = r * Math.cos(phi) * Math.cos(theta);
  if (x * c[0] + y * c[1] + z * c[2] < r * r) return false;
  const w = m[3] * x + m[7] * y + m[11] * z + m[15];
  return w > 0 && [0, 1, 2].every((i) => Math.abs(m[i] * x + m[i + 4] * y + m[i + 8] * z + m[i + 12]) <= w);
}
