# JWT Enforcement Plan for WebSocket Endpoint

> **Historical design document.** The code snippets show the handler interface as it was at the
> time (`websocket`, `started_event`). Since 0.2.0, handlers receive an `AgentConnection` and the
> container owns the socket. See README "WebSocket protocol" for the current contract. The JWT
> modes and the 4001 close code are unchanged.

## Overview
This document outlines the implementation plan for adding JWT (JSON Web Token) enforcement to the WebSocket endpoint (`/ws`) in the vital-agent-container-python application.

## Current Architecture Analysis
- **WebSocket Endpoint**: `/ws` in `AgentContainerApp.websocket_endpoint()`
- **Message Processing**: Messages are JSON arrays with structured objects containing type and intent fields
- **No Current Authentication**: WebSocket connections are accepted without validation
- **Configuration**: Uses YAML-based configuration via `ConfigUtils.load_config()`

## JWT Enforcement Approaches

### Option 1: Header-Based Authentication (Recommended)
JWT token passed via WebSocket headers during connection handshake.

**Pros:**
- Standard approach for WebSocket authentication
- Token validated before connection establishment
- Clean separation of concerns

**Cons:**
- Browser-based WebSocket clients cannot send custom headers (JavaScript WebSocket API limitation)

### Option 2: Data Payload Authentication
JWT tokens included in the first message's `hasJwtJSON` property.

**Authentication Flow:**
- **Service-to-Service Transport**: `jwt_token` key (fallback when headers unavailable)
- **Upstream User Authentication**: `jwt_auth_token` key (original authenticated user/service)

**Pros:**
- Works with all WebSocket clients, including browsers
- Integrates naturally with existing AIMPMessage structure
- Supports dual authentication layers (transport + user)
- Flexible token refresh mechanisms

**Cons:**
- Connection established before authentication
- Slightly more complex message handling logic

### Option 3: Configurable Authentication (Recommended Implementation)
Support JWT enforcement via headers, data payload, or disabled - configurable per deployment.

**Token Priority:**
1. Header-based tokens for service-to-service transport authentication
2. Data payload `jwt_token` for transport when headers unavailable (browser clients)
3. Data payload `jwt_auth_token` for upstream user authentication and authorization

## Implementation Plan

### Phase 1: Core JWT Infrastructure

#### 1.1 JWT Validation Utility
Create `vital_agent_container/utils/jwt_utils.py`:
- JWT token validation and decoding
- Support for multiple JWT algorithms (RS256, HS256)
- Token expiration checking
- Custom claims validation
- Integration with common JWT libraries (PyJWT)

#### 1.2 Authentication Configuration
JWT configuration will be passed via environment variables to `AgentContainerApp.__init__()`:

**Environment Variables:**
```bash
JWT_ENABLED=true
JWT_SECRET_KEY=your-secret-key-here  # For HS256
JWT_PUBLIC_KEY_PATH=/path/to/public/key.pem  # For RS256 (local file)
JWT_JWKS_URL=https://your-auth-provider.com/.well-known/jwks.json  # For RS256 (JWKS)
JWT_ALGORITHM=RS256
JWT_ENFORCEMENT_MODE=header  # "header", "payload", or "none"
JWT_REQUIRED_CLAIMS=sub,exp,iat
JWT_CUSTOM_CLAIMS=user_id,permissions
```

**AgentContainerApp Constructor Update:**
```python
class AgentContainerApp(FastAPI):
    def __init__(self, handler, *, app_home=None, app_config=None, jwt_config=None, **kwargs):
        super().__init__(**kwargs)
        self.handler = handler
        self.app_home = app_home
        load_dotenv()

        if app_config is not None:
            self.config = app_config
        elif app_home is not None:
            self.config = ConfigUtils.load_config(app_home)
        else:
            logger.warning("No app_config or app_home provided; using empty config")
            self.config = {}
        
        # JWT configuration from environment or passed parameters
        self.jwt_config = jwt_config or self._load_jwt_config_from_env()
        
        self.message_processor = AIMPMessageProcessor()
        self.add_routes()
    
    def _load_jwt_config_from_env(self):
        return {
            'enabled': os.getenv('JWT_ENABLED', 'false').lower() == 'true',
            'secret_key': os.getenv('JWT_SECRET_KEY'),
            'public_key_path': os.getenv('JWT_PUBLIC_KEY_PATH'),
            'algorithm': os.getenv('JWT_ALGORITHM', 'RS256'),
            'enforcement_mode': os.getenv('JWT_ENFORCEMENT_MODE', 'none'),
            'required_claims': os.getenv('JWT_REQUIRED_CLAIMS', 'sub,exp,iat').split(','),
            'custom_claims': os.getenv('JWT_CUSTOM_CLAIMS', '').split(',') if os.getenv('JWT_CUSTOM_CLAIMS') else []
        }
```

