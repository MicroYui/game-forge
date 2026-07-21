/** Generated from the committed GameForge API contracts. Do not edit by hand. */
export interface paths {
    "/api/v1/approvals": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Approvals */
        get: operations["approvals_api_v1_approvals_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/approvals/{approval_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Approval */
        get: operations["approval_api_v1_approvals__approval_id__get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/approvals/{approval_id}:approve": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Approve */
        post: operations["approve_api_v1_approvals__approval_id__approve_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/approvals/{approval_id}:reject": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Reject */
        post: operations["reject_api_v1_approvals__approval_id__reject_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/approvals/{approval_id}:request_changes": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Request Changes */
        post: operations["request_changes_api_v1_approvals__approval_id__request_changes_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/artifacts/{artifact_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Artifact */
        get: operations["artifact_api_v1_artifacts__artifact_id__get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/artifacts/{artifact_id}/lineage": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Lineage */
        get: operations["lineage_api_v1_artifacts__artifact_id__lineage_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/auth/login": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Login */
        post: operations["login_api_v1_auth_login_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/auth/logout": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Logout */
        post: operations["logout_api_v1_auth_logout_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/auth/me": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Me */
        get: operations["me_api_v1_auth_me_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/bench/report": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Bench Report */
        get: operations["bench_report_api_v1_bench_report_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/conflict-sets/{conflict_set_id}/conflicts": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Conflicts */
        get: operations["conflicts_api_v1_conflict_sets__conflict_set_id__conflicts_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/constraint-proposals": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Constraint Proposals */
        get: operations["constraint_proposals_api_v1_constraint_proposals_get"];
        put?: never;
        /** Draft Constraint */
        post: operations["draft_constraint_api_v1_constraint_proposals_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/constraint-proposals/{artifact_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Constraint Proposal */
        get: operations["constraint_proposal_api_v1_constraint_proposals__artifact_id__get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/constraint-proposals/{artifact_id}:publish": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Publish Constraint */
        post: operations["publish_constraint_api_v1_constraint_proposals__artifact_id__publish_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/constraint-proposals/{artifact_id}:revise": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Revise Constraint */
        post: operations["revise_constraint_api_v1_constraint_proposals__artifact_id__revise_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/constraint-proposals/{artifact_id}:submit-for-approval": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Submit Constraint */
        post: operations["submit_constraint_api_v1_constraint_proposals__artifact_id__submit_for_approval_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/constraint-proposals/{artifact_id}:validate": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Validate Constraint */
        post: operations["validate_constraint_api_v1_constraint_proposals__artifact_id__validate_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/constraint-proposals:propose": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Propose Constraint */
        post: operations["propose_constraint_api_v1_constraint_proposals_propose_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/constraints": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Constraints */
        get: operations["constraints_api_v1_constraints_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/constraints/{artifact_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Constraint */
        get: operations["constraint_api_v1_constraints__artifact_id__get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/cost/{run_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Get Run Cost */
        get: operations["get_run_cost_api_v1_cost__run_id__get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/diff": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Diff */
        get: operations["diff_api_v1_diff_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/execution-options:resolve": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Resolve Execution Option */
        post: operations["resolve_execution_option_api_v1_execution_options_resolve_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/execution-profiles": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Execution Profiles */
        get: operations["execution_profiles_api_v1_execution_profiles_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/execution-profiles/{profile_id}/versions/{version}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Execution Profile */
        get: operations["execution_profile_api_v1_execution_profiles__profile_id__versions__version__get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/execution-profiles/{profile_id}/versions/{version}/constraint-validation-binding": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Constraint Validation Compiler Binding */
        get: operations["constraint_validation_compiler_binding_api_v1_execution_profiles__profile_id__versions__version__constraint_validation_binding_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/execution-profiles/{profile_id}/versions/{version}/task-suite-derivation-binding": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Task Suite Derivation Binding */
        get: operations["task_suite_derivation_binding_api_v1_execution_profiles__profile_id__versions__version__task_suite_derivation_binding_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/findings": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Findings */
        get: operations["findings_api_v1_findings_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/findings/{finding_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Latest Finding */
        get: operations["latest_finding_api_v1_findings__finding_id__get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/findings/{finding_id}/revisions/{revision}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Exact Finding */
        get: operations["exact_finding_api_v1_findings__finding_id__revisions__revision__get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/generation:propose": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Propose Generation */
        post: operations["propose_generation_api_v1_generation_propose_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/logs/query": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Query Logs */
        get: operations["query_logs_api_v1_logs_query_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/metrics/descriptors": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Get Metric Descriptors */
        get: operations["get_metric_descriptors_api_v1_metrics_descriptors_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/metrics/query": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Query Metrics */
        get: operations["query_metrics_api_v1_metrics_query_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/patches": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Patches */
        get: operations["patches_api_v1_patches_get"];
        put?: never;
        /** Draft Patch */
        post: operations["draft_patch_api_v1_patches_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/patches/{artifact_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Patch */
        get: operations["patch_api_v1_patches__artifact_id__get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/patches/{artifact_id}:apply": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Apply Patch */
        post: operations["apply_patch_api_v1_patches__artifact_id__apply_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/patches/{artifact_id}:rebase": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Rebase Patch */
        post: operations["rebase_patch_api_v1_patches__artifact_id__rebase_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/patches/{artifact_id}:repair": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Repair Patch */
        post: operations["repair_patch_api_v1_patches__artifact_id__repair_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/patches/{artifact_id}:resolve-conflicts": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Resolve Patch Conflicts */
        post: operations["resolve_patch_conflicts_api_v1_patches__artifact_id__resolve_conflicts_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/patches/{artifact_id}:submit-for-approval": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Submit Patch */
        post: operations["submit_patch_api_v1_patches__artifact_id__submit_for_approval_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/patches/{artifact_id}:validate": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Validate Patch */
        post: operations["validate_patch_api_v1_patches__artifact_id__validate_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/playtest/{run_id}/result": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Playtest Result */
        get: operations["playtest_result_api_v1_playtest__run_id__result_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/playtest:run": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Run Playtest */
        post: operations["run_playtest_api_v1_playtest_run_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/refs/{ref_name}/history": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Ref History */
        get: operations["ref_history_api_v1_refs__ref_name__history_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/refs/{ref_name}/rollback-requests": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Draft Rollback */
        post: operations["draft_rollback_api_v1_refs__ref_name__rollback_requests_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/reviews": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Reviews */
        get: operations["reviews_api_v1_reviews_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/reviews/{artifact_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Review */
        get: operations["review_api_v1_reviews__artifact_id__get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/reviews/{artifact_id}/producer-binding": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Review Producer Binding */
        get: operations["review_producer_binding_api_v1_reviews__artifact_id__producer_binding_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/rollback-requests": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Rollback Requests */
        get: operations["rollback_requests_api_v1_rollback_requests_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/rollback-requests/{artifact_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Rollback Request */
        get: operations["rollback_request_api_v1_rollback_requests__artifact_id__get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/rollback-requests/{artifact_id}:apply": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Apply Rollback */
        post: operations["apply_rollback_api_v1_rollback_requests__artifact_id__apply_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/rollback-requests/{artifact_id}:submit-for-approval": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Submit Rollback */
        post: operations["submit_rollback_api_v1_rollback_requests__artifact_id__submit_for_approval_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/rollback-requests/{artifact_id}:validate": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Validate Rollback */
        post: operations["validate_rollback_api_v1_rollback_requests__artifact_id__validate_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/runs": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Runs */
        get: operations["runs_api_v1_runs_get"];
        put?: never;
        /** Submit Run */
        post: operations["submit_run_api_v1_runs_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/runs/{run_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Run */
        get: operations["run_api_v1_runs__run_id__get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/runs/{run_id}/commands": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Run Commands */
        get: operations["run_commands_api_v1_runs__run_id__commands_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/runs/{run_id}/events": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Stream Run Events */
        get: operations["stream_run_events_api_v1_runs__run_id__events_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/runs/{run_id}/finding-links": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Run Finding Links */
        get: operations["run_finding_links_api_v1_runs__run_id__finding_links_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/runs/{run_id}/findings": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Run Findings */
        get: operations["run_findings_api_v1_runs__run_id__findings_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/runs/{run_id}/traces": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** List Run Traces */
        get: operations["list_run_traces_api_v1_runs__run_id__traces_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/runs/{run_id}:cancel": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Cancel Run */
        post: operations["cancel_run_api_v1_runs__run_id__cancel_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/schema-registry/{version}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Schema Registry */
        get: operations["schema_registry_api_v1_schema_registry__version__get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/specs": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Specs */
        get: operations["specs_api_v1_specs_get"];
        put?: never;
        /** Upload Spec */
        post: operations["upload_spec_api_v1_specs_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/specs/{artifact_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Spec */
        get: operations["spec_api_v1_specs__artifact_id__get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/specs/{artifact_id}/graph": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Graph */
        get: operations["graph_api_v1_specs__artifact_id__graph_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/task-suites": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Task Suites */
        get: operations["task_suites_api_v1_task_suites_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/task-suites/{artifact_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Task Suite */
        get: operations["task_suite_api_v1_task_suites__artifact_id__get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/task-suites:derive": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Derive Task Suite */
        post: operations["derive_task_suite_api_v1_task_suites_derive_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/traces/{trace_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Get Trace */
        get: operations["get_trace_api_v1_traces__trace_id__get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/traces/{trace_id}/spans": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Get Trace Spans */
        get: operations["get_trace_spans_api_v1_traces__trace_id__spans_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/workflow-subjects/{artifact_id}/approval-binding": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Subject Approval Binding */
        get: operations["subject_approval_binding_api_v1_workflow_subjects__artifact_id__approval_binding_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
}
export type webhooks = Record<string, never>;
export interface components {
    schemas: {
        /** AgentCostSection */
        AgentCostSection: {
            /** Evidence Ref */
            evidence_ref: string;
            /** Workloads */
            workloads: components["schemas"]["AgentCostWorkload"][];
        };
        /** AgentCostWorkload */
        AgentCostWorkload: {
            /** Evaluated N */
            evaluated_n: number;
            /** Evidence Ref */
            evidence_ref: string;
            /** Known Transport Attempts */
            known_transport_attempts: number;
            /** Known Transport Retries */
            known_transport_retries: number;
            /** Logical Requests */
            logical_requests: number;
            model_snapshot: components["schemas"]["ModelSnapshot"];
            /**
             * Monetary Status
             * @default unavailable
             * @constant
             */
            monetary_status: "unavailable";
            /** Planned N */
            planned_n: number;
            /** Price Book Ref */
            price_book_ref?: null;
            /** Recorded Requests */
            recorded_requests: number;
            request_latency_ms: components["schemas"]["DistributionMetric"];
            /** Session Cache Reuses */
            session_cache_reuses: number;
            tokens: components["schemas"]["TokenTotals"];
            tokens_per_sample: components["schemas"]["DistributionMetric"];
            /** Unknown Transport Attempt Records */
            unknown_transport_attempt_records: number;
            /** Workload Id */
            workload_id: string;
        };
        /** ApprovalDecision */
        ApprovalDecision: {
            actor: components["schemas"]["AuditActor"];
            /** Comment */
            comment?: string | null;
            /**
             * Decision
             * @enum {string}
             */
            decision: "approve" | "reject" | "request_changes";
            /** Decision Id */
            decision_id: string;
            /** Expected Workflow Revision */
            expected_workflow_revision: number;
            /** Occurred At */
            occurred_at: string;
            /** Reason Code */
            reason_code: string;
            /** Requirement Ids */
            requirement_ids: string[];
        };
        /** ApprovalDecisionEligibilityV1 */
        ApprovalDecisionEligibilityV1: {
            /**
             * Decision
             * @enum {string}
             */
            decision: "approve" | "reject" | "request_changes";
            /** Eligible */
            eligible: boolean;
            /** Reason Codes */
            reason_codes: ("workflow_not_pending" | "actor_not_active_human" | "maker_checker_conflict" | "actor_not_assigned" | "route_role_missing" | "permission_denied" | "requirement_already_satisfied" | "actor_already_decided_requirement" | "distinct_requirement_conflict")[];
        };
        /** ApprovalDecisionRequestV1 */
        ApprovalDecisionRequestV1: {
            /** Comment */
            comment?: string | null;
            /**
             * Decision
             * @enum {string}
             */
            decision: "approve" | "reject" | "request_changes";
            /** Expected Workflow Revision */
            expected_workflow_revision: number;
            /** Reason Code */
            reason_code: string;
            /**
             * Request Schema Version
             * @default approval-decision-request@1
             * @constant
             */
            request_schema_version: "approval-decision-request@1";
            /** Requirement Ids */
            requirement_ids: string[];
        };
        /** ApprovalItem */
        ApprovalItem: {
            /** Active Validation Run Id */
            active_validation_run_id?: string | null;
            /** Applied At */
            applied_at?: string | null;
            /** Approval Id */
            approval_id: string;
            approval_policy: components["schemas"]["ApprovalPolicyRefV1"];
            /**
             * Approval Schema Version
             * @default approval@1
             * @constant
             */
            approval_schema_version: "approval@1";
            auto_apply_proof?: components["schemas"]["AutoApplyProofBindingV1"] | null;
            /** Created At */
            created_at: string;
            /** Decided At */
            decided_at?: string | null;
            /** Decisions */
            decisions: components["schemas"]["ApprovalDecision"][];
            domain_registry_ref: components["schemas"]["DomainRegistryRefV1"];
            domain_scope: components["schemas"]["DomainScope"];
            /** Evidence Set Artifact Id */
            evidence_set_artifact_id?: string | null;
            /** Last Validation Failure Artifact Id */
            last_validation_failure_artifact_id?: string | null;
            proposer: components["schemas"]["AuditActor"];
            /** Regression Evidence Artifact Ids */
            regression_evidence_artifact_ids: string[];
            /** Requirements */
            requirements: components["schemas"]["ApprovalRequirement"][];
            /** Role Policy Digest */
            role_policy_digest: string;
            /** Role Policy Version */
            role_policy_version: string;
            route_policy: components["schemas"]["DomainRoutePolicyRefV1"];
            /**
             * Status
             * @enum {string}
             */
            status: "draft" | "validating" | "validation_failed" | "validated" | "pending_approval" | "auto_apply_eligible" | "approved" | "changes_requested" | "rejected" | "applied" | "rolled_back" | "superseded";
            /** Subject Artifact Id */
            subject_artifact_id: string;
            /** Subject Digest */
            subject_digest: string;
            /**
             * Subject Kind
             * @enum {string}
             */
            subject_kind: "patch" | "constraint_proposal" | "rollback_request";
            /** Subject Revision */
            subject_revision: number;
            /** Subject Series Id */
            subject_series_id: string;
            /** Submitted At */
            submitted_at?: string | null;
            /** Supersedes Approval Id */
            supersedes_approval_id?: string | null;
            /** Target Binding */
            target_binding?: (components["schemas"]["PatchTargetBindingV1"] | components["schemas"]["ConstraintTargetBindingV1"] | components["schemas"]["RollbackTargetBindingV1"]) | null;
            /** Workflow Revision */
            workflow_revision: number;
        };
        /** ApprovalPolicyRefV1 */
        ApprovalPolicyRefV1: {
            /** Policy Digest */
            policy_digest: string;
            /** Policy Version */
            policy_version: string;
        };
        /** ApprovalRequirement */
        ApprovalRequirement: {
            /** Assignee Principal Ids */
            assignee_principal_ids: string[];
            /** Distinct From Requirement Ids */
            distinct_from_requirement_ids: string[];
            domain_scope: components["schemas"]["DomainScope"];
            /** Min Approvals */
            min_approvals: number;
            required_permission: components["schemas"]["Permission"];
            /** Requirement Id */
            requirement_id: string;
            /**
             * Route Role
             * @enum {string}
             */
            route_role: "content_designer" | "numeric_designer" | "qa" | "tooling" | "constraint_admin" | "gacha_compliance_reviewer" | "identity_admin";
        };
        /** ApprovalRequirementProgressV1 */
        ApprovalRequirementProgressV1: {
            /** Decision Eligibility */
            decision_eligibility: components["schemas"]["ApprovalDecisionEligibilityV1"][];
            domain_scope: components["schemas"]["DomainScope"];
            /** Eligible For Current Actor */
            eligible_for_current_actor: boolean;
            /** Min Approvals */
            min_approvals: number;
            /** Requirement Id */
            requirement_id: string;
            /**
             * Route Role
             * @enum {string}
             */
            route_role: "content_designer" | "numeric_designer" | "qa" | "tooling" | "constraint_admin" | "gacha_compliance_reviewer" | "identity_admin";
            /** Satisfied */
            satisfied: boolean;
            /** Unmet Distinct From Requirement Ids */
            unmet_distinct_from_requirement_ids: string[];
            /** Valid Approval Count */
            valid_approval_count: number;
        };
        /** ApprovalViewV1 */
        ApprovalViewV1: {
            approval: components["schemas"]["ApprovalItem"];
            /** Current Actor Allowed Requirement Ids */
            current_actor_allowed_requirement_ids: string[];
            /** Requirement Progress */
            requirement_progress: components["schemas"]["ApprovalRequirementProgressV1"][];
            /**
             * View Schema Version
             * @default approval-view@1
             * @constant
             */
            view_schema_version: "approval-view@1";
        };
        /**
         * ArtifactPayloadViewV1
         * @description Verified, schema-bound payload for an already authorized Artifact.
         */
        ArtifactPayloadViewV1: {
            artifact: components["schemas"]["ArtifactSummaryV1"];
            payload: components["schemas"]["JsonValue"];
            /**
             * Resource Revision
             * @default 1
             * @constant
             */
            resource_revision: 1;
            /**
             * View Schema Version
             * @default artifact-payload-view@1
             * @constant
             */
            view_schema_version: "artifact-payload-view@1";
        };
        /**
         * ArtifactSummaryV1
         * @description Safe immutable Artifact projection without object-store coordinates or free-form meta.
         */
        ArtifactSummaryV1: {
            /** Artifact Id */
            artifact_id: string;
            /** Created At */
            created_at?: string | null;
            /** Domain Scope */
            domain_scope: components["schemas"]["DomainScope"] | "all" | null;
            /**
             * Kind
             * @enum {string}
             */
            kind: "source_raw" | "source_rendered" | "ir_snapshot" | "constraint_snapshot" | "constraint_proposal" | "config_export" | "scenario_spec" | "task_suite" | "regression_suite" | "golden_suite" | "bench_dataset" | "benchmark_spec" | "review_report" | "checker_run" | "simulation_run" | "playtest_trace" | "patch" | "validation_evidence" | "regression_evidence" | "rollback_request" | "run_result" | "run_failure" | "cassette_bundle" | "migration_report" | "bench_report" | "operational_evidence";
            /**
             * Lineage Schema Version
             * @enum {string}
             */
            lineage_schema_version: "lineage@1" | "lineage@2";
            /** Parent Artifact Ids */
            parent_artifact_ids: string[];
            /** Payload Hash */
            payload_hash?: string | null;
            /** Payload Schema Id */
            payload_schema_id?: string | null;
            /**
             * Summary Schema Version
             * @default artifact-summary@1
             * @constant
             */
            summary_schema_version: "artifact-summary@1";
            version_tuple: components["schemas"]["VersionTuple"];
        };
        /** AuditActor */
        AuditActor: {
            /** Principal Id */
            principal_id: string;
            /**
             * Principal Kind
             * @enum {string}
             */
            principal_kind: "human" | "service" | "system";
        };
        /** AutoApplyPolicyRefV1 */
        AutoApplyPolicyRefV1: {
            /** Policy Digest */
            policy_digest: string;
            /** Policy Id */
            policy_id: string;
            /** Policy Version */
            policy_version: string;
            registry: components["schemas"]["AutoApplyPolicyRegistryRefV1"];
        };
        /** AutoApplyPolicyRegistryRefV1 */
        AutoApplyPolicyRegistryRefV1: {
            /** Registry Digest */
            registry_digest: string;
            /** Registry Version */
            registry_version: string;
        };
        /** AutoApplyProofBindingV1 */
        AutoApplyProofBindingV1: {
            expected_ref?: components["schemas"]["RefValue"] | null;
            policy: components["schemas"]["AutoApplyPolicyRefV1"];
            /** Proof Artifact Id */
            proof_artifact_id: string;
            /** Subject Digest */
            subject_digest: string;
            /** Target Digest */
            target_digest: string;
            /** Validation Evidence Artifact Id */
            validation_evidence_artifact_id: string;
        };
        /** BenchMeta */
        BenchMeta: {
            /** Corpus Size */
            corpus_size: number;
            /** Generated At */
            generated_at?: string | null;
            /** Report Builder Version */
            report_builder_version: string;
            /** Seed */
            seed?: number | null;
        };
        /** BenchReport */
        BenchReport: {
            /** Agent */
            agent: components["schemas"]["BinaryMetric"][];
            cost_latency: components["schemas"]["CostLatencySection"];
            /** Evidence */
            evidence: components["schemas"]["EvidenceArtifactRef"][];
            external: components["schemas"]["ExternalSection"];
            /** False Positives */
            false_positives: components["schemas"]["BinaryMetric"][];
            hed: components["schemas"]["HedSection"];
            meta: components["schemas"]["BenchMeta"];
            narrative: components["schemas"]["NarrativeSection"];
            /** Power */
            power: components["schemas"]["PowerMetric"][];
            qa: components["schemas"]["QaSection"];
            /**
             * Schema Version
             * @default bench-report@2
             * @constant
             */
            schema_version: "bench-report@2";
            /** Seeded */
            seeded: components["schemas"]["BinaryMetric"][];
            /** Versions */
            versions: components["schemas"]["VersionRef"][];
        };
        /** BenchRunPayloadV1 */
        BenchRunPayloadV1: {
            /** Benchmark Spec Artifact Id */
            benchmark_spec_artifact_id: string;
            /** Case Result Artifact Ids */
            case_result_artifact_ids: string[];
            /** Dataset Artifact Id */
            dataset_artifact_id: string;
            evaluator_profile: components["schemas"]["ProfileRefV1"];
            /**
             * Execution Scope
             * @enum {string}
             */
            execution_scope: "execute_cases" | "aggregate_results";
            /** Partition Ids */
            partition_ids: string[];
            /** Repetition Count */
            repetition_count: number;
            /**
             * @description discriminator enum property added by openapi-typescript
             * @enum {string}
             */
            schema_version: "bench-run@1";
        };
        /** BinaryMetric */
        BinaryMetric: {
            /** Bucket */
            bucket: string;
            /** Ci High */
            ci_high?: number | null;
            /** Ci Low */
            ci_low?: number | null;
            /** Ci Method */
            ci_method?: string | null;
            defect_class?: components["schemas"]["DefectClass"] | null;
            /** Evaluated N */
            evaluated_n: number;
            /** Evidence Ref */
            evidence_ref?: string | null;
            /** K */
            k: number;
            /** Name */
            name: string;
            /** Planned N */
            planned_n: number;
            /** Protocol Id */
            protocol_id?: string | null;
            /** Rate */
            rate?: number | null;
            /**
             * Status
             * @enum {string}
             */
            status: "pending" | "measured" | "underpowered" | "inconclusive" | "failed";
        };
        /** BudgetSetSnapshotV1 */
        BudgetSetSnapshotV1: {
            /** Budget Set Snapshot Id */
            budget_set_snapshot_id: string;
            /**
             * Captured At
             * Format: date-time
             */
            captured_at: string;
            /** Run Id */
            run_id: string;
            /** Selection Policy Version */
            selection_policy_version: string;
            /**
             * Set Schema Version
             * @default budget-set-snapshot@1
             * @constant
             */
            set_schema_version: "budget-set-snapshot@1";
            /** Snapshots */
            snapshots: components["schemas"]["BudgetSnapshotV1"][];
        };
        /** BudgetSnapshotV1 */
        BudgetSnapshotV1: {
            /** Budget Id */
            budget_id: string;
            /** Budget Revision At Freeze */
            budget_revision_at_freeze: number;
            /**
             * Captured At
             * Format: date-time
             */
            captured_at: string;
            /** Consumed */
            consumed: components["schemas"]["CostAmountV1"][];
            /** Limits */
            limits: components["schemas"]["CostAmountV1"][];
            /** Policy Version */
            policy_version: string;
            /** Reserved */
            reserved: components["schemas"]["CostAmountV1"][];
            /** Scope Id */
            scope_id: string;
            /**
             * Scope Kind
             * @enum {string}
             */
            scope_kind: "run" | "principal" | "system";
            /** Snapshot Id */
            snapshot_id: string;
            /**
             * Snapshot Schema Version
             * @default budget-snapshot@1
             * @constant
             */
            snapshot_schema_version: "budget-snapshot@1";
        };
        /** CacheHitObservationV1 */
        CacheHitObservationV1: {
            /** Hit */
            hit?: boolean | null;
            /**
             * Observation Schema Version
             * @default cache-hit-observation@1
             * @constant
             */
            observation_schema_version: "cache-hit-observation@1";
            /**
             * Status
             * @enum {string}
             */
            status: "reported" | "unavailable";
        };
        /** CancelRunPayloadV1 */
        CancelRunPayloadV1: {
            /** Comment */
            comment?: string | null;
            /** Reason Code */
            reason_code: string;
            /**
             * @description discriminator enum property added by openapi-typescript
             * @enum {string}
             */
            schema_version: "run-cancel@1";
        };
        /** CheckerRunPayloadV1 */
        CheckerRunPayloadV1: {
            /** Checker Ids */
            checker_ids: string[];
            checker_profile: components["schemas"]["ProfileRefV1"];
            /** Constraint Snapshot Artifact Id */
            constraint_snapshot_artifact_id?: string | null;
            /** Defect Classes */
            defect_classes: string[];
            /**
             * @description discriminator enum property added by openapi-typescript
             * @enum {string}
             */
            schema_version: "checker-run@1";
            selection: components["schemas"]["GraphSelectionV1"];
            /** Snapshot Artifact Id */
            snapshot_artifact_id: string;
        };
        /** CompletionOracleRefV1 */
        CompletionOracleRefV1: {
            /** Oracle Id */
            oracle_id: string;
            params: components["schemas"]["JsonValue"];
            /** Params Schema Id */
            params_schema_id: string;
            /** Version */
            version: number;
        };
        /** CompletionOracleRegistryRefV1 */
        CompletionOracleRegistryRefV1: {
            /** Digest */
            digest: string;
            /** Registry Version */
            registry_version: number;
        };
        /** ConflictResolution */
        ConflictResolution: components["schemas"]["_KeepCurrentResolution"] | components["schemas"]["_TakeProposedResolution"] | components["schemas"]["_CustomResolution"];
        /** Constraint */
        Constraint: {
            /** Assert */
            assert: string;
            /**
             * Dsl Grammar Version
             * @default dsl@1
             */
            dsl_grammar_version: string;
            forall?: components["schemas"]["Selector"] | null;
            /** Id */
            id: string;
            /**
             * Kind
             * @enum {string}
             */
            kind: "structural" | "numeric" | "narrative";
            /** Note */
            note?: string | null;
            /**
             * Oracle
             * @enum {string}
             */
            oracle: "deterministic" | "llm-assisted" | "mixed";
            /** Predicates */
            predicates?: components["schemas"]["Predicate"][];
            scope?: components["schemas"]["Selector"] | null;
            /**
             * Severity
             * @enum {string}
             */
            severity: "critical" | "major" | "minor";
        };
        /** ConstraintProposalReadViewV1 */
        ConstraintProposalReadViewV1: {
            /** Approval Status */
            approval_status: string;
            artifact: components["schemas"]["ArtifactSummaryV1"];
            proposal: components["schemas"]["ConstraintProposalV1"];
            /**
             * View Schema Version
             * @default constraint-proposal-read-view@1
             * @constant
             */
            view_schema_version: "constraint-proposal-read-view@1";
            /** Workflow Revision */
            workflow_revision: number;
        };
        /** ConstraintProposalV1 */
        ConstraintProposalV1: {
            /** Base Constraint Snapshot Id */
            base_constraint_snapshot_id?: string | null;
            /** Constraints */
            constraints: components["schemas"]["Constraint"][];
            domain_scope: components["schemas"]["DomainScope"];
            /** Dsl Grammar Version */
            dsl_grammar_version: string;
            /**
             * Produced By
             * @enum {string}
             */
            produced_by: "agent" | "human";
            /** Producer Run Id */
            producer_run_id?: string | null;
            /**
             * Proposal Schema Version
             * @default constraint-proposal@1
             * @constant
             */
            proposal_schema_version: "constraint-proposal@1";
            /** Rationale */
            rationale: string;
            /** Revision */
            revision: number;
            /** Source Bindings */
            source_bindings: components["schemas"]["ConstraintSourceBinding"][];
            /** Supersedes Artifact Id */
            supersedes_artifact_id?: string | null;
        };
        /**
         * ConstraintProposeRequestV1
         * @description ``POST /constraint-proposals:propose`` — fixes ``constraint_proposal.propose@1``.
         */
        ConstraintProposeRequestV1: {
            /** Authoring Goal Text */
            authoring_goal_text: string;
            /** Base Constraint Snapshot Artifact Id */
            base_constraint_snapshot_artifact_id?: string | null;
            /** Cassette Artifact Id */
            cassette_artifact_id?: string | null;
            domain_scope: components["schemas"]["DomainScope"];
            /** Dsl Grammar Version */
            dsl_grammar_version: string;
            execution_version_plan?: components["schemas"]["ExecutionVersionPlanV1"] | null;
            extraction_policy: components["schemas"]["ProfileRefV1"];
            /**
             * Llm Execution Mode
             * @default record
             * @enum {string}
             */
            llm_execution_mode: "live" | "record" | "replay";
            /**
             * Request Schema Version
             * @default constraint-propose-request@1
             * @constant
             */
            request_schema_version: "constraint-propose-request@1";
            /** Source Artifact Ids */
            source_artifact_ids: string[];
        };
        /** ConstraintSnapshotViewV1 */
        ConstraintSnapshotViewV1: {
            artifact: components["schemas"]["ArtifactSummaryV1"];
            /** Constraints */
            constraints: components["schemas"]["JsonValue"][];
            /** Dsl Grammar Version */
            dsl_grammar_version: string;
            /**
             * View Schema Version
             * @default constraint-snapshot-view@1
             * @constant
             */
            view_schema_version: "constraint-snapshot-view@1";
        };
        /** ConstraintSourceBinding */
        ConstraintSourceBinding: {
            /** Provenance Hash */
            provenance_hash: string;
            /** Source Artifact Id */
            source_artifact_id: string;
            source_ref?: components["schemas"]["SourceRef"] | null;
        };
        /** ConstraintTargetBindingV1 */
        ConstraintTargetBindingV1: {
            /**
             * Binding Schema Version
             * @default approval-target-binding@1
             * @constant
             */
            binding_schema_version: "approval-target-binding@1";
            expected_ref?: components["schemas"]["RefValue"] | null;
            /** Ref Name */
            ref_name: string;
            /**
             * @description discriminator enum property added by openapi-typescript
             * @enum {string}
             */
            subject_kind: "constraint_proposal";
            /** Target Artifact Id */
            target_artifact_id: string;
            /**
             * Target Artifact Kind
             * @default constraint_snapshot
             * @constant
             */
            target_artifact_kind: "constraint_snapshot";
            /** Target Digest */
            target_digest: string;
            /** Target Snapshot Id */
            target_snapshot_id: string;
        };
        /** ConstraintValidationAdmissionRequestV1 */
        ConstraintValidationAdmissionRequestV1: {
            /** Approval Id */
            approval_id: string;
            /** Base Constraint Snapshot Artifact Id */
            base_constraint_snapshot_artifact_id?: string | null;
            compiler_profile: components["schemas"]["ProfileRefV1"];
            /** Differential Engines */
            differential_engines: components["schemas"]["SolverEngineRefV1"][];
            /** Dsl Grammar Version */
            dsl_grammar_version: string;
            /** Expected Subject Head Revision */
            expected_subject_head_revision: number;
            /** Expected Workflow Revision */
            expected_workflow_revision: number;
            /** Golden Suite Artifact Id */
            golden_suite_artifact_id?: string | null;
            /** Regression Suite Artifact Ids */
            regression_suite_artifact_ids: string[];
            /**
             * Request Schema Version
             * @default constraint-validation-admission-request@1
             * @constant
             */
            request_schema_version: "constraint-validation-admission-request@1";
            /** Seed */
            seed?: number | null;
            /** Subject Digest */
            subject_digest: string;
            target: components["schemas"]["RefReadBindingV1"];
            validation_policy: components["schemas"]["ProfileRefV1"];
        };
        /**
         * ConstraintValidationCompilerBindingViewV1
         * @description Browser-safe exact compiler authority for constraint validation.
         */
        ConstraintValidationCompilerBindingViewV1: {
            /**
             * Binding Schema Version
             * @default constraint-validation-compiler-binding@1
             * @constant
             */
            binding_schema_version: "constraint-validation-compiler-binding@1";
            compiler_profile: components["schemas"]["ProfileRefV1"];
            /** Differential Engines */
            differential_engines: components["schemas"]["SolverEngineRefV1"][];
            /** Profile Payload Hash */
            profile_payload_hash: string;
            run_kind: components["schemas"]["RunKindRef"];
        };
        /** CostAmountV1 */
        CostAmountV1: {
            /**
             * Amount Schema Version
             * @default cost-amount@1
             * @constant
             */
            amount_schema_version: "cost-amount@1";
            /** Currency */
            currency?: string | null;
            /**
             * Dimension
             * @enum {string}
             */
            dimension: "input_token" | "output_token" | "cache_read_token" | "cache_write_token" | "request" | "agent_step" | "wall_time_ns" | "concurrent_run" | "monetary";
            /**
             * Unit
             * @enum {string}
             */
            unit: "token" | "request" | "step" | "ns" | "count" | "currency";
            /** Value */
            value: string;
        };
        /** CostLatencySection */
        CostLatencySection: {
            agent: components["schemas"]["AgentCostSection"];
            deterministic: components["schemas"]["DeterministicRuntimeSection"];
        };
        /**
         * CostSettlementGroupCountV1
         * @description Safe aggregate count for one reservation scope/status pair.
         */
        CostSettlementGroupCountV1: {
            /**
             * Count Schema Version
             * @default cost-settlement-group-count@1
             * @constant
             */
            count_schema_version: "cost-settlement-group-count@1";
            /** Group Count */
            group_count: number;
            /**
             * Scope
             * @enum {string}
             */
            scope: "run_budget_hold" | "attempt_call" | "agent_step";
            /**
             * Status
             * @enum {string}
             */
            status: "reserved" | "reconciled" | "held_unknown" | "conservatively_settled" | "late_reconciled" | "released";
        };
        /**
         * CostSettlementSummaryV1
         * @description Run-level settlement state without reservation or routing identities.
         */
        CostSettlementSummaryV1: {
            /** Group Counts */
            group_counts: components["schemas"]["CostSettlementGroupCountV1"][];
            /** Held Unknown Group Count */
            held_unknown_group_count: number;
            /** Late Adjustment Usage Count */
            late_adjustment_usage_count: number;
            /**
             * Summary Schema Version
             * @default cost-settlement-summary@1
             * @constant
             */
            summary_schema_version: "cost-settlement-summary@1";
            /** Total Group Count */
            total_group_count: number;
            /** Usage Entry Count */
            usage_entry_count: number;
            /**
             * Usage Evidence Status
             * @enum {string}
             */
            usage_evidence_status: "recorded" | "not_recorded";
        };
        /**
         * CostUsageViewV1
         * @description Public cost observation without request, routing, reservation, or fencing internals.
         */
        CostUsageViewV1: {
            /** Adjustment Of Usage Id */
            adjustment_of_usage_id?: string | null;
            /** Attempt No */
            attempt_no: number;
            /**
             * Execution Source
             * @enum {string}
             */
            execution_source: "online" | "full_response_cache" | "cassette_replay";
            latency: components["schemas"]["LatencyObservationV1"];
            monetary: components["schemas"]["MonetaryObservationV1"];
            provider_prefix_cache: components["schemas"]["CacheHitObservationV1"];
            /**
             * Recorded At
             * Format: date-time
             */
            recorded_at: string;
            /** Retry Index */
            retry_index: number;
            /**
             * Scope
             * @enum {string}
             */
            scope: "attempt_call" | "agent_step";
            token_usage: components["schemas"]["TokenUsageObservationV1"];
            /** Transport Attempt */
            transport_attempt?: number | null;
            /** Usage Id */
            usage_id: string;
            /**
             * Usage Schema Version
             * @default cost-usage-view@1
             * @constant
             */
            usage_schema_version: "cost-usage-view@1";
            /** Wall Time Ns */
            wall_time_ns: number;
        };
        /**
         * DefectClass
         * @enum {string}
         */
        DefectClass: "dangling_reference" | "missing_drop_source" | "unreachable_target" | "cyclic_dependency" | "dead_quest" | "unsatisfiable_completion" | "reward_out_of_range" | "prob_sum_ne_1" | "non_monotonic_curve" | "gacha_expectation_violation" | "economy_collapse" | "character_violation" | "spoiler" | "faction_violation" | "uniqueness_violation";
        /** DefectClassCount */
        DefectClassCount: {
            /** Count */
            count: number;
            /** Defect Class */
            defect_class: string;
            /**
             * Severity
             * @enum {string}
             */
            severity: "critical" | "major" | "minor";
        };
        /** DeterministicRuntimeSection */
        DeterministicRuntimeSection: {
            /** Environment Sha256 */
            environment_sha256: string;
            /** Evidence Ref */
            evidence_ref: string;
            per_sample_ms: components["schemas"]["DistributionMetric"];
            /** Setup Ms */
            setup_ms: number;
            /** Workload Id */
            workload_id: string;
        };
        /** DistributionMetric */
        DistributionMetric: {
            /** Bucket */
            bucket: string;
            /** Ci High */
            ci_high?: number | null;
            /** Ci Low */
            ci_low?: number | null;
            /** Ci Method */
            ci_method?: string | null;
            /** Evaluated N */
            evaluated_n: number;
            /** Evidence Ref */
            evidence_ref?: string | null;
            /** Mean */
            mean?: number | null;
            /** Median */
            median?: number | null;
            /** Name */
            name: string;
            /** P95 */
            p95?: number | null;
            /** Planned N */
            planned_n: number;
            /** Primary Estimate */
            primary_estimate?: number | null;
            /** Protocol Id */
            protocol_id?: string | null;
            /**
             * Status
             * @enum {string}
             */
            status: "pending" | "measured" | "underpowered" | "inconclusive" | "failed";
            /** Unit */
            unit: string;
        };
        /** DomainRegistryRefV1 */
        DomainRegistryRefV1: {
            /** Registry Digest */
            registry_digest: string;
            /** Registry Version */
            registry_version: string;
        };
        /** DomainRoutePolicyRefV1 */
        DomainRoutePolicyRefV1: {
            domain_registry_ref: components["schemas"]["DomainRegistryRefV1"];
            /** Route Digest */
            route_digest: string;
            /** Route Version */
            route_version: string;
        };
        /** DomainScope */
        DomainScope: {
            /** Domain Ids */
            domain_ids: string[];
        };
        /**
         * EdgeType
         * @enum {string}
         */
        EdgeType: "HAS_STEP" | "PRECEDES" | "REQUIRES" | "GATED_BY" | "UNLOCKS" | "STARTS_AT" | "TALKS_TO" | "TRIGGERED_BY" | "LOCATED_IN" | "CONTAINS" | "SPAWNS" | "PATH_TO" | "DROPS_FROM" | "GRANTS" | "CONSUMES" | "REWARDS" | "SELLS" | "USES_SKILL" | "APPLIES_EFFECT" | "HAS_STAT_CURVE" | "HOSTILE_TO" | "ALLY_WITH" | "BELONGS_TO" | "REVEALS" | "REFERENCES";
        /** Entity */
        Entity: {
            /** Attrs */
            attrs?: {
                [key: string]: unknown;
            };
            /** Id */
            id: string;
            /**
             * Schema Version
             * @default ir-core@1
             */
            schema_version: string;
            source_ref?: components["schemas"]["SourceRef"] | null;
            /** Tags */
            tags?: string[] | null;
            type: components["schemas"]["NodeType"];
        };
        /** EvidenceArtifactRef */
        EvidenceArtifactRef: {
            /** Available */
            available: boolean;
            /** Evidence Id */
            evidence_id: string;
            /** Path */
            path: string;
            /** Schema Version */
            schema_version: string;
            /** Sha256 */
            sha256?: string | null;
        };
        /** ExecutionOptionResolveRequestV1 */
        ExecutionOptionResolveRequestV1: {
            /**
             * Llm Execution Mode
             * @enum {string}
             */
            llm_execution_mode: "live" | "record" | "replay";
            /** Prospective Request */
            prospective_request: components["schemas"]["ProspectiveGenericAgentRunRequestV1"] | components["schemas"]["ProspectiveGenerationProposeRequestV1"] | components["schemas"]["ProspectiveConstraintProposeRequestV1"] | components["schemas"]["ProspectivePatchRepairRequestV1"] | components["schemas"]["ProspectivePlaytestRunRequestV1"];
            /** Replay Source Run Id */
            replay_source_run_id?: string | null;
            /**
             * Request Schema Version
             * @default execution-option-resolve-request@1
             * @constant
             */
            request_schema_version: "execution-option-resolve-request@1";
            /**
             * Resource Operation Id
             * @enum {string}
             */
            resource_operation_id: "propose_generation_api_v1_generation_propose_post" | "repair_patch_api_v1_patches__artifact_id__repair_post" | "propose_constraint_api_v1_constraint_proposals_propose_post" | "submit_run_api_v1_runs_post" | "run_playtest_api_v1_playtest_run_post";
            run_kind: components["schemas"]["RunKindRef"];
        };
        /** ExecutionOptionViewV1 */
        ExecutionOptionViewV1: {
            /** Cassette Artifact Id */
            cassette_artifact_id?: string | null;
            domain_scope: components["schemas"]["DomainScope"];
            execution_version_plan: components["schemas"]["ExecutionVersionPlanV1"];
            /**
             * Llm Execution Mode
             * @enum {string}
             */
            llm_execution_mode: "live" | "record" | "replay";
            /** Option Id */
            option_id: string;
            /**
             * Option Schema Version
             * @default execution-option@1
             * @constant
             */
            option_schema_version: "execution-option@1";
            /** Prospective Request Hash */
            prospective_request_hash: string;
            /** Resolved Profile Binding Digests */
            resolved_profile_binding_digests: string[];
            /** Resolved Request Hash */
            resolved_request_hash: string;
            /**
             * Resource Operation Id
             * @enum {string}
             */
            resource_operation_id: "propose_generation_api_v1_generation_propose_post" | "repair_patch_api_v1_patches__artifact_id__repair_post" | "propose_constraint_api_v1_constraint_proposals_propose_post" | "submit_run_api_v1_runs_post" | "run_playtest_api_v1_playtest_run_post";
            run_kind: components["schemas"]["RunKindRef"];
            /** Source Run Id */
            source_run_id?: string | null;
        };
        /** ExecutionProfileViewV1 */
        ExecutionProfileViewV1: {
            /** Compatible Run Kinds */
            compatible_run_kinds: components["schemas"]["RunKindRef"][];
            /** Display Name */
            display_name: string;
            domain_scope: components["schemas"]["DomainScope"];
            /** Env Contract Version */
            env_contract_version?: string | null;
            /** Input Schema Ids */
            input_schema_ids: string[];
            /** Output Schema Ids */
            output_schema_ids: string[];
            profile: components["schemas"]["ProfileRefV1"];
            /**
             * Profile Kind
             * @enum {string}
             */
            profile_kind: "generation" | "patch_repair" | "constraint_extraction" | "review" | "llm_triage" | "checker" | "simulation" | "workload" | "config_export" | "task_suite_derivation" | "environment" | "playtest_planner" | "validation" | "constraint_compiler" | "rollback" | "schema_compatibility" | "impact_analysis" | "bench_evaluator" | "artifact_migrator" | "dr_plan" | "restore_target" | "dr_verifier";
            /** Profile Payload Hash */
            profile_payload_hash: string;
            /** Required Capabilities */
            required_capabilities: string[];
            /**
             * Status
             * @enum {string}
             */
            status: "active" | "replay_only" | "disabled";
            /** Stochastic */
            stochastic: boolean;
            target_environment_profile?: components["schemas"]["ProfileRefV1"] | null;
        };
        /** ExecutionVersionPlanV1 */
        ExecutionVersionPlanV1: {
            /** Agent Graph Version */
            agent_graph_version: string;
            /** Model Catalog Digest */
            model_catalog_digest: string;
            /** Model Catalog Version */
            model_catalog_version: number;
            /** Nodes */
            nodes: components["schemas"]["PlannedAgentNodeVersionV1"][];
            /** Plan Digest */
            plan_digest: string;
            /**
             * Plan Schema Version
             * @default execution-version-plan@1
             * @constant
             */
            plan_schema_version: "execution-version-plan@1";
            /** Routing Policy Digest */
            routing_policy_digest: string;
            /** Routing Policy Version */
            routing_policy_version: number;
        };
        /** ExternalSection */
        ExternalSection: {
            /** Adapter Version */
            adapter_version: string;
            after_oracle_fp: components["schemas"]["BinaryMetric"];
            /** Development */
            development: components["schemas"]["BinaryMetric"][];
            /** Evidence Ref */
            evidence_ref: string;
            /** Manifest Sha256 */
            manifest_sha256: string;
            /** Mapping Spec Sha256 */
            mapping_spec_sha256: string;
            /** Qualified Cases */
            qualified_cases: number;
            /** Reader Version */
            reader_version: string;
            /** Repository */
            repository: string;
            /** Source Id */
            source_id: string;
            /** Total Cases */
            total_cases: number;
            /** Verification */
            verification: components["schemas"]["BinaryMetric"][];
        };
        /** Finding */
        Finding: {
            /** Confidence */
            confidence?: number | null;
            /** Constraint Id */
            constraint_id?: string | null;
            /** Created At */
            created_at?: string | null;
            /** Defect Class */
            defect_class: string;
            /** Entities */
            entities?: string[];
            /** Evidence */
            evidence?: {
                [key: string]: unknown;
            };
            /**
             * Finding Schema Version
             * @default finding@1
             * @constant
             */
            finding_schema_version: "finding@1";
            /** Id */
            id: string;
            /** Message */
            message: string;
            /** Minimal Repro */
            minimal_repro?: {
                [key: string]: unknown;
            };
            /**
             * Oracle Type
             * @enum {string}
             */
            oracle_type: "deterministic" | "llm-assisted" | "simulation";
            /** Producer Id */
            producer_id: string;
            /** Producer Run Id */
            producer_run_id: string;
            /** Relations */
            relations?: string[];
            /**
             * Severity
             * @enum {string}
             */
            severity: "critical" | "major" | "minor";
            /** Snapshot Id */
            snapshot_id: string;
            /**
             * Source
             * @enum {string}
             */
            source: "checker" | "sim" | "playtest" | "llm";
            /**
             * Status
             * @enum {string}
             */
            status: "confirmed" | "unproven" | "dismissed" | "fixed" | "accepted_risk";
        };
        /** FindingEvidenceBindingV1 */
        FindingEvidenceBindingV1: {
            /** Evidence Artifact Id */
            evidence_artifact_id: string;
            /** Finding Digest */
            finding_digest: string;
            /** Finding Id */
            finding_id: string;
            /** Finding Revision */
            finding_revision: number;
        };
        /**
         * FindingPayloadV1
         * @description Identity-free semantic payload embedded by an immutable finding revision.
         */
        FindingPayloadV1: {
            /** Confidence */
            confidence?: number | null;
            /** Constraint Id */
            constraint_id?: string | null;
            /** Defect Class */
            defect_class: string;
            /** Entities */
            entities?: string[];
            /** Evidence */
            evidence?: {
                [key: string]: unknown;
            };
            /** Message */
            message: string;
            /** Minimal Repro */
            minimal_repro?: {
                [key: string]: unknown;
            };
            /**
             * Oracle Type
             * @enum {string}
             */
            oracle_type: "deterministic" | "llm-assisted" | "simulation";
            /**
             * Payload Schema Version
             * @default finding-payload@1
             * @constant
             */
            payload_schema_version: "finding-payload@1";
            /** Producer Id */
            producer_id: string;
            /** Producer Run Id */
            producer_run_id: string;
            /** Relations */
            relations?: string[];
            /**
             * Severity
             * @enum {string}
             */
            severity: "critical" | "major" | "minor";
            /** Snapshot Id */
            snapshot_id: string;
            /**
             * Source
             * @enum {string}
             */
            source: "checker" | "sim" | "playtest" | "llm";
            /**
             * Status
             * @enum {string}
             */
            status: "confirmed" | "unproven" | "dismissed" | "fixed" | "accepted_risk";
        };
        /**
         * FindingRevisionV1
         * @description Stable finding series identity plus one immutable semantic revision.
         */
        FindingRevisionV1: {
            /** Created At */
            created_at: string;
            /** Finding Id */
            finding_id: string;
            payload: components["schemas"]["FindingPayloadV1"];
            /** Revision */
            revision: number;
            /**
             * Revision Schema Version
             * @default finding-revision@1
             * @constant
             */
            revision_schema_version: "finding-revision@1";
            /** Supersedes Revision */
            supersedes_revision?: number | null;
        };
        /**
         * GenerationProposeRequestV1
         * @description ``POST /generation:propose`` — fixes ``generation.propose@1``.
         *
         *     The naked ``objective_goal_text`` is turned into an authenticated ``source_raw``
         *     Artifact by the composition root before the Run is created.
         */
        GenerationProposeRequestV1: {
            /** Base Snapshot Artifact Id */
            base_snapshot_artifact_id: string;
            /** Candidate Export Profiles */
            candidate_export_profiles: components["schemas"]["ProfileRefV1"][];
            /** Cassette Artifact Id */
            cassette_artifact_id?: string | null;
            /** Constraint Snapshot Artifact Id */
            constraint_snapshot_artifact_id?: string | null;
            domain_scope: components["schemas"]["DomainScope"];
            execution_version_plan?: components["schemas"]["ExecutionVersionPlanV1"] | null;
            /** Findings */
            findings: components["schemas"]["FindingEvidenceBindingV1"][];
            generation_policy: components["schemas"]["ProfileRefV1"];
            /**
             * Llm Execution Mode
             * @default record
             * @enum {string}
             */
            llm_execution_mode: "live" | "record" | "replay";
            /** Objective Goal Text */
            objective_goal_text: string;
            /**
             * Request Schema Version
             * @default generation-propose-request@1
             * @constant
             */
            request_schema_version: "generation-propose-request@1";
            target: components["schemas"]["RefReadBindingV1"];
        };
        /** GraphItemV1 */
        GraphItemV1: {
            entity?: components["schemas"]["Entity"] | null;
            /** Item Id */
            item_id: string;
            /**
             * Item Kind
             * @enum {string}
             */
            item_kind: "entity" | "relation";
            /**
             * Item Schema Version
             * @default graph-item@1
             * @constant
             */
            item_schema_version: "graph-item@1";
            relation?: components["schemas"]["Relation"] | null;
        };
        /** GraphSelectionV1 */
        GraphSelectionV1: {
            /** Entity Ids */
            entity_ids: string[];
            /**
             * Mode
             * @enum {string}
             */
            mode: "full" | "ids";
            /** Relation Ids */
            relation_ids: string[];
        };
        /** HedSection */
        HedSection: {
            /** Dispositions */
            dispositions: components["schemas"]["BinaryMetric"][];
            /** Evidence Ref */
            evidence_ref: string;
            model_snapshot: components["schemas"]["ModelSnapshot"];
            normalized_distance: components["schemas"]["DistributionMetric"];
            raw_distance: components["schemas"]["DistributionMetric"];
        };
        /** HistogramMetricSampleV1 */
        HistogramMetricSampleV1: {
            /** Count */
            count: number;
            /** Cumulative Bucket Counts */
            cumulative_bucket_counts: number[];
            /** Sum */
            sum?: number | null;
            /**
             * Ts Utc
             * Format: date-time
             */
            ts_utc: string;
        };
        /** HumanConstraintDraftRequestV1 */
        HumanConstraintDraftRequestV1: {
            /** Base Constraint Snapshot Artifact Id */
            base_constraint_snapshot_artifact_id?: string | null;
            /** Constraints */
            constraints: components["schemas"]["Constraint"][];
            domain_scope: components["schemas"]["DomainScope"];
            /** Dsl Grammar Version */
            dsl_grammar_version: string;
            expected_ref: components["schemas"]["RefValue"] | null;
            /** Rationale */
            rationale: string;
            /** Ref Name */
            ref_name: string;
            /**
             * Request Schema Version
             * @default human-constraint-draft-request@1
             * @constant
             */
            request_schema_version: "human-constraint-draft-request@1";
            /** Source Artifact Ids */
            source_artifact_ids: string[];
        };
        /** HumanConstraintRevisionRequestV1 */
        HumanConstraintRevisionRequestV1: {
            /** Approval Id */
            approval_id: string;
            /** Base Constraint Snapshot Artifact Id */
            base_constraint_snapshot_artifact_id?: string | null;
            /** Constraints */
            constraints: components["schemas"]["Constraint"][];
            domain_scope: components["schemas"]["DomainScope"];
            /** Dsl Grammar Version */
            dsl_grammar_version: string;
            expected_ref: components["schemas"]["RefValue"] | null;
            /** Expected Subject Head Revision */
            expected_subject_head_revision: number;
            /** Expected Workflow Revision */
            expected_workflow_revision: number;
            /** Rationale */
            rationale: string;
            /** Ref Name */
            ref_name: string;
            /**
             * Request Schema Version
             * @default human-constraint-revision-request@1
             * @constant
             */
            request_schema_version: "human-constraint-revision-request@1";
            /** Source Artifact Ids */
            source_artifact_ids: string[];
        };
        /** HumanPatchDraftRequestV1 */
        HumanPatchDraftRequestV1: {
            /** Base Snapshot Artifact Id */
            base_snapshot_artifact_id: string;
            /** Candidate Export Profiles */
            candidate_export_profiles: components["schemas"]["ProfileRefV1"][];
            /** Constraint Snapshot Artifact Id */
            constraint_snapshot_artifact_id?: string | null;
            expected_ref: components["schemas"]["RefValue"] | null;
            /** Expected To Fix */
            expected_to_fix: string[];
            /** Ops */
            ops: components["schemas"]["TypedOp"][];
            /** Preconditions */
            preconditions: {
                [key: string]: components["schemas"]["JsonValue"];
            }[];
            /** Rationale */
            rationale: string;
            /** Ref Name */
            ref_name: string;
            /**
             * Request Schema Version
             * @default human-patch-draft-request@1
             * @constant
             */
            request_schema_version: "human-patch-draft-request@1";
            /** Side Effect Risk */
            side_effect_risk: string;
        };
        /** HumanSpecUploadRequestV1 */
        HumanSpecUploadRequestV1: {
            /** Content Payload */
            content_payload: {
                [key: string]: components["schemas"]["JsonValue"];
            };
            domain_scope: components["schemas"]["DomainScope"];
            expected_ref: components["schemas"]["RefValue"] | null;
            /** Meta Schema Version */
            meta_schema_version: string;
            /** Ref Name */
            ref_name: string;
            /**
             * Request Schema Version
             * @default human-spec-upload-request@1
             * @constant
             */
            request_schema_version: "human-spec-upload-request@1";
            /** Schema Registry Version */
            schema_registry_version: string;
        };
        JsonValue: unknown;
        /**
         * JsonValueState
         * @description A JSON value whose absent and explicitly-null states cannot collapse.
         */
        JsonValueState: components["schemas"]["_MissingJsonValueState"] | components["schemas"]["_PresentJsonValueState"];
        /** LatencyObservationV1 */
        LatencyObservationV1: {
            /**
             * Observation Schema Version
             * @default latency-observation@1
             * @constant
             */
            observation_schema_version: "latency-observation@1";
            /** Provider Latency Ms */
            provider_latency_ms?: number | null;
            /**
             * Status
             * @enum {string}
             */
            status: "reported" | "unavailable";
        };
        /** LineageEntryV1 */
        LineageEntryV1: {
            artifact: components["schemas"]["ArtifactSummaryV1"];
            /** Depth */
            depth: number;
            /**
             * Entry Schema Version
             * @default lineage-entry@1
             * @constant
             */
            entry_schema_version: "lineage-entry@1";
        };
        /** LogErrorV1 */
        LogErrorV1: {
            /** Error Type */
            error_type: string;
            /** Message */
            message: string;
            /** Stack Fingerprint */
            stack_fingerprint?: string | null;
        };
        /** LogPageV1 */
        LogPageV1: {
            /**
             * Coverage End
             * Format: date-time
             */
            coverage_end: string;
            /**
             * Coverage Start
             * Format: date-time
             */
            coverage_start: string;
            /** Items */
            items: components["schemas"]["LogRecordViewV1"][];
            /** Next Cursor */
            next_cursor?: string | null;
            /**
             * Page Schema Version
             * @default log-page@1
             * @constant
             */
            page_schema_version: "log-page@1";
            /** Truncated */
            truncated: boolean;
        };
        /** LogRecordV1 */
        LogRecordV1: {
            error?: components["schemas"]["LogErrorV1"] | null;
            /** Event Name */
            event_name: string;
            /** Fields */
            fields?: {
                [key: string]: components["schemas"]["JsonValue"];
            };
            /**
             * Level
             * @enum {string}
             */
            level: "debug" | "info" | "warning" | "error" | "critical";
            /** Log Id */
            log_id: string;
            /**
             * Log Schema Version
             * @default log-record@1
             * @constant
             */
            log_schema_version: "log-record@1";
            /** Message */
            message: string;
            /** Producer Run Id */
            producer_run_id?: string | null;
            /** Request Id */
            request_id?: string | null;
            /** Run Id */
            run_id?: string | null;
            /** Service */
            service: string;
            /** Span Id */
            span_id?: string | null;
            /** Trace Id */
            trace_id?: string | null;
            /**
             * Ts Utc
             * Format: date-time
             */
            ts_utc: string;
        };
        /** LogRecordViewV1 */
        LogRecordViewV1: {
            record: components["schemas"]["LogRecordV1"];
            /**
             * Redacted Fields
             * @default []
             */
            redacted_fields: string[];
        };
        /** MergeConflict */
        MergeConflict: {
            /** Allowed Resolutions */
            allowed_resolutions: ("keep_current" | "take_proposed" | "custom")[];
            base: components["schemas"]["JsonValueState"];
            current: components["schemas"]["JsonValueState"];
            /** Id */
            id: string;
            /** Kind */
            kind: string;
            /** Path */
            path: string;
            proposed: components["schemas"]["JsonValueState"];
        };
        /** MetricDescriptorRefV1 */
        MetricDescriptorRefV1: {
            /** Descriptor Digest */
            descriptor_digest: string;
            /** Descriptor Version */
            descriptor_version: number;
            /** Metric Name */
            metric_name: string;
        };
        /** MetricDescriptorRegistryV1 */
        MetricDescriptorRegistryV1: {
            /** Descriptors */
            descriptors: components["schemas"]["MetricDescriptorV1"][];
            /** Global Series Limit */
            global_series_limit: number;
            /** Registry Digest */
            registry_digest: string;
            /**
             * Registry Schema Version
             * @default metric-descriptor-registry@1
             * @constant
             */
            registry_schema_version: "metric-descriptor-registry@1";
            /** Registry Version */
            registry_version: number;
        };
        /** MetricDescriptorV1 */
        MetricDescriptorV1: {
            /** Descriptor Digest */
            descriptor_digest: string;
            /**
             * Descriptor Schema Version
             * @default metric-descriptor@1
             * @constant
             */
            descriptor_schema_version: "metric-descriptor@1";
            /** Descriptor Version */
            descriptor_version: number;
            /** Histogram Bucket Bounds */
            histogram_bucket_bounds: number[];
            /** Label Keys */
            label_keys: string[];
            /** Metric Name */
            metric_name: string;
            /**
             * Metric Type
             * @enum {string}
             */
            metric_type: "counter" | "histogram" | "gauge";
            /** Series Limit */
            series_limit: number;
            /**
             * Unit
             * @enum {string}
             */
            unit: "1" | "count" | "ratio" | "token" | "request" | "step" | "ns" | "ms" | "s" | "byte";
            /**
             * Unit Schema Version
             * @default metric-units@1
             * @constant
             */
            unit_schema_version: "metric-units@1";
        };
        /** MetricPageV1 */
        MetricPageV1: {
            /**
             * Coverage End
             * Format: date-time
             */
            coverage_end: string;
            /**
             * Coverage Start
             * Format: date-time
             */
            coverage_start: string;
            /** Effective Resolution S */
            effective_resolution_s: number;
            /** Next Cursor */
            next_cursor?: string | null;
            /**
             * Page Schema Version
             * @default metric-page@1
             * @constant
             */
            page_schema_version: "metric-page@1";
            /** Series */
            series: components["schemas"]["MetricSeriesV1"][];
            /** Truncated */
            truncated: boolean;
        };
        /** MetricSeriesV1 */
        MetricSeriesV1: {
            /** Bucket Bounds */
            bucket_bounds?: number[] | null;
            descriptor: components["schemas"]["MetricDescriptorRefV1"];
            /** Histogram Points */
            histogram_points?: components["schemas"]["HistogramMetricSampleV1"][] | null;
            /** Labels */
            labels: {
                [key: string]: string;
            };
            /** Metric Name */
            metric_name: string;
            /**
             * Metric Type
             * @enum {string}
             */
            metric_type: "counter" | "histogram" | "gauge";
            /** Scalar Points */
            scalar_points?: components["schemas"]["ScalarMetricSampleV1"][] | null;
            /**
             * Unit
             * @enum {string}
             */
            unit: "1" | "count" | "ratio" | "token" | "request" | "step" | "ns" | "ms" | "s" | "byte";
        };
        /** ModelSnapshot */
        ModelSnapshot: {
            /** Model */
            model: string;
            /** Provider */
            provider: string;
            /** Snapshot Tag */
            snapshot_tag: string;
        };
        /** MonetaryObservationV1 */
        MonetaryObservationV1: {
            /** Amount */
            amount?: string | null;
            /** Currency */
            currency?: string | null;
            /**
             * Observation Schema Version
             * @default monetary-observation@1
             * @constant
             */
            observation_schema_version: "monetary-observation@1";
            /** Price Book Version */
            price_book_version?: string | null;
            /** Quote Effective At */
            quote_effective_at?: string | null;
            /**
             * Status
             * @enum {string}
             */
            status: "reported" | "unavailable";
        };
        /** NarrativeSection */
        NarrativeSection: {
            /** Bdr */
            bdr: components["schemas"]["BinaryMetric"][];
            clean_fp: components["schemas"]["BinaryMetric"];
            /** Corpus Manifest Sha256 */
            corpus_manifest_sha256: string;
            /** Evidence Ref */
            evidence_ref: string;
            model_snapshot: components["schemas"]["ModelSnapshot"];
            /** Protocol Sha256 */
            protocol_sha256: string;
        };
        /**
         * NodeType
         * @enum {string}
         */
        NodeType: "FACTION" | "CHARACTER" | "NPC" | "QUEST" | "QUEST_STEP" | "DIALOGUE_NODE" | "REGION" | "SPAWN_POINT" | "INTERACTABLE" | "ITEM" | "MONSTER" | "CURRENCY" | "SHOP" | "DROP_TABLE" | "REWARD_TABLE" | "GACHA_POOL" | "EVENT" | "UNLOCK_CONDITION" | "EQUIPMENT" | "SKILL" | "STATUS_EFFECT" | "EFFECT" | "BATTLE_ENCOUNTER" | "FORMULA";
        /** OpaquePageV1[ApprovalViewV1] */
        OpaquePageV1_ApprovalViewV1_: {
            /** Expires At */
            expires_at: string;
            /** Items */
            items: components["schemas"]["ApprovalViewV1"][];
            /** Next Cursor */
            next_cursor?: string | null;
            /**
             * Page Schema Version
             * @default page@1
             * @constant
             */
            page_schema_version: "page@1";
            /** Read Snapshot Id */
            read_snapshot_id: string;
        };
        /** OpaquePageV1[ConstraintProposalReadViewV1] */
        OpaquePageV1_ConstraintProposalReadViewV1_: {
            /** Expires At */
            expires_at: string;
            /** Items */
            items: components["schemas"]["ConstraintProposalReadViewV1"][];
            /** Next Cursor */
            next_cursor?: string | null;
            /**
             * Page Schema Version
             * @default page@1
             * @constant
             */
            page_schema_version: "page@1";
            /** Read Snapshot Id */
            read_snapshot_id: string;
        };
        /** OpaquePageV1[ConstraintSnapshotViewV1] */
        OpaquePageV1_ConstraintSnapshotViewV1_: {
            /** Expires At */
            expires_at: string;
            /** Items */
            items: components["schemas"]["ConstraintSnapshotViewV1"][];
            /** Next Cursor */
            next_cursor?: string | null;
            /**
             * Page Schema Version
             * @default page@1
             * @constant
             */
            page_schema_version: "page@1";
            /** Read Snapshot Id */
            read_snapshot_id: string;
        };
        /** OpaquePageV1[ExecutionProfileViewV1] */
        OpaquePageV1_ExecutionProfileViewV1_: {
            /** Expires At */
            expires_at: string;
            /** Items */
            items: components["schemas"]["ExecutionProfileViewV1"][];
            /** Next Cursor */
            next_cursor?: string | null;
            /**
             * Page Schema Version
             * @default page@1
             * @constant
             */
            page_schema_version: "page@1";
            /** Read Snapshot Id */
            read_snapshot_id: string;
        };
        /** OpaquePageV1[FindingRevisionV1] */
        OpaquePageV1_FindingRevisionV1_: {
            /** Expires At */
            expires_at: string;
            /** Items */
            items: components["schemas"]["FindingRevisionV1"][];
            /** Next Cursor */
            next_cursor?: string | null;
            /**
             * Page Schema Version
             * @default page@1
             * @constant
             */
            page_schema_version: "page@1";
            /** Read Snapshot Id */
            read_snapshot_id: string;
        };
        /** OpaquePageV1[GraphItemV1] */
        OpaquePageV1_GraphItemV1_: {
            /** Expires At */
            expires_at: string;
            /** Items */
            items: components["schemas"]["GraphItemV1"][];
            /** Next Cursor */
            next_cursor?: string | null;
            /**
             * Page Schema Version
             * @default page@1
             * @constant
             */
            page_schema_version: "page@1";
            /** Read Snapshot Id */
            read_snapshot_id: string;
        };
        /** OpaquePageV1[LineageEntryV1] */
        OpaquePageV1_LineageEntryV1_: {
            /** Expires At */
            expires_at: string;
            /** Items */
            items: components["schemas"]["LineageEntryV1"][];
            /** Next Cursor */
            next_cursor?: string | null;
            /**
             * Page Schema Version
             * @default page@1
             * @constant
             */
            page_schema_version: "page@1";
            /** Read Snapshot Id */
            read_snapshot_id: string;
        };
        /** OpaquePageV1[MergeConflict] */
        OpaquePageV1_MergeConflict_: {
            /** Expires At */
            expires_at: string;
            /** Items */
            items: components["schemas"]["MergeConflict"][];
            /** Next Cursor */
            next_cursor?: string | null;
            /**
             * Page Schema Version
             * @default page@1
             * @constant
             */
            page_schema_version: "page@1";
            /** Read Snapshot Id */
            read_snapshot_id: string;
        };
        /** OpaquePageV1[PatchArtifactReadViewV1] */
        OpaquePageV1_PatchArtifactReadViewV1_: {
            /** Expires At */
            expires_at: string;
            /** Items */
            items: components["schemas"]["PatchArtifactReadViewV1"][];
            /** Next Cursor */
            next_cursor?: string | null;
            /**
             * Page Schema Version
             * @default page@1
             * @constant
             */
            page_schema_version: "page@1";
            /** Read Snapshot Id */
            read_snapshot_id: string;
        };
        /** OpaquePageV1[RefHistoryEntryV1] */
        OpaquePageV1_RefHistoryEntryV1_: {
            /** Expires At */
            expires_at: string;
            /** Items */
            items: components["schemas"]["RefHistoryEntryV1"][];
            /** Next Cursor */
            next_cursor?: string | null;
            /**
             * Page Schema Version
             * @default page@1
             * @constant
             */
            page_schema_version: "page@1";
            /** Read Snapshot Id */
            read_snapshot_id: string;
        };
        /** OpaquePageV1[ReviewArtifactViewV1] */
        OpaquePageV1_ReviewArtifactViewV1_: {
            /** Expires At */
            expires_at: string;
            /** Items */
            items: components["schemas"]["ReviewArtifactViewV1"][];
            /** Next Cursor */
            next_cursor?: string | null;
            /**
             * Page Schema Version
             * @default page@1
             * @constant
             */
            page_schema_version: "page@1";
            /** Read Snapshot Id */
            read_snapshot_id: string;
        };
        /** OpaquePageV1[RollbackRequestReadViewV1] */
        OpaquePageV1_RollbackRequestReadViewV1_: {
            /** Expires At */
            expires_at: string;
            /** Items */
            items: components["schemas"]["RollbackRequestReadViewV1"][];
            /** Next Cursor */
            next_cursor?: string | null;
            /**
             * Page Schema Version
             * @default page@1
             * @constant
             */
            page_schema_version: "page@1";
            /** Read Snapshot Id */
            read_snapshot_id: string;
        };
        /** OpaquePageV1[RunCommandViewV1] */
        OpaquePageV1_RunCommandViewV1_: {
            /** Expires At */
            expires_at: string;
            /** Items */
            items: components["schemas"]["RunCommandViewV1"][];
            /** Next Cursor */
            next_cursor?: string | null;
            /**
             * Page Schema Version
             * @default page@1
             * @constant
             */
            page_schema_version: "page@1";
            /** Read Snapshot Id */
            read_snapshot_id: string;
        };
        /** OpaquePageV1[RunFindingLinkViewV1] */
        OpaquePageV1_RunFindingLinkViewV1_: {
            /** Expires At */
            expires_at: string;
            /** Items */
            items: components["schemas"]["RunFindingLinkViewV1"][];
            /** Next Cursor */
            next_cursor?: string | null;
            /**
             * Page Schema Version
             * @default page@1
             * @constant
             */
            page_schema_version: "page@1";
            /** Read Snapshot Id */
            read_snapshot_id: string;
        };
        /** OpaquePageV1[RunViewV1] */
        OpaquePageV1_RunViewV1_: {
            /** Expires At */
            expires_at: string;
            /** Items */
            items: components["schemas"]["RunViewV1"][];
            /** Next Cursor */
            next_cursor?: string | null;
            /**
             * Page Schema Version
             * @default page@1
             * @constant
             */
            page_schema_version: "page@1";
            /** Read Snapshot Id */
            read_snapshot_id: string;
        };
        /** OpaquePageV1[SnapshotDiffEntry] */
        OpaquePageV1_SnapshotDiffEntry_: {
            /** Expires At */
            expires_at: string;
            /** Items */
            items: components["schemas"]["SnapshotDiffEntry"][];
            /** Next Cursor */
            next_cursor?: string | null;
            /**
             * Page Schema Version
             * @default page@1
             * @constant
             */
            page_schema_version: "page@1";
            /** Read Snapshot Id */
            read_snapshot_id: string;
        };
        /** OpaquePageV1[SpecViewV1] */
        OpaquePageV1_SpecViewV1_: {
            /** Expires At */
            expires_at: string;
            /** Items */
            items: components["schemas"]["SpecViewV1"][];
            /** Next Cursor */
            next_cursor?: string | null;
            /**
             * Page Schema Version
             * @default page@1
             * @constant
             */
            page_schema_version: "page@1";
            /** Read Snapshot Id */
            read_snapshot_id: string;
        };
        /** OpaquePageV1[TaskSuiteArtifactViewV1] */
        OpaquePageV1_TaskSuiteArtifactViewV1_: {
            /** Expires At */
            expires_at: string;
            /** Items */
            items: components["schemas"]["TaskSuiteArtifactViewV1"][];
            /** Next Cursor */
            next_cursor?: string | null;
            /**
             * Page Schema Version
             * @default page@1
             * @constant
             */
            page_schema_version: "page@1";
            /** Read Snapshot Id */
            read_snapshot_id: string;
        };
        /** PasswordAuthRequestV1 */
        PasswordAuthRequestV1: {
            /** Login Name */
            login_name: string;
            /**
             * Password
             * Format: password
             */
            password: string;
            /**
             * Schema Version
             * @default password-auth@1
             * @constant
             */
            schema_version: "password-auth@1";
        };
        /**
         * PatchArtifactReadViewV1
         * @description Workflow Patch projection with its stable Artifact identity.
         */
        PatchArtifactReadViewV1: {
            /**
             * Approval Status
             * @enum {string}
             */
            approval_status: "draft" | "validating" | "validation_failed" | "validated" | "pending_approval" | "auto_apply_eligible" | "approved" | "changes_requested" | "rejected" | "applied" | "rolled_back" | "superseded";
            artifact: components["schemas"]["ArtifactSummaryV1"];
            patch: components["schemas"]["PatchV2"];
            /** Regression Status */
            regression_status: string;
            /** Validation Status */
            validation_status: string;
            /**
             * View Schema Version
             * @default patch-artifact-read-view@1
             * @constant
             */
            view_schema_version: "patch-artifact-read-view@1";
            /** Workflow Revision */
            workflow_revision: number;
        };
        /** PatchRebaseRequestV1 */
        PatchRebaseRequestV1: {
            /** Approval Id */
            approval_id: string;
            expected_ref: components["schemas"]["RefValue"];
            /** Expected Subject Head Revision */
            expected_subject_head_revision: number;
            /** Expected Workflow Revision */
            expected_workflow_revision: number;
            /** Ref Name */
            ref_name: string;
            /**
             * Request Schema Version
             * @default patch-rebase-request@1
             * @constant
             */
            request_schema_version: "patch-rebase-request@1";
        };
        /** PatchRepairPayloadV1 */
        PatchRepairPayloadV1: {
            /** Base Snapshot Artifact Id */
            base_snapshot_artifact_id: string;
            /** Candidate Export Profiles */
            candidate_export_profiles: components["schemas"]["ProfileRefV1"][];
            /** Checker Profiles */
            checker_profiles: components["schemas"]["ProfileRefV1"][];
            /** Constraint Snapshot Artifact Id */
            constraint_snapshot_artifact_id?: string | null;
            /** Expected Subject Head Revision */
            expected_subject_head_revision: number;
            /** Expected Workflow Revision */
            expected_workflow_revision: number;
            /** Findings */
            findings: components["schemas"]["FindingEvidenceBindingV1"][];
            /** Preview Snapshot Artifact Id */
            preview_snapshot_artifact_id: string;
            /** Regression Suite Artifact Ids */
            regression_suite_artifact_ids: string[];
            repair_policy: components["schemas"]["ProfileRefV1"];
            /**
             * Schema Version
             * @default patch-repair@1
             * @constant
             */
            schema_version: "patch-repair@1";
            /** Simulation Profiles */
            simulation_profiles: components["schemas"]["ProfileRefV1"][];
            /** Subject Patch Artifact Id */
            subject_patch_artifact_id: string;
            target: components["schemas"]["RefReadBindingV1"];
            /** Validation Evidence Artifact Id */
            validation_evidence_artifact_id: string;
        };
        /**
         * PatchRepairRequestV1
         * @description ``POST /patches/{id}:repair`` — fixes ``patch.repair@1`` via typed params.
         */
        PatchRepairRequestV1: {
            /** Cassette Artifact Id */
            cassette_artifact_id?: string | null;
            execution_version_plan?: components["schemas"]["ExecutionVersionPlanV1"] | null;
            /**
             * Llm Execution Mode
             * @default record
             * @enum {string}
             */
            llm_execution_mode: "live" | "record" | "replay";
            params: components["schemas"]["PatchRepairPayloadV1"];
            /**
             * Request Schema Version
             * @default patch-repair-request@1
             * @constant
             */
            request_schema_version: "patch-repair-request@1";
            /** Seed */
            seed?: number | null;
        };
        /** PatchTargetBindingV1 */
        PatchTargetBindingV1: {
            /**
             * Binding Schema Version
             * @default approval-target-binding@1
             * @constant
             */
            binding_schema_version: "approval-target-binding@1";
            expected_ref?: components["schemas"]["RefValue"] | null;
            /** Ref Name */
            ref_name: string;
            /**
             * @description discriminator enum property added by openapi-typescript
             * @enum {string}
             */
            subject_kind: "patch";
            /** Target Artifact Id */
            target_artifact_id: string;
            /**
             * Target Artifact Kind
             * @default ir_snapshot
             * @constant
             */
            target_artifact_kind: "ir_snapshot";
            /** Target Digest */
            target_digest: string;
            /** Target Snapshot Id */
            target_snapshot_id: string;
        };
        /**
         * PatchV2
         * @description Immutable Patch payload; mutable workflow state lives in ApprovalItem.
         */
        PatchV2: {
            /** Base Snapshot Id */
            base_snapshot_id: string;
            /** Expected To Fix */
            expected_to_fix?: string[];
            /** Ops */
            ops: components["schemas"]["TypedOp"][];
            /**
             * Patch Schema Version
             * @default patch@2
             * @constant
             */
            patch_schema_version: "patch@2";
            /** Preconditions */
            preconditions?: {
                [key: string]: unknown;
            }[];
            /**
             * Produced By
             * @enum {string}
             */
            produced_by: "agent" | "human";
            /** Producer Run Id */
            producer_run_id?: string | null;
            /** Rationale */
            rationale: string;
            /** Revision */
            revision: number;
            /** Side Effect Risk */
            side_effect_risk: string;
            /** Supersedes Artifact Id */
            supersedes_artifact_id?: string | null;
            /** Target Snapshot Id */
            target_snapshot_id: string;
        };
        /** PatchValidationAdmissionRequestV1 */
        PatchValidationAdmissionRequestV1: {
            /** Approval Id */
            approval_id: string;
            /** Base Snapshot Artifact Id */
            base_snapshot_artifact_id: string;
            /** Candidate Config Export Artifact Ids */
            candidate_config_export_artifact_ids: string[];
            /** Checker Profiles */
            checker_profiles: components["schemas"]["ProfileRefV1"][];
            /** Constraint Snapshot Artifact Id */
            constraint_snapshot_artifact_id?: string | null;
            /**
             * Expected Findings
             * @default []
             */
            expected_findings: components["schemas"]["FindingEvidenceBindingV1"][];
            /** Expected Subject Head Revision */
            expected_subject_head_revision: number;
            /** Expected Workflow Revision */
            expected_workflow_revision: number;
            /** Findings */
            findings: components["schemas"]["FindingEvidenceBindingV1"][];
            /** Playtest Trace Artifact Ids */
            playtest_trace_artifact_ids: string[];
            /** Preview Snapshot Artifact Id */
            preview_snapshot_artifact_id: string;
            /** Regression Suite Artifact Ids */
            regression_suite_artifact_ids: string[];
            /**
             * Request Schema Version
             * @default patch-validation-admission-request@1
             * @constant
             */
            request_schema_version: "patch-validation-admission-request@1";
            /** Review Artifact Ids */
            review_artifact_ids: string[];
            /** Seed */
            seed?: number | null;
            /** Simulation Profiles */
            simulation_profiles: components["schemas"]["ProfileRefV1"][];
            /** Subject Digest */
            subject_digest: string;
            target: components["schemas"]["RefReadBindingV1"];
            validation_policy: components["schemas"]["ProfileRefV1"];
        };
        /** Permission */
        Permission: {
            /** Action */
            action: string;
            /** Domain Scope */
            domain_scope: components["schemas"]["DomainScope"] | "all" | null;
            /** Resource Kind */
            resource_kind: string;
        };
        /** PlannedAgentNodeVersionV1 */
        PlannedAgentNodeVersionV1: {
            /** Agent Node Id */
            agent_node_id: string;
            /** Allowed Model Snapshots */
            allowed_model_snapshots: string[];
            /** Prompt Version */
            prompt_version: string;
            /** Tool Version */
            tool_version: string;
        };
        /** PlaytestEpisodeBindingV1 */
        PlaytestEpisodeBindingV1: {
            /** Episode Id */
            episode_id: string;
            /** Scenario Spec Artifact Id */
            scenario_spec_artifact_id: string;
        };
        /** PlaytestProvideInputPayloadV1 */
        PlaytestProvideInputPayloadV1: {
            /** Choice Id */
            choice_id: string;
            /** Expected State Hash */
            expected_state_hash: string;
            /** Interaction Id */
            interaction_id: string;
            /**
             * @description discriminator enum property added by openapi-typescript
             * @enum {string}
             */
            schema_version: "playtest-provide-input@1";
        };
        /** PlaytestRunPayloadV1 */
        PlaytestRunPayloadV1: {
            /** Config Artifact Id */
            config_artifact_id: string;
            /** Constraint Snapshot Artifact Id */
            constraint_snapshot_artifact_id: string;
            environment_profile: components["schemas"]["ProfileRefV1"];
            /** Episodes */
            episodes: components["schemas"]["PlaytestEpisodeBindingV1"][];
            /**
             * Interaction Mode
             * @enum {string}
             */
            interaction_mode: "autonomous" | "bounded_choice";
            /** Max Steps Per Episode */
            max_steps_per_episode: number;
            planner_policy: components["schemas"]["ProfileRefV1"];
            /**
             * Schema Version
             * @default playtest-run@1
             * @constant
             */
            schema_version: "playtest-run@1";
            /** Task Suite Artifact Id */
            task_suite_artifact_id: string;
        };
        /**
         * PlaytestRunRequestV1
         * @description ``POST /playtest:run`` — fixes ``playtest.run@1`` via typed params.
         */
        PlaytestRunRequestV1: {
            /** Cassette Artifact Id */
            cassette_artifact_id?: string | null;
            execution_version_plan?: components["schemas"]["ExecutionVersionPlanV1"] | null;
            /**
             * Llm Execution Mode
             * @default record
             * @enum {string}
             */
            llm_execution_mode: "live" | "record" | "replay";
            params: components["schemas"]["PlaytestRunPayloadV1"];
            /**
             * Request Schema Version
             * @default playtest-run-request@1
             * @constant
             */
            request_schema_version: "playtest-run-request@1";
            /** Seed */
            seed: number;
        };
        /** PowerMetric */
        PowerMetric: {
            /** Achieved Half Width */
            achieved_half_width: number;
            /** Bucket */
            bucket: string;
            defect_class: components["schemas"]["DefectClass"];
            /** Evaluated N */
            evaluated_n: number;
            /** Evidence Ref */
            evidence_ref?: string | null;
            /**
             * Status
             * @enum {string}
             */
            status: "measured" | "underpowered";
            /** Target Half Width */
            target_half_width: number;
        };
        /** Predicate */
        Predicate: {
            /** Expr */
            expr: string;
            /**
             * Oracle
             * @default deterministic
             * @enum {string}
             */
            oracle: "deterministic" | "llm-assisted";
        };
        /** Principal */
        Principal: {
            /** Authz Revision */
            authz_revision: number;
            /** Credential Epoch */
            credential_epoch: number;
            /** Display Name */
            display_name: string;
            /** Id */
            id: string;
            /**
             * Kind
             * @enum {string}
             */
            kind: "human" | "service" | "system";
            /** Revision */
            revision: number;
            /** Roles */
            roles: components["schemas"]["RoleAssignmentV1"][];
            /**
             * Status
             * @enum {string}
             */
            status: "active" | "disabled";
        };
        /** Problem */
        Problem: {
            /** Code */
            code: string;
            /**
             * Conflict Set Id
             * @default null
             */
            conflict_set_id: string | null;
            /** Detail */
            detail: string;
            /**
             * Earliest Cursor
             * @default null
             */
            earliest_cursor: string | null;
            /**
             * Errors
             * @default null
             */
            errors: {
                [key: string]: components["schemas"]["JsonValue"];
            }[] | null;
            /** Instance */
            instance: string;
            /** Request Id */
            request_id: string;
            /**
             * Retry After S
             * @default null
             */
            retry_after_s: number | null;
            /**
             * Run Id
             * @default null
             */
            run_id: string | null;
            /** Status */
            status: number;
            /** Title */
            title: string;
            /**
             * Trace Id
             * @default null
             */
            trace_id: string | null;
            /** Type */
            type: string;
        };
        /** ProfileRefV1 */
        ProfileRefV1: {
            /** Profile Id */
            profile_id: string;
            /** Version */
            version: number;
        };
        /** ProspectiveConstraintProposeRequestV1 */
        ProspectiveConstraintProposeRequestV1: {
            /** Authoring Goal Text */
            authoring_goal_text: string;
            /** Base Constraint Snapshot Artifact Id */
            base_constraint_snapshot_artifact_id?: string | null;
            /** Cassette Artifact Id */
            cassette_artifact_id: null;
            domain_scope: components["schemas"]["DomainScope"];
            /** Dsl Grammar Version */
            dsl_grammar_version: string;
            /** Execution Version Plan */
            execution_version_plan: null;
            extraction_policy: components["schemas"]["ProfileRefV1"];
            /**
             * Llm Execution Mode
             * @default record
             * @enum {string}
             */
            llm_execution_mode: "live" | "record" | "replay";
            /**
             * @description discriminator enum property added by openapi-typescript
             * @enum {string}
             */
            request_schema_version: "constraint-propose-request@1";
            /** Source Artifact Ids */
            source_artifact_ids: string[];
        };
        /** ProspectiveGenerationProposeRequestV1 */
        ProspectiveGenerationProposeRequestV1: {
            /** Base Snapshot Artifact Id */
            base_snapshot_artifact_id: string;
            /** Candidate Export Profiles */
            candidate_export_profiles: components["schemas"]["ProfileRefV1"][];
            /** Cassette Artifact Id */
            cassette_artifact_id: null;
            /** Constraint Snapshot Artifact Id */
            constraint_snapshot_artifact_id?: string | null;
            domain_scope: components["schemas"]["DomainScope"];
            /** Execution Version Plan */
            execution_version_plan: null;
            /** Findings */
            findings: components["schemas"]["FindingEvidenceBindingV1"][];
            generation_policy: components["schemas"]["ProfileRefV1"];
            /**
             * Llm Execution Mode
             * @default record
             * @enum {string}
             */
            llm_execution_mode: "live" | "record" | "replay";
            /** Objective Goal Text */
            objective_goal_text: string;
            /**
             * @description discriminator enum property added by openapi-typescript
             * @enum {string}
             */
            request_schema_version: "generation-propose-request@1";
            target: components["schemas"]["RefReadBindingV1"];
        };
        /**
         * ProspectiveGenericAgentRunRequestV1
         * @description Prospective Agent-only branch of the unchanged generic ``POST /runs`` wire.
         */
        ProspectiveGenericAgentRunRequestV1: {
            /** Cassette Artifact Id */
            cassette_artifact_id: null;
            /** Execution Version Plan */
            execution_version_plan: null;
            /**
             * Llm Execution Mode
             * @enum {string}
             */
            llm_execution_mode: "live" | "record" | "replay";
            /** Params */
            params: components["schemas"]["ReviewRunPayloadV1"] | components["schemas"]["BenchRunPayloadV1"];
            /**
             * @description discriminator enum property added by openapi-typescript
             * @enum {string}
             */
            request_schema_version: "run-submission-request@1";
            /** Seed */
            seed?: number | null;
        };
        /** ProspectivePatchRepairRequestV1 */
        ProspectivePatchRepairRequestV1: {
            /** Cassette Artifact Id */
            cassette_artifact_id: null;
            /** Execution Version Plan */
            execution_version_plan: null;
            /**
             * Llm Execution Mode
             * @default record
             * @enum {string}
             */
            llm_execution_mode: "live" | "record" | "replay";
            params: components["schemas"]["PatchRepairPayloadV1"];
            /**
             * @description discriminator enum property added by openapi-typescript
             * @enum {string}
             */
            request_schema_version: "patch-repair-request@1";
            /** Seed */
            seed?: number | null;
        };
        /** ProspectivePlaytestRunRequestV1 */
        ProspectivePlaytestRunRequestV1: {
            /** Cassette Artifact Id */
            cassette_artifact_id: null;
            /** Execution Version Plan */
            execution_version_plan: null;
            /**
             * Llm Execution Mode
             * @default record
             * @enum {string}
             */
            llm_execution_mode: "live" | "record" | "replay";
            params: components["schemas"]["PlaytestRunPayloadV1"];
            /**
             * @description discriminator enum property added by openapi-typescript
             * @enum {string}
             */
            request_schema_version: "playtest-run-request@1";
            /** Seed */
            seed: number;
        };
        /** QaSection */
        QaSection: {
            assisted_success: components["schemas"]["BinaryMetric"];
            /**
             * Conclusion
             * @enum {string}
             */
            conclusion: "pending" | "savings" | "inconclusive" | "negative" | "failed";
            /** Evidence Ref */
            evidence_ref?: string | null;
            manual_success: components["schemas"]["BinaryMetric"];
            paired_saved_fraction: components["schemas"]["DistributionMetric"];
            paired_saved_minutes: components["schemas"]["DistributionMetric"];
            /** Protocol Sha256 */
            protocol_sha256: string;
            /**
             * Scope
             * @constant
             */
            scope: "single-participant-eight-session-case-study";
            /**
             * Time Scoring
             * @default legacy_observed_capped_active
             * @enum {string}
             */
            time_scoring: "legacy_observed_capped_active" | "incorrect_uses_active_cap";
        };
        /** RebaseResult */
        RebaseResult: {
            /** Conflict Set Id */
            conflict_set_id?: string | null;
            /** New Patch Artifact Id */
            new_patch_artifact_id?: string | null;
            /**
             * Status
             * @enum {string}
             */
            status: "clean" | "conflicted";
        };
        /** RefHistoryEntryV1 */
        RefHistoryEntryV1: {
            /**
             * Entry Schema Version
             * @default ref-history-entry@1
             * @constant
             */
            entry_schema_version: "ref-history-entry@1";
            /** Ref Name */
            ref_name: string;
            value: components["schemas"]["RefValue"];
        };
        /** RefReadBindingV1 */
        RefReadBindingV1: {
            expected_ref?: components["schemas"]["RefValue"] | null;
            /** Ref Name */
            ref_name: string;
        };
        /** RefValue */
        RefValue: {
            /** Artifact Id */
            artifact_id: string;
            /** Revision */
            revision: number;
        };
        /** Relation */
        Relation: {
            /** Attrs */
            attrs?: {
                [key: string]: unknown;
            } | null;
            /** Dst Id */
            dst_id: string;
            /** Id */
            id: string;
            /**
             * Schema Version
             * @default ir-core@1
             */
            schema_version: string;
            source_ref?: components["schemas"]["SourceRef"] | null;
            /** Src Id */
            src_id: string;
            type: components["schemas"]["EdgeType"];
        };
        /** ResolveConflictsRequestV1 */
        ResolveConflictsRequestV1: {
            /** Approval Id */
            approval_id: string;
            /** Conflict Set Id */
            conflict_set_id: string;
            expected_ref: components["schemas"]["RefValue"];
            /** Expected Subject Head Revision */
            expected_subject_head_revision: number;
            /** Expected Workflow Revision */
            expected_workflow_revision: number;
            /** Ref Name */
            ref_name: string;
            /**
             * Request Schema Version
             * @default resolve-conflicts-request@1
             * @constant
             */
            request_schema_version: "resolve-conflicts-request@1";
            /** Resolutions */
            resolutions: components["schemas"]["ConflictResolution"][];
        };
        /** ResolvedExecutionProfileBindingV1 */
        ResolvedExecutionProfileBindingV1: {
            /** Catalog Digest */
            catalog_digest: string;
            /** Catalog Version */
            catalog_version: number;
            /**
             * Expected Profile Kind
             * @enum {string}
             */
            expected_profile_kind: "generation" | "patch_repair" | "constraint_extraction" | "review" | "llm_triage" | "checker" | "simulation" | "workload" | "config_export" | "task_suite_derivation" | "environment" | "playtest_planner" | "validation" | "constraint_compiler" | "rollback" | "schema_compatibility" | "impact_analysis" | "bench_evaluator" | "artifact_migrator" | "dr_plan" | "restore_target" | "dr_verifier";
            /** Field Path */
            field_path: string;
            profile: components["schemas"]["ProfileRefV1"];
            /** Profile Payload Hash */
            profile_payload_hash: string;
        };
        /** ReviewArtifactViewV1 */
        ReviewArtifactViewV1: {
            artifact: components["schemas"]["ArtifactSummaryV1"];
            report: components["schemas"]["ReviewReport"];
            /**
             * View Schema Version
             * @default review-artifact-view@1
             * @constant
             */
            view_schema_version: "review-artifact-view@1";
        };
        /**
         * ReviewProducerBindingViewV1
         * @description Exact occurrence of one Review Artifact in one terminal Run manifest.
         */
        ReviewProducerBindingViewV1: {
            /** Attempt No */
            attempt_no: number;
            /**
             * Finding Authority
             * @enum {string}
             */
            finding_authority: "exact-run-links" | "embedded-only" | "not-applicable";
            /**
             * Manifest Role
             * @enum {string}
             */
            manifest_role: "output" | "evidence";
            /** Outcome Code */
            outcome_code: string;
            /** Outcome Policy Id */
            outcome_policy_id: string;
            /** Outcome Policy Version */
            outcome_policy_version: number;
            /** Outcome Rule Id */
            outcome_rule_id: string;
            /** Review Artifact Id */
            review_artifact_id: string;
            /** Run Id */
            run_id: string;
            run_kind: components["schemas"]["RunKindRef"];
            /** Terminal Manifest Id */
            terminal_manifest_id: string;
            /**
             * Terminal Manifest Kind
             * @enum {string}
             */
            terminal_manifest_kind: "run_result" | "run_failure";
            /**
             * Terminal Status
             * @enum {string}
             */
            terminal_status: "succeeded" | "failed" | "cancelled" | "timed_out";
            /**
             * View Schema Version
             * @default review-producer-binding-view@1
             * @constant
             */
            view_schema_version: "review-producer-binding-view@1";
        };
        /** ReviewReport */
        ReviewReport: {
            /** By Defect Class */
            by_defect_class?: components["schemas"]["DefectClassCount"][];
            /** Created At */
            created_at?: string | null;
            /** Deterministic Findings */
            deterministic_findings?: components["schemas"]["Finding"][];
            /** Llm Assisted Findings */
            llm_assisted_findings?: components["schemas"]["Finding"][];
            /**
             * Review Schema Version
             * @default review@1
             */
            review_schema_version: string;
            /** Simulation Findings */
            simulation_findings?: components["schemas"]["Finding"][];
            /** Snapshot Id */
            snapshot_id: string;
            /** Unproven Findings */
            unproven_findings?: components["schemas"]["Finding"][];
        };
        /** ReviewRunPayloadV1 */
        ReviewRunPayloadV1: {
            /** Checker Profiles */
            checker_profiles: components["schemas"]["ProfileRefV1"][];
            /** Constraint Snapshot Artifact Id */
            constraint_snapshot_artifact_id?: string | null;
            llm_triage_policy?: components["schemas"]["ProfileRefV1"] | null;
            review_profile: components["schemas"]["ProfileRefV1"];
            /**
             * @description discriminator enum property added by openapi-typescript
             * @enum {string}
             */
            schema_version: "review-run@1";
            selection: components["schemas"]["GraphSelectionV1"];
            /** Simulation Profiles */
            simulation_profiles: components["schemas"]["ProfileRefV1"][];
            /** Snapshot Artifact Id */
            snapshot_artifact_id: string;
        };
        /** RoleAssignmentV1 */
        RoleAssignmentV1: {
            /** Assignment Id */
            assignment_id: string;
            /**
             * Assignment Schema Version
             * @default role-assignment@1
             * @constant
             */
            assignment_schema_version: "role-assignment@1";
            /** Granted At */
            granted_at: string;
            granted_by: components["schemas"]["AuditActor"];
            /** Principal Id */
            principal_id: string;
            /** Revision */
            revision: number;
            /** Revoke Reason */
            revoke_reason?: string | null;
            /** Revoked At */
            revoked_at?: string | null;
            revoked_by?: components["schemas"]["AuditActor"] | null;
            /**
             * Role
             * @enum {string}
             */
            role: "content_designer" | "numeric_designer" | "qa" | "tooling" | "constraint_admin" | "gacha_compliance_reviewer" | "identity_admin";
            /** Scope */
            scope: components["schemas"]["DomainScope"] | "all" | null;
            /**
             * Status
             * @enum {string}
             */
            status: "active" | "revoked";
        };
        /** RollbackDraftRequestV1 */
        RollbackDraftRequestV1: {
            expected_current_ref: components["schemas"]["RefValue"];
            /** Reason */
            reason: string;
            /**
             * Request Schema Version
             * @default rollback-draft-request@1
             * @constant
             */
            request_schema_version: "rollback-draft-request@1";
            /** Reverses Approval Id */
            reverses_approval_id?: string | null;
            rollback_profile: components["schemas"]["ProfileRefV1"];
            /** Target Artifact Id */
            target_artifact_id: string;
            /** Target History Revision */
            target_history_revision: number;
        };
        /**
         * RollbackRequestReadViewV1
         * @description Rollback workflow projection with its stable Artifact identity.
         */
        RollbackRequestReadViewV1: {
            /**
             * Approval Status
             * @enum {string}
             */
            approval_status: "draft" | "validating" | "validation_failed" | "validated" | "pending_approval" | "auto_apply_eligible" | "approved" | "changes_requested" | "rejected" | "applied" | "rolled_back" | "superseded";
            artifact: components["schemas"]["ArtifactSummaryV1"];
            request: components["schemas"]["RollbackRequestV1"];
            /**
             * View Schema Version
             * @default rollback-request-read-view@1
             * @constant
             */
            view_schema_version: "rollback-request-read-view@1";
            /** Workflow Revision */
            workflow_revision: number;
        };
        /** RollbackRequestV1 */
        RollbackRequestV1: {
            expected_current_ref: components["schemas"]["RefValue"];
            /** Reason */
            reason: string;
            /** Ref Name */
            ref_name: string;
            /** Reverses Approval Id */
            reverses_approval_id?: string | null;
            rollback_profile_binding: components["schemas"]["ResolvedExecutionProfileBindingV1"];
            /**
             * Rollback Schema Version
             * @default rollback-request@1
             * @constant
             */
            rollback_schema_version: "rollback-request@1";
            /** Target Artifact Id */
            target_artifact_id: string;
            /** Target History Revision */
            target_history_revision: number;
        };
        /** RollbackTargetBindingV1 */
        RollbackTargetBindingV1: {
            /**
             * Binding Schema Version
             * @default approval-target-binding@1
             * @constant
             */
            binding_schema_version: "approval-target-binding@1";
            expected_ref: components["schemas"]["RefValue"];
            /** Ref Name */
            ref_name: string;
            rollback_profile_binding: components["schemas"]["ResolvedExecutionProfileBindingV1"];
            /**
             * @description discriminator enum property added by openapi-typescript
             * @enum {string}
             */
            subject_kind: "rollback_request";
            /** Target Artifact Id */
            target_artifact_id: string;
            /**
             * Target Artifact Kind
             * @enum {string}
             */
            target_artifact_kind: "source_raw" | "source_rendered" | "ir_snapshot" | "constraint_snapshot" | "constraint_proposal" | "config_export" | "scenario_spec" | "task_suite" | "regression_suite" | "golden_suite" | "bench_dataset" | "benchmark_spec" | "review_report" | "checker_run" | "simulation_run" | "playtest_trace" | "patch" | "validation_evidence" | "regression_evidence" | "rollback_request" | "run_result" | "run_failure" | "cassette_bundle" | "migration_report" | "bench_report" | "operational_evidence";
            /** Target Digest */
            target_digest: string;
            /** Target Snapshot Id */
            target_snapshot_id?: string | null;
        };
        /** RollbackValidationAdmissionRequestV1 */
        RollbackValidationAdmissionRequestV1: {
            /** Approval Id */
            approval_id: string;
            expected_current_ref: components["schemas"]["RefValue"];
            /** Expected Subject Head Revision */
            expected_subject_head_revision: number;
            /** Expected Workflow Revision */
            expected_workflow_revision: number;
            /** Impact Profiles */
            impact_profiles: components["schemas"]["ProfileRefV1"][];
            /** Ref Name */
            ref_name: string;
            /** Regression Suite Artifact Ids */
            regression_suite_artifact_ids: string[];
            /**
             * Request Schema Version
             * @default rollback-validation-admission-request@1
             * @constant
             */
            request_schema_version: "rollback-validation-admission-request@1";
            rollback_profile: components["schemas"]["ProfileRefV1"];
            schema_compatibility_policy: components["schemas"]["ProfileRefV1"];
            /** Seed */
            seed?: number | null;
            /** Subject Digest */
            subject_digest: string;
            /** Target Artifact Id */
            target_artifact_id: string;
            /** Target History Revision */
            target_history_revision: number;
        };
        /** RunAcceptedV1 */
        RunAcceptedV1: {
            /**
             * Accepted Schema Version
             * @default run-accepted@1
             * @constant
             */
            accepted_schema_version: "run-accepted@1";
            /** Events Url */
            events_url: string;
            /** Run Id */
            run_id: string;
            /** Status Url */
            status_url: string;
        };
        /** RunCommandAckV1 */
        RunCommandAckV1: {
            /**
             * Ack Schema Version
             * @default run-command-ack@1
             * @constant
             */
            ack_schema_version: "run-command-ack@1";
            /** Client Id */
            client_id: string;
            /** Client Seq */
            client_seq: number;
            /** Command Id */
            command_id: string;
            /** Command Revision */
            command_revision: number;
            /**
             * Persisted Status
             * @enum {string}
             */
            persisted_status: "pending" | "claimed" | "applied" | "rejected";
            /** Run Revision */
            run_revision: number;
            /**
             * Status
             * @enum {string}
             */
            status: "accepted" | "duplicate";
        };
        /** RunCommandV1 */
        RunCommandV1: {
            /** Client Id */
            client_id: string;
            /** Client Seq */
            client_seq: number;
            /** Command Id */
            command_id: string;
            /**
             * Command Schema Version
             * @default run-command@1
             * @constant
             */
            command_schema_version: "run-command@1";
            /** Expected Run Revision */
            expected_run_revision: number;
            /** Idempotency Key */
            idempotency_key: string;
            /** Payload */
            payload: components["schemas"]["CancelRunPayloadV1"] | components["schemas"]["PlaytestProvideInputPayloadV1"];
            /**
             * Payload Schema Id
             * @enum {string}
             */
            payload_schema_id: "run-cancel@1" | "playtest-provide-input@1";
            /**
             * Type
             * @enum {string}
             */
            type: "cancel" | "provide_input";
        } & ({
            payload: components["schemas"]["CancelRunPayloadV1"];
            /** @constant */
            payload_schema_id: "run-cancel@1";
            /** @constant */
            type: "cancel";
        } | {
            payload: components["schemas"]["PlaytestProvideInputPayloadV1"];
            /** @constant */
            payload_schema_id: "playtest-provide-input@1";
            /** @constant */
            type: "provide_input";
        });
        /** RunCommandViewV1 */
        RunCommandViewV1: {
            /** Applied At */
            applied_at?: string | null;
            /** Client Id */
            client_id: string;
            /** Client Seq */
            client_seq: number;
            /** Command Id */
            command_id: string;
            /** Created At */
            created_at: string;
            /**
             * Payload Schema Id
             * @enum {string}
             */
            payload_schema_id: "run-cancel@1" | "playtest-provide-input@1";
            /** Rejection Code */
            rejection_code?: string | null;
            /** Result Event Seq */
            result_event_seq?: number | null;
            /** Revision */
            revision: number;
            /** Run Id */
            run_id: string;
            /**
             * Status
             * @enum {string}
             */
            status: "pending" | "claimed" | "applied" | "rejected";
            /**
             * Type
             * @enum {string}
             */
            type: "cancel" | "provide_input";
        };
        /**
         * RunCostViewV1
         * @description Legacy bounded cost view retained for exact wire compatibility.
         */
        RunCostViewV1: {
            budget_set: components["schemas"]["BudgetSetSnapshotV1"];
            /** Next Cursor */
            next_cursor?: string | null;
            /** Run Id */
            run_id: string;
            /** Usage */
            usage: components["schemas"]["CostUsageViewV1"][];
            /**
             * @description discriminator enum property added by openapi-typescript
             * @enum {string}
             */
            view_schema_version: "run-cost-view@1";
        };
        /**
         * RunCostViewV2
         * @description Bounded public cost view with complete Run settlement state.
         */
        RunCostViewV2: {
            budget_set: components["schemas"]["BudgetSetSnapshotV1"];
            /** Next Cursor */
            next_cursor?: string | null;
            /** Run Id */
            run_id: string;
            settlement_summary: components["schemas"]["CostSettlementSummaryV1"];
            /** Usage */
            usage: components["schemas"]["CostUsageViewV1"][];
            /**
             * @description discriminator enum property added by openapi-typescript
             * @enum {string}
             */
            view_schema_version: "run-cost-view@2";
        };
        /**
         * RunFindingLinkViewV1
         * @description Exact immutable Finding revision bound to one producer Run ordinal.
         */
        RunFindingLinkViewV1: {
            /** Attempt No */
            attempt_no: number;
            /** Evidence Artifact Id */
            evidence_artifact_id: string;
            finding: components["schemas"]["FindingRevisionV1"];
            /** Finding Digest */
            finding_digest: string;
            /** Ordinal */
            ordinal: number;
            /** Run Id */
            run_id: string;
            /**
             * View Schema Version
             * @default run-finding-link-view@1
             * @constant
             */
            view_schema_version: "run-finding-link-view@1";
        };
        /** RunKindRef */
        RunKindRef: {
            /** Kind */
            kind: string;
            /** Version */
            version: number;
        };
        /**
         * RunSubmissionRequestV1
         * @description Generic ``POST /runs`` body. Only ``generic_runs_endpoint`` kinds are admitted.
         *
         *     The typed ``params`` exposes exactly the four public generic Run kinds;
         *     resource-only and internal-only discriminators fail at the transport boundary.
         */
        RunSubmissionRequestV1: {
            /** Cassette Artifact Id */
            cassette_artifact_id?: string | null;
            execution_version_plan?: components["schemas"]["ExecutionVersionPlanV1"] | null;
            /**
             * Llm Execution Mode
             * @default not_applicable
             * @enum {string}
             */
            llm_execution_mode: "not_applicable" | "live" | "record" | "replay";
            /** Params */
            params: components["schemas"]["ReviewRunPayloadV1"] | components["schemas"]["CheckerRunPayloadV1"] | components["schemas"]["SimulationRunPayloadV1"] | components["schemas"]["BenchRunPayloadV1"];
            /**
             * Request Schema Version
             * @default run-submission-request@1
             * @constant
             */
            request_schema_version: "run-submission-request@1";
            /** Seed */
            seed?: number | null;
        };
        /** RunViewV1 */
        RunViewV1: {
            /** Attempt No */
            attempt_no?: number | null;
            /** Events Url */
            events_url: string;
            /** Failure Artifact Id */
            failure_artifact_id?: string | null;
            /** Result Artifact Id */
            result_artifact_id?: string | null;
            /** Revision */
            revision: number;
            /** Run Id */
            run_id: string;
            /**
             * Status
             * @enum {string}
             */
            status: "queued" | "leased" | "running" | "retry_wait" | "succeeded" | "failed" | "cancelled" | "timed_out";
            /** Status Url */
            status_url: string;
            /** Terminal Cassette Artifact Id */
            terminal_cassette_artifact_id?: string | null;
            /**
             * View Schema Version
             * @default run-view@1
             * @constant
             */
            view_schema_version: "run-view@1";
        };
        /** ScalarMetricSampleV1 */
        ScalarMetricSampleV1: {
            /**
             * Ts Utc
             * Format: date-time
             */
            ts_utc: string;
            /** Value */
            value: number;
        };
        /** ScenarioResetBindingV1 */
        ScenarioResetBindingV1: {
            payload: components["schemas"]["JsonValue"];
            /** Payload Hash */
            payload_hash: string;
            /** Reset Schema Id */
            reset_schema_id: string;
        };
        /** SchemaRegistryDocumentV1 */
        SchemaRegistryDocumentV1: {
            /** Registry Digest */
            registry_digest: string;
            /**
             * Registry Schema Version
             * @default schema-registry-document@1
             * @constant
             */
            registry_schema_version: "schema-registry-document@1";
            /** Registry Version */
            registry_version: string;
            /** Schemas */
            schemas: {
                [key: string]: components["schemas"]["JsonValue"];
            };
        };
        /** Selector */
        Selector: {
            /** Node Type */
            node_type: string;
            /** Var */
            var: string;
            /** Where */
            where?: {
                [key: string]: unknown;
            };
        };
        /** SimulationRunPayloadV1 */
        SimulationRunPayloadV1: {
            /** Constraint Snapshot Artifact Id */
            constraint_snapshot_artifact_id?: string | null;
            /** Horizon Steps */
            horizon_steps: number;
            /** Replication Count */
            replication_count: number;
            /** Scenario Artifact Id */
            scenario_artifact_id?: string | null;
            /**
             * @description discriminator enum property added by openapi-typescript
             * @enum {string}
             */
            schema_version: "simulation-run@1";
            simulation_profile: components["schemas"]["ProfileRefV1"];
            /** Snapshot Artifact Id */
            snapshot_artifact_id: string;
            workload_profile: components["schemas"]["ProfileRefV1"];
        };
        /** SnapshotDiff */
        SnapshotDiff: {
            /** Base Snapshot Id */
            base_snapshot_id: string;
            /**
             * Diff Schema Version
             * @default snapshot-diff@1
             * @constant
             */
            diff_schema_version: "snapshot-diff@1";
            /** Entry Count */
            entry_count: number;
            /** Target Snapshot Id */
            target_snapshot_id: string;
        };
        /** SnapshotDiffEntry */
        SnapshotDiffEntry: {
            after: components["schemas"]["JsonValueState"];
            before: components["schemas"]["JsonValueState"];
            /** Path */
            path: string;
        };
        /** SnapshotDiffHttpPageV1 */
        SnapshotDiffHttpPageV1: {
            diff: components["schemas"]["SnapshotDiff"];
            page: components["schemas"]["OpaquePageV1_SnapshotDiffEntry_"];
            /**
             * Page Schema Version
             * @default snapshot-diff-http-page@1
             * @constant
             */
            page_schema_version: "snapshot-diff-http-page@1";
        };
        /** SolverEngineRefV1 */
        SolverEngineRefV1: {
            /** Engine Id */
            engine_id: string;
            /** Version */
            version: number;
        };
        /**
         * SourceRef
         * @description round-trip + minimal-repro provenance (contract §2.1).
         */
        SourceRef: {
            /** Adapter */
            adapter: string;
            /** Column */
            column?: string | null;
            /** File */
            file: string;
            /** Row */
            row?: number | null;
            /** Sheet */
            sheet?: string | null;
        };
        /** SpanDataV1 */
        SpanDataV1: {
            /** Attributes */
            attributes: {
                [key: string]: components["schemas"]["JsonValue"];
            };
            /** Duration Ns */
            duration_ns: number;
            /**
             * Ended At
             * Format: date-time
             */
            ended_at: string;
            error: components["schemas"]["SpanErrorV1"] | null;
            /** Events */
            events: components["schemas"]["SpanEventV1"][];
            /** Links */
            links: components["schemas"]["SpanLinkV1"][];
            /** Name */
            name: string;
            /** Parent Span Id */
            parent_span_id: string | null;
            /** Resource */
            resource: {
                [key: string]: components["schemas"]["JsonValue"];
            };
            /** Span Id */
            span_id: string;
            /**
             * Span Schema Version
             * @default span-data@1
             * @constant
             */
            span_schema_version: "span-data@1";
            /**
             * Started At
             * Format: date-time
             */
            started_at: string;
            /**
             * Status
             * @enum {string}
             */
            status: "unset" | "ok" | "error";
            /** Trace Id */
            trace_id: string;
        };
        /** SpanErrorV1 */
        SpanErrorV1: {
            /** Error Type */
            error_type: string;
            /** Message */
            message: string;
            /** Stack Fingerprint */
            stack_fingerprint?: string | null;
        };
        /** SpanEventV1 */
        SpanEventV1: {
            /** Attributes */
            attributes?: {
                [key: string]: components["schemas"]["JsonValue"];
            };
            /** Name */
            name: string;
            /**
             * Occurred At
             * Format: date-time
             */
            occurred_at: string;
        };
        /** SpanLinkV1 */
        SpanLinkV1: {
            /** Attributes */
            attributes?: {
                [key: string]: components["schemas"]["JsonValue"];
            };
            context: components["schemas"]["TraceContextV1"];
        };
        /** SpanPageV1 */
        SpanPageV1: {
            /** Items */
            items: components["schemas"]["SpanViewV1"][];
            /** Next Cursor */
            next_cursor?: string | null;
            /**
             * Page Schema Version
             * @default span-page@1
             * @constant
             */
            page_schema_version: "span-page@1";
            /** Trace Id */
            trace_id: string;
            /** Truncated */
            truncated: boolean;
        };
        /** SpanViewV1 */
        SpanViewV1: {
            /**
             * Redacted Attribute Keys
             * @default []
             */
            redacted_attribute_keys: string[];
            /**
             * Redacted Event Fields
             * @default []
             */
            redacted_event_fields: string[];
            span: components["schemas"]["SpanDataV1"];
        };
        /** SpecViewV1 */
        SpecViewV1: {
            artifact: components["schemas"]["ArtifactSummaryV1"];
            /** Ref Name */
            ref_name?: string | null;
            ref_value?: components["schemas"]["RefValue"] | null;
            /** Schema Registry Version */
            schema_registry_version: string;
            /** Snapshot Id */
            snapshot_id: string;
            /**
             * View Schema Version
             * @default spec-view@1
             * @constant
             */
            view_schema_version: "spec-view@1";
        };
        /**
         * SubjectApprovalBindingViewV1
         * @description Exact retained workflow binding for one immutable subject Artifact.
         */
        SubjectApprovalBindingViewV1: {
            /** Approval Id */
            approval_id: string;
            /**
             * Approval Status
             * @enum {string}
             */
            approval_status: "draft" | "validating" | "validation_failed" | "validated" | "pending_approval" | "auto_apply_eligible" | "approved" | "changes_requested" | "rejected" | "applied" | "rolled_back" | "superseded";
            /** Is Current Head */
            is_current_head: boolean;
            /** Subject Artifact Id */
            subject_artifact_id: string;
            /** Subject Digest */
            subject_digest: string;
            /** Subject Head Revision */
            subject_head_revision: number;
            /**
             * Subject Kind
             * @enum {string}
             */
            subject_kind: "patch" | "constraint_proposal" | "rollback_request";
            /** Subject Revision */
            subject_revision: number;
            /** Subject Series Id */
            subject_series_id: string;
            /** Workflow Revision */
            workflow_revision: number;
        };
        /** SubmitForApprovalRequestV1 */
        SubmitForApprovalRequestV1: {
            /** Approval Id */
            approval_id: string;
            /** Expected Workflow Revision */
            expected_workflow_revision: number;
            /**
             * Request Schema Version
             * @default submit-for-approval-request@1
             * @constant
             */
            request_schema_version: "submit-for-approval-request@1";
        };
        /** TaskEpisodeV1 */
        TaskEpisodeV1: {
            completion_oracle: components["schemas"]["CompletionOracleRefV1"];
            domain_scope: components["schemas"]["DomainScope"];
            /** Episode Id */
            episode_id: string;
            reset_binding: components["schemas"]["ScenarioResetBindingV1"];
            /** Scenario Spec Artifact Id */
            scenario_spec_artifact_id: string;
            /** Step Budget */
            step_budget: number;
        };
        /** TaskSuiteArtifactViewV1 */
        TaskSuiteArtifactViewV1: {
            artifact: components["schemas"]["ArtifactSummaryV1"];
            task_suite: components["schemas"]["TaskSuiteV1"];
            /**
             * View Schema Version
             * @default task-suite-artifact-view@1
             * @constant
             */
            view_schema_version: "task-suite-artifact-view@1";
        };
        /**
         * TaskSuiteDerivationBindingViewV1
         * @description Browser-safe complete authority for one task-suite derivation profile.
         */
        TaskSuiteDerivationBindingViewV1: {
            /**
             * Binding Schema Version
             * @default task-suite-derivation-binding@1
             * @constant
             */
            binding_schema_version: "task-suite-derivation-binding@1";
            completion_oracle_registry_ref: components["schemas"]["CompletionOracleRegistryRefV1"];
            derivation_profile: components["schemas"]["ProfileRefV1"];
            /** Max Scenarios */
            max_scenarios: number;
            /** Max Total Prepared Artifact Bytes */
            max_total_prepared_artifact_bytes: number;
            /** Profile Payload Hash */
            profile_payload_hash: string;
            run_kind: components["schemas"]["RunKindRef"];
            target_environment_profile: components["schemas"]["ProfileRefV1"];
        };
        /** TaskSuiteDerivePayloadV1 */
        TaskSuiteDerivePayloadV1: {
            completion_oracle_registry_ref: components["schemas"]["CompletionOracleRegistryRefV1"];
            /** Config Artifact Id */
            config_artifact_id: string;
            /** Constraint Snapshot Artifact Id */
            constraint_snapshot_artifact_id: string;
            derivation_profile: components["schemas"]["ProfileRefV1"];
            environment_profile: components["schemas"]["ProfileRefV1"];
            /**
             * Schema Version
             * @default task-suite-derive@1
             * @constant
             */
            schema_version: "task-suite-derive@1";
            /** Source Preview Artifact Id */
            source_preview_artifact_id: string;
        };
        /**
         * TaskSuiteDeriveRequestV1
         * @description ``POST /task-suites:derive`` — fixes ``task_suite.derive@1`` via typed params.
         */
        TaskSuiteDeriveRequestV1: {
            params: components["schemas"]["TaskSuiteDerivePayloadV1"];
            /**
             * Request Schema Version
             * @default task-suite-derive-request@1
             * @constant
             */
            request_schema_version: "task-suite-derive-request@1";
        };
        /** TaskSuiteV1 */
        TaskSuiteV1: {
            completion_oracle_registry_ref: components["schemas"]["CompletionOracleRegistryRefV1"];
            /** Config Export Artifact Id */
            config_export_artifact_id: string;
            /** Constraint Snapshot Artifact Id */
            constraint_snapshot_artifact_id: string;
            /** Env Contract Version */
            env_contract_version: string;
            environment_profile: components["schemas"]["ProfileRefV1"];
            /** Episodes */
            episodes: components["schemas"]["TaskEpisodeV1"][];
            /** Source Preview Artifact Id */
            source_preview_artifact_id: string;
            suite_profile: components["schemas"]["ProfileRefV1"];
            /**
             * Task Suite Schema Version
             * @default task-suite@1
             * @constant
             */
            task_suite_schema_version: "task-suite@1";
        };
        /** TokenTotals */
        TokenTotals: {
            /** Cache Read Tokens */
            cache_read_tokens: number;
            /** Cache Write Tokens */
            cache_write_tokens: number;
            /** Input Tokens */
            input_tokens: number;
            /** Output Tokens */
            output_tokens: number;
            /** Reported Total Tokens */
            reported_total_tokens: number;
        };
        /** TokenUsageObservationV1 */
        TokenUsageObservationV1: {
            /** Cache Read Tokens */
            cache_read_tokens?: number | null;
            /** Cache Write Tokens */
            cache_write_tokens?: number | null;
            /** Input Tokens */
            input_tokens?: number | null;
            /**
             * Observation Schema Version
             * @default token-usage-observation@1
             * @constant
             */
            observation_schema_version: "token-usage-observation@1";
            /** Output Tokens */
            output_tokens?: number | null;
            /**
             * Status
             * @enum {string}
             */
            status: "reported" | "unavailable";
            /** Total Tokens */
            total_tokens?: number | null;
        };
        /** TraceContextV1 */
        TraceContextV1: {
            /**
             * Context Schema Version
             * @default trace-context@1
             * @constant
             */
            context_schema_version: "trace-context@1";
            /** Span Id */
            span_id: string;
            /** Trace Flags */
            trace_flags: string;
            /** Trace Id */
            trace_id: string;
            /** Trace State */
            trace_state?: string | null;
        };
        /** TraceSummaryPageV1 */
        TraceSummaryPageV1: {
            /**
             * Coverage End
             * Format: date-time
             */
            coverage_end: string;
            /**
             * Coverage Start
             * Format: date-time
             */
            coverage_start: string;
            /** Items */
            items: components["schemas"]["TraceSummaryV1"][];
            /** Next Cursor */
            next_cursor?: string | null;
            /**
             * Page Schema Version
             * @default trace-summary-page@1
             * @constant
             */
            page_schema_version: "trace-summary-page@1";
            /** Truncated */
            truncated: boolean;
        };
        /** TraceSummaryV1 */
        TraceSummaryV1: {
            /** Duration Ns */
            duration_ns?: number | null;
            /** Ended At */
            ended_at?: string | null;
            /** Root Span Id */
            root_span_id?: string | null;
            /** Run Ids */
            run_ids: string[];
            /** Service Names */
            service_names: string[];
            /** Span Count */
            span_count: number;
            /**
             * Started At
             * Format: date-time
             */
            started_at: string;
            /**
             * Status
             * @enum {string}
             */
            status: "unset" | "ok" | "error";
            /** Trace Id */
            trace_id: string;
            /**
             * Trace Schema Version
             * @default trace-summary@1
             * @constant
             */
            trace_schema_version: "trace-summary@1";
            /** Truncated */
            truncated: boolean;
        };
        /** TypedOp */
        TypedOp: {
            /** New Value */
            new_value?: unknown | null;
            /** Old Value */
            old_value?: unknown | null;
            /**
             * Op
             * @enum {string}
             */
            op: "add_entity" | "delete_entity" | "set_entity_attr" | "add_relation" | "delete_relation" | "set_relation_attr" | "replace_subgraph";
            /** Op Id */
            op_id: string;
            /** Source Ref */
            source_ref?: {
                [key: string]: unknown;
            } | null;
            /** Target */
            target: string;
        };
        /** VersionRef */
        VersionRef: {
            /** Component */
            component: string;
            /** Sha256 */
            sha256?: string | null;
            /** Version */
            version: string;
        };
        /**
         * VersionTuple
         * @description The frozen ten-field tuple; optional means not applicable, not unknown.
         */
        VersionTuple: {
            /** Agent Graph Version */
            agent_graph_version?: string | null;
            /** Cassette Id */
            cassette_id?: string | null;
            /** Constraint Snapshot Id */
            constraint_snapshot_id?: string | null;
            /** Doc Version */
            doc_version?: string | null;
            /** Env Contract Version */
            env_contract_version?: string | null;
            /** Ir Snapshot Id */
            ir_snapshot_id?: string | null;
            /** Model Snapshot */
            model_snapshot?: string | null;
            /** Prompt Version */
            prompt_version?: string | null;
            /** Seed */
            seed?: number | null;
            /** Tool Version */
            tool_version?: string | null;
        };
        /** WorkflowApplyRequestV1 */
        WorkflowApplyRequestV1: {
            /** Approval Id */
            approval_id: string;
            expected_ref: components["schemas"]["RefValue"] | null;
            /** Expected Workflow Revision */
            expected_workflow_revision: number;
            /** Ref Name */
            ref_name: string;
            /**
             * Request Schema Version
             * @default workflow-apply-request@1
             * @constant
             */
            request_schema_version: "workflow-apply-request@1";
            /** Subject Digest */
            subject_digest: string;
            /** Target Artifact Id */
            target_artifact_id: string;
            /** Target Digest */
            target_digest: string;
        };
        /** WorkflowApplyResultV1 */
        WorkflowApplyResultV1: {
            approval: components["schemas"]["ApprovalViewV1"];
            /** Ref Name */
            ref_name: string;
            /** Ref Transition Id */
            ref_transition_id?: string | null;
            ref_value: components["schemas"]["RefValue"];
            /**
             * Result Schema Version
             * @default workflow-apply-result@1
             * @constant
             */
            result_schema_version: "workflow-apply-result@1";
            /** Reversed Approval Id */
            reversed_approval_id?: string | null;
        };
        /** _CustomResolution */
        _CustomResolution: {
            /**
             * @description discriminator enum property added by openapi-typescript
             * @enum {string}
             */
            choice: "custom";
            /** Conflict Id */
            conflict_id: string;
            custom_value: components["schemas"]["JsonValue"];
        };
        /** _KeepCurrentResolution */
        _KeepCurrentResolution: {
            /**
             * @description discriminator enum property added by openapi-typescript
             * @enum {string}
             */
            choice: "keep_current";
            /** Conflict Id */
            conflict_id: string;
        };
        /** _MissingJsonValueState */
        _MissingJsonValueState: {
            /**
             * @description discriminator enum property added by openapi-typescript
             * @enum {string}
             */
            presence: "missing";
        };
        /** _PresentJsonValueState */
        _PresentJsonValueState: {
            /**
             * @description discriminator enum property added by openapi-typescript
             * @enum {string}
             */
            presence: "present";
            value: components["schemas"]["JsonValue"];
        };
        /** _TakeProposedResolution */
        _TakeProposedResolution: {
            /**
             * @description discriminator enum property added by openapi-typescript
             * @enum {string}
             */
            choice: "take_proposed";
            /** Conflict Id */
            conflict_id: string;
        };
    };
    responses: never;
    parameters: never;
    requestBodies: never;
    headers: never;
    pathItems: never;
}
export type $defs = Record<string, never>;
export interface operations {
    approvals_api_v1_approvals_get: {
        parameters: {
            query?: {
                assignee?: "me" | null;
                cursor?: string | null;
                limit?: number;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    /** @description Caching directive; always `private, no-cache`. */
                    "Cache-Control"?: string;
                    /** @description Strong entity tag bound to the read snapshot of this page. */
                    ETag?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["OpaquePageV1_ApprovalViewV1_"];
                };
            };
            /** @description Invalid cursor or malformed request (problem+json). */
            400: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Authentication is required or failed (problem+json). */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Forbidden: RBAC/CSRF/Origin rejected the request (problem+json). */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The resume cursor is no longer retained (problem+json). */
            410: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request does not match the required schema or is too broad (problem+json). */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A configured quota was exceeded (problem+json). */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A sanitized internal error (problem+json). */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A required dependency is unavailable (problem+json). */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description An unexpected error rendered as RFC 9457 problem+json. */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
        };
    };
    approval_api_v1_approvals__approval_id__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                approval_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    /** @description Caching directive; always `private, no-cache` for resources. */
                    "Cache-Control"?: string;
                    /** @description Strong entity tag of the resource for If-Match optimistic concurrency. */
                    ETag?: string;
                    /** @description The resource's monotonic integer revision. */
                    "X-Resource-Revision"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ApprovalViewV1"];
                };
            };
            /** @description Authentication is required or failed (problem+json). */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Forbidden: RBAC/CSRF/Origin rejected the request (problem+json). */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The requested resource was not found (problem+json). */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request does not match the required schema or is too broad (problem+json). */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A configured quota was exceeded (problem+json). */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A sanitized internal error (problem+json). */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A required dependency is unavailable (problem+json). */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description An unexpected error rendered as RFC 9457 problem+json. */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
        };
    };
    approve_api_v1_approvals__approval_id__approve_post: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": string;
                "If-Match": string;
                /** @description Session-bound CSRF token from login. Required when authenticating with the session cookie with a non-safe HTTP method, including a read-only POST resolver; ignored for ApiKey service clients. */
                "X-CSRF-Token"?: string;
            };
            path: {
                approval_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["ApprovalDecisionRequestV1"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    /** @description Caching directive; always `private, no-cache` for resources. */
                    "Cache-Control"?: string;
                    /** @description Strong entity tag of the resource for If-Match optimistic concurrency. */
                    ETag?: string;
                    /** @description The resource's monotonic integer revision. */
                    "X-Resource-Revision"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ApprovalViewV1"];
                };
            };
            /** @description Invalid cursor or malformed request (problem+json). */
            400: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Authentication is required or failed (problem+json). */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Forbidden: RBAC/CSRF/Origin rejected the request (problem+json). */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The requested resource was not found (problem+json). */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Conflict: revision/idempotency/workflow-guard/precondition (problem+json). */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The resume cursor is no longer retained (problem+json). */
            410: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request payload exceeds its bound (problem+json). */
            413: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request does not match the required schema or is too broad (problem+json). */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A configured quota was exceeded (problem+json). */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A sanitized internal error (problem+json). */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A required dependency is unavailable (problem+json). */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description An unexpected error rendered as RFC 9457 problem+json. */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
        };
    };
    reject_api_v1_approvals__approval_id__reject_post: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": string;
                "If-Match": string;
                /** @description Session-bound CSRF token from login. Required when authenticating with the session cookie with a non-safe HTTP method, including a read-only POST resolver; ignored for ApiKey service clients. */
                "X-CSRF-Token"?: string;
            };
            path: {
                approval_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["ApprovalDecisionRequestV1"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    /** @description Caching directive; always `private, no-cache` for resources. */
                    "Cache-Control"?: string;
                    /** @description Strong entity tag of the resource for If-Match optimistic concurrency. */
                    ETag?: string;
                    /** @description The resource's monotonic integer revision. */
                    "X-Resource-Revision"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ApprovalViewV1"];
                };
            };
            /** @description Invalid cursor or malformed request (problem+json). */
            400: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Authentication is required or failed (problem+json). */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Forbidden: RBAC/CSRF/Origin rejected the request (problem+json). */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The requested resource was not found (problem+json). */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Conflict: revision/idempotency/workflow-guard/precondition (problem+json). */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The resume cursor is no longer retained (problem+json). */
            410: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request payload exceeds its bound (problem+json). */
            413: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request does not match the required schema or is too broad (problem+json). */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A configured quota was exceeded (problem+json). */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A sanitized internal error (problem+json). */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A required dependency is unavailable (problem+json). */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description An unexpected error rendered as RFC 9457 problem+json. */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
        };
    };
    request_changes_api_v1_approvals__approval_id__request_changes_post: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": string;
                "If-Match": string;
                /** @description Session-bound CSRF token from login. Required when authenticating with the session cookie with a non-safe HTTP method, including a read-only POST resolver; ignored for ApiKey service clients. */
                "X-CSRF-Token"?: string;
            };
            path: {
                approval_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["ApprovalDecisionRequestV1"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    /** @description Caching directive; always `private, no-cache` for resources. */
                    "Cache-Control"?: string;
                    /** @description Strong entity tag of the resource for If-Match optimistic concurrency. */
                    ETag?: string;
                    /** @description The resource's monotonic integer revision. */
                    "X-Resource-Revision"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ApprovalViewV1"];
                };
            };
            /** @description Invalid cursor or malformed request (problem+json). */
            400: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Authentication is required or failed (problem+json). */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Forbidden: RBAC/CSRF/Origin rejected the request (problem+json). */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The requested resource was not found (problem+json). */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Conflict: revision/idempotency/workflow-guard/precondition (problem+json). */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The resume cursor is no longer retained (problem+json). */
            410: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request payload exceeds its bound (problem+json). */
            413: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request does not match the required schema or is too broad (problem+json). */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A configured quota was exceeded (problem+json). */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A sanitized internal error (problem+json). */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A required dependency is unavailable (problem+json). */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description An unexpected error rendered as RFC 9457 problem+json. */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
        };
    };
    artifact_api_v1_artifacts__artifact_id__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                artifact_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    /** @description Caching directive; always `private, no-cache` for resources. */
                    "Cache-Control"?: string;
                    /** @description Strong entity tag of the resource for If-Match optimistic concurrency. */
                    ETag?: string;
                    /** @description The resource's monotonic integer revision. */
                    "X-Resource-Revision"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ArtifactPayloadViewV1"];
                };
            };
            /** @description Authentication is required or failed (problem+json). */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Forbidden: RBAC/CSRF/Origin rejected the request (problem+json). */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The requested resource was not found (problem+json). */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request does not match the required schema or is too broad (problem+json). */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A configured quota was exceeded (problem+json). */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A sanitized internal error (problem+json). */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A required dependency is unavailable (problem+json). */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description An unexpected error rendered as RFC 9457 problem+json. */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
        };
    };
    lineage_api_v1_artifacts__artifact_id__lineage_get: {
        parameters: {
            query?: {
                cursor?: string | null;
                limit?: number;
            };
            header?: never;
            path: {
                artifact_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    /** @description Caching directive; always `private, no-cache`. */
                    "Cache-Control"?: string;
                    /** @description Strong entity tag bound to the read snapshot of this page. */
                    ETag?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["OpaquePageV1_LineageEntryV1_"];
                };
            };
            /** @description Invalid cursor or malformed request (problem+json). */
            400: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Authentication is required or failed (problem+json). */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Forbidden: RBAC/CSRF/Origin rejected the request (problem+json). */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The requested resource was not found (problem+json). */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The resume cursor is no longer retained (problem+json). */
            410: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request does not match the required schema or is too broad (problem+json). */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A configured quota was exceeded (problem+json). */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A sanitized internal error (problem+json). */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A required dependency is unavailable (problem+json). */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description An unexpected error rendered as RFC 9457 problem+json. */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
        };
    };
    login_api_v1_auth_login_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["PasswordAuthRequestV1"];
            };
        };
        responses: {
            /** @description Successful Response */
            204: {
                headers: {
                    /** @description Always `no-store` for the authentication response. */
                    "Cache-Control"?: string;
                    /** @description Sets the `gameforge_session` session cookie (HttpOnly, Secure, SameSite=Strict, Path=/). */
                    "Set-Cookie"?: string;
                    /** @description Session-bound CSRF token to echo on mutating requests. */
                    "X-CSRF-Token"?: string;
                    [name: string]: unknown;
                };
                content?: never;
            };
            /** @description Invalid cursor or malformed request (problem+json). */
            400: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Authentication is required or failed (problem+json). */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Forbidden: RBAC/CSRF/Origin rejected the request (problem+json). */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request payload exceeds its bound (problem+json). */
            413: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request does not match the required schema or is too broad (problem+json). */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A configured quota was exceeded (problem+json). */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A sanitized internal error (problem+json). */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A required dependency is unavailable (problem+json). */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description An unexpected error rendered as RFC 9457 problem+json. */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
        };
    };
    logout_api_v1_auth_logout_post: {
        parameters: {
            query?: never;
            header: {
                /** @description Session-bound CSRF token returned by login; required for logout. */
                "X-CSRF-Token": string;
                /** @description Bounded idempotency key for exact command replay. */
                "Idempotency-Key": string;
            };
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            204: {
                headers: {
                    /** @description Always `no-store` for the authentication response. */
                    "Cache-Control"?: string;
                    /** @description Clears the `gameforge_session` session cookie. */
                    "Set-Cookie"?: string;
                    [name: string]: unknown;
                };
                content?: never;
            };
            /** @description Authentication is required or failed (problem+json). */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Forbidden: RBAC/CSRF/Origin rejected the request (problem+json). */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request does not match the required schema or is too broad (problem+json). */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A configured quota was exceeded (problem+json). */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A sanitized internal error (problem+json). */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A required dependency is unavailable (problem+json). */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description An unexpected error rendered as RFC 9457 problem+json. */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
        };
    };
    me_api_v1_auth_me_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    /** @description Always `no-store`. */
                    "Cache-Control"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["Principal"];
                };
            };
            /** @description Authentication is required or failed (problem+json). */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Forbidden: RBAC/CSRF/Origin rejected the request (problem+json). */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A configured quota was exceeded (problem+json). */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A sanitized internal error (problem+json). */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A required dependency is unavailable (problem+json). */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description An unexpected error rendered as RFC 9457 problem+json. */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
        };
    };
    bench_report_api_v1_bench_report_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    /** @description Caching directive; always `private, no-cache` for resources. */
                    "Cache-Control"?: string;
                    /** @description Strong entity tag of the resource for If-Match optimistic concurrency. */
                    ETag?: string;
                    /** @description Exact selected BenchReport Artifact ID; use it with `/api/v1/artifacts/{artifact_id}` for provenance and lineage. */
                    "X-Artifact-ID"?: string;
                    /** @description The resource's monotonic integer revision. */
                    "X-Resource-Revision"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BenchReport"];
                };
            };
            /** @description Authentication is required or failed (problem+json). */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Forbidden: RBAC/CSRF/Origin rejected the request (problem+json). */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The requested resource was not found (problem+json). */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request does not match the required schema or is too broad (problem+json). */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A configured quota was exceeded (problem+json). */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A sanitized internal error (problem+json). */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A required dependency is unavailable (problem+json). */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description An unexpected error rendered as RFC 9457 problem+json. */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
        };
    };
    conflicts_api_v1_conflict_sets__conflict_set_id__conflicts_get: {
        parameters: {
            query?: {
                cursor?: string | null;
                limit?: number;
            };
            header?: never;
            path: {
                conflict_set_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    /** @description Caching directive; always `private, no-cache`. */
                    "Cache-Control"?: string;
                    /** @description Strong entity tag bound to the read snapshot of this page. */
                    ETag?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["OpaquePageV1_MergeConflict_"];
                };
            };
            /** @description Invalid cursor or malformed request (problem+json). */
            400: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Authentication is required or failed (problem+json). */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Forbidden: RBAC/CSRF/Origin rejected the request (problem+json). */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The requested resource was not found (problem+json). */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The resume cursor is no longer retained (problem+json). */
            410: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request does not match the required schema or is too broad (problem+json). */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A configured quota was exceeded (problem+json). */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A sanitized internal error (problem+json). */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A required dependency is unavailable (problem+json). */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description An unexpected error rendered as RFC 9457 problem+json. */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
        };
    };
    constraint_proposals_api_v1_constraint_proposals_get: {
        parameters: {
            query?: {
                cursor?: string | null;
                limit?: number;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    /** @description Caching directive; always `private, no-cache`. */
                    "Cache-Control"?: string;
                    /** @description Strong entity tag bound to the read snapshot of this page. */
                    ETag?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["OpaquePageV1_ConstraintProposalReadViewV1_"];
                };
            };
            /** @description Invalid cursor or malformed request (problem+json). */
            400: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Authentication is required or failed (problem+json). */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Forbidden: RBAC/CSRF/Origin rejected the request (problem+json). */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The resume cursor is no longer retained (problem+json). */
            410: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request does not match the required schema or is too broad (problem+json). */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A configured quota was exceeded (problem+json). */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A sanitized internal error (problem+json). */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A required dependency is unavailable (problem+json). */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description An unexpected error rendered as RFC 9457 problem+json. */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
        };
    };
    draft_constraint_api_v1_constraint_proposals_post: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": string;
                /** @description Session-bound CSRF token from login. Required when authenticating with the session cookie with a non-safe HTTP method, including a read-only POST resolver; ignored for ApiKey service clients. */
                "X-CSRF-Token"?: string;
            };
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["HumanConstraintDraftRequestV1"];
            };
        };
        responses: {
            /** @description Successful Response */
            201: {
                headers: {
                    /** @description Caching directive; always `private, no-cache` for resources. */
                    "Cache-Control"?: string;
                    /** @description Strong entity tag of the resource for If-Match optimistic concurrency. */
                    ETag?: string;
                    /** @description The resource's monotonic integer revision. */
                    "X-Resource-Revision"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ConstraintProposalReadViewV1"];
                };
            };
            /** @description Invalid cursor or malformed request (problem+json). */
            400: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Authentication is required or failed (problem+json). */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Forbidden: RBAC/CSRF/Origin rejected the request (problem+json). */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The requested resource was not found (problem+json). */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Conflict: revision/idempotency/workflow-guard/precondition (problem+json). */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The resume cursor is no longer retained (problem+json). */
            410: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request payload exceeds its bound (problem+json). */
            413: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request does not match the required schema or is too broad (problem+json). */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A configured quota was exceeded (problem+json). */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A sanitized internal error (problem+json). */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A required dependency is unavailable (problem+json). */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description An unexpected error rendered as RFC 9457 problem+json. */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
        };
    };
    constraint_proposal_api_v1_constraint_proposals__artifact_id__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                artifact_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    /** @description Caching directive; always `private, no-cache` for resources. */
                    "Cache-Control"?: string;
                    /** @description Strong entity tag of the resource for If-Match optimistic concurrency. */
                    ETag?: string;
                    /** @description The resource's monotonic integer revision. */
                    "X-Resource-Revision"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ConstraintProposalReadViewV1"];
                };
            };
            /** @description Authentication is required or failed (problem+json). */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Forbidden: RBAC/CSRF/Origin rejected the request (problem+json). */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The requested resource was not found (problem+json). */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request does not match the required schema or is too broad (problem+json). */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A configured quota was exceeded (problem+json). */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A sanitized internal error (problem+json). */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A required dependency is unavailable (problem+json). */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description An unexpected error rendered as RFC 9457 problem+json. */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
        };
    };
    publish_constraint_api_v1_constraint_proposals__artifact_id__publish_post: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": string;
                "If-Match": string;
                /** @description Session-bound CSRF token from login. Required when authenticating with the session cookie with a non-safe HTTP method, including a read-only POST resolver; ignored for ApiKey service clients. */
                "X-CSRF-Token"?: string;
            };
            path: {
                artifact_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["WorkflowApplyRequestV1"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    /** @description Caching directive; always `private, no-cache` for resources. */
                    "Cache-Control"?: string;
                    /** @description Strong entity tag of the resource for If-Match optimistic concurrency. */
                    ETag?: string;
                    /** @description The resource's monotonic integer revision. */
                    "X-Resource-Revision"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["WorkflowApplyResultV1"];
                };
            };
            /** @description Invalid cursor or malformed request (problem+json). */
            400: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Authentication is required or failed (problem+json). */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Forbidden: RBAC/CSRF/Origin rejected the request (problem+json). */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The requested resource was not found (problem+json). */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Conflict: revision/idempotency/workflow-guard/precondition (problem+json). */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The resume cursor is no longer retained (problem+json). */
            410: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request payload exceeds its bound (problem+json). */
            413: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request does not match the required schema or is too broad (problem+json). */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A configured quota was exceeded (problem+json). */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A sanitized internal error (problem+json). */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A required dependency is unavailable (problem+json). */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description An unexpected error rendered as RFC 9457 problem+json. */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
        };
    };
    revise_constraint_api_v1_constraint_proposals__artifact_id__revise_post: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": string;
                "If-Match": string;
                /** @description Session-bound CSRF token from login. Required when authenticating with the session cookie with a non-safe HTTP method, including a read-only POST resolver; ignored for ApiKey service clients. */
                "X-CSRF-Token"?: string;
            };
            path: {
                artifact_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["HumanConstraintRevisionRequestV1"];
            };
        };
        responses: {
            /** @description Successful Response */
            201: {
                headers: {
                    /** @description Caching directive; always `private, no-cache` for resources. */
                    "Cache-Control"?: string;
                    /** @description Strong entity tag of the resource for If-Match optimistic concurrency. */
                    ETag?: string;
                    /** @description The resource's monotonic integer revision. */
                    "X-Resource-Revision"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ConstraintProposalReadViewV1"];
                };
            };
            /** @description Invalid cursor or malformed request (problem+json). */
            400: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Authentication is required or failed (problem+json). */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Forbidden: RBAC/CSRF/Origin rejected the request (problem+json). */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The requested resource was not found (problem+json). */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Conflict: revision/idempotency/workflow-guard/precondition (problem+json). */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The resume cursor is no longer retained (problem+json). */
            410: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request payload exceeds its bound (problem+json). */
            413: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request does not match the required schema or is too broad (problem+json). */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A configured quota was exceeded (problem+json). */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A sanitized internal error (problem+json). */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A required dependency is unavailable (problem+json). */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description An unexpected error rendered as RFC 9457 problem+json. */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
        };
    };
    submit_constraint_api_v1_constraint_proposals__artifact_id__submit_for_approval_post: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": string;
                "If-Match": string;
                /** @description Session-bound CSRF token from login. Required when authenticating with the session cookie with a non-safe HTTP method, including a read-only POST resolver; ignored for ApiKey service clients. */
                "X-CSRF-Token"?: string;
            };
            path: {
                artifact_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["SubmitForApprovalRequestV1"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    /** @description Caching directive; always `private, no-cache` for resources. */
                    "Cache-Control"?: string;
                    /** @description Strong entity tag of the resource for If-Match optimistic concurrency. */
                    ETag?: string;
                    /** @description The resource's monotonic integer revision. */
                    "X-Resource-Revision"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ApprovalViewV1"];
                };
            };
            /** @description Invalid cursor or malformed request (problem+json). */
            400: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Authentication is required or failed (problem+json). */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Forbidden: RBAC/CSRF/Origin rejected the request (problem+json). */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The requested resource was not found (problem+json). */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Conflict: revision/idempotency/workflow-guard/precondition (problem+json). */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The resume cursor is no longer retained (problem+json). */
            410: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request payload exceeds its bound (problem+json). */
            413: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request does not match the required schema or is too broad (problem+json). */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A configured quota was exceeded (problem+json). */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A sanitized internal error (problem+json). */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A required dependency is unavailable (problem+json). */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description An unexpected error rendered as RFC 9457 problem+json. */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
        };
    };
    validate_constraint_api_v1_constraint_proposals__artifact_id__validate_post: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": string;
                "If-Match": string;
                /** @description Session-bound CSRF token from login. Required when authenticating with the session cookie with a non-safe HTTP method, including a read-only POST resolver; ignored for ApiKey service clients. */
                "X-CSRF-Token"?: string;
            };
            path: {
                artifact_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["ConstraintValidationAdmissionRequestV1"];
            };
        };
        responses: {
            /** @description Successful Response */
            202: {
                headers: {
                    /** @description Caching directive; always `private, no-cache` for resources. */
                    "Cache-Control"?: string;
                    /** @description Strong entity tag of the resource for If-Match optimistic concurrency. */
                    ETag?: string;
                    /** @description The resource's monotonic integer revision. */
                    "X-Resource-Revision"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RunAcceptedV1"];
                };
            };
            /** @description Invalid cursor or malformed request (problem+json). */
            400: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Authentication is required or failed (problem+json). */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Forbidden: RBAC/CSRF/Origin rejected the request (problem+json). */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The requested resource was not found (problem+json). */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Conflict: revision/idempotency/workflow-guard/precondition (problem+json). */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The resume cursor is no longer retained (problem+json). */
            410: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request payload exceeds its bound (problem+json). */
            413: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request does not match the required schema or is too broad (problem+json). */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A configured quota was exceeded (problem+json). */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A sanitized internal error (problem+json). */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A required dependency is unavailable (problem+json). */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description An unexpected error rendered as RFC 9457 problem+json. */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
        };
    };
    propose_constraint_api_v1_constraint_proposals_propose_post: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": string;
                /** @description Session-bound CSRF token from login. Required when authenticating with the session cookie with a non-safe HTTP method, including a read-only POST resolver; ignored for ApiKey service clients. */
                "X-CSRF-Token"?: string;
            };
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["ConstraintProposeRequestV1"];
            };
        };
        responses: {
            /** @description Successful Response */
            202: {
                headers: {
                    /** @description Always `private, no-cache`. */
                    "Cache-Control"?: string;
                    /** @description Relative status URL of the accepted Run. */
                    Location?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RunAcceptedV1"];
                };
            };
            /** @description Authentication is required or failed (problem+json). */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Forbidden: RBAC/CSRF/Origin rejected the request (problem+json). */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The requested resource was not found (problem+json). */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Conflict: revision/idempotency/workflow-guard/precondition (problem+json). */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request payload exceeds its bound (problem+json). */
            413: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request does not match the required schema or is too broad (problem+json). */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A configured quota was exceeded (problem+json). */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A sanitized internal error (problem+json). */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A required dependency is unavailable (problem+json). */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description An unexpected error rendered as RFC 9457 problem+json. */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
        };
    };
    constraints_api_v1_constraints_get: {
        parameters: {
            query?: {
                cursor?: string | null;
                limit?: number;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    /** @description Caching directive; always `private, no-cache`. */
                    "Cache-Control"?: string;
                    /** @description Strong entity tag bound to the read snapshot of this page. */
                    ETag?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["OpaquePageV1_ConstraintSnapshotViewV1_"];
                };
            };
            /** @description Invalid cursor or malformed request (problem+json). */
            400: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Authentication is required or failed (problem+json). */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Forbidden: RBAC/CSRF/Origin rejected the request (problem+json). */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The resume cursor is no longer retained (problem+json). */
            410: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request does not match the required schema or is too broad (problem+json). */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A configured quota was exceeded (problem+json). */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A sanitized internal error (problem+json). */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A required dependency is unavailable (problem+json). */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description An unexpected error rendered as RFC 9457 problem+json. */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
        };
    };
    constraint_api_v1_constraints__artifact_id__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                artifact_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    /** @description Caching directive; always `private, no-cache` for resources. */
                    "Cache-Control"?: string;
                    /** @description Strong entity tag of the resource for If-Match optimistic concurrency. */
                    ETag?: string;
                    /** @description The resource's monotonic integer revision. */
                    "X-Resource-Revision"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ConstraintSnapshotViewV1"];
                };
            };
            /** @description Authentication is required or failed (problem+json). */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Forbidden: RBAC/CSRF/Origin rejected the request (problem+json). */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The requested resource was not found (problem+json). */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request does not match the required schema or is too broad (problem+json). */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A configured quota was exceeded (problem+json). */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A sanitized internal error (problem+json). */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A required dependency is unavailable (problem+json). */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description An unexpected error rendered as RFC 9457 problem+json. */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
        };
    };
    get_run_cost_api_v1_cost__run_id__get: {
        parameters: {
            query?: {
                cursor?: string | null;
                limit?: number;
                view_schema_version?: "run-cost-view@1" | "run-cost-view@2";
            };
            header?: never;
            path: {
                run_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RunCostViewV1"] | components["schemas"]["RunCostViewV2"];
                };
            };
            /** @description Invalid cursor or malformed request (problem+json). */
            400: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Authentication is required or failed (problem+json). */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Forbidden: RBAC/CSRF/Origin rejected the request (problem+json). */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The requested resource was not found (problem+json). */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The resume cursor is no longer retained (problem+json). */
            410: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request does not match the required schema or is too broad (problem+json). */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A configured quota was exceeded (problem+json). */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A sanitized internal error (problem+json). */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A required dependency is unavailable (problem+json). */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description An unexpected error rendered as RFC 9457 problem+json. */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
        };
    };
    diff_api_v1_diff_get: {
        parameters: {
            query: {
                base: string;
                target: string;
                cursor?: string | null;
                limit?: number;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    /** @description Caching directive; always `private, no-cache`. */
                    "Cache-Control"?: string;
                    /** @description Strong entity tag bound to the read snapshot of this page. */
                    ETag?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["SnapshotDiffHttpPageV1"];
                };
            };
            /** @description Invalid cursor or malformed request (problem+json). */
            400: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Authentication is required or failed (problem+json). */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Forbidden: RBAC/CSRF/Origin rejected the request (problem+json). */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The requested resource was not found (problem+json). */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The resume cursor is no longer retained (problem+json). */
            410: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request does not match the required schema or is too broad (problem+json). */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A configured quota was exceeded (problem+json). */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A sanitized internal error (problem+json). */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A required dependency is unavailable (problem+json). */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description An unexpected error rendered as RFC 9457 problem+json. */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
        };
    };
    resolve_execution_option_api_v1_execution_options_resolve_post: {
        parameters: {
            query?: never;
            header?: {
                /** @description Session-bound CSRF token from login. Required when authenticating with the session cookie with a non-safe HTTP method, including a read-only POST resolver; ignored for ApiKey service clients. */
                "X-CSRF-Token"?: string;
            };
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["ExecutionOptionResolveRequestV1"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    /** @description Always `private, no-store` for execution options. */
                    "Cache-Control"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ExecutionOptionViewV1"];
                };
            };
            /** @description Authentication is required or failed (problem+json). */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Forbidden: RBAC/CSRF/Origin rejected the request (problem+json). */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Conflict: revision/idempotency/workflow-guard/precondition (problem+json). */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request payload exceeds its bound (problem+json). */
            413: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request does not match the required schema or is too broad (problem+json). */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A configured quota was exceeded (problem+json). */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A sanitized internal error (problem+json). */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A required dependency is unavailable (problem+json). */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description An unexpected error rendered as RFC 9457 problem+json. */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
        };
    };
    execution_profiles_api_v1_execution_profiles_get: {
        parameters: {
            query?: {
                profile_kind?: ("generation" | "patch_repair" | "constraint_extraction" | "review" | "llm_triage" | "checker" | "simulation" | "workload" | "config_export" | "task_suite_derivation" | "environment" | "playtest_planner" | "validation" | "constraint_compiler" | "rollback" | "schema_compatibility" | "impact_analysis" | "bench_evaluator" | "artifact_migrator" | "dr_plan" | "restore_target" | "dr_verifier") | null;
                run_kind?: string | null;
                run_kind_version?: number | null;
                domain_id?: string | null;
                status?: ("active" | "replay_only" | "disabled") | null;
                cursor?: string | null;
                limit?: number;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    /** @description Caching directive; always `private, no-cache`. */
                    "Cache-Control"?: string;
                    /** @description Strong entity tag bound to the read snapshot of this page. */
                    ETag?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["OpaquePageV1_ExecutionProfileViewV1_"];
                };
            };
            /** @description Invalid cursor or malformed request (problem+json). */
            400: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Authentication is required or failed (problem+json). */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Forbidden: RBAC/CSRF/Origin rejected the request (problem+json). */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The resume cursor is no longer retained (problem+json). */
            410: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request does not match the required schema or is too broad (problem+json). */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A configured quota was exceeded (problem+json). */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A sanitized internal error (problem+json). */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A required dependency is unavailable (problem+json). */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description An unexpected error rendered as RFC 9457 problem+json. */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
        };
    };
    execution_profile_api_v1_execution_profiles__profile_id__versions__version__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                profile_id: string;
                version: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    /** @description Caching directive; always `private, no-cache` for resources. */
                    "Cache-Control"?: string;
                    /** @description Strong entity tag of the resource for If-Match optimistic concurrency. */
                    ETag?: string;
                    /** @description The resource's monotonic integer revision. */
                    "X-Resource-Revision"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ExecutionProfileViewV1"];
                };
            };
            /** @description Authentication is required or failed (problem+json). */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Forbidden: RBAC/CSRF/Origin rejected the request (problem+json). */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The requested resource was not found (problem+json). */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request does not match the required schema or is too broad (problem+json). */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A configured quota was exceeded (problem+json). */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A sanitized internal error (problem+json). */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A required dependency is unavailable (problem+json). */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description An unexpected error rendered as RFC 9457 problem+json. */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
        };
    };
    constraint_validation_compiler_binding_api_v1_execution_profiles__profile_id__versions__version__constraint_validation_binding_get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                profile_id: string;
                version: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    /** @description Caching directive; always `private, no-cache` for resources. */
                    "Cache-Control"?: string;
                    /** @description Strong entity tag of the resource for If-Match optimistic concurrency. */
                    ETag?: string;
                    /** @description The resource's monotonic integer revision. */
                    "X-Resource-Revision"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ConstraintValidationCompilerBindingViewV1"];
                };
            };
            /** @description Authentication is required or failed (problem+json). */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Forbidden: RBAC/CSRF/Origin rejected the request (problem+json). */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The requested resource was not found (problem+json). */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Conflict: revision/idempotency/workflow-guard/precondition (problem+json). */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request does not match the required schema or is too broad (problem+json). */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A configured quota was exceeded (problem+json). */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A sanitized internal error (problem+json). */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A required dependency is unavailable (problem+json). */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description An unexpected error rendered as RFC 9457 problem+json. */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
        };
    };
    task_suite_derivation_binding_api_v1_execution_profiles__profile_id__versions__version__task_suite_derivation_binding_get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                profile_id: string;
                version: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    /** @description Caching directive; always `private, no-cache` for resources. */
                    "Cache-Control"?: string;
                    /** @description Strong entity tag of the resource for If-Match optimistic concurrency. */
                    ETag?: string;
                    /** @description The resource's monotonic integer revision. */
                    "X-Resource-Revision"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["TaskSuiteDerivationBindingViewV1"];
                };
            };
            /** @description Authentication is required or failed (problem+json). */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Forbidden: RBAC/CSRF/Origin rejected the request (problem+json). */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The requested resource was not found (problem+json). */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Conflict: revision/idempotency/workflow-guard/precondition (problem+json). */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request does not match the required schema or is too broad (problem+json). */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A configured quota was exceeded (problem+json). */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A sanitized internal error (problem+json). */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A required dependency is unavailable (problem+json). */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description An unexpected error rendered as RFC 9457 problem+json. */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
        };
    };
    findings_api_v1_findings_get: {
        parameters: {
            query?: {
                cursor?: string | null;
                limit?: number;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    /** @description Caching directive; always `private, no-cache`. */
                    "Cache-Control"?: string;
                    /** @description Strong entity tag bound to the read snapshot of this page. */
                    ETag?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["OpaquePageV1_FindingRevisionV1_"];
                };
            };
            /** @description Invalid cursor or malformed request (problem+json). */
            400: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Authentication is required or failed (problem+json). */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Forbidden: RBAC/CSRF/Origin rejected the request (problem+json). */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The resume cursor is no longer retained (problem+json). */
            410: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request does not match the required schema or is too broad (problem+json). */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A configured quota was exceeded (problem+json). */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A sanitized internal error (problem+json). */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A required dependency is unavailable (problem+json). */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description An unexpected error rendered as RFC 9457 problem+json. */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
        };
    };
    latest_finding_api_v1_findings__finding_id__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                finding_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    /** @description Caching directive; always `private, no-cache` for resources. */
                    "Cache-Control"?: string;
                    /** @description Strong entity tag of the resource for If-Match optimistic concurrency. */
                    ETag?: string;
                    /** @description The resource's monotonic integer revision. */
                    "X-Resource-Revision"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["FindingRevisionV1"];
                };
            };
            /** @description Authentication is required or failed (problem+json). */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Forbidden: RBAC/CSRF/Origin rejected the request (problem+json). */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The requested resource was not found (problem+json). */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request does not match the required schema or is too broad (problem+json). */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A configured quota was exceeded (problem+json). */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A sanitized internal error (problem+json). */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A required dependency is unavailable (problem+json). */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description An unexpected error rendered as RFC 9457 problem+json. */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
        };
    };
    exact_finding_api_v1_findings__finding_id__revisions__revision__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                finding_id: string;
                revision: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    /** @description Caching directive; always `private, no-cache` for resources. */
                    "Cache-Control"?: string;
                    /** @description Strong entity tag of the resource for If-Match optimistic concurrency. */
                    ETag?: string;
                    /** @description The resource's monotonic integer revision. */
                    "X-Resource-Revision"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["FindingRevisionV1"];
                };
            };
            /** @description Authentication is required or failed (problem+json). */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Forbidden: RBAC/CSRF/Origin rejected the request (problem+json). */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The requested resource was not found (problem+json). */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request does not match the required schema or is too broad (problem+json). */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A configured quota was exceeded (problem+json). */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A sanitized internal error (problem+json). */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A required dependency is unavailable (problem+json). */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description An unexpected error rendered as RFC 9457 problem+json. */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
        };
    };
    propose_generation_api_v1_generation_propose_post: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": string;
                /** @description Session-bound CSRF token from login. Required when authenticating with the session cookie with a non-safe HTTP method, including a read-only POST resolver; ignored for ApiKey service clients. */
                "X-CSRF-Token"?: string;
            };
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["GenerationProposeRequestV1"];
            };
        };
        responses: {
            /** @description Successful Response */
            202: {
                headers: {
                    /** @description Always `private, no-cache`. */
                    "Cache-Control"?: string;
                    /** @description Relative status URL of the accepted Run. */
                    Location?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RunAcceptedV1"];
                };
            };
            /** @description Authentication is required or failed (problem+json). */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Forbidden: RBAC/CSRF/Origin rejected the request (problem+json). */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The requested resource was not found (problem+json). */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Conflict: revision/idempotency/workflow-guard/precondition (problem+json). */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request payload exceeds its bound (problem+json). */
            413: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request does not match the required schema or is too broad (problem+json). */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A configured quota was exceeded (problem+json). */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A sanitized internal error (problem+json). */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A required dependency is unavailable (problem+json). */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description An unexpected error rendered as RFC 9457 problem+json. */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
        };
    };
    query_logs_api_v1_logs_query_get: {
        parameters: {
            query: {
                start_utc: string;
                end_utc: string;
                services?: string[] | null;
                levels?: ("debug" | "info" | "warning" | "error" | "critical")[] | null;
                event_names?: string[] | null;
                run_id?: string | null;
                trace_id?: string | null;
                span_id?: string | null;
                producer_run_id?: string | null;
                cursor?: string | null;
                limit?: number;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["LogPageV1"];
                };
            };
            /** @description Invalid cursor or malformed request (problem+json). */
            400: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Authentication is required or failed (problem+json). */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Forbidden: RBAC/CSRF/Origin rejected the request (problem+json). */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The requested resource was not found (problem+json). */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The resume cursor is no longer retained (problem+json). */
            410: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request does not match the required schema or is too broad (problem+json). */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A configured quota was exceeded (problem+json). */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A sanitized internal error (problem+json). */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A required dependency is unavailable (problem+json). */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description An unexpected error rendered as RFC 9457 problem+json. */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
        };
    };
    get_metric_descriptors_api_v1_metrics_descriptors_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["MetricDescriptorRegistryV1"];
                };
            };
            /** @description Invalid cursor or malformed request (problem+json). */
            400: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Authentication is required or failed (problem+json). */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Forbidden: RBAC/CSRF/Origin rejected the request (problem+json). */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The requested resource was not found (problem+json). */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The resume cursor is no longer retained (problem+json). */
            410: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request does not match the required schema or is too broad (problem+json). */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A configured quota was exceeded (problem+json). */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A sanitized internal error (problem+json). */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A required dependency is unavailable (problem+json). */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description An unexpected error rendered as RFC 9457 problem+json. */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
        };
    };
    query_metrics_api_v1_metrics_query_get: {
        parameters: {
            query: {
                descriptor_refs: string;
                start_utc: string;
                end_utc: string;
                resolution_s: number;
                max_points: number;
                series_limit: number;
                label_matchers?: string;
                cursor?: string | null;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["MetricPageV1"];
                };
            };
            /** @description Invalid cursor or malformed request (problem+json). */
            400: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Authentication is required or failed (problem+json). */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Forbidden: RBAC/CSRF/Origin rejected the request (problem+json). */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The requested resource was not found (problem+json). */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The resume cursor is no longer retained (problem+json). */
            410: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request does not match the required schema or is too broad (problem+json). */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A configured quota was exceeded (problem+json). */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A sanitized internal error (problem+json). */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A required dependency is unavailable (problem+json). */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description An unexpected error rendered as RFC 9457 problem+json. */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
        };
    };
    patches_api_v1_patches_get: {
        parameters: {
            query?: {
                cursor?: string | null;
                limit?: number;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    /** @description Caching directive; always `private, no-cache`. */
                    "Cache-Control"?: string;
                    /** @description Strong entity tag bound to the read snapshot of this page. */
                    ETag?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["OpaquePageV1_PatchArtifactReadViewV1_"];
                };
            };
            /** @description Invalid cursor or malformed request (problem+json). */
            400: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Authentication is required or failed (problem+json). */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Forbidden: RBAC/CSRF/Origin rejected the request (problem+json). */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The resume cursor is no longer retained (problem+json). */
            410: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request does not match the required schema or is too broad (problem+json). */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A configured quota was exceeded (problem+json). */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A sanitized internal error (problem+json). */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A required dependency is unavailable (problem+json). */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description An unexpected error rendered as RFC 9457 problem+json. */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
        };
    };
    draft_patch_api_v1_patches_post: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": string;
                /** @description Session-bound CSRF token from login. Required when authenticating with the session cookie with a non-safe HTTP method, including a read-only POST resolver; ignored for ApiKey service clients. */
                "X-CSRF-Token"?: string;
            };
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["HumanPatchDraftRequestV1"];
            };
        };
        responses: {
            /** @description Successful Response */
            201: {
                headers: {
                    /** @description Caching directive; always `private, no-cache` for resources. */
                    "Cache-Control"?: string;
                    /** @description Strong entity tag of the resource for If-Match optimistic concurrency. */
                    ETag?: string;
                    /** @description The resource's monotonic integer revision. */
                    "X-Resource-Revision"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["PatchArtifactReadViewV1"];
                };
            };
            /** @description Invalid cursor or malformed request (problem+json). */
            400: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Authentication is required or failed (problem+json). */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Forbidden: RBAC/CSRF/Origin rejected the request (problem+json). */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The requested resource was not found (problem+json). */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Conflict: revision/idempotency/workflow-guard/precondition (problem+json). */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The resume cursor is no longer retained (problem+json). */
            410: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request payload exceeds its bound (problem+json). */
            413: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request does not match the required schema or is too broad (problem+json). */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A configured quota was exceeded (problem+json). */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A sanitized internal error (problem+json). */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A required dependency is unavailable (problem+json). */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description An unexpected error rendered as RFC 9457 problem+json. */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
        };
    };
    patch_api_v1_patches__artifact_id__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                artifact_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    /** @description Caching directive; always `private, no-cache` for resources. */
                    "Cache-Control"?: string;
                    /** @description Strong entity tag of the resource for If-Match optimistic concurrency. */
                    ETag?: string;
                    /** @description The resource's monotonic integer revision. */
                    "X-Resource-Revision"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["PatchArtifactReadViewV1"];
                };
            };
            /** @description Authentication is required or failed (problem+json). */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Forbidden: RBAC/CSRF/Origin rejected the request (problem+json). */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The requested resource was not found (problem+json). */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request does not match the required schema or is too broad (problem+json). */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A configured quota was exceeded (problem+json). */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A sanitized internal error (problem+json). */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A required dependency is unavailable (problem+json). */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description An unexpected error rendered as RFC 9457 problem+json. */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
        };
    };
    apply_patch_api_v1_patches__artifact_id__apply_post: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": string;
                "If-Match": string;
                /** @description Session-bound CSRF token from login. Required when authenticating with the session cookie with a non-safe HTTP method, including a read-only POST resolver; ignored for ApiKey service clients. */
                "X-CSRF-Token"?: string;
            };
            path: {
                artifact_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["WorkflowApplyRequestV1"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    /** @description Caching directive; always `private, no-cache` for resources. */
                    "Cache-Control"?: string;
                    /** @description Strong entity tag of the resource for If-Match optimistic concurrency. */
                    ETag?: string;
                    /** @description The resource's monotonic integer revision. */
                    "X-Resource-Revision"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["WorkflowApplyResultV1"];
                };
            };
            /** @description Invalid cursor or malformed request (problem+json). */
            400: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Authentication is required or failed (problem+json). */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Forbidden: RBAC/CSRF/Origin rejected the request (problem+json). */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The requested resource was not found (problem+json). */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Conflict: revision/idempotency/workflow-guard/precondition (problem+json). */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The resume cursor is no longer retained (problem+json). */
            410: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request payload exceeds its bound (problem+json). */
            413: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request does not match the required schema or is too broad (problem+json). */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A configured quota was exceeded (problem+json). */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A sanitized internal error (problem+json). */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A required dependency is unavailable (problem+json). */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description An unexpected error rendered as RFC 9457 problem+json. */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
        };
    };
    rebase_patch_api_v1_patches__artifact_id__rebase_post: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": string;
                "If-Match": string;
                /** @description Session-bound CSRF token from login. Required when authenticating with the session cookie with a non-safe HTTP method, including a read-only POST resolver; ignored for ApiKey service clients. */
                "X-CSRF-Token"?: string;
            };
            path: {
                artifact_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["PatchRebaseRequestV1"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    /** @description Caching directive; always `private, no-cache` for resources. */
                    "Cache-Control"?: string;
                    /** @description Strong entity tag of the resource for If-Match optimistic concurrency. */
                    ETag?: string;
                    /** @description The resource's monotonic integer revision. */
                    "X-Resource-Revision"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RebaseResult"];
                };
            };
            /** @description Invalid cursor or malformed request (problem+json). */
            400: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Authentication is required or failed (problem+json). */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Forbidden: RBAC/CSRF/Origin rejected the request (problem+json). */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The requested resource was not found (problem+json). */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Conflict: revision/idempotency/workflow-guard/precondition (problem+json). */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The resume cursor is no longer retained (problem+json). */
            410: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request payload exceeds its bound (problem+json). */
            413: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request does not match the required schema or is too broad (problem+json). */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A configured quota was exceeded (problem+json). */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A sanitized internal error (problem+json). */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A required dependency is unavailable (problem+json). */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description An unexpected error rendered as RFC 9457 problem+json. */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
        };
    };
    repair_patch_api_v1_patches__artifact_id__repair_post: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": string;
                /** @description Session-bound CSRF token from login. Required when authenticating with the session cookie with a non-safe HTTP method, including a read-only POST resolver; ignored for ApiKey service clients. */
                "X-CSRF-Token"?: string;
            };
            path: {
                artifact_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["PatchRepairRequestV1"];
            };
        };
        responses: {
            /** @description Successful Response */
            202: {
                headers: {
                    /** @description Always `private, no-cache`. */
                    "Cache-Control"?: string;
                    /** @description Relative status URL of the accepted Run. */
                    Location?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RunAcceptedV1"];
                };
            };
            /** @description Authentication is required or failed (problem+json). */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Forbidden: RBAC/CSRF/Origin rejected the request (problem+json). */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The requested resource was not found (problem+json). */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Conflict: revision/idempotency/workflow-guard/precondition (problem+json). */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request payload exceeds its bound (problem+json). */
            413: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request does not match the required schema or is too broad (problem+json). */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A configured quota was exceeded (problem+json). */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A sanitized internal error (problem+json). */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A required dependency is unavailable (problem+json). */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description An unexpected error rendered as RFC 9457 problem+json. */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
        };
    };
    resolve_patch_conflicts_api_v1_patches__artifact_id__resolve_conflicts_post: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": string;
                "If-Match": string;
                /** @description Session-bound CSRF token from login. Required when authenticating with the session cookie with a non-safe HTTP method, including a read-only POST resolver; ignored for ApiKey service clients. */
                "X-CSRF-Token"?: string;
            };
            path: {
                artifact_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["ResolveConflictsRequestV1"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    /** @description Caching directive; always `private, no-cache` for resources. */
                    "Cache-Control"?: string;
                    /** @description Strong entity tag of the resource for If-Match optimistic concurrency. */
                    ETag?: string;
                    /** @description The resource's monotonic integer revision. */
                    "X-Resource-Revision"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RebaseResult"];
                };
            };
            /** @description Invalid cursor or malformed request (problem+json). */
            400: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Authentication is required or failed (problem+json). */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Forbidden: RBAC/CSRF/Origin rejected the request (problem+json). */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The requested resource was not found (problem+json). */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Conflict: revision/idempotency/workflow-guard/precondition (problem+json). */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The resume cursor is no longer retained (problem+json). */
            410: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request payload exceeds its bound (problem+json). */
            413: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request does not match the required schema or is too broad (problem+json). */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A configured quota was exceeded (problem+json). */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A sanitized internal error (problem+json). */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A required dependency is unavailable (problem+json). */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description An unexpected error rendered as RFC 9457 problem+json. */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
        };
    };
    submit_patch_api_v1_patches__artifact_id__submit_for_approval_post: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": string;
                "If-Match": string;
                /** @description Session-bound CSRF token from login. Required when authenticating with the session cookie with a non-safe HTTP method, including a read-only POST resolver; ignored for ApiKey service clients. */
                "X-CSRF-Token"?: string;
            };
            path: {
                artifact_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["SubmitForApprovalRequestV1"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    /** @description Caching directive; always `private, no-cache` for resources. */
                    "Cache-Control"?: string;
                    /** @description Strong entity tag of the resource for If-Match optimistic concurrency. */
                    ETag?: string;
                    /** @description The resource's monotonic integer revision. */
                    "X-Resource-Revision"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ApprovalViewV1"];
                };
            };
            /** @description Invalid cursor or malformed request (problem+json). */
            400: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Authentication is required or failed (problem+json). */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Forbidden: RBAC/CSRF/Origin rejected the request (problem+json). */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The requested resource was not found (problem+json). */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Conflict: revision/idempotency/workflow-guard/precondition (problem+json). */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The resume cursor is no longer retained (problem+json). */
            410: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request payload exceeds its bound (problem+json). */
            413: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request does not match the required schema or is too broad (problem+json). */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A configured quota was exceeded (problem+json). */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A sanitized internal error (problem+json). */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A required dependency is unavailable (problem+json). */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description An unexpected error rendered as RFC 9457 problem+json. */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
        };
    };
    validate_patch_api_v1_patches__artifact_id__validate_post: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": string;
                "If-Match": string;
                /** @description Session-bound CSRF token from login. Required when authenticating with the session cookie with a non-safe HTTP method, including a read-only POST resolver; ignored for ApiKey service clients. */
                "X-CSRF-Token"?: string;
            };
            path: {
                artifact_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["PatchValidationAdmissionRequestV1"];
            };
        };
        responses: {
            /** @description Successful Response */
            202: {
                headers: {
                    /** @description Caching directive; always `private, no-cache` for resources. */
                    "Cache-Control"?: string;
                    /** @description Strong entity tag of the resource for If-Match optimistic concurrency. */
                    ETag?: string;
                    /** @description The resource's monotonic integer revision. */
                    "X-Resource-Revision"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RunAcceptedV1"];
                };
            };
            /** @description Invalid cursor or malformed request (problem+json). */
            400: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Authentication is required or failed (problem+json). */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Forbidden: RBAC/CSRF/Origin rejected the request (problem+json). */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The requested resource was not found (problem+json). */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Conflict: revision/idempotency/workflow-guard/precondition (problem+json). */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The resume cursor is no longer retained (problem+json). */
            410: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request payload exceeds its bound (problem+json). */
            413: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request does not match the required schema or is too broad (problem+json). */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A configured quota was exceeded (problem+json). */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A sanitized internal error (problem+json). */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A required dependency is unavailable (problem+json). */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description An unexpected error rendered as RFC 9457 problem+json. */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
        };
    };
    playtest_result_api_v1_playtest__run_id__result_get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                run_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    /** @description Caching directive; always `private, no-cache` for resources. */
                    "Cache-Control"?: string;
                    /** @description Strong entity tag of the resource for If-Match optimistic concurrency. */
                    ETag?: string;
                    /** @description The resource's monotonic integer revision. */
                    "X-Resource-Revision"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ArtifactPayloadViewV1"];
                };
            };
            /** @description Authentication is required or failed (problem+json). */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Forbidden: RBAC/CSRF/Origin rejected the request (problem+json). */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The requested resource was not found (problem+json). */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request does not match the required schema or is too broad (problem+json). */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A configured quota was exceeded (problem+json). */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A sanitized internal error (problem+json). */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A required dependency is unavailable (problem+json). */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description An unexpected error rendered as RFC 9457 problem+json. */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
        };
    };
    run_playtest_api_v1_playtest_run_post: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": string;
                /** @description Session-bound CSRF token from login. Required when authenticating with the session cookie with a non-safe HTTP method, including a read-only POST resolver; ignored for ApiKey service clients. */
                "X-CSRF-Token"?: string;
            };
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["PlaytestRunRequestV1"];
            };
        };
        responses: {
            /** @description Successful Response */
            202: {
                headers: {
                    /** @description Always `private, no-cache`. */
                    "Cache-Control"?: string;
                    /** @description Relative status URL of the accepted Run. */
                    Location?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RunAcceptedV1"];
                };
            };
            /** @description Authentication is required or failed (problem+json). */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Forbidden: RBAC/CSRF/Origin rejected the request (problem+json). */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The requested resource was not found (problem+json). */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Conflict: revision/idempotency/workflow-guard/precondition (problem+json). */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request payload exceeds its bound (problem+json). */
            413: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request does not match the required schema or is too broad (problem+json). */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A configured quota was exceeded (problem+json). */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A sanitized internal error (problem+json). */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A required dependency is unavailable (problem+json). */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description An unexpected error rendered as RFC 9457 problem+json. */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
        };
    };
    ref_history_api_v1_refs__ref_name__history_get: {
        parameters: {
            query?: {
                cursor?: string | null;
                limit?: number;
            };
            header?: never;
            path: {
                ref_name: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    /** @description Caching directive; always `private, no-cache`. */
                    "Cache-Control"?: string;
                    /** @description Strong entity tag bound to the read snapshot of this page. */
                    ETag?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["OpaquePageV1_RefHistoryEntryV1_"];
                };
            };
            /** @description Invalid cursor or malformed request (problem+json). */
            400: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Authentication is required or failed (problem+json). */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Forbidden: RBAC/CSRF/Origin rejected the request (problem+json). */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The requested resource was not found (problem+json). */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The resume cursor is no longer retained (problem+json). */
            410: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request does not match the required schema or is too broad (problem+json). */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A configured quota was exceeded (problem+json). */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A sanitized internal error (problem+json). */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A required dependency is unavailable (problem+json). */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description An unexpected error rendered as RFC 9457 problem+json. */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
        };
    };
    draft_rollback_api_v1_refs__ref_name__rollback_requests_post: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": string;
                /** @description Session-bound CSRF token from login. Required when authenticating with the session cookie with a non-safe HTTP method, including a read-only POST resolver; ignored for ApiKey service clients. */
                "X-CSRF-Token"?: string;
            };
            path: {
                ref_name: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["RollbackDraftRequestV1"];
            };
        };
        responses: {
            /** @description Successful Response */
            201: {
                headers: {
                    /** @description Caching directive; always `private, no-cache` for resources. */
                    "Cache-Control"?: string;
                    /** @description Strong entity tag of the resource for If-Match optimistic concurrency. */
                    ETag?: string;
                    /** @description The resource's monotonic integer revision. */
                    "X-Resource-Revision"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RollbackRequestReadViewV1"];
                };
            };
            /** @description Invalid cursor or malformed request (problem+json). */
            400: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Authentication is required or failed (problem+json). */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Forbidden: RBAC/CSRF/Origin rejected the request (problem+json). */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The requested resource was not found (problem+json). */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Conflict: revision/idempotency/workflow-guard/precondition (problem+json). */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The resume cursor is no longer retained (problem+json). */
            410: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request payload exceeds its bound (problem+json). */
            413: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request does not match the required schema or is too broad (problem+json). */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A configured quota was exceeded (problem+json). */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A sanitized internal error (problem+json). */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A required dependency is unavailable (problem+json). */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description An unexpected error rendered as RFC 9457 problem+json. */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
        };
    };
    reviews_api_v1_reviews_get: {
        parameters: {
            query?: {
                cursor?: string | null;
                limit?: number;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    /** @description Caching directive; always `private, no-cache`. */
                    "Cache-Control"?: string;
                    /** @description Strong entity tag bound to the read snapshot of this page. */
                    ETag?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["OpaquePageV1_ReviewArtifactViewV1_"];
                };
            };
            /** @description Invalid cursor or malformed request (problem+json). */
            400: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Authentication is required or failed (problem+json). */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Forbidden: RBAC/CSRF/Origin rejected the request (problem+json). */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The resume cursor is no longer retained (problem+json). */
            410: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request does not match the required schema or is too broad (problem+json). */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A configured quota was exceeded (problem+json). */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A sanitized internal error (problem+json). */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A required dependency is unavailable (problem+json). */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description An unexpected error rendered as RFC 9457 problem+json. */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
        };
    };
    review_api_v1_reviews__artifact_id__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                artifact_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    /** @description Caching directive; always `private, no-cache` for resources. */
                    "Cache-Control"?: string;
                    /** @description Strong entity tag of the resource for If-Match optimistic concurrency. */
                    ETag?: string;
                    /** @description The resource's monotonic integer revision. */
                    "X-Resource-Revision"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ReviewArtifactViewV1"];
                };
            };
            /** @description Authentication is required or failed (problem+json). */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Forbidden: RBAC/CSRF/Origin rejected the request (problem+json). */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The requested resource was not found (problem+json). */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request does not match the required schema or is too broad (problem+json). */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A configured quota was exceeded (problem+json). */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A sanitized internal error (problem+json). */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A required dependency is unavailable (problem+json). */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description An unexpected error rendered as RFC 9457 problem+json. */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
        };
    };
    review_producer_binding_api_v1_reviews__artifact_id__producer_binding_get: {
        parameters: {
            query: {
                run_id: string;
            };
            header?: never;
            path: {
                artifact_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    /** @description Caching directive; always `private, no-cache` for resources. */
                    "Cache-Control"?: string;
                    /** @description Strong entity tag of the resource for If-Match optimistic concurrency. */
                    ETag?: string;
                    /** @description The resource's monotonic integer revision. */
                    "X-Resource-Revision"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ReviewProducerBindingViewV1"];
                };
            };
            /** @description Authentication is required or failed (problem+json). */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Forbidden: RBAC/CSRF/Origin rejected the request (problem+json). */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The requested resource was not found (problem+json). */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request does not match the required schema or is too broad (problem+json). */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A configured quota was exceeded (problem+json). */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A sanitized internal error (problem+json). */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A required dependency is unavailable (problem+json). */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description An unexpected error rendered as RFC 9457 problem+json. */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
        };
    };
    rollback_requests_api_v1_rollback_requests_get: {
        parameters: {
            query?: {
                cursor?: string | null;
                limit?: number;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    /** @description Caching directive; always `private, no-cache`. */
                    "Cache-Control"?: string;
                    /** @description Strong entity tag bound to the read snapshot of this page. */
                    ETag?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["OpaquePageV1_RollbackRequestReadViewV1_"];
                };
            };
            /** @description Invalid cursor or malformed request (problem+json). */
            400: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Authentication is required or failed (problem+json). */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Forbidden: RBAC/CSRF/Origin rejected the request (problem+json). */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The resume cursor is no longer retained (problem+json). */
            410: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request does not match the required schema or is too broad (problem+json). */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A configured quota was exceeded (problem+json). */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A sanitized internal error (problem+json). */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A required dependency is unavailable (problem+json). */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description An unexpected error rendered as RFC 9457 problem+json. */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
        };
    };
    rollback_request_api_v1_rollback_requests__artifact_id__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                artifact_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    /** @description Caching directive; always `private, no-cache` for resources. */
                    "Cache-Control"?: string;
                    /** @description Strong entity tag of the resource for If-Match optimistic concurrency. */
                    ETag?: string;
                    /** @description The resource's monotonic integer revision. */
                    "X-Resource-Revision"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RollbackRequestReadViewV1"];
                };
            };
            /** @description Authentication is required or failed (problem+json). */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Forbidden: RBAC/CSRF/Origin rejected the request (problem+json). */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The requested resource was not found (problem+json). */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request does not match the required schema or is too broad (problem+json). */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A configured quota was exceeded (problem+json). */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A sanitized internal error (problem+json). */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A required dependency is unavailable (problem+json). */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description An unexpected error rendered as RFC 9457 problem+json. */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
        };
    };
    apply_rollback_api_v1_rollback_requests__artifact_id__apply_post: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": string;
                "If-Match": string;
                /** @description Session-bound CSRF token from login. Required when authenticating with the session cookie with a non-safe HTTP method, including a read-only POST resolver; ignored for ApiKey service clients. */
                "X-CSRF-Token"?: string;
            };
            path: {
                artifact_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["WorkflowApplyRequestV1"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    /** @description Caching directive; always `private, no-cache` for resources. */
                    "Cache-Control"?: string;
                    /** @description Strong entity tag of the resource for If-Match optimistic concurrency. */
                    ETag?: string;
                    /** @description The resource's monotonic integer revision. */
                    "X-Resource-Revision"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["WorkflowApplyResultV1"];
                };
            };
            /** @description Invalid cursor or malformed request (problem+json). */
            400: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Authentication is required or failed (problem+json). */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Forbidden: RBAC/CSRF/Origin rejected the request (problem+json). */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The requested resource was not found (problem+json). */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Conflict: revision/idempotency/workflow-guard/precondition (problem+json). */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The resume cursor is no longer retained (problem+json). */
            410: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request payload exceeds its bound (problem+json). */
            413: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request does not match the required schema or is too broad (problem+json). */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A configured quota was exceeded (problem+json). */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A sanitized internal error (problem+json). */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A required dependency is unavailable (problem+json). */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description An unexpected error rendered as RFC 9457 problem+json. */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
        };
    };
    submit_rollback_api_v1_rollback_requests__artifact_id__submit_for_approval_post: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": string;
                "If-Match": string;
                /** @description Session-bound CSRF token from login. Required when authenticating with the session cookie with a non-safe HTTP method, including a read-only POST resolver; ignored for ApiKey service clients. */
                "X-CSRF-Token"?: string;
            };
            path: {
                artifact_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["SubmitForApprovalRequestV1"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    /** @description Caching directive; always `private, no-cache` for resources. */
                    "Cache-Control"?: string;
                    /** @description Strong entity tag of the resource for If-Match optimistic concurrency. */
                    ETag?: string;
                    /** @description The resource's monotonic integer revision. */
                    "X-Resource-Revision"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ApprovalViewV1"];
                };
            };
            /** @description Invalid cursor or malformed request (problem+json). */
            400: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Authentication is required or failed (problem+json). */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Forbidden: RBAC/CSRF/Origin rejected the request (problem+json). */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The requested resource was not found (problem+json). */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Conflict: revision/idempotency/workflow-guard/precondition (problem+json). */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The resume cursor is no longer retained (problem+json). */
            410: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request payload exceeds its bound (problem+json). */
            413: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request does not match the required schema or is too broad (problem+json). */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A configured quota was exceeded (problem+json). */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A sanitized internal error (problem+json). */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A required dependency is unavailable (problem+json). */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description An unexpected error rendered as RFC 9457 problem+json. */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
        };
    };
    validate_rollback_api_v1_rollback_requests__artifact_id__validate_post: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": string;
                "If-Match": string;
                /** @description Session-bound CSRF token from login. Required when authenticating with the session cookie with a non-safe HTTP method, including a read-only POST resolver; ignored for ApiKey service clients. */
                "X-CSRF-Token"?: string;
            };
            path: {
                artifact_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["RollbackValidationAdmissionRequestV1"];
            };
        };
        responses: {
            /** @description Successful Response */
            202: {
                headers: {
                    /** @description Caching directive; always `private, no-cache` for resources. */
                    "Cache-Control"?: string;
                    /** @description Strong entity tag of the resource for If-Match optimistic concurrency. */
                    ETag?: string;
                    /** @description The resource's monotonic integer revision. */
                    "X-Resource-Revision"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RunAcceptedV1"];
                };
            };
            /** @description Invalid cursor or malformed request (problem+json). */
            400: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Authentication is required or failed (problem+json). */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Forbidden: RBAC/CSRF/Origin rejected the request (problem+json). */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The requested resource was not found (problem+json). */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Conflict: revision/idempotency/workflow-guard/precondition (problem+json). */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The resume cursor is no longer retained (problem+json). */
            410: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request payload exceeds its bound (problem+json). */
            413: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request does not match the required schema or is too broad (problem+json). */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A configured quota was exceeded (problem+json). */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A sanitized internal error (problem+json). */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A required dependency is unavailable (problem+json). */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description An unexpected error rendered as RFC 9457 problem+json. */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
        };
    };
    runs_api_v1_runs_get: {
        parameters: {
            query?: {
                status?: ("queued" | "leased" | "running" | "retry_wait" | "succeeded" | "failed" | "cancelled" | "timed_out") | null;
                cursor?: string | null;
                limit?: number;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    /** @description Caching directive; always `private, no-cache`. */
                    "Cache-Control"?: string;
                    /** @description Strong entity tag bound to the read snapshot of this page. */
                    ETag?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["OpaquePageV1_RunViewV1_"];
                };
            };
            /** @description Invalid cursor or malformed request (problem+json). */
            400: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Authentication is required or failed (problem+json). */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Forbidden: RBAC/CSRF/Origin rejected the request (problem+json). */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The resume cursor is no longer retained (problem+json). */
            410: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request does not match the required schema or is too broad (problem+json). */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A configured quota was exceeded (problem+json). */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A sanitized internal error (problem+json). */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A required dependency is unavailable (problem+json). */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description An unexpected error rendered as RFC 9457 problem+json. */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
        };
    };
    submit_run_api_v1_runs_post: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": string;
                /** @description Session-bound CSRF token from login. Required when authenticating with the session cookie with a non-safe HTTP method, including a read-only POST resolver; ignored for ApiKey service clients. */
                "X-CSRF-Token"?: string;
            };
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["RunSubmissionRequestV1"];
            };
        };
        responses: {
            /** @description Successful Response */
            202: {
                headers: {
                    /** @description Always `private, no-cache`. */
                    "Cache-Control"?: string;
                    /** @description Relative status URL of the accepted Run. */
                    Location?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RunAcceptedV1"];
                };
            };
            /** @description Authentication is required or failed (problem+json). */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Forbidden: RBAC/CSRF/Origin rejected the request (problem+json). */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The requested resource was not found (problem+json). */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Conflict: revision/idempotency/workflow-guard/precondition (problem+json). */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request payload exceeds its bound (problem+json). */
            413: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request does not match the required schema or is too broad (problem+json). */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A configured quota was exceeded (problem+json). */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A sanitized internal error (problem+json). */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A required dependency is unavailable (problem+json). */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description An unexpected error rendered as RFC 9457 problem+json. */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
        };
    };
    run_api_v1_runs__run_id__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                run_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    /** @description Caching directive; always `private, no-cache` for resources. */
                    "Cache-Control"?: string;
                    /** @description Strong entity tag of the resource for If-Match optimistic concurrency. */
                    ETag?: string;
                    /** @description The resource's monotonic integer revision. */
                    "X-Resource-Revision"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RunViewV1"];
                };
            };
            /** @description Authentication is required or failed (problem+json). */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Forbidden: RBAC/CSRF/Origin rejected the request (problem+json). */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The requested resource was not found (problem+json). */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request does not match the required schema or is too broad (problem+json). */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A configured quota was exceeded (problem+json). */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A sanitized internal error (problem+json). */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A required dependency is unavailable (problem+json). */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description An unexpected error rendered as RFC 9457 problem+json. */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
        };
    };
    run_commands_api_v1_runs__run_id__commands_get: {
        parameters: {
            query?: {
                cursor?: string | null;
                limit?: number;
            };
            header?: never;
            path: {
                run_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    /** @description Caching directive; always `private, no-cache`. */
                    "Cache-Control"?: string;
                    /** @description Strong entity tag bound to the read snapshot of this page. */
                    ETag?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["OpaquePageV1_RunCommandViewV1_"];
                };
            };
            /** @description Invalid cursor or malformed request (problem+json). */
            400: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Authentication is required or failed (problem+json). */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Forbidden: RBAC/CSRF/Origin rejected the request (problem+json). */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The requested resource was not found (problem+json). */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The resume cursor is no longer retained (problem+json). */
            410: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request does not match the required schema or is too broad (problem+json). */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A configured quota was exceeded (problem+json). */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A sanitized internal error (problem+json). */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A required dependency is unavailable (problem+json). */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description An unexpected error rendered as RFC 9457 problem+json. */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
        };
    };
    stream_run_events_api_v1_runs__run_id__events_get: {
        parameters: {
            query?: never;
            header?: {
                /** @description Last committed SSE event sequence received; omit only for a fresh stream. The raw header is canonical base-10 with no sign or leading zeroes, except `0`. */
                "Last-Event-ID"?: number;
            };
            path: {
                run_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description A stream of Server-Sent Events (`text/event-stream`). Each event's `data:` line is a canonical-JSON RunEvent (see schemas/sse-run-event-v1.json); the SSE `id:` is the persisted event `seq`, echoed via `Last-Event-ID` to resume. Comment lines (`:` keep-alive) do not advance the cursor. */
            200: {
                headers: {
                    /** @description Always `no-store` for the event stream. */
                    "Cache-Control"?: string;
                    /** @description Always `no` so proxies do not buffer the stream. */
                    "X-Accel-Buffering"?: string;
                    /** @description Earliest retained event `seq`; a resume below it fails 410. */
                    "X-Earliest-Event-Cursor"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "text/event-stream": string;
                };
            };
            /** @description Invalid cursor or malformed request (problem+json). */
            400: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Authentication is required or failed (problem+json). */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Forbidden: RBAC/CSRF/Origin rejected the request (problem+json). */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The requested resource was not found (problem+json). */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The resume cursor is no longer retained (problem+json). */
            410: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request does not match the required schema or is too broad (problem+json). */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A sanitized internal error (problem+json). */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A required dependency is unavailable (problem+json). */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description An unexpected error rendered as RFC 9457 problem+json. */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
        };
    };
    run_finding_links_api_v1_runs__run_id__finding_links_get: {
        parameters: {
            query?: {
                cursor?: string | null;
                limit?: number;
            };
            header?: never;
            path: {
                run_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    /** @description Caching directive; always `private, no-cache`. */
                    "Cache-Control"?: string;
                    /** @description Strong entity tag bound to the read snapshot of this page. */
                    ETag?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["OpaquePageV1_RunFindingLinkViewV1_"];
                };
            };
            /** @description Invalid cursor or malformed request (problem+json). */
            400: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Authentication is required or failed (problem+json). */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Forbidden: RBAC/CSRF/Origin rejected the request (problem+json). */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The requested resource was not found (problem+json). */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The resume cursor is no longer retained (problem+json). */
            410: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request does not match the required schema or is too broad (problem+json). */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A configured quota was exceeded (problem+json). */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A sanitized internal error (problem+json). */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A required dependency is unavailable (problem+json). */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description An unexpected error rendered as RFC 9457 problem+json. */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
        };
    };
    run_findings_api_v1_runs__run_id__findings_get: {
        parameters: {
            query?: {
                cursor?: string | null;
                limit?: number;
            };
            header?: never;
            path: {
                run_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    /** @description Caching directive; always `private, no-cache`. */
                    "Cache-Control"?: string;
                    /** @description Strong entity tag bound to the read snapshot of this page. */
                    ETag?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["OpaquePageV1_FindingRevisionV1_"];
                };
            };
            /** @description Invalid cursor or malformed request (problem+json). */
            400: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Authentication is required or failed (problem+json). */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Forbidden: RBAC/CSRF/Origin rejected the request (problem+json). */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The requested resource was not found (problem+json). */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The resume cursor is no longer retained (problem+json). */
            410: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request does not match the required schema or is too broad (problem+json). */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A configured quota was exceeded (problem+json). */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A sanitized internal error (problem+json). */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A required dependency is unavailable (problem+json). */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description An unexpected error rendered as RFC 9457 problem+json. */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
        };
    };
    list_run_traces_api_v1_runs__run_id__traces_get: {
        parameters: {
            query?: {
                cursor?: string | null;
                limit?: number;
            };
            header?: never;
            path: {
                run_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["TraceSummaryPageV1"];
                };
            };
            /** @description Invalid cursor or malformed request (problem+json). */
            400: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Authentication is required or failed (problem+json). */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Forbidden: RBAC/CSRF/Origin rejected the request (problem+json). */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The requested resource was not found (problem+json). */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The resume cursor is no longer retained (problem+json). */
            410: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request does not match the required schema or is too broad (problem+json). */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A configured quota was exceeded (problem+json). */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A sanitized internal error (problem+json). */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A required dependency is unavailable (problem+json). */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description An unexpected error rendered as RFC 9457 problem+json. */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
        };
    };
    cancel_run_api_v1_runs__run_id__cancel_post: {
        parameters: {
            query?: never;
            header?: {
                /** @description Session-bound CSRF token from login. Required when authenticating with the session cookie with a non-safe HTTP method, including a read-only POST resolver; ignored for ApiKey service clients. */
                "X-CSRF-Token"?: string;
            };
            path: {
                run_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["RunCommandV1"] & {
                    payload: components["schemas"]["CancelRunPayloadV1"];
                    /** @constant */
                    payload_schema_id: "run-cancel@1";
                    /** @constant */
                    type: "cancel";
                };
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    /** @description Always `no-store`. */
                    "Cache-Control"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RunCommandAckV1"];
                };
            };
            /** @description Authentication is required or failed (problem+json). */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Forbidden: RBAC/CSRF/Origin rejected the request (problem+json). */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The requested resource was not found (problem+json). */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Conflict: revision/idempotency/workflow-guard/precondition (problem+json). */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request payload exceeds its bound (problem+json). */
            413: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request does not match the required schema or is too broad (problem+json). */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A configured quota was exceeded (problem+json). */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A sanitized internal error (problem+json). */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A required dependency is unavailable (problem+json). */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description An unexpected error rendered as RFC 9457 problem+json. */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
        };
    };
    schema_registry_api_v1_schema_registry__version__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                version: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    /** @description Caching directive; always `private, no-cache` for resources. */
                    "Cache-Control"?: string;
                    /** @description Strong entity tag of the resource for If-Match optimistic concurrency. */
                    ETag?: string;
                    /** @description The resource's monotonic integer revision. */
                    "X-Resource-Revision"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["SchemaRegistryDocumentV1"];
                };
            };
            /** @description Authentication is required or failed (problem+json). */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Forbidden: RBAC/CSRF/Origin rejected the request (problem+json). */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The requested resource was not found (problem+json). */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request does not match the required schema or is too broad (problem+json). */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A configured quota was exceeded (problem+json). */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A sanitized internal error (problem+json). */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A required dependency is unavailable (problem+json). */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description An unexpected error rendered as RFC 9457 problem+json. */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
        };
    };
    specs_api_v1_specs_get: {
        parameters: {
            query?: {
                cursor?: string | null;
                limit?: number;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    /** @description Caching directive; always `private, no-cache`. */
                    "Cache-Control"?: string;
                    /** @description Strong entity tag bound to the read snapshot of this page. */
                    ETag?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["OpaquePageV1_SpecViewV1_"];
                };
            };
            /** @description Invalid cursor or malformed request (problem+json). */
            400: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Authentication is required or failed (problem+json). */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Forbidden: RBAC/CSRF/Origin rejected the request (problem+json). */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The resume cursor is no longer retained (problem+json). */
            410: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request does not match the required schema or is too broad (problem+json). */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A configured quota was exceeded (problem+json). */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A sanitized internal error (problem+json). */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A required dependency is unavailable (problem+json). */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description An unexpected error rendered as RFC 9457 problem+json. */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
        };
    };
    upload_spec_api_v1_specs_post: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": string;
                /** @description Session-bound CSRF token from login. Required when authenticating with the session cookie with a non-safe HTTP method, including a read-only POST resolver; ignored for ApiKey service clients. */
                "X-CSRF-Token"?: string;
            };
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["HumanSpecUploadRequestV1"];
            };
        };
        responses: {
            /** @description Successful Response */
            201: {
                headers: {
                    /** @description Caching directive; always `private, no-cache` for resources. */
                    "Cache-Control"?: string;
                    /** @description Strong entity tag of the resource for If-Match optimistic concurrency. */
                    ETag?: string;
                    /** @description The resource's monotonic integer revision. */
                    "X-Resource-Revision"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["SpecViewV1"];
                };
            };
            /** @description Invalid cursor or malformed request (problem+json). */
            400: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Authentication is required or failed (problem+json). */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Forbidden: RBAC/CSRF/Origin rejected the request (problem+json). */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The requested resource was not found (problem+json). */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Conflict: revision/idempotency/workflow-guard/precondition (problem+json). */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The resume cursor is no longer retained (problem+json). */
            410: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request payload exceeds its bound (problem+json). */
            413: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request does not match the required schema or is too broad (problem+json). */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A configured quota was exceeded (problem+json). */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A sanitized internal error (problem+json). */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A required dependency is unavailable (problem+json). */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description An unexpected error rendered as RFC 9457 problem+json. */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
        };
    };
    spec_api_v1_specs__artifact_id__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                artifact_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    /** @description Caching directive; always `private, no-cache` for resources. */
                    "Cache-Control"?: string;
                    /** @description Strong entity tag of the resource for If-Match optimistic concurrency. */
                    ETag?: string;
                    /** @description The resource's monotonic integer revision. */
                    "X-Resource-Revision"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["SpecViewV1"];
                };
            };
            /** @description Authentication is required or failed (problem+json). */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Forbidden: RBAC/CSRF/Origin rejected the request (problem+json). */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The requested resource was not found (problem+json). */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request does not match the required schema or is too broad (problem+json). */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A configured quota was exceeded (problem+json). */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A sanitized internal error (problem+json). */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A required dependency is unavailable (problem+json). */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description An unexpected error rendered as RFC 9457 problem+json. */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
        };
    };
    graph_api_v1_specs__artifact_id__graph_get: {
        parameters: {
            query?: {
                cursor?: string | null;
                limit?: number;
            };
            header?: never;
            path: {
                artifact_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    /** @description Caching directive; always `private, no-cache`. */
                    "Cache-Control"?: string;
                    /** @description Strong entity tag bound to the read snapshot of this page. */
                    ETag?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["OpaquePageV1_GraphItemV1_"];
                };
            };
            /** @description Invalid cursor or malformed request (problem+json). */
            400: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Authentication is required or failed (problem+json). */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Forbidden: RBAC/CSRF/Origin rejected the request (problem+json). */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The requested resource was not found (problem+json). */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The resume cursor is no longer retained (problem+json). */
            410: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request does not match the required schema or is too broad (problem+json). */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A configured quota was exceeded (problem+json). */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A sanitized internal error (problem+json). */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A required dependency is unavailable (problem+json). */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description An unexpected error rendered as RFC 9457 problem+json. */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
        };
    };
    task_suites_api_v1_task_suites_get: {
        parameters: {
            query?: {
                config_artifact_id?: string | null;
                constraint_artifact_id?: string | null;
                environment_profile_id?: string | null;
                environment_profile_version?: number | null;
                cursor?: string | null;
                limit?: number;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    /** @description Caching directive; always `private, no-cache`. */
                    "Cache-Control"?: string;
                    /** @description Strong entity tag bound to the read snapshot of this page. */
                    ETag?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["OpaquePageV1_TaskSuiteArtifactViewV1_"];
                };
            };
            /** @description Invalid cursor or malformed request (problem+json). */
            400: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Authentication is required or failed (problem+json). */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Forbidden: RBAC/CSRF/Origin rejected the request (problem+json). */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The resume cursor is no longer retained (problem+json). */
            410: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request does not match the required schema or is too broad (problem+json). */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A configured quota was exceeded (problem+json). */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A sanitized internal error (problem+json). */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A required dependency is unavailable (problem+json). */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description An unexpected error rendered as RFC 9457 problem+json. */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
        };
    };
    task_suite_api_v1_task_suites__artifact_id__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                artifact_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    /** @description Caching directive; always `private, no-cache` for resources. */
                    "Cache-Control"?: string;
                    /** @description Strong entity tag of the resource for If-Match optimistic concurrency. */
                    ETag?: string;
                    /** @description The resource's monotonic integer revision. */
                    "X-Resource-Revision"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["TaskSuiteArtifactViewV1"];
                };
            };
            /** @description Authentication is required or failed (problem+json). */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Forbidden: RBAC/CSRF/Origin rejected the request (problem+json). */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The requested resource was not found (problem+json). */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request does not match the required schema or is too broad (problem+json). */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A configured quota was exceeded (problem+json). */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A sanitized internal error (problem+json). */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A required dependency is unavailable (problem+json). */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description An unexpected error rendered as RFC 9457 problem+json. */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
        };
    };
    derive_task_suite_api_v1_task_suites_derive_post: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": string;
                /** @description Session-bound CSRF token from login. Required when authenticating with the session cookie with a non-safe HTTP method, including a read-only POST resolver; ignored for ApiKey service clients. */
                "X-CSRF-Token"?: string;
            };
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["TaskSuiteDeriveRequestV1"];
            };
        };
        responses: {
            /** @description Successful Response */
            202: {
                headers: {
                    /** @description Always `private, no-cache`. */
                    "Cache-Control"?: string;
                    /** @description Relative status URL of the accepted Run. */
                    Location?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RunAcceptedV1"];
                };
            };
            /** @description Authentication is required or failed (problem+json). */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Forbidden: RBAC/CSRF/Origin rejected the request (problem+json). */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The requested resource was not found (problem+json). */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Conflict: revision/idempotency/workflow-guard/precondition (problem+json). */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request payload exceeds its bound (problem+json). */
            413: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request does not match the required schema or is too broad (problem+json). */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A configured quota was exceeded (problem+json). */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A sanitized internal error (problem+json). */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A required dependency is unavailable (problem+json). */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description An unexpected error rendered as RFC 9457 problem+json. */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
        };
    };
    get_trace_api_v1_traces__trace_id__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                trace_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["TraceSummaryV1"];
                };
            };
            /** @description Invalid cursor or malformed request (problem+json). */
            400: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Authentication is required or failed (problem+json). */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Forbidden: RBAC/CSRF/Origin rejected the request (problem+json). */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The requested resource was not found (problem+json). */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The resume cursor is no longer retained (problem+json). */
            410: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request does not match the required schema or is too broad (problem+json). */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A configured quota was exceeded (problem+json). */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A sanitized internal error (problem+json). */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A required dependency is unavailable (problem+json). */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description An unexpected error rendered as RFC 9457 problem+json. */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
        };
    };
    get_trace_spans_api_v1_traces__trace_id__spans_get: {
        parameters: {
            query?: {
                cursor?: string | null;
                limit?: number;
            };
            header?: never;
            path: {
                trace_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["SpanPageV1"];
                };
            };
            /** @description Invalid cursor or malformed request (problem+json). */
            400: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Authentication is required or failed (problem+json). */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Forbidden: RBAC/CSRF/Origin rejected the request (problem+json). */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The requested resource was not found (problem+json). */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The resume cursor is no longer retained (problem+json). */
            410: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request does not match the required schema or is too broad (problem+json). */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A configured quota was exceeded (problem+json). */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A sanitized internal error (problem+json). */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A required dependency is unavailable (problem+json). */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description An unexpected error rendered as RFC 9457 problem+json. */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
        };
    };
    subject_approval_binding_api_v1_workflow_subjects__artifact_id__approval_binding_get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                artifact_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    /** @description Caching directive; always `private, no-cache` for resources. */
                    "Cache-Control"?: string;
                    /** @description Strong entity tag of the resource for If-Match optimistic concurrency. */
                    ETag?: string;
                    /** @description The resource's monotonic integer revision. */
                    "X-Resource-Revision"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["SubjectApprovalBindingViewV1"];
                };
            };
            /** @description Authentication is required or failed (problem+json). */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description Forbidden: RBAC/CSRF/Origin rejected the request (problem+json). */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The requested resource was not found (problem+json). */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description The request does not match the required schema or is too broad (problem+json). */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A configured quota was exceeded (problem+json). */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A sanitized internal error (problem+json). */
            500: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description A required dependency is unavailable (problem+json). */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
            /** @description An unexpected error rendered as RFC 9457 problem+json. */
            default: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/problem+json": components["schemas"]["Problem"];
                };
            };
        };
    };
}
