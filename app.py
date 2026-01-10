#!/usr/bin/env python
"""
Standalone Cast Hub implementation.
This is a complete, standalone implementation that doesn't depend on WebServer.py.
Uses FastAPI for HTTP server and WebSocket support.

Run with: python cast_hub.py

Requirements:
    pip install fastapi uvicorn
    
    Optional (for form data support):
    pip install python-multipart
    
    Note: Form data will work without python-multipart by parsing raw body,
    but python-multipart provides better form data parsing support.
"""

import sys
import os
import json
import uuid
import urllib.parse
import hmac
import hashlib
from typing import Dict, List, Optional
from datetime import datetime

# Try to import FastAPI - install with: pip install fastapi uvicorn
try:
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Response, HTTPException
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import JSONResponse
    from fastapi.staticfiles import StaticFiles
    import uvicorn
    HAS_FASTAPI = True
except ImportError:
    print("ERROR: FastAPI not installed. Install with: pip install fastapi uvicorn")
    sys.exit(1)


app = FastAPI(title="Cast Hub")

# Enable CORS with explicit configuration for Azure
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
    max_age=3600,
)

# Mount static files directory
base_dir = os.path.dirname(os.path.abspath(__file__))
static_dir = os.path.join(base_dir, "Resources", "docroot")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")


