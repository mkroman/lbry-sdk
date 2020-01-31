import os
import asyncio
import binascii
import logging
import random
import typing
from typing import Optional
from aiohttp.web import Request
from lbry.error import InvalidStreamDescriptorError
from lbry.file.source_manager import SourceManager
from lbry.stream.descriptor import StreamDescriptor
from lbry.stream.managed_stream import ManagedStream
from lbry.file.source import ManagedDownloadSource
if typing.TYPE_CHECKING:
    from lbry.conf import Config
    from lbry.blob.blob_manager import BlobManager
    from lbry.dht.node import Node
    from lbry.wallet.wallet import WalletManager
    from lbry.wallet.transaction import Transaction
    from lbry.extras.daemon.analytics import AnalyticsManager
    from lbry.extras.daemon.storage import SQLiteStorage, StoredContentClaim

log = logging.getLogger(__name__)


def path_or_none(p) -> Optional[str]:
    if not p:
        return
    return binascii.unhexlify(p).decode()


class StreamManager(SourceManager):
    _sources: typing.Dict[str, ManagedStream]

    filter_fields = set(SourceManager.filter_fields)
    filter_fields.update({
        'sd_hash',
        'stream_hash',
        'full_status',  # TODO: remove
        'blobs_remaining',
        'blobs_in_stream'
    })

    def __init__(self, loop: asyncio.AbstractEventLoop, config: 'Config', blob_manager: 'BlobManager',
                 wallet_manager: 'WalletManager', storage: 'SQLiteStorage', node: Optional['Node'],
                 analytics_manager: Optional['AnalyticsManager'] = None):
        super().__init__(loop, config, storage, analytics_manager)
        self.blob_manager = blob_manager
        self.wallet_manager = wallet_manager
        self.node = node
        self.resume_saving_task: Optional[asyncio.Task] = None
        self.re_reflect_task: Optional[asyncio.Task] = None
        self.update_stream_finished_futs: typing.List[asyncio.Future] = []
        self.running_reflector_uploads: typing.List[asyncio.Task] = []

    def add(self, source: ManagedStream):
        super().add(source)
        self.storage.content_claim_callbacks[source.stream_hash] = lambda: self._update_content_claim(source)

    async def _update_content_claim(self, stream: ManagedStream):
        claim_info = await self.storage.get_content_claim(stream.stream_hash)
        self._sources.setdefault(stream.sd_hash, stream).set_claim(claim_info, claim_info['value'])

    async def recover_streams(self, file_infos: typing.List[typing.Dict]):
        to_restore = []

        async def recover_stream(sd_hash: str, stream_hash: str, stream_name: str,
                                 suggested_file_name: str, key: str,
                                 content_fee: Optional['Transaction']) -> Optional[StreamDescriptor]:
            sd_blob = self.blob_manager.get_blob(sd_hash)
            blobs = await self.storage.get_blobs_for_stream(stream_hash)
            descriptor = await StreamDescriptor.recover(
                self.blob_manager.blob_dir, sd_blob, stream_hash, stream_name, suggested_file_name, key, blobs
            )
            if not descriptor:
                return
            to_restore.append((descriptor, sd_blob, content_fee))

        await asyncio.gather(*[
            recover_stream(
                file_info['sd_hash'], file_info['stream_hash'], binascii.unhexlify(file_info['stream_name']).decode(),
                binascii.unhexlify(file_info['suggested_file_name']).decode(), file_info['key'],
                file_info['content_fee']
            ) for file_info in file_infos
        ])

        if to_restore:
            await self.storage.recover_streams(to_restore, self.config.download_dir)

        # if self.blob_manager._save_blobs:
        #     log.info("Recovered %i/%i attempted streams", len(to_restore), len(file_infos))

    async def _load_stream(self, rowid: int, sd_hash: str, file_name: Optional[str],
                           download_directory: Optional[str], status: str,
                           claim: Optional['StoredContentClaim'], content_fee: Optional['Transaction'],
                           added_on: Optional[int]):
        try:
            descriptor = await self.blob_manager.get_stream_descriptor(sd_hash)
        except InvalidStreamDescriptorError as err:
            log.warning("Failed to start stream for sd %s - %s", sd_hash, str(err))
            return
        stream = ManagedStream(
            self.loop, self.config, self.blob_manager, descriptor.sd_hash, download_directory, file_name, status,
            claim, content_fee=content_fee, rowid=rowid, descriptor=descriptor,
            analytics_manager=self.analytics_manager, added_on=added_on
        )
        self.add(stream)

    async def initialize_from_database(self):
        to_recover = []
        to_start = []

        await self.storage.update_manually_removed_files_since_last_run()

        for file_info in await self.storage.get_all_lbry_files():
            # if the sd blob is not verified, try to reconstruct it from the database
            # this could either be because the blob files were deleted manually or save_blobs was not true when
            # the stream was downloaded
            if not self.blob_manager.is_blob_verified(file_info['sd_hash']):
                to_recover.append(file_info)
            to_start.append(file_info)
        if to_recover:
            await self.recover_streams(to_recover)

        log.info("Initializing %i files", len(to_start))
        to_resume_saving = []
        add_stream_tasks = []
        for file_info in to_start:
            file_name = path_or_none(file_info['file_name'])
            download_directory = path_or_none(file_info['download_directory'])
            if file_name and download_directory and not file_info['saved_file'] and file_info['status'] == 'running':
                to_resume_saving.append((file_name, download_directory, file_info['sd_hash']))
            add_stream_tasks.append(self.loop.create_task(self._load_stream(
                file_info['rowid'], file_info['sd_hash'], file_name,
                download_directory, file_info['status'],
                file_info['claim'], file_info['content_fee'],
                file_info['added_on']
            )))
        if add_stream_tasks:
            await asyncio.gather(*add_stream_tasks, loop=self.loop)
        log.info("Started stream manager with %i files", len(self._sources))
        if not self.node:
            log.info("no DHT node given, resuming downloads trusting that we can contact reflector")
        if to_resume_saving:
            log.info("Resuming saving %i files", len(to_resume_saving))
            self.resume_saving_task = asyncio.ensure_future(asyncio.gather(
                *(self._sources[sd_hash].save_file(file_name, download_directory)
                  for (file_name, download_directory, sd_hash) in to_resume_saving),
                loop=self.loop
            ))

    async def reflect_streams(self):
        while True:
            if self.config.reflect_streams and self.config.reflector_servers:
                sd_hashes = await self.storage.get_streams_to_re_reflect()
                sd_hashes = [sd for sd in sd_hashes if sd in self._sources]
                batch = []
                while sd_hashes:
                    stream = self._sources[sd_hashes.pop()]
                    if self.blob_manager.is_blob_verified(stream.sd_hash) and stream.blobs_completed:
                        if not stream.fully_reflected.is_set():
                            host, port = random.choice(self.config.reflector_servers)
                            batch.append(stream.upload_to_reflector(host, port))
                    if len(batch) >= self.config.concurrent_reflector_uploads:
                        await asyncio.gather(*batch, loop=self.loop)
                        batch = []
                if batch:
                    await asyncio.gather(*batch, loop=self.loop)
            await asyncio.sleep(300, loop=self.loop)

    async def start(self):
        await super().start()
        self.re_reflect_task = self.loop.create_task(self.reflect_streams())

    def stop(self):
        if self.resume_saving_task and not self.resume_saving_task.done():
            self.resume_saving_task.cancel()
        if self.re_reflect_task and not self.re_reflect_task.done():
            self.re_reflect_task.cancel()
        while self.update_stream_finished_futs:
            self.update_stream_finished_futs.pop().cancel()
        while self.running_reflector_uploads:
            self.running_reflector_uploads.pop().cancel()
        super().stop()
        log.info("finished stopping the stream manager")

    def _upload_stream_to_reflector(self, stream: ManagedStream):
        if self.config.reflector_servers:
            host, port = random.choice(self.config.reflector_servers)
            task = self.loop.create_task(stream.upload_to_reflector(host, port))
            self.running_reflector_uploads.append(task)
            task.add_done_callback(
                lambda _: None
                if task not in self.running_reflector_uploads else self.running_reflector_uploads.remove(task)
            )

    async def create(self, file_path: str, key: Optional[bytes] = None,
                     iv_generator: Optional[typing.Generator[bytes, None, None]] = None) -> ManagedStream:
        descriptor = await StreamDescriptor.create_stream(
            self.loop, self.blob_manager.blob_dir, file_path, key=key, iv_generator=iv_generator,
            blob_completed_callback=self.blob_manager.blob_completed
        )
        await self.storage.store_stream(
            self.blob_manager.get_blob(descriptor.sd_hash), descriptor
        )
        row_id = await self.storage.save_published_file(
            descriptor.stream_hash, os.path.basename(file_path), os.path.dirname(file_path), 0
        )
        source = ManagedStream(
            self.loop, self.config, self.blob_manager, descriptor.sd_hash, os.path.dirname(file_path),
            os.path.basename(file_path), status=ManagedDownloadSource.STATUS_FINISHED,
            rowid=row_id, descriptor=descriptor
        )
        self.add(source)
        if self.config.reflect_streams:
            self._upload_stream_to_reflector(source)
        return source

    async def _delete(self, stream: ManagedStream, delete_file: Optional[bool] = False):
        blob_hashes = [stream.sd_hash] + [b.blob_hash for b in stream.descriptor.blobs[:-1]]
        await self.blob_manager.delete_blobs(blob_hashes, delete_from_db=False)
        await self.storage.delete_stream(stream.descriptor)

    async def stream_partial_content(self, request: Request, sd_hash: str):
        return await self._sources[sd_hash].stream_file(request, self.node)
