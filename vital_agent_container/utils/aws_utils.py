import os
import requests


class AWSUtils:

    @staticmethod
    def get_task_arn():
        metadata_uri = os.environ.get('ECS_CONTAINER_METADATA_URI_V4')
        if not metadata_uri:
            return "local-instance"
        task_metadata_url = f"{metadata_uri}/task"

        try:
            response = requests.get(task_metadata_url)
            response.raise_for_status()
            metadata = response.json()
            task_arn = metadata.get('TaskARN')
            return task_arn
        except requests.RequestException as e:
            return "local-instance"
