# Cast Hub

3D Slicer CAST hub implementation using FastAPI + WebSockets in python.

This service handles:

- Mock auth and token issuance (`/oauth/token`)
- subscription management (`/api/hub`)
- publish fan-out to subscribers
- websocket `get-request` / `get-response` flow (`/api/hub/cast-get`)
- conference helper endpoint (`/api/hub/conference-client`)

## Requirements

- Python 3.10+
- pip

Install dependencies:

```bash
pip install fastapi uvicorn python-multipart
```

## Run
python app.py
Default local endpoint examples:

Hub: http://127.0.0.1:2017/api/hub
Token: http://127.0.0.1:2017/oauth/token
Get: http://127.0.0.1:2017/api/hub/cast-get

## Main Endpoints

POST /oauth/token
Returns access token and optional session metadata.

Typical response includes:

access_token
subscriber_name
topic
POST /api/hub
Handles:

subscribe (hub.mode=subscribe)
unsubscribe (hub.mode=unsubscribe)
publish (JSON payload with event)
For websocket subscribe, response returns hub.channel.endpoint.

GET /api/hub/cast-get
Triggers a websocket get-request to a subscriber and waits for get-response.

Common query params:

subscriber (required)
topic (optional)
dataType (optional, e.g. FHIRcastContext)
actor (optional)
GET /api/hub/conference-client
Conference utility endpoint.

## Subscription Payload Notes
Form subscribe payload should include:

hub.mode=subscribe
hub.channel.type=websocket
hub.callback=<callback-url>
hub.topic=<topic>
hub.events=<csv|*>
subscriber.name=<name>
subscriber.actors=<json-array-string>
Example: subscriber.actors=["WORKLIST_CLIENT","WATCHER"]

FHIRcastContext Behavior
When subscriber receives websocket get-request with dataType=FHIRcastContext, client can respond with:

last sent context payload, or
empty fallback:
{
  "context.type": "",
  "context": []
}
If last event type contains close, fallback empty context should be returned.

## Development Tips
Keep python app.py running while testing from cast client demo.
Watch server logs for subscribe/publish/get-request flow.
Validate websocket endpoint generation when deploying behind HTTPS/proxy.
Security / Production Notes
Replace demo token/auth logic with real auth provider.
Restrict CORS origins.
Add TLS termination and trusted proxy handling.
Add structured logging + metrics.
Add persistence if subscription state must survive restarts.
Repo Structure (key file)
app.py — main FastAPI app + Cast Hub runtime
