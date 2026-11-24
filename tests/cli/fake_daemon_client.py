import httpx

from orca.cli.daemon_client import DaemonLoadingClient


class FakeDaemonClient(DaemonLoadingClient):
    def __init__(self, transport: httpx.BaseTransport) -> None:
        super().__init__("http://test-daemon", timeout=30.0)
        self._transport = transport

    def _make_http_client(self) -> httpx.Client:
        return httpx.Client(
            transport=self._transport, base_url=self._base_url,
            timeout=self._timeout,
        )
