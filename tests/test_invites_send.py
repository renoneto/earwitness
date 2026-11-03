"""L3: explicit invite sending with transport idempotency (red-capable).

Every send goes through the real ResendDeliveryProvider with a fake HTTP
transport. The fake behaves like Resend's documented idempotency (same
key and same bytes: the original reply, no second email; same key and
other bytes: 409 invalid_idempotent_request; same key while in progress:
409 concurrent_idempotent_requests) and records every acceptance in a
JSON file in the test's temporary directory, so acceptance survives a
client process that dies and a fresh transport object. urllib is patched
to fail: no test can reach the network or a live key.
"""
from __future__ import annotations

import http.client
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from earwitness.app import access as _access
from earwitness.app import collaboration as _collab
from earwitness.app import db as _db
from earwitness.app import invites_send as _send
from earwitness.app import monitoring as _mon
from earwitness.app import ops as _ops
from tests import backend_fixtures

OWNER = "oidc:https://i.example:owner"
EDITOR = "oidc:https://i.example:editor"
INVITEE = "invitee@example.test"
FAKE_KEY = "re_fake_test_key_not_live"
SENDER = "EarWitness <invites@example.test>"
BASE_URL = "http://127.0.0.1:8771"
# Invites carry wall-clock creation times, so the test clock starts now.
T0 = datetime.now(timezone.utc).replace(microsecond=0)
REPO = Path(__file__).resolve().parent.parent


def _forbidden_network(*_a, **_k):
    raise AssertionError("network access is forbidden in invite tests")


class FakeResend:
    """Durable fake of the Resend send-email endpoint."""

    def __init__(self, state_path, mode="ok"):
        self.state_path = str(state_path)
        self.mode = mode
        self.lock = threading.Lock()
        self.entered = threading.Event()
        self.release = threading.Event()
        self.on_accept = None  # called after a new acceptance is recorded
        if not os.path.exists(self.state_path):
            self._save({"keys": {}, "accepted": [], "requests": []})

    def _load(self):
        with open(self.state_path, encoding="utf-8") as fh:
            return json.load(fh)

    def _save(self, state):
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.state_path)

    def state(self):
        with self.lock:
            return self._load()

    def __call__(self, url, headers, body, timeout):
        assert isinstance(body, bytes)
        mode = self.mode
        with self.lock:
            state = self._load()
            key = headers.get("Idempotency-Key", "")
            text = body.decode("utf-8")
            state["requests"].append({
                "url": url, "key": key, "body": text,
                "headers": {k: v for k, v in headers.items() if k != "Authorization"},
                "authorization": headers.get("Authorization", "")})
            if mode == "reject":
                self._save(state)
                return 422, b'{"name":"missing_required_field","message":"x"}'
            known = state["keys"].get(key) if key else None
            if known is not None:
                self._save(state)
                if known["body"] != text:
                    return 409, b'{"name":"invalid_idempotent_request"}'
                if known["in_progress"]:
                    return 409, b'{"name":"concurrent_idempotent_requests"}'
                return 200, json.dumps({"id": known["id"]}).encode()
            message_id = str(uuid.uuid4())
            record = {"body": text, "id": message_id, "in_progress": mode == "block"}
            if key:
                state["keys"][key] = record
            if mode != "block":
                state["accepted"].append({"key": key, "id": message_id, "body": text})
            self._save(state)
        if mode == "block":
            self.entered.set()
            assert self.release.wait(20), "blocked send was never released"
            with self.lock:
                state = self._load()
                state["keys"][key]["in_progress"] = False
                state["accepted"].append({"key": key, "id": message_id, "body": text})
                self._save(state)
        if self.on_accept is not None:
            self.on_accept()
        if mode == "die":
            os._exit(73)
        if mode == "slow":
            time.sleep(1.5)
        reply = json.dumps({"id": message_id}).encode()
        if mode == "truncated":
            return 200, reply[: len(reply) // 2]
        if mode == "invalid":
            return 200, b"<html>accepted</html>"
        if mode == "missing":
            return 200, b""
        if mode == "no_id":
            return 200, b"{}"
        if mode == "incomplete":
            raise http.client.IncompleteRead(reply[:5], len(reply) - 5)
        return 200, reply


def _open(path):
    con = sqlite3.connect(str(path), isolation_level=None, timeout=15.0)
    con.row_factory = sqlite3.Row
    return con


def _make_workspace(root, name="ws"):
    db_path = backend_fixtures.make_workspace(os.path.join(root, name), schema_version=8)
    con = _open(db_path)
    try:
        _collab.create_workspace(con, "local", "Acme", OWNER)
        inv = _collab.create_invite(con, "local", EDITOR, "editor", OWNER)
        _collab.accept_invite(con, inv["invite_id"], EDITOR)
    finally:
        con.close()
    return db_path


def _invite(con, member=INVITEE, role="viewer", namespace="local", actor=OWNER):
    return _collab.create_invite(con, namespace, member, role, actor)["invite_id"]


def _mailer(transport, env="test-a", clock=None):
    provider = _mon.ResendDeliveryProvider(FAKE_KEY, SENDER, transport=transport)
    return _send.InviteMailer(provider, environment_id=env,
                              public_base_url=BASE_URL, clock=clock)


def _outbox(con, outbox_id):
    row = con.execute("SELECT * FROM notification_outbox WHERE id=?", (outbox_id,)).fetchone()
    out = dict(row)
    out["detail"] = json.loads(out["detail"])
    return out


def _child_send(db_path, state_path, invite_id, now_iso):
    """Run in a separate process: the transport dies after acceptance."""
    fake = FakeResend(state_path, mode="die")
    now = datetime.fromisoformat(now_iso)
    con = _open(db_path)
    _send.send_invite(con, workspace_id="local", hosted_workspace_id="acme",
                      invite_id=invite_id, actor=OWNER,
                      mailer=_mailer(fake, clock=lambda: now))


class _Base(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch("urllib.request.urlopen", _forbidden_network)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.db_path = _make_workspace(self.root)
        self.state_path = os.path.join(self.root, "resend-acceptance.json")
        self.fake = FakeResend(self.state_path)
        self.now = T0
        self.mailer = _mailer(self.fake, clock=lambda: self.now)
        self.con = _open(self.db_path)
        self.addCleanup(self.con.close)

    def send(self, invite_id, con=None, mailer=None, actor=OWNER, workspace="local"):
        return _send.send_invite(con or self.con, workspace_id=workspace,
                                 hosted_workspace_id="acme", invite_id=invite_id,
                                 actor=actor, mailer=mailer or self.mailer)

    def recover(self, outbox_id, con=None, mailer=None, workspace="local"):
        return _send.recover_invite_message(con or self.con, workspace_id=workspace,
                                            outbox_id=outbox_id,
                                            mailer=mailer or self.mailer)

    def _copy_db(self, name):
        self.con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        target = os.path.join(self.root, name)
        con = _open(target)
        try:
            self.con.backup(con)
        finally:
            con.close()
        return target


class TransportBytesTest(_Base):
    def test_idempotency_header_and_stable_payload_bytes(self):
        invite_id = _invite(self.con)
        out = self.send(invite_id)
        self.assertEqual(out["status"], "sent", out)
        state = self.fake.state()
        self.assertEqual(len(state["requests"]), 1)
        self.assertEqual(len(state["accepted"]), 1)
        req = state["requests"][0]
        row = _outbox(self.con, out["message_id"])
        message = row["detail"]["message"]
        self.assertEqual(req["url"], "https://api.resend.com/emails")
        self.assertEqual(req["headers"]["Idempotency-Key"], message["idempotency_key"])
        self.assertEqual(req["headers"]["Content-Type"], "application/json")
        self.assertEqual(req["authorization"], f"Bearer {FAKE_KEY}")
        self.assertLessEqual(len(message["idempotency_key"]), 256)
        self.assertIn("test-a", message["idempotency_key"])
        self.assertIn(message["nonce"], message["idempotency_key"])
        self.assertEqual(req["body"], message["body"], "the stored bytes are the sent bytes")
        body = json.loads(req["body"])
        self.assertEqual(body["to"], [INVITEE])
        self.assertEqual(body["from"], SENDER)
        self.assertIn(f"{BASE_URL}/#/invite?workspace=acme&id={invite_id}", body["text"])
        self.assertNotIn("headers", body, "the key is a transport header, not an email header")
        self.assertNotIn("X-Entity-Ref-ID", req["body"])
        self.assertNotIn("exactly once", req["body"].lower())
        self.assertEqual(row["state"], "sent")
        self.assertEqual(row["detail"]["provider_message_id"], state["accepted"][0]["id"])
        self.assertEqual(row["detail"]["kind"], "workspace_invite")
        self.assertEqual(row["detail"]["invite_id"], invite_id)
        status = _send.invite_message_status(self.con, "local", out["message_id"])
        self.assertEqual(status["status"], "sent")

    def test_sent_invite_is_not_sent_again(self):
        invite_id = _invite(self.con)
        first = self.send(invite_id)
        again = self.send(invite_id)
        self.assertEqual((again["status"], again["message_id"]), ("sent", first["message_id"]))
        self.assertEqual(len(self.fake.state()["requests"]), 1)

    def test_queued_delivery_row_is_the_message_that_is_sent(self):
        invite_id = _invite(self.con)
        cur = self.con.execute(
            "INSERT INTO notification_outbox (workspace_id, event_id, recipient, state,"
            " detail, created_at) VALUES ('local', NULL, ?, 'pending', ?, ?)",
            (INVITEE, json.dumps({"kind": "workspace_invite", "invite_id": invite_id,
                                  "role": "viewer"}), T0.isoformat()))
        out = self.send(invite_id)
        self.assertEqual((out["status"], out["message_id"]), ("sent", cur.lastrowid))
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0], 1)

    def test_alert_deliver_classifies_a_truncated_2xx_as_uncertain(self):
        provider = _mon.ResendDeliveryProvider(FAKE_KEY, SENDER,
                                               transport=FakeResend(self.state_path, "truncated"))
        with self.assertRaises(_mon.ProviderUncertain):
            provider.deliver({"id": 1, "recipient": INVITEE, "event_id": 1, "detail": "{}"})


