import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import helpers as H  # noqa: E402
from trading_agent import db  # noqa: E402
from trading_agent.config import parse_scope  # noqa: E402


class Env:
    def __init__(self, tmp_path, **kw):
        self.tmp = tmp_path
        self.settings = H.make_settings(tmp_path, **kw)
        self.con = db.open_runtime(self.settings)
        self.scope = parse_scope("pa_brooks:ES:RTH:5m", self.settings)
        self.report = tmp_path / "reports" / "data_availability.md"

    @property
    def root(self):
        return self.settings.data_root

    def ingest(self, **kw):
        from trading_agent.pipeline import run_ingest
        kw.setdefault("report_path", self.report)
        return run_ingest(self.settings, self.con, **kw)

    def scan(self, as_of):
        from trading_agent.scan import run_scan
        return run_scan(self.settings, self.con, self.scope, as_of)

    def count(self, where="1=1", *args):
        return self.con.execute(f"SELECT COUNT(*) FROM bars WHERE {where}", args).fetchone()[0]


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.con.close()
