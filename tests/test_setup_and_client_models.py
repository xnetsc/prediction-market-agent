import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
from prediction_market_agent.core.config import Config
from prediction_market_agent.runtime.dashboard import create_app
from prediction_market_agent.runtime.setup_guide import setup_guide
from prediction_market_agent.plugins.providers._model_catalog import ClientModelCatalog, normalize_models
from prediction_market_agent.plugins.providers.codex import CodexCliBackend
from prediction_market_agent.plugins.providers.claude import ClaudeCliBackend


def plugin(name,ready,fields=()):
    return {'name':name,'enabled':True,'readiness':{'ready':ready,'reasons':[]},'configuration':{'fields':list(fields)}}


class SetupGuideTests(unittest.TestCase):
    def test_missing_requirements_and_one_ready_provider(self):
        fields=[{'name':'PRIVATE_TOKEN','label':'平台密钥','description':'由平台提供','required':True,'configured':False,'value':''}]
        manifest={'plugins':{'api':[plugin('one',False,fields)],'decision_provider':[plugin('working',True),plugin('optional',False)],'decision_strategy':[]},'robot_paused':False}
        result=setup_guide({'running':False},manifest,automatic_start=True)
        self.assertEqual(result['state'],'not_running')
        self.assertTrue(result['steps'][0]['ready'])
        self.assertNotIn('missing_fields',result['steps'][1]['actions'][0])
        self.assertFalse(any(s['id']=='strategy' for s in result['steps']))
        self.assertFalse(any(s['id']=='pause' for s in result['steps']))
        self.assertEqual(result['remaining'],1)

    def test_paused_partial_and_management_only_are_distinct(self):
        manifest={'plugins':{},'robot_paused':False}
        for runtime,automatic,state in [({'robot_paused':True},True,'paused'),
            ({'running':True,'platforms':{'a':{'running':True},'b':{'running':False}}},True,'partial'),
            ({'running':True},True,'running'),({'running':False},False,'management_only')]:
            with self.subTest(state=state):self.assertEqual(setup_guide(runtime,manifest,automatic_start=automatic)['state'],state)

    def test_management_endpoint_explains_disabled_runtime_without_starting_engine(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            config=Config(working_directory=root,session_db=root/'session.db',auth_db=root/'auth.db',
                application_config_file=root/'application.json',management_file=root/'selection.json',plugin_directories_file=root/'directories.json')
            with patch('prediction_market_agent.runtime.controller.TradingEngine',side_effect=AssertionError('Should not start trading')):
                with TestClient(create_app(config,start_robot=False),base_url='http://localhost') as client:
                    result=client.post('/api/local',json={'url':'/api/runtime'})
                    self.assertEqual(result.status_code,200)
                    self.assertEqual(result.json()['setup']['state'],'management_only')
                    self.assertGreater(result.json()['setup']['remaining'],0)


class ClientModelsTests(unittest.TestCase):
    def test_metadata_and_model_specific_efforts(self):
        catalog=ClientModelCatalog('claude',None)
        models=normalize_models('claude',[{'value':'default','displayName':'Default','supportsEffort':True,'supportedEffortLevels':['low','high']},{'value':'quick'}])
        with patch.object(catalog,'models',return_value=models):
            self.assertEqual([x['value'] for x in catalog.choices('CLAUDE_EFFORT',{'CLAUDE_MODEL':''})],['','low','high'])
            self.assertEqual(catalog.choices('CLAUDE_EFFORT',{'CLAUDE_MODEL':'quick'}),[{'value':'','label':'使用客户端默认强度'}])
            self.assertEqual(len(catalog.choices('CLAUDE_MODEL',{})),3)
        with self.assertRaises(ValueError):normalize_models('codex',[{'model':'x','supportedReasoningEfforts':[{'reasoningEffort':3}]}])

    def test_protocol_initialization_and_pagination_never_send_prompt(self):
        for client,lines in [('codex',[{'id':1,'result':{}},{'id':2,'result':{'data':[{'model':'one'}],'nextCursor':'next'}},{'id':3,'result':{'data':[{'model':'two'}],'nextCursor':None}}]),
                             ('claude',[{'type':'control_response','response':{'request_id':'model-catalog','subtype':'success','response':{'models':[{'value':'one'}]}}}])]:
            with self.subTest(client=client):
                process=SimpleNamespace(stdin=io.StringIO(),stdout=io.StringIO(''.join(json.dumps(line)+'\n' for line in lines)))
                values=ClientModelCatalog(client,None)._read(process)
                self.assertTrue(values)
                sent=[json.loads(line) for line in process.stdin.getvalue().splitlines()]
                self.assertNotIn('turn/start',process.stdin.getvalue())
                self.assertNotIn('"type": "user"',process.stdin.getvalue())
                if client=='codex':self.assertEqual(sent[-1]['params']['cursor'],'next')

    def test_model_list_errors_do_not_expose_private_paths_or_tokens(self):
        control=SimpleNamespace(executable=lambda:'/not/installed',environment=lambda:{})
        with patch('subprocess.Popen',side_effect=OSError('secret-material')):
            with self.assertRaises(ValueError) as caught:ClientModelCatalog('claude',control).models()
        self.assertNotIn('secret-material',str(caught.exception))

    def test_effort_is_forwarded_to_both_cli_commands(self):
        with patch('prediction_market_agent.plugins.providers.codex.resolve_executable',return_value='codex'),patch('prediction_market_agent.plugins.providers.codex.subprocess.run',return_value=SimpleNamespace(returncode=1,stdout='',stderr='fixture')) as run:
            backend=CodexCliBackend('codex','chosen','',20,effort='xhigh')
            with self.assertRaises(Exception):backend.complete('fixture',{},'fixture')
            self.assertIn('model_reasoning_effort="xhigh"',run.call_args.args[0])
        with patch('prediction_market_agent.plugins.providers.claude.resolve_executable',return_value='claude'),patch('prediction_market_agent.plugins.providers.claude.subprocess.run',return_value=SimpleNamespace(returncode=0,stdout='{"structured_output":{}}',stderr='')) as run:
            backend=ClaudeCliBackend('claude','chosen','',20,effort='high');backend.complete('fixture',{},'fixture')
            command=run.call_args.args[0];self.assertEqual(command[command.index('--effort')+1],'high')
