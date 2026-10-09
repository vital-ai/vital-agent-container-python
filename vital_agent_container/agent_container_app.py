import asyncio
import gc
import json
import os
import sys
import tracemalloc
import uvicorn
import logging
from logging.handlers import RotatingFileHandler
from pythonjsonlogger import jsonlogger
import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState
from vital_agent_container.tasks.task_manager_async_client import TaskManagerAsyncClient
from vital_agent_container.utils.aws_utils import AWSUtils
from vital_agent_container.utils.config_utils import ConfigUtils
from vital_agent_container.utils.jwt_utils import JWTUtils, JWTValidationError, JWTExpiredError, JWTInvalidClaimsError
from vital_agent_container.processor.aimp_message_processor import AIMPMessageProcessor


logger = logging.getLogger("VitalAgentContainerLogger")
logger.setLevel(logging.INFO)

formatter = jsonlogger.JsonFormatter('%(asctime)s %(name)s %(levelname)s %(message)s')

stream_handler = logging.StreamHandler(sys.stdout)
stream_handler.setFormatter(formatter)

# log_file = "/var/log/agentcontainer/app.log"

log_file = "app.log"

file_handler = RotatingFileHandler(log_file, maxBytes=5 * 1024 * 1024, backupCount=5)
file_handler.setFormatter(formatter)

logger.addHandler(stream_handler)
logger.addHandler(file_handler)

service_identifier = AWSUtils.get_task_arn()

# How long a handler task that closed the socket gets to finish before cleanup cancels it
SERVER_CLOSE_GRACE_SECONDS = 5.0


class _ConnectionRejected(Exception):
    """Raised by a receive loop's JWT step to end the connection with a specific close code."""

    def __init__(self, code: int, reason: str):
        super().__init__(reason)
        self.code = code
        self.reason = reason


