import asyncio
import json
from typing import Any, Dict, List, Optional
from socketio.async_client import AsyncClient # type: ignore[import-untyped]

from orca_driver_interface.driver_interfaces import IDriver, ILabwarePlaceableDriver


class SocketClient:
    def __init__(self, uri: str) -> None:
        self.uri = uri
        # self.cache_ttl = 5  # Cache TTL in seconds
        # self.last_checked_initialized = 0
        # self.last_checked_running = 0
        self.sio = AsyncClient()

    async def connect(self) -> None:
        await self.sio.connect(self.uri)

    @property
    def is_connected(self) -> bool:
        return self.sio.connected

    async def disconnect(self):
        await self.sio.disconnect()

    async def send(self, action: str, data: Dict[str, Any]) -> Any:
        await self.sio.emit(action, data)
        response = await self.sio.call(f'{action}_response')
        return response


class RemoteDriverClient(IDriver):
    def __init__(self, name: str):
        self._name: str = name
        self._uri: str | None = None
        self._sio: SocketClient | None = None
        self._init_options: Dict[str, Any] = {}

    @property
    def is_connected(self) -> bool:
        if self._sio is None:
            return False
        return self._sio.is_connected

    async def connect(self) -> None:
        ip = self._init_options["ip"] or "localhost"
        port = self._init_options["port"] or 5001
        self._uri = f"ws://{ip}:{port}"
        self._sio = SocketClient(self._uri)
        await self._sio.connect()

    async def disconnect(self) -> None:
        assert self._sio is not None
        await self._sio.disconnect()

    async def send(self, action: str, data: Dict[str, Any]) -> Any:
        assert self._sio is not None
        return await self._sio.send(action, data)
        
    def _run_async_task(self, action: str, data: Dict[str, Any] = {}) -> Dict[str, Any]:
        try:
            loop = asyncio.get_running_loop()
            future = asyncio.run_coroutine_threadsafe(self.send(action, data), loop)
            return json.loads(future.result())
        except RuntimeError:
            return json.loads(asyncio.run(self.send(action, data)))

    @property
    def name(self) -> str:
        return self._name

    @property
    def is_initialized(self) -> bool:
        dict_ret = self._run_async_task("is_initialized")
        return dict_ret["is_initialized"]

    @property
    def is_running(self) -> bool:
        dict_ret = self._run_async_task("is_running")
        return dict_ret["is_running"]

    def set_init_options(self, init_options: Dict[str, Any]) -> None:
        self._run_async_task("set_init_options", init_options)
        
    async def initialize(self) -> None:
        self._run_async_task("initialize")

    async def execute(self, command: str, options: Dict[str, Any]) -> None:
        dict_ret = self._run_async_task("execute", {"command": command, "options": options})


class RemoteLabwarePlaceableDriverClient(RemoteDriverClient, ILabwarePlaceableDriver):
    
    async def prepare_for_pick(self, labware_name: str, labware_type: str, barcode: Optional[str] = None, alias: Optional[str] = None) -> None:
        self._run_async_task("prepare_for_pick", {"labware_name": labware_name, "labware_type": labware_type, "barcode": barcode, "alias": alias})

    async def prepare_for_place(self, labware_name: str, labware_type: str, barcode: Optional[str] = None, alias: Optional[str] = None) -> None:
        self._run_async_task("prepare_for_place", {"labware_name": labware_name, "labware_type": labware_type, "barcode": barcode, "alias": alias})

    async def notify_picked(self, labware_name: str, labware_type: str, barcode: Optional[str] = None, alias: Optional[str] = None) -> None:
        self._run_async_task("notify_picked", {"labware_name": labware_name, "labware_type": labware_type, "barcode": barcode, "alias": alias})

    async def notify_placed(self, labware_name: str, labware_type: str, barcode: Optional[str] = None, alias: Optional[str] = None) -> None:
        self._run_async_task("notify_placed", {"labware_name": labware_name, "labware_type": labware_type, "barcode": barcode, "alias": alias})


class RemoteTransporterDriverClient(RemoteDriverClient):

    async def pick(self, position_name: str, labware_type: str) -> None:
        self._run_async_task(f"pick", {"position_name": position_name, "labware_type": labware_type})

    async def place(self, position_name: str, labware_type: str) -> None:
        self._run_async_task(f"place" , {"position_name": position_name, "labware_type": labware_type})

    def get_taught_positions(self) -> List[str]:
        ret_dict = self._run_async_task("set_init_options")
        return ret_dict["positions"]
    
if __name__ == '__main__':

    s = RemoteDriverClient("test_client")
    s.set_init_options({"ip": "localhost", "port": 5001})
    asyncio.run(s.connect())
    asyncio.run(s.initialize())
    print(s.is_initialized)
    asyncio.run(s.disconnect())