class CastHub:
    """Cast Hub implementation for managing subscriptions and broadcasting events"""
    
    def __init__(self):
        self.subscriptions: List[Dict] = []
        self.websocket_connections: Dict[str, WebSocket] = {}  # endpoint -> WebSocket
        self.conferences: List[Dict] = []
        self.last_context: Dict[str, Dict] = {}  # topic -> context
        self.audit_log: List[Dict] = []  # List of logged events
        self.audit_log_counter: int = 0  # Incrementing message number
        self.server_port = 2017
        self.user_count = 0
        self.page_loads = 0
        self.app_logs: List[str] = []  # Store recent logs
        self.max_logs = 100  # Keep last 100 log entries
    
    def log(self, message: str):
        """Add a log message to the in-memory log buffer"""
        timestamp = datetime.now().isoformat()
        log_entry = f"[{timestamp}] {message}"
        print(log_entry)  # Still print to stdout
        self.app_logs.append(log_entry)
        # Keep only last max_logs entries
        if len(self.app_logs) > self.max_logs:
            self.app_logs = self.app_logs[-self.max_logs:]
    
    def get_logs(self, count: int = 50) -> List[str]:
        """Get recent log entries"""
        return self.app_logs[-count:]
    
    def set_server_port(self, port: int):
        """Set the server port for generating WebSocket URLs"""
        self.server_port = port
    
    def check_subscription_request(self, subscription_request: Dict) -> Dict:
        """Verify WebSub subscription callback"""
        callback = subscription_request.get("hub.callback") or subscription_request.get("hub_callback")
        secret = subscription_request.get("hub.secret") or subscription_request.get("hub_secret")
        topic = subscription_request.get("hub.topic") or subscription_request.get("hub_topic")
        
        if not callback or not secret or not topic:
            return {"status": 400, "data": "Missing required parameters"}
        
        try:
            # Send GET request to callback with challenge
            import urllib.request
            challenge_url = f"{callback}?hub.challenge={secret}&hub.topic={topic}"
            req = urllib.request.Request(challenge_url)
            with urllib.request.urlopen(req, timeout=5) as response:
                status = response.getcode()
                data = response.read().decode()
                if data == secret and status == 200:
                    return {"status": 200, "data": data}
                else:
                    return {"status": 500, "data": "Verification failed"}
        except Exception as e:
            print(f"[LOG] WebSub verification error: {e}")
            return {"status": 500, "data": str(e)}
    
    def add_subscription(self, subscription_data: Dict) -> Dict:
        """Handle subscription/unsubscription requests - matches CastHubRequestHandler.handleHubSubscription"""
        hub_mode = subscription_data.get("hub.mode", subscription_data.get("hub_mode", ""))
        hub_topic = subscription_data.get("hub.topic", subscription_data.get("hub_topic", ""))
        hub_events = subscription_data.get("hub.events", subscription_data.get("hub_events", ""))
        hub_callback = subscription_data.get("hub.callback", subscription_data.get("hub_callback", ""))
        hub_secret = subscription_data.get("hub.secret", subscription_data.get("hub_secret", ""))
        hub_lease = subscription_data.get("hub.lease_seconds", subscription_data.get("hub.lease", subscription_data.get("hub_lease", "7200")))
        subscriber_name = subscription_data.get("subscriber.name", subscription_data.get("subscriber_name", "unknown"))
        channel_type = subscription_data.get("hub.channel.type", subscription_data.get("hub_channel_type", "websub"))
        channel_endpoint = subscription_data.get("hub.channel.endpoint", subscription_data.get("hub_channel_endpoint", ""))
        host = subscription_data.get("host", subscription_data.get("Host", ""))
        
        if hub_mode == "subscribe":
            # Verify subscription request for WebSub
            if channel_type != "websocket":
                verify_result = self.check_subscription_request({
                    "hub.callback": hub_callback,
                    "hub.secret": hub_secret,
                    "hub.topic": hub_topic
                })
                
                if verify_result["status"] != 200:
                    print(f"[LOG] WebSub verification failed: {verify_result['status']}")
                    raise ValueError("WebSub verification failed")
            
            # Generate WebSocket endpoint identifier
            websocket_endpoint = str(uuid.uuid4())
            
            # Determine protocol and host from request
            # Use wss for HTTPS (Azure), ws for HTTP (local)
            # Extract host from request headers or use provided host
            request_host = host if host else f"localhost:{self.server_port}"
            
            # Determine if HTTPS based on common patterns
            is_secure = (
                "azurewebsites.net" in request_host or 
                "https" in request_host or
                request_host.startswith("secure.") or
                not "localhost" in request_host.lower()
            )
            protocol = "wss" if is_secure else "ws"
            
            # Remove port from host if it contains azurewebsites.net (Azure handles this)
            if "azurewebsites.net" in request_host or ":" not in request_host:
                websocket_url = f"{protocol}://{request_host}/bind/{websocket_endpoint}"
            else:
                websocket_url = f"{protocol}://{request_host}/bind/{websocket_endpoint}"
            
            subscription = {
                "channel": channel_type,
                "endpoint": websocket_url if channel_type == "websocket" else hub_callback,
                "websocket_endpoint": websocket_endpoint,
                "callback": hub_callback,
                "events": hub_events,
                "secret": hub_secret,
                "topic": hub_topic,
                "lease": int(hub_lease),
                "session": hub_topic,
                "subscriber": subscriber_name,
                "host": host,
                "created": datetime.now().isoformat()
            }
            
            self.subscriptions.append(subscription)
            self.log(f"Subscription added: {subscriber_name} for topic {hub_topic} via {channel_type}")
            
            return {
                "subscription": subscription,
                "websocket_url": websocket_url if channel_type == "websocket" else None
            }
        
        elif hub_mode == "unsubscribe":
            # Handle unsubscribe
            removed_count = self.remove_subscription(
                endpoint=channel_endpoint.split("/bind/")[-1] if channel_endpoint and "/bind/" in channel_endpoint else None,
                callback=hub_callback,
                topic=hub_topic
            )
            return {"removed": removed_count}
        
        else:
            raise ValueError(f"Invalid hub.mode: {hub_mode}")
    
    def remove_subscription(self, endpoint: str = None, callback: str = None, topic: str = None) -> int:
        """Remove subscriptions matching the given criteria"""
        removed_count = 0
        for sub in self.subscriptions[:]:
            if endpoint and sub.get("websocket_endpoint") == endpoint:
                self.subscriptions.remove(sub)
                removed_count += 1
                self.log(f"Removed subscription for endpoint: {endpoint}")
            elif callback and sub.get("callback") == callback and topic and sub.get("topic") == topic:
                self.subscriptions.remove(sub)
                removed_count += 1
                self.log(f"Removed subscription for callback: {callback}, topic: {topic}")
        
        if removed_count > 0:
            self.log(f"Removed {removed_count} subscription(s), remaining: {len(self.subscriptions)}")
        return removed_count
    
    def get_subscriptions(self) -> List[Dict]:
        """Get all active subscriptions"""
        return self.subscriptions
    
    def send_event(self, topic: str, event_type: str, event_data: Dict):
        """Helper method to create a notification (actual sending happens in async endpoint)"""
        notification = {
            "timestamp": datetime.now().isoformat(),
            "id": str(uuid.uuid4()),
            "event": {
                "hub.topic": topic,
                "hub.event": event_type,
                "context": event_data
            }
        }
        return notification
    
    def register_websocket(self, endpoint: str, websocket: WebSocket):
        """Register a WebSocket connection"""
        self.websocket_connections[endpoint] = websocket
        self.log(f"WebSocket registered for endpoint: {endpoint} (total: {len(self.websocket_connections)})")
    
    def unregister_websocket(self, endpoint: str):
        """Unregister a WebSocket connection"""
        if endpoint in self.websocket_connections:
            del self.websocket_connections[endpoint]
            self.remove_subscription(endpoint=endpoint)
            print(f"[LOG] WebSocket unregistered for endpoint: {endpoint} (remaining: {len(self.websocket_connections)})")
    
    def add_audit_log(self, user: str, topic: str, event_name: str, event_data: Dict, direction: str = "received"):
        """Add an entry to the audit log
        
        Args:
            user: User/subscriber name
            topic: Topic name
            event_name: Event type name
            event_data: Event data/context
            direction: "received" or "sent"
        """
        self.audit_log_counter += 1
        log_entry = {
            "message_number": self.audit_log_counter,
            "timestamp": datetime.now().isoformat(),
            "user": user,
            "topic": topic,
            "event_name": event_name,
            "event_data": event_data,
            "direction": direction
        }
        self.audit_log.append(log_entry)
        # Keep only last 1000 entries to prevent memory issues
        if len(self.audit_log) > 1000:
            self.audit_log = self.audit_log[-1000:]
    
    def get_audit_log(self, user_filter: Optional[str] = None, topic_filter: Optional[str] = None) -> List[Dict]:
        """Get audit log entries, optionally filtered by user or topic"""
        filtered_log = self.audit_log
        if user_filter:
            filtered_log = [entry for entry in filtered_log if user_filter.lower() in entry.get("user", "").lower()]
        if topic_filter:
            filtered_log = [entry for entry in filtered_log if topic_filter.lower() in entry.get("topic", "").lower()]
        # Return in reverse order (latest first)
        return list(reversed(filtered_log))
    
    def get_audit_log_unique_values(self) -> Dict[str, List[str]]:
        """Get unique users and topics from audit log"""
        users = set()
        topics = set()
        for entry in self.audit_log:
            user = entry.get("user")
            topic = entry.get("topic")
            if user and str(user).strip():  # Check for non-empty string
                users.add(str(user).strip())
            if topic and str(topic).strip():  # Check for non-empty string
                topics.add(str(topic).strip())
        return {
            "users": sorted(list(users)),
            "topics": sorted(list(topics))
        }
    
    def clear_audit_log(self):
        """Clear all entries from the audit log"""
        self.audit_log.clear()
        print("[LOG] Audit log cleared")


