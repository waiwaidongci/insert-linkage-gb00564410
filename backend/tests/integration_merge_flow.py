"""两厂归并的端到端集成测试：SQLite 上验证冻结依据、可恢复重试与导出。

运行方式（在 backend 目录）：
    DATABASE_URL='sqlite:////tmp/merge-flow.db' JWT_SECRET=test \
    ARTIFACT_DIR=/tmp/merge-flow-art /tmp/venv2/bin/python tests/integration_merge_flow.py
"""

import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "sqlite:////tmp/merge-flow.db")
os.environ.setdefault("JWT_SECRET", "integration-secret")
os.environ.setdefault("ARTIFACT_DIR", "/tmp/merge-flow-art")
os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:6399/0")  # 无 Redis 也应容错
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from datetime import timedelta  # noqa: E402

from sqlalchemy import select  # noqa: E402

from src.config.database import Base, Session, engine  # noqa: E402
from src.config.settings import ARTIFACTS as ART  # noqa: E402
from src.constants.enums import Status  # noqa: E402
from src.domain.demo import demo_rows, merge_rows  # noqa: E402
from src.models.jobs import Run  # noqa: E402
from src.services import execution  # noqa: E402
from src.services.datasets import save  # noqa: E402
from src.utils.time import now  # noqa: E402

GROUPS = "GA: CP-A01、CP-A02、CP-A03\nGB: CP-B01、CP-B02、CP-B03\nGC: CP-C01、CP-C02"
PARAMS = {"cut_distance": 0.60, "merge_dataset_id": None, "signed_groups": GROUPS}


class MergeFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Base.metadata.create_all(engine)
        with Session.begin() as db:
            cls.old, _ = save(db, "旧批台账", "old.csv", demo_rows(), "seed")
            cls.new, _ = save(db, "新批台账", "new.csv", merge_rows(), "seed")
        PARAMS["merge_dataset_id"] = cls.new.id

    def _make_run(self):
        with Session.begin() as db:
            run = Run(name="两厂归并集成", dataset_id=self.old.id, params=dict(PARAMS), author="analyst")
            db.add(run)
            db.flush()
            return run.id

    def test_claim_freezes_both_batches_and_execute_writes_report(self):
        run_id = self._make_run()
        lease = execution.claim()
        self.assertIsNotNone(lease)
        job_id, attempt, payload = lease
        self.assertEqual(job_id, run_id)
        self.assertEqual(attempt, 1)
        self.assertEqual(payload["kind"], "merge")
        # 冻结快照同时携带两批行与指纹。
        self.assertEqual(payload["old"]["checksum"], self.old.checksum)
        self.assertEqual(payload["new"]["checksum"], self.new.checksum)
        self.assertEqual(len(payload["new"]["rows"]), 5)

        execution.execute(lease)
        with Session() as db:
            run = db.get(Run, run_id)
            self.assertEqual(run.status, Status.COMPLETE)
            self.assertEqual(run.export_file, f"analysis-{run_id}-1.csv")
            self.assertEqual(run.result["kind"], "merge")
            self.assertTrue((ART / run.export_file).is_file())
            group_titles = [t["title"] for t in run.result["tables"]]
            self.assertIn("归并打样清单与最远规格对", group_titles)

    def test_interrupted_retry_reuses_same_frozen_basis_and_single_export(self):
        run_id = self._make_run()
        first = execution.claim()
        self.assertEqual(first[1], 1)
        # 模拟首次执行中断：不写结果，租约过期后可被再次领取。
        second = execution.claim()
        # 同一运行仍在 RUNNING 且租约未过期时不会被重复领取。
        self.assertIsNone(second)
        with Session.begin() as db:
            run = db.get(Run, run_id, with_for_update=True)
            run.lease_until = now() - timedelta(minutes=1)  # 租约过期，模拟中断恢复。
        retried = execution.claim()
        self.assertEqual(retried[0], run_id)
        self.assertEqual(retried[1], 2)  # 沿用同一运行，attempt 递增而非新建出单。
        self.assertEqual(retried[2]["old"]["checksum"], self.old.checksum)
        self.assertEqual(retried[2]["new"]["checksum"], self.new.checksum)
        execution.execute(retried)
        with Session() as db:
            run = db.get(Run, run_id)
            self.assertEqual(run.status, Status.COMPLETE)
            self.assertEqual(run.attempt, 2)
            # 重试只产生一份新导出文件，运行记录仍为同一条，不重复出单。
            self.assertEqual(run.export_file, f"analysis-{run_id}-2.csv")
            self.assertEqual(len(db.scalars(select(Run).where(Run.id == run_id)).all()), 1)

    def test_cluster_run_still_uses_original_analysis_path(self):
        with Session.begin() as db:
            run = Run(name="纯切树", dataset_id=self.old.id,
                      params={"cut_distance": 0.40}, author="analyst")
            db.add(run)
            db.flush()
            run_id = run.id
        lease = execution.claim()
        self.assertEqual(lease[2]["kind"], "cluster")
        execution.execute(lease)
        with Session() as db:
            run = db.get(Run, run_id)
            self.assertEqual(run.status, Status.COMPLETE)
            self.assertNotIn("kind", run.result)  # 原切树结果结构保持不变。


if __name__ == "__main__":
    unittest.main(verbosity=2)
