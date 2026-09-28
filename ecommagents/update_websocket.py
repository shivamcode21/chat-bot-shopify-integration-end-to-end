"""Script to update websocket_chat.py for encoded client_id support."""

# Read file
with open('fashion_bot/fashion_bot/websocket_chat.py', 'r') as f:
    content = f.read()

# Old endpoint code
old_endpoint = '''@websocket_router.websocket("/ws/chat/{client_name}/{session_id}")
async def websocket_chat_endpoint(websocket: WebSocket, client_name: str, session_id: str):
    """
    WebSocket endpoint for web chat widget.
    
    Flow:
    1. Accept WebSocket connection
    2. Resolve client_name → client_id (from database cache)
    3. Cache client_id in session object for entire connection lifetime
    4. All messages in this session use the cached client_id (no re-lookup)
    5. Cache persists until WebSocket disconnects
    
    Args:
        client_name: Human-readable client name (e.g., 'Groovee') - resolved to UUID from clients table
        session_id: Unique session identifier from widget
    """
    await websocket.accept()
    
    # Track WebSocket connection
    metrics_collector = get_metrics_collector()
    metrics_collector.track_connection(session_id)
    
    # Step 1: Resolve client name to UUID from database (cached, refreshed every 10 min)
    client_id = await aresolve_client_name_to_id(client_name)
    
    if not client_id:
        logger.error(f"❌ Invalid client name: {client_name}")
        await websocket.send_json({
            "type": "error",
            "message": f"Invalid client: {client_name}. Please contact support.",
            "timestamp": datetime.utcnow().isoformat()
        })
        await websocket.close(code=1008, reason=f"Invalid client name: {client_name}")
        return
    
    logger.info(f"🔌 WebSocket connected: client_name={client_name}, client_id={client_id[:8]}..., session={session_id[:8]}...")'''

# New endpoint code with support for encoded client_id
new_endpoint = '''@websocket_router.websocket("/ws/chat/{client_identifier}/{session_id}")
async def websocket_chat_endpoint(websocket: WebSocket, client_identifier: str, session_id: str):
    """
    WebSocket endpoint for web chat widget.
    
    Supports TWO identifier formats:
    1. Encoded client_id (recommended): base64-encoded client_id (from widget using encodedClientId)
    2. Client name (legacy): human-readable client name (resolved via database lookup)
    
    Flow:
    1. Accept WebSocket connection
    2. Detect identifier format and resolve to client_id:
       - If encoded client_id: decode directly (no DB lookup)
       - If client name: resolve via database cache
    3. Cache client_id in session object for entire connection lifetime
    4. All messages in this session use the cached client_id (no re-lookup)
    5. Cache persists until WebSocket disconnects
    
    Args:
        client_identifier: Either base64-encoded client_id OR client_name (auto-detected)
        session_id: Unique session identifier from widget
    """
    await websocket.accept()
    
    # Track WebSocket connection
    metrics_collector = get_metrics_collector()
    metrics_collector.track_connection(session_id)
    
    # Step 1: Resolve client_identifier to client_id
    # Supports encoded client_id (direct decode, no DB lookup) OR client_name (DB lookup)
    if is_encoded_client_id(client_identifier):
        try:
            client_id = decode_client_id(client_identifier)
            logger.info(f"🔌 WebSocket: decoded client_id from encoded identifier, session={session_id[:8]}...")
        except ValueError as e:
            logger.error(f"❌ Invalid encoded client_id: {client_identifier[:20]}...")
            await websocket.send_json({
                "type": "error",
                "message": "Invalid client identifier. Please contact support.",
                "timestamp": datetime.utcnow().isoformat()
            })
            await websocket.close(code=1008, reason="Invalid client identifier")
            return
    else:
        # Legacy: resolve client_name to client_id from database cache
        client_id = await aresolve_client_name_to_id(client_identifier)
        if not client_id:
            logger.error(f"❌ Invalid client name: {client_identifier}")
            await websocket.send_json({
                "type": "error",
                "message": f"Invalid client: {client_identifier}. Please contact support.",
                "timestamp": datetime.utcnow().isoformat()
            })
            await websocket.close(code=1008, reason=f"Invalid client name: {client_identifier}")
            return
        logger.info(f"🔌 WebSocket: resolved client_name={client_identifier} -> client_id, session={session_id[:8]}...")
    
    logger.info(f"🔌 WebSocket connected: client_id={client_id[:8]}..., session={session_id[:8]}...")'''

if old_endpoint in content:
    content = content.replace(old_endpoint, new_endpoint)
    with open('fashion_bot/fashion_bot/websocket_chat.py', 'w') as f:
        f.write(content)
    print('Endpoint updated successfully')
else:
    print('Pattern not found - checking content...')
    # Find what's actually there
    idx = content.find('@websocket_router.websocket')
    if idx >= 0:
        print('Found @websocket_router.websocket at index', idx)
        print('Content around it:')
        print(content[idx:idx+800])
    else:
        print('Could not find @websocket_router.websocket')