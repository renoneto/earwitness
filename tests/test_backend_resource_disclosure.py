"""Resource scope and retained-resource policy consumer regressions."""
import json
import unittest

from earwitness.app import access, briefs, campaigns, citations, db, exports, services
from tests import backend_fixtures


class ResourceDisclosureRegression(unittest.TestCase):
    def setUp(self):
        self.con = backend_fixtures.make_memory_workspace()
        backend_fixtures.publish_sources(self.con)
        self.addCleanup(self.con.close)
        self.camp = campaigns.save_customer_campaign(self.con, {"campaign": {
            "name": "Private research", "mode": "research", "brand": "Lumen"}})["campaign"]
        self.brief = briefs.create_brief(self.con, {
            "title": "Private provided text", "provided_text": "An entered message."})
        self.entry = {"row_namespace": "local", "workspace_id": "local", "is_owner": False}

    def reader(self, scopes, policy=None):
        return access.AccessContext.create(principal_id="reader", principal_kind="viewer",
            workspace_id="local", credential_kind="reader_bearer", scopes=scopes,
            content_policy=policy)

    def deny_resource(self, key):
        self.con.execute("INSERT INTO source_content_policy"
            " (source_key,content_category,agent_disclosure,updated_at) VALUES (?,?,?,?)",
            (key, "campaign", "deny", db.utcnow()))

    def test_research_reader_cannot_read_campaign_or_provided_brief(self):
        ctx = self.reader({"research:read", "transcripts:read"})
        for getter in (lambda: services.customer_campaign_detail(ctx, self.con, self.entry, self.camp["id"]),
                       lambda: services.customer_brief_version(ctx, self.con, self.entry,
                                                              self.brief["brief_id"], 1)):
            with self.assertRaises(access.AuthorizationError) as denied:
                getter()
            self.assertEqual(denied.exception.status, 403)

    def test_resource_denial_applies_before_directory_count_or_text(self):
        ctx = self.reader({"campaigns:read"})
        self.deny_resource(f"campaign:{self.camp['id']}")
        self.deny_resource(f"brief:{self.brief['brief_id']}")
        directory = services.customer_campaigns(ctx, self.con, self.entry)
        self.assertNotIn(self.camp["id"], [row["id"] for row in directory["campaigns"]])
        selected = services.customer_briefs(ctx, self.con, self.entry, q="Private provided")
        self.assertEqual(selected["items"], [])
        self.assertEqual(selected["total"], 0)
        with self.assertRaises(access.AuthorizationError):
            services.customer_brief_version(ctx, self.con, self.entry, self.brief["brief_id"], 1)

    def test_captured_campaign_policy_loss_denies_every_stored_getter(self):
        draft = campaigns.build_customer_draft(self.con, self.camp["id"])
        frozen = campaigns.freeze_customer_report(self.con, self.camp["id"],
            draft["campaign"]["revision"], draft["pins"])
        before = self.con.execute("SELECT payload,digest,captured_json FROM exports WHERE id=?",
                                  (frozen["export_id"],)).fetchone()
        self.deny_resource(f"campaign:{self.camp['id']}")
        for getter in (lambda: campaigns.get_customer_report(self.con, self.camp["id"], frozen["report_no"]),
                       lambda: campaigns.get_customer_report_html(self.con, self.camp["id"], frozen["report_no"]),
                       lambda: exports.load_export_bytes(self.con, frozen["export_id"])):
            with self.assertRaises(access.AuthorizationError):
                getter()
        after = self.con.execute("SELECT payload,digest,captured_json FROM exports WHERE id=?",
                                 (frozen["export_id"],)).fetchone()
        self.assertEqual(tuple(after), tuple(before))
