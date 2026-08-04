from dataclasses import dataclass, field

from app.models import Device, OSType, ScanTrigger
from app.services.scan_queue import PRIORITY_SCHEDULED
from app.services.scheduler import SchedulerService


@dataclass
class RecordingQueue:
    calls: list[tuple[int, ScanTrigger, int]] = field(default_factory=list)

    def enqueue_device(self, device_id, trigger, priority):
        self.calls.append((device_id, trigger, priority))


def test_scheduler_enqueues_instead_of_scanning(app):
    queue = RecordingQueue()
    scheduler = SchedulerService(app.state.session_factory, queue, 123)
    scheduler._enqueue_device(42)
    assert queue.calls == [(42, ScanTrigger.SCHEDULED, PRIORITY_SCHEDULED)]


def test_scheduled_job_uses_configured_jitter(app):
    queue = RecordingQueue()
    scheduler = SchedulerService(app.state.session_factory, queue, 123)
    device = Device(
        id=99,
        name="scheduled",
        host="10.0.0.99",
        os_type=OSType.LINUX,
        port=22,
        username="ops",
        encrypted_password="unused",
        scan_interval_minutes=5,
        scheduled_enabled=True,
    )
    scheduler.sync_device(device)
    job = scheduler.scheduler.get_job("device-scan-99")
    assert job is not None
    assert job.trigger.jitter == 123


def test_marker_device_has_no_scheduled_job(app):
    queue = RecordingQueue()
    scheduler = SchedulerService(app.state.session_factory, queue, 123)
    device = Device(
        id=100,
        name="marker",
        host="10.0.0.100",
        os_type=OSType.LINUX,
        port=22,
        username="ops",
        encrypted_password="unused",
        scan_interval_minutes=5,
        scheduled_enabled=True,
        collection_enabled=False,
    )
    scheduler.sync_device(device)
    assert scheduler.scheduler.get_job("device-scan-100") is None
