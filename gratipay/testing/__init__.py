"""Helpers for testing Gratipay.
"""
from __future__ import absolute_import, division, print_function, unicode_literals

import itertools
import unittest
from collections import defaultdict
from os.path import dirname, join, realpath

from aspen import resources
from aspen.utils import utcnow
from aspen.testing.client import Client
from gratipay.billing.exchanges import record_exchange, record_exchange_result
from gratipay.elsewhere import UserInfo
from gratipay.main import website
from gratipay.models.account_elsewhere import AccountElsewhere
from gratipay.models.exchange_route import ExchangeRoute
from gratipay.models.participant import Participant
from gratipay.security.user import User
from gratipay.testing.vcr import use_cassette
from psycopg2 import IntegrityError, InternalError


TOP = realpath(join(dirname(dirname(__file__)), '..'))
WWW_ROOT = str(realpath(join(TOP, 'www')))
PROJECT_ROOT = str(TOP)


class ClientWithAuth(Client):

    def __init__(self, *a, **kw):
        Client.__init__(self, *a, **kw)
        Client.website = website

    def build_wsgi_environ(self, *a, **kw):
        """Extend base class to support authenticating as a certain user.
        """

        self.cookie.clear()

        # csrf - for both anon and authenticated
        csrf_token = kw.get('csrf_token', b'ThisIsATokenThatIsThirtyTwoBytes')
        if csrf_token:
            self.cookie[b'csrf_token'] = csrf_token
            kw[b'HTTP_X-CSRF-TOKEN'] = csrf_token

        # user authentication
        auth_as = kw.pop('auth_as', None)
        if auth_as:
            user = User.from_username(auth_as)
            user.sign_in(self.cookie)

        for k, v in kw.pop('cookies', {}).items():
            self.cookie[k] = v

        return Client.build_wsgi_environ(self, *a, **kw)