class ProcessDeathTest(_Base):
    def test_process_death_after_acceptance_recovers_without_a_second_email(self):
        invite_id = _invite(self.con)
        env = dict(os.environ, PYTHONPATH=str(REPO), PYTHONDONTWRITEBYTECODE="1")
        code = ("import sys; from tests.test_invites_send import _child_send;"
                " _child_send(*sys.argv[1:])")
        proc = subprocess.run([sys.executable, "-c", code, str(self.db_path),
                               self.state_path, invite_id, T0.isoformat()],
                              cwd=str(REPO), env=env, capture_output=True, timeout=120)
        self.assertEqual(proc.returncode, 73, proc.stderr.decode()[-2000:])
        accepted = self.fake.state()["accepted"]
        self.assertEqual(len(accepted), 1)
        row = self.con.execute("SELECT id, state FROM notification_outbox").fetchone()
        self.assertEqual(row["state"], "uncertain", "the write-ahead record survived the death")
        # Inside the in-flight lease nothing is replayed.
        early = self.recover(row["id"])
        self.assertEqual(early["status"], "uncertain")
        self.assertEqual(len(self.fake.state()["requests"]), 1)
        # A restarted client (fresh transport object over the same durable
        # record) replays the identical request under the same key.
        self.now = T0 + timedelta(minutes=10)
        restarted = _mailer(FakeResend(self.state_path), clock=lambda: self.now)
        out = self.recover(row["id"], mailer=restarted)
        self.assertEqual(out["status"], "sent", out)
        state = self.fake.state()
        self.assertEqual(len(state["accepted"]), 1, "no second email")
        self.assertEqual(out["provider_message_id"], accepted[0]["id"])
        first, replay = state["requests"]
        self.assertEqual((first["key"], first["body"]), (replay["key"], replay["body"]))


