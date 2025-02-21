'''
UrlRequest
==========

.. versionadded:: 1.0.8

You can use the :class:`UrlRequest` to make asynchronous requests on the
web and get the result when the request is completed. The spirit is the
same as the XHR object in Javascript.

The content is also decoded if the Content-Type is
application/json and the result automatically passed through json.loads.


The syntax to create a request::

    from kivy.network.urlrequest import UrlRequest
    req = UrlRequest(url, on_success, on_redirect, on_failure, on_error,
                     on_progress, req_body, req_headers, chunk_size,
                     timeout, method, decode, debug, file_path, ca_file,
                     verify)


Only the first argument is mandatory: the rest are optional.
By default, a "GET" request will be sent. If the :attr:`UrlRequest.req_body` is
not None, a "POST" request will be sent. It's up to you to adjust
:attr:`UrlRequest.req_headers` to suit your requirements and the response
to the request will be accessible as the parameter called "result" on
the callback function of the on_success event.


Example of fetching JSON::

    def got_json(req, result):
        for key, value in req.resp_headers.items():
            print('{}: {}'.format(key, value))

    req = UrlRequest('https://httpbin.org/headers', got_json)

Example of Posting data (adapted from httplib example)::

    import urllib

    def bug_posted(req, result):
        print('Our bug is posted!')
        print(result)

    params = urllib.urlencode({'@number': 12524, '@type': 'issue',
        '@action': 'show'})
    headers = {'Content-type': 'application/x-www-form-urlencoded',
              'Accept': 'text/plain'}
    req = UrlRequest('bugs.python.org', on_success=bug_posted, req_body=params,
            req_headers=headers)

If you want a synchronous request, you can call the wait() method.

'''

import os
from abc import abstractmethod, ABC
from base64 import b64encode
from collections import deque
from http.client import HTTPConnection
from json import loads
import ssl
from threading import Event, Thread
from time import sleep
from urllib.parse import urlparse, urlunparse

import requests
from kivy.clock import Clock
from kivy.config import Config
from kivy.logger import Logger
from kivy.utils import platform
from kivy.weakmethod import WeakMethod

try:
    from http.client import HTTPSConnection
except ImportError:
    # depending on the platform, if openssl support wasn't compiled before
    # python, this class is not available.
    HTTPSConnection = None

# list to save UrlRequest and prevent GC on un-referenced objects
g_requests = []


class UrlRequestABC:
    """Grouping of abstract methods that must be defined by implementations.
    Currently, urllib and requests are supported.
    """

    @abstractmethod
    def get_chunks(self, resp, chunk_size, total_size, report_progress, q,
                   trigger, fd):
        raise NotImplementedError

    @abstractmethod
    def get_response(self, resp):
        raise NotImplementedError

    @abstractmethod
    def get_total_size(self, resp):
        raise NotImplementedError

    @abstractmethod
    def get_content_type(self, resp):
        raise NotImplementedError

    @abstractmethod
    def get_status_code(self, resp):
        raise NotImplementedError

    @abstractmethod
    def get_all_headers(self, resp):
        raise NotImplementedError

    @abstractmethod
    def close_connection(self, req):
        raise NotImplementedError

    @abstractmethod
    def _parse_url(self, url):
        raise NotImplementedError

    @abstractmethod
    def _get_connection_for_scheme(self, scheme):
        '''Return the Connection class for a particular scheme.
        This is an internal function that can be expanded to support custom
        schemes.

        Actual supported schemes: http, https.
        '''
        raise NotImplementedError

    @abstractmethod
    def call_request(self, body, headers):
        raise NotImplementedError


