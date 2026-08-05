import logging
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.collectors import LinuxCollector, WindowsCollector
from app.config import Settings, get_settings
from app.database import (
    assert_database_current,
    create_database_engine,
    create_session_factory,
)
from app.models import SystemSetting
from app.routes import api, pages
from app.security import CredentialCipher, SecretRedactingFilter
from app.services.import_testing import ImportTestService
from app.services.process_guard import SQLiteProcessGuard, sqlite_database_path
from app.services.scan_queue import ScanQueueService
from app.services.scheduler import SchedulerService
from app.services.sqlite_writes import SQLiteWriteCoordinator
from app.services.topology import HostAddressResolver
from app.services.topology_cache import TopologyCache


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or get_settings()
    engine = create_database_engine(resolved)
    session_factory = create_session_factory(engine)
    cipher = CredentialCipher(resolved.app_secret_key)
    sqlite_write_coordinator = SQLiteWriteCoordinator(
        session_factory,
        (),
        enabled=engine.dialect.name == "sqlite",
    )
    sqlite_process_guard = SQLiteProcessGuard(sqlite_database_path(resolved.database_url))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        sqlite_process_guard.acquire()
        try:
            assert_database_current(engine)
            with (
                sqlite_write_coordinator.write_once("initialize_settings"),
                session_factory() as session,
            ):
                setting = session.get(SystemSetting, 1)
                if setting is None:
                    session.add(
                        SystemSetting(
                            id=1,
                            history_retention_days=resolved.history_retention_days,
                        )
                    )
                    session.commit()
            app.state.scan_queue.start()
            if app.state.scheduler:
                app.state.scheduler.start()
            app.state.import_test_service.resume_pending()
            try:
                yield
            finally:
                if app.state.scheduler:
                    app.state.scheduler.shutdown()
                app.state.import_executor.shutdown(wait=True, cancel_futures=False)
                app.state.scan_queue.shutdown()
        finally:
            engine.dispose()
            sqlite_process_guard.release()

    app = FastAPI(
        title="连接图谱",
        description="Linux SSH 与 Windows WinRM 服务器连接拓扑",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.settings = resolved
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.cipher = cipher
    app.state.sqlite_process_guard = sqlite_process_guard
    app.state.sqlite_write_coordinator = sqlite_write_coordinator
    app.state.address_resolver = HostAddressResolver()
    app.state.linux_collector = LinuxCollector(resolved.remote_timeout_seconds)
    app.state.windows_collector = WindowsCollector(resolved.remote_timeout_seconds)
    app.state.topology_cache = TopologyCache(ttl_seconds=30)
    app.state.import_executor = ThreadPoolExecutor(
        max_workers=resolved.import_test_max_workers,
        thread_name_prefix="import-test",
    )
    app.state.scan_queue = ScanQueueService(
        session_factory,
        cipher,
        app.state.linux_collector,
        app.state.windows_collector,
        sqlite_write_coordinator,
        max_workers=resolved.scan_max_workers,
        queue_size=resolved.scan_queue_size,
        on_successful_scan=app.state.topology_cache.clear,
    )
    app.state.import_test_service = ImportTestService(
        session_factory,
        cipher,
        app.state.import_executor,
        app.state.linux_collector,
        app.state.windows_collector,
        sqlite_write_coordinator,
        app.state.scan_queue.create_import_scan_batch,
    )
    app.state.scheduler = (
        SchedulerService(
            session_factory,
            app.state.scan_queue,
            resolved.scan_jitter_seconds,
            sqlite_write_coordinator,
            on_history_purged=app.state.topology_cache.clear,
        )
        if resolved.scheduler_enabled
        else None
    )
    app.mount("/static", StaticFiles(directory="app/static"), name="static")
    app.include_router(pages.router)
    app.include_router(api.router)

    redactor = SecretRedactingFilter()
    for handler in logging.getLogger().handlers:
        handler.addFilter(redactor)
    return app


app = create_app()