class OuterTransactionTest(_Base):
    def test_send_and_recover_refuse_a_caller_owned_transaction(self):
        invite_id = _invite(self.con)
        before = [tuple(r) for r in self.con.execute("SELECT * FROM notification_outbox")]
        with self.assertRaises(_send.OutwardSendInTransaction):
            with _db.transaction(self.con, write=True):
                self.send(invite_id)
        self.assertEqual(self.fake.state()["requests"], [])
        self.assertEqual([tuple(r) for r in self.con.execute("SELECT * FROM notification_outbox")],
                         before)
        self.fake.mode = "truncated"
        out = self.send(invite_id)
        with self.assertRaises(_send.OutwardSendInTransaction):
            with _db.transaction(self.con, write=True):
                self.recover(out["message_id"])
        self.assertEqual(len(self.fake.state()["requests"]), 1)


class ConcurrencyTest(_Base):
    def test_concurrent_send_and_recover_accept_once(self):
        invite_id = _invite(self.con)
        self.fake.mode = "block"
        result = {}

        def run_send():
            con = _open(self.db_path)
            try:
                result["a"] = self.send(invite_id, con=con)
            finally:
                con.close()

        thread = threading.Thread(target=run_send)
        thread.start()
        try:
            self.assertTrue(self.fake.entered.wait(20))
            self.fake.mode = "ok"
            other = _open(self.db_path)
            try:
                second = self.send(invite_id, con=other)
                self.assertEqual(second["status"], "uncertain")
                outbox_id = second["message_id"]
                self.assertEqual(self.recover(outbox_id, con=other)["status"], "uncertain")
                self.assertEqual(len(self.fake.state()["requests"]), 1,
                                 "inside the lease neither path calls the provider")
                self.now = T0 + timedelta(minutes=10)
                late = self.recover(outbox_id, con=other)
                self.assertEqual(late["status"], "uncertain", late)
            finally:
                other.close()
        finally:
            self.fake.release.set()
            thread.join(30)
        self.assertEqual(result["a"]["status"], "sent", result)
        self.assertEqual(len(self.fake.state()["accepted"]), 1)
        self.assertEqual(_outbox(self.con, outbox_id)["state"], "sent")
        self.assertEqual(self.recover(outbox_id)["status"], "sent")
        self.assertEqual(len(self.fake.state()["accepted"]), 1)


class AmbiguousBodyTest(_Base):
    def test_missing_invalid_truncated_2xx_bodies_are_uncertain_never_resent(self):
        modes = ("missing", "invalid", "truncated", "no_id", "incomplete")
        ids = {}
        for mode in modes:
            with self.subTest(mode=mode):
                self.fake.mode = mode
                invite_id = _invite(self.con, member=f"{mode}@example.test")
                out = self.send(invite_id)
                self.assertEqual(out["status"], "uncertain", out)
                self.assertFalse(out["unresolved"])
                again = self.send(invite_id)
                self.assertEqual((again["status"], again["message_id"]),
                                 ("uncertain", out["message_id"]))
                ids[mode] = out["message_id"]
        state = self.fake.state()
        self.assertEqual(len(state["requests"]), len(modes), "never retry-and-send")
        self.assertEqual(len(state["accepted"]), len(modes))
        self.fake.mode = "ok"
        self.now = T0 + timedelta(minutes=10)
        for mode, outbox_id in ids.items():
            with self.subTest(recover=mode):
                self.assertEqual(self.recover(outbox_id)["status"], "sent")
        state = self.fake.state()
        self.assertEqual(len(state["accepted"]), len(modes), "reconciliation sent nothing new")

    def test_definite_rejection_fails_and_a_new_message_gets_a_new_identity(self):
        invite_id = _invite(self.con)
        self.fake.mode = "reject"
        failed = self.send(invite_id)
        self.assertEqual(failed["status"], "failed", failed)
        self.fake.mode = "ok"
        sent = self.send(invite_id)
        self.assertEqual(sent["status"], "sent")
        self.assertNotEqual(sent["message_id"], failed["message_id"])
        k1 = _outbox(self.con, failed["message_id"])["detail"]["message"]["idempotency_key"]
        k2 = _outbox(self.con, sent["message_id"])["detail"]["message"]["idempotency_key"]
        self.assertNotEqual(k1, k2)
        self.assertEqual(_outbox(self.con, failed["message_id"])["state"], "failed")

    def test_a_refused_replay_does_not_become_failed(self):
        invite_id = _invite(self.con)
        self.fake.mode = "truncated"
        out = self.send(invite_id)
        self.fake.mode = "reject"
        self.now = T0 + timedelta(minutes=10)
        again = self.recover(out["message_id"])
        self.assertEqual(again["status"], "uncertain", "the first attempt may have been accepted")