class UrlRequestBase(Thread, UrlRequestABC, ABC):
    '''A UrlRequest. See module documentation for usage.

    .. versionchanged:: 1.5.1
        Add `debug` parameter

    .. versionchanged:: 1.0.10
        Add `method` parameter

    .. versionchanged:: 1.8.0

        Parameter `decode` added.
        Parameter `file_path` added.
        Parameter `on_redirect` added.
        Parameter `on_failure` added.

    .. versionchanged:: 1.9.1

        Parameter `ca_file` added.
        Parameter `verify` added.

    .. versionchanged:: 1.10.0

        Parameters `proxy_host`, `proxy_port` and `proxy_headers` added.

    .. versionchanged:: 1.11.0

        Parameters `on_cancel` added.

    .. versionchanged:: 2.2.0

        Parameters `on_finish` added.
        Parameters `auth` added.

    :Parameters:
        `url`: str
            Complete url string to call.
        `on_success`: callback(request, result)
            Callback function to call when the result has been fetched.
        `on_redirect`: callback(request, result)
            Callback function to call if the server returns a Redirect.
        `on_failure`: callback(request, result)
            Callback function to call if the server returns a Client or
            Server Error.
        `on_error`: callback(request, error)
            Callback function to call if an error occurs.
        `on_progress`: callback(request, current_size, total_size)
            Callback function that will be called to report progression of the
            download. `total_size` might be -1 if no Content-Length has been
            reported in the http response.
            This callback will be called after each `chunk_size` is read.
        `on_cancel`: callback(request)
            Callback function to call if user requested to cancel the download
            operation via the .cancel() method.
        `on_finish`: callback(request)
            Additional callback function to call if request is done.
        `req_body`: str, defaults to None
            Data to sent in the request. If it's not None, a POST will be done
            instead of a GET.
        `req_headers`: dict, defaults to None
            Custom headers to add to the request.
        `chunk_size`: int, defaults to 8192
            Size of each chunk to read, used only when `on_progress` callback
            has been set. If you decrease it too much, a lot of on_progress
            callbacks will be fired and will slow down your download. If you
            want to have the maximum download speed, increase the chunk_size
            or don't use ``on_progress``.
        `timeout`: int, defaults to None
            If set, blocking operations will time out after this many seconds.
        `method`: str, defaults to 'GET' (or 'POST' if ``body`` is specified)
            The HTTP method to use.
        `decode`: bool, defaults to True
            If False, skip decoding of the response.
        `debug`: bool, defaults to False
            If True, it will use the Logger.debug to print information
            about url access/progression/errors.
        `file_path`: str, defaults to None
            If set, the result of the UrlRequest will be written to this path
            instead of in memory.
        `ca_file`: str, defaults to None
            Indicates an SSL CA certificate file path to validate HTTPS
            certificates against
        `verify`: bool, defaults to True
            If False, disables SSL CA certificate verification
        `proxy_host`: str, defaults to None
            If set, the proxy host to use for this connection.
        `proxy_port`: int, defaults to None
            If set, and `proxy_host` is also set, the port to use for
            connecting to the proxy server.
        `proxy_headers`: dict, defaults to None
            If set, and `proxy_host` is also set, the headers to send to the
            proxy server in the ``CONNECT`` request.
        `auth`: HTTPBasicAuth, defaults to None
            If set, request will use basicauth to authenticate.
            Only used in "Requests" implementation
        `start`: bool, defaults to True
            If True, the request will be started automatically. Otherwise, the
            user may work with the request object before starting the request.
            This enables binding callbacks in a similar manner to kivy's
            event-driven paradigm.
        `on_start`: callback(request)
            Callback function to call when the request is sent. Useful when
            preparing requests in advance of execution.
    '''

    def __init__(
            self, url, on_success=None, on_redirect=None,
            on_failure=None, on_error=None, on_progress=None,
            req_body=None, req_headers=None, chunk_size=8192,
            timeout=None, method=None, decode=True, debug=False,
            file_path=None, ca_file=None, verify=True, proxy_host=None,
            proxy_port=None, proxy_headers=None, user_agent=None,
            on_cancel=None, on_finish=None, cookies=None, auth=None,
            start=True, on_start=None,
    ):
        super().__init__()
        self._queue = deque()
        self._trigger_result = Clock.create_trigger(self._dispatch_result, 0)
        self.daemon = True

        # weak methods corresponding to callbacks
        self.on_start = [WeakMethod(on_start)] if on_start else []
        self.on_success = [WeakMethod(on_success)] if on_success else []
        self.on_redirect = [WeakMethod(on_redirect)] if on_redirect else []
        self.on_failure = [WeakMethod(on_failure)] if on_failure else []
        self.on_error = [WeakMethod(on_error)] if on_error else []
        self.on_progress = [WeakMethod(on_progress)] if on_progress else []
        self.on_cancel = [WeakMethod(on_cancel)] if on_cancel else []
        self.on_finish = [WeakMethod(on_finish)] if on_finish else []

        # internal values not set via parameters
        self._result = None
        self._error = None
        self._is_finished = False
        self._resp_status = None
        self._resp_headers = None
        self._resp_length = -1
        self._cancel_event = Event()

        # internal values set via parameters
        self.decode = decode
        self.file_path = file_path
        self._debug = debug
        self._chunk_size = chunk_size
        self._timeout = timeout
        self._method = method
        self.verify = verify
        self._proxy_host = proxy_host
        self._proxy_port = proxy_port
        self._proxy_headers = proxy_headers
        self._user_agent = user_agent
        self._cookies = cookies
        self._requested_url = url
        self._auth = auth

        if platform in ['android', 'ios']:
            import certifi
            self.ca_file = ca_file or certifi.where()
        else:
            self.ca_file = ca_file

        #: Url of the request
        self.url = url

        #: Request body passed in __init__
        self.req_body = req_body

        #: Request headers passed in __init__
        self.req_headers = req_headers

        # save our request to prevent GC
        g_requests.append(self)

        # allow manually starting the request
        if start:
            self.start()

    def start(self) -> None:
        self._dispatch_callbacks(self.on_start)
        super().start()

    def run(self):

        q, req_body, req_headers, url = self.prepare_request_()

        try:
            result, resp = self._fetch_url(url, req_body, req_headers, q)
            if self.decode:
                result = self.decode_result(result, resp)
        except Exception as e:
            q(('error', None, e))
        else:
            if not self._cancel_event.is_set():
                q(('success', resp, result))
            else:
                q(('killed', None, None))

        # using trigger can result in a missed on_success event
        # noinspection PyArgumentList
        self._trigger_result()

        # clean ourselves when the queue is empty
        while len(self._queue):
            sleep(.1)
            # noinspection PyArgumentList
            self._trigger_result()

        # ok, authorize the GC to clean us.
        if self in g_requests:
            g_requests.remove(self)

    def prepare_request_(self):
        '''Prepare headers, cookie, auth, etc.'''

        q = self._queue.appendleft
        url = self.url
        req_body = self.req_body

        req_headers = self.req_headers or {}
        user_agent = self._user_agent
        cookies = self._cookies

        # set user_agent from init value
        if user_agent:
            req_headers.setdefault('User-Agent', user_agent)

        # set user_agent from kivy config value
        elif (
                Config.has_section('network')
                and 'useragent' in Config.items('network')
        ):
            useragent = Config.get('network', 'useragent')
            req_headers.setdefault('User-Agent', useragent)

        if cookies:
            req_headers.setdefault("Cookie", cookies)

        return q, req_body, req_headers, url

    def _fetch_url(self, url, body, headers, q):
        # Parse and fetch the current url
        trigger = self._trigger_result
        chunk_size = self._chunk_size
        report_progress = self.on_progress is not None
        file_path = self.file_path

        if self._debug:
            Logger.debug('UrlRequest: {0} Fetch url <{1}>'.format(
                id(self), url))
            Logger.debug('UrlRequest: {0} - body: {1}'.format(
                id(self), body))
            Logger.debug('UrlRequest: {0} - headers: {1}'.format(
                id(self), headers))

        req, resp = self.call_request(body, headers)

        # read content
        if report_progress or file_path is not None:
            total_size = self.get_total_size(resp)

            # before starting the download, send a fake progress to permit the
            # user to initialize his ui
            if report_progress:
                q(('progress', resp, (0, total_size)))

            if file_path is not None:
                with open(file_path, 'wb') as fd:
                    bytes_so_far, result = self.get_chunks(
                        resp, chunk_size, total_size, report_progress, q,
                        trigger, fd=fd
                    )
            else:
                bytes_so_far, result = self.get_chunks(
                    resp, chunk_size, total_size, report_progress, q, trigger
                )

            # ensure that results are dispatched for the last chunk,
            # avoid trigger
            if report_progress:
                q(('progress', resp, (bytes_so_far, total_size)))
                trigger()
        else:
            result = self.get_response(resp)
            try:
                if isinstance(result, bytes):
                    result = result.decode('utf-8')
            except UnicodeDecodeError:
                # if it's an image? decoding would not work
                pass

        self.close_connection(req)

        # return everything
        return result, resp

    def decode_result(self, result, resp):
        '''Decode the result fetched from url according to Content-Type.
        Currently, supports only application/json.
        '''
        # Entry to decode url from the content type.
        # For example, if the content type is a json, it will be automatically
        # decoded.
        content_type = self.get_content_type(resp)
        if content_type is not None:
            ct = content_type.split(';')[0]
            if ct == 'application/json':
                if isinstance(result, bytes):
                    result = result.decode('utf-8')
                try:
                    return loads(result)
                except Exception as e:
                    if self._debug:
                        Logger.debug(
                            'UrlRequest: {0} failed to decode result'
                            'with exception {1}.'.format(id(self), e)
                        )
                    return result

        return result

    def _on_status_code(self, data, resp, status_class):
        '''When the http request was "successful" in the sense that the server
         returned a response code--the code may indicate an error, but we did
         successfully get a response.'''

        if status_class in (1, 2):
            if self._debug:
                Logger.debug(
                    'UrlRequest: {0} Download finished with '
                    '{1} data len'.format(id(self), data)
                )
            self._dispatch_callbacks(self.on_success, data)

        elif status_class == 3:
            if self._debug:
                Logger.debug('UrlRequest: {} Download '
                             'redirected'.format(id(self)))
            self._dispatch_callbacks(self.on_redirect, data)

        elif status_class in (4, 5):
            if self._debug:
                Logger.debug(
                    'UrlRequest: {} Download failed with '
                    'http error {}'.format(
                        id(self),
                        self.get_status_code(resp)
                    )
                )
            self._dispatch_callbacks(self.on_failure, data)

    def _dispatch_callbacks(self, callbacks: list[WeakMethod], *largs, **kwargs):
        '''Dispatch any callbacks associated with the http response or user
        driven actions.
        '''

        if callbacks:
            for callback in callbacks:
                func = callback()
                if func:
                    func(self, *largs, **kwargs)

    def _dispatch_result(self, dt):
        '''Method called by clock trigger. Checks request progress and
        dispatches results'''

        while True:
            # Read the result pushed on the queue, and dispatch to the client
            try:
                result, resp, data = self._queue.pop()
            except IndexError:
                return

            if resp:
                self.modify_response_headers(resp)

            # When we reach python 3.11 as the minimum supported version, this
            # hot mess should be replaced with match statement syntax.

            # server returned a response
            if result == 'success':
                self._is_finished = True
                self._result = data

                status_class = self.get_status_code(resp) // 100

                self._on_status_code(data, resp, status_class)

            # result is pending
            elif result == 'progress':
                if self._debug:
                    Logger.debug('UrlRequest: {0} Download progress '
                                 '{1}'.format(id(self), data))
                self._dispatch_callbacks(self.on_progress, data[0], data[1])

            # server did not return a response
            elif result == 'error':
                self._is_finished = True
                self._error = data

                if self._debug:
                    Logger.debug('UrlRequest: {0} Download error '
                                 '<{1}>'.format(id(self), data))
                self._dispatch_callbacks(self.on_error, data)

            # user cancelled the request
            elif result == 'killed':
                if self._debug:
                    Logger.debug('UrlRequest: Cancelled by user')
                self._dispatch_callbacks(self.on_cancel)

            # this block should never be reached in normal use
            else:
                raise ValueError('UrlRequest: {} Unknown result value {}'
                                 .format((id(self)), result))

            # additional callback when result is finished
            if result != "progress":
                if self._debug:
                    Logger.debug('UrlRequest: Request is finished')
                self._dispatch_callbacks(self.on_finish)

    def modify_response_headers(self, resp):
        ''' XXX usage of dict can be dangerous if multiple headers
            are set even if it's invalid. But it look like it's ok
            ?  http://stackoverflow.com/questions/2454494/..
            ..urllib2-multiple-set-cookie-headers-in-response'''

        final_cookies = ""
        parsed_headers = []
        for key, value in self.get_all_headers(resp):
            if key == "Set-Cookie":
                final_cookies += "{};".format(value)
            else:
                parsed_headers.append((key, value))
        parsed_headers.append(("Set-Cookie", final_cookies[:-1]))

        self._resp_headers = dict(parsed_headers)
        self._resp_status = self.get_status_code(resp)

    @property
    def is_finished(self):
        '''Return True if the request has finished, whether it's a
        success or a failure.
        '''
        return self._is_finished

    @property
    def result(self):
        '''Return the result of the request.
        This value is not determined until the request is finished.
        '''
        return self._result

    @property
    def resp_headers(self):
        '''If the request has been completed, return a dictionary containing
        the headers of the response. Otherwise, it will return None.
        '''
        return self._resp_headers

    @property
    def resp_status(self):
        '''Return the status code of the response if the request is complete,
        otherwise return None.
        '''
        return self._resp_status

    @property
    def error(self):
        '''Return the error of the request.
        This value is not determined until the request is completed.
        '''
        return self._error

    @property
    def chunk_size(self):
        '''Return the size of a chunk, used only in "progress" mode (when
        on_progress callback is set.)
        '''
        return self._chunk_size

    def wait(self, delay=0.5):
        '''Wait for the request to finish (until :attr:`resp_status` is not
        None)

        .. note::
            This method is intended to be used in the main thread, and the
            callback will be dispatched from the same thread
            from which you're calling.

        .. versionadded:: 1.1.0
        '''
        while self.resp_status is None:
            self._dispatch_result(delay)
            sleep(delay)

    def cancel(self):
        '''Cancel the current request. It will be aborted, and the result
        will not be dispatched. Once cancelled, the callback on_cancel will
        be called.

        .. versionadded:: 1.11.0
        '''
        self._cancel_event.set()

    def bind(self, **kwargs):
        '''Mimics the standard event binding present throughout kivy, without
        creating the overhead of actually binding and unbinding methods for a
        request object that is typically short-lived.

        To use this method, the UrlRequest object must be created with the
        parameter start set to False. This allows assigning callbacks after
        object creation, similar to the way that callbacks are bound to kivy
        events. The UrlRequest must be manually started after the fact, usually
        immediately after the binding is done.

        The advantage of using bind is derived from code reuse; it is
        possible to create multiple, similar requests that may handle their
        responses differently given different app states. It is also possible
        to easily bind multiple requests to similar sets of callbacks,
        without defining a helper function for each request.

        The following code snippets are equivalent:

        > r = UrlRequest(url, on_success=callback)

        > r = UrlRequest(url, start=False)
        > r.bind(on_success=callback)
        > r.start()

        An advantage is demonstrated in the following equivalent snippets:

        > def my_on_success_callback_helper(request, response):
        >     update_app_display(request, response)
        >     update_app_state(request, response)
        >
        > def my_on_cancel_callback_helper(request, response):
        >     update_app_state(request)
        >
        > r = UrlRequest(
        >     url,
        >     on_success=my_on_success_callback_helper,
        >     on_cancel=my_on_cancel_callback_helper,
        > )

        > r = UrlRequest(url, start=False)
        > r.bind(on_success=[update_app_display, update_app_state])
        > r.bind(on_cancel=update_app_state)
        > r.start()

        '''

        # ensure thread has not been started as callbacks may otherwise be
        # missed when they are assigned after states are reached
        assert not self._started.is_set()

        for callback_type, methods in kwargs.items():
            # iterable
            try:
                for method in methods:
                    callbacks_list = getattr(self, callback_type)
                    callbacks_list.append(WeakMethod(method))

            # not iterable
            except TypeError:
                method = methods
                callbacks_list = getattr(self, callback_type)
                callbacks_list.append(WeakMethod(method))