# Global Cast Hub instance
cast_hub = CastHub()


@app.get("/topics")
async def get_topics():
    """Get list of available topics"""
    topics = list(set(sub.get("topic") for sub in cast_hub.get_subscriptions() if sub.get("topic")))
    return topics


@app.get("/debug/websockets")
async def debug_websockets():
    """Debug endpoint to check WebSocket connections"""
    return {
        "active_connections": len(cast_hub.websocket_connections),
        "endpoints": list(cast_hub.websocket_connections.keys()),
        "subscriptions": len(cast_hub.subscriptions),
        "websocket_subscriptions": len([s for s in cast_hub.subscriptions if s.get("channel") == "websocket"])
    }


@app.get("/debug/logs")
async def debug_logs(count: int = 50):
    """Get recent application logs"""
    return {
        "count": len(cast_hub.get_logs(count)),
        "logs": cast_hub.get_logs(count)
    }


@app.get("/conference")
async def get_conference():
    """Get all conferences"""
    return cast_hub.conferences


@app.post("/conference")
async def post_conference(request: Request):
    """Create a conference"""
    content_type = request.headers.get("content-type", "")
    try:
        if "application/json" in content_type:
            data = await request.json()
        else:
            # Try form data
            try:
                form_data = await request.form()
                data = dict(form_data)
            except AssertionError as e:
                if "python-multipart" in str(e):
                    body = await request.body()
                    if body:
                        from urllib.parse import parse_qs
                        parsed = parse_qs(body.decode())
                        data = {k: v[0] if len(v) == 1 else v for k, v in parsed.items()}
                    else:
                        raise HTTPException(status_code=400, detail="No data provided")
                else:
                    raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not parse request: {e}")
    
    conference = {
        "user": data.get("user"),
        "title": data.get("title"),
        "topics": data.get("topics", [])
    }
    cast_hub.conferences.append(conference)
    print(f"[LOG] Conference created: {conference.get('title')}")
    return {"status": "created"}