class EnvironmentAndRestoreTest(_Base):

    def test_environments_never_share_a_key(self):
        invite_id = _invite(self.con)
        copy = self._copy_db("env-b.sqlite3")
        a = self.send(invite_id)
        con_b = _open(copy)
        try:
            mailer_b = _mailer(self.fake, env="test-b", clock=lambda: self.now)
            b = self.send(invite_id, con=con_b, mailer=mailer_b)
            self.assertEqual(a["message_id"], b["message_id"], "same row id in both copies")
            ka = _outbox(self.con, a["message_id"])["detail"]["message"]["idempotency_key"]
            kb = _outbox(con_b, b["message_id"])["detail"]["message"]["idempotency_key"]
        finally:
            con_b.close()
        self.assertNotEqual(ka, kb)
        self.assertIn("test-b", kb)
        self.assertEqual(len(self.fake.state()["accepted"]), 2)

    def test_another_environment_never_replays_a_restored_message(self):
        invite_id = _invite(self.con)
        self.fake.mode = "truncated"
        out = self.send(invite_id)
        copy = self._copy_db("restored-into-b.sqlite3")
        self.fake.mode = "ok"
        self.now = T0 + timedelta(minutes=10)
        con_b = _open(copy)
        try:
            mailer_b = _mailer(self.fake, env="test-b", clock=lambda: self.now)
            res = self.recover(out["message_id"], con=con_b, mailer=mailer_b)
            self.assertEqual(res["status"], "uncertain")
            again = self.send(invite_id, con=con_b, mailer=mailer_b)
            self.assertEqual(again["status"], "uncertain")
        finally:
            con_b.close()
        self.assertEqual(len(self.fake.state()["requests"]), 1)

    def test_restored_replay_keeps_identity_and_new_message_never_reuses_it(self):
        invite_id = _invite(self.con)
        before_any = self._copy_db("before-any.sqlite3")
        self.fake.mode = "truncated"
        out = self.send(invite_id)
        key = _outbox(self.con, out["message_id"])["detail"]["message"]["idempotency_key"]
        after_claim = self._copy_db("after-claim.sqlite3")
        self.fake.mode = "ok"
        self.now = T0 + timedelta(minutes=10)
        self.assertEqual(self.recover(out["message_id"])["status"], "sent")
        accepted_id = self.fake.state()["accepted"][0]["id"]
        # Restore the backup taken while the message was uncertain: the
        # replay keeps its identity and the provider returns the original.
        con_r = _open(after_claim)
        try:
            self.now = T0 + timedelta(minutes=20)
            res = self.recover(out["message_id"], con=con_r)
            self.assertEqual(res["status"], "sent")
            self.assertEqual(res["provider_message_id"], accepted_id)
            self.assertEqual(_outbox(con_r, out["message_id"])["detail"]["message"]["idempotency_key"], key)
        finally:
            con_r.close()
        self.assertEqual(len(self.fake.state()["accepted"]), 1)
        # Restore a backup from before the message existed: the new
        # message reuses the row id but never the identity.
        con_old = _open(before_any)
        try:
            new = self.send(invite_id, con=con_old)
            self.assertEqual(new["message_id"], out["message_id"])
            self.assertEqual(new["status"], "sent", new)
            new_key = _outbox(con_old, new["message_id"])["detail"]["message"]["idempotency_key"]
        finally:
            con_old.close()
        self.assertNotEqual(new_key, key)
        keys = [r["key"] for r in self.fake.state()["accepted"]]
        self.assertEqual(sorted(keys), sorted({key, new_key}))


class WindowTest(_Base):
    def test_ambiguous_message_past_the_window_is_never_resent(self):
        invite_id = _invite(self.con)
        self.fake.mode = "truncated"
        out = self.send(invite_id)
        self.fake.mode = "ok"
        for offset in (timedelta(hours=23, minutes=30), timedelta(hours=25), timedelta(days=6)):
            with self.subTest(offset=offset):
                self.now = T0 + offset
                res = self.recover(out["message_id"])
                self.assertEqual(res["status"], "uncertain")
                self.assertTrue(res["unresolved"], res)
                again = self.send(invite_id)
                self.assertEqual(again["status"], "uncertain")
        self.assertEqual(len(self.fake.state()["requests"]), 1)
        listed = _send.unresolved_messages(self.con, "local", now=self.now)
        self.assertEqual([m["message_id"] for m in listed], [out["message_id"]])
        self.assertEqual(listed[0]["invite_id"], invite_id)
        self.assertTrue(_outbox(self.con, out["message_id"])["detail"]["unresolved"])

    def test_identity_conflict_is_unresolved_without_replay(self):
        invite_id = _invite(self.con)
        self.fake.mode = "truncated"
        out = self.send(invite_id)
        # Corrupt the recorded acceptance body: the provider now reports
        # that this key was used with another payload.
        state = self.fake.state()
        key = state["requests"][0]["key"]
        state["keys"][key]["body"] = "{}"
        self.fake._save(state)
        self.fake.mode = "ok"
        self.now = T0 + timedelta(minutes=10)
        res = self.recover(out["message_id"])
        self.assertEqual(res["status"], "uncertain")
        self.assertTrue(res["unresolved"])
        self.now = T0 + timedelta(minutes=30)
        self.recover(out["message_id"])
        self.assertEqual(len(self.fake.state()["requests"]), 2)


