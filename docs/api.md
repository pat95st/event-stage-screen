# Remote control API (v1)

The REST API lets other systems control the outputs – for example
Bitfocus Companion ("Generic HTTP" module), a Stream Deck with a web-request
plugin, a show controller or a script.

## Authentication

Create an API key in the web interface under **System › Remote control (API
keys)**. The key (`ess_…`) is shown only once. Send it with every request,
preferably as a header:

```
Authorization: Bearer ess_…
```

`X-API-Key: ess_…` and the query parameter `?key=ess_…` work as well. The
query parameter is convenient for devices that can only call a URL, but the
key then appears in logs of proxies – use it only in your own network.

All actions accept `POST` (JSON body) and `GET` (query parameters). `GET` is
only accepted with an API key, never with a browser session, so that a link
on a web page cannot trigger actions. After 20 rejected keys within 5 minutes
an address is blocked for 5 minutes.

Responses are JSON: `{"ok": true, "result": …}` or `{"error": "…"}` with an
HTTP status of 4xx/5xx.

Base URL: `http://<device>:8080`. Outputs are numbered `1` and `2`.

## Endpoints

| Method | Path | Parameters | Effect |
|---|---|---|---|
| GET | `/api/v1/status` | – | Outputs with live scene, scene list, automation, blackout, test pattern, ERM status |
| GET/POST | `/api/v1/outputs/{n}/scene` | `scene` = scene id or name | Show a scene (manual override of automatic switching) |
| GET/POST | `/api/v1/outputs/{n}/scene/next` | – | Next scene of the output (in list order, wraps around) |
| GET/POST | `/api/v1/outputs/{n}/scene/previous` | – | Previous scene |
| GET/POST | `/api/v1/outputs/{n}/automatic` | – | Return to automatic scene switching |
| GET/POST | `/api/v1/outputs/{n}/blackout` | `on` = `true`/`false`, omitted = toggle | Black out an output |
| GET/POST | `/api/v1/blackout` | `on` as above | Black out both outputs |
| GET/POST | `/api/v1/outputs/{n}/testpattern` | `pattern` = `bars`, `grid`, `edges`, empty = off | Show a test pattern |
| GET/POST | `/api/v1/outputs/{n}/identify` | `seconds` (default 6) | Show the output number |
| GET/POST | `/api/v1/identify` | `seconds` | Identify both outputs |
| GET/POST | `/api/v1/outputs/{n}/elements/{element}/visible` | `visible` = `true`/`false`, omitted = toggle | Show/hide an element of the live scene (`element` = element id or name) |

Scene and element names are matched case-insensitively.

## Examples

```bash
curl -X POST -H "Authorization: Bearer ess_…" -H "Content-Type: application/json" \
  -d '{"scene": "Break"}' http://stage-left.local:8080/api/v1/outputs/1/scene
```

```bash
curl -H "Authorization: Bearer ess_…" http://stage-left.local:8080/api/v1/outputs/1/blackout?on=true
```

```bash
curl "http://stage-left.local:8080/api/v1/outputs/2/elements/Logo/visible?key=ess_…"
```

### Bitfocus Companion

Use the "Generic HTTP Requests" module:

- Base URL: `http://<device>:8080/api/v1`
- Header: `Authorization: Bearer ess_…`
- Button action "POST": URL `outputs/1/scene/next`, body empty;
  or `outputs/1/scene` with body `{"scene": "Walk-in"}`.
- Feedback via `GET status` (e.g. `outputs[0].scene`).
