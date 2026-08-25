from __future__ import annotations

import pytest

from shadowfleet import db as sfdb
from shadowfleet import testdata


@pytest.fixture
def conn(tmp_path):
    c = sfdb.connect(str(tmp_path / "t.db"), verbose=False)
    yield c
    c.close()


@pytest.fixture
def fleet(tmp_path):
    """A populated database with the synthetic fleet and every detector run."""
    from shadowfleet import detect_gaps, detect_spoof, detect_sts

    path = str(tmp_path / "fleet.db")
    c = sfdb.connect(path, verbose=False)
    testdata.build(c)
    c.close()
    detect_gaps.main(["--db", path, "--min-hours", "4"])
    detect_sts.main(["--db", path])
    detect_spoof.main(["--db", path])
    c = sfdb.connect(path, verbose=False)
    yield c
    c.close()