class AuthorizationTest(_Base):
    def _count(self):
        return self.con.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0]

    def test_send_time_validity_and_authority(self):
        invite_id = _invite(self.con)
        with self.assertRaises(_access.AuthorizationError):
            self.send(invite_id, actor=EDITOR)
        with self.assertRaises(_access.AuthorizationError):
            self.send(invite_id, actor="oidc:https://i.example:stranger")
        accepted = _invite(self.con, member=EDITOR.replace("editor", "x"))
        _collab.accept_invite(self.con, accepted, EDITOR.replace("editor", "x"))
        expired = _invite(self.con, member="late@example.test")
        self.con.execute("UPDATE invites SET created_at=? WHERE id=?",
                         ((T0 - _send.INVITE_TTL - timedelta(minutes=1)).isoformat(), expired))
        revoked = _invite(self.con, member="gone@example.test")
        self.con.execute("UPDATE invites SET status='revoked' WHERE id=?", (revoked,))
        not_email = _invite(self.con, member="oidc:https://i.example:nomail")
        for bad in (accepted, expired, revoked, not_email, "invite:missing"):
            with self.subTest(invite=bad):
                with self.assertRaises(_ops.BadInput):
                    self.send(bad)
        with self.assertRaises(_ops.MissingPrecondition):
            _send.send_invite(self.con, workspace_id="local", hosted_workspace_id="acme",
                              invite_id=invite_id, actor=OWNER, mailer=None)
        # An owner whose membership is revoked may no longer send.
        _collab.create_invite(self.con, "local", "oidc:https://i.example:co", "owner", OWNER)
        co = self.con.execute("SELECT id FROM invites WHERE member=?",
                              ("oidc:https://i.example:co",)).fetchone()[0]
        _collab.accept_invite(self.con, co, "oidc:https://i.example:co")
        _collab.revoke_member(self.con, "local", "oidc:https://i.example:co", OWNER)
        with self.assertRaises(_access.AuthorizationError):
            self.send(invite_id, actor="oidc:https://i.example:co")
        self.assertEqual(self._count(), 0)
        self.assertEqual(self.fake.state()["requests"], [])

    def test_tenant_isolation(self):
        _collab.create_workspace(self.con, "other", "Other", "oidc:https://i.example:boss")
        foreign = _invite(self.con, namespace="other", actor="oidc:https://i.example:boss")
        with self.assertRaises((_access.AuthorizationError, _ops.BadInput)):
            self.send(foreign)
        foreign_out = _send.send_invite(self.con, workspace_id="other",
                                        hosted_workspace_id="other-ws", invite_id=foreign,
                                        actor="oidc:https://i.example:boss",
                                        mailer=self.mailer)
        self.assertEqual(foreign_out["status"], "sent")
        with self.assertRaises(_ops.BadInput):
            self.recover(foreign_out["message_id"])
        with self.assertRaises(_ops.BadInput):
            _send.invite_message_status(self.con, "local", foreign_out["message_id"])
        self.assertEqual(_send.unresolved_messages(self.con, "local"), [])
        mine = _invite(self.con)
        k_mine = _outbox(self.con, self.send(mine)["message_id"])["detail"]["message"]["idempotency_key"]
        k_foreign = _outbox(self.con, foreign_out["message_id"])["detail"]["message"]["idempotency_key"]
        self.assertNotEqual(k_mine.split("/")[2], k_foreign.split("/")[2])


class UnrelatedRowsTest(_Base):
    def test_unrelated_outbox_rows_untouched(self):
        other_invite = _invite(self.con, member="other@example.test")
        for state in ("pending", "queued", "uncertain", "failed", "sent"):
            self.con.execute(
                "INSERT INTO notification_outbox (workspace_id, event_id, recipient, state,"
                " detail, created_at) VALUES ('local', NULL, ?, ?, ?, ?)",
                (f"alert-{state}@example.test", state,
                 json.dumps({"kind": "customer_alert", "n": state}), T0.isoformat()))
        self.con.execute(
            "INSERT INTO notification_outbox (workspace_id, event_id, recipient, state,"
            " detail, created_at) VALUES ('local', NULL, 'other@example.test', 'pending', ?, ?)",
            (json.dumps({"kind": "workspace_invite", "invite_id": other_invite,
                         "role": "viewer"}), T0.isoformat()))
        self.con.execute(
            "INSERT INTO notification_outbox (workspace_id, event_id, recipient, state,"
            " detail, created_at) VALUES ('local', NULL, 'bad@example.test', 'pending', 'not json', ?)",
            (T0.isoformat(),))
        before = [tuple(r) for r in self.con.execute("SELECT * FROM notification_outbox ORDER BY id")]
        invite_id = _invite(self.con)
        self.fake.mode = "truncated"
        out = self.send(invite_id)
        self.fake.mode = "ok"
        self.now = T0 + timedelta(minutes=10)
        self.recover(out["message_id"])
        self.now = T0 + timedelta(days=2)
        _send.unresolved_messages(self.con, "local")
        after = [tuple(r) for r in self.con.execute(
            "SELECT * FROM notification_outbox WHERE id != ? ORDER BY id", (out["message_id"],))]
        self.assertEqual(after, before)


class ConfigurationTest(unittest.TestCase):
    FULL = {"EARWITNESS_INVITE_EMAIL_UNAPPROVED": "resend",
            "RESEND_API_KEY": FAKE_KEY, "EARWITNESS_RESEND_FROM": SENDER,
            "EARWITNESS_ENVIRONMENT_ID": "pilot-1",
            "EARWITNESS_PUBLIC_BASE_URL": "https://app.example.test"}

    def test_sending_is_off_unless_explicitly_configured(self):
        self.assertIsNone(_send.mailer_from_env({}))
        partial = dict(self.FULL)
        partial.pop("EARWITNESS_INVITE_EMAIL_UNAPPROVED")
        self.assertIsNone(_send.mailer_from_env(partial))
        for missing in ("RESEND_API_KEY", "EARWITNESS_RESEND_FROM",
                        "EARWITNESS_ENVIRONMENT_ID", "EARWITNESS_PUBLIC_BASE_URL"):
            env = dict(self.FULL)
            env.pop(missing)
            with self.subTest(missing=missing), self.assertRaises(ValueError):
                _send.mailer_from_env(env)
        with mock.patch("urllib.request.urlopen", _forbidden_network):
            mailer = _send.mailer_from_env(self.FULL)
        self.assertEqual(mailer.environment_id, "pilot-1")
        self.assertIsInstance(mailer.provider, _mon.ResendDeliveryProvider)
        with self.assertRaises(ValueError):
            _send.mailer_from_env(dict(self.FULL, EARWITNESS_ENVIRONMENT_ID="has/slash"))


class _RouteBase(unittest.TestCase):
    """The send route over the real HostedApp surface."""

    def setUp(self):
        from tests import test_backend_authority as _authority_tests
        patcher = mock.patch("urllib.request.urlopen", _forbidden_network)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.fixture = _authority_tests.HostedReaderHttpTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.headers = self.fixture._csrf()
        self.fake = FakeResend(os.path.join(self.fixture.tmp.name, "resend.json"))
        self.fixture.app.invite_mailer = _mailer(self.fake)
        status, out = self.fixture._call("/api/v1/workspaces/acme/invites",
                                         body={"member": INVITEE, "role": "viewer"},
                                         headers=self.headers)
        self.assertEqual(status, 200, out)
        self.invite_id = out["invite_id"]
        self.path = f"/api/v1/workspaces/acme/invites/{self.invite_id}/send"

    def _rows(self):
        con = sqlite3.connect(self.fixture.db_path)
        try:
            return [tuple(r) for r in con.execute("SELECT * FROM notification_outbox")]
        finally:
            con.close()


