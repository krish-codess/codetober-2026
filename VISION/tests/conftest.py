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
        web_dir=tmp_path / "no-web",
        device_token="dev-token",
        admin_token="admin-token",
    )


@pytest.fixture(scope="session")
def smoke_run(tmp_path_factory):
    """The whole pipeline, tiny, on generated data with planted defects. (root, dataset, record)"""
    pytest.importorskip("torch")
    from squeeze import data, pipeline, synth

    root = tmp_path_factory.mktemp("pipe")
    synth.dataset(root / "raw", seed=0, per_class=40, size=64)
    ds = data.build(root / "raw", root / "ds", size=64)
    return root, ds, pipeline.execute(pipeline.SMOKE, ds, root / "runs")
