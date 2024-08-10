import os
import uvicorn
from vital_agent_container.agent_container_app import AgentContainerApp
from vital_agent_container.handler.impl.aimp_echo_message_handler import AIMPEchoMessageHandler


def main():
    print('Test Agent Container App')

    current_file_directory = os.path.dirname(os.path.abspath(__file__))

    parent_directory = os.path.dirname(current_file_directory)

    app_home = parent_directory

    handler = AIMPEchoMessageHandler()

    agent_container_app = AgentContainerApp(handler, app_home)

    uvicorn.run(host="0.0.0.0", port=6006, app=agent_container_app)


if __name__ == "__main__":

    main()
