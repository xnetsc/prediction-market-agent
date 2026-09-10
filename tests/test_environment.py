import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request

from fastapi.testclient import TestClient
from prediction_market_agent.core.config import Config, ApplicationConfigStore
from prediction_market_agent.plugin_system.discovery import PluginCatalog, PluginSpec
from prediction_market_agent.plugin_system.network_diagnostics import DiagnosticNetworkRoute, configured_proxy_route
from prediction_market_agent.runtime.environment import EnvironmentDiagnostics, probe_service, services_from, RouteProxy
from prediction_market_agent.runtime.dashboard import create_app


class EnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        root=Path(self.temp.name)
        self.config=Config(working_directory=root,session_db=root/'session.db',auth_db=root/'auth.db',
            application_config_file=root/'application.json',management_file=root/'selection.json',
            plugin_directories_file=root/'directories.json')
        self.settings=ApplicationConfigStore(self.config.application_config_file)

    def test_observations_validate_public_ip_optional_port_and_do_not_expose_response(self):
        service={'name':'fixture','url':'https://echo.example/json','ip_field':'ip','port_field':'port'}
        for value,expected in [({'ip':'8.8.8.8','port':'54321'},'ok'),({'ip':'2606:4700:4700::1111'},'ok'),
                               ({'ip':'127.0.0.1'},'error'),({'ip':'not-an-ip'},'error')]:
            with self.subTest(value=value),patch('urllib.request.build_opener') as build:
                build.return_value.open.return_value=io.BytesIO(json.dumps(value).encode())
                result=probe_service(service,DiagnosticNetworkRoute('direct'),1)
                self.assertEqual(result['status'],expected)
                request=build.return_value.open.call_args.args[0]
                self.assertNotIn('Authorization',dict(request.header_items()))
                self.assertNotIn('Cookie',dict(request.header_items()))
        with patch('urllib.request.build_opener') as build:
            build.return_value.open.side_effect=ValueError('password-do-not-expose')
            self.assertNotIn('password-do-not-expose',json.dumps(probe_service(service,DiagnosticNetworkRoute('x'),1)))
            build.return_value.open.side_effect=HTTPError(service['url'],302,'redirect',{},io.BytesIO())
            self.assertIn('302',probe_service(service,DiagnosticNetworkRoute('x'),1)['error'])

    def test_snapshot_redaction_staleness_and_no_network_on_read(self):
        catalog=PluginCatalog();proxy=['http://person:private-proxy-password@proxy.example:8080']
        catalog.register(PluginSpec('api','fixture','fixture','fixture.py',lambda: self.fail('Must not create trading instance'),
            teardown=lambda:None,network_routes_callback=lambda:(DiagnosticNetworkRoute('HTTP',proxy[0]),)))
        monitor=EnvironmentDiagnostics(self.config,SimpleNamespace(catalog=catalog),self.settings)
        request=SimpleNamespace(client=SimpleNamespace(host='127.0.0.1'),scope={'server':('0.0.0.0',8765)},base_url='http://localhost/')
        with patch('urllib.request.build_opener',side_effect=AssertionError('snapshot made an HTTP request')):
            report=monitor.snapshot(request)
        inherited=next(row for row in report['routes'] if row['id']=='inherited')
        self.assertIn(inherited['proxy'].split(':',1)[0],{'DIRECT','http','https'})
        self.assertNotIn('@',inherited['proxy'])
        self.assertNotIn('private-proxy-password',json.dumps(report))
        self.assertFalse(any(row['result'] for row in report['routes']))
        with patch('prediction_market_agent.runtime.environment.probe_service',return_value={'status':'ok','ip':'8.8.8.8','family':'IPv4'}):
            monitor.probe('api:fixture:0')
        row=next(row for row in monitor.snapshot(request)['routes'] if row['id']=='api:fixture:0')
        self.assertFalse(row['stale'])
        proxy[0]='http://different.example:9999'
        row=next(row for row in monitor.snapshot(request)['routes'] if row['id']=='api:fixture:0')
        self.assertTrue(row['stale'])
        with self.assertRaises(ValueError):monitor.probe('disabled:plugin')

    def test_inherited_route_uses_host_snapshot_and_same_probe_targets(self):
        snapshot=self.config.working_directory/'.deployment'/'host-proxy.json'
        snapshot.parent.mkdir()
        snapshot.write_text(json.dumps({'status':'proxy','proxy':'http://proxy.example:8080','source':'fixture','no_proxy':''}))
        config=Config(**{**self.config.__dict__,'host_proxy_file':Path('.deployment/host-proxy.json')})
        monitor=EnvironmentDiagnostics(config,SimpleNamespace(catalog=PluginCatalog()),self.settings)
        inherited=monitor.routes()['inherited'][1]
        self.assertEqual(inherited.proxy,'http://proxy.example:8080')
        observed=[]
        def result(service,route,timeout):
            observed.append((service['url'],route.proxy))
            return {'status':'ok','ip':'8.8.8.8','family':'IPv4'}
        with patch('prediction_market_agent.runtime.environment.probe_service',side_effect=result):
            monitor.probe('direct');monitor.probe('inherited')
        direct_urls=[url for url,proxy in observed if not proxy]
        inherited_urls=[url for url,proxy in observed if proxy=='http://proxy.example:8080']
        self.assertCountEqual(direct_urls,inherited_urls)
        self.assertEqual(len(direct_urls),len(observed)//2)
        self.assertEqual(len(inherited_urls),len(observed)//2)

    def test_configured_services_empty_list_and_validation(self):
        self.assertEqual(services_from('[]'),[])
        for value in ['{}','[{"name":"x","url":"http://echo.example","ip_field":"ip"}]',
                      '[{"name":"x","url":"https://user:secret@echo.example","ip_field":"ip"}]']:
            with self.assertRaises(ValueError):services_from(value)
        self.settings.save({'environment_probe_services':'[]'})
        monitor=EnvironmentDiagnostics(self.config,SimpleNamespace(catalog=PluginCatalog()),self.settings)
        with patch('urllib.request.build_opener',side_effect=AssertionError('Disabled probes made network request')):
            self.assertEqual(monitor.probe('direct')['status'],'disabled')

    def test_platform_proxy_diagnostics_do_not_require_trading_credentials(self):
        from prediction_market_agent.plugins.api import binance, polymarket
        from prediction_market_agent.plugin_system.discovery import PluginInitializationContext
        for name,module in [('binance',binance),('polymarket',polymarket)]:
            spec=module.initialize_plugin(PluginInitializationContext('api',Path(name+'.py'),self.config.working_directory))
            self.addCleanup(spec.teardown)
            spec.configuration.save_callback({name.upper()+'_HTTP_PROXY':'http://proxy.example:8080'})
            self.assertEqual(spec.network_routes_callback()[0].proxy,'http://proxy.example:8080')
            self.assertFalse(spec.readiness().ready)

    def test_different_observers_are_not_collapsed_to_one_ip(self):
        monitor=EnvironmentDiagnostics(self.config,SimpleNamespace(catalog=PluginCatalog()),self.settings)
        values=[{'status':'ok','ip':'8.8.8.8','family':'IPv4'},
                {'status':'ok','ip':'1.1.1.1','family':'IPv4'},
                {'status':'error','ip':None,'family':None}]
        with patch('prediction_market_agent.runtime.environment.probe_service',side_effect=values):
            result=monitor.probe('direct')
        self.assertEqual(result['status'],'partial')
        self.assertTrue(result['same_family_disagreement'])
        self.assertEqual(len(result['ips']),2)

    def test_diagnostic_route_bypass_is_independent_and_proxy_credentials_stay_proxy_only(self):
        route=DiagnosticNetworkRoute('fixture','http://name:password@proxy.example:8118','bypass.example')
        self.assertNotIn('password',repr(route))
        handler=RouteProxy(route)
        req=Request('https://echo.example/json')
        handler.proxy_open(req,route.proxy,'https')
        self.assertEqual(req.host,'proxy.example:8118')
        self.assertIn('Proxy-authorization',dict(req.header_items()))
        self.assertNotIn('Authorization',dict(req.header_items()))
        bypass=Request('https://bypass.example/json')
        handler.proxy_open(bypass,route.proxy,'https')
        self.assertEqual(bypass.host,'bypass.example')
        with self.assertRaises(ValueError):configured_proxy_route(lambda:{'KEY':'secret'},'PROXY')

    def test_environment_requires_existing_access_control_and_static_asset_is_available(self):
        with TestClient(create_app(self.config,start_robot=False),base_url='http://localhost') as client:
            result=client.post('/api/local',json={'url':'/api/environment'})
            self.assertEqual(result.status_code,200)
            self.assertIn('routes',result.json())
            self.assertIn('environmentPanel',client.get('/').text)
            self.assertEqual(client.get('/assets/environment.js').status_code,200)
        with TestClient(create_app(self.config,start_robot=False),base_url='https://robot.example') as client:
            self.assertEqual(client.post('/api/local',json={'url':'/api/environment/probe','body':{'route_id':'direct'}}).status_code,403)
            self.assertEqual(client.get('/api/environment').status_code,404)
