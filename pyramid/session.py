import base64
import binascii
import hashlib
import hmac
import os
import time

from zope.deprecation import deprecated
from zope.interface import implementer

from webob.cookies import SignedSerializer

from pyramid.compat import (
    pickle,
    PY3,
    text_,
    bytes_,
    native_,
    )

from pyramid.exceptions import BadCSRFToken
from pyramid.interfaces import ISession
from pyramid.util import strings_differ

def manage_accessed(wrapped):
    """ Decorator which causes a cookie to be renewed when an accessor
    method is called."""
    def accessed(session, *arg, **kw):
        session.accessed = now = int(time.time())
        if now - session.renewed > session._reissue_time:
            session.changed()
        return wrapped(session, *arg, **kw)
    accessed.__doc__ = wrapped.__doc__
    return accessed

def manage_changed(wrapped):
    """ Decorator which causes a cookie to be set when a setter method
    is called."""
    def changed(session, *arg, **kw):
        session.accessed = int(time.time())
        session.changed()
        return wrapped(session, *arg, **kw)
    changed.__doc__ = wrapped.__doc__
    return changed

def signed_serialize(data, secret):
    """ Serialize any pickleable structure (``data``) and sign it
    using the ``secret`` (must be a string).  Return the
    serialization, which includes the signature as its first 40 bytes.
    The ``signed_deserialize`` method will deserialize such a value.

    This function is useful for creating signed cookies.  For example:

    .. code-block:: python

       cookieval = signed_serialize({'a':1}, 'secret')
       response.set_cookie('signed_cookie', cookieval)
    """
    pickled = pickle.dumps(data, pickle.HIGHEST_PROTOCOL)
    sig = hmac.new(bytes_(secret, 'utf-8'), pickled, hashlib.sha1).hexdigest()
    return sig + native_(base64.b64encode(pickled))

def signed_deserialize(serialized, secret, hmac=hmac):
    """ Deserialize the value returned from ``signed_serialize``.  If
    the value cannot be deserialized for any reason, a
    :exc:`ValueError` exception will be raised.

    This function is useful for deserializing a signed cookie value
    created by ``signed_serialize``.  For example:

    .. code-block:: python

       cookieval = request.cookies['signed_cookie']
       data = signed_deserialize(cookieval, 'secret')
    """
    # hmac parameterized only for unit tests
    try:
        input_sig, pickled = (bytes_(serialized[:40]),
                              base64.b64decode(bytes_(serialized[40:])))
    except (binascii.Error, TypeError) as e:
        # Badly formed data can make base64 die
        raise ValueError('Badly formed base64 data: %s' % e)

    sig = bytes_(hmac.new(
        bytes_(secret, 'utf-8'), pickled, hashlib.sha1,
    ).hexdigest())

    # Avoid timing attacks (see
    # http://seb.dbzteam.org/crypto/python-oauth-timing-hmac.pdf)
    if strings_differ(sig, input_sig):
        raise ValueError('Invalid signature')

    return pickle.loads(pickled)

def check_csrf_token(request,
                     token='csrf_token',
                     header='X-CSRF-Token',
                     raises=True):
    """ Check the CSRF token in the request's session against the value in
    ``request.params.get(token)`` or ``request.headers.get(header)``.
    If a ``token`` keyword is not supplied to this function, the string
    ``csrf_token`` will be used to look up the token in ``request.params``.
    If a ``header`` keyword is not supplied to this function, the string
    ``X-CSRF-Token`` will be used to look up the token in ``request.headers``.

    If the value supplied by param or by header doesn't match the value
    supplied by ``request.session.get_csrf_token()``, and ``raises`` is
    ``True``, this function will raise an
    :exc:`pyramid.exceptions.BadCSRFToken` exception.
    If the check does succeed and ``raises`` is ``False``, this
    function will return ``False``.  If the CSRF check is successful, this
    function will return ``True`` unconditionally.

    Note that using this function requires that a :term:`session factory` is
    configured.

    .. versionadded:: 1.4a2
    """
    supplied_token = request.params.get(token, request.headers.get(header))
    if supplied_token != request.session.get_csrf_token():
        if raises:
            raise BadCSRFToken('check_csrf_token(): Invalid token')
        return False
    return True

