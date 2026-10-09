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
from dataclasses import dataclass
from typing import Optional
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from starlette.datastructures import State
from vital_agent_container.connection import AgentConnection, ConnectionClosed, _Channel
from vital_agent_container.protocol import (
    CloseCode, INTENT_PROPERTY, INTENT_INTERRUPT, INTENT_CLOSE, URI_PROPERTY,
    ERROR_BUSY, STATUS_DONE, STATUS_IDLE, STATUS_INTERRUPTED, control_error, control_status,
)
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


# How long a cancelled handler (deadline, interrupt, disconnect, shutdown) gets to unwind
# before it is abandoned
HANDLER_UNWIND_GRACE_SECONDS = 5.0

# Interrupted handlers that may still be running (unwinding or abandoned) while a connection
# starts a new one. 1 means at most two live handlers per connection.
MAX_LINGERING_HANDLERS_PER_CONNECTION = 1

# Connection state that is snapshotted into each run (written by JWT enforcement)
RUN_STATE_KEYS = ("jwt_payload", "jwt_token", "jwt_tokens")


class _ConnectionRejected(Exception):
    """Raised by a connection's JWT step to end the connection with a specific close code."""

    def __init__(self, code: int, reason: str):
        super().__init__(reason)
        self.code = code
        self.reason = reason


