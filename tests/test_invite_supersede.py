"""L22 (Astra L8D-R1-02): a pending invitation must not undo a removal or
a role downgrade (red-capable).

The sequence under test: the owner creates two invitations for B (for
example after an uncertain send), B accepts one, the owner removes or
downgrades B, then B accepts the other and regains membership or the
higher role. Rules:

- Newest wins: creating an invitation supersedes every older pending
  invitation for the same identity (the principal, any email bound to it
  in this workspace by a verified acceptance, and the session's verified
  email when it accepted by account). ASCII email addresses compare
  case-insensitively; other addresses and account ids exactly.
- Accepting an invitation supersedes the identity's other pending
  invitations, including one addressed to the session's verified email.
- Removing a member or changing a member's role supersedes the member's
  outstanding invitations, and acceptance refuses any invitation created
  before the latest membership decision about the accepting account. An
  invitation created after the decision still works.
- Acceptance enforces the 7-day expiry used at send.
- The owner can withdraw a pending invitation. A superseded or withdrawn
  invitation cannot be sent; an uncertain message for it stays listed.
"""
from __future__ import annotations

import json
import sqlite3
import unittest
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

from earwitness.app import collaboration as _collab
from earwitness.app import db as _db
from earwitness.app import invites_send as _send
from earwitness.app import monitoring as _mon
from earwitness.app import ops as _ops
from earwitness.app import services as _services
from tests.test_uniform_refusals import CARDS, FAKE_INVITE, ISSUER, OWNER, _Base

CAROL_EMAIL = "carol@example.test"
ERIN_EMAIL = "erin@example.test"


def _no_network(*_a, **_k):
    raise AssertionError("no provider call is expected in these tests")


class _Supersede(_Base):
    def setUp(self):
        super().setUp()
        self.erin = f"oidc:{ISSUER}:erin"
        self.bob = self.session("bob")

    # -- owner actions through the real routes ------------------------------
    def owner(self, path, method="POST", body=None):
        return self.http._call(path, method=method, body=body,
                               headers=self.http._csrf())

    def make_invite(self, member, role="viewer"):
        status, body = self.owner("/api/v1/workspaces/acme/invites",
                                  body={"member": member, "role": role})
        self.assertEqual(status, 200, body)
        return body["invite_id"]

    def remove(self, member):
        status, body = self.owner(f"/api/v1/workspaces/acme/members/{member}",
                                  method="DELETE")
        self.assertEqual(status, 200, body)

    def set_role(self, member, role):
        status, body = self.owner(f"/api/v1/workspaces/acme/members/{member}",
                                  method="PUT", body={"role": role})
        self.assertEqual(status, 200, body)

    def withdraw(self, invite, headers=None, workspace="acme"):
        path = f"/api/v1/workspaces/{workspace}/invites/{invite}/withdraw"
        if headers is None:
            return self.owner(path)
        return self.http._call(path, headers=headers)

    def pending(self):
        status, body = self.owner("/api/v1/workspaces/acme/invites", method="GET")
        self.assertEqual(status, 200, body)
        return [i["id"] for i in body["invites"]]

    # -- direct reads --------------------------------------------------------
    def role(self, member):
        con = self._db()
        try:
            return _collab.get_role(con, "local", member)
        finally:
            con.close()

    def status_of(self, invite):
        con = self._db()
        try:
            return con.execute("SELECT status FROM invites WHERE id=?",
                               (invite,)).fetchone()["status"]
        finally:
            con.close()

    def legacy_duplicate(self, member, role):
        """A second pending row as an older build left it: no supersede
        happened when it was created."""
        invite = f"invite:{uuid.uuid4().hex[:12]}"
        con = self._db()
        try:
            con.execute("INSERT INTO invites (id, workspace_id, member, role, status,"
                        " created_at) VALUES (?, 'local', ?, ?, 'created', ?)",
                        (invite, member, role,
                         datetime.now(timezone.utc).isoformat(timespec="seconds")))
        finally:
            con.close()
        return invite

    def refused(self, answer, message="invite is no longer pending"):
        status, body = answer
        self.assertEqual((status, body.get("error", {}).get("message")),
                         (400, message), body)