@app.delete("/conference")
async def delete_conference(request: Request):
    """Delete a conference"""
    content_type = request.headers.get("content-type", "")
    try:
        if "application/json" in content_type:
            data = await request.json()
        else:
            # Try form data
            try:
                form_data = await request.form()
                data = dict(form_data)
            except AssertionError as e:
                if "python-multipart" in str(e):
                    body = await request.body()
                    if body:
                        from urllib.parse import parse_qs
                        parsed = parse_qs(body.decode())
                        data = {k: v[0] if len(v) == 1 else v for k, v in parsed.items()}
                    else:
                        raise HTTPException(status_code=400, detail="No data provided")
                else:
                    raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not parse request: {e}")
    
    user = data.get("user")
    removed = []
    for conf in cast_hub.conferences[:]:
        if conf.get("user") == user:
            cast_hub.conferences.remove(conf)
            removed.append(conf)
        elif user in conf.get("topics", []):
            print(f"[LOG] User {user} exited conference {conf.get('title')}")
    
    return {"removed": len(removed)}


@app.post("/status")
async def post_status():
    """Status endpoint - returns hub status"""
    import socket
    hostname = socket.gethostname()
    status_msg = f"Hub Status\n"
    status_msg += f"Hostname: {hostname}\n"
    status_msg += f"Port: {cast_hub.server_port}\n"
    
    # Get network interfaces (simplified)
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        status_msg += f"IP: {ip}\n"
    except:
        pass
    
    status_msg += f"Subscriptions: {len(cast_hub.get_subscriptions())}\n"
    for sub in cast_hub.get_subscriptions():
        status_msg += f"  - {sub.get('subscriber', 'unknown')}: {sub.get('topic', 'unknown')} ({sub.get('host', 'unknown')})\n"
    
    print(status_msg)
    return Response(content=status_msg, media_type="text/plain")


@app.get("/api/hub/admin")
@app.get("/api/hub/admin/")
async def get_hub_status(request: Request):
    """Get hub status page showing all users and endpoints"""
    # Use the same path resolution as the static mount
    base_dir = os.path.dirname(os.path.abspath(__file__))
    html_path = os.path.join(base_dir, "Resources", "docroot", "hub-status.html")
    
    if os.path.exists(html_path):
        # Read and return the file content directly
        with open(html_path, 'r', encoding='utf-8') as f:
            html_content = f.read()
        return Response(content=html_content, media_type="text/html")
    
    # If file not found, try redirecting to static mount
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/static/hub-status.html", status_code=302)


@app.get("/api/hub/status")
async def get_hub_status_json():
    """Get hub status as JSON"""
    subscriptions = cast_hub.get_subscriptions()
    
    return {
        "total_subscriptions": len(subscriptions),
        "total_websockets": len(cast_hub.websocket_connections),
        "total_topics": len(set(sub.get("topic") for sub in subscriptions if sub.get("topic"))),
        "total_messages": len(cast_hub.audit_log),
        "subscriptions": subscriptions,
        "websocket_endpoints": list(cast_hub.websocket_connections.keys()),
        "topics": list(set(sub.get("topic") for sub in subscriptions if sub.get("topic")))
    }


@app.get("/api/audit-log")
async def get_audit_log(user: Optional[str] = None, topic: Optional[str] = None):
    """Get audit log entries with optional filtering by user or topic"""
    log_entries = cast_hub.get_audit_log(user_filter=user, topic_filter=topic)
    unique_values = cast_hub.get_audit_log_unique_values()
    return {
        "entries": log_entries,
        "count": len(log_entries),
        "unique_users": unique_values["users"],
        "unique_topics": unique_values["topics"]
    }


@app.delete("/api/audit-log")
async def clear_audit_log():
    """Clear all audit log entries"""
    cast_hub.clear_audit_log()
    return {"status": "cleared", "message": "Audit log cleared successfully"}