class RouteTest(_RouteBase):
    def test_owner_sends_and_the_list_shows_the_status(self):
        status, out = self.fixture._call(self.path, body={}, headers=self.headers)
        self.assertEqual(status, 200, out)
        self.assertEqual(out["status"], "sent")
        self.assertEqual(len(self.fake.state()["accepted"]), 1)
        status, listed = self.fixture._call("/api/v1/workspaces/acme/invites",
                                            method="GET", headers=self.headers)
        row = [i for i in listed["invites"] if i["id"] == self.invite_id][0]
        self.assertEqual(row["delivery"], "sent")

    def test_route_refusals_send_nothing(self):
        status, _ = self.fixture._call(self.path, body={}, headers={
            "authorization": f"Bearer {self.fixture.reader_token}"})
        self.assertEqual(status, 403)
        status, _ = self.fixture._call(
            f"/api/v1/workspaces/other/invites/{self.invite_id}/send", body={},
            headers=self.headers)
        self.assertEqual(status, 404)
        self.fixture.app.invite_mailer = None
        status, out = self.fixture._call(self.path, body={}, headers=self.headers)
        self.assertEqual(status, 428, out)
        self.assertEqual(self._rows(), [])
        self.assertEqual(self.fake.state()["requests"], [])


class EventLoopTest(_RouteBase):
    """MED-1: the provider call leaves the event loop."""

    async def _acall(self, path, method, headers, body=None):
        raw_body = json.dumps(body or {}).encode()
        hdrs = [(b"host", b"127.0.0.1:8771"),
                (b"content-length", str(len(raw_body)).encode())]
        hdrs.extend((k.encode(), v.encode()) for k, v in headers.items())
        scope = {"type": "http", "method": method, "path": path, "scheme": "http",
                 "headers": hdrs, "query_string": b""}
        sent = []

        async def receive():
            return {"type": "http.request", "body": raw_body, "more_body": False}

        async def send(message):
            sent.append(message)

        await self.fixture.app(scope, receive, send)
        return next(m["status"] for m in sent if m["type"] == "http.response.start")

    def test_an_unrelated_request_completes_while_a_slow_send_is_in_flight(self):
        import asyncio
        status, session = self.fixture._call("/api/v1/session", method="GET",
                                             headers=self.headers)
        self.assertEqual(status, 200)
        post_headers = dict(self.headers)
        post_headers["x-earwitness-workspace-switch-revision"] = str(
            session["workspace_switch_revision"])
        self.fake.mode = "slow"
        timings = {}

        async def scenario():
            start = time.monotonic()

            async def timed(name, coro):
                timings[name] = (await coro, time.monotonic() - start)

            send_task = asyncio.create_task(timed(
                "send", self._acall(self.path, "POST", post_headers)))
            await asyncio.sleep(0.1)
            await timed("session", self._acall("/api/v1/session", "GET", self.headers))
            await send_task

        asyncio.run(scenario())
        self.assertEqual(timings["send"][0], 200)
        self.assertEqual(timings["session"][0], 200)
        self.assertLess(timings["session"][1], 1.0, timings)
        self.assertGreaterEqual(timings["send"][1], 1.5, timings)
        self.assertEqual(len(self.fake.state()["accepted"]), 1)


class OperatorVisibilityTest(_Base):
    """MED-2: every stuck uncertain message is visible to an operator."""

    def test_an_uncertain_message_never_recovered_is_listed_after_the_lease(self):
        invite_id = _invite(self.con)
        self.fake.mode = "truncated"
        out = self.send(invite_id)
        self.assertEqual(_send.unresolved_messages(self.con, "local",
                                                   now=T0 + timedelta(seconds=30)), [])
        later = T0 + timedelta(days=3)
        listed = _send.unresolved_messages(self.con, "local", now=later)
        self.assertEqual([m["message_id"] for m in listed], [out["message_id"]])
        self.assertEqual(listed[0]["invite_id"], invite_id)
        self.assertEqual(listed[0]["age_s"], 3 * 86400)
        self.assertTrue(listed[0]["reason"])
        self.assertEqual(len(self.fake.state()["requests"]), 1, "listing sends nothing")

    def test_a_process_death_message_is_listed_with_a_clear_reason(self):
        invite_id = _invite(self.con)
        self.fake.mode = "incomplete"
        out = self.send(invite_id)
        self.con.execute(
            "UPDATE notification_outbox SET detail=json_set(detail, '$.status_reason',"
            " 'send in progress') WHERE id=?", (out["message_id"],))
        listed = _send.unresolved_messages(self.con, "local", now=T0 + timedelta(minutes=5))
        self.assertEqual(len(listed), 1)
        self.assertNotEqual(listed[0]["reason"], "send in progress")

    def test_another_environment_marks_the_message_unresolved(self):
        invite_id = _invite(self.con)
        self.fake.mode = "truncated"
        out = self.send(invite_id)
        copy = self._copy_db("env-b-visible.sqlite3")
        self.fake.mode = "ok"
        con_b = _open(copy)
        try:
            mailer_b = _mailer(self.fake, env="test-b", clock=lambda: self.now)
            for offset in (timedelta(minutes=10), timedelta(hours=25), timedelta(days=6)):
                with self.subTest(offset=offset):
                    self.now = T0 + offset
                    res = self.recover(out["message_id"], con=con_b, mailer=mailer_b)
                    self.assertEqual(res["status"], "uncertain")
                    self.assertTrue(res["unresolved"], res)
                    self.assertIn("test-a", res["reason"])
                    listed = _send.unresolved_messages(con_b, "local", now=self.now)
                    self.assertEqual([m["message_id"] for m in listed], [out["message_id"]])
                    self.assertTrue(listed[0]["unresolved"])
        finally:
            con_b.close()
        self.assertEqual(len(self.fake.state()["requests"]), 1)

    def test_resending_an_uncertain_message_says_reconcile(self):
        invite_id = _invite(self.con)
        self.fake.mode = "truncated"
        self.send(invite_id)
        again = self.send(invite_id)
        self.assertEqual(again["reason"], "reconcile the uncertain send before any resend")


