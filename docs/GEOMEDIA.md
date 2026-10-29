# GeoMedia in World view

## One prompt for World view

Use the World prompt for requests such as “aircraft over Paris”, “earthquakes near
Japan”, or “public cameras in Ljubljana”. The location router selects the data layer
and locates the place. There are no category/region/country controls in the HUD.

A public-camera request starts a short-lived headless Edge lookup of OpenCCTV's
public List/Search interface. It blocks the global marker dataset and unrelated feed
strip. Up to 50 matching camera records are validated, shown as clustered native globe
markers, and an image feed is selected automatically. Clicking a marker opens its
feed in Nathan's floating inspector. No separate window opens. If an automatic
selection fails, Nathan tries up to five candidates; manual selections stay selected.

The lookup closes its browser when finished and caches at most four searches for
five minutes. Playwright controls the installed Edge browser; no browser download,
credentials, copied API tokens, or persistent global database is used. Feed URLs
come from the public search results and play directly from the provider. Image feeds
refresh at the source's reported interval; they are not necessarily continuous video.
Coordinates are supplied by the source and should be treated as approximate.

OpenCCTV's interface can change. Lookup errors are shown in Nathan rather than
opening an external browser or substituting demo footage. Provider CORS, availability,
and iframe restrictions can still stop individual feeds. Existing external-viewer
endpoints are retained for compatibility but native search no longer calls them.

The separate Media toggle shows the configured catalogue or labeled demo fixtures.

The eight bundled fixtures deliberately use illustrative coordinates. Images are local
synthetic artwork; HLS and MP4 are public test media, not cameras at those coordinates.

## Catalogue

Set `NEXT_PUBLIC_GEOMEDIA_ENDPOINT=https://your-host/catalogue.json` in the server
environment (or `.env`) and restart Nathan. This is a FastAPI application, so the
variable is read by Python rather than a Next.js build. No frontend build is needed.
The server caches remote JSON for five minutes and falls back to the labeled local
fixture if the endpoint fails or violates the contract.

Accepted payloads: an array of nodes, `{ "items": [...] }`, `{ "nodes": [...] }`,
or a GeoJSON FeatureCollection of Points whose properties hold the node fields and
whose geometry supplies `[longitude, latitude]`. IDs must be unique. The strict
TypeScript interface is `nathan/web/static/js/geomedia.d.ts`; the server validates
the same contract in `nathan/geomedia.py`. Refresh intervals must be at least 1,000 ms.
URLs must use HTTP(S) or the application's `/static/` path. Unknown fields, malformed
nodes, non-finite/out-of-range coordinates and duplicate IDs reject the catalogue.

The catalogue is fetched and indexed once in a module worker. Camera changes trigger
debounced queries plus periodic updates during rotation. Queries use a horizon bounding
box (including antimeridian wrapping), then camera-frustum and hemisphere clipping.
Supercluster reduces visible points by zoom and bounds the HUD to 600 elements using
coarser clusters when necessary. Filtering rebuilds the index in the worker. The main
thread receives only visible cluster/marker records, not the full catalogue.

## Media and agent tools

- `filterMediaNodes({region, category, country, visible})` toggles/filters the layer and
  returns up to 30 matching node IDs with the total count. Omitted filters reset to all.
- `flyToMediaNode({id})` enables media, clears filters, flies to a node and opens it.
- `world_view({place, layers: "media"})` flies to a place with media enabled.

These use the existing shared focus state and its browser polling. Open World view
to see the commands. Media requests go directly from the browser to the source;
HLS requires source CORS permission. The inspector uses native HLS where available,
otherwise the bundled hls.js, with standard video for MP4. Image polling preloads
frames before swapping them; failed streams keep a Refresh and Open source action.
Iframes are sandboxed without same-origin, navigation or popup privileges. Some
providers require privileges this sandbox intentionally does not grant. Browsers do
not reliably expose iframe embedding failures; Open source is always available.

Bundled third-party code: Supercluster 8.0.1 (ISC) and hls.js 1.6.13 (Apache-2.0),
with license files in `nathan/web/static/vendor/`.

## Checks

`uv run pytest tests/test_geomedia.py tests/test_tier4.py tests/test_web.py`

`node --test tests/js/geomedia.test.mjs`

The worker test indexes 50,000 deterministic points and checks bounded queries,
filtering, cluster expansion, ID lookup, coordinate wrapping and frustum rejection.
It does not establish a hardware-independent 60 FPS rendering guarantee.