@app.get("/images/3DSlicer-DesktopIcon.png")
async def get_slicer_icon():
    """Serve the 3D Slicer desktop icon"""
    import os
    # First try root directory
    icon_path = os.path.join(os.path.dirname(__file__), "3DSlicer-DesktopIcon.png")
    if os.path.exists(icon_path):
        from fastapi.responses import FileResponse
        return FileResponse(icon_path, media_type="image/png")
    # Fallback to Resources directory
    icon_path = os.path.join(os.path.dirname(__file__), "Resources", "docroot", "images", "3DSlicer-DesktopIcon.png")
    if os.path.exists(icon_path):
        from fastapi.responses import FileResponse
        return FileResponse(icon_path, media_type="image/png")
    raise HTTPException(status_code=404, detail="Icon not found")


@app.get("/favicon.ico")
async def get_favicon():
    """Serve the favicon"""
    import os
    favicon_path = os.path.join(os.path.dirname(__file__), "Resources", "docroot", "favicon.ico")
    if os.path.exists(favicon_path):
        from fastapi.responses import FileResponse
        return FileResponse(favicon_path, media_type="image/x-icon")
    else:
        # Fallback: try relative path from current working directory
        fallback_path = os.path.join("Modules", "Scripted", "Cast", "Resources", "docroot", "favicon.ico")
        if os.path.exists(fallback_path):
            from fastapi.responses import FileResponse
            return FileResponse(fallback_path, media_type="image/x-icon")
        raise HTTPException(status_code=404, detail="Favicon not found")


@app.get("/api/hub/{topic}")
async def get_hub_topic(topic: str, request: Request):
    """Get hub topic information or authenticate"""
    if topic == "authenticate":
        # Handle authentication
        username = request.query_params.get("username", "")
        secret = request.query_params.get("secret", "")
        # Simple authentication (in production, use proper database)
        if username and secret:
            cast_hub.user_count += 1
            topic_id = f"user-{cast_hub.user_count}"
            return {"topic": topic_id}
        else:
            raise HTTPException(status_code=400, detail="Missing credentials")
    else:
        # Get context for topic
        context = cast_hub.last_context.get(topic, {})
        return context


@app.post("/api/hub/")
@app.post("/api/hub")
async def post_hub(request: Request):
    """Handle subscription requests"""
    # Parse form data or JSON
    content_type = request.headers.get("content-type", "")
    subscription_data = {}
    
    if "application/json" in content_type:
        subscription_data = await request.json()
    else:
        # Try form data - may require python-multipart
        try:
            form_data = await request.form()
            subscription_data = dict(form_data)
        except AssertionError as e:
            if "python-multipart" in str(e):
                # Fall back to parsing raw body as form data
                body = await request.body()
                if body:
                    try:
                        from urllib.parse import parse_qs
                        parsed = parse_qs(body.decode())
                        subscription_data = {k: v[0] if len(v) == 1 else v for k, v in parsed.items()}
                    except Exception:
                        raise HTTPException(status_code=400, detail="Could not parse form data. Install python-multipart for better form data support.")
            else:
                raise
    
    # Also get query parameters
    query_params = dict(request.query_params)
    subscription_data.update(query_params)
    
    # Add host header for WebSocket URL generation
    subscription_data["host"] = request.headers.get("host", request.headers.get("Host", ""))
    
    try:
        hub_mode = subscription_data.get("hub.mode", subscription_data.get("hub_mode", "subscribe"))
        
        if hub_mode == "unsubscribe":
            # Handle unsubscribe
            result = cast_hub.add_subscription(subscription_data)
            return {"status": "unsubscribed", "removed": result.get("removed", 0)}
        else:
            # Handle subscribe
            result = cast_hub.add_subscription(subscription_data)
            
            # Return appropriate response - 202 Accepted for subscription requests
            if result.get("websocket_url"):
                return JSONResponse(
                    content={"hub.channel.endpoint": result["websocket_url"]},
                    status_code=202
                )
            else:
                return JSONResponse(
                    content={"status": "subscribed", "subscription": result["subscription"]},
                    status_code=202
                )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/hub/{topic}")