class AgentContainerApp(FastAPI):
    def __init__(self, handler, *, app_home=None, app_config=None, jwt_config=None,
                 ws_idle_timeout=None, ws_task_timeout=None, **kwargs):
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
        
        # Validate JWT configuration if enabled
        if self.jwt_config.get('enabled', False):
            try:
                JWTUtils.validate_jwt_config(self.jwt_config)
                logger.info(f"JWT enforcement enabled: mode={self.jwt_config.get('enforcement_mode', 'none')}")
            except ValueError as e:
                logger.error(f"Invalid JWT configuration: {e}")
                raise
        else:
            logger.info("JWT enforcement disabled")

        # WebSocket lifecycle timeouts (seconds); None means disabled
        self.ws_idle_timeout = self._resolve_timeout(ws_idle_timeout, 'WS_IDLE_TIMEOUT_SECONDS', 300)
        self.ws_task_timeout = self._resolve_timeout(ws_task_timeout, 'WS_TASK_TIMEOUT_SECONDS', 600)
        logger.info(f"WebSocket timeouts: idle={self.ws_idle_timeout}s, task={self.ws_task_timeout}s")

        self.message_processor = AIMPMessageProcessor()
        self.add_routes()
    
    @staticmethod
    def _resolve_timeout(value, env_var: str, default: float):
        """Explicit value, else env var, else default. 0 or empty disables (returns None)."""
        if value is None:
            raw = os.getenv(env_var)
            if raw is None:
                value = default
            elif not raw.strip():
                value = 0
            else:
                try:
                    value = float(raw)
                except ValueError:
                    raise ValueError(f"{env_var} must be a number of seconds, got {raw!r}") from None
        value = float(value)
        return value if value > 0 else None

    @staticmethod
    def _ws_open(websocket: WebSocket) -> bool:
        """True only if neither side has closed, i.e. it is safe to send or close."""
        return (websocket.application_state == WebSocketState.CONNECTED
                and websocket.client_state == WebSocketState.CONNECTED)

    def _load_jwt_config_from_env(self):
        """Load JWT configuration from environment variables"""
        return {
            'enabled': os.getenv('JWT_ENABLED', 'false').lower() == 'true',
            'secret_key': os.getenv('JWT_SECRET_KEY'),
            'public_key_path': os.getenv('JWT_PUBLIC_KEY_PATH'),
            'jwks_url': os.getenv('JWT_JWKS_URL'),
            'algorithm': os.getenv('JWT_ALGORITHM', 'RS256'),
            'enforcement_mode': os.getenv('JWT_ENFORCEMENT_MODE', 'none'),
            'audience': os.getenv('JWT_AUDIENCE'),
            'required_claims': os.getenv('JWT_REQUIRED_CLAIMS', 'sub,exp,iat').split(','),
            'custom_claims': os.getenv('JWT_CUSTOM_CLAIMS', '').split(',') if os.getenv('JWT_CUSTOM_CLAIMS') else []
        }

    async def validate_jwt_token(self, token: str):
        """Validate JWT token using JWTUtils"""
        if not token:
            return None
        return await JWTUtils.validate_jwt_token(token, self.jwt_config)
    
    async def extract_jwt_from_message_payload(self, data: str):
        """Extract JWT tokens from hasJwtJSON property in first message"""
        try:
            message_obj = json.loads(data)
            if isinstance(message_obj, list) and len(message_obj) > 0:
                first_message = message_obj[0]
                jwt_json_str = first_message.get("http://vital.ai/ontology/vital-aimp#hasJwtJSON")
                if jwt_json_str:
                    jwt_data = json.loads(jwt_json_str)
                    
                    jwt_payloads = {}
                    jwt_tokens = {}
                    
                    # Check for service-to-service transport token
                    jwt_token = jwt_data.get("jwt_token")
                    if jwt_token:
                        logger.info("Found jwt_token in message payload")
                        jwt_payloads['jwt_token'] = await self.validate_jwt_token(jwt_token)
                        jwt_tokens['jwt_token'] = jwt_token
                    
                    # Check for upstream authenticated user/service token  
                    jwt_auth_token = jwt_data.get("jwt_auth_token")
                    if jwt_auth_token:
                        logger.info("Found jwt_auth_token in message payload")
                        jwt_payloads['jwt_auth_token'] = await self.validate_jwt_token(jwt_auth_token)
                        jwt_tokens['jwt_auth_token'] = jwt_auth_token
                    
                    return (jwt_payloads, jwt_tokens) if jwt_payloads else (None, None)
            return (None, None)
        except (json.JSONDecodeError, KeyError, Exception) as e:
            logger.error(f"Error extracting JWT from message payload: {e}")
            return (None, None)

    async def process_ws_message(self, *, websocket: WebSocket, data: str, started_event: asyncio.Event = None, jwt_payload=None, jwt_token=None, client_info: str = "unknown"):

        logger.info(f"process_ws_message: Processing: {data}")

        # Use provided JWT payload or get from websocket state
        if jwt_payload is None:
            jwt_payload = getattr(websocket.state, 'jwt_payload', None)

        # Use provided JWT token or get from websocket state
        if jwt_token is None:
            jwt_token = getattr(websocket.state, 'jwt_token', None)

        # Create client; use the caller-provided started_event so the awaiting
        # coroutine in _process_message_with_intent_handling is the one the handler sets.
        client = TaskManagerAsyncClient()
        if started_event is None:
            started_event = asyncio.Event()

        # Backstop deadline for the whole handler run. Handlers should keep their own,
        # tighter deadlines so they can send a proper error response.
        deadline = asyncio.timeout(self.ws_task_timeout)
        try:
            try:
                async with deadline:
                    await self.message_processor.process_message(
                        handler=self.handler,
                        app_config=self.config,
                        client=client,
                        websocket=websocket,
                        data=data,
                        started_event=started_event,
                        jwt_payload=jwt_payload,
                        jwt_token=jwt_token
                    )
            except TimeoutError:
                # A TimeoutError from the handler's own deadline is not ours to swallow
                if not deadline.expired():
                    raise
            # Checked outside the except: a handler that swallows the CancelledError
            # exits the block normally, but the deadline still fired
            if deadline.expired():
                logger.error(f"Handler exceeded {self.ws_task_timeout}s deadline for {client_info}; cancelled")
                await client.cancel_current_tasks()
                if self._ws_open(websocket):
                    try:
                        await websocket.close(code=1011, reason="Handler timeout")
                    except Exception as e:
                        logger.warning(f"Error closing WebSocket after handler timeout for {client_info}: {e}")
        finally:
            # Never leave the receive loop waiting on a handler that didn't signal
            started_event.set()

    def add_routes(self):
        @self.on_event("shutdown")
        async def shutdown_event():
            logger.info("Shutting down...")

        @self.get("/health")
        async def health_check():
            logger.info("health check")
            return {"status": "ok"}

        @self.websocket("/ws")
        async def websocket_endpoint(websocket: WebSocket):
            client_info = f"{websocket.client.host}:{websocket.client.port}" if websocket.client else "unknown"
            logger.info(f"WebSocket connection attempt from {client_info}")
            
            jwt_enforcement_enabled = self.jwt_config.get('enabled', False)
            enforcement_mode = self.jwt_config.get('enforcement_mode', 'none')
            
            try:
                if not jwt_enforcement_enabled:
                    await self._handle_jwt_disabled_websocket(websocket, client_info)
                elif enforcement_mode == 'header':
                    await self._handle_jwt_header_mode_websocket(websocket, client_info)
                elif enforcement_mode == 'payload':
                    await self._handle_jwt_payload_mode_websocket(websocket, client_info)
                else:
                    logger.error(f"Invalid JWT enforcement mode '{enforcement_mode}' for {client_info}")
                    await websocket.close(code=1011, reason="Invalid configuration")
                    
            except Exception as e:
                logger.error(f"WebSocket endpoint error for {client_info}: {str(e)}")
                try:
                    await websocket.close(code=1011, reason="Internal server error")
                except:
                    pass  # Connection might already be closed

    async def _handle_jwt_disabled_websocket(self, websocket: WebSocket, client_info: str):
        """Handle WebSocket connection with JWT enforcement disabled"""
        logger.info(f"JWT enforcement disabled, accepting connection from {client_info}")
        await websocket.accept()
        logger.info(f"WebSocket connection accepted for {client_info}")

        async def get_jwt(data):
            # Process message without JWT validation
            return None, None

        await self._run_receive_loop(websocket, client_info, get_jwt)

    async def _handle_jwt_header_mode_websocket(self, websocket: WebSocket, client_info: str):
        """Handle WebSocket connection with JWT header mode enforcement"""
        logger.info(f"JWT header mode enabled for {client_info}")
        
        # Extract and validate JWT from headers BEFORE accepting
        auth_header = websocket.headers.get("authorization")
        jwt_token_header = websocket.headers.get("x-jwt-token")
        
        if not (auth_header or jwt_token_header):
            logger.warning(f"JWT required but not provided in headers for {client_info}")
            await websocket.close(code=4001, reason="JWT token required")
            return
        
        token = auth_header or jwt_token_header
        # Clean the token (remove Bearer prefix if present)
        if token.startswith('Bearer '):
            clean_token = token[7:]
        else:
            clean_token = token
            
        try:
            header_jwt_payload = await self.validate_jwt_token(token)
            if not header_jwt_payload:
                logger.warning(f"Header JWT validation failed for {client_info}")
                await websocket.close(code=4001, reason="Invalid JWT token")
                return
            
            user_id = header_jwt_payload.get('sub', 'unknown')
            logger.info(f"Header JWT authentication successful for user {user_id} from {client_info}")
            
        except Exception as e:
            logger.error(f"Header JWT validation error for {client_info}: {str(e)}")
            await websocket.close(code=4001, reason="JWT validation failed")
            return
        
        # Accept connection after successful header validation
        await websocket.accept()
        logger.info(f"WebSocket connection accepted for {client_info}")
        websocket.state.jwt_payload = header_jwt_payload
        websocket.state.jwt_token = clean_token

        async def get_jwt(data):
            # Process message with header JWT payload
            return header_jwt_payload, clean_token

        await self._run_receive_loop(websocket, client_info, get_jwt)

    async def _handle_jwt_payload_mode_websocket(self, websocket: WebSocket, client_info: str):
        """Handle WebSocket connection with JWT payload mode enforcement"""
        logger.info(f"JWT payload mode enabled for {client_info}")
        
        # Accept connection immediately for payload mode
        await websocket.accept()
        logger.info(f"WebSocket connection accepted for {client_info}")

        async def get_jwt(data):
            # Extract and validate JWT from message payload
            try:
                message_jwt_payload, message_jwt_tokens = await self.extract_jwt_from_message_payload(data)
            except Exception as e:
                logger.error(f"Payload JWT extraction error for {client_info}: {str(e)}")
                raise _ConnectionRejected(4001, "JWT validation failed")

            if not message_jwt_payload:
                logger.warning(f"No JWT found in message payload for {client_info}")
                raise _ConnectionRejected(4001, "JWT token required in message payload")

            # Log authentication info based on JWT format
            if isinstance(message_jwt_payload, dict) and ('jwt_token' in message_jwt_payload or 'jwt_auth_token' in message_jwt_payload):
                transport_jwt = message_jwt_payload.get('jwt_token', {})
                auth_jwt = message_jwt_payload.get('jwt_auth_token', {})
                transport_user = transport_jwt.get('sub', 'unknown') if transport_jwt else 'none'
                auth_user = auth_jwt.get('sub', 'unknown') if auth_jwt else 'none'
                logger.info(f"Payload JWT authentication successful - Transport: {transport_user}, Auth: {auth_user} from {client_info}")
            else:
                user_id = message_jwt_payload.get('sub', 'unknown')
                logger.info(f"Payload JWT authentication successful for user {user_id} from {client_info}")

            websocket.state.jwt_payload = message_jwt_payload
            websocket.state.jwt_tokens = message_jwt_tokens
            return message_jwt_payload, message_jwt_tokens

        await self._run_receive_loop(websocket, client_info, get_jwt)

    async def _run_receive_loop(self, websocket: WebSocket, client_info: str, get_jwt):
        """Receive loop shared by all JWT modes; owns the connection's lifecycle.

        get_jwt(data) returns (jwt_payload, jwt_token) for a message, or raises
        _ConnectionRejected to end the connection with that close code.
        """
        client = TaskManagerAsyncClient()
        background_tasks = []
        message_count = 0
        # Close used by cleanup if the socket is still open when the loop ends
        close_code, close_reason = 1011, "Internal server error"
        closed_by_server = False

        try:
            # Exits once either side closes; a handler closing the socket is a normal end
            while websocket.application_state == WebSocketState.CONNECTED:
                try:
                    data = await asyncio.wait_for(websocket.receive_text(), timeout=self.ws_idle_timeout)
                except TimeoutError:
                    background_tasks[:] = self._prune_tasks(background_tasks, client_info)
                    if background_tasks:
                        continue  # work in flight: not idle (bounded by the task deadline)
                    logger.info(f"Idle timeout for {client_info} after {message_count} messages")
                    close_code, close_reason = 1000, "Idle timeout"
                    break

                background_tasks[:] = self._prune_tasks(background_tasks, client_info)
                message_count += 1
                logger.debug(f"Received message #{message_count} from {client_info}")

                try:
                    jwt_payload, jwt_token = await get_jwt(data)
                except _ConnectionRejected as rejected:
                    close_code, close_reason = rejected.code, rejected.reason
                    break

                await self._process_message_with_intent_handling(websocket, data, client, background_tasks, client_info, message_count, jwt_payload, jwt_token)
            else:
                closed_by_server = True
                logger.info(f"WebSocket closed by server for {client_info} after {message_count} messages")

        except WebSocketDisconnect:
            logger.info(f"WebSocket disconnected normally for {client_info} after {message_count} messages")
        except RuntimeError as e:
            # Starlette raises RuntimeError (not WebSocketDisconnect) when reading a socket
            # that our side already closed, e.g. a handler closing between our check and read
            if websocket.application_state == WebSocketState.DISCONNECTED:
                closed_by_server = True
                logger.info(f"WebSocket closed by server for {client_info} after {message_count} messages")
            else:
                logger.error(f"WebSocket communication error for {client_info}: {str(e)}")
        except asyncio.CancelledError:
            close_code, close_reason = 1001, "Server shutting down"
            raise
        except Exception as e:
            logger.error(f"WebSocket communication error for {client_info}: {str(e)}")
        finally:
            try:
                if closed_by_server:
                    await self._let_closing_tasks_finish(background_tasks)
            finally:
                # Runs even if the server is cancelled during the grace wait
                await self._cleanup_websocket_resources(client, background_tasks, client_info,
                                                        websocket=websocket, close_code=close_code, close_reason=close_reason)

    async def _let_closing_tasks_finish(self, background_tasks: list):
        """Give a task that closed the socket time to finish before cleanup cancels it.

        Starlette marks the socket DISCONNECTED before the close frame is actually sent, so the
        loop can see the close while the closing task is still sending it. Cancelling the task
        then would drop the close frame.
        """
        pending = [task for task in background_tasks if not task.done()]
        if pending:
            await asyncio.wait(pending, timeout=SERVER_CLOSE_GRACE_SECONDS)

    def _prune_tasks(self, background_tasks: list, client_info: str) -> list:
        """Return the unfinished tasks, dropping finished ones so they release their websocket/payload."""
        live = []
        for task in background_tasks:
            if not task.done():
                live.append(task)
            elif not task.cancelled() and task.exception() is not None:
                # Retrieving the exception also avoids asyncio's "never retrieved" warning
                logger.error(f"Handler task failed for {client_info}: {task.exception()!r}")
        return live

    async def _process_message_with_intent_handling(self, websocket: WebSocket, data: str, client, background_tasks: list, client_info: str, message_count: int, jwt_payload, jwt_token=None):
        """Handle message processing with interrupt intent logic"""
        try:
            message_obj = json.loads(data)
            message_intent = message_obj[0].get("http://vital.ai/ontology/vital-aimp#hasIntent", None)
            
            if message_intent == "interrupt":
                logger.info(f"Processing interrupted by client {client_info}")
                client.log_current_tasks()
                await client.cancel_current_tasks()
                for task in background_tasks:
                    task.cancel()
                try:
                    await asyncio.gather(*background_tasks, return_exceptions=True)
                except Exception as e:
                    logger.error(f"Error in interrupt gather for {client_info}: {e}")
                try:
                    if client.ws_active and self._ws_open(websocket):
                        await websocket.close()
                        client._ws_active = False
                except Exception as e:
                    logger.error(f"Error in interrupt websocket close for {client_info}: {e}")
                return
            
            if any(not task.done() for task in background_tasks):
                logger.info(f"Currently processing task for {client_info}, ignoring new request")
                if self._ws_open(websocket):
                    await websocket.send_text("processing task. ignoring message.")
            else:
                logger.info(f"Processing message #{message_count} for {client_info}")
                started_event = asyncio.Event()
                task = asyncio.create_task(self.process_ws_message(websocket=websocket, data=data, started_event=started_event, jwt_payload=jwt_payload, jwt_token=jwt_token, client_info=client_info))
                background_tasks.append(task)
                # Wait until the handler signals it has started, OR the task finishes.
                # Waiting on started_event alone would hang forever (leaking the
                # connection + decoded payload) if the task errors before setting it.
                started_waiter = asyncio.ensure_future(started_event.wait())
                try:
                    await asyncio.wait({started_waiter, task}, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    started_waiter.cancel()
                logger.info(f"Handler started for message #{message_count} for {client_info}")
                return
                
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON in message from {client_info}: {str(e)}")
            if self._ws_open(websocket):
                await websocket.close(code=1003, reason="Invalid JSON")
            return
        except Exception as e:
            logger.error(f"Message processing error for {client_info}: {str(e)}")
            if self._ws_open(websocket):
                await websocket.close(code=1011, reason="Message processing failed")
            return

    async def _cleanup_websocket_resources(self, client, background_tasks: list, client_info: str,
                                           websocket: WebSocket = None, close_code: int = 1000, close_reason: str = ""):
        """Clean up WebSocket resources including TaskManagerAsyncClient and background tasks"""
        # Log failures of tasks that already finished; the gather below discards exceptions.
        # This is the only prune when the handler closed the socket (the loop exits without one).
        background_tasks[:] = self._prune_tasks(background_tasks, client_info)

        # Clean up background tasks
        if background_tasks:
            try:
                for task in background_tasks:
                    task.cancel()
                await asyncio.gather(*background_tasks, return_exceptions=True)
            except Exception as e:
                logger.error(f"Error in final gather for {client_info}: {e}")
        
        # Cancel any remaining tasks in client
        try:
            if client:
                await client.cancel_current_tasks()
        except Exception as e:
            logger.error(f"Error canceling tasks for {client_info}: {e}")

        # Close the socket if nothing else has (idle timeout, rejection, unexpected error)
        if websocket is not None and self._ws_open(websocket):
            try:
                await websocket.close(code=close_code, reason=close_reason)
            except Exception as e:
                logger.warning(f"Error closing WebSocket for {client_info}: {e}")