### Phase 2: WebSocket Authentication Middleware

#### 2.1 Authentication Decorator/Middleware
Create `vital_agent_container/middleware/auth_middleware.py`:
- Pre-connection JWT validation for header-based auth
- Post-connection JWT validation for payload-based auth
- Authentication state management
- Error response handling

#### 2.2 WebSocket Endpoint Modifications
Update `agent_container_app.py`:

**Header-Based Authentication:**
```python
@self.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    jwt_payload = None
    
    # Try header-based authentication first (if enabled)
    if self.jwt_config.get('enforcement_mode') == 'header':
        auth_header = websocket.headers.get("authorization")
        jwt_token = websocket.headers.get("x-jwt-token")
        
        if auth_header or jwt_token:
            try:
                jwt_payload = await self.validate_jwt_token(auth_header or jwt_token)
                websocket.state.jwt_payload = jwt_payload
            except JWTValidationError as e:
                await websocket.close(code=4001, reason=f"Authentication failed: {str(e)}")
                return
    
    await websocket.accept()
    
    # If no header auth and payload auth is enabled, wait for first message
    if not jwt_payload and self.jwt_config.get('enforcement_mode') == 'payload':
        websocket.state.awaiting_auth = True
```

### Phase 3: Message Processing Integration

#### 3.1 Authentication Context
Update `AgentContainerApp.process_ws_message()` to pass JWT payload:
```python
async def process_ws_message(self, *, client: httpx.AsyncClient, websocket: WebSocket, data: str,
                             started_event: asyncio.Event):

    logger.info(f"process_ws_message: Processing: {data}")
    
    # Get JWT payload from websocket state for claim verification
    jwt_payload = getattr(websocket.state, 'jwt_payload', None)

    await self.message_processor.process_message(
        handler=self.handler,
        app_config=self.config,
        client=client,
        websocket=websocket,
        data=data,
        started_event=started_event,
        jwt_payload=jwt_payload
    )
```

Update `AIMPMessageProcessor.process_message()` signature:
```python
async def process_message(self, *, handler, app_config, client, websocket, data, started_event, jwt_payload=None):
    # Pass JWT payload to handlers for authorization decisions and claim verification
    await handler.process_message(
        app_config=app_config,
        client=client,
        websocket=websocket,
        data=data,
        started_event=started_event,
        jwt_payload=jwt_payload
    )
```

#### 3.2 Handler Interface Update
Modify `AIMPMessageHandlerInf` to accept JWT payload:
```python
async def process_message(self, *, app_config, client, websocket, data, started_event, jwt_payload=None):
    # Handlers can now access user context, permissions, and verify specific claims
    # Example: Check user permissions for specific operations
    if jwt_payload:
        user_id = jwt_payload.get('sub')
        permissions = jwt_payload.get('permissions', [])
        custom_claims = jwt_payload.get('user_id')
        
        # Verify claims as needed for the specific message processing
        if 'admin' not in permissions and self._requires_admin_access(data):
            raise UnauthorizedError("Admin access required")
    
    # Process message with JWT context
    pass
```

### Phase 4: Error Handling and Security

#### 4.1 Custom Exception Classes
Create `vital_agent_container/exceptions/auth_exceptions.py`:
- `JWTValidationError`
- `JWTExpiredError` 
- `JWTInvalidClaimsError`
- `AuthenticationRequiredError`

#### 4.2 Security Logging
Enhance logging in `agent_container_app.py`:
- Authentication attempts (success/failure)
- JWT validation errors
- Unauthorized access attempts
- Token expiration events

#### 4.3 WebSocket Close Codes
Implement standard WebSocket close codes:
- `4001`: Authentication failed
- `4002`: JWT expired
- `4003`: Invalid JWT format
- `4004`: Missing required claims

### Phase 5: Configuration and Environment

#### 5.1 Environment Variables
Add to `.env.example`:
```
JWT_SECRET_KEY=your-secret-key-here
JWT_PUBLIC_KEY_PATH=/path/to/public/key.pem
JWT_ALGORITHM=RS256
JWT_ENABLED=true
```

