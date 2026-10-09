from typing import Optional, Dict, Any
import httpx
from abc import ABC, abstractmethod
from vital_agent_container.connection import AgentConnection


class AIMPMessageHandlerInf(ABC):

    @abstractmethod
    async def process_message(self, *, app_config: Dict[str, Any], client: httpx.AsyncClient, connection: AgentConnection, data: str, jwt_payload: Optional[Dict[str, Any]] = None, jwt_token: Optional[str] = None):
        """Handle one client message. The run is over when this returns.

        Send replies with connection.send_text / send_json. Call connection.request_close()
        to end the connection after this run; never close the socket directly.
        """
        pass