class RemovalAndDowngradeTest(_Supersede):
    def test_two_pending_accept_one_remove_accept_other_is_refused(self):
        first = self.legacy_duplicate(self.carol, "editor")
        second = self.legacy_duplicate(self.carol, "editor")
        carol = self.session("carol")
        status, body = self.accept(first, "acme", carol)
        self.assertEqual(status, 200, body)
        self.remove(self.carol)
        self.refused(self.accept(second, "acme", carol))
        self.assertIsNone(self.role(self.carol), "removal must stay effective")

    def test_two_invites_created_by_the_owner_accept_one_remove_accept_other(self):
        older = self.make_invite(self.carol, "editor")
        newer = self.make_invite(self.carol, "editor")
        carol = self.session("carol")
        status, body = self.accept(newer, "acme", carol)
        self.assertEqual(status, 200, body)
        self.remove(self.carol)
        self.refused(self.accept(older, "acme", carol))
        self.assertIsNone(self.role(self.carol))

    def test_downgrade_then_accept_old_higher_role_is_refused(self):
        first = self.legacy_duplicate(self.carol, "editor")
        second = self.legacy_duplicate(self.carol, "owner")
        carol = self.session("carol")
        self.assertEqual(self.accept(first, "acme", carol)[0], 200)
        self.set_role(self.carol, "viewer")
        self.refused(self.accept(second, "acme", carol))
        self.assertEqual(self.role(self.carol), "viewer")

    def test_removal_supersedes_the_members_outstanding_invitations(self):
        carol = self.session("carol")
        self.assertEqual(self.accept(self.invite, "acme", carol)[0], 200)
        # An invitation issued while carol is a member, then her removal.
        promote = self.make_invite(self.carol, "editor")
        self.remove(self.carol)
        self.assertEqual(self.status_of(promote), "superseded")
        self.assertNotIn(promote, self.pending())
        self.refused(self.accept(promote, "acme", carol))
        self.assertIsNone(self.role(self.carol))

    def test_role_change_supersedes_the_members_outstanding_invitations(self):
        carol = self.session("carol")
        self.assertEqual(self.accept(self.invite, "acme", carol)[0], 200)
        promote = self.make_invite(self.carol, "owner")
        self.set_role(self.carol, "viewer")
        self.assertEqual(self.status_of(promote), "superseded")
        self.refused(self.accept(promote, "acme", carol))
        self.assertEqual(self.role(self.carol), "viewer")

    def test_a_new_invitation_after_removal_is_accepted(self):
        carol = self.session("carol")
        self.assertEqual(self.accept(self.invite, "acme", carol)[0], 200)
        self.remove(self.carol)
        again = self.make_invite(self.carol, "editor")
        status, body = self.accept(again, "acme", carol)
        self.assertEqual(status, 200, body)
        self.assertEqual(self.role(self.carol), "editor")

    def test_a_new_invitation_after_downgrade_is_accepted(self):
        carol = self.session("carol")
        self.assertEqual(self.accept(self.invite, "acme", carol)[0], 200)
        self.set_role(self.carol, "editor")
        self.set_role(self.carol, "viewer")
        again = self.make_invite(self.carol, "editor")
        self.assertEqual(self.accept(again, "acme", carol)[0], 200)
        self.assertEqual(self.role(self.carol), "editor")


class NewestWinsTest(_Supersede):
    def test_a_new_invitation_supersedes_older_pending_ones(self):
        older = self.make_invite(self.carol, "owner")
        newer = self.make_invite(self.carol, "viewer")
        self.assertEqual(self.status_of(older), "superseded")
        self.assertNotIn(older, self.pending())
        self.assertIn(newer, self.pending())
        carol = self.session("carol")
        self.refused(self.accept(older, "acme", carol))
        self.assertIsNone(self.role(self.carol))
        self.assertEqual(self.accept(newer, "acme", carol)[0], 200)
        self.assertEqual(self.role(self.carol), "viewer")

    def test_new_invitations_for_other_people_are_untouched(self):
        self.make_invite(self.dave, "viewer")
        self.make_invite("someone@example.test", "viewer")
        self.assertEqual(self.status_of(self.invite), "created")
        self.assertEqual(self.status_of(self.by_email), "created")

    def test_accepting_supersedes_the_identitys_other_pending_invitations(self):
        by_email = self.make_invite(CAROL_EMAIL, "owner")
        carol = self.session("carol", email=CAROL_EMAIL, verified=True)
        self.assertEqual(self.accept(self.invite, "acme", carol)[0], 200)
        self.assertEqual(self.status_of(by_email), "superseded")
        self.refused(self.accept(by_email, "acme", carol))
        self.assertEqual(self.role(self.carol), "viewer")


