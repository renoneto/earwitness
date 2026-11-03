"""G2-01: remove an observation (an ad or passage) from a research campaign.

DELETE /api/v1/customer/campaigns/<id>/targets/<target>/evidence with
{ref, expected_revision} removes the one link a POST to the same path made.
Whoever can add can remove: the same permission function, CSRF, rate limit
and audit path as the attach. The evidence itself is untouched; a second
remove answers cleanly; Undo is the existing POST with the new revision.

Fixture data only (tests.test_customer_campaigns seed helpers).
"""
from __future__ import annotations

import json
import sqlite3
import unittest
from unittest import mock

from earwitness.app import access, campaigns, citations, services


class _Hosted(unittest.TestCase):
    def setUp(self):
        from tests.test_backend_http import HostedHttpTest
        self._harness = HostedHttpTest
        HostedHttpTest.setUp(self)
        self.path, self.cid, self.tid, self.ref, self.rev = self._fixture()

    def tearDown(self):
        self._harness.tearDown(self)

    def _call(self, *args, **kwargs):
        return self._harness._call(self, *args, **kwargs)

    def _db(self):
        con = sqlite3.connect(self.db_path, isolation_level=None)
        con.row_factory = sqlite3.Row
        return con

    def _fixture(self, ep="ep-g2", ev="ev-g2", name="BetterHelp ads", version_no=95,
                 actor="owner"):
        from tests.test_customer_campaigns import add_candidate, seed_episode
        con = self._db()
        try:
            seed_episode(con, ep_id=ep, version_no=version_no)
            add_candidate(con, ev, episode_id=ep)
            ref = citations.current_source_ref(con, "evidence", ev)
            camp = campaigns.save_customer_campaign(con, {"campaign": {
                "name": name, "mode": "research", "brand": "BetterHelp"}},
                actor=actor)["campaign"]
            target = campaigns.create_target(con, camp["id"], "episode", episode_id=ep,
                                             expected_campaign_revision=camp["revision"])
            rev = self._rev(con, camp["id"])
            campaigns.attach_target_evidence(con, camp["id"], target["id"], ref, rev)
            rev = self._rev(con, camp["id"])
            con.commit()
        finally:
            con.close()
        path = f"/api/v1/customer/campaigns/{camp['id']}/targets/{target['id']}/evidence"
        return path, camp["id"], target["id"], ref, rev

    @staticmethod
    def _rev(con, cid):
        return con.execute("SELECT revision FROM campaigns WHERE id=?", (cid,)).fetchone()[0]

    def _links(self, tid=None):
        con = self._db()
        try:
            return [r[0] for r in con.execute(
                "SELECT ref FROM target_evidence WHERE target_id=?", (tid or self.tid,))]
        finally:
            con.close()

    def _audit(self, action):
        con = self._db()
        try:
            return [dict(r) for r in con.execute(
                "SELECT * FROM audit WHERE action=? ORDER BY id", (action,))]
        finally:
            con.close()

    def _evidence_state(self):
        """Every row the evidence itself lives in: unchanged by a remove."""
        con = self._db()
        try:
            out = {}
            for table in ("evidence", "source_states"):
                out[table] = [tuple(r) for r in con.execute(f'SELECT * FROM "{table}" ORDER BY 1')]
            return out
        finally:
            con.close()

    def _remove(self, rev=None, **kw):
        return self._call(self.path, method="DELETE",
                          body={"ref": self.ref, "expected_revision": self.rev if rev is None else rev},
                          **kw)

    def _attach(self, rev=None, **kw):
        return self._call(self.path, method="POST",
                          body={"ref": self.ref, "expected_revision": self.rev if rev is None else rev},
                          **kw)

    def _editor(self, user="u-editor", sess="sess-editor"):
        from earwitness.app import collaboration as _collab
        from tests.test_backend_http import _register_session
        con = self._db()
        try:
            inv = _collab.create_invite(con, "local", f"oidc:https://i.example:{user}",
                                        "editor", "oidc:https://i.example:u-owner")
            _collab.accept_invite(con, inv["invite_id"], f"oidc:https://i.example:{user}")
            con.commit()
        finally:
            con.close()
        # The harness sends the viewer's CSRF token for any non-owner session.
        _register_session(self.app, sess, user, csrf="tok-viewer")
        return sess


