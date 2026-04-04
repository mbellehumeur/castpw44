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

    slicer.util.pip_install('fastapi', 'uvicorn', 'python-multipart', 'gunicorn')
"""

import sys
import os
import json
import uuid
import time
import asyncio
import base64
import copy
import socket
import urllib.parse
import urllib.request
import hmac
import hashlib
import glob
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional, Any
from datetime import datetime
from urllib.parse import parse_qs

# Try to import FastAPI - install with: pip install fastapi uvicorn
try:
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Response, HTTPException
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import JSONResponse, FileResponse, RedirectResponse
    from fastapi.staticfiles import StaticFiles
    from starlette.middleware.base import BaseHTTPMiddleware
    import uvicorn
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


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Middleware to log HTTP requests for specific endpoints"""
    
    async def dispatch(self, request: Request, call_next):
        # Check if this is an endpoint we want to log
        path = request.url.path
        method = request.method
        
        # Exclude certain endpoints from logging
        excluded_paths = ["/api/hub/status", "/api/hub/admin", "/api/hub/admin/", "/api/hub/logs", "/api/hub/logs/"]
        
        should_log = False
        if path.startswith("/api/hub") and method in ["GET", "POST", "DELETE"]:
            # Check if this path should be excluded
            if path not in excluded_paths:
                should_log = True
        elif path == "/oauth/token" and method == "POST":
            should_log = True
        elif path == "/conference" and method in ["GET", "POST", "DELETE"]:
            should_log = True
        
        if should_log:
            # Get client info
            client_host = request.client.host if request.client else "unknown"
            client_port = request.client.port if request.client else "unknown"
            client_addr = f"{client_host}:{client_port}"
            
            # Process request
            start_time = datetime.now()
            response = await call_next(request)
            process_time = (datetime.now() - start_time).total_seconds()
            
            # Log the request
            status_code = response.status_code
            log_message = f'INFO:     {client_addr} - "{method} {path} HTTP/1.1" {status_code}'
            cast_hub.log(log_message)
            
            return response
        else:
            # Don't log, just process normally
            return await call_next(request)


# Mount static files directory
base_dir = os.path.dirname(os.path.abspath(__file__))
static_dir = os.path.join(base_dir, "Resources", "docroot")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")


def _subscription_handles_event(sub: Dict, topic_name: str, event_type: str) -> bool:
    """Topic plus event filter — same eligibility as delivery (before channel / echo checks)."""
    if topic_name != sub.get("topic", ""):
        return False
    subscribed_events = (sub.get("events") or "").lower()
    et = event_type.lower()
    return et in subscribed_events or "*" in subscribed_events


