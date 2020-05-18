"""
本模块提供基于Http轮训的后端通用类和函数

.. attention::
    PyWebIO 的会话状态保存在进程内，所以不支持多进程部署的后端服务
        比如使用 ``uWSGI`` 部署后端服务，并使用 ``--processes n`` 选项设置了多进程；
        或者使用 ``nginx`` 等反向代理将流量负载到多个后端副本上。

    A note on run backend server with uWSGI：

    If you start uWSGI without threads, the Python GIL will not be enabled,
    so threads generated by your application will never run.
    `uWSGI doc <https://uwsgi-docs.readthedocs.io/en/latest/WSGIquickstart.html#a-note-on-python-threads>`_

"""
import asyncio
import fnmatch
import logging
import threading
from typing import Dict

import time
from ..session import CoroutineBasedSession, Session, register_session_implement_for_target
from ..session.base import get_session_info_from_headers
from ..utils import random_str, LRUDict


class HttpContext:
    """一次Http请求的上下文， 不同的后端框架需要根据框架提供的方法实现本类的方法"""

    backend_name = ''  # 当前使用的Web框架名

    def request_obj(self):
        """返回当前请求对象"""
        pass

    def request_method(self):
        """返回当前请求的方法，大写"""
        pass

    def request_headers(self):
        """返回当前请求的header字典"""
        pass

    def request_url_parameter(self, name, default=None):
        """返回当前请求的URL参数"""
        pass

    def request_json(self):
        """返回当前请求的json反序列化后的内容，若请求数据不为json格式，返回None"""
        pass

    def set_header(self, name, value):
        """为当前响应设置header"""
        pass

    def set_status(self, status):
        """为当前响应设置http status"""
        pass

    def set_content(self, content, json_type=False):
        """设置响应的内容。方法应该仅被调用一次

        :param content:
        :param bool json_type: content是否要序列化成json格式，并将 content-type 设置为application/json
        """
        pass

    def get_response(self):
        """获取当前的响应对象，用于在私图函数中返回"""
        pass

    def get_client_ip(self):
        """获取用户的ip"""
        pass


logger = logging.getLogger(__name__)
_event_loop = None