class RemoveRouteTest(_Hosted):
    def test_the_owner_removes_one_link_and_the_evidence_stays(self):
        before = self._evidence_state()
        status, _h, data = self._remove()
        self.assertEqual(status, 200, data)
        self.assertEqual((data["target_id"], data["ref"], data["removed"]),
                         (self.tid, self.ref, True))
        self.assertEqual(data["campaign_revision"], self.rev + 1)
        self.assertEqual(self._links(), [])
        self.assertEqual(self._evidence_state(), before)

    def test_an_editor_removes_from_a_campaign_they_made(self):
        sess = self._editor()
        self.path, self.cid, self.tid, self.ref, self.rev = self._fixture(
            ep="ep-g2-own", ev="ev-g2-own", name="Editor's own", version_no=96,
            actor="oidc:https://i.example:u-editor")
        status, _h, data = self._remove(session=sess)
        self.assertEqual(status, 200, data)
        self.assertEqual(self._links(), [])
        row = self._audit("target_evidence_detach")[-1]
        self.assertEqual(row["actor"], "oidc:https://i.example:u-editor")

    def test_an_editor_who_did_not_make_the_campaign_is_refused_both_ways(self):
        """FR-11: add and remove keep one permission check, the campaign's
        maker or an owner."""
        sess = self._editor()
        status, _h, data = self._remove(session=sess)
        self.assertEqual(status, 403, data)
        self.assertEqual(data["error"]["message"], "Only the person who made this can change it.")
        self.assertEqual(self._links(), [self.ref])
        self.assertEqual(self._audit("target_evidence_detach"), [])
        self.assertEqual(self._attach(session=sess)[0], status)

    def test_the_remove_is_audited_like_the_attach(self):
        status, _h, data = self._remove()
        self.assertEqual(status, 200, data)
        attach = self._audit("target_evidence_attach")[-1]
        rows = self._audit("target_evidence_detach")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        # Same audit path as attach: who, when, campaign, target, evidence ref.
        self.assertEqual((row["target_type"], row["target_id"]), ("campaign", str(self.cid)))
        self.assertEqual(json.loads(row["detail"]), {"target": self.tid, "ref": self.ref})
        self.assertTrue(row["actor"])
        self.assertTrue(row["at"])
        self.assertEqual(set(attach), set(row))

    def test_a_viewer_is_refused_as_the_attach_refuses(self):
        status, _h, data = self._remove(session=self._viewer_id)
        self.assertEqual(status, 403, data)
        self.assertEqual(self._links(), [self.ref])
        self.assertEqual(self._audit("target_evidence_detach"), [])
        self.assertEqual(self._attach(session=self._viewer_id)[0], status)

    def test_someone_outside_the_workspace_is_refused_as_the_attach_refuses(self):
        from tests.test_backend_http import _register_session
        _register_session(self.app, "sess-stranger", "u-stranger", csrf="tok-viewer")
        status, _h, data = self._remove(session="sess-stranger")
        self.assertIn(status, (403, 404), data)
        self.assertEqual(self._links(), [self.ref])
        self.assertEqual(self._attach(session="sess-stranger")[0], status)

    def test_a_remove_without_csrf_is_refused(self):
        status, _h, data = self._remove(csrf=False)
        self.assertEqual(status, 403, data)
        self.assertIn("CSRF", data["_raw_body"])
        self.assertEqual(self._links(), [self.ref])

    def test_a_stale_revision_is_refused(self):
        status, _h, data = self._remove(rev=self.rev - 1)
        self.assertEqual((status, data["error"]["code"]), (412, "CONFLICT"), data)
        self.assertEqual(self._links(), [self.ref])

    def test_a_second_remove_answers_cleanly_and_changes_nothing(self):
        status, _h, first = self._remove()
        self.assertEqual(status, 200, first)
        audits = len(self._audit("target_evidence_detach"))
        # A retry with the same (now stale) revision: no 412, no new audit.
        status, _h, again = self._remove()
        self.assertEqual(status, 200, again)
        self.assertEqual((again["removed"], again["campaign_revision"]),
                         (False, first["campaign_revision"]))
        self.assertEqual(len(self._audit("target_evidence_detach")), audits)

    def test_undo_re_attaches_through_the_existing_post(self):
        status, _h, out = self._remove()
        self.assertEqual(status, 200, out)
        status, _h, back = self._attach(rev=out["campaign_revision"])
        self.assertEqual(status, 200, back)
        self.assertNotIn("duplicate", back)
        self.assertEqual(self._links(), [self.ref])

    def test_the_campaign_list_count_follows(self):
        def count():
            status, _h, data = self._call("/api/v1/customer/campaigns")
            self.assertEqual(status, 200, data)
            return next(c["observation_count"] for c in data["campaigns"] if c["id"] == self.cid)
        self.assertEqual(count(), 1)
        self.assertEqual(self._remove()[0], 200)
        self.assertEqual(count(), 0)

    def test_a_target_of_another_campaign_is_refused(self):
        other_path, other_cid, other_tid, other_ref, _rev = self._fixture(
            ep="ep-g2b", ev="ev-g2b", name="Other", version_no=94)
        # This campaign's id, the other campaign's target.
        path = f"/api/v1/customer/campaigns/{self.cid}/targets/{other_tid}/evidence"
        status, _h, data = self._call(path, method="DELETE",
                                      body={"ref": other_ref, "expected_revision": self.rev})
        self.assertEqual(status, 400, data)
        self.assertIn("unknown target", data["_raw_body"])
        self.assertEqual(self._links(other_tid), [other_ref])

    def test_only_this_targets_link_goes(self):
        # CR-R1-01: the same ad on a second target here and on a target of
        # another campaign keeps both links; the other campaign is untouched.
        con = self._db()
        try:
            second = campaigns.create_target(con, self.cid, "episode", episode_id="ep-g2",
                                             expected_campaign_revision=self.rev)
            campaigns.attach_target_evidence(con, self.cid, second["id"], self.ref,
                                             self._rev(con, self.cid))
            other = campaigns.save_customer_campaign(con, {"campaign": {
                "name": "Other ads", "mode": "research", "brand": "BetterHelp"}})["campaign"]
            far = campaigns.create_target(con, other["id"], "episode", episode_id="ep-g2",
                                          expected_campaign_revision=other["revision"])
            campaigns.attach_target_evidence(con, other["id"], far["id"], self.ref,
                                             self._rev(con, other["id"]))
            self.rev = self._rev(con, self.cid)
            other_rev = self._rev(con, other["id"])
            con.commit()
        finally:
            con.close()
        detach_before = len(self._audit("target_evidence_detach"))
        status, _h, data = self._remove()
        self.assertEqual((status, data.get("removed")), (200, True), data)
        self.assertEqual(self._links(), [])
        self.assertEqual(self._links(second["id"]), [self.ref])
        self.assertEqual(self._links(far["id"]), [self.ref])
        con = self._db()
        try:
            self.assertEqual(self._rev(con, other["id"]), other_rev)
        finally:
            con.close()
        rows = self._audit("target_evidence_detach")
        self.assertEqual(len(rows), detach_before + 1)
        self.assertEqual(rows[-1]["target_id"], str(self.cid))
        self.assertFalse([r for r in rows if r["target_id"] == str(other["id"])])

    def test_a_missing_ref_is_refused(self):
        status, _h, data = self._call(self.path, method="DELETE",
                                      body={"expected_revision": self.rev})
        self.assertEqual(status, 400, data)
        self.assertEqual(self._links(), [self.ref])

    def test_an_archived_campaign_keeps_its_observations(self):
        con = self._db()
        try:
            con.execute("UPDATE campaigns SET status='archived' WHERE id=?", (self.cid,))
            con.commit()
        finally:
            con.close()
        status, _h, data = self._remove()
        self.assertEqual(status, 409, data)
        self.assertEqual(self._links(), [self.ref])


