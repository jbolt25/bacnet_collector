from pathlib import Path
from bacnet_console.db import Store
from collections import namedtuple

Device = namedtuple('Device', ['instance', 'name', 'address', 'points'])
Point = namedtuple('Point', ['name', 'object_id', 'property_id', 'units'])

def test_register_config_correctness(tmp_path: Path):
    store = Store(tmp_path / "config.sqlite3")

    dev1 = Device(1001, "Device 1", "192.168.1.10", [
        Point("P1", "analog-input,1", "present-value", "F"),
        Point("P2", "analog-input,2", "present-value", "F")
    ])

    store.register_config([dev1])

    with store._lock, store._db:
        devices = store._db.execute("SELECT instance, name, current_address, configured FROM devices").fetchall()
        assert len(devices) == 1
        assert tuple(devices[0]) == (1001, "Device 1", "192.168.1.10", 1)

        points = store._db.execute("SELECT name, object_id, configured, disabled FROM points ORDER BY name").fetchall()
        assert len(points) == 2
        assert tuple(points[0]) == ("P1", "analog-input,1", 1, 0)
        assert tuple(points[1]) == ("P2", "analog-input,2", 1, 0)

        # Disable a point manually to ensure it's preserved
        store._db.execute("UPDATE points SET disabled=1 WHERE name='P1'")

    # Re-register with the same config, it should update and keep disabled=1
    store.register_config([dev1])

    with store._lock, store._db:
        points = store._db.execute("SELECT name, object_id, configured, disabled FROM points ORDER BY name").fetchall()
        assert len(points) == 2
        assert tuple(points[0]) == ("P1", "analog-input,1", 0, 1) # disabled=1, configured=0
        assert tuple(points[1]) == ("P2", "analog-input,2", 1, 0) # disabled=0, configured=1

    store.close()