class PickleSerializer(object):
    """ A Webob cookie serializer that uses the pickle protocol to dump Python
    data to bytes."""
    def loads(self, bstruct):
        return pickle.loads(bstruct)

    def dumps(self, appstruct):
        return pickle.dumps(appstruct, pickle.HIGHEST_PROTOCOL)

def BaseCookieSessionFactory(
    serializer,
    cookie_name='session',
    max_age=None,
    path='/',
    domain=None,
    secure=False,
    httponly=False,
    timeout=1200,
    reissue_time=0,
    set_on_exception=True,
    ):
    """
    .. versionadded:: 1.5
    
    Configure a :term:`session factory` which will provide cookie-based
    sessions.  The return value of this function is a :term:`session factory`,
    which may be provided as the ``session_factory`` argument of a
    :class:`pyramid.config.Configurator` constructor, or used as the
    ``session_factory`` argument of the
    :meth:`pyramid.config.Configurator.set_session_factory` method.

    The session factory returned by this function will create sessions
    which are limited to storing fewer than 4000 bytes of data (as the
    payload must fit into a single cookie).

    .. warning:

       This class provides no protection from tampering and is only intended
       to be used by framework authors to create their own cookie-based
       session factories.

    Parameters:

    ``serializer``
      An object with two methods: ``loads`` and ``dumps``.  The ``loads`` method
      should accept bytes and return a Python object.  The ``dumps`` method
      should accept a Python object and return bytes.  A ``ValueError`` should
      be raised for malformed inputs.

    ``cookie_name``
      The name of the cookie used for sessioning. Default: ``'session'``.

    ``max_age``
      The maximum age of the cookie used for sessioning (in seconds).
      Default: ``None`` (browser scope).

    ``path``
      The path used for the session cookie. Default: ``'/'``.

    ``domain``
      The domain used for the session cookie.  Default: ``None`` (no domain).

    ``secure``
      The 'secure' flag of the session cookie. Default: ``False``.

    ``httponly``
      Hide the cookie from Javascript by setting the 'HttpOnly' flag of the
      session cookie. Default: ``False``.

    ``timeout``
      A number of seconds of inactivity before a session times out. If
      ``None`` then the cookie never expires. Default: 1200.

    ``reissue_time``
      The number of seconds that must pass before the cookie is automatically
      reissued as the result of a request which accesses the session. The
      duration is measured as the number of seconds since the last session
      cookie was issued and 'now'.  If this value is ``0``, a new cookie
      will be reissued on every request accesses the session. If ``None``
      then the cookie's lifetime will never be extended.

      A good rule of thumb: if you want auto-expired cookies based on
      inactivity: set the ``timeout`` value to 1200 (20 mins) and set the
      ``reissue_time`` value to perhaps a tenth of the ``timeout`` value
      (120 or 2 mins).  It's nonsensical to set the ``timeout`` value lower
      than the ``reissue_time`` value, as the ticket will never be reissued.
      However, such a configuration is not explicitly prevented.

      Default: ``0``.

    ``set_on_exception``
      If ``True``, set a session cookie even if an exception occurs
      while rendering a view. Default: ``True``.

    .. versionadded: 1.5a3
    """

    @implementer(ISession)
    class CookieSession(dict):
        """ Dictionary-like session object """

        # configuration parameters
        _cookie_name = cookie_name
        _cookie_max_age = max_age
        _cookie_path = path
        _cookie_domain = domain
        _cookie_secure = secure
        _cookie_httponly = httponly
        _cookie_on_exception = set_on_exception
        _timeout = timeout
        _reissue_time = reissue_time

        # dirty flag
        _dirty = False

        def __init__(self, request):
            self.request = request
            now = time.time()
            created = renewed = now
            new = True
            value = None
            state = {}
            cookieval = request.cookies.get(self._cookie_name)
            if cookieval is not None:
                try:
                    value = serializer.loads(bytes_(cookieval))
                except ValueError:
                    # the cookie failed to deserialize, dropped
                    value = None

            if value is not None:
                try:
                    renewed, created, state = value
                    new = False
                    if now - renewed > self._timeout:
                        # expire the session because it was not renewed
                        # before the timeout threshold
                        state = {}
                except TypeError:
                    # value failed to unpack properly or renewed was not
                    # a numeric type so we'll fail deserialization here
                    state = {}

            self.created = created
            self.accessed = renewed
            self.renewed = renewed
            self.new = new
            dict.__init__(self, state)

        # ISession methods
        def changed(self):
            if not self._dirty:
                self._dirty = True
                def set_cookie_callback(request, response):
                    self._set_cookie(response)
                    self.request = None # explicitly break cycle for gc
                self.request.add_response_callback(set_cookie_callback)

        def invalidate(self):
            self.clear() # XXX probably needs to unset cookie

        # non-modifying dictionary methods
        get = manage_accessed(dict.get)
        __getitem__ = manage_accessed(dict.__getitem__)
        items = manage_accessed(dict.items)
        values = manage_accessed(dict.values)
        keys = manage_accessed(dict.keys)
        __contains__ = manage_accessed(dict.__contains__)
        __len__ = manage_accessed(dict.__len__)
        __iter__ = manage_accessed(dict.__iter__)

        if not PY3:
            iteritems = manage_accessed(dict.iteritems)
            itervalues = manage_accessed(dict.itervalues)
            iterkeys = manage_accessed(dict.iterkeys)
            has_key = manage_accessed(dict.has_key)

        # modifying dictionary methods
        clear = manage_changed(dict.clear)
        update = manage_changed(dict.update)
        setdefault = manage_changed(dict.setdefault)
        pop = manage_changed(dict.pop)
        popitem = manage_changed(dict.popitem)
        __setitem__ = manage_changed(dict.__setitem__)
        __delitem__ = manage_changed(dict.__delitem__)

        # flash API methods
        @manage_changed
        def flash(self, msg, queue='', allow_duplicate=True):
            storage = self.setdefault('_f_' + queue, [])
            if allow_duplicate or (msg not in storage):
                storage.append(msg)

        @manage_changed
        def pop_flash(self, queue=''):
            storage = self.pop('_f_' + queue, [])
            return storage

        @manage_accessed
        def peek_flash(self, queue=''):
            storage = self.get('_f_' + queue, [])
            return storage

        # CSRF API methods
        @manage_changed
        def new_csrf_token(self):
            token = text_(binascii.hexlify(os.urandom(20)))
            self['_csrft_'] = token
            return token

        @manage_accessed
        def get_csrf_token(self):
            token = self.get('_csrft_', None)
            if token is None:
                token = self.new_csrf_token()
            return token

        # non-API methods
        def _set_cookie(self, response):
            if not self._cookie_on_exception:
                exception = getattr(self.request, 'exception', None)
                if exception is not None: # dont set a cookie during exceptions
                    return False
            cookieval = native_(serializer.dumps(
                (self.accessed, self.created, dict(self))
                ))
            if len(cookieval) > 4064:
                raise ValueError(
                    'Cookie value is too long to store (%s bytes)' %
                    len(cookieval)
                    )
            response.set_cookie(
                self._cookie_name,
                value=cookieval,
                max_age=self._cookie_max_age,
                path=self._cookie_path,
                domain=self._cookie_domain,
                secure=self._cookie_secure,
                httponly=self._cookie_httponly,
                )
            return True

    return CookieSession


