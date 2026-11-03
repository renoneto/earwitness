"""JE-SETTINGS item 1: the members payload carries a display field, so the
browser never receives a raw account id as display text. The rule: the
member's own display name, else the email's local part (only where the
email is visible), else "Member". The id stays in `member` for controls."""
from __future__ import annotations

import unittest

from earwitness.app import access as A
from earwitness.app import collaboration as _collab
from earwitness.app import customer as _customer
from earwitness.app import db
from earwitness.app import services as _services
from tests import test_followup_d_display as fd

ISS = "oidc:https://127.0.0.1:9259:"
NAMED = ISS + "acct-" + "1" * 32
MAILED = ISS + "acct-" + "2" * 32
BARE = ISS + "acct-" + "3" * 32
ENTRY = {"row_namespace": "local", "workspace_id": "w"}


def _ctx(principal, kind="viewer"):
    return A.AccessContext.create(principal_id=principal, principal_kind=kind, workspace_id="local",
                                  credential_kind="browser_session")


class MemberDisplayTest(fd._Db):
    def _setup(self):
        con = self._con()
        for member in (NAMED, MAILED, BARE):
            inv = _collab.create_invite(con, "local", member, "editor", fd.OWNER)
            _collab.accept_invite(con, inv.get("invite_id") or inv.get("id"), member)
        db.audit(con, MAILED, "invite_accept_bound", "membership", MAILED,
                 {"workspace": "local", "bound_email": "sam.k@corp.example"}, 1)
        db.audit(con, NAMED, _customer.MEMBER_PREFS_ACTION, "settings", "preferences",
                 {"display_name": "  Pat Lee "}, 1)
        return con

    def _display(self, con, ctx):
        rows = _services.collab_members(ctx, con, ENTRY)["members"]
        return {r["member"]: r["display"] for r in rows}

    def test_display_follows_the_rule_and_never_the_id(self):
        con = self._setup()
        owner = self._display(con, _ctx(fd.OWNER, "owner"))
        self.assertEqual(owner[NAMED], "Pat Lee", "the member's own display name")
        self.assertEqual(owner[MAILED], "sam.k", "else the email's local part")
        self.assertEqual(owner[BARE], "Member", "else Member")
        self.assertEqual(owner[fd.OWNER], "Member")
        self_view = self._display(con, _ctx(MAILED))
        self.assertEqual(self_view[MAILED], "sam.k", "a member sees their own local part")
        other = self._display(con, _ctx(BARE))
        self.assertEqual(other[MAILED], "Member", "another member's hidden email is never used")
        self.assertEqual(other[NAMED], "Pat Lee")
        for view in (owner, self_view, other):
            for member, shown in view.items():
                self.assertNotIn("acct-", shown)
                self.assertNotIn(member, shown)

    def test_the_id_stays_as_the_control_value(self):
        con = self._setup()
        rows = _services.collab_members(_ctx(fd.OWNER, "owner"), con, ENTRY)["members"]
        self.assertLessEqual({NAMED, MAILED, BARE, fd.OWNER}, {r["member"] for r in rows})

    def test_route_payload_has_display(self):
        status, out = self.fixture._call(fd.MEMBERS, method="GET", headers=self.headers)
        self.assertEqual(status, 200, out)
        self.assertTrue(all(isinstance(m.get("display"), str) and m["display"] for m in out["members"]), out)


if __name__ == "__main__":
    unittest.main()