class ClockInsideLockTest(_Base):
    """LOW-2: the send and recovery clocks are read under the write lock."""

    def test_now_is_read_inside_the_write_transaction(self):
        seen = []

        def clock():
            seen.append(self.con.in_transaction)
            return self.now

        mailer = _mailer(self.fake, clock=clock)
        invite_id = _invite(self.con)
        self.fake.mode = "truncated"
        out = self.send(invite_id, mailer=mailer)
        self.now = T0 + timedelta(minutes=10)
        self.recover(out["message_id"], mailer=mailer)
        self.assertTrue(seen)
        self.assertTrue(all(seen), seen)


class AlertOutboxRefusesInvitesTest(_Base):
    """LOW-1: the alert outbox functions never touch invite messages."""

    def test_alert_functions_refuse_invite_rows_without_writing(self):
        _mon.ensure_aux(self.con)
        queued_invite = _invite(self.con, member="queued@example.test")
        queued = self.con.execute(
            "INSERT INTO notification_outbox (workspace_id, event_id, recipient, state,"
            " detail, created_at) VALUES ('local', NULL, 'queued@example.test', 'pending', ?, ?)",
            (json.dumps({"kind": "workspace_invite", "invite_id": queued_invite,
                         "role": "viewer"}), T0.isoformat())).lastrowid
        self.fake.mode = "truncated"
        uncertain = self.send(_invite(self.con))["message_id"]
        self.fake.mode = "reject"
        failed = self.send(_invite(self.con, member="failed@example.test"))["message_id"]
        self.assertEqual(_outbox(self.con, failed)["state"], "failed")
        provider = _mon.ResendDeliveryProvider(FAKE_KEY, SENDER, transport=self.fake)
        before = [tuple(r) for r in self.con.execute("SELECT * FROM notification_outbox ORDER BY id")]
        requests = len(self.fake.state()["requests"])
        calls = [("attempt_delivery", lambda i: _mon.attempt_delivery(self.con, i, provider)),
                 ("recover_outbox", lambda i: _mon.recover_outbox(self.con, i, provider)),
                 ("reconcile_delivery", lambda i: _mon.reconcile_delivery(
                     self.con, i, "confirmed_sent", {"by": "test"})),
                 ("reconcile_delivery", lambda i: _mon.reconcile_delivery(
                     self.con, i, "confirmed_failed", {"by": "test"}))]
        for name, call in calls:
            for row_id in (queued, uncertain, failed):
                with self.subTest(fn=name, row=row_id):
                    with self.assertRaises(_ops.BadInput) as ctx:
                        call(row_id)
                    self.assertIn("invite", str(ctx.exception))
        after = [tuple(r) for r in self.con.execute("SELECT * FROM notification_outbox ORDER BY id")]
        self.assertEqual(after, before)
        self.assertEqual(len(self.fake.state()["requests"]), requests)


class AlertClassificationTest(unittest.TestCase):
    """NIT-2: the alert path's reply classification is pinned."""

    def _deliver(self, status, reply=b"{}", raises=None):
        seen = {}

        def transport(url, headers, body, timeout):
            seen.update(headers)
            if raises is not None:
                raise raises
            return status, reply

        provider = _mon.ResendDeliveryProvider(FAKE_KEY, SENDER, transport=transport)
        try:
            return provider.deliver({"id": 1, "recipient": INVITEE, "event_id": 1,
                                     "detail": "{}"})
        finally:
            self.assertNotIn("Idempotency-Key", seen)

    def test_ambiguous_replies_raise_provider_uncertain(self):
        cases = [(s, b'{"name":"x"}', None) for s in (409, 408, 410, 429, 500, 502, 503, 504)]
        cases += [(200, b"", None), (200, b"not json", None), (200, b"{}", None),
                  (200, b'{"id": ""}', None), (200, b'["id"]', None), (202, b'{"id"', None),
                  (200, b"", http.client.IncompleteRead(b"{", 5)),
                  (200, b"", TimeoutError("timed out"))]
        for status, reply, raises in cases:
            with self.subTest(status=status, reply=reply, raises=raises):
                with self.assertRaises(_mon.ProviderUncertain):
                    self._deliver(status, reply, raises)

    def test_definite_refusals_raise_value_error(self):
        for status in (400, 403, 422):
            with self.subTest(status=status):
                with self.assertRaises(ValueError) as ctx:
                    self._deliver(status, b'{"name":"validation_error"}')
                self.assertNotIsInstance(ctx.exception, _mon.ProviderUncertain)

    def test_a_readable_2xx_is_sent(self):
        out = self._deliver(200, b'{"id":"abc"}')
        self.assertEqual(out["message_id"], "abc")