#### 5.2 JWT Configuration Validation
Add validation in `AgentContainerApp.__init__()`:
- Validate JWT configuration on startup
- Check for required JWT settings when enabled
- Validate key files and algorithms
- Log JWT configuration status

### Phase 6: Testing and Documentation

#### 6.1 Unit Tests
Create `test/test_jwt_auth.py`:
- JWT validation logic tests
- WebSocket authentication flow tests
- Error handling tests
- Configuration tests

#### 6.2 Integration Tests
Update `test/test_app.py`:
- End-to-end WebSocket authentication tests
- Message processing with JWT context tests
- Authentication failure scenarios

**Data Payload Authentication (First Message):**
```python
# In the message processing loop, check for JWT in first message
if hasattr(websocket.state, 'awaiting_auth') and websocket.state.awaiting_auth:
    jwt_payload = await self.extract_jwt_from_message_payload(data)
    if jwt_payload:
        websocket.state.jwt_payload = jwt_payload
        websocket.state.awaiting_auth = False
    else:
        await websocket.send_text(json.dumps({
            "error": "Authentication required in first message",
            "code": 4001
        }))
        await websocket.close(code=4001)
        return

async def extract_jwt_from_message_payload(self, data: str):
    """Extract JWT tokens from hasJwtJSON property in first message"""
    try:
        message_obj = json.loads(data)
        if isinstance(message_obj, list) and len(message_obj) > 0:
            first_message = message_obj[0]
            jwt_json_str = first_message.get("http://vital.ai/ontology/vital-aimp#hasJwtJSON")
            if jwt_json_str:
                jwt_data = json.loads(jwt_json_str)
                
                # Check for service-to-service transport token (fallback when headers unavailable)
                jwt_token = jwt_data.get("jwt_token")
                if jwt_token:
                    logger.info(f"JWT token: {jwt_token}")
                    transport_payload = await self.validate_jwt_token(jwt_token)
                
                # Check for upstream authenticated user/service token  
                jwt_auth_token = jwt_data.get("jwt_auth_token")
                if jwt_auth_token:
                    logger.info(f"JWT auth token: {jwt_auth_token}")
                    auth_payload = await self.validate_jwt_token(jwt_auth_token)
                
                # Return both payloads for processing
                return {
                    'jwt_token': transport_payload,     # Service transport auth
                    'jwt_auth_token': auth_payload      # User/upstream auth
                }
        return None
    except (json.JSONDecodeError, KeyError, Exception) as e:
        logger.error(f"Error extracting JWT from message payload: {e}")
        return None
```

## Message Format Extensions

### JWT in Data Payload (hasJwtJSON Property)
```json
[
  {
    "http://vital.ai/ontology/vital-aimp#hasJwtJSON": "{\"jwt_token\":\"eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9...\",\"jwt_auth_token\":\"eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9...\"}"
  }
]
```

**JWT Token Types in hasJwtJSON:**

- **`jwt_token`**: Service-to-service transport JWT token
  - Used when headers are not available (e.g., browser-to-service connections)
  - Normally would be in the Authorization header for service-to-service requests
  - Validates the transport layer authentication

- **`jwt_auth_token`**: Upstream authenticated user/service JWT token
  - Represents the original authenticated user, service, or agent that initiated the request
  - Contains permissions and claims that dictate what the agent can do
  - Determines access to specific tools and operations
  - May be passed through multiple service hops while maintaining original authentication context


## Security Considerations

1. **Token Storage**: JWT tokens should not be logged or stored in plain text
2. **Key Management**: Use secure key storage for JWT signing keys
3. **Token Expiration**: Implement reasonable token expiration times
4. **Rate Limiting**: Consider implementing rate limiting for authentication attempts
5. **CORS**: Ensure proper CORS configuration for WebSocket connections
6. **TLS**: Enforce TLS/WSS connections in production

## Deployment Considerations

1. **Monitoring**: Add metrics for authentication success/failure rates
2. **Documentation**: Update API documentation with authentication requirements

## Dependencies

Add to `pyproject.toml`:
```toml
pyjwt = "^2.8.0"
cryptography = "^41.0.0"  # For RS256 support
```

## Implementation Priority

1. **High Priority**: JWT validation utility, configurable authentication (header + payload)
2. **Medium Priority**: Configuration-based enforcement modes, error handling
3. **Low Priority**: Advanced features like token refresh, detailed logging

This plan provides a comprehensive approach to adding JWT enforcement with header-based authentication.