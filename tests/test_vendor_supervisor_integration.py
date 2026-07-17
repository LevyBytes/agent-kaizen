"""H2.3/H2.4 supervisor integration: fail-closed capabilities, gates, and vendor lifecycle."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _harness import REPO_ROOT, kaizen  # noqa: E402
from test_session_drive import _DEAD_OLLAMA, _PREAMBLE, _DrivenSubprocess, _rmtree  # noqa: E402

sys.path.insert(0, str(REPO_ROOT))

FAKE_CLAUDE_WORKER = str(Path(__file__).resolve().parent / "fake_claude_agent_worker.py")


class CapabilityIntegrationTest(unittest.TestCase):
    def test_normal_local_descriptor_uses_exact_code_evidence_while_vendors_stay_probe_gated(self) -> None:
        from kaizen_components.orchestration import supervisor as supervisor_module
        from kaizen_components.orchestration.supervisor import Supervisor

        sup = Supervisor()
        expected_evidence = {
            "streaming": "local-llm-adapter-ordered-delta",
            "governed_context": "supervisor-session-artifact-materializer",
            "diff_snapshots": "tool-gateway-workspace-proposal-executor",
            "controlled_tools": "tool-gateway-canonical-tool-set",
            "process_execution": "tool-gateway-direct-argv-process",
        }
        spoofed_vendor = {
            "id": "claude", "label": "Claude", "drivable": True,
            "availability": {"state": "available", "code": None, "message": ""},
            "models": [], "auth_modes": ["subscription"], "permission_modes": ["plan"],
            "warnings": [], "_code_proven_features": dict(expected_evidence),
        }
        with mock.patch.object(sup, "_probe_ollama_models", return_value=([], [], "available")):
            raw_local = sup._local_llm_capability()
            with mock.patch.object(sup, "_codex_capability", return_value=dict(spoofed_vendor, id="codex")), \
                 mock.patch.object(sup, "_claude_capability", return_value=dict(spoofed_vendor)):
                built = {entry["id"]: entry for entry in sup._build_capabilities()}

        self.assertEqual(raw_local["_code_proven_features"], expected_evidence)
        self.assertEqual(
            built["local_llm"]["features"],
            {
                "streaming": True,
                "image_attachments": False,
                "governed_context": True,
                "diff_snapshots": True,
                "writer_leasing": True,
                "subscription_auth": False,
                "controlled_tools": True,
                "process_execution": True,
                "test_extension": True,
            },
        )
        self.assertNotIn("_code_proven_features", built["local_llm"])
        for engine in ("codex", "claude"):
            for feature in supervisor_module._LOCAL_LLM_CODE_FEATURE_EVIDENCE:
                self.assertFalse(built[engine]["features"][feature], (engine, feature))

        tampered = dict(raw_local)
        tampered["_code_proven_features"] = {**expected_evidence, "streaming": "unproven"}
        with mock.patch.object(sup, "_local_llm_capability", return_value=tampered), \
             mock.patch.object(sup, "_codex_capability", return_value=dict(spoofed_vendor, id="codex")), \
             mock.patch.object(sup, "_claude_capability", return_value=dict(spoofed_vendor)):
            local_features = sup._build_capabilities()[0]["features"]
        for feature in expected_evidence:
            self.assertFalse(local_features[feature], feature)

    def test_codex_probe_is_d_scoped_and_claude_shape_comes_from_sdk_probe(self) -> None:
        from kaizen_components.orchestration.adapters import codex
        from kaizen_components.orchestration.adapters import claude_sdk
        from kaizen_components.orchestration.supervisor import Supervisor

        seen: dict[str, object] = {}

        def installed(_command="codex", *, runtime_dir=None):
            seen["runtime_dir"] = str(runtime_dir)
            return {
                "id": "codex",
                "label": "Codex",
                "drivable": False,
                "availability": {
                    "state": "policy_gate_unavailable",
                    "code": "DENIED_POLICY_GATE_UNAVAILABLE",
                    "message": "bounded reads unavailable",
                },
                "models": [],
                "default_model": None,
                "default_reasoning_effort": None,
                "auth_modes": ["subscription", "api-key"],
                "permission_modes": [],
                "warnings": [],
            }

        # Mock the Claude module probe so this stays hermetic (drivable regardless of an installed claude):
        # a passing probe canonicalizes to a drivable claude descriptor with all four permission modes.
        def claude_installed(_workspace, *, runtime_root=None, worker_command=None, logger=None):
            del runtime_root, worker_command
            self.assertEqual(logger, sup.log)
            return {
                "id": "claude", "label": "Claude", "drivable": True,
                "availability": {"state": "available", "code": None, "message": ""},
                "models": [{"id": "sonnet", "label": "sonnet",
                            "reasoning_efforts": ["low", "high"], "default_effort": None}],
                "default_model": None, "default_reasoning_effort": None,
                "auth_modes": ["subscription"],
                "permission_modes": ["plan", "ask", "agent", "full"],
                "warnings": [],
                "_streaming_proven": True,
                "_subscription_auth_proven": True,
                "_controlled_tools_proven": True,
            }

        before_temp = os.environ.get("TEMP")
        before_home = os.environ.get("CODEX_HOME")
        sup = Supervisor()
        with mock.patch.object(codex, "installed_capability", side_effect=installed), \
             mock.patch.object(claude_sdk, "probe_capability", side_effect=claude_installed), \
             mock.patch.object(sup, "_frozen_claude_provider_target",
                               return_value=mock.Mock(command=("node", "worker.js"))):
            capabilities = sup._build_capabilities()
        by_id = {entry["id"]: entry for entry in capabilities}

        runtime_dir = Path(str(seen["runtime_dir"])).resolve()
        self.assertTrue(runtime_dir.is_relative_to(sup.runtime_dir.resolve()))
        self.assertEqual(os.environ.get("TEMP"), before_temp)
        self.assertEqual(os.environ.get("CODEX_HOME"), before_home)
        self.assertFalse(by_id["codex"]["drivable"])
        self.assertEqual(by_id["codex"]["availability"]["code"], "DENIED_POLICY_GATE_UNAVAILABLE")
        self.assertEqual(by_id["codex"]["permission_modes"], [])
        self.assertTrue(by_id["claude"]["drivable"])
        self.assertEqual(by_id["claude"]["availability"]["state"], "available")
        self.assertIsNone(by_id["claude"]["availability"]["code"])
        self.assertEqual(by_id["claude"]["permission_modes"], ["plan", "ask", "agent", "full"])
        self.assertEqual(by_id["claude"]["auth_modes"], ["subscription"])
        self.assertTrue(by_id["claude"]["features"]["subscription_auth"])
        for feature in (
            "streaming", "image_attachments", "governed_context", "diff_snapshots",
            "controlled_tools", "process_execution",
        ):
            self.assertFalse(by_id["claude"]["features"][feature], feature)

    def test_only_targeted_feature_probe_results_enable_advanced_features(self) -> None:
        from kaizen_components.orchestration.supervisor import Supervisor

        descriptor = {
            "id": "claude", "label": "Claude", "drivable": True,
            "availability": {"state": "available", "code": None, "message": ""},
            "models": [{"id": "sonnet", "label": "Sonnet"}],
            "auth_modes": ["subscription"], "permission_modes": ["plan"], "warnings": [],
            # Legacy initialize/close claims must not light any advanced feature.
            "_streaming_proven": True, "_image_attachments_proven": True,
            "_governed_context_proven": True, "_diff_snapshots_proven": True,
            "_controlled_tools_proven": True, "_process_execution_proven": True,
            # Only these exact targeted probes are accepted.
            "_probed_features": ["streaming", "controlled_tools", "unknown"],
        }
        sup = Supervisor()
        with mock.patch.object(sup, "_local_llm_capability", return_value=dict(descriptor, id="local_llm")), \
             mock.patch.object(sup, "_codex_capability", return_value=dict(descriptor, id="codex")), \
             mock.patch.object(sup, "_claude_capability", return_value=dict(descriptor)):
            features = sup._build_capabilities()[2]["features"]
        self.assertTrue(features["streaming"])
        self.assertTrue(features["controlled_tools"])
        for feature in ("image_attachments", "governed_context", "diff_snapshots", "process_execution"):
            self.assertFalse(features[feature], feature)

    def test_duplicate_probe_claim_keeps_only_that_feature_dark(self) -> None:
        from kaizen_components.orchestration.supervisor import Supervisor

        descriptor = {
            "id": "claude", "label": "Claude", "drivable": True,
            "availability": {"state": "available", "code": None, "message": ""},
            "models": [{"id": "model-a", "label": "Model A"}],
            "auth_modes": ["subscription"], "permission_modes": ["plan"], "warnings": [],
            "_probed_features": ["streaming", "streaming", "controlled_tools", 7],
        }
        sup = Supervisor()
        with mock.patch.object(sup, "_local_llm_capability", return_value=dict(descriptor, id="local_llm")), \
             mock.patch.object(sup, "_codex_capability", return_value=dict(descriptor, id="codex")), \
             mock.patch.object(sup, "_claude_capability", return_value=dict(descriptor)):
            features = sup._build_capabilities()[2]["features"]
        self.assertFalse(features["streaming"])
        self.assertTrue(features["controlled_tools"])

    def test_fake_worker_targeted_probes_shape_all_six_features_without_provider_calls(self) -> None:
        from kaizen_components.orchestration.adapters import claude_sdk
        from kaizen_components.orchestration.supervisor import Supervisor

        with tempfile.TemporaryDirectory(dir="AI/work") as tmp:
            descriptor = claude_sdk.probe_capability(
                tmp, worker_command=[sys.executable, FAKE_CLAUDE_WORKER],
            )
        sup = Supervisor()
        dark = dict(descriptor, drivable=False, _probed_features=[])
        with mock.patch.object(sup, "_local_llm_capability", return_value=dict(dark, id="local_llm")), \
             mock.patch.object(sup, "_codex_capability", return_value=dict(dark, id="codex")), \
             mock.patch.object(sup, "_claude_capability", return_value=dict(descriptor)):
            features = sup._build_capabilities()[2]["features"]
        for feature in (
            "streaming", "image_attachments", "governed_context", "diff_snapshots",
            "controlled_tools", "process_execution",
        ):
            self.assertTrue(features[feature], feature)

    def test_only_explicit_refresh_requests_supported_models_from_sdk_probe(self) -> None:
        from kaizen_components.orchestration.adapters import claude_sdk
        from kaizen_components.orchestration.supervisor import Supervisor

        descriptor = claude_sdk.unavailable_capability()
        sup = Supervisor()
        target = mock.Mock(command=("node", "worker.js"))
        with mock.patch.object(sup, "_frozen_claude_provider_target", return_value=target), \
             mock.patch.object(claude_sdk, "probe_capability", return_value=descriptor) as probe:
            sup._claude_capability()
            sup._claude_capability(refresh_catalog=True)
        self.assertEqual(probe.call_args_list[0].kwargs["logger"], sup.log)
        self.assertEqual(probe.call_args_list[1].kwargs["logger"], sup.log)
        self.assertNotIn("refresh_models", probe.call_args_list[0].kwargs)
        self.assertIs(probe.call_args_list[1].kwargs["refresh_models"], True)


class CapabilityCacheTtlTest(unittest.TestCase):
    @staticmethod
    def _catalog(model_id: str) -> list[dict]:
        return [{"id": "claude", "models": [{"id": model_id}]}]

    def test_capabilities_refresh_is_explicit_and_ttl_boundary_uses_startup_catalog(self) -> None:
        from kaizen_components.orchestration import supervisor as supervisor_module
        from kaizen_components.orchestration.claude_worker_protocol import MODEL_CATALOG_TTL_SECONDS
        from kaizen_components.orchestration.supervisor import Supervisor

        sup = Supervisor()
        sup._capabilities = self._catalog("cached")
        sup._capabilities_built_at = 10.0

        with mock.patch.object(supervisor_module.time, "monotonic", return_value=309.999), \
             mock.patch.object(sup, "_build_capabilities", side_effect=AssertionError("early rebuild")):
            cached = sup._handle_session_capabilities({})
        self.assertEqual(cached["engines"][0]["models"][0]["id"], "cached")

        with mock.patch.object(supervisor_module.time, "monotonic", side_effect=[310.0, 311.0]), \
             mock.patch.object(sup, "_build_capabilities", return_value=self._catalog("ttl")) as build:
            ttl = sup._handle_session_capabilities({})
        self.assertEqual(MODEL_CATALOG_TTL_SECONDS, 300)
        build.assert_called_once_with()
        self.assertEqual(ttl["engines"][0]["models"][0]["id"], "ttl")
        self.assertEqual(sup._capabilities_built_at, 311.0)

        with mock.patch.object(supervisor_module.time, "monotonic", side_effect=[312.0, 313.0]), \
             mock.patch.object(sup, "_build_capabilities", return_value=self._catalog("explicit")) as build:
            explicit = sup._handle_session_capabilities({"refresh": True})
        build.assert_called_once_with(refresh_claude_catalog=True)
        self.assertEqual(explicit["engines"][0]["models"][0]["id"], "explicit")

    def test_start_gate_cache_read_enforces_the_same_exact_ttl_boundary(self) -> None:
        from kaizen_components.orchestration import supervisor as supervisor_module
        from kaizen_components.orchestration.supervisor import Supervisor

        sup = Supervisor()
        sup._capabilities = self._catalog("cached")
        sup._capabilities_built_at = 10.0
        with mock.patch.object(supervisor_module.time, "monotonic", return_value=309.999), \
             mock.patch.object(sup, "_build_capabilities", side_effect=AssertionError("early rebuild")):
            cached = sup._capability_for_engine("claude")
        self.assertEqual(cached["models"][0]["id"], "cached")

        with mock.patch.object(supervisor_module.time, "monotonic", side_effect=[310.0, 311.0]), \
             mock.patch.object(sup, "_build_capabilities", return_value=self._catalog("ttl")) as build:
            ttl = sup._capability_for_engine("claude")
        build.assert_called_once_with()
        self.assertEqual(ttl["models"][0]["id"], "ttl")
        self.assertEqual(sup._capabilities_built_at, 311.0)


class AdapterConstructionBranchTest(unittest.TestCase):
    def _snapshot(self, engine: str):
        from kaizen_components.orchestration import policy

        return policy.build_policy_snapshot(
            engine=engine,
            permission_mode="ask",
            workspace_root=str(REPO_ROOT),
            designated_write_roots=[],
            rules=[],
            protected_paths=[],
            vendor_config_paths=[],
        )

    def test_vendor_constructor_kwargs_are_engine_specific(self) -> None:
        from kaizen_components.orchestration import policy
        from kaizen_components.orchestration import adapters
        from kaizen_components.orchestration.supervisor import Supervisor

        sup = Supervisor()
        fake_codex = mock.Mock()
        with mock.patch.object(adapters, "create_adapter", return_value=fake_codex) as create:
            codex_adapter = sup._build_driven_adapter(
                "preflight-codex",
                {"engine_name": "codex", "model": "must-not-pass", "max_turns": 7,
                 "approval_timeout": 9.0, "tools": {"bad": object()}},
                snapshot=self._snapshot("codex"),
                recorder_override=lambda _event: None,
            )
        args, kwargs = create.call_args
        self.assertEqual(args[0], "codex")
        self.assertIsInstance(args[1], policy.PolicyEngine)
        self.assertNotIn("model", kwargs)
        self.assertNotIn("max_turns", kwargs)
        self.assertNotIn("tools", kwargs)
        self.assertIn("codex_home", kwargs)
        self.assertIn("env", kwargs)
        sup._cleanup_vendor_runtime(codex_adapter)

        fake_claude = mock.Mock()
        with mock.patch.object(adapters, "create_adapter", return_value=fake_claude) as create:
            claude_adapter = sup._build_driven_adapter(
                "preflight-claude",
                {"engine_name": "claude", "model": "must-not-pass", "max_turns": 7,
                 "approval_timeout": 9.0, "tools": {"bad": object()}},
                snapshot=self._snapshot("claude"),
                recorder_override=lambda _event: None,
            )
        args, kwargs = create.call_args
        self.assertEqual(args, ("claude",))
        self.assertNotIn("model", kwargs)
        self.assertNotIn("max_turns", kwargs)
        self.assertNotIn("tools", kwargs)
        self.assertNotIn("codex_home", kwargs)
        self.assertIn("env", kwargs)
        self.assertEqual(kwargs["workspace_root"], sup.repo_root)
        self.assertEqual(kwargs["runtime_root"], sup.repo_root)
        sup._cleanup_vendor_runtime(claude_adapter)


class FutureVendorLifecycleTest(_DrivenSubprocess):
    def test_boot_capability_build_is_the_first_cached_read(self) -> None:
        out = self.drive(
            "from unittest import mock\n"
            "sup = Supervisor(); sup.boot()\n"
            "stamp = sup._capabilities_built_at\n"
            "with mock.patch.object(sup, '_build_capabilities', side_effect=AssertionError('reprobe')):\n"
            "    caps = sup._handle_control({'op':'session/capabilities','args':{}})\n"
            "sup.shutdown()\n"
            "out = {'stamp': stamp, 'status': caps['status'], 'ids': [x['id'] for x in caps['engines']]}\n",
            env={**_DEAD_OLLAMA, "PATH": ""},
        )
        self.assertGreater(out["stamp"], 0.0)
        self.assertEqual(out["status"], "OK")
        self.assertEqual(out["ids"], ["local_llm", "codex", "claude"])

    def test_claude_missing_required_effort_denies_before_records_or_adapter_spawn(self) -> None:
        out = self.drive(
            "from kaizen_components import db\n"
            "created=[]\n"
            "def factory(run_id, recorder, kwargs): created.append(run_id); raise AssertionError('adapter must not spawn')\n"
            "sup=Supervisor(); sup.policy=policy.build_engine_from_db(); sup._adapter_factory=factory\n"
            "sup._capabilities=[{'id':'claude','label':'Claude','drivable':True,\n"
            " 'availability':{'state':'available','code':None,'message':''},\n"
            " 'models':[{'id':'account-model','label':'Account Model','reasoning_efforts':['low','high']}],\n"
            " 'default_model':None,'default_reasoning_effort':'high','auth_modes':['subscription'],\n"
            " 'permission_modes':['plan'],'warnings':[]}]\n"
            "denied=sup._handle_control({'op':'session/start','args':{'engine':'claude','prompt':'x',\n"
            " 'model':'account-model','profile':{'permission_mode':'plan'}}})\n"
            "counts=[db.fetch_one('SELECT COUNT(*) FROM agent_sessions')[0],\n"
            " db.fetch_one('SELECT COUNT(*) FROM agent_runs')[0],db.fetch_one('SELECT COUNT(*) FROM agent_events')[0]]\n"
            "sup._capabilities[0]['models']=[{'id':'no-effort-model','label':'No Effort','reasoning_efforts':[]}]\n"
            "sup._capabilities[0]['default_reasoning_effort']=None\n"
            "valid_no_effort=sup._validate_profile('claude','no-effort-model',{'permission_mode':'plan'},False)\n"
            "bad_no_effort=sup._validate_profile('claude','no-effort-model',{'permission_mode':'plan','reasoning_effort':'high'},False)\n"
            "bad_id=sup._validate_profile('claude','missing-model',{'permission_mode':'plan'},False)\n"
            "sup._capabilities=[{'id':'codex','models':[{'id':'codex-model','reasoning_efforts':['high']}],\n"
            " 'default_model':None,'default_reasoning_effort':None,'auth_modes':['subscription'],\n"
            " 'permission_modes':['plan']}]\n"
            "codex=sup._validate_profile('codex','codex-model',{'permission_mode':'plan'},False)\n"
            "out={'denied':denied,'counts':counts,'created':len(created),'valid_no_effort':valid_no_effort,\n"
            " 'bad_no_effort':bad_no_effort,'bad_id':bad_id,'codex':codex}\n",
            env=_DEAD_OLLAMA,
        )
        self.assertEqual(out["denied"]["code"], "DENIED_EFFORT_UNSUPPORTED")
        self.assertIsNone(out["denied"]["reasoning_effort"])
        self.assertEqual(out["denied"]["allowed"], ["low", "high"])
        self.assertEqual(out["counts"], [0, 0, 0])
        self.assertEqual(out["created"], 0)
        self.assertIsNone(out["valid_no_effort"]["reasoning_effort"])
        self.assertEqual(out["bad_no_effort"]["code"], "DENIED_EFFORT_UNSUPPORTED")
        self.assertEqual(out["bad_id"]["code"], "DENIED_MODEL_UNAVAILABLE")
        self.assertIsNone(out["codex"]["reasoning_effort"])

    def test_claude_budget_denial_is_zero_record_and_runtime_exhaustion_terminal(self) -> None:
        out = self.drive(
            "from kaizen_components import db\n"
            "from kaizen_components.orchestration.adapters import TurnResult\n"
            "class BudgetVendor:\n"
            "    def __init__(self): self._active=None\n"
            "    @property\n"
            "    def active_turn_id(self): return self._active\n"
            "    def open(self, profile, snapshot): return {'status':'OK','profile':dict(profile)}\n"
            "    def bind_session(self, session_id): self.session_id=session_id\n"
            "    def on_approval(self, callback): self.approval=callback\n"
            "    def on_delta(self, callback): self.delta=callback\n"
            "    def run_turn(self, prompt):\n"
            "        return TurnResult('FAILED', vendor_turn_id='budget-1', error_code='MODEL_CALL_BUDGET_EXHAUSTED', fatal=True)\n"
            "    def kill(self): return {'status':'OK','killed':True,'termination_proven':True}\n"
            "    def close(self): return {'status':'OK','closed':True,'termination_proven':True}\n"
            "created=[]\n"
            "def factory(run_id, recorder, kwargs):\n"
            "    adapter=BudgetVendor(); created.append(adapter); return adapter\n"
            "sup=Supervisor(); sup.policy=policy.build_engine_from_db(); sup._adapter_factory=factory\n"
            "sup._capabilities=[{'id':'claude','label':'Claude','drivable':True,\n"
            " 'availability':{'state':'available','code':None,'message':''},\n"
            " 'models':[{'id':'claude-test-model','label':'Claude Test Model','reasoning_efforts':['high'],'default_effort':'high'}],\n"
            " 'default_model':'claude-test-model','default_reasoning_effort':'high','auth_modes':['subscription'],\n"
            " 'permission_modes':['plan'],'warnings':[],'features':{'streaming':False}}]\n"
            "bad=sup._handle_control({'op':'session/start','args':{'engine':'claude','prompt':'x','max_turns':33,\n"
            " 'model':'claude-test-model','profile':{'permission_mode':'plan','reasoning_effort':'high'}}})\n"
            "zero=[db.fetch_one('SELECT COUNT(*) FROM agent_sessions')[0],db.fetch_one('SELECT COUNT(*) FROM agent_runs')[0],\n"
            " db.fetch_one('SELECT COUNT(*) FROM agent_events')[0]]\n"
            "start=sup._handle_control({'op':'session/start','args':{'engine':'claude','prompt':'x','max_turns':2,\n"
            " 'model':'claude-test-model','profile':{'permission_mode':'plan','reasoning_effort':'high'}}})\n"
            "state=wait_terminal(sup,start['agent_run_id'])\n"
            "events=sup._handle_control({'op':'session/events','args':{'agent_run_id':start['agent_run_id'],'since':0}})\n"
            "out={'bad':bad,'zero':zero,'state':state,'wire_terminal':events['terminal'],\n"
            " 'wire_state':events.get('terminal_state'),'turn_state':events.get('turn_state')}\n",
            env=_DEAD_OLLAMA,
        )
        self.assertEqual(out["bad"]["code"], "MODEL_CALL_BUDGET_EXHAUSTED")
        self.assertEqual(out["zero"], [0, 0, 0])
        self.assertTrue(out["state"]["terminal"])
        self.assertEqual(out["state"]["terminal_state"], "failure")
        self.assertTrue(out["wire_terminal"])
        self.assertEqual(out["wire_state"], "failure")
        self.assertEqual(out["turn_state"], "terminal")

    def test_vendor_profile_and_preflight_denials_leave_zero_rows(self) -> None:
        out = self.drive(
            "from pathlib import Path\n"
            "from kaizen_components import db\n"
            "class Refusal(Exception):\n"
            "    def payload(self): return {'status': 'DENIED', 'code': 'DENIED_CREDENTIAL_FILE', 'required_action': 'repair credential file'}\n"
            "class FailingVendor:\n"
            "    def __init__(self, recorder, kwargs): self.killed = False; self.runtime = None\n"
            "    @property\n"
            "    def active_turn_id(self): return None\n"
            "    def open(self, profile, snapshot): self.runtime = self._kaizen_vendor_runtime; raise Refusal()\n"
            "    def kill(self): self.killed = True; return {'status': 'OK', 'killed': True}\n"
            "created = []\n"
            "def factory(run_id, recorder, kwargs): adapter = FailingVendor(recorder, kwargs); created.append(adapter); return adapter\n"
            "sup = Supervisor(); sup.policy = policy.build_engine_from_db(); sup._adapter_factory = factory\n"
            "sup._capabilities = [{'id': 'codex', 'label': 'Codex', 'drivable': True, 'availability': {'state': 'available', 'code': None, 'message': ''},\n"
            " 'models': [], 'default_model': None, 'default_reasoning_effort': None, 'auth_modes': ['subscription', 'api-key'], 'permission_modes': ['ask'], 'warnings': []}]\n"
            "bad_auth = sup._handle_control({'op': 'session/start', 'args': {'engine': 'codex', 'prompt': 'x', 'profile': {'permission_mode': 'ask', 'auth_mode': 'none'}}})\n"
            "bad_permission = sup._handle_control({'op': 'session/start', 'args': {'engine': 'codex', 'prompt': 'x', 'profile': {'permission_mode': 'plan'}}})\n"
            "preflight = sup._handle_control({'op': 'session/start', 'args': {'engine': 'codex', 'prompt': 'x', 'profile': {'permission_mode': 'ask'}}})\n"
            "counts = [db.fetch_one('SELECT COUNT(*) FROM agent_sessions')[0], db.fetch_one('SELECT COUNT(*) FROM agent_runs')[0]]\n"
            "adapter = created[0]\n"
            "out = {'bad_auth': bad_auth['code'], 'bad_permission': bad_permission['code'], 'preflight': preflight['code'],\n"
            " 'counts': counts, 'killed': adapter.killed, 'runtime_exists': Path(adapter.runtime).exists(), 'gates': len(sup._session_policy_gates)}\n",
            env=_DEAD_OLLAMA,
        )
        self.assertEqual(out["bad_auth"], "DENIED_PROFILE_UNSUPPORTED")
        self.assertEqual(out["bad_permission"], "DENIED_PROFILE_UNSUPPORTED")
        self.assertEqual(out["preflight"], "DENIED_CREDENTIAL_FILE")
        self.assertEqual(out["counts"], [0, 0])
        self.assertTrue(out["killed"])
        self.assertFalse(out["runtime_exists"])
        self.assertEqual(out["gates"], 0)

    def test_future_compatible_codex_preflights_before_rows_and_cleans_gate(self) -> None:
        out = self.drive(
            "from pathlib import Path\n"
            "from kaizen_components import db\n"
            "from kaizen_components.orchestration.adapters import TurnResult\n"
            "class FakeVendor:\n"
            "    def __init__(self, recorder, kwargs):\n"
            "        self.recorder, self.kwargs = recorder, dict(kwargs)\n"
            "        self.open_counts, self.bind_counts, self.turn_counts = [], [], []\n"
            "        self.bound = None; self.closed = False; self.killed = False; self.runtime = None\n"
            "        self.snapshot = None; self._active = None; self._approval = None; self._child = None\n"
            "    @property\n"
            "    def active_turn_id(self): return self._active\n"
            "    @property\n"
            "    def hook_registration(self):\n"
            "        return {'gate_id': 'future-gate', 'profile_hash': self.snapshot.profile_hash}\n"
            "    def open(self, profile, snapshot):\n"
            "        self.snapshot = snapshot; self.runtime = self._kaizen_vendor_runtime\n"
            "        self.open_counts.append([db.fetch_one('SELECT COUNT(*) FROM agent_sessions')[0], db.fetch_one('SELECT COUNT(*) FROM agent_runs')[0]])\n"
            "        self.recorder({'event_kind': 'session', 'marker': 'open', 'summary': 'buffered preflight', 'payload': {}})\n"
            "        return {'status': 'OK', 'profile': dict(profile)}\n"
            "    def bind_session(self, session_id):\n"
            "        self.bound = session_id\n"
            "        self.bind_counts.append([db.fetch_one('SELECT COUNT(*) FROM agent_sessions WHERE id = ?', (session_id,))[0], db.fetch_one('SELECT COUNT(*) FROM agent_runs')[0]])\n"
            "    def on_approval(self, cb): self._approval = cb\n"
            "    def run_turn(self, prompt):\n"
            "        self.turn_counts.append([db.fetch_one('SELECT COUNT(*) FROM agent_sessions')[0], db.fetch_one('SELECT COUNT(*) FROM agent_runs')[0]])\n"
            "        return TurnResult('OK', vendor_turn_id='vendor-' + str(len(self.turn_counts)), final_text='reply ' + prompt, fatal=False)\n"
            "    def steer(self, text): return {'status': 'OK'}\n"
            "    def interrupt(self): return {'status': 'OK'}\n"
            "    def close(self): self.closed = True; return {'status': 'OK', 'closed': True}\n"
            "    def kill(self): self.killed = True; return {'status': 'OK', 'killed': True}\n"
            "created = []\n"
            "def factory(run_id, recorder, kwargs):\n"
            "    adapter = FakeVendor(recorder, kwargs); created.append(adapter); return adapter\n"
            "sup = Supervisor(); sup.policy = policy.build_engine_from_db(); sup._adapter_factory = factory\n"
            "sup._capabilities = [\n"
            " {'id': 'codex', 'label': 'Codex', 'drivable': True, 'availability': {'state': 'available', 'code': None, 'message': ''},\n"
            "  'models': [{'id': 'future-model', 'label': 'future-model', 'reasoning_efforts': ['high'], 'default_effort': 'high'}],\n"
            "  'default_model': 'future-model', 'default_reasoning_effort': 'high', 'auth_modes': ['subscription', 'api-key'], 'permission_modes': ['ask'], 'warnings': []}\n"
            "]\n"
            "start = sup._handle_control({'op': 'session/start', 'args': {'engine': 'codex', 'prompt': 'one', 'profile': {'permission_mode': 'ask'}}})\n"
            "rid = start['agent_run_id']; adapter = created[0]; wait_idle(sup, rid)\n"
            "gate = sup._handle_control({'op': 'session/policy-check', 'args': {'gate_id': 'future-gate', 'profile_hash': start['profile_hash'], 'payload': {'hook_event_name': 'PreToolUse', 'tool_name': 'Bash', 'tool_input': {'command': 'git push origin main'}, 'cwd': str(sup.repo_root)}}})\n"
            "mismatch = sup._handle_control({'op': 'session/policy-check', 'args': {'gate_id': 'future-gate', 'profile_hash': 'wrong', 'payload': {}}})\n"
            "unknown = sup._handle_control({'op': 'session/policy-check', 'args': {'gate_id': 'missing', 'profile_hash': start['profile_hash'], 'payload': {}}})\n"
            "t8_before = db.fetch_one(\"SELECT COUNT(*) FROM agent_events WHERE agent_run_id = ? AND event_kind = 'finalization'\", (rid,))[0]\n"
            "turn = sup._handle_control({'op': 'session/turn', 'args': {'agent_run_id': rid, 'prompt': 'two'}}); wait_idle(sup, rid)\n"
            "events = sup._handle_control({'op': 'session/events', 'args': {'agent_run_id': rid, 'since': 0}})['events']\n"
            "rows = [db.fetch_one('SELECT COUNT(*) FROM agent_sessions WHERE id = ?', (start['session_id'],))[0], db.fetch_one('SELECT COUNT(*) FROM agent_runs WHERE id = ?', (rid,))[0]]\n"
            "close = sup._handle_control({'op': 'session/close', 'args': {'agent_run_id': rid}})\n"
            "after = sup._handle_control({'op': 'session/policy-check', 'args': {'gate_id': 'future-gate', 'profile_hash': start['profile_hash'], 'payload': {}}})\n"
            "runtime_exists = Path(adapter.runtime).exists()\n"
            "out = {'start': start, 'turn': turn, 'close': close, 'open_counts': adapter.open_counts, 'bind_counts': adapter.bind_counts, 'turn_counts': adapter.turn_counts,\n"
            " 'kwargs': sorted(adapter.kwargs), 'gate': gate['result'], 'mismatch': mismatch['code'], 'unknown': unknown['code'], 'after': after['code'],\n"
            " 'rows': rows, 't8_before': t8_before, 'first': [events[0]['event_kind'], events[0]['marker']], 'runtime_exists': runtime_exists, 'closed': adapter.closed}\n",
            env=_DEAD_OLLAMA,
        )
        self.assertEqual(out["start"]["status"], "OK")
        self.assertEqual(out["start"]["profile"]["auth_mode"], "subscription")
        self.assertEqual(out["start"]["profile"]["model"], "future-model")
        self.assertEqual(out["open_counts"], [[0, 0]])
        self.assertEqual(out["bind_counts"], [[1, 1]])
        self.assertEqual(out["turn_counts"], [[1, 1], [1, 1]])
        self.assertNotIn("model", out["kwargs"])
        self.assertNotIn("max_turns", out["kwargs"])
        self.assertNotIn("tools", out["kwargs"])
        self.assertEqual(out["gate"], "deny")
        self.assertEqual(out["mismatch"], "DENIED_PROFILE_MISMATCH")
        self.assertEqual(out["unknown"], "DENIED_POLICY_GATE_UNAVAILABLE")
        self.assertEqual(out["after"], "DENIED_POLICY_GATE_UNAVAILABLE")
        self.assertEqual(out["rows"], [1, 1])
        self.assertEqual(out["t8_before"], 0)
        self.assertEqual(out["first"], ["profile", "point"])
        self.assertFalse(out["runtime_exists"])
        self.assertTrue(out["closed"])


class RealClaudeAdapterDrivenLifecycleTest(unittest.TestCase):
    """Supervisor multi-turn coverage through the real SDK-worker adapter and hermetic JSONL worker."""

    def _drive_claude(self, body: str) -> dict:
        """Builds a fresh K1-initialized scratch plane, runs a real ClaudeSdkAdapter against the hermetic JSONL worker, returns its RESULT payload, and removes the plane in finally."""
        script = "BODY = " + repr(body) + "\n" + _PREAMBLE
        full = dict(os.environ)
        # Scrub any ambient CLAUDE_CONFIG_DIR so subscription-mode env composition is deterministic.
        full.pop("CLAUDE_CONFIG_DIR", None)
        scratch_parent = REPO_ROOT / "AI/work/harness-ui-v1"
        scratch_parent.mkdir(parents=True, exist_ok=True)
        root = Path(tempfile.mkdtemp(prefix="h2.4-fake-claude-", dir=str(scratch_parent)))
        try:
            rc, payload = kaizen(root, "K1")
            self.assertEqual(rc, 0, payload)
            full["KAIZEN_REPO_ROOT"] = str(root)
            full["KAIZEN_LLM_BASE_URL"] = "http://127.0.0.1:1/v1"
            proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                                  cwd=str(REPO_ROOT), env=full, timeout=180)
            for line in proc.stdout.splitlines():
                if line.startswith("RESULT "):
                    return json.loads(line[len("RESULT "):])
            self.fail(f"no RESULT.\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr[-2000:]}")
        finally:
            _rmtree(root)

    def test_two_turns_resume_one_c1_t5_and_close_writes_one_t8(self) -> None:
        body = (
            "import sys\n"
            "from pathlib import Path\n"
            "from kaizen_components import db\n"
            "from kaizen_components.orchestration import supervisor as S\n"
            "from kaizen_components.orchestration.adapters.claude_sdk import ClaudeSdkAdapter\n"
            "sup = Supervisor(); sup.boot()\n"
            "sup._driven_test_records = True\n"
            "FAKE = " + repr(FAKE_CLAUDE_WORKER) + "\n"
            "spawn_markers = []\n"
            "def factory(run_id, recorder, kwargs):\n"
            "    def tracked_spawner(*args, **spawn_kwargs):\n"
            "        child = kwargs['spawner'](*args, **spawn_kwargs)\n"
            "        spawn_markers.append(json.loads(S.OWNERSHIP_FILE.read_text(encoding='utf-8'))['__workspace_writer__'])\n"
            "        return child\n"
            "    return ClaudeSdkAdapter(worker_command=[sys.executable, FAKE], recorder=recorder,\n"
            "        logger=(lambda _m: None), spawner=tracked_spawner, env=kwargs['env'],\n"
            "        workspace_root=kwargs['workspace_root'], runtime_root=kwargs['runtime_root'])\n"
            "sup._adapter_factory = factory\n"
            "sup._capabilities = [{'id': 'claude', 'label': 'Claude', 'drivable': True,\n"
            "    'availability': {'state': 'available', 'code': None, 'message': ''},\n"
            "    'models': [{'id': 'claude-test-model', 'label': 'Claude Test Model', 'reasoning_efforts': ['high'], 'default_effort': 'high'}],\n"
            "    'default_model': 'claude-test-model', 'default_reasoning_effort': 'high', 'auth_modes': ['subscription'],\n"
            "    'permission_modes': ['plan', 'ask', 'agent', 'full'], 'warnings': [],\n"
            "    'features': {'streaming': True, 'controlled_tools': True}}]\n"
            "children_before = len(sup._children)\n"
            "start = sup._handle_control({'op': 'session/start', 'args': {'engine': 'claude', 'prompt': 'one',\n"
            "    'model': 'claude-test-model', 'max_turns': 8, 'profile': {'permission_mode': 'ask', 'reasoning_effort': 'high'}}})\n"
            "assert start.get('status') == 'OK', start\n"
            "rid, sid = start['agent_run_id'], start['session_id']\n"
            "wait_idle(sup, rid, budget=60.0)\n"
            "pre1 = sup._safe_reduce(rid)\n"
            "t8_mid = db.fetch_one(\"SELECT COUNT(*) FROM agent_events WHERE agent_run_id = ? AND event_kind = 'finalization'\", (rid,))[0]\n"
            "turn2 = sup._handle_control({'op': 'session/turn', 'args': {'agent_run_id': rid, 'prompt': 'two'}})\n"
            "wait_idle(sup, rid, budget=60.0)\n"
            "counts = [db.fetch_one('SELECT COUNT(*) FROM agent_sessions WHERE id = ?', (sid,))[0],\n"
            "          db.fetch_one('SELECT COUNT(*) FROM agent_runs WHERE id = ?', (rid,))[0]]\n"
            "chat = [ (r[0], json.loads(r[1])) for r in db.fetch_all(\"SELECT marker, body FROM agent_events WHERE agent_run_id = ? AND event_kind = 'chat_message' ORDER BY sequence_no\", (rid,))]\n"
            "close = sup._handle_control({'op': 'session/close', 'args': {'agent_run_id': rid}})\n"
            "state = sup._safe_reduce(rid)\n"
            "t8_after = db.fetch_one(\"SELECT COUNT(*) FROM agent_events WHERE agent_run_id = ? AND event_kind = 'finalization' AND marker = 'close_ok'\", (rid,))[0]\n"
            "runtime_root = Path(str(sup.repo_root)) / 'AI' / 'work' / 'orchestration' / 'runtime' / 'claude-driving'\n"
            "leftover = sorted(p.name for p in runtime_root.iterdir()) if runtime_root.exists() else []\n"
            "children_after = len(sup._children)\n"
            "sup.shutdown()\n"
            "out = {'start_status': start['status'], 'turn2_status': turn2['status'], 'close_status': close['status'],\n"
            "    'counts': counts, 'pre1_terminal': pre1['terminal'], 't8_mid': t8_mid, 't8_after': t8_after,\n"
            "    'chat_roles': [(m, b['role']) for (m, b) in chat], 'chat_texts': [b['text'] for (_m, b) in chat],\n"
            "    'leftover': leftover, 'children_before': children_before, 'children_after': children_after,\n"
            "    'terminal': state['terminal'] if state else None, 'terminal_state': state.get('terminal_state') if state else None,\n"
            "    'spawn_markers': spawn_markers}\n"
        )
        out = self._drive_claude(body)
        self.assertEqual(out["start_status"], "OK", out)
        self.assertEqual(out["turn2_status"], "OK", out)
        self.assertEqual(out["close_status"], "OK", out)
        # Exactly one C1 + one T5 across both turns.
        self.assertEqual(out["counts"], [1, 1], out)
        # No T8 while idle; exactly one success T8 at close.
        self.assertFalse(out["pre1_terminal"], out)
        self.assertEqual(out["t8_mid"], 0, out)
        self.assertEqual(out["t8_after"], 1, out)
        # Two user + two assistant chat_message events in order.
        self.assertEqual(
            [role for (_m, role) in out["chat_roles"]],
            ["user", "assistant", "user", "assistant"],
            out,
        )
        self.assertEqual(out["chat_texts"][-1], "FAKE_CLAUDE_OK", out)
        # One SDK worker remains alive across both turns; the conversation does not respawn a CLI.
        # No leaked private runtime dir, no leaked children.
        self.assertEqual(out["leftover"], [], out)
        self.assertEqual(out["children_before"], 0, out)
        self.assertEqual(out["children_after"], 0, out)
        self.assertEqual(len(out["spawn_markers"]), 1, out)
        self.assertTrue(all(marker["child_pids"] for marker in out["spawn_markers"]), out)
        self.assertTrue(out["terminal"], out)
        self.assertEqual(out["terminal_state"], "success", out)

    def test_closed_vendor_conversation_proves_full_resumed_leg_metadata(self) -> None:
        body = (
            "import sys\n"
            "from pathlib import Path\n"
            "from kaizen_components import db\n"
            "from kaizen_components.orchestration.adapters.claude_sdk import ClaudeSdkAdapter\n"
            "sup = Supervisor(); sup.boot(); sup._driven_test_records = True\n"
            "FAKE = " + repr(FAKE_CLAUDE_WORKER) + "\n"
            "def factory(run_id, recorder, kwargs):\n"
            "    return ClaudeSdkAdapter(worker_command=[sys.executable, FAKE], recorder=recorder,\n"
            "        logger=(lambda _m: None), spawner=kwargs['spawner'], env=kwargs['env'],\n"
            "        workspace_root=kwargs['workspace_root'], runtime_root=kwargs['runtime_root'])\n"
            "sup._adapter_factory = factory\n"
            "sup._capabilities = [{'id':'claude','label':'Claude','drivable':True,\n"
            " 'availability':{'state':'available','code':None,'message':''},\n"
            " 'models':[{'id':'claude-test-model','label':'Claude Test Model','reasoning_efforts':['high'],'default_effort':'high'}],\n"
            " 'default_model':'claude-test-model','default_reasoning_effort':'high','auth_modes':['subscription'],\n"
            " 'permission_modes':['plan','ask','agent','full'],'warnings':[], 'features':{'streaming':True,'controlled_tools':True}}]\n"
            "start=sup._handle_control({'op':'session/start','args':{'engine':'claude','prompt':'one',\n"
            " 'model':'claude-test-model','max_turns':3,'profile':{'permission_mode':'plan','reasoning_effort':'high'}}})\n"
            "assert start.get('status') == 'OK', start\n"
            "rid,sid=start['agent_run_id'],start['session_id']\n"
            "wait_idle(sup,rid,budget=60.0)\n"
            "sup._handle_control({'op':'session/close','args':{'agent_run_id':rid}})\n"
            "turn=sup._handle_control({'op':'session/turn','args':{'agent_run_id':rid,'prompt':'two'}})\n"
            "new_rid=turn['agent_run_id']; wait_idle(sup,new_rid,budget=60.0)\n"
            "profile=json.loads(db.fetch_one(\"SELECT body FROM agent_events WHERE agent_run_id=? AND event_kind='profile'\",(new_rid,))[0])\n"
            "entry=sup._handle_control({'op':'session/list','args':{'controller':'driven'}})['sessions'][0]\n"
            "sup._handle_control({'op':'session/close','args':{'agent_run_id':new_rid}}); sup.shutdown()\n"
            "out={'turn':turn,'profile':profile,'entry':entry}\n"
        )
        out = self._drive_claude(body)
        self.assertEqual(out["turn"]["status"], "OK", out)
        self.assertTrue(out["turn"]["transcript_seeded"], out)
        self.assertEqual(out["turn"]["resume_fidelity"], "reduced", out)
        self.assertEqual(out["turn"]["omitted_message_count"], 0, out)
        self.assertEqual(out["profile"]["resume_fidelity"], "reduced", out)
        self.assertEqual(out["entry"]["resume_fidelity"], "reduced", out)
        self.assertEqual(out["turn"]["profile"]["max_turns"], 3, out)
        self.assertEqual(out["profile"]["effective"]["max_turns"], 3, out)
        self.assertEqual(out["entry"]["profile"]["max_turns"], 3, out)


if __name__ == "__main__":
    unittest.main()