class UrlRequestUrllib(UrlRequestBase):

    def get_chunks(
            self, resp, chunk_size, total_size, report_progress, q,
            trigger, fd=None
    ):
        bytes_so_far = 0
        result = b''

        while 1:
            chunk = resp.read(chunk_size)
            if not chunk:
                break

            if fd:
                fd.write(chunk)

            else:
                result += chunk

            bytes_so_far += len(chunk)

            if report_progress:
                q(('progress', resp, (bytes_so_far, total_size)))
                trigger()

            if self._cancel_event.is_set():
                break

        return bytes_so_far, result

    def get_response(self, resp):
        return resp.read()

    def get_total_size(self, resp):
        try:
            return int(resp.getheader('content-length'))
        except Exception:
            return -1

    def get_content_type(self, resp):
        return resp.getheader('Content-Type', None)

    def get_status_code(self, resp):
        return resp.status

    def get_all_headers(self, resp):
        return resp.getheaders()

    def close_connection(self, req):
        req.close()

    def _parse_url(self, url):
        parse = urlparse(url)
        host = parse.hostname
        port = parse.port
        userpass = None

        # append user + pass to hostname if specified
        if parse.username and parse.password:
            userpass = {
                "Authorization": "Basic {}".format(b64encode(
                    "{}:{}".format(
                        parse.username,
                        parse.password
                    ).encode('utf-8')
                ).decode('utf-8'))
            }

        return host, port, userpass, parse

    def _get_connection_for_scheme(self, scheme):
        '''Return the Connection class for a particular scheme.
        This is an internal function that can be expanded to support custom
        schemes.

        Actual supported schemes: http, https.
        '''
        if scheme == 'http':
            return HTTPConnection
        elif scheme == 'https' and HTTPSConnection is not None:
            return HTTPSConnection
        else:
            raise Exception('No class for scheme %s' % scheme)

    def call_request(self, body, headers):
        timeout = self._timeout
        ca_file = self.ca_file
        verify = self.verify
        url = self._requested_url

        # parse url
        host, port, userpass, parse = self._parse_url(url)
        if userpass and not headers:
            headers = userpass
        elif userpass and headers:
            key = list(userpass.keys())[0]
            headers[key] = userpass[key]

        # translate scheme to connection class
        cls = self._get_connection_for_scheme(parse.scheme)

        # reconstruct path to pass on the request
        path = parse.path
        if parse.params:
            path += ';' + parse.params
        if parse.query:
            path += '?' + parse.query
        if parse.fragment:
            path += '#' + parse.fragment

        # create connection instance
        args = {}
        if timeout is not None:
            args['timeout'] = timeout

        if (ca_file is not None and hasattr(ssl, 'create_default_context') and
                parse.scheme == 'https'):
            ctx = ssl.create_default_context(cafile=ca_file)
            ctx.verify_mode = ssl.CERT_REQUIRED
            args['context'] = ctx

        if not verify and parse.scheme == 'https' and (
                hasattr(ssl, 'create_default_context')):
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            args['context'] = ctx

        if self._proxy_host:
            Logger.debug('UrlRequest: {0} - proxy via {1}:{2}'.format(
                id(self), self._proxy_host, self._proxy_port
            ))
            req = cls(self._proxy_host, self._proxy_port, **args)
            if parse.scheme == 'https':
                req.set_tunnel(host, port, self._proxy_headers)
            else:
                path = urlunparse(parse)
        else:
            req = cls(host, port, **args)

        # send request
        method = self._method
        if method is None:
            method = 'GET' if body is None else 'POST'

        req.request(method, path, body, headers or {})

        # read header
        return req, req.getresponse()


