from __future__ import absolute_import, division, print_function, unicode_literals

from decimal import Decimal as D
import os

import balanced
import braintree
import mock
import pytest
from psycopg2 import IntegrityError

from gratipay.billing.exchanges import create_card_hold
from gratipay.billing.payday import NoPayday, Payday
from gratipay.exceptions import NegativeBalance
from gratipay.models.participant import Participant
from gratipay.testing import Foobar
from gratipay.testing.billing import BillingHarness
from gratipay.testing.emails import EmailHarness


class TestPayday(BillingHarness):

    def test_payday_moves_money(self):
        A = self.make_team(is_approved=True)
        self.obama.set_subscription_to(A, '6.00')  # under $10!
        with mock.patch.object(Payday, 'fetch_card_holds') as fch:
            fch.return_value = {}
            Payday.start().run()

        obama = Participant.from_username('obama')
        hannibal = Participant.from_username('hannibal')

        assert hannibal.balance == D('6.00')
        assert obama.balance == D('3.41')

    @mock.patch.object(Payday, 'fetch_card_holds')
    def test_payday_doesnt_move_money_from_a_suspicious_account(self, fch):
        self.db.run("""
            UPDATE participants
               SET is_suspicious = true
             WHERE username = 'obama'
        """)
        team = self.make_team(owner=self.homer, is_approved=True)
        self.obama.set_subscription_to(team, '6.00')  # under $10!
        fch.return_value = {}
        Payday.start().run()

        obama = Participant.from_username('obama')
        homer = Participant.from_username('homer')

        assert obama.balance == D('0.00')
        assert homer.balance == D('0.00')

    @mock.patch.object(Payday, 'fetch_card_holds')
    def test_payday_doesnt_move_money_to_a_suspicious_account(self, fch):
        self.db.run("""
            UPDATE participants
               SET is_suspicious = true
             WHERE username = 'homer'
        """)
        team = self.make_team(owner=self.homer, is_approved=True)
        self.obama.set_subscription_to(team, '6.00')  # under $10!
        fch.return_value = {}
        Payday.start().run()

        obama = Participant.from_username('obama')
        homer = Participant.from_username('homer')

        assert obama.balance == D('0.00')
        assert homer.balance == D('0.00')

    @pytest.mark.xfail(reason="haven't migrated transfer_takes yet")
    @mock.patch.object(Payday, 'fetch_card_holds')
    @mock.patch('gratipay.billing.payday.create_card_hold')
    def test_ncc_failing(self, cch, fch):
        self.janet.set_tip_to(self.homer, 24)
        fch.return_value = {}
        cch.return_value = (None, 'oops')
        payday = Payday.start()
        before = self.fetch_payday()
        assert before['ncc_failing'] == 0
        payday.payin()
        after = self.fetch_payday()
        assert after['ncc_failing'] == 1

    @pytest.mark.xfail(reason="#3399")
    def test_update_cached_amounts(self):
        team = self.make_participant('team', claimed_time='now', number='plural')
        alice = self.make_participant('alice', claimed_time='now', last_bill_result='')
        bob = self.make_participant('bob', claimed_time='now')
        carl = self.make_participant('carl', claimed_time='now', last_bill_result="Fail!")
        dana = self.make_participant('dana', claimed_time='now')
        emma = self.make_participant('emma')
        alice.set_tip_to(dana, '3.00')
        alice.set_tip_to(bob, '6.00')
        alice.set_tip_to(emma, '1.00')
        alice.set_tip_to(team, '4.00')
        bob.set_tip_to(alice, '5.00')
        team.add_member(bob)
        team.set_take_for(bob, D('1.00'), bob)
        bob.set_tip_to(dana, '2.00')  # funded by bob's take
        bob.set_tip_to(emma, '7.00')  # not funded, insufficient receiving
        carl.set_tip_to(dana, '2.08')  # not funded, failing card

        def check():
            alice = Participant.from_username('alice')
            bob = Participant.from_username('bob')
            carl = Participant.from_username('carl')
            dana = Participant.from_username('dana')
            emma = Participant.from_username('emma')
            assert alice.giving == D('13.00')
            assert alice.receiving == D('5.00')
            assert bob.giving == D('7.00')
            assert bob.receiving == D('7.00')
            assert bob.taking == D('1.00')
            assert carl.giving == D('0.00')
            assert carl.receiving == D('0.00')
            assert dana.receiving == D('5.00')
            assert dana.npatrons == 2
            assert emma.receiving == D('1.00')
            assert emma.npatrons == 1
            funded_tips = self.db.all("SELECT amount FROM tips WHERE is_funded ORDER BY id")
            assert funded_tips == [3, 6, 1, 4, 5, 2]

        # Pre-test check
        check()

        # Check that update_cached_amounts doesn't mess anything up
        Payday.start().update_cached_amounts()
        check()

        # Check that update_cached_amounts actually updates amounts
        self.db.run("""
            UPDATE tips SET is_funded = false;
            UPDATE participants
               SET giving = 0
                 , npatrons = 0
                 , receiving = 0
                 , taking = 0;
        """)
        Payday.start().update_cached_amounts()
        check()

    @pytest.mark.xfail(reason="#3399")
    def test_update_cached_amounts_depth(self):
        alice = self.make_participant('alice', claimed_time='now', last_bill_result='')
        usernames = ('bob', 'carl', 'dana', 'emma', 'fred', 'greg')
        users = [self.make_participant(username, claimed_time='now') for username in usernames]

        prev = alice
        for user in reversed(users):
            prev.set_tip_to(user, '1.00')
            prev = user

        def check():
            for username in reversed(usernames[1:]):
                user = Participant.from_username(username)
                assert user.giving == D('1.00')
                assert user.receiving == D('1.00')
                assert user.npatrons == 1
            funded_tips = self.db.all("SELECT id FROM tips WHERE is_funded ORDER BY id")
            assert len(funded_tips) == 6

        check()
        Payday.start().update_cached_amounts()
        check()

    @mock.patch('gratipay.billing.payday.log')
    def test_start_prepare(self, log):
        self.clear_tables()
        self.make_participant('bob', balance=10, claimed_time=None)
        self.make_participant('carl', balance=10, claimed_time='now')

        payday = Payday.start()

        get_participants = lambda c: c.all("SELECT * FROM payday_participants")

        with self.db.get_cursor() as cursor:
            payday.prepare(cursor)
            participants = get_participants(cursor)

        expected_logging_call_args = [
            ('Starting a new payday.'),
            ('Payday started at {}.'.format(payday.ts_start)),
            ('Prepared the DB.'),
        ]
        expected_logging_call_args.reverse()
        for args, _ in log.call_args_list:
            assert args[0] == expected_logging_call_args.pop()

        log.reset_mock()

        # run a second time, we should see it pick up the existing payday
        second_payday = Payday.start()
        with self.db.get_cursor() as cursor:
            payday.prepare(cursor)
            second_participants = get_participants(cursor)

        assert payday.ts_start == second_payday.ts_start
        participants = list(participants)
        second_participants = list(second_participants)

        # carl is the only valid participant as he has a claimed time
        assert len(participants) == 1
        assert participants == second_participants

        expected_logging_call_args = [
            ('Picking up with an existing payday.'),
            ('Payday started at {}.'.format(second_payday.ts_start)),
            ('Prepared the DB.'),
        ]
        expected_logging_call_args.reverse()
        for args, _ in log.call_args_list:
            assert args[0] == expected_logging_call_args.pop()

    def test_end(self):
        Payday.start().end()
        result = self.db.one("SELECT count(*) FROM paydays "
                             "WHERE ts_end > '1970-01-01'")
        assert result == 1

    def test_end_raises_NoPayday(self):
        with self.assertRaises(NoPayday):
            Payday().end()

    @mock.patch('gratipay.billing.payday.log')
    @mock.patch('gratipay.billing.payday.Payday.payin')
    def test_payday(self, payin, log):
        greeting = 'Greetings, program! It\'s PAYDAY!!!!'
        Payday.start().run()
        log.assert_any_call(greeting)
        assert payin.call_count == 1