class EmailAddressedTest(_Supersede):
    def test_unbound_email_invitation_from_before_removal_is_refused(self):
        # carol joined by account; an older invitation to her email stays
        # pending because nothing tied that email to her account yet.
        by_email = self.make_invite(CAROL_EMAIL, "editor")
        plain = self.session("carol", identity="carol-no-email")
        self.assertEqual(self.accept(self.invite, "acme", plain)[0], 200)
        self.remove(self.carol)
        verified = self.session("carol", email=CAROL_EMAIL, verified=True,
                                identity="carol-verified")
        self.refused(self.accept(by_email, "acme", verified))
        self.assertIsNone(self.role(self.carol))
        con = self._db()
        try:
            member = con.execute("SELECT member FROM invites WHERE id=?",
                                 (by_email,)).fetchone()["member"]
        finally:
            con.close()
        self.assertEqual(member, CAROL_EMAIL, "a refused acceptance binds nothing")

    def test_unbound_email_invitation_from_before_downgrade_is_refused(self):
        by_email = self.make_invite(CAROL_EMAIL, "owner")
        plain = self.session("carol", identity="carol-no-email")
        self.assertEqual(self.accept(self.invite, "acme", plain)[0], 200)
        self.set_role(self.carol, "editor")
        self.set_role(self.carol, "viewer")
        verified = self.session("carol", email=CAROL_EMAIL, verified=True,
                                identity="carol-verified")
        self.refused(self.accept(by_email, "acme", verified))
        self.assertEqual(self.role(self.carol), "viewer")

    def test_removal_supersedes_invitations_to_a_bound_email(self):
        erin = self.session("erin", email=ERIN_EMAIL, verified=True)
        self.assertEqual(self.accept(self.by_email, "acme", erin)[0], 200)
        promote = self.make_invite(ERIN_EMAIL, "owner")
        self.remove(self.erin)
        self.assertEqual(self.status_of(promote), "superseded")
        self.refused(self.accept(promote, "acme", erin))
        self.assertIsNone(self.role(self.erin))

    def test_a_new_email_invitation_for_a_bound_email_supersedes_the_principals(self):
        erin = self.session("erin", email=ERIN_EMAIL, verified=True)
        self.assertEqual(self.accept(self.by_email, "acme", erin)[0], 200)
        by_account = self.make_invite(self.erin, "owner")
        by_email = self.make_invite(ERIN_EMAIL, "viewer")
        self.assertEqual(self.status_of(by_account), "superseded")
        self.assertEqual(self.status_of(by_email), "created")

    def test_a_new_email_invitation_after_removal_is_accepted(self):
        erin = self.session("erin", email=ERIN_EMAIL, verified=True)
        self.assertEqual(self.accept(self.by_email, "acme", erin)[0], 200)
        self.remove(self.erin)
        again = self.make_invite(ERIN_EMAIL, "editor")
        status, body = self.accept(again, "acme", erin)
        self.assertEqual(status, 200, body)
        self.assertEqual(self.role(self.erin), "editor")