class CastHub:
    """Cast Hub implementation for managing subscriptions and broadcasting events"""
    
    def __init__(self):
        self.subscriptions: List[Dict] = []
        self.websocket_connections: Dict[str, WebSocket] = {}  # endpoint -> WebSocket
        self.admin_websockets: List[Dict] = []  # Track admin connections with metadata: [{"websocket": WebSocket, "location": str, "connected_at": str}]
        self.log_websockets: List[Dict] = []  # Track log viewer connections: [{"websocket": WebSocket, "location": str, "connected_at": str}]
        self.log_queue: List[str] = []  # Queue of new log entries to broadcast
        self.log_queue_total: int = 0  # Monotonic counter of total entries ever added
        self.conferences: List[Dict] = []
        self.last_context: Dict[str, Dict] = {}  # topic -> context
        self.audit_log: List[Dict] = []  # List of logged events
        self.audit_log_counter: int = 0  # Incrementing message number
        self.server_port = 2017
        self.user_count = 0
        self.product_counts: Dict[str, int] = {}  # Track counts per productName
        self.page_loads = 0
        self.app_logs: List[str] = []  # Store recent logs
        self.max_logs = 100  # Keep last 100 log entries
        self.last_admin_refresh_time: float = 0.0  # Track last admin refresh time for rate limiting
        self.pending_admin_refresh_task = None  # Track pending refresh task (to cancel if needed)
        self.single_user_mode: bool = False  # When enabled, token endpoint always returns topic 'SINGLE-USER'
        self.pending_get_requests: Dict[str, Any] = {}  # request_id -> Queue for responses
    
    def log(self, message: str):
        """Add a log message to the in-memory log buffer"""
        timestamp = datetime.now().isoformat()
        log_entry = f"[{timestamp}] {message}"
        print(log_entry)  # Still print to stdout
        self.app_logs.append(log_entry)
        # Keep only last max_logs entries
        if len(self.app_logs) > self.max_logs:
            self.app_logs = self.app_logs[-self.max_logs:]
        
        # Add to queue for WebSocket broadcasting
        self.log_queue.append(log_entry)
        self.log_queue_total += 1
        if len(self.log_queue) > 1000:
            self.log_queue = self.log_queue[-1000:]
    
    def register_log_websocket(self, websocket: WebSocket, location: str = "unknown"):
        """Register a log viewer WebSocket connection"""
        # Check if this websocket is already registered
        for log_client in self.log_websockets:
            if log_client["websocket"] == websocket:
                return  # Already registered
        
        log_client = {
            "websocket": websocket,
            "location": location,
            "connected_at": datetime.now().isoformat()
        }
        self.log_websockets.append(log_client)
        self.log(f"Log viewer WebSocket registered from {location} (total: {len(self.log_websockets)})")
    
    def unregister_log_websocket(self, websocket: WebSocket):
        """Unregister a log viewer WebSocket connection"""
        for log_client in self.log_websockets[:]:
            if log_client["websocket"] == websocket:
                location = log_client.get("location", "unknown")
                self.log_websockets.remove(log_client)
                self.log(f"Log viewer WebSocket unregistered from {location} (remaining: {len(self.log_websockets)})")
                return
    
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
            self.log(f"WebSub verification error: {e}")
            return {"status": 500, "data": str(e)}
    
    def add_subscription(self, subscription_data: Dict) -> Dict:
        """Handle subscription/unsubscription requests - matches CastHubRequestHandler.handleHubSubscription"""
        hub_mode = subscription_data.get("hub.mode", subscription_data.get("hub_mode", "subscribe"))
        hub_topic = subscription_data.get("hub.topic", subscription_data.get("hub_topic", ""))
        hub_events = subscription_data.get("hub.events", subscription_data.get("hub_events", ""))
        hub_callback = subscription_data.get("hub.callback", subscription_data.get("hub_callback", ""))
        hub_secret = subscription_data.get("hub.secret", subscription_data.get("hub_secret", ""))
        hub_lease = subscription_data.get("hub.lease_seconds", subscription_data.get("hub.lease", subscription_data.get("hub_lease", "7200")))
        subscriber_name = subscription_data.get("subscriber.name", subscription_data.get("subscriber_name", "unknown"))
        raw_sub_actors = subscription_data.get("subscriber.actors")
        subscriber_actors = []
        if raw_sub_actors is not None:
            try:
                parsed_sub_actors = (
                    json.loads(raw_sub_actors)
                    if isinstance(raw_sub_actors, str)
                    else raw_sub_actors
                )
                if isinstance(parsed_sub_actors, list):
                    subscriber_actors = [
                        str(x).strip() for x in parsed_sub_actors if str(x).strip()
                    ]
                elif isinstance(parsed_sub_actors, str):
                    s = parsed_sub_actors.strip()
                    if s:
                        subscriber_actors = [s]
            except (json.JSONDecodeError, TypeError):
                s = str(raw_sub_actors).strip()
                if s:
                    subscriber_actors = [s]
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
                    self.log(f"WebSub verification failed: {verify_result['status']}")
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
                "actors": subscriber_actors,
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
            self.log(f"WebSocket unregistered for endpoint: {endpoint} (remaining: {len(self.websocket_connections)})")
    
    def register_admin_websocket(self, websocket: WebSocket, location: str = "unknown"):
        """Register an admin WebSocket connection with location info"""
        # Check if this websocket is already registered
        for admin_client in self.admin_websockets:
            if admin_client["websocket"] == websocket:
                return  # Already registered
        
        admin_client = {
            "websocket": websocket,
            "location": location,
            "connected_at": datetime.now().isoformat()
        }
        self.admin_websockets.append(admin_client)
        self.log(f"Admin WebSocket registered from {location} (total: {len(self.admin_websockets)})")
    
    def unregister_admin_websocket(self, websocket: WebSocket):
        """Unregister an admin WebSocket connection"""
        for admin_client in self.admin_websockets[:]:
            if admin_client["websocket"] == websocket:
                location = admin_client.get("location", "unknown")
                self.admin_websockets.remove(admin_client)
                self.log(f"Admin WebSocket unregistered from {location} (remaining: {len(self.admin_websockets)})")
                return
    
    async def _do_send_admin_refresh(self):
        """Internal method to actually send the refresh command"""
        if not self.admin_websockets:
            return
        
        self.last_admin_refresh_time = time.time()
        
        message = {
            "type": "refresh",
            "timestamp": datetime.now().isoformat()
        }
        
        disconnected = []
        for admin_client in self.admin_websockets:
            try:
                await admin_client["websocket"].send_json(message)
            except Exception as e:
                self.log(f"Error sending refresh to admin: {e}")
                disconnected.append(admin_client["websocket"])
        
        # Clean up disconnected websockets
        for ws in disconnected:
            self.unregister_admin_websocket(ws)
    
    async def send_admin_refresh_command(self):
        """Send refresh command to all connected admin clients (rate limited to max 1 per 2 seconds)
        If rate limited, schedules a delayed send. Only the last suppressed refresh will be sent.
        """
        if not self.admin_websockets:
            return
        
        current_time = time.time()
        time_since_last = current_time - self.last_admin_refresh_time
        
        # If enough time has passed (>= 2 seconds), send immediately
        if time_since_last >= 2.0:
            # Cancel any pending task since we're sending now
            if self.pending_admin_refresh_task and not self.pending_admin_refresh_task.done():
                self.pending_admin_refresh_task.cancel()
                self.pending_admin_refresh_task = None
            
            await self._do_send_admin_refresh()
        else:
            # Rate limited - cancel any existing pending task and schedule a new one
            # This ensures only the last suppressed refresh is sent
            if self.pending_admin_refresh_task and not self.pending_admin_refresh_task.done():
                self.pending_admin_refresh_task.cancel()
            
            # Schedule to send after remaining time (2.0 - time_since_last)
            delay = 2.0 - time_since_last
            
            async def delayed_send():
                await asyncio.sleep(delay)
                await self._do_send_admin_refresh()
                self.pending_admin_refresh_task = None
            
            self.pending_admin_refresh_task = asyncio.create_task(delayed_send())
    
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
    
    def get_audit_log(self, user_filter: Optional[str] = None, topic_filter: Optional[str] = None, event_filter: Optional[str] = None) -> List[Dict]:
        """Get audit log entries, optionally filtered by user, topic, or event"""
        filtered_log = self.audit_log
        if user_filter:
            filtered_log = [entry for entry in filtered_log if user_filter.lower() in entry.get("user", "").lower()]
        if topic_filter:
            filtered_log = [entry for entry in filtered_log if topic_filter.lower() in entry.get("topic", "").lower()]
        if event_filter:
            filtered_log = [entry for entry in filtered_log if event_filter.lower() in entry.get("event_name", "").lower()]
        # Return in reverse order (latest first)
        return list(reversed(filtered_log))
    
    def get_audit_log_unique_values(self) -> Dict[str, List[str]]:
        """Get unique users, topics, and events from audit log"""
        users = set()
        topics = set()
        events = set()
        for entry in self.audit_log:
            user = entry.get("user")
            topic = entry.get("topic")
            event_name = entry.get("event_name")
            if user and str(user).strip():  # Check for non-empty string
                users.add(str(user).strip())
            if topic and str(topic).strip():  # Check for non-empty string
                topics.add(str(topic).strip())
            if event_name and str(event_name).strip():  # Check for non-empty string
                events.add(str(event_name).strip())
        return {
            "users": sorted(list(users)),
            "topics": sorted(list(topics)),
            "events": sorted(list(events))
        }
    
    def clear_audit_log(self):
        """Clear all entries from the audit log"""
        self.audit_log.clear()
        self.log("Audit log cleared")

    def count_messages_for_subscriber(self, subscriber: str) -> Dict[str, int]:
        """Count audit rows for this subscriber (user field only; topic on the row is ignored)."""
        received = 0
        sent = 0
        key = (subscriber or "").strip()
        if not key:
            return {"received": 0, "sent": 0}
        for entry in self.audit_log:
            if (entry.get("user") or "").strip() != key:
                continue
            direction = entry.get("direction", "")
            if direction == "received":
                received += 1
            elif direction == "sent":
                sent += 1
        return {"received": received, "sent": sent}
    
    async def reset_all(self):
        """Reset everything - clear subscriptions, conferences, and audit log (like restarting the service)"""
        # Close all WebSocket connections
        disconnected_endpoints = []
        for endpoint, websocket in list(self.websocket_connections.items()):
            try:
                await websocket.close()
                self.log(f"WebSocket closed for endpoint: {endpoint}")
            except Exception as e:
                self.log(f"Error closing WebSocket for endpoint {endpoint}: {e}")
            disconnected_endpoints.append(endpoint)
        
        # Close all admin WebSocket connections
        disconnected_admin = []
        for admin_client in list(self.admin_websockets):
            try:
                await admin_client["websocket"].close()
                self.log("Admin WebSocket closed")
            except Exception as e:
                self.log(f"Error closing admin WebSocket: {e}")
            disconnected_admin.append(admin_client["websocket"])
        
        # Close all log WebSocket connections
        disconnected_logs = []
        for log_client in list(self.log_websockets):
            try:
                await log_client["websocket"].close()
                self.log("Log viewer WebSocket closed")
            except Exception as e:
                self.log(f"Error closing log viewer WebSocket: {e}")
            disconnected_logs.append(log_client["websocket"])
        
        # Clear all data
        self.subscriptions.clear()
        self.websocket_connections.clear()
        self.admin_websockets.clear()
        self.log_websockets.clear()
        self.conferences.clear()
        self.audit_log.clear()
        self.audit_log_counter = 0
        self.user_count = 0
        self.product_counts.clear()
        self.page_loads = 0
        self.last_context.clear()
        self.pending_get_requests.clear()
        
        self.log(f"Hub reset - all subscriptions, conferences, audit log cleared, and {len(disconnected_endpoints)} WebSocket(s) disconnected")


