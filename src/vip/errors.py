class DomainError(Exception):
    """Safe public error code; never include raw provider data or secrets."""

    def __init__(self, code: str = "operation_rejected"):
        self.code = code
        super().__init__(code)


class IntegrationUnavailable(Exception):
    """The official provider contract has not been implemented and verified."""


class GatewayError(Exception):
    """Safe transport failure; never retain a provider response or credential here."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)