async def post_hub_topic(topic: str, request: Request):
    """Handle POST /api/hub/{topic} - receive events and broadcast to subscribers"""
    try:
        notification = await request.json()
        print(f"[LOG] Received event for topic {topic}: {notification.get('event', {}).get('hub.event', 'unknown')}")
        
        # Broadcast to subscribers (sync method, but WebSocket sending needs async)
        event = notification.get("event", {})
        topic_name = event.get("hub.topic", "")
        event_type = event.get("hub.event", "")
        context = event.get("context", {})
        
        # Update lastContext
        if "close" in event_type.lower():
            cast_hub.last_context[topic_name] = {}
        else:
            if context:
                cast_hub.last_context[topic_name] = context
        
        # Add to audit log - mark as received
        # Extract user from subscriptions that match this topic
        user = "unknown"
        for sub in cast_hub.subscriptions:
            if sub.get("topic") == topic_name:
                user = sub.get("subscriber", "unknown")
                break
        # If no subscription found, try to extract from topic name
        if user == "unknown":
            user = topic_name if topic_name.startswith("user-") else topic_name
        cast_hub.add_audit_log(user=user, topic=topic_name, event_name=event_type, event_data=context, direction="received")
        
        # Calculate HMAC signature
        notification_json = json.dumps(notification)
        
        # Send to matching subscriptions
        for sub in cast_hub.subscriptions[:]:  # Copy to allow removal
            # Check if subscription matches
            subscribed_events = sub.get("events", "").lower()
            if topic_name != sub.get("topic", ""):
                continue
            if event_type.lower() not in subscribed_events and "*" not in subscribed_events:
                continue
            
            secret = sub.get("secret", "")
            channel = sub.get("channel", "websub")
            
            # Calculate HMAC
            hmac_sig = ""
            if secret:
                hmac_sig = hmac.new(secret.encode(), notification_json.encode(), hashlib.sha256).hexdigest()
            
            if channel == "websocket":
                # WebSocket delivery - async
                endpoint = sub.get("websocket_endpoint")
                if endpoint and endpoint in cast_hub.websocket_connections:
                    try:
                        websocket = cast_hub.websocket_connections[endpoint]
                        # FastAPI WebSocket.send_text() is async - await it
                        await websocket.send_text(notification_json)
                        print(f"[LOG] Sent WebSocket message to {sub.get('subscriber')} via endpoint {endpoint}")
                        # Log sent message
                        cast_hub.add_audit_log(
                            user=sub.get("subscriber", "unknown"),
                            topic=topic_name,
                            event_name=event_type,
                            event_data=context,
                            direction="sent"
                        )
                    except Exception as e:
                        print(f"[LOG] WebSocket send error for {endpoint}: {type(e).__name__}: {e}")
                        print(f"[LOG] Removing failed WebSocket connection and subscription")
                        if endpoint in cast_hub.websocket_connections:
                            del cast_hub.websocket_connections[endpoint]
                        if sub in cast_hub.subscriptions:
                            cast_hub.subscriptions.remove(sub)
                else:
                    if not endpoint:
                        print(f"[LOG] WebSocket endpoint not set for subscription: {sub.get('subscriber')}")
                    else:
                        print(f"[LOG] WebSocket not bound for subscription: {sub.get('subscriber')}")
            else:
                # WebSub delivery - HTTP POST to callback (can be async but using sync for now)
                callback = sub.get("callback")
                if callback:
                    try:
                        import urllib.request
                        req = urllib.request.Request(callback)
                        req.add_header("Content-Type", "application/json")
                        req.add_header("X-Hub-Signature", f"sha256={hmac_sig}")
                        req.data = notification_json.encode()
                        req.get_method = lambda: "POST"
                        
                        # In production, this should be async (use aiohttp or httpx)
                        import asyncio
                        loop = asyncio.get_event_loop()
                        await loop.run_in_executor(None, lambda: urllib.request.urlopen(req, timeout=5))
                        print(f"[LOG] Sent WebSub notification to {callback}")
                        # Log sent message
                        cast_hub.add_audit_log(
                            user=sub.get("subscriber", "unknown"),
                            topic=topic_name,
                            event_name=event_type,
                            event_data=context,
                            direction="sent"
                        )
                    except Exception as e:
                        print(f"[LOG] WebSub delivery error to {callback}: {e}")
        
        # Handle conferences - broadcast to attendees
        for conference in cast_hub.conferences:
            if conference.get("user") == topic_name:
                for attendee_topic in conference.get("topics", []):
                    # Find subscriptions for attendee
                    for sub in cast_hub.subscriptions:
                        if sub.get("topic") == attendee_topic and sub.get("channel") == "websocket":
                            endpoint = sub.get("websocket_endpoint")
                            if endpoint and endpoint in cast_hub.websocket_connections:
                                try:
                                    websocket = cast_hub.websocket_connections[endpoint]
                                    await websocket.send_text(notification_json)
                                    print(f"[LOG] Sent conference message to attendee: {attendee_topic}")
                                    # Log sent conference message
                                    cast_hub.add_audit_log(
                                        user=sub.get("subscriber", "unknown"),
                                        topic=attendee_topic,
                                        event_name=event_type,
                                        event_data=context,
                                        direction="sent"
                                    )
                                except Exception as e:
                                    print(f"[LOG] Conference WebSocket error: {e}")
        
        return {"status": "received"}
    except Exception as e:
        print(f"[LOG] Error handling event: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/api/hub/")
