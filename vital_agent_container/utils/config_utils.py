import logging
import os

import yaml


class ConfigUtils:

    @staticmethod
    def load_config(app_home):

        with open(f"{app_home}/agent_config.yaml", "r") as config_stream:
            try:
                return yaml.safe_load(config_stream)
            except yaml.YAMLError as exc:
                logger = logging.getLogger("VitalAgentContainerLogger")
                logger.info("failed to load config file")
