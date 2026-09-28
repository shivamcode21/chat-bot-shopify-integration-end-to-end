import uuid
from datetime import datetime
from typing import Optional

class ClientData:
    def __init__(self, name: str, domain: Optional[str] = None, logo_url: Optional[str] = None, support_email: Optional[str] = None, support_number: Optional[str] = None):
        self.id = str(uuid.uuid4())
        self.name = name
        self.domain = domain
        self.logo_url = logo_url
        self.support_email = support_email
        self.support_number = support_number
        self.created_at = datetime.utcnow()

    def as_tuple(self):
        return (
            self.id,
            self.name,
            self.domain,
            self.logo_url,
            self.support_email,
            self.support_number,
            self.created_at
        )
