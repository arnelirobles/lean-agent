# Review rules

Read by the adversarial-review skill before any code. Each rule is a question a reviewer answers
yes or no with a line. Keep them specific to this repository; general advice belongs in the skill.

## Contract

- The version that moves on a consumer-visible change: `src/Contract.cs`, `Contract.Version`.
- Consumers and the range each accepts:
  - `acme-console`, `src/lib/contract.ts`, `SUPPORTED = { min: 1, max: 3 }`
  - `acme-site`, the published `@acme/site` package, semver
- A change that moves the version merges only after every consumer above accepts the new value.

## Boundaries

- What belongs in the core and what belongs in a module, and the one-line test that decides it.
- Nothing secret, or hashed from a secret, in any record that is publicly readable.

## Data

- Every query and cache key names its tenant.
- Every new stored shape has a migration, and a test proves existing data survives it.

## Tests

- A bug fix names the test that fails without it.
- An assertion over a collection first asserts the collection is not empty.

## Style

- House writing rules that apply to code comments, docs and review threads.
