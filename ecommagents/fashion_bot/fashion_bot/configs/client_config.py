import json
from typing import Dict, Any

class ClientConfig:
    def __init__(self, client_id: str, config_key: str, config_value: Dict[str, Any]):
        self.client_id = client_id
        self.config_key = config_key
        self.config_value = config_value

    def as_tuple(self):
        return (
            self.client_id,
            self.config_key,
            json.dumps(self.config_value)
        )

