from __future__ import annotations

import copy
import hashlib
import json

import pytest


REGISTERED_SEARCH_SPEC_PAYLOAD = {
    "adjacency": {
        "first_parent_child_edges": 1,
        "first_parent_predecessor_edges": 1,
        "include_reachable_lineage_sources": True,
        "nonrecursive": True,
        "require_shared_exact_eligible_path_with_anchor": True,
    },
    "candidate_order": ["committed_at", "commit_oid"],
    "candidate_path_gate": "any_changed_path_eligible",
    "config_path_globs": ["mods/**/*.txt"],
    "config_only_rule": "all_changed_paths_eligible",
    "diff_merge_policy": "exclude_multi_parent_commits_from_diff_direct",
    "diff_match_scope": "eligible_path_patch_bytes",
    "diff_regex_encoding": "ascii_bytes",
    "excluded_path_globs": [
        "mods/**/README*.txt",
        "mods/**/animations/**",
        "mods/**/books/**",
        "mods/**/cutscenes/**",
        "mods/**/docs/**",
        "mods/**/languages/**",
        "mods/**/languages.txt",
        "mods/**/licenses/**",
        "mods/**/menus/**",
        "mods/**/readme*.txt",
        "mods/**/soundfx/**",
        "mods/**/tilesetdefs/**",
    ],
    "expected_revision_count": 7049,
    "git_commands": {
        "common_prefix": [
            "git",
            "--no-optional-locks",
            "--no-replace-objects",
            "-c",
            "color.ui=false",
            "-c",
            "core.attributesFile=/dev/null",
            "-c",
            "core.quotePath=true",
            "-c",
            "diff.noprefix=false",
            "-c",
            "diff.mnemonicPrefix=false",
            "-c",
            "diff.renames=false",
            "-c",
            "diff.algorithm=myers",
            "-c",
            "diff.indentHeuristic=false",
            "-c",
            "diff.interHunkContext=0",
            "-c",
            "diff.suppressBlankEmpty=false",
            "-c",
            "diff.orderFile=/dev/null",
            "-C",
            "{repo}",
        ],
        "empty_tree_args": ["hash-object", "-t", "tree", "--stdin"],
        "eligible_path_suffix": ["--", "{eligible_paths...}"],
        "history_args": ["rev-list", "--topo-order", "--reverse", "{revision_range}"],
        "metadata_args": [
            "show",
            "-s",
            "--no-show-signature",
            "--encoding=UTF-8",
            "--format=%H%x00%P%x00%ct%x00%s%x00%B",
            "{commit}",
        ],
        "patch_args": [
            "diff",
            "--binary",
            "--full-index",
            "--no-color",
            "--no-ext-diff",
            "--no-textconv",
            "--no-renames",
            "--src-prefix=a/",
            "--dst-prefix=b/",
            "--unified=3",
            "--inter-hunk-context=0",
            "--diff-algorithm=myers",
            "--no-indent-heuristic",
            "--submodule=short",
            "--ignore-submodules=none",
            "{parent}",
            "{commit}",
        ],
        "patch_id_args": ["patch-id", "--stable"],
        "paths_args": [
            "diff-tree",
            "--no-commit-id",
            "--name-status",
            "--no-renames",
            "-r",
            "-z",
            "{parent}",
            "{commit}",
        ],
        "resolve_args": ["rev-parse", "--verify", "{pinned_head}^{commit}"],
        "version_command": ["git", "--version"],
    },
    "git_environment_policy": {
        "drop_inherited_prefixes": ["GIT_"],
        "fixed": {
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "LANG": "C",
            "LC_ALL": "C",
            "TZ": "UTC",
        },
        "inherit_allowlist": ["PATH"],
    },
    "history_walk": "all_reachable_topo_order",
    "issue_pr_discovery": "disabled_offline_only",
    "lineage_regexes": [
        {
            "link_type": "backport",
            "pattern": "(?m)^Backport-of: ([0-9a-f]{40})$",
            "rule_id": "trailer.backport_of",
        },
        {
            "link_type": "cherry_pick",
            "pattern": r"(?m)^\(cherry picked from commit ([0-9a-f]{40})\)$",
            "rule_id": "trailer.cherry_pick_x",
        },
        {
            "link_type": "revert",
            "pattern": r"(?m)^This reverts commit ([0-9a-f]{40})\.$",
            "rule_id": "trailer.git_revert",
        },
    ],
    "lineage_message_field": "full_percent_B_utf8",
    "message_field": "subject_percent_s_utf8",
    "path_eligibility": "include_and_not_exclude",
    "path_glob_semantics": "component_fnmatch_double_star_zero_or_more",
    "pinned_head": "fe23b5ba73f99f0c3969f8b23dbabaa8f7a6b602",
    "rounds": [
        {
            "diff_regexes": [],
            "message_regexes": [
                {
                    "pattern": r"(?i)\A(?=[^\r\n]*\b(?:fix(?:ed|es)?|bugs?|bugfix(?:ed|es)?|broken|incorrect|wrong|missing|stuck|unreachable|not[ \t]+appearing|not[ \t]+being[ \t]+able|completed[ \t]+before)\b)(?=[^\r\n]*\b(?:quests?|status(?:es)?|loot|drops?|references?|spawns?|chests?|enem(?:y|ies)|items?)\b)[^\r\n]*\Z",
                    "rule_id": "initial.message_bug_and_domain",
                }
            ],
            "name": "initial",
        },
        {
            "diff_regexes": [
                {
                    "pattern": r"(?m)^[+-](?![+-])[ \t]*(?:requires_status|requires_not_status|set_status|unset_status|pickup_status|loot|chance|weight|requires_item|item)[ \t]*=",
                    "rule_id": "expanded.diff_behavior_key",
                }
            ],
            "message_regexes": [
                {
                    "pattern": r"(?i)\A(?!merge(?:[ \t]|\Z))(?=[^\r\n]*\b(?:fix(?:ed|es)?|bugs?|bugfix(?:ed|es)?|broken|incorrect|wrong|missing|stuck|unreachable|not[ \t]+appearing|not[ \t]+being[ \t]+able|completed[ \t]+before)\b)[^\r\n]*\Z",
                    "rule_id": "expanded.message_bug_language",
                }
            ],
            "name": "expanded",
        },
    ],
    "schema_version": "flare-b0a@1",
    "selected_round_semantics": "union_through_selected",
    "source_repo": "https://github.com/flareteam/flare-game.git",
    "stop_condition": "exhaust_reachable_range",
}
REGISTERED_SEARCH_SPEC_BYTES = (
    json.dumps(
        REGISTERED_SEARCH_SPEC_PAYLOAD,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    + b"\n"
)
REGISTERED_SEARCH_SPEC_SHA256 = hashlib.sha256(REGISTERED_SEARCH_SPEC_BYTES).hexdigest()


@pytest.fixture
def registered_search_spec_payload():
    return copy.deepcopy(REGISTERED_SEARCH_SPEC_PAYLOAD)


@pytest.fixture
def registered_search_spec_bytes():
    return REGISTERED_SEARCH_SPEC_BYTES


@pytest.fixture
def registered_search_spec_sha256():
    return REGISTERED_SEARCH_SPEC_SHA256
