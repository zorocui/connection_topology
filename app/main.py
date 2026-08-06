import logging
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
from app.services.database_transactions import PostgresTransactionRunner
from app.services.import_testing import ImportTestService
from app.services.postgres_notifications import (
    TOPOLOGY_CHANNEL,
    PostgresNotificationListener,
)
from app.services.scan_queue import ScanQueueService
from app.services.scheduler import SchedulerService
from app.services.topology import HostAddressResolver
from app.services.topology_cache import TopologyCache


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or get_settings()
    engine = create_database_engine(resolved)
    session_factory = create_session_factory(engine)
    cipher = CredentialCipher(resolved.app_secret_key)
    transaction_runner = PostgresTransactionRunner(
        session_factory,
        max_concurrent_transactions=resolved.db_pool_size + resolved.db_max_overflow,
    )
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        assert_database_current(engine)
        app.state.migration_current = True

        def initialize_settings(session):
            setting = session.get(SystemSetting, 1)
            if setting is None:
                session.add(
                    SystemSetting(
                        id=1,
                        history_retention_days=resolved.history_retention_days,
                    )
                )

        transaction_runner.run("initialize_settings", initialize_settings)
        app.state.scan_queue.start()
        app.state.import_test_service.start()
        if app.state.scheduler:
            app.state.scheduler.start()
        app.state.topology_listener.start()
        try:
            yield
        finally:
            if app.state.scheduler:
                app.state.scheduler.shutdown()
            app.state.import_test_service.shutdown()
            app.state.scan_queue.shutdown()
            app.state.topology_listener.shutdown()
            engine.dispose()

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
    app.state.migration_current = False
    app.state.transaction_runner = transaction_runner
    app.state.address_resolver = HostAddressResolver()
    app.state.linux_collector = LinuxCollector(resolved.remote_timeout_seconds)
    app.state.windows_collector = WindowsCollector(resolved.remote_timeout_seconds)
    app.state.topology_cache = TopologyCache(ttl_seconds=30)
    app.state.topology_listener = PostgresNotificationListener(
        resolved.database_url,
        TOPOLOGY_CHANNEL,
        app.state.topology_cache.clear,
    )
    app.state.scan_queue = ScanQueueService(
        session_factory,
        cipher,
        app.state.linux_collector,
        app.state.windows_collector,
        transaction_runner,
        max_workers=resolved.scan_max_workers,
        queue_size=resolved.scan_queue_size,
        lease_seconds=resolved.scan_lease_seconds,
        heartbeat_seconds=resolved.task_heartbeat_seconds,
        on_successful_scan=app.state.topology_cache.clear,
    )
    app.state.import_test_service = ImportTestService(
        session_factory,
        cipher,
        None,
        app.state.linux_collector,
        app.state.windows_collector,
        transaction_runner,
        app.state.scan_queue.create_import_scan_batch,
        max_workers=resolved.import_test_max_workers,
        global_limit=resolved.import_test_max_workers,
        lease_seconds=resolved.scan_lease_seconds,
        heartbeat_seconds=resolved.task_heartbeat_seconds,
    )
    app.state.scheduler = (
        SchedulerService(
            session_factory,
            app.state.scan_queue,
            resolved.scan_jitter_seconds,
            transaction_runner,
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