# Global Cast Hub instance
cast_hub = CastHub()

# Add the request logging middleware (after cast_hub is created)
app.add_middleware(RequestLoggingMiddleware)


async def _parse_request_body(request: Request) -> dict:
    """Parse request body as JSON or form data, with fallback to raw body parsing."""
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        return await request.json()
    try:
        form_data = await request.form()
        merged = {}
        for key in form_data.keys():
            values = form_data.getlist(key)
            merged[key] = values[0] if len(values) == 1 else values
        return merged
    except AssertionError as e:
        if "python-multipart" in str(e):
            body = await request.body()
            if body:
                parsed = parse_qs(body.decode())
                return {k: v[0] if len(v) == 1 else v for k, v in parsed.items()}
            return {}
        raise


def _serve_html_page(filename: str):
    """Serve an HTML file from Resources/docroot, or redirect to static mount."""
    html_path = os.path.join(base_dir, "Resources", "docroot", filename)
    if os.path.exists(html_path):
        with open(html_path, 'r', encoding='utf-8') as f:
            html_content = f.read()
        return Response(content=html_content, media_type="text/html")
    return RedirectResponse(url=f"/static/{filename}", status_code=302)


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
    try:
        data = await _parse_request_body(request)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not parse request: {e}")
    
    conference = {
        "user": data.get("user"),
        "title": data.get("title"),
        "topics": data.get("topics", [])
    }
    cast_hub.conferences.append(conference)
    cast_hub.log(f"Conference created: {conference.get('title')}")

    # Send conference-start to all participants' WebSockets (title + subscriber names)
    conference_user = conference.get("user")
    attendee_topics = conference.get("topics", [])
    all_participant_topics = [conference_user] + attendee_topics
    subscriber_names = []
    sent_endpoints = set()
    for participant_topic in all_participant_topics:
        for sub in cast_hub.subscriptions:
            if sub.get("topic") == participant_topic and sub.get("channel") == "websocket":
                name = sub.get("subscriber", "unknown")
                if name not in subscriber_names:
                    subscriber_names.append(name)
                break
    # Cast-style message: timestamp, id, event with hub.topic, hub.event, context
    notification = {
        "timestamp": datetime.now().isoformat(),
        "id": str(uuid.uuid4()),
        "event": {
            "hub.topic": conference_user or "",
            "hub.event": "conference-start",
            "context": {
                "title": conference.get("title") or "",
                "participants": subscriber_names,
            },
        },
    }
    message_json = json.dumps(notification)
    for participant_topic in all_participant_topics:
        for sub in cast_hub.subscriptions:
            if sub.get("topic") == participant_topic and sub.get("channel") == "websocket":
                endpoint = sub.get("websocket_endpoint")
                if endpoint and endpoint in cast_hub.websocket_connections and endpoint not in sent_endpoints:
                    try:
                        ws = cast_hub.websocket_connections[endpoint]
                        await ws.send_text(message_json)
                        sent_endpoints.add(endpoint)
                        cast_hub.log(f"Sent conference-start to participant: {sub.get('subscriber')}")
                    except Exception as e:
                        cast_hub.log(f"Conference-start WebSocket error: {e}")

    # Send admin refresh command (rate limited)
    await cast_hub.send_admin_refresh_command()
    
    return {"status": "created"}


@app.delete("/conference")
async def delete_conference(request: Request):
    """Delete a conference"""
    try:
        data = await _parse_request_body(request)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not parse request: {e}")
    
    user = data.get("user")
    removed = []
    for conf in cast_hub.conferences[:]:
        if conf.get("user") == user:
            cast_hub.conferences.remove(conf)
            removed.append(conf)
        elif user in conf.get("topics", []):
            cast_hub.log(f"User {user} exited conference {conf.get('title')}")
    
    # Send admin refresh command if conferences were removed (rate limited)
    if len(removed) > 0:
        await cast_hub.send_admin_refresh_command()
    
    return {"removed": len(removed)}


@app.get("/status")
async def get_status():
    """Status endpoint - returns hub status"""
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
    except Exception:
        pass
    
    status_msg += f"Subscriptions: {len(cast_hub.get_subscriptions())}\n"
    for sub in cast_hub.get_subscriptions():
        status_msg += f"  - {sub.get('subscriber', 'unknown')}: {sub.get('topic', 'unknown')} ({sub.get('host', 'unknown')})\n"
    
    print(status_msg)
    return Response(content=status_msg, media_type="text/plain")


@app.get("/api/hub/test-client")
@app.get("/api/hub/test-client/")
async def get_test_client():
    """Get test client page for subscribing and publishing"""
    return _serve_html_page("test-client.html")


@app.get("/api/hub/conference-client")
@app.get("/api/hub/conference-client/")
async def get_conference_client():
    """Get conference client page"""
    return _serve_html_page("conference-client.html")


@app.get("/api/hub/admin")
@app.get("/api/hub/admin/")
async def get_hub_status():
    """Get hub status page showing all users and endpoints"""
    return _serve_html_page("admin.html")


@app.get("/api/hub/logs")
@app.get("/api/hub/logs/")
async def get_logs_viewer():
    """Get logs viewer page"""
    return _serve_html_page("logs.html")


