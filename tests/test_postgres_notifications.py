import threading
import time

from app.services.postgres_notifications import (
    TOPOLOGY_CHANNEL,
    PostgresNotificationListener,
    notify_topology_changed,
)


def _listener(app, event):
    return PostgresNotificationListener(
        app.state.settings.database_url,
        TOPOLOGY_CHANNEL,
        event.set,
        retry_seconds=0.05,
    )


def test_committed_notification_reaches_another_connection(app):
    received = threading.Event()
    listener = _listener(app, received)
    listener.start()
    try:
        time.sleep(0.1)
        app.state.transaction_runner.run(
            "notify_test", lambda session: notify_topology_changed(session)
        )
        assert received.wait(3)
    finally:
        listener.shutdown()


def test_rolled_back_notification_is_not_delivered(app):
    received = threading.Event()
    listener = _listener(app, received)
    listener.start()
    try:
        time.sleep(0.1)
        with app.state.session_factory() as session:
            notify_topology_changed(session)
            session.rollback()
        assert received.wait(0.3) is False
    finally:
        listener.shutdown()