def UnencryptedCookieSessionFactoryConfig(
    secret,
    timeout=1200,
    cookie_name='session',
    cookie_max_age=None,
    cookie_path='/',
    cookie_domain=None,
    cookie_secure=False,
    cookie_httponly=False,
    cookie_on_exception=True,
    signed_serialize=signed_serialize,
    signed_deserialize=signed_deserialize,
    ):
    """
    .. deprecated:: 1.5
        Use :func:`pyramid.session.SignedCookieSessionFactory` instead.
        Caveat: Cookies generated using ``SignedCookieSessionFactory`` are not
        compatible with cookies generated using
        ``UnencryptedCookieSessionFactory``, so existing user session data
        will be destroyed if you switch to it.
    
    Configure a :term:`session factory` which will provide unencrypted
    (but signed) cookie-based sessions.  The return value of this
    function is a :term:`session factory`, which may be provided as
    the ``session_factory`` argument of a
    :class:`pyramid.config.Configurator` constructor, or used
    as the ``session_factory`` argument of the
    :meth:`pyramid.config.Configurator.set_session_factory`
    method.

    The session factory returned by this function will create sessions
    which are limited to storing fewer than 4000 bytes of data (as the
    payload must fit into a single cookie).

    Parameters:

    ``secret``
      A string which is used to sign the cookie.

    ``timeout``
      A number of seconds of inactivity before a session times out.

    ``cookie_name``
      The name of the cookie used for sessioning.

    ``cookie_max_age``
      The maximum age of the cookie used for sessioning (in seconds).
      Default: ``None`` (browser scope).

    ``cookie_path``
      The path used for the session cookie.

    ``cookie_domain``
      The domain used for the session cookie.  Default: ``None`` (no domain).

    ``cookie_secure``
      The 'secure' flag of the session cookie.

    ``cookie_httponly``
      The 'httpOnly' flag of the session cookie.

    ``cookie_on_exception``
      If ``True``, set a session cookie even if an exception occurs
      while rendering a view.

    ``signed_serialize``
      A callable which takes more or less arbitrary Python data structure and
      a secret and returns a signed serialization in bytes.
      Default: ``signed_serialize`` (using pickle).

    ``signed_deserialize``
      A callable which takes a signed and serialized data structure in bytes
      and a secret and returns the original data structure if the signature
      is valid. Default: ``signed_deserialize`` (using pickle).
    """

    class SerializerWrapper(object):
        def __init__(self, secret):
            self.secret = secret
            
        def loads(self, bstruct):
            return signed_deserialize(bstruct, secret)

        def dumps(self, appstruct):
            return signed_serialize(appstruct, secret)

    serializer = SerializerWrapper(secret)

    return BaseCookieSessionFactory(
        serializer,
        cookie_name=cookie_name,
        max_age=cookie_max_age,
        path=cookie_path,
        domain=cookie_domain,
        secure=cookie_secure,
        httponly=cookie_httponly,
        timeout=timeout,
        reissue_time=0, # to keep session.accessed == session.renewed
        set_on_exception=cookie_on_exception,
    )

