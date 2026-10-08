import json

import numpy as np

from squeeze import data, stats, synth


def test_every_planted_defect_is_quarantined_or_flagged_never_dropped(tmp_path):
    planted = synth.dataset(tmp_path / "raw", seed=1)
    ds = data.build(tmp_path / "raw", tmp_path / "out", size=64)
    quarantine = {
        q["path"]: q["reason"] for q in map(json.loads, (tmp_path / "out/quarantine.jsonl").read_text().splitlines())
    }
    index = {k["path"]: k for k in json.loads((tmp_path / "out/index.json").read_text())["items"]}
    for path, expect in planted.items():
        if expect.startswith("converted"):
            assert expect in index[path]["flags"], path
        else:
            assert quarantine[path].startswith(expect), (path, quarantine.get(path))
    n_files = sum(1 for p in (tmp_path / "raw").rglob("*") if p.is_file())
    assert len(index) + len(quarantine) == n_files  # nothing vanished
    assert ds.images.shape == (len(index), 64, 64, 3)


def test_eval_split_is_exactly_the_imagenet_validation_provenance(tmp_path):
    synth.dataset(tmp_path / "raw", defects=False)
    ds = data.build(tmp_path / "raw", tmp_path / "out", size=64)
    for name, split in zip(ds.names, ds.splits, strict=True):
        assert (split == "eval") == name.startswith(data.EVAL_PREFIX)
    assert set(ds.splits) == set(data.SPLITS)


def test_build_is_idempotent_and_versioned_by_content(tmp_path):
    synth.dataset(tmp_path / "raw", defects=False)
    a = data.build(tmp_path / "raw", tmp_path / "out", size=64)
    stamp = (tmp_path / "out/images.u8").stat().st_mtime_ns
    b = data.build(tmp_path / "raw", tmp_path / "out", size=64)
    assert a.version == b.version and (tmp_path / "out/images.u8").stat().st_mtime_ns == stamp
    version, before = a.version, dict(zip(a.names, a.splits, strict=True))
    del a, b  # Windows will not rewrite a file that is still memory-mapped
    extra = tmp_path / "raw/train/n01440764/n01440764_777.JPEG"
    extra.write_bytes(synth._jpeg(synth._image(np.random.default_rng(9), 0, 64)))
    c = data.build(tmp_path / "raw", tmp_path / "out", size=64)
    assert c.version != version
    # the late file moved nothing that was already there
    assert all(before[n] == s for n, s in zip(c.names, c.splits, strict=True) if n in before)


def test_deploy_conditions_are_seeded_and_darker():
    x = np.random.default_rng(0).integers(0, 255, (4, 32, 32, 3), dtype=np.uint8)
    a, b = data.deploy_conditions(x, seed=3), data.deploy_conditions(x, seed=3)
    assert np.array_equal(a, b) and a.mean() < x.mean()


def test_wilson_and_paired_delta():
    lo, hi = stats.wilson(500, 500)
    assert 0.99 < lo < 1.0 and hi > 0.9999
    a = np.ones(500, bool)
    b = a.copy()
    b[:5] = False
    delta, lo, hi = stats.paired_delta(a, b)
    assert delta == -0.01 and lo < delta < hi <= 0


def test_a_limited_subset_is_stable_and_spans_the_classes(tmp_path):
    synth.dataset(tmp_path / "raw", defects=False, per_class=40)
    ds = data.build(tmp_path / "raw", tmp_path / "out", size=64)
    some = ds.idx("train", 60)
    assert len(set(ds.labels[some])) == 10  # not just the first classes in folder order
    assert np.array_equal(some, ds.idx("train", 60)) and set(some) <= set(ds.idx("train"))
    assert set(ds.idx("train", 30)) <= set(some)  # growing the limit only adds images