@app.get("/api/hub/status")
async def get_hub_status_json():
    """Get hub status as JSON"""
    subscriptions = cast_hub.get_subscriptions()
    count_by_subscriber: Dict[str, Dict[str, int]] = {}
    subscriptions_with_counts = []
    for sub in subscriptions:
        row = dict(sub)
        key = (sub.get("subscriber") or "").strip()
        if key not in count_by_subscriber:
            count_by_subscriber[key] = cast_hub.count_messages_for_subscriber(key)
        c = count_by_subscriber[key]
        row["published"] = c["sent"]
        row["received"] = c["received"]
        subscriptions_with_counts.append(row)

    return {
        "total_subscriptions": len(subscriptions),
        "total_websockets": len(cast_hub.websocket_connections),
        "total_topics": len(set(sub.get("topic") for sub in subscriptions if sub.get("topic"))),
        "total_messages": len(cast_hub.audit_log),
        "total_admin_clients": len(cast_hub.admin_websockets),
        "single_user_mode": cast_hub.single_user_mode,
        "subscriptions": subscriptions_with_counts,
        "websocket_endpoints": list(cast_hub.websocket_connections.keys()),
        "topics": list(set(sub.get("topic") for sub in subscriptions if sub.get("topic")))
    }


@app.get("/api/audit-log")
async def get_audit_log(user: Optional[str] = None, topic: Optional[str] = None, event: Optional[str] = None):
    """Get audit log entries with optional filtering by user, topic, or event"""
    log_entries = cast_hub.get_audit_log(user_filter=user, topic_filter=topic, event_filter=event)
    unique_values = cast_hub.get_audit_log_unique_values()
    return {
        "entries": log_entries,
        "count": len(log_entries),
        "unique_users": unique_values["users"],
        "unique_topics": unique_values["topics"],
        "unique_events": unique_values["events"]
    }


@app.delete("/api/audit-log")
async def clear_audit_log():
    """Clear all audit log entries"""
    cast_hub.clear_audit_log()
    return {"status": "cleared", "message": "Audit log cleared successfully"}


@app.get("/images/3DSlicer-DesktopIcon.png")
async def get_slicer_icon():
    """Serve the 3D Slicer desktop icon"""
    icon_path = os.path.join(os.path.dirname(__file__), "3DSlicer-DesktopIcon.png")
    if os.path.exists(icon_path):
        return FileResponse(icon_path, media_type="image/png")
    icon_path = os.path.join(os.path.dirname(__file__), "Resources", "docroot", "images", "3DSlicer-DesktopIcon.png")
    if os.path.exists(icon_path):
        return FileResponse(icon_path, media_type="image/png")
    raise HTTPException(status_code=404, detail="Icon not found")


@app.get("/favicon.ico")
async def get_favicon():
    """Serve the favicon"""
    favicon_path = os.path.join(os.path.dirname(__file__), "Resources", "docroot", "favicon.ico")
    if os.path.exists(favicon_path):
        return FileResponse(favicon_path, media_type="image/x-icon")
    else:
        fallback_path = os.path.join("Modules", "Scripted", "Cast", "Resources", "docroot", "favicon.ico")
        if os.path.exists(fallback_path):
            return FileResponse(fallback_path, media_type="image/x-icon")
        raise HTTPException(status_code=404, detail="Favicon not found")


def _load_sceneview_from_folder(folder_path: str) -> Optional[Dict[str, Any]]:
    """Load View, Slice, and Camera data from the most recent *Scene*.mrml or *.mrml in folder_path."""
    if not folder_path or not os.path.isdir(folder_path):
        return None
    pattern = os.path.join(folder_path, "*.mrml")
    files = glob.glob(pattern)
    if not files:
        return None
    scene_files = [f for f in files if "Scene" in os.path.basename(f)]
    candidates = scene_files if scene_files else files
    best = max(candidates, key=lambda f: os.path.getmtime(f))
    try:
        tree = ET.parse(best)
        root = tree.getroot()
    except ET.ParseError:
        return None

    cameras_by_tag: Dict[str, Dict[str, Any]] = {}
    for cam in root.findall("Camera"):
        tag = (cam.get("singletonTag") or cam.get("layoutLabel") or "").strip()
        cameras_by_tag[tag] = {
            "position": cam.get("position"),
            "focalPoint": cam.get("focalPoint"),
            "viewUp": cam.get("viewUp"),
            "viewAngle": cam.get("viewAngle"),
            "parallelProjection": cam.get("parallelProjection"),
            "parallelScale": cam.get("parallelScale"),
        }

    viewports: List[Dict[str, Any]] = []
    for view in root.findall("View"):
        tag = (view.get("layoutLabel") or view.get("layoutName") or view.get("singletonTag") or "").strip()
        v: Dict[str, Any] = {
            "type": "View",
            "id": view.get("id"),
            "name": view.get("name"),
            "layoutLabel": view.get("layoutLabel"),
            "layoutName": view.get("layoutName"),
            "fieldOfView": view.get("fieldOfView"),
        }
        if tag and tag in cameras_by_tag:
            v["camera"] = cameras_by_tag[tag]
        viewports.append(v)

    for sl in root.findall("Slice"):
        s: Dict[str, Any] = {
            "type": "Slice",
            "id": sl.get("id"),
            "name": sl.get("name"),
            "layoutLabel": sl.get("layoutLabel"),
            "layoutName": sl.get("layoutName"),
            "orientation": sl.get("orientation"),
            "fieldOfView": sl.get("fieldOfView"),
            "dimensions": sl.get("dimensions"),
            "sliceToRAS": sl.get("sliceToRAS"),
        }
        viewports.append(s)

    return {"source": "sceneview", "file": os.path.basename(best), "viewports": viewports}


