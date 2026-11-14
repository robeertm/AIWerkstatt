"""Self-healing retry: schema migration for existing databases and hard bounds.

Before 0.14.2 the ``tasks`` table had no ``started_at``/``error`` columns, so every call
of ``engine.resurrect_stale_tasks`` raised ``OperationalError`` and the ingest loop
swallowed it: the self-healing never ran. These tests pin the migration (idempotent, only
missing columns) and the bounds that keep a reactivated retry from looping forever.

Run: cd backend && python3 -m unittest discover -s tests
"""
import atexit
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest

_TMP_DATA = tempfile.mkdtemp(prefix="aiw-heal-data-")
_TMP_WS = tempfile.mkdtemp(prefix="aiw-heal-ws-")
atexit.register(shutil.rmtree, _TMP_DATA, True)
atexit.register(shutil.rmtree, _TMP_WS, True)
os.environ.setdefault("AIWERKSTATT_DATA", _TMP_DATA)
os.environ.setdefault("AIWERKSTATT_WORKSPACES", _TMP_WS)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# The schema of the tables as an older release created them (no columns added later).
_OLD_SCHEMA = """
CREATE TABLE threads (
  id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT, title TEXT,
  state TEXT DEFAULT 'open', session_id TEXT DEFAULT '', created_at TEXT
);
CREATE TABLE tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT, thread_id INTEGER, kind TEXT,
  status TEXT DEFAULT 'queued', attempts INTEGER DEFAULT 0,
  created_at TEXT, finished_at TEXT
);
"""


def _iso(seconds_ago=0):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - seconds_ago))


class Migration(unittest.TestCase):
    """``db.migrate`` on a database file in the old layout."""

    @classmethod
    def setUpClass(cls):
        global db
        import db

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="aiw-heal-old-")
        self.c = sqlite3.connect(os.path.join(self.dir, "engine.db"))
        self.c.executescript(_OLD_SCHEMA)
        self.c.execute("INSERT INTO threads(project_id,title,created_at) VALUES('p','t',?)", (_iso(),))
        self.c.execute("INSERT INTO tasks(thread_id,kind,status,created_at) VALUES(1,'new','failed',?)",
                       (_iso(60),))
        self.c.execute("INSERT INTO tasks(thread_id,kind,status,created_at) VALUES(1,'followup','queued',?)",
                       (_iso(),))
        self.c.commit()

    def tearDown(self):
        self.c.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def cols(self, table):
        return {r[1] for r in self.c.execute("PRAGMA table_info(%s)" % table)}

    def test_adds_missing_columns_once(self):
        self.assertNotIn("started_at", self.cols("tasks"))
        added = db.migrate(self.c)
        self.assertIn("tasks.started_at", added)
        self.assertIn("tasks.error", added)
        self.assertIn("threads.origin", added)
        for col in ("started_at", "error", "req_model", "req_effort", "origin"):
            self.assertIn(col, self.cols("tasks"))
        # idempotent: a second (and third) run adds nothing and does not fail
        self.assertEqual(db.migrate(self.c), [])
        self.assertEqual(db.migrate(self.c), [])

    def test_keeps_rows_and_closes_old_failures(self):
        db.migrate(self.c)
        rows = self.c.execute("SELECT status, error FROM tasks ORDER BY id").fetchall()
        self.assertEqual(len(rows), 2)
        # a run that had already failed before the upgrade is not re-run by surprise
        self.assertEqual(rows[0][0], "failed")
        self.assertTrue(rows[0][1])
        # everything else is untouched
        self.assertEqual(rows[1], ("queued", None))

    def test_partial_schema(self):
        # a database that already has some of the later columns gets only the rest
        self.c.execute("ALTER TABLE tasks ADD COLUMN req_model TEXT")
        self.c.commit()
        added = db.migrate(self.c)
        self.assertNotIn("tasks.req_model", added)
        self.assertIn("tasks.error", added)

    def test_live_schema_is_complete(self):
        live = {r[1] for r in db.query_all("PRAGMA table_info(tasks)")}
        self.assertTrue({"started_at", "error"} <= live)