class Harness(unittest.TestCase):

    client = ClientWithAuth(www_root=WWW_ROOT, project_root=PROJECT_ROOT)
    db = client.website.db
    platforms = client.website.platforms
    tablenames = db.all("SELECT tablename FROM pg_tables "
                        "WHERE schemaname='public'")
    seq = itertools.count(0)


    @classmethod
    def setUpClass(cls):
        cls.setUpVCR()


    @classmethod
    def setUpVCR(cls):
        """Set up VCR.

        We use the VCR library to freeze API calls. Frozen calls are stored in
        tests/fixtures/ for your convenience (otherwise your first test run
        would take fooooorrr eeeevvveeerrrr). If you find that an API call has
        drifted from our frozen version of it, simply remove that fixture file
        and rerun. The VCR library should recreate the fixture with the new
        information, and you can commit that with your updated tests.

        """
        cls.vcr_cassette = use_cassette(cls.__name__)
        cls.vcr_cassette.__enter__()


    @classmethod
    def tearDownClass(cls):
        cls.vcr_cassette.__exit__(None, None, None)


    def setUp(self):
        self.clear_tables()


    def tearDown(self):
        resources.__cache__ = {}  # Clear the simplate cache.
        self.clear_tables()


    def clear_tables(self):
        tablenames = self.tablenames[:]
        while tablenames:
            tablename = tablenames.pop()
            try:
                # I tried TRUNCATE but that was way slower for me.
                self.db.run("DELETE FROM %s CASCADE" % tablename)
            except (IntegrityError, InternalError):
                tablenames.insert(0, tablename)
        self.db.run("ALTER SEQUENCE participants_id_seq RESTART WITH 1")
        self.db.run("SELECT create_system_accounts()")  # repopulate the `accounts` table


    def make_elsewhere(self, platform, user_id, user_name, **kw):
        info = UserInfo( platform=platform
                       , user_id=unicode(user_id)
                       , user_name=user_name
                       , **kw
                       )
        return AccountElsewhere.upsert(info)


    def show_table(self, table):
        print('\n{:=^80}'.format(table))
        data = self.db.all('select * from '+table, back_as='namedtuple')
        if len(data) == 0:
            return
        widths = list(len(k) for k in data[0]._fields)
        for row in data:
            for i, v in enumerate(row):
                widths[i] = max(widths[i], len(unicode(v)))
        for k, w in zip(data[0]._fields, widths):
            print("{0:{width}}".format(unicode(k), width=w), end=' | ')
        print()
        for row in data:
            for v, w in zip(row, widths):
                print("{0:{width}}".format(unicode(v), width=w), end=' | ')
            print()

    def make_team(self, *a, **kw):

        _kw = defaultdict(str)
        _kw.update(kw)

        if a:
            _kw['name'] = a[0]
            try: _kw['owner'] = a[1]
            except IndexError: pass
        if 'name' not in _kw:
            _kw['name'] = "The A Team"
        if 'owner' not in _kw:
            _kw['owner'] = "hannibal"
        elif isinstance(_kw['owner'], Participant):
            _kw['owner'] = _kw['owner'].username
        if 'slug' not in _kw:
            _kw['slug'] = _kw['name'].replace('.', '').replace(' ', '').replace(',', '')
        if 'slug_lower' not in _kw:
            _kw['slug_lower'] = _kw['slug'].lower()
        if 'is_approved' not in _kw:
            _kw['is_approved'] = False

        if Participant.from_username(_kw['owner']) is None:
            self.make_participant(_kw['owner'], claimed_time='now', last_paypal_result='')

        team = self.db.one("""
            INSERT INTO teams
                        (slug, slug_lower, name, homepage, product_or_service,
                         getting_involved, getting_paid, owner, is_approved)
                 VALUES (%(slug)s, %(slug_lower)s, %(name)s, %(homepage)s,
                         %(product_or_service)s, %(getting_involved)s, %(getting_paid)s,
                         %(owner)s, %(is_approved)s)
              RETURNING teams.*::teams
        """, _kw)

        return team


    def make_participant(self, username, **kw):
        participant = self.db.one("""
            INSERT INTO participants
                        (username, username_lower)
                 VALUES (%s, %s)
              RETURNING participants.*::participants
        """, (username, username.lower()))

        if 'elsewhere' in kw or 'claimed_time' in kw:
            platform = kw.pop('elsewhere', 'github')
            self.db.run("""
                INSERT INTO elsewhere
                            (platform, user_id, user_name, participant)
                     VALUES (%s,%s,%s,%s)
            """, (platform, participant.id, username, username))

        # Insert exchange routes
        if 'last_bill_result' in kw:
            ExchangeRoute.insert(participant, 'braintree-cc', '/cards/foo', kw.pop('last_bill_result'))
        if 'last_paypal_result' in kw:
            ExchangeRoute.insert(participant, 'paypal', 'abcd@gmail.com', kw.pop('last_paypal_result'))

        # Update participant
        if kw:
            if kw.get('claimed_time') == 'now':
                kw['claimed_time'] = utcnow()
            cols, vals = zip(*kw.items())
            cols = ', '.join(cols)
            placeholders = ', '.join(['%s']*len(vals))
            participant = self.db.one("""
                UPDATE participants
                   SET ({0}) = ({1})
                 WHERE username=%s
             RETURNING participants.*::participants
            """.format(cols, placeholders), vals+(username,))

        return participant


    def fetch_payday(self):
        return self.db.one("SELECT * FROM paydays", back_as=dict)


    def make_exchange(self, route, amount, fee, participant, status='succeeded', error=''):
        if not isinstance(route, ExchangeRoute):
            network = route
            route = ExchangeRoute.from_network(participant, network)
            if not route:
                route = ExchangeRoute.insert(participant, network, 'dummy-address')
                assert route
        e_id = record_exchange(self.db, route, amount, fee, participant, 'pre')
        record_exchange_result(self.db, e_id, status, error, participant)
        return e_id


class Foobar(Exception): pass
