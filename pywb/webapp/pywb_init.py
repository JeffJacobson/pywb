from pywb.utils.loaders import load_yaml_config

from pywb.framework.archivalrouter import ArchivalRouter, Route
from pywb.framework.proxy import ProxyArchivalRouter
from pywb.framework.wbrequestresponse import WbRequest
from pywb.framework.memento import MementoRequest
from pywb.framework.basehandlers import BaseHandler

from views import J2TemplateView, add_env_globals
from views import J2HtmlCapturesView, HeadInsertView

from live_rewrite_handler import RewriteHandler

from query_handler import QueryHandler
from handlers import WBHandler
from handlers import StaticHandler
from handlers import DebugEchoHandler, DebugEchoEnvHandler
from cdx_api_handler import CDXAPIHandler


import os
import logging


#=================================================================
DEFAULT_CONFIG = 'pywb/default_config.yaml'

#=================================================================
class DictChain:
    def __init__(self, *dicts):
        self.dicts = dicts

    def get(self, key, default_val=None):
        for d in self.dicts:
            val = d.get(key)
            if val is not None:
                return val
        return default_val


#=================================================================
def create_wb_handler(query_handler, config):
    wb_handler_class = config.get('wb_handler_class', WBHandler)

    wb_handler = wb_handler_class(
        query_handler,
        config=config,
    )

    return wb_handler


#=================================================================
def create_live_handler(config):
    wb_handler_class = config.get('wb_handler_class', RewriteHandler)

    live_handler = wb_handler_class(config)

    return live_handler


#=================================================================
def init_route_config(value, config):
    if isinstance(value, str) or isinstance(value, list):
        value = dict(index_paths=value)

    route_config = DictChain(value, config)
    return route_config


#=================================================================
def init_collection(route_config):
    ds_rules_file = route_config.get('domain_specific_rules', None)

    html_view = (J2HtmlCapturesView.
                 create_template(route_config.get('query_html'),
                                 'Captures Page'))

    server_cls = route_config.get('server_cls')

    query_handler = QueryHandler.init_from_config(route_config,
                                                  ds_rules_file,
                                                  html_view,
                                                  server_cls)

    return query_handler


#=================================================================
def add_cdx_api_handler(name, cdx_api_suffix, routes, query_handler):
    # if bool, use -cdx suffix, else use custom string
    # as the suffix
    if isinstance(cdx_api_suffix, bool):
        name += '-cdx'
    else:
        name += str(cdx_api_suffix)

    routes.append(Route(name, CDXAPIHandler(query_handler)))


#=================================================================
def create_cdx_server_app(passed_config):
    """
    Create a cdx server api-only app
    For each collection, create a /<coll>-cdx access point
    which follows the cdx api
    """

    defaults = load_yaml_config(DEFAULT_CONFIG)

    config = DictChain(passed_config, defaults)

    collections = config.get('collections')

    routes = []

    for name, value in collections.iteritems():
        route_config = init_route_config(value, config)
        query_handler = init_collection(route_config)

        cdx_api_suffix = route_config.get('enable_cdx_api', True)

        add_cdx_api_handler(name, cdx_api_suffix, routes, query_handler)

    return ArchivalRouter(routes)


