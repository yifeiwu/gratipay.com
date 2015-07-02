from __future__ import print_function, unicode_literals

from gratipay.utils import fake_data
from gratipay.testing import Harness


class TestFakeData(Harness):
    """
    Ensure the fake_data script doesn't throw any exceptions
    """

    def test_fake_data(self):
        num_participants = 5
        num_tips = 5
        num_teams = 1
        num_transfers = 5
        num_subscriptions = 5
        num_payments = 5
        fake_data.populate_db(self.db, num_participants, num_tips, num_teams, num_transfers, num_subscriptions, num_payments)
        tips = self.db.all("SELECT * FROM tips")
        participants = self.db.all("SELECT * FROM participants")
        transfers = self.db.all("SELECT * FROM transfers")
        teams = self.db.all("SELECT * FROM teams")
        subscriptions = self.db.all("SELECT * FROM subscriptions")
        payments = self.db.all("SELECT * FROM payments")
        assert len(tips) == num_tips
        assert len(participants) == num_participants
        assert len(transfers) == num_transfers
        assert len(teams) == num_teams
        assert len(subscriptions) == num_subscriptions
        assert len(payments) == num_payments