@app.get("/api/hub/cast-get")
@app.get("/api/hub/cast-get/")
async def get_cast_subscriber(request: Request):
    """Check if a subscriber exists and is connected"""
    subscriber = request.query_params.get("subscriber", "").strip()
    data_type = request.query_params.get("dataType", "").strip()
    topic_param = request.query_params.get("topic", "").strip()
    actor_param = request.query_params.get("actor", "").strip()
    actor_value = None
    if actor_param:
        try:
            actor_value = json.loads(actor_param)
        except json.JSONDecodeError:
            actor_value = actor_param

    if not subscriber:
        raise HTTPException(status_code=400, detail="Missing 'subscriber' parameter")
    
    # When subscriber is 3DSLICER, load sceneview from in-project sceneview folder
    if subscriber.upper() == "3DSLICER":
        sceneview_folder = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sceneview")
        response_data = _load_sceneview_from_folder(sceneview_folder)
        if response_data is None:
            response_data = {"error": "No sceneview data found", "folder": sceneview_folder}
        cast_hub.log(f"Loaded sceneview for subscriber 3DSLICER from {sceneview_folder}")
        out_3d = {
            "subscriber": subscriber,
            "dataType": data_type if data_type else None,
            "exists": True,
            "connected": False,
            "topic": topic_param if topic_param else None,
            "endpoint": None,
            "response": response_data,
        }
        if actor_value is not None:
            out_3d["actor"] = actor_value
        return out_3d
    
    # Check if subscriber has a subscription
    subscriber_exists = False
    subscriber_connected = False
    subscription_info = None
    
    for sub in cast_hub.get_subscriptions():
        sub_name = sub.get("subscriber", "").strip()
        if sub_name != subscriber:
            continue
        if topic_param and sub.get("topic", "").strip() != topic_param:
            continue
        subscriber_exists = True
        subscription_info = sub

        # Check if they have an active WebSocket connection
        websocket_endpoint = sub.get("websocket_endpoint")
        if websocket_endpoint and websocket_endpoint in cast_hub.websocket_connections:
            subscriber_connected = True
        break
    
    # If subscriber exists and is connected, send message and wait for response
    response_data = None
    request_id = None
    if subscriber_exists and subscriber_connected and subscription_info:
        websocket_endpoint = subscription_info.get("websocket_endpoint")
        if websocket_endpoint and websocket_endpoint in cast_hub.websocket_connections:
            try:
                websocket = cast_hub.websocket_connections[websocket_endpoint]
                
                # Generate unique request ID
                request_id = str(uuid.uuid4())
                
                # Create queue for response
                response_queue = asyncio.Queue()
                cast_hub.pending_get_requests[request_id] = response_queue
                
                # Create message with dataType request in event format
                topic_name = subscription_info.get("topic", "")
                notification = {
                    "timestamp": datetime.now().isoformat(),
                    "id": str(uuid.uuid4()),
                    "event": {
                        "hub.topic": topic_name,
                        "hub.event": "get-request",
                        "context": {
                            "requestId": request_id,
                            "dataType": data_type if data_type else None
                        }
                    }
                }
                if actor_value is not None:
                    notification["actor"] = actor_value
                message_json = json.dumps(notification)
                
                await websocket.send_text(message_json)
                cast_hub.log(f"Sent get request message to subscriber '{subscriber}' with dataType '{data_type}', requestId: {request_id}")
                
                # Wait for response with timeout (5 seconds)
                try:
                    response_data = await asyncio.wait_for(response_queue.get(), timeout=5.0)
                    cast_hub.log(f"Received response for requestId {request_id}")
                except asyncio.TimeoutError:
                    cast_hub.log(f"Timeout waiting for response to requestId {request_id}")
                    response_data = {"error": "Timeout waiting for response"}
                finally:
                    # Clean up
                    if request_id in cast_hub.pending_get_requests:
                        del cast_hub.pending_get_requests[request_id]
                
            except Exception as e:
                cast_hub.log(f"Error sending WebSocket message to subscriber '{subscriber}': {type(e).__name__}: {e}")
                # Mark as not connected if send fails
                subscriber_connected = False
                # Clean up request if it exists
                if request_id and request_id in cast_hub.pending_get_requests:
                    del cast_hub.pending_get_requests[request_id]
    
    out = {
        "subscriber": subscriber,
        "dataType": data_type if data_type else None,
        "exists": subscriber_exists,
        "connected": subscriber_connected,
        "topic": subscription_info.get("topic") if subscription_info else None,
        "endpoint": subscription_info.get("websocket_endpoint") if subscription_info else None,
        "response": response_data,
    }
    if topic_param:
        out["requestedTopic"] = topic_param
    if actor_value is not None:
        out["actor"] = actor_value
    return out


def _dicom_send_websocket_frames(notification: dict, notification_json: str) -> tuple:
    """
    WebSocket subscribers receive dicom-send as: (1) JSON without embedded base64 payload,
    with resource.binaryTransfer=True and resource.byteLength set; (2) raw DICOM bytes.
    WebSub subscribers still receive the full JSON (notification_json) with base64 data.
    """
    event = notification.get("event") or {}
    if event.get("hub.event") != "dicom-send":
        return notification_json, None

    n2 = copy.deepcopy(notification)
    ev2 = n2.get("event") or {}
    ctx = ev2.get("context")
    items = ctx if isinstance(ctx, list) else ([ctx] if ctx is not None else [])

    for item in items:
        if not isinstance(item, dict):
            continue
        res = item.get("resource")
        if not isinstance(res, dict):
            continue
        data = res.get("data")
        if not isinstance(data, str) or not data:
            continue
        try:
            raw = base64.b64decode(data)
        except Exception:
            return notification_json, None
        stripped = copy.deepcopy(res)
        stripped.pop("data", None)
        stripped["binaryTransfer"] = True
        stripped["byteLength"] = len(raw)
        item["resource"] = stripped
        return json.dumps(n2), raw

    return notification_json, None


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