@dataclass
class _HandlerRun:
    """One running handler invocation."""
    task: asyncio.Task
    client: TaskManagerAsyncClient
    connection: AgentConnection
    started_at: float

    def remaining(self, timeout: Optional[float]) -> Optional[float]:
        if timeout is None:
            return None
        return max(0.0, self.started_at + timeout - asyncio.get_running_loop().time())


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

        # Cancelled handler tasks that didn't unwind in time; strong refs so they aren't GC'd mid-run
        self._abandoned_tasks = set()

        self.message_processor = AIMPMessageProcessor()
        self.add_routes()
    
    @staticmethod
    def _resolve_timeout(value, env_var: str, default: float):
        """Explicit value, else env var, else default. Only an explicit 0 disables (returns None).

        An empty env var means "use the default", so a deploy template that renders a
        missing value as empty can't silently turn a timeout off.
        """
        if value is None:
            raw = (os.getenv(env_var) or "").strip()
            if not raw:
                value = default
            else:
                try:
                    value = float(raw)
                except ValueError:
                    raise ValueError(f"{env_var} must be a number of seconds, got {raw!r}") from None
        value = float(value)
        if value < 0:
            raise ValueError(f"{env_var} must be 0 (disabled) or a positive number of seconds, got {value}")
        if value == 0:
            logger.warning(f"{env_var} is 0: this WebSocket timeout is disabled")
            return None
        return value


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
                    await websocket.close(code=CloseCode.INTERNAL_ERROR, reason="Invalid configuration")
                    
            except Exception as e:
                logger.error(f"WebSocket endpoint error for {client_info}: {str(e)}")
                try:
                    await websocket.close(code=CloseCode.INTERNAL_ERROR, reason="Internal server error")
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

        await self._run_connection(websocket, client_info, get_jwt)

    async def _handle_jwt_header_mode_websocket(self, websocket: WebSocket, client_info: str):
        """Handle WebSocket connection with JWT header mode enforcement"""
        logger.info(f"JWT header mode enabled for {client_info}")
        
        # Extract and validate JWT from headers BEFORE accepting
        auth_header = websocket.headers.get("authorization")
        jwt_token_header = websocket.headers.get("x-jwt-token")
        
        if not (auth_header or jwt_token_header):
            logger.warning(f"JWT required but not provided in headers for {client_info}")
            await websocket.close(code=CloseCode.UNAUTHORIZED, reason="JWT token required")
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
                await websocket.close(code=CloseCode.UNAUTHORIZED, reason="Invalid JWT token")
                return
            
            user_id = header_jwt_payload.get('sub', 'unknown')
            logger.info(f"Header JWT authentication successful for user {user_id} from {client_info}")
            
        except Exception as e:
            logger.error(f"Header JWT validation error for {client_info}: {str(e)}")
            await websocket.close(code=CloseCode.UNAUTHORIZED, reason="JWT validation failed")
            return
        
        # Accept connection after successful header validation
        await websocket.accept()
        logger.info(f"WebSocket connection accepted for {client_info}")
        websocket.state.jwt_payload = header_jwt_payload
        websocket.state.jwt_token = clean_token

        async def get_jwt(data):
            # Process message with header JWT payload
            return header_jwt_payload, clean_token

        await self._run_connection(websocket, client_info, get_jwt)

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
                raise _ConnectionRejected(CloseCode.UNAUTHORIZED, "JWT validation failed")

            if not message_jwt_payload:
                logger.warning(f"No JWT found in message payload for {client_info}")
                raise _ConnectionRejected(CloseCode.UNAUTHORIZED, "JWT token required in message payload")

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

        await self._run_connection(websocket, client_info, get_jwt)

    async def _run_connection(self, websocket: WebSocket, client_info: str, get_jwt):
        """Run one accepted connection (see README "WebSocket protocol").

        The loop always reads, so interrupts, close requests and busy rejections work while a
        handler runs, and it is the only place the socket is closed. Every interaction ends with
        exactly one of: an `sdk.status done`, an `sdk.status interrupted`, or a close code.
        get_jwt(data) returns (jwt_payload, jwt_token) for a message, or raises _ConnectionRejected.
        """
        channel = _Channel(websocket)
        receive_task = None
        run: Optional[_HandlerRun] = None
        unwinding = set()  # unwind tasks for interrupted runs (each bounded by the grace period)
        lingering = set()  # interrupted handler tasks not yet finished, abandoned ones included
        client_close_requested = False
        message_count = 0
        # Close used when the loop ends; overwritten by each normal way of ending
        close_code, close_reason = CloseCode.INTERNAL_ERROR, "Internal server error"

        try:
            while True:
                if receive_task is None:
                    receive_task = asyncio.ensure_future(websocket.receive_text())
                waiting = {receive_task} if run is None else {receive_task, run.task}
                # Idle timer only while no handler runs; otherwise the handler's deadline
                timeout = self.ws_idle_timeout if run is None else run.remaining(self.ws_task_timeout)
                done, _ = await asyncio.wait(waiting, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)

                if not done:
                    if run is None:
                        logger.info(f"Idle timeout for {client_info} after {message_count} messages")
                        close_code, close_reason = CloseCode.NORMAL, "Idle timeout"
                    else:
                        logger.error(f"Handler exceeded {self.ws_task_timeout}s deadline for {client_info}; cancelling")
                        close_code, close_reason = CloseCode.HANDLER_TIMEOUT, "Handler timeout"
                        # Close before cancelling, so the client sees 4008 whatever the handler's
                        # cleanup does; bounded even if a handler send is stuck
                        run.connection._end()
                        await channel.close(close_code, close_reason)
                    break

                if run is not None and run.task in done:
                    finished, run = run, None
                    error = await self._finish_run(finished, client_info)
                    if error is not None:
                        if isinstance(error, ConnectionClosed) or not channel.is_open:
                            logger.info(f"Handler for {client_info} stopped: connection has ended")
                        else:
                            logger.error(f"Handler failed for {client_info}: {error!r}")
                            close_code, close_reason = CloseCode.INTERNAL_ERROR, "Handler error"
                        break
                    if finished.connection.close_requested:
                        close_code, close_reason = CloseCode.NORMAL, "Closed by service"
                        break
                    if client_close_requested:
                        close_code, close_reason = CloseCode.NORMAL, "Closed by client request"
                        break
                    await self._send_control(channel, control_status(STATUS_DONE, finished.connection.request_uri))

                if receive_task in done:
                    finished_receive, receive_task = receive_task, None
                    data = finished_receive.result()  # raises WebSocketDisconnect when the client leaves
                    message_count += 1
                    logger.debug(f"Received message #{message_count} from {client_info}")

                    # JWT first, as before contract v2: in payload mode every message, whatever it
                    # contains, must carry a valid JWT
                    jwt_payload, jwt_token = await get_jwt(data)

                    try:
                        first = json.loads(data)[0]
                        intent = first.get(INTENT_PROPERTY)
                        uri = first.get(URI_PROPERTY)
                    except json.JSONDecodeError:
                        logger.warning(f"Invalid JSON from {client_info}")
                        close_code, close_reason = CloseCode.UNSUPPORTED_DATA, "Invalid JSON"
                        break
                    except (TypeError, KeyError, IndexError, AttributeError):
                        logger.warning(f"Message from {client_info} is not an AIMP message list")
                        close_code, close_reason = CloseCode.UNSUPPORTED_DATA, "Invalid message"
                        break

                    if intent == INTENT_INTERRUPT:
                        if run is None:
                            await self._send_control(channel, control_status(STATUS_IDLE, uri))
                        else:
                            logger.info(f"Interrupt from {client_info}; cancelling handler")
                            interrupted, run = run, None
                            # The interrupted run can no longer send, so it may unwind in the
                            # background while the loop keeps reading
                            interrupted.connection._end()
                            lingering.add(interrupted.task)
                            interrupted.task.add_done_callback(lingering.discard)
                            unwind = asyncio.create_task(self._cancel_run(interrupted, client_info))
                            unwinding.add(unwind)
                            unwind.add_done_callback(unwinding.discard)
                            await self._send_control(
                                channel, control_status(STATUS_INTERRUPTED, interrupted.connection.request_uri))
                    elif intent == INTENT_CLOSE:
                        if run is None:
                            close_code, close_reason = CloseCode.NORMAL, "Closed by client request"
                            break
                        logger.info(f"Close requested by {client_info}; closing after the running handler")
                        client_close_requested = True
                    elif run is not None:
                        logger.info(f"Busy: rejecting message #{message_count} from {client_info}")
                        await self._send_control(
                            channel, control_error(ERROR_BUSY, "a message is already being processed", uri))
                    elif len(lingering) > MAX_LINGERING_HANDLERS_PER_CONNECTION:
                        # Interrupt-then-resend may overlap one cancelled handler, not any number
                        logger.info(f"Busy: rejecting message #{message_count} from {client_info}; "
                                    f"{len(lingering)} interrupted handlers still running")
                        await self._send_control(
                            channel, control_error(ERROR_BUSY, "previous requests are still being cancelled", uri))
                    else:
                        logger.info(f"Processing message #{message_count} for {client_info}")
                        run = self._start_run(channel, websocket, client_info, data, uri, jwt_payload, jwt_token)

        except (WebSocketDisconnect, ConnectionClosed):
            logger.info(f"Client {client_info} disconnected after {message_count} messages")
        except _ConnectionRejected as rejected:
            close_code, close_reason = rejected.code, rejected.reason
        except asyncio.CancelledError:
            close_code, close_reason = CloseCode.GOING_AWAY, "Server shutting down"
            raise
        except Exception as e:
            logger.error(f"WebSocket communication error for {client_info}: {e!r}")
        finally:
            self._discard_receive(receive_task)
            try:
                if run is not None:
                    await self._cancel_run(run, client_info)
                if unwinding:
                    await asyncio.wait(set(unwinding))  # each is bounded by the unwind grace
            finally:
                if channel.is_open:
                    logger.info(f"Closing connection for {client_info}: {int(close_code)} {close_reason}")
                await channel.close(close_code, close_reason)

    @staticmethod
    async def _send_control(channel: _Channel, message: dict):
        await channel.send_text(json.dumps(message))

    def _start_run(self, channel: _Channel, websocket: WebSocket, client_info: str, data: str,
                   request_uri, jwt_payload, jwt_token) -> _HandlerRun:
        # Snapshot connection state for this run, so later messages (busy, interrupt, close)
        # can't change what a running handler sees
        state = State({key: getattr(websocket.state, key) for key in RUN_STATE_KEYS
                       if hasattr(websocket.state, key)})
        connection = AgentConnection(channel, client_info, state=state, request_uri=request_uri)
        client = TaskManagerAsyncClient()
        task = asyncio.create_task(self.message_processor.process_message(
            handler=self.handler,
            app_config=self.config,
            client=client,
            connection=connection,
            data=data,
            jwt_payload=jwt_payload,
            jwt_token=jwt_token
        ))
        return _HandlerRun(task=task, client=client, connection=connection,
                           started_at=asyncio.get_running_loop().time())

    async def _finish_run(self, run: _HandlerRun, client_info: str) -> Optional[BaseException]:
        """Release a finished run's resources; return the exception it raised, if any."""
        run.connection._end()
        await self._close_client(run.client, client_info)
        return None if run.task.cancelled() else run.task.exception()

    async def _cancel_run(self, run: _HandlerRun, client_info: str):
        """Cancel a handler and give it HANDLER_UNWIND_GRACE_SECONDS to unwind, then abandon it."""
        run.connection._end()
        run.task.cancel()
        try:
            await run.client.cancel_current_tasks()
        except Exception as e:
            logger.error(f"Error canceling client tasks for {client_info}: {e!r}")
        try:
            await asyncio.wait({run.task}, timeout=HANDLER_UNWIND_GRACE_SECONDS)
        finally:
            if run.task.done():
                self._retrieve_exception(run.task)
                await self._close_client(run.client, client_info)
            else:
                self._abandoned_tasks.add(run.task)
                logger.error(f"Cancelled handler for {client_info} did not finish within "
                             f"{HANDLER_UNWIND_GRACE_SECONDS}s; abandoning it "
                             f"({len(self._abandoned_tasks)} abandoned handlers in this process)")
                run.task.add_done_callback(self._forget_abandoned_task)

    @staticmethod
    async def _close_client(client: TaskManagerAsyncClient, client_info: str):
        try:
            await client.aclose()
        except Exception as e:
            logger.warning(f"Error closing HTTP client for {client_info}: {e!r}")

    def _forget_abandoned_task(self, task: asyncio.Task):
        self._abandoned_tasks.discard(task)
        self._retrieve_exception(task)

    @staticmethod
    def _retrieve_exception(task: asyncio.Task):
        """What a cancelled handler raises while unwinding is not a new failure; retrieve it
        so asyncio doesn't log "exception was never retrieved"."""
        if not task.cancelled():
            task.exception()

    @staticmethod
    def _discard_receive(receive_task):
        if receive_task is None:
            return
        if receive_task.done():
            if not receive_task.cancelled():
                receive_task.exception()
        else:
            receive_task.cancel()