class VerifiedAliasTest(_Supersede):
    """Round 1, finding 1: a member who joined by account while the session
    carried a verified email keeps that email as part of its identity."""

    def join_with_alias(self):
        carol = self.session("carol", email=CAROL_EMAIL, verified=True)
        self.assertEqual(self.accept(self.invite, "acme", carol)[0], 200)
        return carol

    def alt(self):
        return self.session("carol-alt", email=CAROL_EMAIL, verified=True)

    def alt_role(self):
        return self.role(f"oidc:{ISSUER}:carol-alt")

    def test_removed_members_other_account_cannot_accept_a_pre_removal_email_invite(self):
        self.join_with_alias()
        promote = self.make_invite(CAROL_EMAIL, "owner")
        self.remove(self.carol)
        self.assertEqual(self.status_of(promote), "superseded")
        self.assertNotIn(promote, self.pending())
        self.refused(self.accept(promote, "acme", self.alt()))
        self.assertIsNone(self.alt_role())
        self.assertIsNone(self.role(self.carol))

    def test_role_change_supersedes_invites_to_the_verified_alias(self):
        self.join_with_alias()
        promote = self.make_invite(CAROL_EMAIL, "owner")
        self.set_role(self.carol, "editor")
        self.assertEqual(self.status_of(promote), "superseded")
        self.refused(self.accept(promote, "acme", self.alt()))
        self.assertIsNone(self.alt_role())

    def test_acceptance_refuses_a_pre_removal_alias_invite_the_removal_missed(self):
        # A row still pending although it predates the removal (for example
        # restored from a backup): acceptance is the second line of defence.
        self.join_with_alias()
        promote = self.make_invite(CAROL_EMAIL, "owner")
        self.remove(self.carol)
        con = self._db()
        try:
            con.execute("UPDATE invites SET status='created' WHERE id=?", (promote,))
        finally:
            con.close()
        self.refused(self.accept(promote, "acme", self.alt()))
        self.assertIsNone(self.alt_role())
        self.assertEqual(self.status_of(promote), "created", "a refusal changes nothing")

    def test_a_new_email_invite_after_removal_is_accepted_by_the_other_account(self):
        self.join_with_alias()
        self.remove(self.carol)
        again = self.make_invite(CAROL_EMAIL, "editor")
        status, body = self.accept(again, "acme", self.alt())
        self.assertEqual(status, 200, body)
        self.assertEqual(self.alt_role(), "editor")

    def test_the_alias_does_not_change_the_members_listed_email(self):
        self.join_with_alias()
        status, body = self.owner("/api/v1/workspaces/acme/members", method="GET")
        self.assertEqual(status, 200, body)
        listed = {m["member"]: m["email"] for m in body["members"]}
        self.assertEqual(listed[self.carol], "")


class CurrentVerifiedEmailTest(_Supersede):
    """Astra round 2, R2-01: acceptance counts the session's current
    verified email in the identity it checks, even on a first acceptance.
    Account A joins with verified email E; the owner invites account B
    directly; A is removed or downgraded; B, whose session also verifies
    E, must not accept the older invitation."""

    ALT = f"oidc:{ISSUER}:carol-alt"

    def setUp(self):
        super().setUp()
        carol = self.session("carol", email=CAROL_EMAIL, verified=True)
        self.assertEqual(self.accept(self.invite, "acme", carol)[0], 200)
        self.older = self.make_invite(self.ALT, "owner")

    def alt(self, email=CAROL_EMAIL, verified=True):
        return self.session("carol-alt", email=email, verified=verified,
                            identity=f"alt-{email}-{verified}")

    def alt_bindings(self):
        con = self._db()
        try:
            return [json.loads(r["detail"]) for r in con.execute(
                "SELECT detail FROM audit WHERE action='invite_accept_bound'"
                " AND target_id=?", (self.ALT,))]
        finally:
            con.close()

    def assert_refused_and_unchanged(self):
        self.refused(self.accept(self.older, "acme", self.alt()))
        self.assertIsNone(self.role(self.ALT), "no membership from a refusal")
        self.assertEqual(self.status_of(self.older), "created", "a refusal changes nothing")
        self.assertEqual(self.alt_bindings(), [], "a refusal records no alias")

    def test_first_acceptance_after_removal_with_the_removed_members_email(self):
        self.remove(self.carol)
        self.assertEqual(self.status_of(self.older), "created")
        self.assert_refused_and_unchanged()
        self.assertIsNone(self.role(self.carol))

    def test_first_acceptance_after_downgrade_with_the_members_email(self):
        self.set_role(self.carol, "editor")
        self.assert_refused_and_unchanged()
        self.assertEqual(self.role(self.carol), "editor")

    def test_unrelated_verified_email_accepts(self):
        self.remove(self.carol)
        status, body = self.accept(self.older, "acme",
                                   self.alt(email="dana@example.test"))
        self.assertEqual(status, 200, body)
        self.assertEqual(self.role(self.ALT), "owner")

    def test_unverified_email_is_not_an_alias_and_does_not_refuse(self):
        self.remove(self.carol)
        status, body = self.accept(self.older, "acme", self.alt(verified=False))
        self.assertEqual(status, 200, body)
        self.assertEqual(self.role(self.ALT), "owner")
        self.assertEqual([b.get("verified_alias") for b in self.alt_bindings()], [None])

    def test_invitation_created_after_the_decision_is_accepted(self):
        self.remove(self.carol)
        fresh = self.make_invite(self.ALT, "editor")
        status, body = self.accept(fresh, "acme", self.alt())
        self.assertEqual(status, 200, body)
        self.assertEqual(self.role(self.ALT), "editor")


