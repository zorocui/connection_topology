import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models import (
    Device,
    ImportBatch,
    ImportBatchStatus,
    ImportRowResult,
    ImportStatus,
    ImportTestStatus,
    OSType,
)
from app.services.import_test_leases import (
    ImportTestLeaseLost,
    claim_import_tests,
    renew_import_test_leases,
)


def _seed_rows(app, count: int, *, expired_worker: str | None = None) -> list[int]:
    with app.state.session_factory() as session:
        batch = ImportBatch(
            filename="lease-tests.xlsx",
            status=ImportBatchStatus.TESTING,
            total_rows=count,
            imported_rows=count,
            test_pending_rows=0 if expired_worker else count,
            test_running_rows=count if expired_worker else 0,
        )
        session.add(batch)
        session.flush()
        row_ids = []
        for index in range(count):
            device = Device(
                name=f"lease-{index}", host=f"198.51.100.{index + 1}",
                os_type=OSType.LINUX, port=22, username="ops",
                encrypted_password=app.state.cipher.encrypt("secret"),
            )
            session.add(device)
            session.flush()
            row = ImportRowResult(
                batch_id=batch.id, row_number=index + 2, device_id=device.id,
                import_status=ImportStatus.IMPORTED, import_message="ok",
                test_status=(ImportTestStatus.RUNNING if expired_worker else ImportTestStatus.PENDING),
                test_worker_id=expired_worker,
                test_lease_expires_at=(datetime.now(timezone.utc) - timedelta(seconds=1) if expired_worker else None),
            )
            session.add(row)
            session.flush()
            row_ids.append(row.id)
        session.commit()
        return row_ids


def test_two_import_workers_respect_global_twenty_and_do_not_overlap(app):
    row_ids = _seed_rows(app, 30)
    barrier = threading.Barrier(2)

    def claim(worker: str):
        barrier.wait()
        return app.state.transaction_runner.run(
            f"claim_{worker}",
            lambda session: claim_import_tests(session, worker, 20, 20, 90),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(claim, "import-a")
        second_future = pool.submit(claim, "import-b")
        first, second = first_future.result(), second_future.result()
    assert set(first).isdisjoint(second)
    assert len(first) + len(second) == 20
    assert set(first + second) <= set(row_ids)


def test_expired_import_test_is_reclaimed_and_old_result_is_rejected(app):
    row_id = _seed_rows(app, 1, expired_worker="dead")[0]
    claimed = app.state.transaction_runner.run(
        "reclaim", lambda session: claim_import_tests(session, "new", 1, 20, 90)
    )
    assert claimed == [row_id]
    with pytest.raises(ImportTestLeaseLost):
        app.state.import_test_service._save_result_for_worker(
            row_id,
            "dead",
            ImportTestStatus.SUCCESS,
            "stale",
        )


def test_heartbeat_reports_rows_that_are_no_longer_owned(app):
    row_ids = _seed_rows(app, 2)
    claimed = app.state.transaction_runner.run(
        "claim", lambda session: claim_import_tests(session, "worker", 2, 20, 90)
    )
    assert claimed == row_ids
    with app.state.session_factory() as session:
        row = session.get(ImportRowResult, row_ids[1])
        row.test_worker_id = "other"
        session.commit()
    lost = app.state.transaction_runner.run(
        "heartbeat",
        lambda session: renew_import_test_leases(session, "worker", row_ids, 90),
    )
    assert lost == {row_ids[1]}
    with app.state.session_factory() as session:
        owned = session.scalar(select(ImportRowResult).where(ImportRowResult.id == row_ids[0]))
        assert owned.test_heartbeat_at is not None
