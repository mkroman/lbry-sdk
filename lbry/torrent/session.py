import asyncio
import binascii
from typing import Optional

import libtorrent


NOTIFICATION_MASKS = [
    "error",
    "peer",
    "port_mapping",
    "storage",
    "tracker",
    "debug",
    "status",
    "progress",
    "ip_block",
    "dht",
    "stats",
    "session_log",
    "torrent_log",
    "peer_log",
    "incoming_request",
    "dht_log",
    "dht_operation",
    "port_mapping_log",
    "picker_log",
    "file_progress",
    "piece_progress",
    "upload",
    "block_progress"
]


DEFAULT_FLAGS = (  # fixme: somehow the logic here is inverted?
        libtorrent.add_torrent_params_flags_t.flag_paused
        | libtorrent.add_torrent_params_flags_t.flag_auto_managed
        | libtorrent.add_torrent_params_flags_t.flag_duplicate_is_error
        | libtorrent.add_torrent_params_flags_t.flag_upload_mode
        | libtorrent.add_torrent_params_flags_t.flag_update_subscribe
)


def get_notification_type(notification) -> str:
    for i, notification_type in enumerate(NOTIFICATION_MASKS):
        if (1 << i) & notification:
            return notification_type
    raise ValueError("unrecognized notification type")


class TorrentHandle:
    def __init__(self, loop, executor, handle):
        self._loop = loop
        self._executor = executor
        self._handle = handle
        self.finished = asyncio.Event(loop=loop)

    def _show_status(self):
        status = self._handle.status()
        if not status.is_seeding:
            print('%.2f%% complete (down: %.1f kB/s up: %.1f kB/s peers: %d) %s' % (
                status.progress * 100, status.download_rate / 1000, status.upload_rate / 1000,
                status.num_peers, status.state))
        elif not self.finished.is_set():
            self.finished.set()
            print("finished!")

    async def status_loop(self):
        while True:
            await self._loop.run_in_executor(
                self._executor, self._show_status
            )
            if self.finished.is_set():
                break
            await asyncio.sleep(1, loop=self._loop)

    async def pause(self):
        await self._loop.run_in_executor(
            self._executor, self._handle.pause
        )

    async def resume(self):
        await self._loop.run_in_executor(
            self._executor, self._handle.resume
        )


class TorrentSession:
    def __init__(self, loop, executor):
        self._loop = loop
        self._executor = executor
        self._session = None
        self._handles = {}

    async def bind(self, interface: str = '0.0.0.0', port: int = 6881):
        settings = {
            'listen_interfaces': f"{interface}:{port}",
            'enable_outgoing_utp': True,
            'enable_incoming_utp': True,
            'enable_outgoing_tcp': True,
            'enable_incoming_tcp': True
        }
        self._session = await self._loop.run_in_executor(
            self._executor, libtorrent.session, settings  # pylint: disable=c-extension-no-member
        )
        await self._loop.run_in_executor(
            self._executor,
            # lambda necessary due boost functions raising errors when asyncio inspects them. try removing later
            lambda: self._session.add_dht_router("router.utorrent.com", 6881)  # pylint: disable=unnecessary-lambda
        )
        self._loop.create_task(self.process_alerts())

    def _pop_alerts(self):
        for alert in self._session.pop_alerts():
            print("alert: ", alert)

    async def process_alerts(self):
        while True:
            await self._loop.run_in_executor(
                self._executor, self._pop_alerts
            )
            await asyncio.sleep(1, loop=self._loop)

    async def pause(self):
        await self._loop.run_in_executor(
            self._executor, lambda: self._session.save_state()  # pylint: disable=unnecessary-lambda
        )
        await self._loop.run_in_executor(
            self._executor, lambda: self._session.pause()  # pylint: disable=unnecessary-lambda
        )

    async def resume(self):
        await self._loop.run_in_executor(
            self._executor, self._session.resume
        )

    def _add_torrent(self, btih: str, download_directory: Optional[str]):
        params = {'info_hash': binascii.unhexlify(btih.encode()), 'flags': DEFAULT_FLAGS}
        if download_directory:
            params['save_path'] = download_directory
        self._handles[btih] = TorrentHandle(self._loop, self._executor, self._session.add_torrent(params))

    async def add_torrent(self, btih, download_path):
        await self._loop.run_in_executor(
            self._executor, self._add_torrent, btih, download_path
        )
        self._loop.create_task(self._handles[btih].status_loop())
        await self._handles[btih].finished.wait()

    async def remove_torrent(self, btih, remove_files=False):
        if btih in self._handles:
            handle = self._handles[btih]
            self._session.remove_torrent(handle, 1 if remove_files else 0)
            self._handles.pop(btih)


def get_magnet_uri(btih):
    return f"magnet:?xt=urn:btih:{btih}"
