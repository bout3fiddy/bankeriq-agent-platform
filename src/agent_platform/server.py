"""Run an application endpoint with trusted authentication and explicit bind settings."""

from dataclasses import dataclass

from agent_runtime.hosting.endpoint import RuntimeEndpoint
from agent_runtime.providers.hosts import Authenticator


@dataclass
class LocalServer:
    """Default to loopback; container startup explicitly selects all interfaces."""

    endpoint: RuntimeEndpoint
    authenticate: Authenticator
    port: int
    host: str = "127.0.0.1"

    def run(self) -> None:

        # Application composition supplies the endpoint and authenticator. For
        # example, LocalServer(endpoint, authenticate, 8088) binds loopback; the
        # validation container explicitly requests 0.0.0.0. Provider import stays
        # here so configuring a server does not initialise an SDK or open a socket.
        from agent_runtime.providers.foundry.hosting import create_hosted_server

        create_hosted_server(self.endpoint, authenticate=self.authenticate).run(
            host=self.host, port=self.port
        )
