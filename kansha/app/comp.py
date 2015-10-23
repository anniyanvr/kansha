# -*- coding:utf-8 -*-
# --
# Copyright (c) 2012-2014 Net-ng.
# All rights reserved.
#
# This software is licensed under the BSD License, as described in
# the file LICENSE.txt, which you should have received as part of
# this distribution.
# --

import cgi
import json
import sys
import configobj
import pkg_resources
import webob

from nagare import component, wsgi, security, config, log, i18n
from nagare.admin import command
from nagare.i18n import _
from nagare.namespaces import xhtml5

from .. import exceptions

from ..authentication import login

from ..board import comp as board
from ..board.boardsmanager import BoardsManager

from ..user.usermanager import UserManager
from ..user import user_profile

from ..security import SecurityManager, Unauthorized

from ..services.simpleassetsmanager.simpleassetsmanager import SimpleAssetsManager
from ..services.search import SearchEngine
from ..services import mail
from kansha import services


def run():
    return command.run('kansha.commands')


class Kansha(object):
    """The Kansha root component"""

    def __init__(self, app_title, app_banner, custom_css,
                 search, services_service):
        """Initialization
        """
        self._services = services_service

        self.app_title = app_title
        self.app_banner = app_banner
        self.custom_css = custom_css
        self.title = component.Component(self, 'tab')
        self.user_menu = component.Component(None)
        self.content = component.Component(None).on_answer(self.select_board)
        self.user_manager = UserManager()
        self.boards_manager = BoardsManager()
        self.search_engine = search
        self.default_board_id = None

    def initialization(self):
        """ Initialize Kansha application

        Initialize user_menu with current user,
        Initialize last board

        Return:
         - app initialized
        """
        self.user_menu = component.Component(security.get_user())
        if security.get_user():
            if self.default_board_id:
                self.select_board(self.default_board_id)
            else:
                self.select_last_board()
        return self

    def select_board(self, id_):
        """Selected a board by id

        In:
          - ``id_`` -- the id of the board
        """
        if not id_:
            return
        if self.boards_manager.get_by_id(id_):
            self.content.becomes(
                self._services(
                    board.Board,
                    id_,
                    self.app_title,
                    self.app_banner,
                    self.custom_css,
                    self.search_engine,
                    on_board_archive=self.select_last_board,
                    on_board_leave=self.select_last_board
                )
            )
            # if user is logged, update is last board
            user = security.get_user()
            if user:
                user.set_last_board(self.content())
        else:
            raise exceptions.BoardNotFound()

    def select_board_by_uri(self, uri):
        """Selected a board by URI

        In:
          - ``uri`` -- the uri of the board
        """
        b = self.boards_manager.get_by_uri(uri)
        self.default_board_id = b.id if (b and not b.archived) else None

    def select_last_board(self):
        """Selects the last used board if it's possible

        Otherwise, content becomes user's home
        """
        user = security.get_user()
        data_board = user.get_last_board()
        if data_board and not data_board.archived and data_board.has_member(user):
            self.select_board(data_board.id)
        else:
            self.content.becomes(
                self._services(
                    user_profile.UserProfile,
                    self.app_title,
                    self.app_banner,
                    self.custom_css,
                    user.data,
                    self.search_engine
                ),
                'edit'
            )


class MainTask(component.Task):
    def __init__(self, app_title, app_banner, custom_css, main_app,
                 cfg, search, services_service):
        self._services = services_service

        self.app_title = app_title
        self.app_banner = app_banner
        self.custom_css = custom_css
        self.auth_cfg = cfg['authentication']
        self.tpl_cfg = cfg['tpl_cfg']
        self.app = services_service(
            Kansha,
            self.app_title,
            self.app_banner,
            self.custom_css,
            search
        )
        self.main_app = main_app
        self.search_engine = search
        self.cfg = cfg

    def go(self, comp):
        user = security.get_user()
        while user is None:
            # not logged ? Call login component
            comp.call(
                self._services(
                    login.Login,
                    self.app_title,
                    self.app_banner,
                    self.custom_css,
                    self.cfg,
                )
            )
            user = security.get_user()
            if user.last_login is None:
                # first connection.
                # Load template boards if any,
                self.app.boards_manager.create_boards_from_templates(user.data, self.cfg['tpl_cfg'])
                #  then index cards
                self.app.boards_manager.index_user_cards(user.data,
                                                         self.search_engine)
            user.update_last_login()

        comp.call(self.app.initialization())
        # Logout
        if user is not None:
            security.get_manager().logout()


class App(object):
    def __init__(self, app_title, custom_css, cfg,
                 search, services_service):
        self._services = services_service

        self.app_title = app_title
        self.app_banner = cfg['pub_cfg']['banner']
        self.favicon = cfg['pub_cfg']['favicon']
        self.custom_css = custom_css
        self.search_engine = search
        self.task = component.Component(
            services_service(
                MainTask,
                self.app_title,
                self.app_banner,
                self.custom_css,
                self, cfg,
                search
            )
        )


