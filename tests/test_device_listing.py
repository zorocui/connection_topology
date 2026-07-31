from app.models import Cluster, Device, OSType
from app.services.device_listing import (
    build_page_links,
    list_device_page,
)


def add_device(
    session,
    app,
    *,
    name,
    host,
    username="tester",
    cluster=None,
):
    device = Device(
        name=name,
        host=host,
        os_type=OSType.LINUX,
        port=22,
        username=username,
        encrypted_password=app.state.cipher.encrypt("secret"),
        cluster_id=cluster.id if cluster else None,
        scheduled_enabled=False,
    )
    session.add(device)
    return device


def add_numbered_devices(session, app, count):
    for index in range(count):
        add_device(
            session,
            app,
            name=f"device-{index:03d}",
            host=f"10.20.{index // 250}.{index % 250 + 1}",
        )
    session.commit()


def test_device_page_reads_only_requested_slice(app):
    with app.state.session_factory() as session:
        add_numbered_devices(session, app, 45)

        first = list_device_page(session, "", 1, 20)
        second = list_device_page(session, "", 2, 20)
        third = list_device_page(session, "", 3, 20)

        assert [len(first.items), len(second.items), len(third.items)] == [
            20,
            20,
            5,
        ]
        assert first.items[0].name == "device-000"
        assert second.items[0].name == "device-020"
        assert third.items[-1].name == "device-044"
        assert third.total_items == 45
        assert third.total_pages == 3
        assert (third.first_item, third.last_item) == (41, 45)


def test_device_search_matches_only_name_and_host(app):
    with app.state.session_factory() as session:
        cluster = Cluster(name="needle-cluster")
        session.add(cluster)
        session.flush()
        add_device(
            session,
            app,
            name="needle-name",
            host="192.0.2.10",
        )
        add_device(
            session,
            app,
            name="username-only",
            host="192.0.2.20",
            username="needle-user",
        )
        add_device(
            session,
            app,
            name="cluster-only",
            host="198.51.100.20",
            cluster=cluster,
        )
        session.commit()

        by_name = list_device_page(session, " needle ", 1, 20)
        by_host = list_device_page(session, "198.51.100", 1, 20)

        assert [device.name for device in by_name.items] == ["needle-name"]
        assert [device.name for device in by_host.items] == ["cluster-only"]
        assert by_name.query == "needle"


def test_device_search_treats_sql_wildcards_as_literals(app):
    with app.state.session_factory() as session:
        add_device(
            session,
            app,
            name="literal%device",
            host="192.0.2.1",
        )
        add_device(
            session,
            app,
            name="literalXdevice",
            host="192.0.2.2",
        )
        add_device(
            session,
            app,
            name="literal_device",
            host="192.0.2.3",
        )
        session.commit()

        percent = list_device_page(session, "%", 1, 20)
        underscore = list_device_page(session, "_", 1, 20)

        assert [device.name for device in percent.items] == ["literal%device"]
        assert [device.name for device in underscore.items] == [
            "literal_device"
        ]


def test_device_page_clamps_page_and_handles_empty_results(app):
    with app.state.session_factory() as session:
        add_numbered_devices(session, app, 25)

        clamped = list_device_page(session, "", 99, 20)
        empty = list_device_page(session, "not-found", 9, 20)

        assert clamped.page == 2
        assert len(clamped.items) == 5
        assert empty.page == 1
        assert empty.total_pages == 0
        assert empty.items == []
        assert (empty.first_item, empty.last_item) == (0, 0)
        assert empty.page_links == []


def test_device_page_rejects_invalid_arguments(app):
    with app.state.session_factory() as session:
        for page, page_size, message in [
            (0, 20, "页码必须大于等于 1"),
            (1, 30, "每页数量仅支持 20、50、100"),
        ]:
            try:
                list_device_page(session, "", page, page_size)
            except ValueError as exc:
                assert str(exc) == message
            else:
                raise AssertionError("无效分页参数未被拒绝")


def test_build_page_links_compresses_large_ranges():
    assert build_page_links(1, 3) == [1, 2, 3]
    assert build_page_links(1, 100) == [1, 2, 3, None, 100]
    assert build_page_links(50, 100) == [
        1,
        None,
        48,
        49,
        50,
        51,
        52,
        None,
        100,
    ]
    assert build_page_links(100, 100) == [
        1,
        None,
        98,
        99,
        100,
    ]