class SamePermissionTest(unittest.TestCase):
    """Whoever can add can remove: one gate for both, never a second check."""

    def _gate_calls(self, fn):
        calls = []

        def gate(con, entry, ctx, action, operation):
            calls.append((action, operation))
            raise access.AuthorizationError("refused", 403)
        with mock.patch.object(services, "_require_customer_role", side_effect=gate):
            with self.assertRaises(access.AuthorizationError):
                fn(mock.Mock(), mock.Mock(), {"is_owner": False}, 7, "ct-1", "sr1:x", 3)
        return calls

    def test_remove_runs_the_attach_permission_check(self):
        detach = getattr(services, "customer_detach_target_evidence", None)
        self.assertIsNotNone(detach, "services.customer_detach_target_evidence is missing")
        attach = self._gate_calls(services.customer_attach_target_evidence)
        self.assertEqual(attach, [("organize", "customer_attach_target_evidence")])
        self.assertEqual(self._gate_calls(detach), attach)

    def test_a_share_guest_cannot_remove(self):
        detach = getattr(services, "customer_detach_target_evidence", None)
        self.assertIsNotNone(detach, "services.customer_detach_target_evidence is missing")
        ctx = mock.Mock(credential_kind="browser_session", authority_kind="share")
        con = mock.Mock()
        for fn in (services.customer_attach_target_evidence, detach):
            with self.assertRaises(access.AuthorizationError) as caught:
                fn(ctx, con, {"is_owner": False}, 7, "ct-1", "sr1:x", 3)
            self.assertEqual(caught.exception.status, 403)
        con.execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
