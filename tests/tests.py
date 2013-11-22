from datetime import timedelta
import sys
try:
    from urllib.parse import urlencode
except ImportError:
    from urllib import urlencode

from django.conf import settings
from django.contrib.auth import SESSION_KEY, authenticate, login
from django.contrib.auth.models import User
from django.contrib.sessions.backends.base import CreateError
from django.core.urlresolvers import reverse
from django.db import IntegrityError
from django.http import HttpRequest
from django.test import TestCase, Client as BaseClient
from django.test.utils import override_settings
from django.utils.timezone import now

from user_sessions.backends.db import SessionStore
from user_sessions.models import Session
from user_sessions.templatetags.user_sessions import location, device

if sys.version_info[:2] < (2, 7):
    from django.utils.unittest.case import skipUnless
else:
    from unittest import skipUnless

try:
    from django.contrib.gis.geoip import GeoIP
    geoip = GeoIP()
    geoip_msg = None
except Exception as e:
    geoip = None
    geoip_msg = str(e)


class Client(BaseClient):
    def login(self, **credentials):
        """
        Sets the Factory to appear as if it has successfully logged into a site.

        Returns True if login is possible; False if the provided credentials
        are incorrect, or the user is inactive, or if the sessions framework is
        not available.
        """
        user = authenticate(**credentials)
        if user and user.is_active:
            # Create a fake request to store login details.
            request = HttpRequest()
            if self.session:
                request.session = self.session
            else:
                request.session = SessionStore('Python/2.7', '127.0.0.1')
            login(request, user)

            # Save the session values.
            request.session.save()

            # Set the cookie to represent the session.
            session_cookie = settings.SESSION_COOKIE_NAME
            self.cookies[session_cookie] = request.session.session_key
            cookie_data = {
                'max-age': None,
                'path': '/',
                'domain': settings.SESSION_COOKIE_DOMAIN,
                'secure': settings.SESSION_COOKIE_SECURE or None,
                'expires': None,
            }
            self.cookies[session_cookie].update(cookie_data)

            return True
        else:
            return False


class MiddlewareTest(TestCase):
    def test_unmodified_session(self):
        self.client.get('/', HTTP_USER_AGENT='Python/2.7')
        self.assertNotIn(settings.SESSION_COOKIE_NAME, self.client.cookies)

    def test_modify_session(self):
        self.client.get('/modify_session/', HTTP_USER_AGENT='Python/2.7')
        self.assertIn(settings.SESSION_COOKIE_NAME, self.client.cookies)
        session = Session.objects.get(
            pk=self.client.cookies[settings.SESSION_COOKIE_NAME].value
        )
        self.assertEqual(session.user_agent, 'Python/2.7')
        self.assertEqual(session.ip, '127.0.0.1')

    def test_login(self):
        user = User.objects.create_superuser('bouke', '', 'secret')
        response = self.client.post('/admin/',
                                    data={
                                        'username': 'bouke',
                                        'password': 'secret',
                                        'this_is_the_login_form': '1',
                                        'next': '/admin/'},
                                    HTTP_USER_AGENT='Python/2.7')
        self.assertRedirects(response, '/admin/')
        session = Session.objects.get(
            pk=self.client.cookies[settings.SESSION_COOKIE_NAME].value
        )
        self.assertEqual(user, session.user)

    def test_long_ua(self):
        self.client.get('/modify_session/',
                        HTTP_USER_AGENT=''.join('a' for _ in range(400)))


class ViewsTest(TestCase):
    client_class = Client

    def setUp(self):
        User.objects.create_user('bouke', '', 'secret')
        assert self.client.login(username='bouke', password='secret')

    def test_list(self):
        response = self.client.get(reverse('user_sessions:session_list'))
        self.assertContains(response, 'Active Sessions')
        self.assertContains(response, 'End Session', 2)

    def test_delete(self):
        session_key = self.client.cookies[settings.SESSION_COOKIE_NAME].value
        response = self.client.post(reverse('user_sessions:session_delete',
                                            args=[session_key]))
        self.assertRedirects(response, reverse('user_sessions:session_list'))


class AdminTest(TestCase):
    client_class = Client

    def setUp(self):
        User.objects.create_superuser('bouke', '', 'secret')
        assert self.client.login(username='bouke', password='secret')

        expired = SessionStore('Python/2.5', '20.13.1.1')
        expired.set_expiry(-365*86400)
        expired.save()
        unexpired = SessionStore('Python/2.7', '1.1.1.1')
        unexpired.save()

        self.admin_url = reverse('admin:user_sessions_session_changelist')

    def test_list(self):
        response = self.client.get(self.admin_url)
        self.assertContains(response, 'Select session to change')
        self.assertContains(response, '127.0.0.1')
        self.assertContains(response, '20.13.1.1')
        self.assertContains(response, '1.1.1.1')

    def test_mine(self):
        my_sessions = '%s?%s' % (self.admin_url, urlencode({'owner': 'my'}))
        response = self.client.get(my_sessions)
        self.assertContains(response, '127.0.0.1')
        self.assertNotContains(response, '1.1.1.1')

    def test_expired(self):
        expired = '%s?%s' % (self.admin_url, urlencode({'active': '0'}))
        response = self.client.get(expired)
        self.assertContains(response, '20.13.1.1')
        self.assertNotContains(response, '1.1.1.1')

    def test_unexpired(self):
        unexpired = '%s?%s' % (self.admin_url, urlencode({'active': '1'}))
        response = self.client.get(unexpired)
        self.assertContains(response, '1.1.1.1')
        self.assertNotContains(response, '20.13.1.1')


