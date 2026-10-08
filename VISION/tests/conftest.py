import dataclasses

import pytest

from squeeze import config


@pytest.fixture
def cfg(tmp_path):
    (tmp_path / "pk").mkdir()
    return dataclasses.replace(
        config.load(),
        db_path=tmp_path / "t.db",
        packages_dir=tmp_path / "pk",
        raw_dir=tmp_path / "raw",
        device_token="dev-token",
        admin_token="admin-token",
    )