class HealthConfigTest(unittest.TestCase):
    """NIT-3: production start-up checks the invite email settings."""

    FULL = dict(ConfigurationTest.FULL)

    def _problems(self, env, mode="production"):
        from earwitness.app import asgi as _asgi
        from earwitness.app import health as _health
        keys = list(self.FULL)
        with mock.patch.dict(os.environ, {}, clear=False):
            for key in keys:
                os.environ.pop(key, None)
            os.environ.update(env)
            problems = _health.check_config(_asgi.HostedConfig(mode=mode))
        return [p for p in problems if "INVITE" in p or "RESEND" in p
                or "PUBLIC_BASE_URL" in p or "ENVIRONMENT_ID" in p]

    def test_unset_invite_email_adds_no_problem(self):
        self.assertEqual(self._problems({}), [])

    def test_valid_https_configuration_passes(self):
        self.assertEqual(self._problems(self.FULL), [])

    def test_production_refuses_bad_invite_email_settings(self):
        cases = {"EARWITNESS_PUBLIC_BASE_URL": dict(self.FULL, EARWITNESS_PUBLIC_BASE_URL="http://app.example.test"),
                 "RESEND_API_KEY": {k: v for k, v in self.FULL.items() if k != "RESEND_API_KEY"},
                 "EARWITNESS_ENVIRONMENT_ID": dict(self.FULL, EARWITNESS_ENVIRONMENT_ID="a/b"),
                 "EARWITNESS_INVITE_EMAIL_UNAPPROVED": dict(self.FULL, EARWITNESS_INVITE_EMAIL_UNAPPROVED="smtp")}
        for name, env in cases.items():
            with self.subTest(name=name):
                problems = self._problems(env)
                self.assertTrue(any(name in p for p in problems), problems)

    def test_local_test_mode_skips_the_production_rule(self):
        env = dict(self.FULL, EARWITNESS_PUBLIC_BASE_URL="http://127.0.0.1:8771")
        self.assertEqual(self._problems(env, mode="local-test"), [])


class FollowUpTest(_Base):
    """Review round 2 follow-ups (NIT-B, NIT-A claim token)."""

    def test_a_second_send_inside_the_lease_says_send_in_progress(self):
        invite_id = _invite(self.con)
        self.fake.mode = "block"
        result = {}

        def run_send():
            con = _open(self.db_path)
            try:
                result["a"] = self.send(invite_id, con=con)
            finally:
                con.close()

        thread = threading.Thread(target=run_send)
        thread.start()
        try:
            self.assertTrue(self.fake.entered.wait(20))
            second = self.send(invite_id)
            self.assertEqual((second["status"], second["reason"]),
                             ("uncertain", "send in progress"))
            # The same in-flight record past the lease (a stopped process)
            # asks for reconciliation instead.
            self.now = T0 + _send.IN_FLIGHT_LEASE + timedelta(seconds=1)
            late = self.send(invite_id)
            self.assertEqual(late["reason"], "reconcile the uncertain send before any resend")
        finally:
            self.fake.release.set()
            thread.join(30)
        self.assertEqual(result["a"]["status"], "sent")
        self.assertEqual(len(self.fake.state()["requests"]), 1)

    def test_a_superseded_late_result_never_changes_the_row(self):
        invite_id = _invite(self.con)
        first = _send.prepare_send(self.con, workspace_id="local", hosted_workspace_id="acme",
                                   invite_id=invite_id, actor=OWNER, mailer=self.mailer)
        self.assertTrue(first.needs_transmit)
        self.now = T0 + timedelta(minutes=10)
        second = _send.prepare_recover(self.con, workspace_id="local",
                                       outbox_id=first.row_id, mailer=self.mailer)
        self.assertTrue(second.needs_transmit)
        self.assertNotEqual(first.token, second.token)
        before = tuple(self.con.execute("SELECT * FROM notification_outbox WHERE id=?",
                                        (first.row_id,)).fetchone())
        for outcome in ({"outcome": "uncertain", "replayable": False, "reason": "late 409"},
                        {"outcome": "failed", "reason": "late refusal"},
                        {"outcome": "uncertain", "replayable": True, "reason": "late timeout"}):
            with self.subTest(outcome=outcome["reason"]):
                out = first.finish(self.con, outcome)
                self.assertEqual(out["status"], "uncertain")
                after = tuple(self.con.execute("SELECT * FROM notification_outbox WHERE id=?",
                                               (first.row_id,)).fetchone())
                self.assertEqual(after, before)


class RouteGuardTest(_RouteBase):
    """NIT-A: the outward route's authority pre-check and final fence."""

    def _post_headers(self):
        status, session = self.fixture._call("/api/v1/session", method="GET",
                                             headers=self.headers)
        self.assertEqual(status, 200)
        return dict(self.headers, **{"x-earwitness-workspace-switch-revision":
                                     str(session["workspace_switch_revision"])})

    def _revoke_owner(self):
        con = sqlite3.connect(self.fixture.db_path, timeout=15)
        try:
            con.execute("UPDATE memberships SET revoked=1 WHERE member=?", (OWNER,))
            con.commit()
        finally:
            con.close()

    def test_a_member_revoked_before_the_send_is_refused_before_any_claim(self):
        from earwitness.app import services as _services
        headers = self._post_headers()
        real = _services._current_authority

        def revoking(*args, **kwargs):
            self._revoke_owner()  # revoked after admission, before the pre-check
            return real(*args, **kwargs)

        with mock.patch.object(_services, "_current_authority", side_effect=revoking), \
                mock.patch.object(_services, "collab_invite_send",
                                  wraps=_services.collab_invite_send) as handler:
            status, out = self.fixture._call(self.path, body={}, headers=headers)
        self.assertEqual(status, 403, out)
        self.assertEqual(out["error"]["message"], "workspace membership required")
        handler.assert_not_called()
        self.assertEqual(self._rows(), [])
        self.assertEqual(self.fake.state()["requests"], [])

    def test_authority_revoked_during_transmit_gives_403_and_the_send_stays_recorded(self):
        headers = self._post_headers()
        self.fake.on_accept = self._revoke_owner
        status, out = self.fixture._call(self.path, body={}, headers=headers)
        self.assertEqual(status, 403, out)
        self.assertEqual(len(self.fake.state()["accepted"]), 1)
        con = sqlite3.connect(self.fixture.db_path)
        try:
            states = [r[0] for r in con.execute("SELECT state FROM notification_outbox")]
        finally:
            con.close()
        self.assertEqual(states, ["sent"])


if __name__ == "__main__":
    unittest.main()
