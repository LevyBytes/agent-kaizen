"""Authoritative module ownership for the canonical test runner's explicit lanes."""

from __future__ import annotations

from pathlib import Path


TEST_ROOT = Path(__file__).resolve().parent

LANE_DESCRIPTIONS = {
    "core": "fast deterministic unit, static, schema, and contract tests (default)",
    "platform": "bounded Windows/POSIX filesystem, process, installer, and transport contracts",
    "slow": "subprocess-heavy, concurrency, timeout, benchmark, and integration tests (explicit only)",
    "live": "provider and live-service tests (explicit only; individual environment gates still apply)",
    "extension": "unreleased extension foundation tests (local explicit only)",
}

CORE_MODULES = {
    "test_apply_evidence",
    "test_artifact_cache",
    "test_aux",
    "test_claude_sdk_adapter",
    "test_claude_worker_protocol",
    "test_continuation",
    "test_contract_lint",
    "test_diff_snapshots",
    "test_fleet_reducers",
    "test_import_guard",
    "test_model_pins",
    "test_orchestration_modes",
    "test_output",
    "test_policy",
    "test_redaction",
    "test_schema",
    "test_security_hardening",
    "test_session_artifacts",
    "test_skill_ops",
    "test_skill_management",
    "test_skill_publication",
    "test_streaming",
    "test_test_runner",
    "test_validate_dco",
    "test_vectors",
    "test_workflow_static",
    "test_yt_transcript",
}

PLATFORM_MODULES = {
    "test_children",
    "test_claude_runtime_setup",
    "test_installer_claude_runtime",
    "test_installer_sh_static",
    "test_installer_static",
    "test_loopback_p0",
    "test_otel_rx",
    "test_p0_transport_integration",
    "test_proposal_executor",
    "test_workspace_path_authority",
}

SLOW_MODULES = {
    "test_agent_runs",
    "test_approval_broker",
    "test_backend_registry",
    "test_backends",
    "test_bench_smoke",
    "test_claude_runtime",
    "test_cli_wiring",
    "test_codex_adapter",
    "test_comfy_mcp",
    "test_comfy_runtime",
    "test_comfyui",
    "test_control_http",
    "test_db_backup",
    "test_db_retry",
    "test_dedup",
    "test_diff_approval_integration",
    "test_dispatch",
    "test_doc_examples",
    "test_fleet_authoritative",
    "test_fleet_coordination",
    "test_fleet_core",
    "test_fleet_metrics",
    "test_fleet_ops",
    "test_hooked_governor",
    "test_input_hardening",
    "test_integration_chains",
    "test_irl_ops",
    "test_judge",
    "test_lab_misc_ops",
    "test_learning_ops",
    "test_local_llm_adapter",
    "test_migration_ops",
    "test_mirror",
    "test_mode_profiles",
    "test_model_candidates",
    "test_model_monitor",
    "test_model_monitor_gui",
    "test_multimodel_vectors",
    "test_observed_claude_hooks",
    "test_op_coverage",
    "test_output_validate",
    "test_pii_scan",
    "test_plan_packet_ops",
    "test_policy_ops",
    "test_purge_test",
    "test_pytorch",
    "test_quality_ops",
    "test_reconcile",
    "test_records",
    "test_remote_dispatch",
    "test_report_ops",
    "test_reports",
    "test_retrieval",
    "test_search_escaping",
    "test_session_artifact_integration",
    "test_session_drive",
    "test_session_records",
    "test_skill_context",
    "test_supervisor",
    "test_tool_gateway",
    "test_transformers_backend",
    "test_vendor_supervisor_integration",
}

LIVE_MODULES = {
    "test_backends_live",
    "test_claude_live",
    "test_codex_live",
    "test_comfyui_live",
    "test_model_integration",
    "test_observed_claude_live",
    "test_session_artifacts_live",
}

LIVE_EXTRA_SELECTORS = {
    "test_session_drive.LiveDrivenSmokeTest",
    "test_streaming.LiveOllamaStreamingTest",
}

EXTENSION_MODULES = {
    "extension.test_build_vsix",
    "test_test_extension",
}


def root_test_modules() -> set[str]:
    """Return root test modules; nested extension tests remain explicitly owned."""
    return {path.stem for path in TEST_ROOT.glob("test_*.py")}


def lane_modules(name: str) -> tuple[str, ...]:
    """Resolve a lane to sorted unittest module selectors."""
    configured = {
        "core": CORE_MODULES,
        "platform": PLATFORM_MODULES,
        "slow": SLOW_MODULES,
        "live": LIVE_MODULES,
        "extension": EXTENSION_MODULES,
    }
    selected = configured[name]
    if name == "live":
        selected = selected | LIVE_EXTRA_SELECTORS
    return tuple(sorted(selected))


def validate_manifest() -> None:
    """Fail if root ownership is missing, overlapping, or references a missing module."""
    explicit = {
        "core": CORE_MODULES,
        "platform": PLATFORM_MODULES,
        "slow": SLOW_MODULES,
        "live": LIVE_MODULES,
        "extension": EXTENSION_MODULES & root_test_modules(),
    }
    names = tuple(explicit)
    for index, left in enumerate(names):
        for right in names[index + 1:]:
            overlap = explicit[left] & explicit[right]
            if overlap:
                raise RuntimeError(f"test lane overlap {left}/{right}: {sorted(overlap)}")
    configured_root = set().union(*explicit.values())
    missing = root_test_modules() - configured_root
    if missing:
        raise RuntimeError(f"test lane modules are unclassified: {sorted(missing)}")
    unknown = configured_root - root_test_modules()
    if unknown:
        raise RuntimeError(f"test lane modules do not exist: {sorted(unknown)}")
    nested_missing = [
        selector for selector in EXTENSION_MODULES
        if selector not in root_test_modules() and not (TEST_ROOT / Path(*selector.split(".")).with_suffix(".py")).is_file()
    ]
    if nested_missing:
        raise RuntimeError(f"nested test lane modules do not exist: {nested_missing}")
    missing_live_selectors = []
    for selector in LIVE_EXTRA_SELECTORS:
        module, class_name = selector.split(".", 1)
        source = TEST_ROOT / f"{module}.py"
        if not source.is_file() or f"class {class_name}(" not in source.read_text(encoding="utf-8"):
            missing_live_selectors.append(selector)
    if missing_live_selectors:
        raise RuntimeError(f"live test selectors do not exist: {missing_live_selectors}")


validate_manifest()