#=================================================================
class DirectoryCollsLoader(object):
    def __init__(self, config, static_routes):
        self.config = config
        self.static_routes = static_routes

    def __call__(self):
        colls = {}

        root_dir = self.config.get('collections_root')
        if not root_dir:
            return colls

        for name in os.listdir(root_dir):
            full = os.path.join(root_dir, name)
            if not os.path.isdir(full):
                continue

            coll = self.load_dir(full, name)
            if coll:
                colls[name] = coll

        return colls

    def _add_if_exists(self, coll, root_dir, dir_key, required=False):
        if dir_key in coll:
            # already set
            return False

        thedir = self.config.get('paths').get(dir_key)

        if not thedir:
            msg = 'No "{0}" for collection {1}'.format(dir_key, root_dir)
            if required:
                raise Exception(msg)
            else:
                logging.warn(msg)
            return False

        fulldir = os.path.join(root_dir, thedir)
        if os.path.isdir(fulldir):
            fulldir = os.path.abspath(fulldir) + os.path.sep
            coll[dir_key] = fulldir
            return True
        elif required:
            msg = 'Dir "{0}" does not exist for "{1}"'.format(fulldir, dir_key)
            raise Exception(msg)

        return False

    def load_dir(self, root_dir, name):
        config_file = os.path.join(root_dir, 'config.yaml')
        if os.path.isfile(config_file):
            coll = load_yaml_file(config_file)
        else:
            coll = {}

        self._add_if_exists(coll, root_dir, 'index_paths', True)
        self._add_if_exists(coll, root_dir, 'archive_paths', True)

        if self._add_if_exists(coll, root_dir, 'static_path', False):
            self.static_routes['static/' + name] = coll['static_path']

        # Add templates
        templates_dir = self.config.get('paths').get('templates_dir')
        if templates_dir:
            template_dir = os.path.join(root_dir, templates_dir)
            if template_dir:
                for tname, tfile in self.config.get('paths')['template_files'].iteritems():
                    if tname in coll:
                        # Already set
                        continue

                    full = os.path.join(template_dir, tfile)
                    if os.path.isfile(full):
                        coll[tname] = full

        return coll


#=================================================================
def create_wb_router(passed_config={}):

    defaults = load_yaml_config(DEFAULT_CONFIG)

    config = DictChain(passed_config, defaults)

    routes = []

    port = config.get('port')

    collections = config.get('collections', {})

    static_routes = config.get('static_routes', {})

    # collections based on file system
    dir_loader = DirectoryCollsLoader(config, static_routes)
    collections.update(dir_loader())

    if config.get('enable_memento', False):
        request_class = MementoRequest
    else:
        request_class = WbRequest

    # store live and replay handlers
    handler_dict = {}

    # setup template globals
    template_globals = config.get('template_globals')
    if template_globals:
        add_env_globals(template_globals)

    for name, value in collections.iteritems():
        if isinstance(value, BaseHandler):
            handler_dict[name] = value
            routes.append(Route(name, value, config=route_config))
            continue

        route_config = init_route_config(value, config)

        if route_config.get('index_paths') == '$liveweb':
            live = create_live_handler(route_config)
            handler_dict[name] = live
            routes.append(Route(name, live, config=route_config))
            continue

        query_handler = init_collection(route_config)

        wb_handler = create_wb_handler(
            query_handler=query_handler,
            config=route_config,
        )

        handler_dict[name] = wb_handler

        logging.debug('Adding Collection: ' + name)

        route_class = route_config.get('route_class', Route)

        routes.append(route_class(name, wb_handler,
                                  config=route_config,
                                  request_class=request_class))

        # cdx query handler
        cdx_api_suffix = route_config.get('enable_cdx_api', False)

        if cdx_api_suffix:
            add_cdx_api_handler(name, cdx_api_suffix, routes, query_handler)

    if config.get('debug_echo_env', False):
        routes.append(Route('echo_env', DebugEchoEnvHandler()))

    if config.get('debug_echo_req', False):
        routes.append(Route('echo_req', DebugEchoHandler()))

    for static_name, static_path in static_routes.iteritems():
        routes.append(Route(static_name, StaticHandler(static_path)))

    # resolve any cross handler references
    for route in routes:
        if hasattr(route.handler, 'resolve_refs'):
            route.handler.resolve_refs(handler_dict)

    # default to regular archival mode
    router = ArchivalRouter

    if config.get('enable_http_proxy', False):
        router = ProxyArchivalRouter

        view = J2TemplateView.create_template(
            config.get('proxy_select_html'),
            'Proxy Coll Selector')

        if 'proxy_options' not in passed_config:
            passed_config['proxy_options'] = {}

        if view:
            passed_config['proxy_options']['proxy_select_view'] = view

        view = J2TemplateView.create_template(
            config.get('proxy_cert_download_html'),
            'Proxy Cert Download')

        if view:
            passed_config['proxy_options']['proxy_cert_download_view'] = view

    # Finally, create wb router
    return router(
        routes,
        port=port,

        abs_path=config.get('absolute_paths', True),

        home_view=J2TemplateView.create_template(config.get('home_html'),
                                                 'Home Page'),

        error_view=J2TemplateView.create_template(config.get('error_html'),
                                                 'Error Page'),
        config=config
    )