async def _handle_publish_notification(notification: dict, path_topic: str = ""):
    """Handle publish notification payload and broadcast to subscribers."""
    message_id = notification.get("id", "unknown")
    event = notification.get("event", {})
    event_type = event.get("hub.event", "unknown")
    topic_name = event.get("hub.topic", "").strip()

    if not topic_name:
        topic_name = (path_topic or "").strip()
        if not topic_name:
            raise HTTPException(status_code=400, detail="Missing event.hub.topic for publish request")
        # Keep event payload canonical for downstream logic/audit.
        event["hub.topic"] = topic_name
        notification["event"] = event

    cast_hub.log(f"Received cast message ID: {message_id} for topic {topic_name}, event: {event_type}")

    # Broadcast to subscribers (sync method, but WebSocket sending needs async)
    context = event.get("context", {})

    # Full JSON for HMAC + WebSub; WebSocket may use stripped JSON + binary body for dicom-send
    notification_json = json.dumps(notification)
    ws_text, ws_binary = _dicom_send_websocket_frames(notification, notification_json)

    def _audit_context():
        if event_type == "dicom-send" and ws_binary is not None:
            try:
                return json.loads(ws_text).get("event", {}).get("context", context)
            except Exception:
                return {"hub.event": "dicom-send", "binaryTransfer": True}
        return context

    audit_ctx = _audit_context()

    # Update lastContext
    if "close" in event_type.lower():
        cast_hub.last_context[topic_name] = {}
    else:
        if context:
            if event_type == "dicom-send" and ws_binary is not None:
                cast_hub.last_context[topic_name] = audit_ctx
            else:
                cast_hub.last_context[topic_name] = context

    # Audit: one "received" per subscriber whose subscription matches this publish (topic + events).
    matching_subs = [sub for sub in cast_hub.subscriptions if _subscription_handles_event(sub, topic_name, event_type)]
    if matching_subs:
        for sub in matching_subs:
            cast_hub.add_audit_log(
                user=sub.get("subscriber", "unknown"),
                topic=topic_name,
                event_name=event_type,
                event_data=audit_ctx,
                direction="received",
            )
    else:
        user = topic_name if topic_name.startswith("user-") else topic_name
        cast_hub.add_audit_log(
            user=user,
            topic=topic_name,
            event_name=event_type,
            event_data=audit_ctx,
            direction="received",
        )
    
    # Publisher subscriber to suppress echo (from hub.source in event)
    publisher_subscriber = event.get("hub.source", "").strip() or None
    
    # Track endpoints that have already received the message to prevent duplicates
    sent_endpoints = set()
    
    # Send to matching subscriptions
    for sub in cast_hub.subscriptions[:]:  # Copy to allow removal
        if not _subscription_handles_event(sub, topic_name, event_type):
            continue

        secret = sub.get("secret", "")
        channel = sub.get("channel", "websub")
        
        # Calculate HMAC
        hmac_sig = ""
        if secret:
            hmac_sig = hmac.new(secret.encode(), notification_json.encode(), hashlib.sha256).hexdigest()
        
        if channel == "websocket":
            # Skip publisher (suppress echo) when hub.source matches
            if publisher_subscriber and sub.get("subscriber") == publisher_subscriber:
                continue
            # WebSocket delivery - async
            endpoint = sub.get("websocket_endpoint")
            if endpoint and endpoint in cast_hub.websocket_connections:
                try:
                    websocket = cast_hub.websocket_connections[endpoint]
                    if ws_binary is not None:
                        await websocket.send_text(ws_text)
                        await websocket.send_bytes(ws_binary)
                    else:
                        await websocket.send_text(notification_json)
                    cast_hub.log(f"Sent WebSocket message to {sub.get('subscriber')} via endpoint {endpoint}")
                    # Track this endpoint as having received the message
                    sent_endpoints.add(endpoint)
                    # Log sent message
                    cast_hub.add_audit_log(
                        user=sub.get("subscriber", "unknown"),
                        topic=topic_name,
                        event_name=event_type,
                        event_data=audit_ctx,
                        direction="sent"
                    )
                except Exception as e:
                    cast_hub.log(f"WebSocket send error for {endpoint}: {type(e).__name__}: {e}")
                    cast_hub.log(f"Removing failed WebSocket connection and subscription")
                    if endpoint in cast_hub.websocket_connections:
                        del cast_hub.websocket_connections[endpoint]
                    if sub in cast_hub.subscriptions:
                        cast_hub.subscriptions.remove(sub)
            else:
                if not endpoint:
                    cast_hub.log(f"WebSocket endpoint not set for subscription: {sub.get('subscriber')}")
                else:
                    cast_hub.log(f"WebSocket not bound for subscription: {sub.get('subscriber')}")
        else:
            # Skip publisher (suppress echo) when hub.source matches
            if publisher_subscriber and sub.get("subscriber") == publisher_subscriber:
                continue
            # WebSub delivery - HTTP POST to callback (can be async but using sync for now)
            callback = sub.get("callback")
            if callback:
                try:
                    req = urllib.request.Request(callback)
                    req.add_header("Content-Type", "application/json")
                    req.add_header("X-Hub-Signature", f"sha256={hmac_sig}")
                    req.data = notification_json.encode()
                    req.get_method = lambda: "POST"
                    
                    loop = asyncio.get_event_loop()
                    await loop.run_in_executor(None, lambda: urllib.request.urlopen(req, timeout=5))
                    cast_hub.log(f"Sent WebSub notification to {callback}")
                    # Log sent message
                    cast_hub.add_audit_log(
                        user=sub.get("subscriber", "unknown"),
                        topic=topic_name,
                        event_name=event_type,
                        event_data=audit_ctx,
                        direction="sent"
                    )
                except Exception as e:
                    cast_hub.log(f"WebSub delivery error to {callback}: {e}")
    
    # Handle conferences - broadcast to attendees (skip if already sent)
    for conference in cast_hub.conferences:
        conference_user = conference.get("user")
        attendee_topics = conference.get("topics", [])
        
        # Check if message is from any conference participant (host or attendee)
        is_participant = (conference_user == topic_name) or (topic_name in attendee_topics)
        
        if is_participant:
            # Send to all participants (host + all attendees)
            all_participants = [conference_user] + attendee_topics
            
            for participant_topic in all_participants:
                # Find subscriptions for participant
                for sub in cast_hub.subscriptions:
                    if sub.get("topic") == participant_topic and sub.get("channel") == "websocket":
                        # Skip publisher (suppress echo) when hub.source matches
                        if publisher_subscriber and sub.get("subscriber") == publisher_subscriber:
                            continue
                        endpoint = sub.get("websocket_endpoint")
                        if endpoint and endpoint in cast_hub.websocket_connections:
                            # Skip if already sent to this endpoint
                            if endpoint in sent_endpoints:
                                continue
                            try:
                                websocket = cast_hub.websocket_connections[endpoint]
                                if ws_binary is not None:
                                    await websocket.send_text(ws_text)
                                    await websocket.send_bytes(ws_binary)
                                else:
                                    await websocket.send_text(notification_json)
                                cast_hub.log(f"Sent conference message to participant: {participant_topic}")
                                # Track this endpoint as having received the message
                                sent_endpoints.add(endpoint)
                                # Log sent conference message
                                cast_hub.add_audit_log(
                                    user=sub.get("subscriber", "unknown"),
                                    topic=participant_topic,
                                    event_name=event_type,
                                    event_data=audit_ctx,
                                    direction="sent"
                                )
                            except Exception as e:
                                cast_hub.log(f"Conference WebSocket error: {e}")

    return {"status": "received"}