deprecated(
    'UnencryptedCookieSessionFactoryConfig',
    'The UnencryptedCookieSessionFactoryConfig callable is deprecated as of '
    'Pyramid 1.5.  Use ``pyramid.session.SignedCookieSessionFactory`` instead. '
    'Caveat: Cookies generated using SignedCookieSessionFactory are not '
    'compatible with cookies generated using UnencryptedCookieSessionFactory, '
    'so existing user session data will be destroyed if you switch to it.'
    )

def SignedCookieSessionFactory(
    secret,
    cookie_name='session',
    max_age=None,
    path='/',
    domain=None,
    secure=False,
    httponly=False,
    set_on_exception=True,
    timeout=1200,
    reissue_time=0,
    hashalg='sha512',
    salt='pyramid.session.',
    serializer=None,
    ):
    """
    .. versionadded:: 1.5
    
    Configure a :term:`session factory` which will provide signed
    cookie-based sessions.  The return value of this
    function is a :term:`session factory`, which may be provided as
    the ``session_factory`` argument of a
    :class:`pyramid.config.Configurator` constructor, or used
    as the ``session_factory`` argument of the
    :meth:`pyramid.config.Configurator.set_session_factory`
    method.

    The session factory returned by this function will create sessions
    which are limited to storing fewer than 4000 bytes of data (as the
    payload must fit into a single cookie).

    Parameters:

    ``secret``
      A string which is used to sign the cookie. The secret should be at
      least as long as the block size of the selected hash algorithm. For
      ``sha512`` this would mean a 128 bit (64 character) secret.  It should
      be unique within the set of secret values provided to Pyramid for
      its various subsystems (see :ref:`admonishment_against_secret_sharing`).

    ``hashalg``
      The HMAC digest algorithm to use for signing. The algorithm must be
      supported by the :mod:`hashlib` library. Default: ``'sha512'``.

    ``salt``
      A namespace to avoid collisions between different uses of a shared
      secret. Reusing a secret for different parts of an application is
      strongly discouraged (see :ref:`admonishment_against_secret_sharing`).
      Default: ``'pyramid.session.'``.

    ``cookie_name``
      The name of the cookie used for sessioning. Default: ``'session'``.

    ``max_age``
      The maximum age of the cookie used for sessioning (in seconds).
      Default: ``None`` (browser scope).

    ``path``
      The path used for the session cookie. Default: ``'/'``.

    ``domain``
      The domain used for the session cookie.  Default: ``None`` (no domain).

    ``secure``
      The 'secure' flag of the session cookie. Default: ``False``.

    ``httponly``
      Hide the cookie from Javascript by setting the 'HttpOnly' flag of the
      session cookie. Default: ``False``.

    ``timeout``
      A number of seconds of inactivity before a session times out. If
      ``None`` then the cookie never expires. Default: 1200.

    ``reissue_time``
      The number of seconds that must pass before the cookie is automatically
      reissued as the result of a request which accesses the session. The
      duration is measured as the number of seconds since the last session
      cookie was issued and 'now'.  If this value is ``0``, a new cookie
      will be reissued on every request accesses the session. If ``None``
      then the cookie's lifetime will never be extended.

      A good rule of thumb: if you want auto-expired cookies based on
      inactivity: set the ``timeout`` value to 1200 (20 mins) and set the
      ``reissue_time`` value to perhaps a tenth of the ``timeout`` value
      (120 or 2 mins).  It's nonsensical to set the ``timeout`` value lower
      than the ``reissue_time`` value, as the ticket will never be reissued.
      However, such a configuration is not explicitly prevented.

      Default: ``0``.

    ``set_on_exception``
      If ``True``, set a session cookie even if an exception occurs
      while rendering a view. Default: ``True``.

    ``serializer``
      An object with two methods: ``loads`` and ``dumps``.  The ``loads`` method
      should accept bytes and return a Python object.  The ``dumps`` method
      should accept a Python object and return bytes.  A ``ValueError`` should
      be raised for malformed inputs.  If a serializer is not passed, the
      :class:`pyramid.session.PickleSerializer` serializer will be used.

    .. versionadded: 1.5a3
    """
    if serializer is None:
        serializer = PickleSerializer()

    signed_serializer = SignedSerializer(
        secret,
        salt, 
        hashalg,
        serializer=serializer,
        )

    return BaseCookieSessionFactory(
        signed_serializer,
        cookie_name=cookie_name,
        max_age=max_age,
        path=path,
        domain=domain,
        secure=secure,
        httponly=httponly,
        timeout=timeout,
        reissue_time=reissue_time,
        set_on_exception=set_on_exception,
    )