# todo: use lock to avoid thread race condition
class HttpHandler:
    # type: Dict[str, Session]
    _webio_sessions = {}  # WebIOSessionID -> WebIOSession()
    _webio_expire = LRUDict()  # WebIOSessionID -> last active timestamp。按照最后活跃时间递增排列

    _last_check_session_expire_ts = 0  # 上次检查session有效期的时间戳

    DEFAULT_SESSION_EXPIRE_SECONDS = 60  # 超过60s会话不活跃则视为会话过期
    SESSIONS_CLEANUP_INTERVAL = 20  # 清理过期会话间隔（秒）
    WAIT_MS_ON_POST = 100  # 在处理完POST请求时，等待WAIT_MS_ON_POST毫秒再读取返回数据。Task的command可以立即返回

    @classmethod
    def _remove_expired_sessions(cls, session_expire_seconds):
        logger.debug("removing expired sessions")
        """清除当前会话列表中的过期会话"""
        while cls._webio_expire:
            sid, active_ts = cls._webio_expire.popitem(last=False)

            if time.time() - active_ts < session_expire_seconds:
                # 当前session未过期
                cls._webio_expire[sid] = active_ts
                cls._webio_expire.move_to_end(sid, last=False)
                break

            # 清理session
            logger.debug("session %s expired" % sid)
            session = cls._webio_sessions.get(sid)
            if session:
                session.close()
                del cls._webio_sessions[sid]

    @classmethod
    def _remove_webio_session(cls, sid):
        cls._webio_sessions.pop(sid, None)
        cls._webio_expire.pop(sid, None)

    def _process_cors(self, context: HttpContext):
        """处理跨域请求：检查请求来源并根据可访问性设置headers"""
        origin = context.request_headers().get('Origin', '')
        if self.check_origin(origin):
            context.set_header('Access-Control-Allow-Origin', origin)
            context.set_header('Access-Control-Allow-Methods', 'GET, POST')
            context.set_header('Access-Control-Allow-Headers', 'content-type, webio-session-id')
            context.set_header('Access-Control-Expose-Headers', 'webio-session-id')
            context.set_header('Access-Control-Max-Age', str(1440 * 60))

    def handle_request(self, context: HttpContext):
        """处理请求"""
        cls = type(self)

        if _event_loop:
            asyncio.set_event_loop(_event_loop)

        request_headers = context.request_headers()

        if context.request_method() == 'OPTIONS':  # preflight request for CORS
            self._process_cors(context)
            context.set_status(204)
            return context.get_response()

        if request_headers.get('Origin'):  # set headers for CORS request
            self._process_cors(context)

        if context.request_url_parameter('test'):  # 测试接口，当会话使用给予http的backend时，返回 ok
            context.set_content('ok')
            return context.get_response()

        webio_session_id = None

        # webio-session-id 的请求头为空时，创建新 Session
        if 'webio-session-id' not in request_headers or not request_headers['webio-session-id']:
            if context.request_method() == 'POST':  # 不能在POST请求中创建Session，防止CSRF攻击
                context.set_status(403)
                return context.get_response()

            webio_session_id = random_str(24)
            context.set_header('webio-session-id', webio_session_id)
            session_info = get_session_info_from_headers(context.request_headers())
            session_info['user_ip'] = context.get_client_ip()
            session_info['request'] = context.request_obj()
            session_info['backend'] = context.backend_name
            webio_session = self.session_cls(self.target, session_info=session_info)
            cls._webio_sessions[webio_session_id] = webio_session
        elif request_headers['webio-session-id'] not in cls._webio_sessions:  # WebIOSession deleted
            context.set_content([dict(command='close_session')], json_type=True)
            return context.get_response()
        else:
            webio_session_id = request_headers['webio-session-id']
            webio_session = cls._webio_sessions[webio_session_id]

        if context.request_method() == 'POST':  # client push event
            if context.request_json() is not None:
                webio_session.send_client_event(context.request_json())
                time.sleep(cls.WAIT_MS_ON_POST / 1000.0)
        elif context.request_method() == 'GET':  # client pull messages
            pass

        cls._webio_expire[webio_session_id] = time.time()
        # clean up at intervals
        if time.time() - cls._last_check_session_expire_ts > self.session_cleanup_interval:
            cls._last_check_session_expire_ts = time.time()
            self._remove_expired_sessions(self.session_expire_seconds)

        context.set_content(webio_session.get_task_commands(), json_type=True)

        if webio_session.closed():
            self._remove_webio_session(webio_session_id)

        return context.get_response()

    def __init__(self, target, session_cls,
                 session_expire_seconds=None,
                 session_cleanup_interval=None,
                 allowed_origins=None, check_origin=None):
        """获取用于与后端实现进行整合的view函数，基于http请求与前端进行通讯

        :param target: 任务函数。任务函数为协程函数时，使用 :ref:`基于协程的会话实现 <coroutine_based_session>` ；任务函数为普通函数时，使用基于线程的会话实现。
        :param int session_expire_seconds: 会话不活跃过期时间。
        :param int session_cleanup_interval: 会话清理间隔。
        :param list allowed_origins: 除当前域名外，服务器还允许的请求的来源列表。
            来源包含协议和域名和端口部分，允许使用 Unix shell 风格的匹配模式:

            - ``*`` 为通配符
            - ``?`` 匹配单个字符
            - ``[seq]`` 匹配seq内的字符
            - ``[!seq]`` 匹配不在seq内的字符

            比如 ``https://*.example.com`` 、 ``*://*.example.com``
        :param callable check_origin: 请求来源检查函数。接收请求来源(包含协议和域名和端口部分)字符串，
            返回 ``True/False`` 。若设置了 ``check_origin`` ， ``allowed_origins`` 参数将被忽略
        """
        cls = type(self)

        self.target = target
        self.session_cls = session_cls
        self.check_origin = check_origin
        self.session_expire_seconds = session_expire_seconds or cls.DEFAULT_SESSION_EXPIRE_SECONDS
        self.session_cleanup_interval = session_cleanup_interval or cls.SESSIONS_CLEANUP_INTERVAL

        if check_origin is None:
            self.check_origin = lambda origin: any(
                fnmatch.fnmatch(origin, patten)
                for patten in allowed_origins or []
            )


def run_event_loop(debug=False):
    """运行事件循环

    基于协程的会话在启动基于线程的http服务器之前需要启动一个单独的线程来运行事件循环。

    :param debug: Set the debug mode of the event loop.
       See also: https://docs.python.org/3/library/asyncio-dev.html#asyncio-debug-mode
    """
    global _event_loop
    CoroutineBasedSession.event_loop_thread_id = threading.current_thread().ident
    _event_loop = asyncio.new_event_loop()
    _event_loop.set_debug(debug)
    asyncio.set_event_loop(_event_loop)
    _event_loop.run_forever()