@app.post("/api/hub/")
@app.post("/api/hub")
async def post_hub(request: Request):
    """Handle subscribe/unsubscribe and publish requests."""
    try:
        request_data = await _parse_request_body(request)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not parse request: {e}")
    
    # Also get query parameters
    query_params = dict(request.query_params)
    request_data.update(query_params)
    
    # Add host header for WebSocket URL generation
    request_data["host"] = request.headers.get("host", request.headers.get("Host", ""))
    
    try:
        hub_mode = request_data.get("hub.mode", request_data.get("hub_mode", "")).strip().lower()
        if hub_mode == "subscribe" or hub_mode == "unsubscribe":
            subscription_data = request_data
            if hub_mode == "unsubscribe":
                # Handle unsubscribe
                result = cast_hub.add_subscription(subscription_data)
                return {"status": "unsubscribed", "removed": result.get("removed", 0)}
            # Handle subscribe
            result = cast_hub.add_subscription(subscription_data)
            
            # Return appropriate response - 202 Accepted for subscription requests
            if result.get("websocket_url"):
                return JSONResponse(
                    content={"hub.channel.endpoint": result["websocket_url"]},
                    status_code=202
                )
            return JSONResponse(
                content={"status": "subscribed", "subscription": result["subscription"]},
                status_code=202
            )

        # Publish via /api/hub or /api/hub/
        event = request_data.get("event")
        if isinstance(event, dict):
            return await _handle_publish_notification(request_data)

        raise HTTPException(
            status_code=400,
            detail="Invalid /api/hub POST payload: expected hub.mode for subscribe/unsubscribe or event payload for publish"
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        # Send admin refresh command (rate limited)
        await cast_hub.send_admin_refresh_command()


@app.post("/api/hub/{topic}")
async def post_hub_topic(topic: str, request: Request):
    """Handle POST /api/hub/{topic} - receive events and broadcast to subscribers"""
    try:
        notification = await request.json()
        return await _handle_publish_notification(notification, path_topic=topic)
    except HTTPException:
        raise
    except Exception as e:
        cast_hub.log(f"Error handling event: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        # Send admin refresh command (rate limited)
        await cast_hub.send_admin_refresh_command()


@app.delete("/api/hub/")
@app.delete("/api/hub")
async def delete_hub(request: Request):
    """Handle DELETE /api/hub/ - clear all subscriptions"""
    try:
        unsubscribe_data = await _parse_request_body(request)
    except Exception:
        unsubscribe_data = {}
    
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
    cast_hub.log("All subscriptions cleared")
    return {"status": "cleared"}


@app.websocket("/bind/{endpoint}")
async def websocket_endpoint(websocket: WebSocket, endpoint: str):
    """WebSocket endpoint for event delivery"""
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
    
    # Send admin refresh on connection
    await cast_hub.send_admin_refresh_command()
    
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
                cast_hub.log(f"Keepalive error for {endpoint}: {e}")
                break
    
    keepalive_task = asyncio.create_task(keepalive())
    
    try:
        while True:
            # Receive messages from client (ping, pong, etc.)
            data = await websocket.receive_text()
            try:
                message = json.loads(data)
                cast_hub.log(f"Received WebSocket message from {endpoint}: {message}")
                
                # Check if this is a response to a pending get_request (in event format)
                event = message.get("event", {})
                if event.get("hub.event") == "get-response":
                    context = event.get("context", {})
                    request_id = context.get("requestId")
                    if request_id and request_id in cast_hub.pending_get_requests:
                        response_queue = cast_hub.pending_get_requests[request_id]
                        await response_queue.put(message)
                        cast_hub.log(f"Routed get-response for requestId {request_id}")
                    else:
                        cast_hub.log(f"Received get-response for unknown requestId {request_id}")
                
                # Respond to pong messages
                if message.get("type") == "pong":
                    cast_hub.log(f"Received pong from {endpoint}")
            except json.JSONDecodeError:
                cast_hub.log(f"Received non-JSON WebSocket message from {endpoint}: {data}")
    except WebSocketDisconnect:
        cast_hub.log(f"WebSocket disconnected for endpoint: {endpoint}")
    except Exception as e:
        cast_hub.log(f"WebSocket error for endpoint {endpoint}: {type(e).__name__}: {e}")
        # Send admin refresh on error
        await cast_hub.send_admin_refresh_command()
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
        
        # Send admin refresh on disconnect
        await cast_hub.send_admin_refresh_command()


@app.websocket("/ws/admin")
async def admin_websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint for admin page - receives refresh commands"""
    await websocket.accept()
    
    # Extract location information from request headers
    location = "unknown"
    try:
        # Try to get client IP and other location info
        client_host = websocket.client.host if websocket.client else "unknown"
        client_port = websocket.client.port if websocket.client else "unknown"
        location = f"{client_host}:{client_port}"
    except Exception as e:
        cast_hub.log(f"Could not extract location info: {e}")
        location = "unknown"
    
    cast_hub.log(f"Admin WebSocket connection accepted from {location}")
    
    # Register admin WebSocket with location
    cast_hub.register_admin_websocket(websocket, location)
    
    # Send initial connection confirmation
    try:
        await websocket.send_json({
            "type": "connection.established",
            "role": "admin",
            "timestamp": datetime.now().isoformat()
        })
    except Exception as e:
        cast_hub.log(f"Error sending admin connection confirmation: {e}")
    
    try:
        while True:
            # Receive messages from admin client (pong, etc.)
            data = await websocket.receive_text()
            try:
                message = json.loads(data)
                cast_hub.log(f"Received admin WebSocket message: {message}")
                
                # Respond to pong messages
                if message.get("type") == "pong":
                    cast_hub.log("Received pong from admin client")
            except json.JSONDecodeError:
                cast_hub.log(f"Received non-JSON admin message: {data}")
    except WebSocketDisconnect:
        cast_hub.log("Admin WebSocket disconnected")
    except Exception as e:
        cast_hub.log(f"Admin WebSocket error: {type(e).__name__}: {e}")
    finally:
        # Unregister admin WebSocket
        cast_hub.unregister_admin_websocket(websocket)
        cast_hub.log("Admin WebSocket cleanup completed")


@app.websocket("/ws/logs")
async def logs_websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint for log viewer - streams application logs"""
    await websocket.accept()
    
    # Extract location information from request
    location = "unknown"
    try:
        client_host = websocket.client.host if websocket.client else "unknown"
        client_port = websocket.client.port if websocket.client else "unknown"
        location = f"{client_host}:{client_port}"
    except Exception as e:
        cast_hub.log(f"Could not extract location info: {e}")
        location = "unknown"
    
    cast_hub.log(f"Log viewer WebSocket connection accepted from {location}")
    
    # Register log WebSocket with location
    cast_hub.register_log_websocket(websocket, location)
    
    # Send initial connection confirmation and all recent logs
    try:
        await websocket.send_json({
            "type": "connection.established",
            "role": "log_viewer",
            "timestamp": datetime.now().isoformat()
        })
        
        # Send all recent logs
        recent_logs = cast_hub.get_logs(100)  # Send last 100 logs
        for log_entry in recent_logs:
            await websocket.send_json({
                "type": "log",
                "message": log_entry,
                "timestamp": datetime.now().isoformat()
            })
    except Exception as e:
        cast_hub.log(f"Error sending initial logs: {e}")
    
    # Track total log entries seen so far (monotonic, survives queue trimming)
    last_seen_total = [cast_hub.log_queue_total]
    
    async def broadcast_new_logs():
        """Background task to check for new logs and broadcast them"""
        while True:
            try:
                await asyncio.sleep(0.1)
                current_total = cast_hub.log_queue_total
                
                if current_total > last_seen_total[0]:
                    new_count = min(current_total - last_seen_total[0], len(cast_hub.log_queue))
                    new_logs = cast_hub.log_queue[-new_count:] if new_count > 0 else []
                    for log_entry in new_logs:
                        try:
                            await websocket.send_json({
                                "type": "log",
                                "message": log_entry,
                                "timestamp": datetime.now().isoformat()
                            })
                        except Exception as e:
                            cast_hub.log(f"Error sending log entry: {e}")
                            break
                    last_seen_total[0] = current_total
            except Exception as e:
                cast_hub.log(f"Error in broadcast_new_logs: {e}")
                break
    
    broadcast_task = asyncio.create_task(broadcast_new_logs())
    
    try:
        while True:
            # Receive messages from log viewer client (pong, etc.)
            data = await websocket.receive_text()
            try:
                message = json.loads(data)
                cast_hub.log(f"Received log viewer WebSocket message: {message}")
                
                # Respond to pong messages
                if message.get("type") == "pong":
                    cast_hub.log("Received pong from log viewer client")
            except json.JSONDecodeError:
                cast_hub.log(f"Received non-JSON log viewer message: {data}")
    except WebSocketDisconnect:
        cast_hub.log("Log viewer WebSocket disconnected")
    except Exception as e:
        cast_hub.log(f"Log viewer WebSocket error: {type(e).__name__}: {e}")
    finally:
        # Cancel broadcast task
        broadcast_task.cancel()
        try:
            await broadcast_task
        except asyncio.CancelledError:
            pass
        
        # Unregister log WebSocket
        cast_hub.unregister_log_websocket(websocket)
        cast_hub.log("Log viewer WebSocket cleanup completed")


@app.post("/api/admin/refresh")
async def trigger_admin_refresh():
    """Trigger refresh command to all connected admin clients"""
    await cast_hub.send_admin_refresh_command()
    return {"status": "sent", "clients": len(cast_hub.admin_websockets)}


@app.post("/api/admin/reset")
async def reset_hub(request: Request):
    """Reset the hub - clear all subscriptions, conferences, and audit log (like restarting the service)"""
    # Parse request body for single_user_mode
    single_user_mode = False
    try:
        data = await _parse_request_body(request)
        single_user_mode = data.get("single_user_mode", False)
        if isinstance(single_user_mode, str):
            single_user_mode = single_user_mode.lower() == "true"
    except Exception:
        pass
    
    # Set single-user mode
    cast_hub.single_user_mode = single_user_mode
    
    await cast_hub.reset_all()
    
    # Note: No need to send refresh command since all admin websockets were just closed
    
    mode_msg = " (Single-user mode enabled)" if single_user_mode else ""
    return {"status": "reset", "message": f"All subscriptions, conferences, audit log cleared, and all WebSocket connections disconnected{mode_msg}"}


@app.post("/oauth/token")
async def post_oauth_token(request: Request):
    """Handle POST /oauth/token - OAuth token endpoint"""
    try:
        request_data = await _parse_request_body(request)
    except Exception:
        request_data = {}
    
    # Also check query parameters
    query_params = dict(request.query_params)
    request_data.update(query_params)
    
    # Check if single-user mode is enabled
    if cast_hub.single_user_mode:
        topic = "SINGLE-USER"
        user_name = "SINGLE-USER"
        cast_hub.user_count += 1
        count = cast_hub.user_count

        client_product_name = request_data.get("client_product_name")
        if client_product_name:
            subscriber_name = f"SINGLE-USER-{client_product_name}-{count}"
        else:
            subscriber_name = f"SINGLE-USER-APP-{count}"
    # Check if topic is provided - if so, use it directly without incrementing count
    elif request_data.get("topic"):
        provided_topic = request_data.get("topic")
        # Use the provided topic without incrementing user_count
        topic = provided_topic
        user_name = provided_topic
        subscriber_name = provided_topic
        count = 0  # Not used when topic is provided, but set for response consistency
        
        # Check if client_product_name is provided for subscriber_name
        client_product_name = request_data.get("client_product_name")
        if client_product_name:
            subscriber_name = client_product_name + "-" + user_name
    else:
        # Original logic: generate topic and increment count
        # Check if client_product_Name is provided
        client_product_name = request_data.get("client_product_name")
        
        # Use default user-{count} format
        cast_hub.user_count += 1
        count = cast_hub.user_count

        user_name = f"USER-{cast_hub.user_count}"
        topic = user_name
        subscriber_name = user_name
        if client_product_name:
            subscriber_name = client_product_name + "-" + user_name
    
    response = {
        "token_type": "Bearer",
        "expires_in": 3600,
        "scope": "openid",
        "topic": topic,
        "id_token": f"mock_id_token_{count}",
        "access_token": f"mock_access_token_{count}",
        "user_name": user_name,
        "subscriber_name": subscriber_name
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
    print(f"  GET    http://{args.host}:{args.port}/api/hub/admin (admin status page)")
    print(f"  GET    http://{args.host}:{args.port}/test-client (test client page)")
    print(f"  GET    http://{args.host}:{args.port}/api/hub/{{topic}}")
    print(f"  POST   http://{args.host}:{args.port}/api/hub/")
    print(f"  POST   http://{args.host}:{args.port}/api/hub/{{topic}}")
    print(f"  DELETE http://{args.host}:{args.port}/api/hub/")
    print(f"  GET    http://{args.host}:{args.port}/topics")
    print(f"  GET    http://{args.host}:{args.port}/conference")
    print(f"  POST   http://{args.host}:{args.port}/conference")
    print(f"  DELETE http://{args.host}:{args.port}/conference")
    print(f"  GET    http://{args.host}:{args.port}/status")
    print(f"  POST   http://{args.host}:{args.port}/oauth/token")
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
