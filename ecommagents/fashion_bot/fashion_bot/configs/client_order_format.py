from typing import Optional

class ClientOrderFormat:
    def __init__(self, client_id: str, order_prefix: Optional[str], validation_regex: Optional[str], display_format: Optional[str]):
        self.client_id = client_id
        self.order_prefix = order_prefix
        self.validation_regex = validation_regex
        self.display_format = display_format

    def as_tuple(self):
        return (
            self.client_id,
            self.order_prefix,
            self.validation_regex,
            self.display_format
        )