class RemovedOwnersInvitesTest(_Supersede):
    """L22b review round 1, LOW-1: an invitation never outlives its
    creator's authority. Removing a member, or taking the owner role from
    it, supersedes the pending invitations that member created."""

    OWNER2 = f"oidc:{ISSUER}:owner2"
    BEA = f"oidc:{ISSUER}:bea"

    def setUp(self):
        super().setUp()
        con = self._db()
        try:
            _collab.accept_invite(con, _collab.create_invite(
                con, "local", self.OWNER2, "owner", OWNER)["invite_id"], self.OWNER2)
            self.by_owner2 = _collab.create_invite(
                con, "local", self.BEA, "owner", self.OWNER2)["invite_id"]
            self.by_owner = _collab.create_invite(
                con, "local", self.dave, "editor", OWNER)["invite_id"]
        finally:
            con.close()

    def test_removing_an_owner_supersedes_the_invites_it_created(self):
        self.remove(self.OWNER2)
        self.assertEqual(self.status_of(self.by_owner2), "superseded")
        self.assertNotIn(self.by_owner2, self.pending())
        self.refused(self.accept(self.by_owner2, "acme", self.session("bea")))
        self.assertIsNone(self.role(self.BEA))

    def test_the_remaining_owners_invites_still_work(self):
        self.remove(self.OWNER2)
        self.assertEqual(self.status_of(self.by_owner), "created")
        status, body = self.accept(self.by_owner, "acme", self.session("dave"))
        self.assertEqual(status, 200, body)
        self.assertEqual(self.role(self.dave), "editor")

    def test_downgrading_an_owner_supersedes_its_pending_owner_invites(self):
        self.set_role(self.OWNER2, "editor")
        self.assertEqual(self.status_of(self.by_owner2), "superseded")
        self.refused(self.accept(self.by_owner2, "acme", self.session("bea")))
        self.assertIsNone(self.role(self.BEA))
        self.assertEqual(self.status_of(self.by_owner), "created")


class AcceptanceRaceTest(_Supersede):
    """L22b review round 1, NIT: an invitation withdrawn between the
    acceptance's first read and its write lock is refused and grants
    nothing."""

    def test_withdraw_between_read_and_write_lock_is_refused(self):
        real = _db.transaction
        fired = []

        @contextmanager
        def withdraw_first(con, *, write):
            if not fired:
                fired.append(True)
                other = self._db()
                try:
                    _collab.withdraw_invite(other, "local", self.invite, OWNER)
                finally:
                    other.close()
            with real(con, write=write) as inner:
                yield inner

        ctx = SimpleNamespace(credential_kind="browser_session",
                              authority_kind="bootstrap",
                              scopes=frozenset({"invite:accept"}),
                              principal_id=self.carol)
        con = self._db()
        try:
            with mock.patch.object(_db, "transaction", withdraw_first):
                with self.assertRaises(_ops.BadInput) as raised:
                    _services.collab_invite_accept(
                        ctx, con, {"row_namespace": "local"}, self.invite,
                        CAROL_EMAIL)
        finally:
            con.close()
        self.assertEqual(fired, [True])
        self.assertEqual(str(raised.exception), "invite is no longer pending")
        self.assertIsNone(self.role(self.carol))
        self.assertEqual(self.status_of(self.invite), "withdrawn")


