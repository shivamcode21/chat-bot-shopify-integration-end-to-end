class ClientIntent:
    def __init__(self, client_id: str, intent_key: str, enabled: bool = True):
        self.client_id = client_id
        self.intent_key = intent_key
        self.enabled = enabled

    def as_tuple(self):
        return (
            self.client_id,
            self.intent_key,
            self.enabled
        )