class SelfHealing(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        global db, engine
        import db
        import engine
        cls.project = engine.create_project("Heal test", "x", "", "admin", provider="demo")
        cls._saved = (engine.RETRY_BASE_SEC, engine.RETRY_CAP_SEC, engine.MAX_RETRIES,
                      engine.RETRY_MAX_AGE_SEC)

    def setUp(self):
        engine.RETRY_BASE_SEC, engine.RETRY_CAP_SEC, engine.MAX_RETRIES, \
            engine.RETRY_MAX_AGE_SEC = self._saved
        th =engine.create_thread(self.project, "Heal", "build something", "admin")
        self.thread_id = th["thread_id"]
        self.task_id = db.query_one("SELECT id FROM tasks WHERE thread_id=?", (self.thread_id,))["id"]

    def tearDown(self):
        # leave nothing behind that a later test could pick up
        db.execute("UPDATE tasks SET status='cancelled' WHERE thread_id=? "
                   "AND status NOT IN ('done','cancelled')", (self.thread_id,))
        self._claim()

    # -- helpers ------------------------------------------------------------
    def _claim(self):
        """What the orchestrator does: take every descriptor out of the queue."""
        q = engine.TASK_QUEUE
        for n in os.listdir(q) if os.path.isdir(q) else []:
            try:
                os.remove(os.path.join(q, n))
            except OSError:
                pass

    def _fail(self):
        """A run that ends without a reply (what ingest records for a ``failed`` event)."""
        db.execute("UPDATE tasks SET status='failed', finished_at=? WHERE id=?",
                   (engine.store.now(), self.task_id))

    def task(self):
        return db.query_one("SELECT * FROM tasks WHERE id=?", (self.task_id,))

    def heal(self, alive=lambda pid: False):
        # the database is shared with other test modules: count only this test's task
        return [x for x in engine.resurrect_stale_tasks(alive) if x[2] == self.task_id]

    def events(self, needle):
        return db.query_all("SELECT * FROM events WHERE thread_id=? AND text LIKE ?",
                            (self.thread_id, "%" + needle + "%"))

    # -- tests --------------------------------------------------------------
    def test_runs_without_error(self):
        # the original bug: "no such column: t.started_at"
        self._claim()
        self.heal()

    def test_retry_is_bounded(self):
        """No endless loop: even with no cooldown at all and a run that always fails, the
        self-healer stops after MAX_RETRIES attempts and never picks the task up again."""
        engine.RETRY_BASE_SEC = engine.RETRY_CAP_SEC = 0
        engine.MAX_RETRIES = 3
        self._claim()
        self._fail()
        restarts = 0
        for _ in range(200):
            restarts += len(self.heal())
            self._claim()
            if self.task()["status"] == "queued":
                self._fail()
        t = self.task()
        self.assertEqual(restarts, 3)
        self.assertEqual(t["attempts"], 3)
        self.assertEqual(t["status"], "failed")
        self.assertIn("3 automatic retries", t["error"])
        self.assertEqual(len(self.events("Automatic retries stopped")), 1)
        self.assertEqual(len(self.events("retry (")), 3)
        self.assertEqual(engine._thread_status(self.thread_id), "failed")

    def test_retry_keeps_the_first_start_of_the_task(self):
        """0.18.0: started_at moves to each new attempt; first_started_at ("task since") never does."""
        engine.RETRY_BASE_SEC = engine.RETRY_CAP_SEC = 0
        engine.MAX_RETRIES = 3
        self._claim()
        db.execute("UPDATE tasks SET status='failed', finished_at=?, started_at=?, first_started_at=NULL WHERE id=?",
                   (_iso(100), _iso(200), self.task_id))
        self.assertEqual(len(self.heal()), 1)
        t = self.task()
        self.assertEqual(t["first_started_at"], _iso(200))        # carried over from the old attempt
        self.assertGreater(t["started_at"], t["first_started_at"])
        self._claim()
        self._fail()
        self.assertEqual(len(self.heal()), 1)
        self.assertEqual(self.task()["first_started_at"], _iso(200))   # second retry: still the first

    def test_defaults_three_retries_growing_pause(self):
        """Defaults: at most 3 automatic retries, pauses of 5, 15 and 45 minutes."""
        self.assertEqual(engine.MAX_RETRIES, 3)
        self.assertEqual([engine._retry_cooldown(n) for n in (0, 1, 2)], [300, 900, 2700])
        self.assertEqual(engine._retry_cooldown(9), 2700)

    def test_cooldown_measured_from_last_attempt(self):
        engine.MAX_RETRIES = 10
        self._claim()
        # the original request is old (well past the 900 s cap), the last attempt is fresh
        db.execute("UPDATE tasks SET created_at=?, status='failed', finished_at=?, attempts=2 "
                   "WHERE id=?", (_iso(3600), _iso(5), self.task_id))
        self.assertEqual(self.heal(), [])
        self.assertEqual(self.task()["status"], "retrying")
        self.assertEqual(engine._thread_status(self.thread_id), "retrying")
        # once the cooldown (2 attempts → 2700 s) has passed, the next attempt starts
        db.execute("UPDATE tasks SET finished_at=? WHERE id=?", (_iso(2800), self.task_id))
        self.assertEqual(len(self.heal()), 1)
        t = self.task()
        self.assertEqual((t["status"], t["attempts"]), ("queued", 3))
        self.assertTrue(t["started_at"])
        self.assertIsNone(t["finished_at"])
        self.assertTrue(os.path.exists(os.path.join(engine.TASK_QUEUE, "%d.json" % self.task_id)))

    def test_requeue_resets_cooldown_clock(self):
        """A runner that dies before writing anything leaves no finished_at — the cooldown
        must then count from the re-queue, not from an older attempt."""
        engine.RETRY_BASE_SEC, engine.RETRY_CAP_SEC = 90, 900
        self._claim()
        db.execute("UPDATE tasks SET status='failed', finished_at=?, started_at=? WHERE id=?",
                   (_iso(1000), _iso(1100), self.task_id))
        self.assertEqual(len(self.heal()), 1)
        self._claim()                                  # claimed, runner gone, status still queued
        self.assertEqual(self.heal(), [])
        self.assertEqual(self.task()["status"], "retrying")

    def test_age_window_ends_retries(self):
        self._claim()
        db.execute("UPDATE tasks SET created_at=?, status='running' WHERE id=?",
                   (_iso(engine.RETRY_MAX_AGE_SEC + 60), self.task_id))
        self.assertEqual(self.heal(), [])
        t = self.task()
        self.assertEqual(t["status"], "failed")
        self.assertTrue(t["error"])
        self.assertEqual(len(self.events("Automatic retries stopped")), 1)
        self.heal()
        self.assertEqual(len(self.events("Automatic retries stopped")), 1)

    def test_old_failed_closed_silently(self):
        self._claim()
        db.execute("UPDATE tasks SET created_at=?, status='failed', finished_at=? WHERE id=?",
                   (_iso(engine.RETRY_MAX_AGE_SEC + 60), _iso(engine.RETRY_MAX_AGE_SEC), self.task_id))
        self.heal()
        self.assertTrue(self.task()["error"])
        self.assertEqual(len(self.events("Automatic retries stopped")), 0)
        self.assertEqual(engine._thread_status(self.thread_id), "failed")

    def test_live_runner_is_left_alone(self):
        self._claim()
        self._fail()
        self.assertEqual(self.heal(lambda pid: True), [])
        self.assertEqual(self.heal(lambda pid: 1 / 0), [])
        self.assertEqual(self.task()["attempts"], 0)

    def test_queued_descriptor_is_left_alone(self):
        # not claimed yet → a legitimate wait, not an orphan
        self.assertEqual(self.heal(), [])
        self.assertEqual(self.task()["status"], "queued")

    def test_paused_while_limit_active(self):
        self._claim()
        self._fail()
        saved = engine.LIMIT_FILE
        engine.LIMIT_FILE = os.path.join(_TMP_DATA, "limit-test.json")
        with open(engine.LIMIT_FILE, "w", encoding="utf-8") as f:
            f.write('{"until": %d}' % (int(time.time()) + 600))
        try:
            self.assertTrue(engine.limit_status().get("active"))
            self.assertEqual(self.heal(), [])
            self.assertEqual(self.task()["attempts"], 0)
        finally:
            os.remove(engine.LIMIT_FILE)
            engine.LIMIT_FILE = saved

    def test_stop_while_waiting_for_retry(self):
        """A stopped task must not be re-queued by the self-healer."""
        engine.RETRY_BASE_SEC = engine.RETRY_CAP_SEC = 0
        self._claim()
        self._fail()
        self.assertEqual(len(self.heal()), 1)          # re-queued, descriptor in the queue
        engine.enqueue_stop(self.project, self.thread_id)
        self.assertEqual(self.task()["status"], "cancelled")
        self.assertFalse(os.path.exists(os.path.join(engine.TASK_QUEUE, "%d.json" % self.task_id)))
        self._claim()
        for _ in range(5):
            self.assertEqual(self.heal(), [])
        self.assertEqual(self.task()["status"], "cancelled")

    def test_stop_cancels_failed_and_retrying(self):
        for status in ("failed", "retrying"):
            db.execute("UPDATE tasks SET status=?, error=NULL WHERE id=?", (status, self.task_id))
            engine.enqueue_stop(self.project, self.thread_id)
            self.assertEqual(self.task()["status"], "cancelled", status)
            self._claim()


if __name__ == "__main__":
    unittest.main()