class EmailCaseTest(_Supersede):
    """Round 1, finding 3: ASCII email addresses compare case-insensitively;
    non-ASCII addresses and account ids stay exact (round 2, finding 1:
    Unicode folding would merge distinct addresses, as admission.py says)."""

    def test_unicode_folding_does_not_match_a_different_address(self):
        for label, invited, presented in (
                ("sharp s", "strasse@example.test", "straße@example.test"),
                ("kelvin sign", "kate@example.test", "Kate@example.test")):
            with self.subTest(case=label):
                invite = self.make_invite(invited, "owner")
                subject = "uni-" + label.replace(" ", "-")
                other = self.session(subject, email=presented, verified=True)
                self.assertEqual(self.accept(invite, "acme", other),
                                 CARDS["unknown_object"])
                self.assertIsNone(self.role(f"oidc:{ISSUER}:{subject}"))
                self.assertEqual(self.status_of(invite), "created")

    def test_unicode_folding_does_not_supersede_a_different_address(self):
        older = self.make_invite("strasse@example.test", "owner")
        self.make_invite("straße@example.test", "viewer")
        self.assertEqual(self.status_of(older), "created")

    def test_newest_wins_across_email_case(self):
        older = self.make_invite("Carol@Example.test", "owner")
        newer = self.make_invite(CAROL_EMAIL, "viewer")
        self.assertEqual(self.status_of(older), "superseded")
        self.assertEqual(self.status_of(newer), "created")

    def test_invitee_accepts_an_invite_addressed_in_another_case(self):
        invite = self.make_invite("Carol@Example.TEST", "editor")
        carol = self.session("carol", email=CAROL_EMAIL, verified=True)
        status, body = self.accept(invite, "acme", carol)
        self.assertEqual(status, 200, body)
        self.assertEqual(self.role(self.carol), "editor")

    def test_removal_supersedes_a_case_variant_of_a_bound_email(self):
        erin = self.session("erin", email=ERIN_EMAIL, verified=True)
        self.assertEqual(self.accept(self.by_email, "acme", erin)[0], 200)
        promote = self.make_invite("ERIN@example.test", "owner")
        self.remove(self.erin)
        self.assertEqual(self.status_of(promote), "superseded")

    def test_account_ids_stay_exact(self):
        other = self.make_invite(f"oidc:{ISSUER}:Carol", "owner")
        self.make_invite(self.carol, "viewer")
        self.assertEqual(self.status_of(other), "created")


class ExpiryTest(_Supersede):
    def age(self, invite, days):
        con = self._db()
        try:
            when = datetime.now(timezone.utc) - timedelta(days=days)
            con.execute("UPDATE invites SET created_at=? WHERE id=?",
                        (when.isoformat(timespec="seconds"), invite))
        finally:
            con.close()

    def test_expired_invitation_is_refused_at_acceptance(self):
        self.age(self.invite, 8)
        self.refused(self.accept(self.invite, "acme", self.session("carol")),
                     "invite has expired")
        self.assertIsNone(self.role(self.carol))

    def test_expired_email_invitation_is_refused_and_binds_nothing(self):
        self.age(self.by_email, 8)
        erin = self.session("erin", email=ERIN_EMAIL, verified=True)
        self.refused(self.accept(self.by_email, "acme", erin), "invite has expired")
        self.assertIsNone(self.role(self.erin))

    def test_invitation_inside_the_window_is_accepted(self):
        self.age(self.invite, 6)
        self.assertEqual(self.accept(self.invite, "acme", self.session("carol"))[0], 200)

    def test_expired_invitation_is_not_listed_as_pending(self):
        # Round 1, finding 4.
        self.age(self.invite, 8)
        self.age(self.by_email, 6)
        listed = self.pending()
        self.assertNotIn(self.invite, listed)
        self.assertIn(self.by_email, listed)

    def test_one_ttl_constant(self):
        self.assertIs(_send.INVITE_TTL, _collab.INVITE_TTL)
        self.assertEqual(_collab.INVITE_TTL, timedelta(days=7))


class WithdrawTest(_Supersede):
    def test_owner_withdraws_and_acceptance_is_refused(self):
        status, body = self.withdraw(self.invite)
        self.assertEqual(status, 200, body)
        self.assertEqual((body["invite_id"], body["status"]), (self.invite, "withdrawn"))
        self.assertEqual(self.status_of(self.invite), "withdrawn")
        self.assertNotIn(self.invite, self.pending())
        self.refused(self.accept(self.invite, "acme", self.session("carol")))
        self.assertIsNone(self.role(self.carol))

    def test_owner_keeps_its_specific_messages(self):
        self.refused(self.withdraw(self.consumed))
        self.withdraw(self.invite)
        self.refused(self.withdraw(self.invite))
        self.refused(self.withdraw(FAKE_INVITE), "unknown invite")

    def test_outsider_and_member_cannot_tell_real_from_fabricated(self):
        con = self._db()
        try:
            member = f"oidc:{ISSUER}:viewer2"
            _collab.accept_invite(con, _collab.create_invite(
                con, "local", member, "viewer", OWNER)["invite_id"], member)
        finally:
            con.close()
        for who, session in (("outsider", self.bob),
                             ("viewer", self.session("viewer2", active="acme"))):
            answers = {
                "real pending": self.withdraw(self.invite, session),
                "consumed": self.withdraw(self.consumed, session),
                "fabricated": self.withdraw(FAKE_INVITE, session),
            }
            if who == "outsider":
                # A member knows its own workspace exists; an outsider must
                # not learn that either.
                answers["fabricated workspace"] = self.withdraw(self.invite, session, "nope")
            first = next(iter(answers.values()))
            for label, answer in answers.items():
                with self.subTest(who=who, case=label):
                    self.assertEqual(answer, first)
                    self.assertIn(answer[0], (403, 404, 409))
        self.assertEqual(self.status_of(self.invite), "created")

    def test_bad_csrf_is_refused_before_lookup(self):
        headers = dict(self.http._csrf(), **{"x-csrf-token": "wrong"})
        status, body = self.http._call(
            f"/api/v1/workspaces/acme/invites/{self.invite}/withdraw", headers=headers)
        self.assertEqual((status, body["error"]["message"]),
                         (403, "valid CSRF token is required"))
        self.assertEqual(self.status_of(self.invite), "created")


