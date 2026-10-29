export interface GeoMediaNode {
  id: string;
  title: string;
  category: string;
  coordinates: [number, number]; // [lng, lat]
  region?: string;
  country?: string;
  streamUrl: string;
  streamType: 'hls' | 'image-poll' | 'mp4' | 'iframe';
  refreshIntervalMs?: number;
  attributionUrl?: string;
}