class WSGIApp(wsgi.WSGIApp):
    """This application uses a HTML5 renderer"""
    renderer_factory = xhtml5.Renderer

    ConfigSpec = {
        'application': {'as_root': 'boolean(default=True)',
                        'title': 'string(default="")',
                        'banner': 'string(default="")',
                        'custom_css': 'string(default="")',
                        'favicon': 'string(default="img/favicon.ico")',
                        'disclaimer': 'string(default="")',
                        'activity_monitor': "string(default='')",
                        'templates': "string(default='')"},
        'locale': {
            'major': 'string(default="en")',
            'minor': 'string(default="US")'
        }
    }

    def set_config(self, config_filename, conf, error):
        super(WSGIApp, self).set_config(config_filename, conf, error)
        conf = configobj.ConfigObj(
            conf, configspec=configobj.ConfigObj(self.ConfigSpec), interpolation='Template')
        config.validate(config_filename, conf, error)

        self._services = services.ServicesRepository(
            config_filename, error, conf
        )

        self.as_root = conf['application']['as_root']
        self.app_title = unicode(conf['application']['title'], 'utf-8')
        self.custom_css = conf['application']['custom_css']
        self.application_path = conf['application']['path']

        # search_engine engine configuration
        self.search_engine = SearchEngine(**conf['search'])

        # other
        self.security = SecurityManager(conf['application']['crypto_key'])
        self.debug = conf['application']['debug']
        self.default_locale = i18n.Locale(
            conf['locale']['major'], conf['locale']['minor'])
        tpl_cfg = conf['application']['templates']
        pub_cfg = {
            'disclaimer': conf['application']['disclaimer'].decode('utf-8'),
            'banner': conf['application']['banner'].decode('utf-8'),
            'favicon': conf['application']['favicon'].decode('utf-8')
        }
        self.app_cfg = {
            'authentication': conf['authentication'],
            'tpl_cfg': tpl_cfg,
            'pub_cfg': pub_cfg
        }
        self.activity_monitor = conf['application']['activity_monitor']

    def set_publisher(self, publisher):
        if self.as_root:
            publisher.register_application(self.application_path, '', self,
                                           self)

    def create_root(self):
        return super(WSGIApp, self).create_root(
            self.app_title,
            self.custom_css,
            self.app_cfg,
            self.search_engine,
            self._services
        )

    def start_request(self, root, request, response):
        super(WSGIApp, self).start_request(root, request, response)
        if security.get_user():
            self.set_locale(security.get_user().get_locale())

    def __call__(self, environ, start_response):
        query = environ['QUERY_STRING']
        if ('state=' in query) and (('code=' in query) or ('error=' in query)):
            request = webob.Request(environ)
            environ['QUERY_STRING'] += ('&' + request.params['state'])

        return super(WSGIApp, self).__call__(environ, start_response)

    def on_exception(self, request, response):
        exc_class, e = sys.exc_info()[:2]
        for k, v in request.POST.items():
            if isinstance(v, cgi.FieldStorage):
                request.POST[k] = u'Content not displayed'
        log.exception(e)
        response.headers['Content-Type'] = 'text/html'
        package = pkg_resources.Requirement.parse('kansha')
        error = pkg_resources.resource_string(
            package, 'static/html/error.html')
        error = unicode(error, 'utf8')
        response.charset = 'utf8'
        data = {'text': u'', 'status': 200,
                'go_back': _(u'Go back'),
                'app_title': self.app_title,
                'custom_css': self.custom_css}
        if exc_class == Unauthorized:
            status = 403
            text = _(u"You are not authorized to access this board")
        elif exc_class == exceptions.BoardNotFound:
            status = 404
            text = _(u"This board doesn't exists")
        elif exc_class == exceptions.NotFound:
            status = 404
            text = _(u"Page not found")
        elif exc_class == exceptions.KanshaException:
            status = 500
            text = _(unicode(e.message))
        else:
            status = 500
            text = _(u"Unable to proceed. Please contact us.")
        # Raise exception if debug
        if self.debug:
            raise
        data['status'] = status
        data['text'] = text
        response.status = status
        if request.is_xhr:
            response.body = json.dumps({'details': text, 'status': status})
        else:
            response.text = error % data
        return response

    def on_callback_lookuperror(self, request, response, async):
        """A callback was not found

        In:
          - ``request`` -- the web request object
          - ``response`` -- the web response object
          - ``async`` -- is an XHR request ?
        """
        # log.exception("\n%s" % request)
        if self.debug:
            raise


def create_pipe(app, *args, **kw):
    '''Use with Apache only, fixes the content-length when gzipped'''

    try:
        from paste.gzipper import middleware
        app = middleware(app)
    except ImportError:
        pass
    return app


app = WSGIApp(lambda *args: component.Component(App(*args)))
