from __future__ import unicode_literals

import json
import mock
import pytest
from decimal import Decimal

from gratipay.testing import Harness
from gratipay.models.team import Team, AlreadyMigrated


REVIEW_URL = "https://github.com/gratipay/test-gremlin/issues/9"


class TestTeams(Harness):

    valid_data = {
        'name': 'Gratiteam',
        'product_or_service': 'We make widgets.',
        'homepage': 'http://gratipay.com/',
        'onboarding_url': 'http://inside.gratipay.com/',
        'todo_url': 'https://github.com/gratipay',
        'agree_public': 'true',
        'agree_payroll': 'true',
        'agree_terms': 'true',
    }

    def post_new(self, data, auth_as='alice', expected=200):
        r =  self.client.POST('/teams/create.json', data=data, auth_as=auth_as, raise_immediately=False)
        assert r.code == expected
        return r

    def test_harness_can_make_a_team(self):
        team = self.make_team()
        assert team.name == 'The Enterprise'
        assert team.owner == 'picard'

    def test_can_construct_from_slug(self):
        self.make_team()
        team = Team.from_slug('TheEnterprise')
        assert team.name == 'The Enterprise'
        assert team.owner == 'picard'

    def test_can_construct_from_id(self):
        team = Team.from_id(self.make_team().id)
        assert team.name == 'The Enterprise'
        assert team.owner == 'picard'

    @mock.patch('gratipay.models.team.Team.create_github_review_issue')
    def test_can_create_new_team(self, cgri):
        cgri.return_value = REVIEW_URL
        self.make_participant('alice', claimed_time='now', email_address='', last_paypal_result='')
        r = self.post_new(dict(self.valid_data))
        team = self.db.one("SELECT * FROM teams")
        assert team
        assert team.owner == 'alice'
        assert json.loads(r.body)['review_url'] == team.review_url

    def test_all_fields_persist(self):
        self.make_participant('alice', claimed_time='now', email_address='', last_paypal_result='')
        self.post_new(dict(self.valid_data))
        team = Team.from_slug('gratiteam')
        assert team.name == 'Gratiteam'
        assert team.homepage == 'http://gratipay.com/'
        assert team.product_or_service == 'We make widgets.'
        fallback = 'https://github.com/gratipay/team-review/issues#error-401'
        assert team.review_url in (REVIEW_URL, fallback)

    def test_casing_of_urls_survives(self):
        self.make_participant('alice', claimed_time='now', email_address='', last_paypal_result='')
        self.post_new(dict( self.valid_data
                          , homepage='Http://gratipay.com/'
                          , onboarding_url='http://INSIDE.GRATipay.com/'
                          , todo_url='hTTPS://github.com/GRATIPAY'
                           ))
        team = Team.from_slug('gratiteam')
        assert team.homepage == 'Http://gratipay.com/'
        assert team.onboarding_url == 'http://INSIDE.GRATipay.com/'
        assert team.todo_url == 'hTTPS://github.com/GRATIPAY'

    def test_401_for_anon_creating_new_team(self):
        self.post_new(self.valid_data, auth_as=None, expected=401)
        assert self.db.one("SELECT COUNT(*) FROM teams") == 0

    def test_error_message_for_no_valid_email(self):
        self.make_participant('alice', claimed_time='now')
        r = self.post_new(dict(self.valid_data), expected=400)
        assert self.db.one("SELECT COUNT(*) FROM teams") == 0
        assert "You must have a verified email address to apply for a new team." in r.body

    def test_error_message_for_no_payout_route(self):
        self.make_participant('alice', claimed_time='now', email_address='alice@example.com')
        r = self.post_new(dict(self.valid_data), expected=400)
        assert self.db.one("SELECT COUNT(*) FROM teams") == 0
        assert "You must attach a PayPal account to apply for a new team." in r.body

    def test_error_message_for_public_review(self):
        self.make_participant('alice', claimed_time='now', email_address='alice@example.com', last_paypal_result='')
        data = dict(self.valid_data)
        del data['agree_public']
        r = self.post_new(data, expected=400)
        assert self.db.one("SELECT COUNT(*) FROM teams") == 0
        assert "Sorry, you must agree to have your application publicly reviewed." in r.body

    def test_error_message_for_payroll(self):
        self.make_participant('alice', claimed_time='now', email_address='alice@example.com', last_paypal_result='')
        data = dict(self.valid_data)
        del data['agree_payroll']
        r = self.post_new(data, expected=400)
        assert self.db.one("SELECT COUNT(*) FROM teams") == 0
        assert "Sorry, you must agree to be responsible for payroll." in r.body

    def test_error_message_for_terms(self):
        self.make_participant('alice', claimed_time='now', email_address='alice@example.com', last_paypal_result='')
        data = dict(self.valid_data)
        del data['agree_terms']
        r = self.post_new(data, expected=400)
        assert self.db.one("SELECT COUNT(*) FROM teams") == 0
        assert "Sorry, you must agree to the terms of service." in r.body

    def test_error_message_for_missing_fields(self):
        self.make_participant('alice', claimed_time='now', email_address='alice@example.com', last_paypal_result='')
        data = dict(self.valid_data)
        del data['name']
        r = self.post_new(data, expected=400)
        assert self.db.one("SELECT COUNT(*) FROM teams") == 0
        assert "Please fill out the 'Team Name' field." in r.body

    def test_error_message_for_bad_url(self):
        self.make_participant('alice', claimed_time='now', email_address='alice@example.com', last_paypal_result='')

        r = self.post_new(dict(self.valid_data, homepage='foo'), expected=400)
        assert self.db.one("SELECT COUNT(*) FROM teams") == 0
        assert "Please enter an http[s]:// URL for the 'Homepage' field." in r.body

        r = self.post_new(dict(self.valid_data, onboarding_url='foo'), expected=400)
        assert "an http[s]:// URL for the 'Self-onboarding Documentation URL' field." in r.body

        r = self.post_new(dict(self.valid_data, todo_url='foo'), expected=400)
        assert "Please enter an http[s]:// URL for the 'To-do URL' field." in r.body

    def test_error_message_for_slug_collision(self):
        self.make_participant('alice', claimed_time='now', email_address='alice@example.com', last_paypal_result='')
        self.post_new(dict(self.valid_data))
        r = self.post_new(dict(self.valid_data), expected=400)
        assert self.db.one("SELECT COUNT(*) FROM teams") == 1
        assert "Sorry, there is already a team using 'gratiteam'." in r.body

    def test_approved_team_shows_up_on_homepage(self):
        self.make_team(is_approved=True)
        assert 'The Enterprise' in self.client.GET("/").body

    def test_unreviewed_team_shows_up_on_homepage(self):
        self.make_team(is_approved=None)
        assert 'The Enterprise' in self.client.GET("/").body

    def test_rejected_team_shows_up_on_homepage(self):
        self.make_team(is_approved=False)
        assert 'The Enterprise' in self.client.GET("/").body

    def test_stripping_required_inputs(self):
        self.make_participant('alice', claimed_time='now', email_address='alice@example.com', last_paypal_result='')
        data = dict(self.valid_data)
        data['name'] = "     "
        r = self.post_new(data, expected=400)
        assert self.db.one("SELECT COUNT(*) FROM teams") == 0
        assert "Please fill out the 'Team Name' field." in r.body

    def test_migrate_tips_to_payment_instructions(self):
        alice = self.make_participant('alice', claimed_time='now')
        bob = self.make_participant('bob', claimed_time='now')
        self.make_participant('old_team')
        self.make_tip(alice, 'old_team', '1.00')
        self.make_tip(bob, 'old_team', '2.00')
        new_team = self.make_team('new_team', owner='old_team')

        ntips = new_team.migrate_tips()
        assert ntips == 2

        payment_instructions = self.db.all("SELECT * FROM payment_instructions ORDER BY participant ASC")
        assert len(payment_instructions) == 2
        assert payment_instructions[0].participant == 'alice'
        assert payment_instructions[0].team == 'new_team'
        assert payment_instructions[0].amount == 1
        assert payment_instructions[1].participant == 'bob'
        assert payment_instructions[1].team == 'new_team'
        assert payment_instructions[1].amount == 2

    def test_migrate_tips_only_runs_once(self):
        alice = self.make_participant('alice', claimed_time='now')
        self.make_participant('old_team')
        self.make_tip(alice, 'old_team', '1.00')
        new_team = self.make_team('new_team', owner='old_team')

        new_team.migrate_tips()

        with pytest.raises(AlreadyMigrated):
            new_team.migrate_tips()

        payment_instructions = self.db.all("SELECT * FROM payment_instructions")
        assert len(payment_instructions) == 1

    def test_migrate_tips_checks_for_multiple_teams(self):
        alice = self.make_participant('alice', claimed_time='now')
        self.make_participant('old_team')
        self.make_tip(alice, 'old_team', '1.00')
        new_team = self.make_team('new_team', owner='old_team')
        new_team.migrate_tips()

        newer_team = self.make_team('newer_team', owner='old_team')

        with pytest.raises(AlreadyMigrated):
            newer_team.migrate_tips()

        payment_instructions = self.db.all("SELECT * FROM payment_instructions")
        assert len(payment_instructions) == 1


    # cached values - receiving, nreceiving_from

    def test_receiving_only_includes_funded_payment_instructions(self):
        alice = self.make_participant('alice', claimed_time='now', last_bill_result='')
        bob = self.make_participant('bob', claimed_time='now', last_bill_result="Fail!")
        team = self.make_team(is_approved=True)

        alice.set_payment_instruction(team, '3.00') # The only funded payment instruction
        bob.set_payment_instruction(team, '5.00')

        assert team.receiving == Decimal('3.00')
        assert team.nreceiving_from == 1

        funded_payment_instruction = self.db.one("SELECT * FROM payment_instructions "
                                                 "WHERE is_funded ORDER BY id")
        assert funded_payment_instruction.participant == alice.username

    def test_receiving_only_includes_latest_payment_instructions(self):
        alice = self.make_participant('alice', claimed_time='now', last_bill_result='')
        team = self.make_team(is_approved=True)

        alice.set_payment_instruction(team, '5.00')
        alice.set_payment_instruction(team, '3.00')

        assert team.receiving == Decimal('3.00')
        assert team.nreceiving_from == 1
