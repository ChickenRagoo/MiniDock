"""Что сейчас играет: медиа-сессии Windows (Spotify, браузеры, YouTube Music…) — название, исполнитель,
обложка и кнопки «назад / пауза / вперёд». Работает через системное API Windows (то же, что у всплывающего
окна громкости Windows 10), в отдельном потоке со своим циклом asyncio — интерфейс не ждёт."""
import asyncio
import threading

from PySide6.QtCore import QObject, Signal

try:
    from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as Manager
    from winrt.windows.storage.streams import Buffer, InputStreamOptions
except Exception:  # пакетов winrt нет — карточек плееров просто не будет
    Manager = None

PLAYING = 4


class MediaWatcher(QObject):
    """sessions(list) — список dict(id, app, title, artist, playing, cover: bytes | None)."""
    sessions = Signal(list)

    def __init__(self):
        super().__init__()
        self.loop = None
        self.manager = None
        self.covers = {}  # (приложение, название) -> байты обложки: не перечитываем одно и то же
        self.available = Manager is not None
        if self.available:
            threading.Thread(target=self._run, daemon=True, name="MiniDockMedia").start()

    def _run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def _submit(self, coro):
        if self.loop is None:
            return None
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    # -- запросы из интерфейса
    def refresh(self):
        if self.available:
            self._submit(self._collect())

    def control(self, sid, action):
        """action: toggle | next | prev."""
        if self.available:
            self._submit(self._control(sid, action))

    # -- в потоке asyncio
    async def _get_manager(self):
        if self.manager is None:
            self.manager = await Manager.request_async()
        return self.manager

    async def _cover(self, props, key):
        if key in self.covers:
            return self.covers[key]
        data = None
        try:
            if props.thumbnail is not None:
                stream = await props.thumbnail.open_read_async()
                buf = Buffer(stream.size)
                await stream.read_async(buf, stream.size, InputStreamOptions.READ_AHEAD)
                data = bytes(memoryview(buf))
        except Exception:
            data = None
        if len(self.covers) > 32:
            self.covers.clear()
        self.covers[key] = data
        return data

    async def _collect(self):
        out = []
        try:
            mgr = await self._get_manager()
            for i, s in enumerate(mgr.get_sessions()):
                try:
                    props = await s.try_get_media_properties_async()
                    info = s.get_playback_info()
                    app = s.source_app_user_model_id or ""
                    title, artist = props.title or "", props.artist or ""
                    out.append(dict(id=i, app=app, title=title, artist=artist,
                                    playing=info.playback_status == PLAYING,
                                    cover=await self._cover(props, (app, title, artist))))
                except Exception:
                    continue
        except Exception:
            out = []
        self.sessions.emit(out)

    async def _control(self, sid, action):
        try:
            mgr = await self._get_manager()
            sessions = list(mgr.get_sessions())
            if not 0 <= sid < len(sessions):
                return
            s = sessions[sid]
            if action == "toggle":
                await s.try_toggle_play_pause_async()
            elif action == "next":
                await s.try_skip_next_async()
            elif action == "prev":
                await s.try_skip_previous_async()
        except Exception:
            pass
        await asyncio.sleep(0.25)
        await self._collect()
