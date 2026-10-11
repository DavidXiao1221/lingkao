"""Async tutoring uses local HTTP, isolated databases, and injected fake providers."""
from concurrent.futures import ThreadPoolExecutor
import copy
from dataclasses import replace
import json
from pathlib import Path
import sys
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ai_tutor
import engine
import server
import test_http as http_fixture


class AIHTTPTests(unittest.TestCase):
    request = http_fixture.HTTPTests.request
    mutate = http_fixture.HTTPTests.mutate
    stored = http_fixture.HTTPTests.stored
    enter_training = http_fixture.HTTPTests.enter_training

    def setUp(self):
        http_fixture.HTTPTests.setUp(self)
        self.server.ai_config = ai_tutor.TutorConfig(api_key="fixture-http-secret", min_interval_seconds=0)
        self.release = threading.Event()
        self.entered = threading.Event()
        self.calls = []

        def provider(config, messages):
            self.calls.append(copy.deepcopy(messages))
            self.entered.set()
            if not self.release.wait(5):
                raise ai_tutor.TutorError("测试请求超时。")
            return "先写出内层函数的导数。", {"prompt_tokens": 11, "completion_tokens": 9}

        self.server.ai_client = provider

    def tearDown(self):
        self.release.set()
        self.wait_workers()
        http_fixture.HTTPTests.tearDown(self)

    def wait_workers(self):
        with self.server.ai_guard:
            workers = list(self.server.ai_workers)
        for worker in workers:
            worker.join(6)
            self.assertFalse(worker.is_alive())

    def ask(self, **extra):
        payload = {"revision": self.state["revision"], "instance_id": self.state["instance_id"],
                   "message": "第一步怎么选？", "consent": True, **extra}
        response = self.request("POST", "/api/tutor", payload, self.cookie, self.state["csrf"])
        if response[0] == 202:
            self.state = response[1]
        return response

    def refresh(self):
        status, self.state, _ = self.request("GET", "/api/state", cookie=self.cookie)
        self.assertEqual(status, 200)
        return self.state

    def test_pending_does_not_block_reads_and_success_records_assistance(self):
        self.enter_training()
        self.assertEqual(self.ask(consent=False)[0], 202)
        self.assertTrue(self.entered.wait(2))
        self.assertEqual(self.state["ai_request"]["status"], "pending")
        self.assertFalse(self.state["help_used"])
        started = time.monotonic()
        self.refresh()
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(self.state["ai_limits"]["participant_used"], 1)
        for action_name, fields in (("answer", {"answer": "0"}), ("hint", {}), ("catalog", {})):
            with self.subTest(action=action_name):
                self.assertEqual(self.mutate(action_name, **fields)[0], 409)
        self.assertEqual(self.request("POST", "/api/reset", {"revision": self.state["revision"]},
                                      self.cookie, self.state["csrf"])[0], 409)
        self.release.set()
        self.wait_workers()
        self.refresh()
        self.assertNotEqual(self.state["ai_request"]["status"], "pending")
        self.assertTrue(self.state["help_used"])
        self.assertEqual(self.state["chat"][-1]["mode"], "ai")
        self.assertEqual(self.state["chat"][-1]["content"], "先写出内层函数的导数。")
        serialized = json.dumps(self.calls, ensure_ascii=False)
        for forbidden in (self.state["participant"], self.state["csrf"], self.state["instance_id"],
                          self.server.ai_config.api_key, self.cookie.split("=", 1)[1]):
            self.assertNotIn(forbidden, serialized)
        for question in engine.QUESTIONS.values():
            if question.get("pool") != "practice":
                self.assertNotIn(question["id"], serialized)

    def test_cancel_then_reset_discards_late_reply_and_keeps_daily_charge(self):
        self.enter_training()
        old_participant = self.state["participant"]
        self.assertEqual(self.ask()[0], 202)
        self.assertTrue(self.entered.wait(2))
        request_id = self.state["ai_request"]["id"]
        self.assertEqual(self.mutate("cancel_ai", request_id=request_id)[0], 200)
        self.assertFalse(self.state["help_used"])
        self.assertEqual(self.state["chat"], [])
        status, self.state, _ = self.request("POST", "/api/reset", {"revision": self.state["revision"]},
                                           self.cookie, self.state["csrf"])
        self.assertEqual(status, 200)
        self.release.set()
        self.wait_workers()
        self.refresh()
        self.assertNotEqual(old_participant, self.state["participant"])
        self.assertEqual(self.state["ai_limits"]["daily_used"], 1)
        self.assertEqual(self.state["ai_limits"]["participant_used"], 0)
        self.assertEqual(self.state["chat"], [])
        old_quota = self.server.store.ai_limits(old_participant, self.server.ai_config)
        self.assertEqual(old_quota["participant_used"], 1)

    def test_failure_is_sanitized_and_still_charged_without_help(self):
        self.enter_training()
        self.server.ai_client = lambda *args: (_ for _ in ()).throw(RuntimeError(self.server.ai_config.api_key))
        self.assertEqual(self.ask()[0], 202)
        self.wait_workers()
        self.refresh()
        self.assertFalse(self.state["help_used"])
        self.assertEqual(self.state["chat"], [])
        self.assertTrue(self.state["ai_request"].get("error"))
        self.assertEqual(self.state["ai_limits"]["participant_used"], 1)
        for path in ("/api/state", "/api/health", "/api/export"):
            status, payload, _ = self.request("GET", path, cookie=self.cookie)
            self.assertEqual(status, 200)
            self.assertNotIn(self.server.ai_config.api_key, json.dumps(payload))
        self.assertEqual(self.request("GET", "/.env", cookie=self.cookie)[0], 404)
        self.assertEqual(self.request("GET", "/ai_tutor.py", cookie=self.cookie)[0], 404)

    def test_application_deadline_expires_and_drops_late_response(self):
        self.enter_training()
        self.assertEqual(self.ask()[0], 202)
        self.assertTrue(self.entered.wait(2))
        sid = self.cookie.split("=", 1)[1]
        with self.server.store.lock(sid):
            state, snapshot = self.server.store.load_snapshot(sid)
            state["ai_request"]["deadline"] = time.time() - 1
            self.server.store.save(sid, state, expected_snapshot=snapshot)
        self.refresh()
        self.assertEqual(self.state["ai_request"]["status"], "expired")
        self.release.set()
        self.wait_workers()
        self.refresh()
        self.assertFalse(self.state["help_used"])
        self.assertEqual(self.state["chat"], [])
        self.assertEqual(self.state["ai_limits"]["participant_used"], 1)

    def test_duplicate_request_and_limits_never_make_extra_provider_calls(self):
        self.enter_training()
        before = copy.deepcopy(self.state)
        self.server.ai_config = replace(self.server.ai_config, participant_limit=1)
        self.assertEqual(self.ask()[0], 202)
        self.assertTrue(self.entered.wait(2))
        duplicate = {"revision": before["revision"], "instance_id": before["instance_id"],
                     "message": "重复请求", "consent": True}
        self.assertEqual(self.request("POST", "/api/tutor", duplicate, self.cookie, before["csrf"])[0], 409)
        self.release.set()
        self.wait_workers()
        self.refresh()
        self.assertEqual(self.ask()[0], 429)
        self.assertEqual(len(self.calls), 1)
        persisted = server.SessionStore(self.db_path).ai_limits(self.state["participant"], self.server.ai_config)
        self.assertEqual(persisted["participant_used"], 1)

    def test_validation_disabled_and_assessment_do_not_charge_or_call(self):
        self.enter_training()
        for extra in ({"message": "x" * 601}, {"message": " "},
                      {"instance_id": "old-instance"}):
            with self.subTest(extra=extra):
                self.assertNotEqual(self.ask(**extra)[0], 202)
        self.server.ai_config = replace(self.server.ai_config, api_key="")
        self.assertEqual(self.ask()[0], 503)
        self.refresh()
        self.assertFalse(self.state["ai_enabled"])
        self.assertEqual(self.state["ai_limits"]["participant_used"], 0)
        self.assertEqual(self.calls, [])

    def test_second_server_observes_pending_without_replaying_and_can_cancel(self):
        self.enter_training()
        self.assertEqual(self.ask()[0], 202)
        self.assertTrue(self.entered.wait(2))
        other = server.LocalServer(port=0, db_path=self.db_path, ai_config=ai_tutor.TutorConfig())
        thread = threading.Thread(target=other.serve_forever, daemon=True)
        thread.start()
        try:
            status, state, _ = self.request("GET", "/api/state", cookie=self.cookie, port=other.server_port)
            self.assertEqual(status, 200)
            self.assertEqual(state["ai_request"]["status"], "pending")
            payload = {"action": "answer", "answer": "0", "revision": state["revision"],
                       "instance_id": state["instance_id"]}
            self.assertEqual(self.request("POST", "/api/action", payload, self.cookie,
                                          state["csrf"], port=other.server_port)[0], 409)
            payload.update(action="cancel_ai", request_id=state["ai_request"]["id"])
            self.assertEqual(self.request("POST", "/api/action", payload, self.cookie,
                                          state["csrf"], port=other.server_port)[0], 200)
            self.release.set()
            self.wait_workers()
            self.refresh()
            self.assertEqual(self.state["ai_request"]["status"], "cancelled")
            self.assertFalse(self.state["help_used"])
            self.assertEqual(self.state["chat"], [])
            self.assertEqual(len(self.calls), 1)
        finally:
            other.shutdown()
            other.server_close()
            thread.join(2)

    def test_expired_network_call_keeps_cross_server_capacity_until_worker_finishes(self):
        self.server.ai_config = replace(self.server.ai_config, concurrency=1)
        self.server.ai_slots = threading.BoundedSemaphore(1)
        self.enter_training()
        self.assertEqual(self.ask()[0], 202)
        self.assertTrue(self.entered.wait(2))
        sid = self.cookie.split("=", 1)[1]
        with self.server.store.lock(sid):
            state, snapshot = self.server.store.load_snapshot(sid)
            state["ai_request"]["deadline"] = time.time() - 1
            self.server.store.save(sid, state, expected_snapshot=snapshot)
            with self.server.store.connect() as connection:
                connection.execute("UPDATE ai_reservations SET deadline=?", (time.time() - 1,))
        self.refresh()
        self.assertEqual(self.state["ai_request"]["status"], "expired")
        other = server.LocalServer(port=0, db_path=self.db_path, ai_config=self.server.ai_config,
                                   ai_client=lambda *_: ("第二位的讲解。", {}))
        thread = threading.Thread(target=other.serve_forever, daemon=True)
        thread.start()
        try:
            _, state, headers = self.request("GET", "/api/state", port=other.server_port)
            cookie = headers["Set-Cookie"].split(";", 1)[0]
            for name, fields in (("start", {"minutes": 30}), ("select", None)):
                fields = fields or {"question_ref": state["catalog"]["questions"][0]["ref"]}
                status, state, _ = self.request("POST", "/api/action",
                    {"action": name, "revision": state["revision"], "instance_id": state.get("instance_id"), **fields},
                    cookie, state["csrf"], port=other.server_port)
                self.assertEqual(status, 200)
            payload = {"revision": state["revision"], "instance_id": state["instance_id"], "message": "讲解"}
            self.assertEqual(self.request("POST", "/api/tutor", payload, cookie,
                                          state["csrf"], port=other.server_port)[0], 503)
            self.assertEqual(other.store.ai_limits(state["participant"], other.ai_config)["participant_used"], 0)
            self.release.set()
            self.wait_workers()
            self.assertEqual(self.request("POST", "/api/tutor", payload, cookie,
                                          state["csrf"], port=other.server_port)[0], 202)
            with other.ai_guard:
                workers = list(other.ai_workers)
            for worker in workers:
                worker.join(3)
        finally:
            other.shutdown()
            other.server_close()
            thread.join(2)


class QuotaTransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "quota.sqlite3"
        self.store = server.SessionStore(self.path)
        self.config = ai_tutor.TutorConfig(api_key="fixture", min_interval_seconds=0)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def reserve(self, store, sid, name, snapshot=None, config=None):
        state, current = store.load_snapshot(sid)
        state["revision"] += 1
        store.reserve_ai(sid, state, expected_snapshot=current if snapshot is None else snapshot,
                         request_id=name, deadline=time.time() + 45, config=config or self.config)
        return state

    def test_stale_snapshot_rolls_back_every_quota_and_reservation(self):
        sid = self.store.create()
        _, snapshot = self.store.load_snapshot(sid)
        state = self.reserve(self.store, sid, "first")
        with self.assertRaises(server.ActionError) as error:
            self.reserve(self.store, sid, "stale", snapshot=snapshot)
        self.assertEqual(error.exception.status, 409)
        limits = self.store.ai_limits(state["participant"], self.config)
        self.assertEqual(limits["participant_used"], 1)
        self.assertEqual(limits["daily_used"], 1)
        with self.store.connect() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM ai_reservations").fetchone()[0], 1)

    def test_independent_stores_race_for_last_daily_allowance(self):
        other = server.SessionStore(self.path)
        sessions = [self.store.create(), other.create()]
        config = replace(self.config, daily_limit=1)
        barrier = threading.Barrier(2)

        def attempt(index):
            barrier.wait(3)
            try:
                self.reserve((self.store, other)[index], sessions[index], f"request-{index}", config=config)
                return 202
            except server.ActionError as exc:
                return exc.status

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, range(2)))
        self.assertEqual(sorted(results), [202, 429])
        with self.store.connect() as connection:
            self.assertEqual(connection.execute("SELECT sum(calls) FROM ai_participants").fetchone()[0], 1)

    def test_concurrent_capacity_is_durable_and_failure_release_does_not_refund(self):
        config = replace(self.config, concurrency=1)
        first, second = self.store.create(), self.store.create()
        state = self.reserve(self.store, first, "first", config=config)
        with self.assertRaises(server.ActionError) as error:
            self.reserve(server.SessionStore(self.path), second, "second", config=config)
        self.assertEqual(error.exception.status, 503)
        self.store.release_ai("first")
        self.reserve(self.store, second, "second", config=config)
        limits = self.store.ai_limits(state["participant"], config)
        self.assertEqual(limits["participant_used"], 1)
        self.assertEqual(limits["daily_used"], 2)

    def test_process_exit_reclaims_live_deadline_slot_without_refunding_quota(self):
        sid = self.store.create()
        config = replace(self.config, concurrency=1)
        code = """
import os,sys,time
import ai_tutor
from server import SessionStore
store=SessionStore(sys.argv[1])
state,snapshot=store.load_snapshot(sys.argv[2])
state['revision']+=1
store.reserve_ai(sys.argv[2],state,expected_snapshot=snapshot,request_id='crashed-worker',
                 deadline=time.time()+600,config=ai_tutor.TutorConfig(concurrency=1,min_interval_seconds=0))
print('reserved',flush=True)
sys.stdin.readline()
os._exit(0)
"""
        process = subprocess.Popen([sys.executable, "-c", code, str(self.path), sid],
                                   cwd=engine.ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(), "reserved")
            other = self.store.create()
            with self.assertRaises(server.ActionError) as error:
                self.reserve(self.store, other, "blocked", config=config)
            self.assertEqual(error.exception.status, 503)
            process.communicate("exit\n", timeout=10)
            self.assertEqual(process.returncode, 0)
            self.reserve(self.store, other, "after-exit", config=config)
            participant = self.store.load(sid)["participant"]
            limits = self.store.ai_limits(participant, config)
            self.assertEqual(limits["participant_used"], 1)
            self.assertEqual(limits["daily_used"], 2)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)

    def test_old_reservation_schema_upgrades_without_losing_quota(self):
        import sqlite3
        old_path = Path(self.temp.name) / "old.sqlite3"
        with sqlite3.connect(old_path) as connection:
            connection.execute("CREATE TABLE ai_reservations (id TEXT PRIMARY KEY,deadline REAL NOT NULL,finished INTEGER NOT NULL DEFAULT 0)")
            connection.execute("INSERT INTO ai_reservations VALUES ('old', ?, 0)", (time.time() - 10,))
        connection.close()
        old_store = server.SessionStore(old_path)
        try:
            sid = old_store.create()
            self.reserve(old_store, sid, "new", config=replace(self.config, concurrency=1))
            with old_store.connect() as connection:
                self.assertEqual(connection.execute("SELECT finished FROM ai_reservations WHERE id='old'").fetchone()[0], 1)
                self.assertTrue(connection.execute("SELECT owner_token FROM ai_reservations WHERE id='new'").fetchone()[0])
        finally:
            old_store.close()


if __name__ == "__main__":
    unittest.main()
