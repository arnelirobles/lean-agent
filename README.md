# lean-agent

Four skills for running coding agents over a backlog, written as plain Markdown. No hooks, no MCP servers, no scripts.

**Upgrading from 1.x?** Version 2.0.0 moves every hook, the brain issue list and MCP server, the gates and the test tools to [lean-agent-tools](https://github.com/arnelirobles/lean-agent-tools). Updating lean-agent alone removes them, including the secret-scan guard. Install lean-agent-tools in the same sitting, then restart Claude Code:

```
/plugin marketplace add arnelirobles/lean-agent-tools
/plugin install lean-agent-tools@lean-agent-tools
```

Then update lean-agent to 2.0.0 from the `/plugin` menu.

Your settings carry over. The `LEAN_*` names, `brain`, `test-phase` and the `lean-agent` command keep their names.

## Install

From the Claude plugin directory, search for `lean-agent` and install it. Or add the marketplace yourself:

```
/plugin marketplace add arnelirobles/lean-agent
/plugin install lean-agent@lean-agent
```

## Skills

- `method`: the working rules. Plan in one-agent tickets, keep changes small, run the repository's own gate, test only what the change touches, and finish a batch with a review and a retro. Loads on its own when you plan, review or finish work.
- `shape-ticket`: writes or refines an issue in an agent-ready shape (Goal, Where, Covers, Done when, Risks, Constraints, Out of scope), after searching for an issue it belongs to.
- `adversarial-review`: reviews one change as an adversary. One fresh agent hunts for defects against fixed questions and the repository's rules. A second fresh agent tries to disprove each finding. Only survivors are reported, with proof. The review log is a Markdown file in the repo.
- `lean-retro`: a retro after a batch merges. Facts come from `git log` and `gh pr list`. Fixed triggers decide what to propose, and a person approves every change.

## lean-agent-tools

[lean-agent-tools](https://github.com/arnelirobles/lean-agent-tools) is the add-on. It adds the hooks (secret-scan, commit author and public text checks, test-before-push and others), the brain issue list with its MCP server and web page, the test queue (`test-phase`, `heavy.sh`), and the review and retro scripts. Its `tools` skill tells Claude which tool replaces which plain step in these skills. It installs from its own marketplace, because the plugin directory does not accept hooks, local MCP servers or scripts.

## License

[Mozilla Public License 2.0](LICENSE).