class SessionStoreTest(TestCase):
    def setUp(self):
        self.store = SessionStore('Python/2.7', '127.0.0.1', None)

    def test_untouched_init(self):
        self.assertFalse(self.store.modified)
        self.assertFalse(self.store.accessed)

    def test_auth_session_key(self):
        self.assertFalse(SESSION_KEY in self.store)
        self.assertFalse(self.store.modified)
        self.assertTrue(self.store.accessed)

        self.store.get(SESSION_KEY)
        self.assertFalse(self.store.modified)

        self.store[SESSION_KEY] = 1
        self.assertTrue(self.store.modified)

    def test_save(self):
        self.store[SESSION_KEY] = 1
        self.store.save()

        session = Session.objects.get(pk=self.store.session_key)
        self.assertEqual(session.user_agent, 'Python/2.7')
        self.assertEqual(session.ip, '127.0.0.1')
        self.assertEqual(session.user_id, 1)
        self.assertAlmostEqual(now(), session.last_activity,
                               delta=timedelta(seconds=5))

    def test_load_unmodified(self):
        self.store[SESSION_KEY] = 1
        self.store.save()
        store2 = SessionStore('Python/2.7', '127.0.0.1',
                              self.store.session_key)
        store2.load()
        self.assertEqual(store2.user_agent, 'Python/2.7')
        self.assertEqual(store2.ip, '127.0.0.1')
        self.assertEqual(store2.user_id, 1)
        self.assertEqual(store2.modified, False)

    def test_load_modified(self):
        self.store[SESSION_KEY] = 1
        self.store.save()
        store2 = SessionStore('Python/3.3', '8.8.8.8', self.store.session_key)
        store2.load()
        self.assertEqual(store2.user_agent, 'Python/3.3')
        self.assertEqual(store2.ip, '8.8.8.8')
        self.assertEqual(store2.user_id, 1)
        self.assertEqual(store2.modified, True)


@skipUnless(geoip, geoip_msg)
class LocationTemplateFilterTest(TestCase):
    def test_no_location(self):
        self.assertEqual(location('127.0.0.1'), '<i>unknown</i>')

    def test_google(self):
        self.assertEqual(location('8.8.8.8'), 'United States')


class DeviceTemplateFilterTest(TestCase):
    def test_ie(self):
        self.assertEqual(
            'Internet Explorer on Windows XP',
            device('Mozilla/4.0 (Windows; MSIE 6.0; Windows NT 5.1; SV1; '
                   '.NET CLR 2.0.50727)')
        )
        self.assertEqual(
            'Internet Explorer on Windows Vista',
            device('Mozilla/4.0 (compatible; MSIE 8.0; Windows NT 6.0; '
                   'Trident/4.0; SLCC1; .NET CLR 2.0.50727; .NET CLR 1.1.4322;'
                   ' InfoPath.2; .NET CLR 3.5.21022; .NET CLR 3.5.30729; '
                   'MS-RTC LM 8; OfficeLiveConnector.1.4; OfficeLivePatch.1.3;'
                   ' .NET CLR 3.0.30729)')
        )
        self.assertEqual(
            'Internet Explorer on Windows 7',
            device('Mozilla/5.0 (compatible; MSIE 10.0; Windows NT 6.1; '
                   'Trident/6.0)')
        )
        self.assertEqual(
            'Internet Explorer on Windows 8',
            device('Mozilla/5.0 (compatible; MSIE 10.0; Windows NT 6.2; '
                   'Win64; x64; Trident/6.0)')
        )
        self.assertEqual(
            'Internet Explorer on Windows 8.1',
            device('Mozilla/5.0 (IE 11.0; Windows NT 6.3; Trident/7.0; '
                   '.NET4.0E; .NET4.0C; rv:11.0) like Gecko')
        )

    def test_apple(self):
        self.assertEqual(
            'Safari on iPad',
            device('Mozilla/5.0 (iPad; U; CPU OS 4_2_1 like Mac OS X; ja-jp) '
                   'AppleWebKit/533.17.9 (KHTML, like Gecko) Version/5.0.2 '
                   'Mobile/8C148 Safari/6533.18.5')
        )
        self.assertEqual(
            'Safari on iPhone',
            device('Mozilla/5.0 (iPhone; CPU iPhone OS 7_0 like Mac OS X) '
                   'AppleWebKit/537.51.1 (KHTML, like Gecko) Version/7.0 '
                   'Mobile/11A465 Safari/9537.53')
        )
        self.assertEqual(
            'Safari on OS X',
            device('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_8_2) '
                   'AppleWebKit/536.26.17 (KHTML, like Gecko) Version/6.0.2 '
                   'Safari/536.26.17')
        )

    def test_android(self):
        # androids identify themselves as Safari to get the good stuff
        self.assertEqual(
            'Safari on Android',
            device('Mozilla/5.0 (Linux; U; Android 1.5; de-de; HTC Magic '
                   'Build/CRB17) AppleWebKit/528.5+ (KHTML, like Gecko) '
                   'Version/3.1.2 Mobile Safari/525.20.1')
        )

    def test_firefox(self):
        self.assertEqual(
            'Firefox on Windows 7',
            device('Mozilla/5.0 (Windows NT 6.1; Win64; x64; rv:22.0) '
                   'Gecko/20130328 Firefox/22.0')
        )

    def test_chrome(self):
        self.assertEqual(
            'Chrome on Windows 8.1',
            device('Mozilla/5.0 (Windows NT 6.3; WOW64) AppleWebKit/537.36 ('
                   'KHTML, like Gecko) Chrome/30.0.1599.101 Safari/537.36')
        )