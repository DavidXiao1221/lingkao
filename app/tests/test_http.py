"""Exercise real HTTP requests against an isolated loopback server and database."""
from concurrent.futures import ThreadPoolExecutor
import copy
import http.client
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server
import engine


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "test.sqlite3"
        self.server = server.LocalServer(port=0, db_path=self.db_path)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_port
        status, state, headers = self.request("GET", "/api/state")
        self.assertEqual(status, 200)
        self.state = state
        self.cookie = headers["Set-Cookie"].split(";", 1)[0]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.temp.cleanup()

    def request(self, method, path, payload=None, cookie=None, token=None, headers=None, port=None):
        conn = http.client.HTTPConnection("127.0.0.1", port or self.port, timeout=15)
        request_headers = dict(headers or {})
        if cookie:
            request_headers["Cookie"] = cookie
        if token is not None:
            request_headers["X-CSRF-Token"] = token
        body = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
        try:
            conn.request(method, path, body=body, headers=request_headers)
            response = conn.getresponse()
            raw = response.read()
            response_headers = dict(response.getheaders())
            data = json.loads(raw) if "application/json" in response_headers.get("Content-Type", "") else raw
            return response.status, data, response_headers
        finally:
            conn.close()

    def mutate(self, name, state=None, **fields):
        state = state or self.state
        payload = {"action": name, "revision": state["revision"], "instance_id": state["instance_id"], **fields}
        result = self.request("POST", "/api/action", payload, self.cookie, state["csrf"])
        if result[0] == 200:
            self.state = result[1]
        return result

    def stored(self):
        sid = self.cookie.split("=", 1)[1]
        with self.server.store.lock(sid):
            return self.server.store.load(sid)

    def count_sessions(self):
        with self.server.store.connect() as connection:
            return connection.execute("SELECT count(*) FROM sessions").fetchone()[0]

    def enter_training(self):
        self.assertEqual(self.mutate("start", minutes=20)[0], 200)
        self.assertEqual(self.mutate("skip")[0], 200)
        self.assertEqual(self.mutate("start_skill")[0], 200)
        self.assertEqual(self.mutate("begin_practice")[0], 200)

    def test_health_and_static_do_not_create_sessions_or_enable_ai(self):
        with patch.dict("os.environ", {"DEEPSEEK_API_KEY": "not-a-real-key", "OPENAI_API_KEY": "not-a-real-key"}):
            status, data, _ = self.request("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertFalse(data["ai_enabled"])
        self.assertTrue(data["local_only"])
        self.assertEqual(self.request("GET", "/")[0], 200)
        self.assertEqual(self.count_sessions(), 1)

    def test_private_and_unknown_paths_do_not_create_session(self):
        for path in ("/.env", "/server.py", "/course.json", "/data/lingkao-alpha.sqlite3", "/../server.py", "/api/unknown"):
            with self.subTest(path=path):
                status, _, headers = self.request("GET", path)
                self.assertEqual(status, 404)
                self.assertNotIn("Set-Cookie", headers)
        self.assertEqual(self.request("POST", "/api/unknown", {})[0], 404)
        self.assertEqual(self.count_sessions(), 1)

    def test_cookie_sessions_are_isolated_and_refresh_preserves_instance(self):
        self.assertIn(server.COOKIE_NAME, self.cookie)
        self.assertEqual(self.mutate("start", minutes=20)[0], 200)
        status, refreshed, _ = self.request("GET", "/api/state", cookie=self.cookie)
        self.assertEqual(status, 200)
        self.assertEqual(refreshed["instance_id"], self.state["instance_id"])
        _, other, other_headers = self.request("GET", "/api/state")
        self.assertNotEqual(other["participant"], self.state["participant"])
        self.assertNotEqual(other["csrf"], self.state["csrf"])
        self.assertEqual(other["phase"], "setup")
        self.assertIn("HttpOnly", other_headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", other_headers["Set-Cookie"])

    def test_csrf_and_reset_rotation_and_stale_revision(self):
        old = copy.deepcopy(self.state)
        self.assertEqual(self.request("POST", "/api/reset", {"revision": old["revision"]}, self.cookie, "wrong")[0], 403)
        status, reset, _ = self.request("POST", "/api/reset", {"revision": old["revision"]}, self.cookie, old["csrf"])
        self.assertEqual(status, 200)
        self.assertEqual(reset["revision"], old["revision"] + 1)
        self.assertNotEqual(reset["csrf"], old["csrf"])
        self.assertNotEqual(reset["participant"], old["participant"])
        self.assertEqual(self.request("POST", "/api/reset", {"revision": old["revision"]}, self.cookie, old["csrf"])[0], 403)
        self.assertEqual(self.request("POST", "/api/reset", {"revision": old["revision"]}, self.cookie, reset["csrf"])[0], 409)
        self.assertEqual(self.stored()["participant"], reset["participant"])
        self.assertEqual(self.count_sessions(), 1)

    def test_assessment_export_and_teaching_endpoints_are_blocked(self):
        self.assertEqual(self.mutate("start", minutes=20)[0], 200)
        self.assertEqual(self.state["phase"], "diagnostic")
        self.assertFalse(self.state["export_allowed"])
        self.assertEqual(self.request("GET", "/api/export", cookie=self.cookie)[0], 409)
        for action_name in ("hint", "example", "solution", "foundation"):
            with self.subTest(action=action_name):
                self.assertNotEqual(self.mutate(action_name)[0], 200)
        payload = {"revision": self.state["revision"], "instance_id": self.state["instance_id"], "message": "第一步怎么选？"}
        self.assertNotEqual(self.request("POST", "/api/tutor", payload, self.cookie, self.state["csrf"])[0], 200)
        self.assertIsNone(self.state["feedback"])
        self.assertIsNone(self.state["lesson"])
        self.assertNotIn("qid", self.state)
        self.assertEqual(self.mutate("skip")[0], 200)
        status, exported, _ = self.request("GET", "/api/export", cookie=self.cookie)
        self.assertEqual(status, 200)
        serialized = json.dumps(exported)
        self.assertNotIn(self.state["csrf"], serialized)
        self.assertNotIn(self.cookie.split("=", 1)[1], serialized)
        self.assertNotIn("instance_id", serialized)

    def test_concurrent_double_post_is_applied_once(self):
        payload = {"action": "start", "minutes": 20, "revision": self.state["revision"]}
        barrier = threading.Barrier(2)
        def submit():
            barrier.wait(timeout=3)
            return self.request("POST", "/api/action", payload, self.cookie, self.state["csrf"])
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: submit(), range(2)))
        self.assertEqual(sorted(result[0] for result in results), [200, 409])
        self.assertEqual(self.stored()["revision"], self.state["revision"] + 1)

    def test_concurrent_resets_validate_csrf_after_lock(self):
        payload = {"revision": self.state["revision"]}
        barrier = threading.Barrier(2)
        def reset():
            barrier.wait(timeout=3)
            return self.request("POST", "/api/reset", payload, self.cookie, self.state["csrf"])
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: reset(), range(2)))
        self.assertEqual(sorted(result[0] for result in results), [200, 403])
        self.assertEqual(self.stored()["revision"], self.state["revision"] + 1)

    def shared_database_race(self, first_path, first_payload, second_payload):
        """Force two independent servers to read the same snapshot, then commit in order."""
        other = server.LocalServer(port=0, db_path=self.db_path)
        thread = threading.Thread(target=other.serve_forever, daemon=True)
        thread.start()
        ready = threading.Barrier(2)
        committed = threading.Event()
        real_save = server.SessionStore.save

        def coordinated_save(store, *args, **kwargs):
            ready.wait(timeout=8)
            if store is self.server.store:
                try:
                    return real_save(store, *args, **kwargs)
                finally:
                    committed.set()
            self.assertTrue(committed.wait(8))
            return real_save(store, *args, **kwargs)

        try:
            with patch.object(server.SessionStore, "save", coordinated_save), ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(self.request, "POST", first_path, first_payload, self.cookie, self.state["csrf"])
                second = pool.submit(self.request, "POST", "/api/action", second_payload,
                                     self.cookie, self.state["csrf"], port=other.server_port)
                results = first.result(), second.result()
            self.assertEqual([result[0] for result in results], [200, 409])
            self.state = results[0][1]
            return results
        finally:
            other.shutdown()
            other.server_close()
            thread.join(2)

    def enter_transfer(self):
        self.enter_training()
        while self.state["stage"] == "guided":
            self.assertEqual(self.mutate("skip")[0], 200)

    def test_two_servers_cannot_erase_help_with_concurrent_answer(self):
        self.enter_transfer()
        answer = engine.current_q(self.stored())["answer"]
        payload = {"revision": self.state["revision"], "instance_id": self.state["instance_id"]}
        before_count = len(self.stored()["attempts"])
        self.shared_database_race("/api/action", dict(payload, action="hint"),
                                  dict(payload, action="answer", answer=answer))
        self.assertEqual(len(self.stored()["attempts"]), before_count)
        self.assertEqual(len(self.state["hints"]), 1)
        self.assertTrue(self.state["help_used"])
        self.assertEqual(self.mutate("answer", answer=answer)[0], 200)
        attempt = self.stored()["attempts"][-1]
        self.assertTrue(attempt["assisted"])
        self.assertFalse(attempt["independent_complete"])

    def test_two_servers_cannot_erase_first_wrong_attempt(self):
        self.enter_transfer()
        answer = engine.current_q(self.stored())["answer"]
        payload = {"revision": self.state["revision"], "instance_id": self.state["instance_id"], "action": "answer"}
        before_count = len(self.stored()["attempts"])
        self.shared_database_race("/api/action", dict(payload, answer="0"), dict(payload, answer=answer))
        self.assertEqual(len(self.stored()["attempts"]), before_count + 1)
        self.assertEqual(self.stored()["attempts"][-1]["outcome"], "incorrect")
        self.assertEqual(self.mutate("answer", answer=answer)[0], 200)
        attempts = self.stored()["attempts"]
        self.assertEqual(len(attempts), before_count + 2)
        self.assertEqual([a["outcome"] for a in attempts[-2:]], ["incorrect", "correct"])
        self.assertFalse(attempts[-1]["first_attempt"])
        self.assertFalse(attempts[-1]["independent_complete"])

    def test_two_servers_reset_cannot_be_undone_by_inflight_answer(self):
        self.enter_transfer()
        old = copy.deepcopy(self.state)
        payload = {"revision": old["revision"], "instance_id": old["instance_id"],
                   "action": "answer", "answer": engine.current_q(self.stored())["answer"]}
        self.shared_database_race("/api/reset", {"revision": old["revision"]}, payload)
        stored = self.stored()
        self.assertEqual(stored["phase"], "setup")
        self.assertEqual(stored["attempts"], [])
        self.assertNotEqual(stored["participant"], old["participant"])
        self.assertNotEqual(stored["csrf"], old["csrf"])
        self.assertEqual(self.request("POST", "/api/action", payload, self.cookie, old["csrf"])[0], 403)

    def test_snapshot_check_survives_independent_process_and_legacy_json(self):
        sid = self.cookie.split("=", 1)[1]
        # Older snapshots need not use the current serializer's whitespace.
        with self.server.store.connect() as connection:
            connection.execute("UPDATE sessions SET data=? WHERE id=?",
                               (json.dumps(self.stored(), indent=2), sid))
        state, snapshot = self.server.store.load_snapshot(sid)
        code = """
import sys
from server import SessionStore
store = SessionStore(sys.argv[1])
state, snapshot = store.load_snapshot(sys.argv[2])
state['revision'] += 1
state['minutes'] = 45
store.save(sys.argv[2], state, expected_snapshot=snapshot)
"""
        subprocess.run([sys.executable, "-c", code, str(self.db_path), sid],
                       cwd=engine.ROOT, check=True, capture_output=True, timeout=15)
        state["revision"] += 1
        with self.assertRaises(server.ActionError) as error:
            self.server.store.save(sid, state, expected_snapshot=snapshot)
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(self.stored()["minutes"], 45)
        self.assertEqual(self.stored()["revision"], state["revision"])

    def test_exception_does_not_save_partial_mutations(self):
        before = self.stored()
        def failing_action(state, payload):
            state["revision"] += 10
            state["participant"] = "should-not-be-saved"
            raise server.ActionError("测试中止", status=409)
        with patch.object(server, "action", failing_action):
            self.assertEqual(self.mutate("start", minutes=20)[0], 409)
        self.assertEqual(before, self.stored())

    def test_get_waits_for_inflight_mutation(self):
        entered = threading.Event()
        release = threading.Event()
        real_action = server.action
        def slow_action(state, payload):
            entered.set()
            self.assertTrue(release.wait(3))
            real_action(state, payload)
        with patch.object(server, "action", slow_action), ThreadPoolExecutor(max_workers=2) as pool:
            mutation = pool.submit(self.mutate, "start", minutes=20)
            self.assertTrue(entered.wait(3))
            read = pool.submit(self.request, "GET", "/api/state", cookie=self.cookie)
            release.set()
            self.assertEqual(mutation.result()[0], 200)
            self.assertEqual(read.result()[1]["phase"], "diagnostic")

    def test_preview_is_read_only_and_stale_instance_rejected(self):
        self.enter_training()
        before = self.stored()
        payload = {"instance_id": self.state["instance_id"], "expression": "x^2+C"}
        status, result, _ = self.request("POST", "/api/preview", payload, self.cookie, self.state["csrf"])
        self.assertEqual(status, 200)
        self.assertTrue(result["valid"])
        self.assertIn("<math", result["mathml"])
        self.assertEqual(before, self.stored())
        payload["instance_id"] = "expired-instance"
        self.assertEqual(self.request("POST", "/api/preview", payload, self.cookie, self.state["csrf"])[0], 409)
        self.assertEqual(before, self.stored())

    def test_cancelled_response_writes_do_not_trigger_second_response_or_server_error(self):
        self.enter_training()
        before = self.stored()
        payload = {'instance_id': self.state['instance_id'], 'expression': 'x^2+C'}
        real_respond = server.Handler.respond
        real_end_headers = server.Handler.end_headers
        for location in ('headers', 'body'):
            for error in (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, TimeoutError):
                with self.subTest(location=location, error=error.__name__):
                    statuses = []
                    def record_response(handler, code, *args, **kwargs):
                        statuses.append(code)
                        return real_respond(handler, code, *args, **kwargs)
                    def disconnect(handler):
                        if location == 'headers':
                            raise error('client cancelled preview')
                        real_end_headers(handler)
                        handler.wfile.write = Mock(side_effect=error('client cancelled preview'))
                    with patch.object(server.Handler, 'respond', record_response), \
                         patch.object(server.Handler, 'end_headers', disconnect), \
                         patch.object(server, 'preview', return_value={'valid': True, 'mathml': '<math/>'}), \
                         patch.object(self.server, 'handle_error') as handle_error:
                        with self.assertRaises((http.client.HTTPException, ConnectionError, TimeoutError)):
                            self.request('POST', '/api/preview', payload, self.cookie, self.state['csrf'])
                        self.assertEqual(statuses, [200])
                        handle_error.assert_not_called()
                    self.assertEqual(before, self.stored())
        self.assertEqual(self.request('GET', '/api/health')[0], 200)

    def test_response_serialization_errors_are_not_suppressed(self):
        handler = Mock(spec=server.Handler)
        with self.assertRaises(TypeError):
            server.Handler.respond(handler, 200, {'not_json_serializable': object()})
        handler.end_headers.assert_not_called()

    def test_request_and_host_limits(self):
        self.assertEqual(self.request("GET", "/api/state", headers={"Host": "example.com"})[0], 403)
        self.assertEqual(self.request("POST", "/api/reset", {}, self.cookie, self.state["csrf"], {"Origin": "https://example.com"})[0], 403)
        self.assertEqual(self.request("POST", "/api/action", {"big": "x" * 17000}, self.cookie, self.state["csrf"])[0], 413)
        self.assertEqual(self.request("POST", "/api/action", [], self.cookie, self.state["csrf"])[0], 400)
        with patch.object(server, "POSTS_PER_MINUTE", 0):
            self.assertEqual(self.mutate("start", minutes=20)[0], 429)

    def test_restart_reads_durable_database(self):
        self.enter_training()
        self.assertEqual(self.mutate("hint")[0], 200)
        before = self.state
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.server = server.LocalServer(port=0, db_path=self.db_path)
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        status, after, _ = self.request("GET", "/api/state", cookie=self.cookie)
        self.assertEqual(status, 200)
        self.assertEqual(after["instance_id"], before["instance_id"])
        self.assertEqual(after["revision"], before["revision"])
        self.assertTrue(after["help_used"])


if __name__ == "__main__":
    unittest.main()