class SendTest(_Supersede):
    """L3 message rows: a superseded or withdrawn invitation is not
    sendable; an uncertain message for it stays listed."""

    def mailer(self):
        provider = _mon.ResendDeliveryProvider("re_fake_test_key_not_live",
                                               "EarWitness <invites@example.test>",
                                               transport=_no_network)
        return _send.InviteMailer(provider, environment_id="test-a",
                                  public_base_url="http://127.0.0.1:8771")

    def send(self, invite):
        con = self._db()
        try:
            return _send.send_invite(con, workspace_id="local",
                                     hosted_workspace_id="acme", invite_id=invite,
                                     actor=OWNER, mailer=self.mailer())
        finally:
            con.close()

    def uncertain_message(self, invite):
        first = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(
            timespec="seconds")
        detail = {"kind": "workspace_invite", "invite_id": invite, "role": "viewer",
                  "status_reason": "send in progress",
                  "message": {"environment": "test-a", "nonce": "n" * 32,
                              "idempotency_key": "earwitness-invite/test-a/x/" + "n" * 32,
                              "body": "{}", "first_attempt_at": first,
                              "claimed_at": first, "attempts": 1}}
        con = self._db()
        try:
            return con.execute(
                "INSERT INTO notification_outbox (workspace_id, event_id, recipient,"
                " state, detail, created_at) VALUES ('local', NULL, ?, 'uncertain', ?, ?)",
                (ERIN_EMAIL, json.dumps(detail), first)).lastrowid
        finally:
            con.close()

    def unresolved(self):
        con = self._db()
        try:
            return [m["message_id"] for m in _send.unresolved_messages(con, "local")]
        finally:
            con.close()

    def test_superseded_invitation_is_not_sendable_and_its_message_stays_listed(self):
        message = self.uncertain_message(self.by_email)
        replacement = self.make_invite(ERIN_EMAIL, "viewer")
        self.assertEqual(self.status_of(self.by_email), "superseded")
        with self.assertRaises(_ops.BadInput) as caught:
            self.send(self.by_email)
        self.assertEqual(str(caught.exception), "invite is no longer pending")
        self.assertIn(message, self.unresolved())
        self.assertIn(replacement, self.pending())

    def test_withdrawn_invitation_is_not_sendable_and_its_message_stays_listed(self):
        message = self.uncertain_message(self.by_email)
        self.assertEqual(self.withdraw(self.by_email)[0], 200)
        with self.assertRaises(_ops.BadInput):
            self.send(self.by_email)
        self.assertIn(message, self.unresolved())


class StrangerTest(_Supersede):
    def test_stranger_cannot_tell_superseded_withdrawn_or_expired_from_fabricated(self):
        superseded = self.make_invite(self.dave, "editor")
        self.make_invite(self.dave, "viewer")
        withdrawn = self.make_invite("frank@example.test", "viewer")
        self.withdraw(withdrawn)
        con = self._db()
        try:
            con.execute("UPDATE invites SET created_at='2000-01-01T00:00:00+00:00'"
                        " WHERE id=?", (self.invite,))
        finally:
            con.close()
        for label, invite in (("superseded", superseded), ("withdrawn", withdrawn),
                              ("expired", self.invite), ("fabricated", FAKE_INVITE)):
            with self.subTest(case=label):
                self.assertEqual(self.accept(invite, "acme", self.bob),
                                 CARDS["unknown_object"])


if __name__ == "__main__":
    unittest.main()
