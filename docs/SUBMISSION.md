# Directory submission

agent-msg is a skills-only plugin with a bundled Python CLI. It requires a local
environment with running Claude Code/Codex sessions and access to their native
sockets. It is not a hosted MCP service or a way for a cloud-only chat to access a
user's computer. Describe that requirement in every listing.

## Release inputs

- Repository: https://github.com/nsssayom/agent-msg
- Plugin directory: `plugins/agent-msg/`
- Marketplace: `.claude-plugin/marketplace.json` at the repository root
- Version: `0.1.1` (initial alpha)
- Download: `agent-msg-plugin-0.1.1.zip` from the matching GitHub release
- Listing text, prompts, icon paths: `src/agent_msg/plugin_assets/interface.json`
- Data handling: [PRIVACY.md](../PRIVACY.md)
- License: [LICENSE](../LICENSE)
- Support: https://github.com/nsssayom/agent-msg/issues

GitHub distribution does not imply approval or endorsement by either harness
vendor. Native messaging protocols are version-sensitive. Disclose the Claude
adapter's use of local peer authentication, the journal contents, and that a
receiving harness can forward messages to its model provider.

## Claude directory

Open the [submission portal](https://claude.ai/directory/manage/new) using the
publisher's paid Claude account with a connected GitHub account that can push to
this repository. Select a plugin bundle and enter:

| Field | Value |
| --- | --- |
| Repository | `nsssayom/agent-msg` |
| Plugin path | `plugins/agent-msg` |
| Branch or tag | `v0.1.1` |

The tag pins the reviewed release; select a new tag explicitly for a future
update. The plugin's bundled README supplies its listing description. Inspect
validation findings and complete the review form truthfully.
Submit for review, record the submission ID, and wait for approval before
claiming directory availability.

The data-handling form should disclose message and process metadata storage,
indefinite local retention until the user deletes the journal, and processing
by the receiving harness and its model provider. The publisher must confirm
the contact email, intended audience, and compliance acknowledgements in the portal.

Sources: [Anthropic's submission instructions](https://claude.com/docs/plugins/submit)
and [directory checklist](https://claude.com/docs/plugins/pre-submission-checklist).

## OpenAI directory

Open [Plugins](https://platform.openai.com/plugins) in the publisher's organization.
The account needs a verified developer identity and submission access. Upload the
plugin ZIP as a skills-only package. Check that the imported skill can locate
`lib/agent_msg/` and its launcher, and that the local-runtime limitation is retained.
Review the metadata and skill findings and correct the archive if needed.

The listing's `developerName` is currently the GitHub publisher, `nsssayom`;
confirm it matches the verified publishing identity before submission. No public
identity verification or policy attestation has been completed by this repository.
Do not invent account details, verification status, or review results.

Source: [OpenAI's upload and review instructions](https://developers.openai.com/plugins/deploy/submission).

## Reviewer scenarios

Use two disposable local harness sessions in the same scratch repository and the
same journal path. Python 3.10+ is required. Prior live tests used macOS, Claude
Code 2.1.286, Codex CLI 0.159.0, and daemon 0.159.2. Linux live delivery and
restricted sandbox integration are not validated.

| Scenario | Expected result |
| --- | --- |
| List sessions in the current workdir | Both peers appear with their harness and session identity; no message is sent. |
| Send from Codex to Claude and request `agent-msg reply` | One native message and a correlated reply are journaled. |
| Send from Claude to Codex with `--wait` | Wait completes only after a journaled reply from the addressed peer. |
| Send while the Codex peer is busy | Native steering is used and the correlated reply is recorded. |
| Inspect messages in `agent-msg ui` | Browser opens a loopback inspector; conversations and delivery events are visible. |
| Select an ambiguous or absent name | No native dispatch; CLI reports an error. |
| Receive routing metadata with a command or socket path | Protocol validation rejects unsupported route fields; no routing command executes. |
| Wait expires or dispatch becomes uncertain | CLI reports the outcome; the skill checks the journal instead of resending automatically. |

These are reviewer instructions, not a claim that a directory review has passed.
Record actual submission IDs and review outcomes only after the portals return them.
