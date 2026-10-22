# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Account review of an account with an assignment of another period
(spec 4.1, K-03, review E5). Synthetic data (D6)."""

from odoo.exceptions import UserError
from odoo.tests import tagged

from . import test_account_assignment as assignment_tests


@tagged("post_install_l10n", "post_install", "-at_install")
class TestAssignmentReviewPeriods(assignment_tests.TestAssignmentSkr03):
    # only the tests of this class, not those of SKR03 again
    allow_inherited_tests_method = False

    def test_proposal_not_applied_to_a_partly_assigned_account(self):
        """An account assigned until 2024 and used in 2026 is unassigned in
        the review of 2026. Its proposal is not applied silently: the user
        decides the periods of the account."""
        own = self.own_account(self.OWN_EXPENSE_START, "expense", "Own supplies")
        self.Assignment.create(
            {
                "company_id": self.company.id,
                "account_id": own.id,
                "category_id": self.categories["EXP_WORK_EQUIPMENT"].id,
                "date_to": "2024-12-31",
            }
        )
        self.entry([(own, 40.0), (self.bank, -40.0)])
        review = self.env["l10n_de_euer.account.review"].create(
            {"company_id": self.company.id, "year": assignment_tests.YEAR}
        )
        review.action_review()
        line = review.line_ids.filtered(lambda item: item.account_id == own)
        self.assertTrue(line.unassigned)
        self.assertTrue(line.proposal_id)
        with self.assertRaises(UserError):
            line.action_apply_proposal()
        self.assertEqual(self.Assignment.search_count([("account_id", "=", own.id)]), 1)
