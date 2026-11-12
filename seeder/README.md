# Seeder

The seeder is the engine every playground runs to create its data in Gravitee: OpenFGA stores, an Access Management
domain, APIs, applications, subscriptions, groups and developer portals in API Management (in its own environment and in
those a playground adds), the objects of Gamma, and the Kubernetes resources that the Gravitee Kubernetes Operator turns into API Management objects. It talks to the platform only
through its management APIs, and records what it creates so that it can delete exactly that later.

## How a playground runs it

A playground has one service that builds `../../../../../seeder`, mounts its `seed/` folder at `/seed` and its `.state/` folder
at `/state`, and carries the label `io.gravitee.role: seed`. The Lab UI runs it when a Start sets the playground up and
on *Re-seed*, and runs `seed.py --remove` for *Delete what it created*. The service and the rules for seed files are in
[the seed](../docs/playgrounds.md#the-seed). The image is `ghcr.io/gravitee-io-labs/gravitee-lab/seeder`, built from
[`Dockerfile`](Dockerfile) with Python 3.15, `requirements.txt` and the OpenFGA command-line tool.

## Layout of this folder

| Where | What |
|---|---|
| `lab_seeder/` | The seeder: one module for each thing it seeds, the helpers they share (`common.py`, `controls.py`, `refills.py`, `gamma.py`), the lint of seed files (`seedlint.py`) and the kit of the tests (`testkit.py`) |
| `seed.py` | The command the image runs, and the path a playground's own entrypoint script runs (`python seed.py`): it calls `lab_seeder.seed.main` |
| `seedlint.py` | The path a catalog runs the lint at (`python lab/seeder/seedlint.py`): it hands over to the module of the package |
| `tests/` | The tests, and the invented samples they read (`fixtures/samples/`) |
| `Dockerfile`, `requirements.txt` | The image |
| `README.md`, `AGENTS.md` | This page, and the guide of an agent that works here |

## Layout of a seed folder

A module reads its own folder and does nothing when it is absent: [the seed's files](../docs/playgrounds.md#files) lists the
folders. `MODULES` in `lab_seeder/seed.py` is the order they run in, and `--remove` runs them in reverse.

A file is named after the object it describes, as its id or its slug: `acme-shop-llm.json` for the API `Acme Shop LLM`,
`acme-shop-ace.yaml` for the agent with the entity id `acme-shop-ace`. Files are read in name order, so prefix them with
`01-`, `02-` to choose the order.

## Order and parallelism

The modules run one after the other, and a removal in the reverse. What a module knows to
be independent, it makes at the same time:

- **Inside a module.** The groups, the APIs and the applications of an environment, then its subscriptions; the environments;
  the portals; the agent cards and the MCP servers the catalog reads; what a removal deletes of one kind. A folder whose
  files depend on one another (the pages of a portal, the manifests of a cluster, the policies of a schema) stays in name
  order. The platform's users are the organization's, and it does not make them well at the same time (a user was not found
  after it was made, or a search missed one): one environment at a time makes them, behind a lock, and looks for a new user
  until the platform lists it.
- **Waits.** The seed does not wait for the gateway: what the management API holds is what the seed made, and the gateway takes
  it within seconds. `SEED_WAIT_GATEWAY=1` brings the waits back (every API served, the protections and the budgets in force, a
  chat through every LLM proxy). What the seed must wait for to be right stays: an API that is replaced leaves the gateway
  before it is imported again, a user that a group has just made is looked for until the platform lists it, and with `AM_GATEWAY_URL` the AM gateway
  serves the domain and knows each application that has a secret, which a service that starts after the seed signs in with
  at once.
- **Load.** `SEED_WORKERS` (4 by default) is the number of requests in flight at any moment, whatever the number of steps that
  wait for an answer; `1` runs everything in order. The first step that fails stops the others at their next request, and
  its failure is the last line of the output.

A step that is new goes through `common.parallel` or `common.together` for what is independent in it; a request goes through
`Api`, a wait through `pause`, and a record through `common.record`, which is safe to call from several steps.

## Settings

The seed service's environment is the seeder's settings: the endpoints, the credentials and the timeouts. They are
listed, with their defaults, in [the seed's settings](../docs/playgrounds.md#settings).

## Work on the seeder

The module contract, what the Lab UI reads from a run, the behavior to keep and how to run the tests are in
[AGENTS.md](AGENTS.md).

## Playground saved states

The Lab UI runs `python -m lab_seeder.checkpoints save|check|restore /checkpoint/resources.json` in the playground's
seed service, with the current seeder source mounted read-only. The adapter uses that service's endpoints and
credentials, its own `/state/created.json`, and a private transaction folder. It never runs the seed or reads a shared
database volume. `check` validates ownership and conflicts without changing resources; restore keeps only this
playground's previous resources for rollback. See [Saved playground states](../docs/saved-playground-states.md) for
supported resources and recovery files. `consumer_states.py` follows subscriptions to the recorded APIs, including
consumer apps made in the Developer Portal. It restores private application settings and memberships, subscription
status, configuration and metadata, and saved key values and access. Shared applications stay read-only; keys that
also grant access outside the playground refuse restoration. The adapter keeps APIs stopped until consumer access
is reconciled, and never adds manual consumer apps to the seed’s permanent ownership record.