@app.delete("/api/hub")
async def delete_hub(request: Request):
    """Handle DELETE /api/hub/ - clear all subscriptions"""
    # Also support unsubscribe via DELETE with body
    content_type = request.headers.get("content-type", "")
    unsubscribe_data = {}
    
    if "application/json" in content_type:
        try:
            unsubscribe_data = await request.json()
        except:
            pass
    else:
        # Try form data
        try:
            form_data = await request.form()
            unsubscribe_data = dict(form_data)
        except AssertionError as e:
            if "python-multipart" in str(e):
                # Fall back to parsing raw body
                body = await request.body()
                if body:
                    try:
                        from urllib.parse import parse_qs
                        parsed = parse_qs(body.decode())
                        unsubscribe_data = {k: v[0] if len(v) == 1 else v for k, v in parsed.items()}
                    except Exception:
                        pass
            else:
                pass
    
    # If unsubscribe data provided, remove specific subscriptions
    if unsubscribe_data:
        endpoint = unsubscribe_data.get("hub.channel.endpoint") or unsubscribe_data.get("hub_channel_endpoint")
        callback = unsubscribe_data.get("hub.callback") or unsubscribe_data.get("hub_callback")
        topic = unsubscribe_data.get("hub.topic") or unsubscribe_data.get("hub_topic")
        
        if endpoint or (callback and topic):
            removed_count = cast_hub.remove_subscription(
                endpoint=endpoint.split("/bind/")[-1] if endpoint and "/bind/" in endpoint else None,
                callback=callback,
                topic=topic
            )
            return {"status": "unsubscribed", "removed": removed_count}
    
    # Otherwise, clear all subscriptions
    cast_hub.subscriptions.clear()
    print("[LOG] All subscriptions cleared")
    return {"status": "cleared"}


@app.websocket("/bind/{endpoint}")
async def websocket_endpoint(websocket: WebSocket, endpoint: str):
    """WebSocket endpoint for event delivery"""
    import asyncio
    
    await websocket.accept()
    cast_hub.log(f"WebSocket connection accepted for endpoint: {endpoint}")
    
    # Register WebSocket connection
    cast_hub.register_websocket(endpoint, websocket)
    
    # Send initial connection confirmation
    try:
        await websocket.send_json({
            "type": "connection.established",
            "endpoint": endpoint,
            "timestamp": datetime.now().isoformat()
        })
    except Exception as e:
        cast_hub.log(f"Error sending connection confirmation: {e}")
    
    # Keepalive task to prevent Azure from closing idle connections
    async def keepalive():
        while True:
            try:
                await asyncio.sleep(30)  # Send ping every 30 seconds
                await websocket.send_json({
                    "type": "ping",
                    "timestamp": datetime.now().isoformat()
                })
            except Exception as e:
                print(f"[LOG] Keepalive error for {endpoint}: {e}")
                break
    
    keepalive_task = asyncio.create_task(keepalive())
    
    try:
        while True:
            # Receive messages from client (ping, pong, etc.)
            data = await websocket.receive_text()
            try:
                message = json.loads(data)
                print(f"[LOG] Received WebSocket message from {endpoint}: {message}")
                
                # Respond to pong messages
                if message.get("type") == "pong":
                    cast_hub.log(f"Received pong from {endpoint}")
            except json.JSONDecodeError:
                print(f"[LOG] Received non-JSON WebSocket message from {endpoint}: {data}")
    except WebSocketDisconnect:
        cast_hub.log(f"WebSocket disconnected for endpoint: {endpoint}")
    except Exception as e:
        cast_hub.log(f"WebSocket error for endpoint {endpoint}: {type(e).__name__}: {e}")
    finally:
        # Cancel keepalive task
        keepalive_task.cancel()
        try:
            await keepalive_task
        except asyncio.CancelledError:
            pass
        
        # Unregister WebSocket connection
        cast_hub.unregister_websocket(endpoint)
        cast_hub.log(f"WebSocket cleanup completed for endpoint: {endpoint}")