class TestPayin(BillingHarness):

    def create_card_holds(self):
        payday = Payday.start()
        with self.db.get_cursor() as cursor:
            payday.prepare(cursor)
            return payday.create_card_holds(cursor)

    @mock.patch.object(Payday, 'fetch_card_holds')
    @mock.patch('braintree.Transaction.submit_for_settlement')
    @mock.patch('braintree.Transaction.sale')
    def test_payin_pays_in(self, sale, sfs, fch):
        fch.return_value = {}
        team = self.make_team('Gratiteam', is_approved=True)
        self.obama.set_subscription_to(team, 1)

        txn_attrs = {
            'amount': 1,
            'tax_amount': 0,
            'status': 'authorized',
            'custom_fields': {'participant_id': self.obama.id},
            'credit_card': {'token': self.obama_route.address},
            'id': 'dummy_id'
        }
        submitted_txn_attrs = txn_attrs.copy()
        submitted_txn_attrs.update(status='submitted_for_settlement')
        authorized_txn = braintree.Transaction(None, txn_attrs)
        submitted_txn = braintree.Transaction(None, submitted_txn_attrs)
        sale.return_value.transaction = authorized_txn
        sale.return_value.is_success = True
        sfs.return_value.transaction = submitted_txn
        sfs.return_value.is_success = True

        Payday.start().payin()
        payments = self.db.all("SELECT amount, direction FROM payments")
        assert payments == [(1, 'to-team'), (1, 'to-participant')]

    @mock.patch('braintree.Transaction.sale')
    def test_payin_doesnt_try_failed_cards(self, sale):
        team = self.make_team('Gratiteam', is_approved=True)
        self.obama_route.update_error('error')
        self.obama.set_subscription_to(team, 1)

        Payday.start().payin()
        assert not sale.called


    # fetch_card_holds - fch

    def test_fch_returns_an_empty_dict_when_there_are_no_card_holds(self):
        assert Payday.start().fetch_card_holds([]) == {}


    @mock.patch.object(Payday, 'fetch_card_holds')
    @mock.patch('gratipay.billing.payday.create_card_hold')
    def test_hold_amount_includes_negative_balance(self, cch, fch):
        self.db.run("""
            UPDATE participants SET balance = -10 WHERE username='obama'
        """)
        team = self.make_team('The A Team', is_approved=True)
        self.obama.set_subscription_to(team, 25)
        fch.return_value = {}
        cch.return_value = (None, 'some error')
        self.create_card_holds()
        assert cch.call_args[0][-1] == 35

    def test_payin_fetches_and_uses_existing_holds(self):
        team = self.make_team(owner=self.homer, is_approved=True)
        self.obama.set_subscription_to(team, '20.00')
        hold, error = create_card_hold(self.db, self.obama, D(20))
        assert hold is not None
        assert not error
        with mock.patch('gratipay.billing.payday.create_card_hold') as cch:
            cch.return_value = (None, None)
            self.create_card_holds()
            assert not cch.called, cch.call_args_list

    @mock.patch.object(Payday, 'fetch_card_holds')
    def test_payin_cancels_existing_holds_of_insufficient_amounts(self, fch):
        team = self.make_team(owner=self.homer, is_approved=True)
        self.obama.set_subscription_to(team, '30.00')
        hold, error = create_card_hold(self.db, self.obama, D(10))
        assert not error
        fch.return_value = {self.obama.id: hold}
        with mock.patch('gratipay.billing.payday.create_card_hold') as cch:
            fake_hold = object()
            cch.return_value = (fake_hold, None)
            holds = self.create_card_holds()
            hold = braintree.Transaction.find(hold.id)
            assert len(holds) == 1
            assert holds[self.obama.id] is fake_hold
            assert hold.status == 'voided'

    @pytest.mark.xfail(reason="Don't think we'll need this anymore since we aren't using balanced, "
                              "leaving it here till I'm sure.")
    @mock.patch('gratipay.billing.payday.CardHold')
    @mock.patch('gratipay.billing.payday.cancel_card_hold')
    def test_fetch_card_holds_handles_extra_holds(self, cancel, CardHold):
        fake_hold = mock.MagicMock()
        fake_hold.meta = {'participant_id': 0}
        fake_hold.amount = 1061
        fake_hold.save = mock.MagicMock()
        CardHold.query.filter.return_value = [fake_hold]
        for attr, state in (('failure_reason', 'failed'),
                            ('voided_at', 'cancelled'),
                            ('debit_href', 'captured')):
            holds = Payday.fetch_card_holds(set())
            assert fake_hold.meta['state'] == state
            fake_hold.save.assert_called_with()
            assert len(holds) == 0
            setattr(fake_hold, attr, None)
        holds = Payday.fetch_card_holds(set())
        cancel.assert_called_with(fake_hold)
        assert len(holds) == 0

    @pytest.mark.xfail(reason="haven't migrated transfer_takes yet")
    @mock.patch('gratipay.billing.payday.log')
    def test_payin_cancels_uncaptured_holds(self, log):
        self.janet.set_tip_to(self.homer, 42)
        alice = self.make_participant('alice', claimed_time='now',
                                      is_suspicious=False)
        self.make_exchange('balanced-cc', 50, 0, alice)
        alice.set_tip_to(self.janet, 50)
        Payday.start().payin()
        assert log.call_args_list[-3][0] == ("Captured 0 card holds.",)
        assert log.call_args_list[-2][0] == ("Canceled 1 card holds.",)
        assert Participant.from_id(alice.id).balance == 0
        assert Participant.from_id(self.janet.id).balance == 8
        assert Participant.from_id(self.homer.id).balance == 42

    def test_payin_cant_make_balances_more_negative(self):
        self.db.run("""
            UPDATE participants SET balance = -10 WHERE username='janet'
        """)
        payday = Payday.start()
        with self.db.get_cursor() as cursor:
            payday.prepare(cursor)
            cursor.run("""
                UPDATE payday_participants
                   SET new_balance = -50
                 WHERE username IN ('janet', 'homer')
            """)
            with self.assertRaises(NegativeBalance):
                payday.update_balances(cursor)

    @mock.patch.object(Payday, 'fetch_card_holds')
    @mock.patch('braintree.Transaction.sale')
    def test_card_hold_error(self, bt_sale, fch):
        team = self.make_team(owner=self.homer, is_approved=True)
        self.obama.set_subscription_to(team, '17.00')
        bt_sale.side_effect = Foobar
        fch.return_value = {}
        Payday.start().payin()
        payday = self.fetch_payday()
        assert payday['ncc_failing'] == 1

    def test_payin_doesnt_make_null_payments(self):
        team = self.make_team('Gratiteam', is_approved=True)
        alice = self.make_participant('alice', claimed_time='now')
        alice.set_subscription_to(team, 1)
        alice.set_subscription_to(team, 0)
        a_team = self.make_participant('a_team', claimed_time='now', number='plural')
        a_team.add_member(alice)
        Payday.start().payin()
        payments = self.db.all("SELECT * FROM payments WHERE amount = 0")
        assert not payments


    def test_payday_journal_updates_participant_and_team_balances_for_payroll(self):
        self.make_team(is_approved=True)
        assert Participant.from_username('hannibal').balance == 0

        payday = Payday.start()
        with self.db.get_cursor() as cursor:
            payday.prepare(cursor)

            cursor.run("UPDATE payday_teams SET balance=20 WHERE slug='TheATeam'")
            cursor.run("""
                INSERT INTO payday_journal
                            (amount, debit, credit)
                     VALUES ( 10.77
                            , (SELECT id FROM accounts WHERE team='TheATeam')
                            , (SELECT id FROM accounts WHERE participant='hannibal')
                             )
            """)
            assert cursor.one("SELECT balance FROM payday_teams "
                              "WHERE slug='TheATeam'") == D('9.23')
            assert cursor.one("SELECT new_balance FROM payday_participants "
                              "WHERE username='hannibal'") == D('10.77')
            assert self.db.one("SELECT balance FROM participants "
                               "WHERE username='hannibal'") == 0

    def test_payday_journal_updates_participant_and_team_balances_for_subscriptions(self):
        self.make_team(is_approved=True)
        self.make_participant('alice', claimed_time='now', balance=20)

        payday = Payday.start()
        with self.db.get_cursor() as cursor:
            payday.prepare(cursor)

            cursor.run("""
                INSERT INTO payday_journal
                            (amount, debit, credit)
                     VALUES ( 10.77
                            , (SELECT id FROM accounts WHERE participant='alice')
                            , (SELECT id FROM accounts WHERE team='TheATeam')
                             )
            """)
            assert cursor.one("SELECT balance FROM payday_teams "
                              "WHERE slug='TheATeam'") == D('10.77')
            assert cursor.one("SELECT new_balance FROM payday_participants "
                              "WHERE username='alice'") == D('9.23')

    def test_payday_journal_disallows_negative_payday_team_balance(self):
        self.make_team()
        assert Participant.from_username('hannibal').balance == 0

        payday = Payday.start()
        with self.db.get_cursor() as cursor:
            payday.prepare(cursor)
            cursor.run("UPDATE payday_teams SET balance=10 WHERE slug='TheATeam'")
            with pytest.raises(IntegrityError):
                cursor.run("""
                    INSERT INTO payday_journal
                                (amount, debit, credit)
                         VALUES ( 10.77
                                , (SELECT id FROM accounts WHERE team='TheATeam')
                                , (SELECT id FROM accounts WHERE participant='hannibal')
                                 )
                """)

    def test_payday_journal_disallows_negative_payday_participant_balance(self):
        self.make_team()
        self.make_participant('alice', claimed_time='now', balance=10)

        payday = Payday.start()
        with self.db.get_cursor() as cursor:
            payday.prepare(cursor)
            with pytest.raises(IntegrityError):
                cursor.run("""
                    INSERT INTO payday_journal
                                (amount, debit, credit)
                         VALUES ( 10.77
                                , (SELECT id FROM accounts WHERE participant='alice')
                                , (SELECT id FROM accounts WHERE team='TheATeam')
                                 )
                """)


    def test_process_subscriptions(self):
        alice = self.make_participant('alice', claimed_time='now', balance=1)
        hannibal = self.make_participant('hannibal', claimed_time='now', last_paypal_result='')
        lecter = self.make_participant('lecter', claimed_time='now', last_paypal_result='')
        A = self.make_team('The A Team', hannibal, is_approved=True)
        B = self.make_team('The B Team', lecter, is_approved=True)
        alice.set_subscription_to(A, D('0.51'))
        alice.set_subscription_to(B, D('0.50'))

        payday = Payday.start()
        with self.db.get_cursor() as cursor:
            payday.prepare(cursor)
            payday.process_subscriptions(cursor)
            assert cursor.one("select balance from payday_teams where slug='TheATeam'") == D('0.51')
            assert cursor.one("select balance from payday_teams where slug='TheBTeam'") == 0
            payday.make_journal_entries(cursor)

        assert Participant.from_username('alice').balance == D('0.49')
        assert Participant.from_username('hannibal').balance == 0
        assert Participant.from_username('lecter').balance == 0

        entries = self.db.one("SELECT * FROM journal")
        assert entries.amount == D('0.51')
        assert entries.debit == self.db.one("SELECT id FROM accounts WHERE participant='alice'")
        assert entries.credit == self.db.one("SELECT id FROM accounts WHERE team='TheATeam'")

    @pytest.mark.xfail(reason="haven't migrated_transfer_takes yet")
    def test_transfer_takes(self):
        a_team = self.make_participant('a_team', claimed_time='now', number='plural', balance=20)
        alice = self.make_participant('alice', claimed_time='now')
        a_team.add_member(alice)
        a_team.add_member(self.make_participant('bob', claimed_time='now'))
        a_team.set_take_for(alice, D('1.00'), alice)

        payday = Payday.start()

        # Test that payday ignores takes set after it started
        a_team.set_take_for(alice, D('2.00'), alice)

        # Run the transfer multiple times to make sure we ignore takes that
        # have already been processed
        for i in range(3):
            with self.db.get_cursor() as cursor:
                payday.prepare(cursor)
                payday.transfer_takes(cursor, payday.ts_start)
                payday.update_balances(cursor)

        participants = self.db.all("SELECT username, balance FROM participants")

        for p in participants:
            if p.username == 'a_team':
                assert p.balance == D('18.99')
            elif p.username == 'alice':
                assert p.balance == D('1.00')
            elif p.username == 'bob':
                assert p.balance == D('0.01')
            else:
                assert p.balance == 0

    def test_process_draws(self):
        alice = self.make_participant('alice', claimed_time='now', balance=1)
        hannibal = self.make_participant('hannibal', claimed_time='now', last_paypal_result='')
        A = self.make_team('The A Team', hannibal, is_approved=True)
        alice.set_subscription_to(A, D('0.51'))

        payday = Payday.start()
        with self.db.get_cursor() as cursor:
            payday.prepare(cursor)
            payday.process_subscriptions(cursor)
            payday.transfer_takes(cursor, payday.ts_start)
            payday.process_draws(cursor)
            assert cursor.one("select new_balance from payday_participants "
                              "where username='hannibal'") == D('0.51')
            assert cursor.one("select balance from payday_teams where slug='TheATeam'") == 0
            payday.update_balances(cursor)

        assert Participant.from_id(alice.id).balance == D('0.49')
        assert Participant.from_username('hannibal').balance == D('0.51')

        payment = self.db.one("SELECT * FROM payments WHERE direction='to-participant'")
        assert payment.amount == D('0.51')

    @pytest.mark.xfail(reason="haven't migrated_transfer_takes yet")
    @mock.patch.object(Payday, 'fetch_card_holds')
    def test_transfer_takes_doesnt_make_negative_transfers(self, fch):
        hold = balanced.CardHold(amount=1500, meta={'participant_id': self.janet.id},
                                 card_href=self.card_href)
        hold.capture = lambda *a, **kw: None
        hold.save = lambda *a, **kw: None
        fch.return_value = {self.janet.id: hold}
        self.janet.update_number('plural')
        self.janet.set_tip_to(self.homer, 10)
        self.janet.add_member(self.david)
        Payday.start().payin()
        assert Participant.from_id(self.david.id).balance == 0
        assert Participant.from_id(self.homer.id).balance == 10
        assert Participant.from_id(self.janet.id).balance == 0

    @pytest.mark.xfail(reason="haven't migrated take_over_balances yet")
    def test_take_over_during_payin(self):
        alice = self.make_participant('alice', claimed_time='now', balance=50)
        bob = self.make_participant('bob', claimed_time='now', elsewhere='twitter')
        alice.set_tip_to(bob, 18)
        payday = Payday.start()
        with self.db.get_cursor() as cursor:
            payday.prepare(cursor)
            bruce = self.make_participant('bruce', claimed_time='now')
            bruce.take_over(('twitter', str(bob.id)), have_confirmation=True)
            payday.process_subscriptions(cursor)
            bruce.delete_elsewhere('twitter', str(bob.id))
            billy = self.make_participant('billy', claimed_time='now')
            billy.take_over(('github', str(bruce.id)), have_confirmation=True)
            payday.update_balances(cursor)
        payday.take_over_balances()
        assert Participant.from_id(bob.id).balance == 0
        assert Participant.from_id(bruce.id).balance == 0
        assert Participant.from_id(billy.id).balance == 18

    @mock.patch.object(Payday, 'fetch_card_holds')
    @mock.patch('gratipay.billing.payday.capture_card_hold')
    def test_payin_dumps_transfers_for_debugging(self, cch, fch):
        team = self.make_team(owner=self.homer, is_approved=True)
        self.obama.set_subscription_to(team, '10.00')
        fake_hold = mock.MagicMock()
        fake_hold.amount = 1500
        fch.return_value = {self.obama.id: fake_hold}
        cch.side_effect = Foobar
        open_ = mock.MagicMock()
        open_.side_effect = open
        with mock.patch.dict(__builtins__, {'open': open_}):
            with self.assertRaises(Foobar):
                Payday.start().payin()
        filename = open_.call_args_list[-1][0][0]
        assert filename.endswith('_payments.csv')
        os.unlink(filename)

class TestNotifyParticipants(EmailHarness):

    def test_it_notifies_participants(self):
        kalel = self.make_participant('kalel', claimed_time='now', is_suspicious=False,
                                      email_address='kalel@example.net', notify_charge=3)
        team = self.make_team('Gratiteam', is_approved=True)
        kalel.set_subscription_to(team, 10)

        for status in ('failed', 'succeeded'):
            payday = Payday.start()
            self.make_exchange('balanced-cc', 10, 0, kalel, status)
            payday.end()
            payday.notify_participants()

            emails = self.db.one('SELECT * FROM email_queue')
            assert emails.spt_name == 'charge_'+status

            Participant.dequeue_emails()
            assert self.get_last_email()['to'][0]['email'] == 'kalel@example.net'
            assert 'Gratiteam' in self.get_last_email()['text']
