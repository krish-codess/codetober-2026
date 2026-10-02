from goldstandard import migrate


def test_every_migration_has_a_rollback_and_contiguous_versions():
    ms = migrate.discover()
    assert [m.version for m in ms] == list(range(1, len(ms) + 1))
    assert all(m.down.strip() for m in ms)
