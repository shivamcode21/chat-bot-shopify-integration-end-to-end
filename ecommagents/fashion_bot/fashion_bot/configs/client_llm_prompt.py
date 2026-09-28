class ClientLLMPrompt:
    def __init__(self, client_id: str, prompt_type: str, content: str):
        self.client_id = client_id
        self.prompt_type = prompt_type
        self.content = content

    def as_tuple(self):
        return (
            self.client_id,
            self.prompt_type,
            self.content
        )