@app.post("/oauth/token")
async def post_oauth_token():
    """Handle POST /oauth/token - OAuth token endpoint"""
    cast_hub.user_count += 1
    response = {
        "token_type": "Bearer",
        "expires_in": 3600,
        "scope": "openid",
        "topic": f"user-{cast_hub.user_count}",
        "id_token": f"mock_id_token_{cast_hub.user_count}",
        "access_token": f"mock_access_token_{cast_hub.user_count}"
    }
    return response


@app.get("/api/powercast-connector/configuration")
async def get_powercast_config():
    """Handle GET /api/powercast-connector/configuration"""
    return {"test_endpoint": "https://nuance-testserver/test/teste"}


@app.get("/api/powercast-connector/login")
async def get_powercast_login():
    """Handle GET /api/powercast-connector/login"""
    return {"authorization_endpoint": "https://nuance-auth0-server/oauth/authorize"}


@app.options("/api/hub/")
@app.options("/api/hub")
@app.options("/api/hub/{topic}")
async def options_hub():
    """Handle CORS preflight requests"""
    return Response(status_code=204)


def main():
    """Main function to run the Cast Hub server"""
    import argparse
    
    parser = argparse.ArgumentParser(description="Standalone Cast Hub Server")
    parser.add_argument("--port", type=int, default=2017, help="Server port (default: 2017)")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Server host (default: 0.0.0.0)")
    args = parser.parse_args()
    
    cast_hub.set_server_port(args.port)
    
    print("=" * 60)
    print("Standalone Cast Hub Server")
    print("=" * 60)
    print(f"Server running on http://{args.host}:{args.port}")
    print(f"Hub API endpoint: http://{args.host}:{args.port}/api/hub/")
    print("")
    print("Test endpoints:")
    print(f"  GET    http://{args.host}:{args.port}/")
    print(f"  GET    http://{args.host}:{args.port}/api/hub/admin (status page)")
    print(f"  GET    http://{args.host}:{args.port}/api/hub/{{topic}}")
    print(f"  POST   http://{args.host}:{args.port}/api/hub/")
    print(f"  POST   http://{args.host}:{args.port}/api/hub/{{topic}}")
    print(f"  DELETE http://{args.host}:{args.port}/api/hub/")
    print(f"  GET    http://{args.host}:{args.port}/topics")
    print(f"  GET    http://{args.host}:{args.port}/conference")
    print(f"  POST   http://{args.host}:{args.port}/conference")
    print(f"  DELETE http://{args.host}:{args.port}/conference")
    print(f"  POST   http://{args.host}:{args.port}/status")
    print(f"  POST   http://{args.host}:{args.port}/oauth/token")
    print(f"  GET    http://{args.host}:{args.port}/api/powercast-connector/configuration")
    print(f"  GET    http://{args.host}:{args.port}/api/powercast-connector/login")
    print("")
    print(f"WebSocket connections: ws://{args.host}:{args.port}/bind/<endpoint>")
    print("")
    print("Using FastAPI with built-in WebSocket support")
    print("Press Ctrl+C to stop the server")
    print("=" * 60)
    
    # Run the server
    # Note: On Windows, if Ctrl+C doesn't work:
    #   - Press Ctrl+C twice (second press forces interrupt)
    #   - Use Ctrl+Break instead
    #   - Close the terminal window
    #   - Or use: taskkill /F /PID <process_id>
    try:
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    except KeyboardInterrupt:
        print("\n[LOG] Server stopped by user")


if __name__ == "__main__":
    main()