class UrlRequestRequests(UrlRequestBase):

    def get_chunks(
            self, resp, chunk_size, total_size, report_progress, q,
            trigger, fd=None
    ):
        bytes_so_far = 0
        result = b''

        for chunk in resp.iter_content(chunk_size):
            if not chunk:
                break

            if fd:
                fd.write(chunk)

            else:
                result += chunk

            bytes_so_far += len(chunk)

            if report_progress:
                q(('progress', resp, (bytes_so_far, total_size)))
                trigger()

            if self._cancel_event.is_set():
                break

        return bytes_so_far, result

    def get_response(self, resp):
        return resp.content

    def get_total_size(self, resp):
        return int(resp.headers.get('Content-Length', -1))

    def get_content_type(self, resp):
        return resp.headers.get('Content-Type', None)

    def get_status_code(self, resp):
        return resp.status_code

    def get_all_headers(self, resp):
        return resp.headers.items()

    def close_connection(self, req):
        pass

    def call_request(self, body, headers):
        timeout = self._timeout
        ca_file = self.ca_file
        verify = self.verify
        url = self._requested_url
        auth = self._auth

        req = requests
        kwargs = {}

        # determine default method if not set via __init__
        if self._method is None:
            method = 'get' if body is None else 'post'

        # otherwise, use the user provided http method
        else:
            method = self._method.lower()

        req_call = getattr(req, method)

        if auth:
            kwargs["auth"] = auth

        # send request
        response = req_call(
            url,
            data=body,
            headers=headers,
            timeout=timeout,
            verify=verify,
            cert=ca_file,
            **kwargs
        )

        return None, response


implementation_map = {
    "default": UrlRequestUrllib,
    "requests": UrlRequestRequests,
    "urllib": UrlRequestUrllib,
}

if not os.environ.get("KIVY_DOC_INCLUDE"):
    preferred_implementation = Config.getdefault(
        "network", "implementation", "default"
    )
else:
    preferred_implementation = "default"

UrlRequest = implementation_map.get(preferred_implementation)
