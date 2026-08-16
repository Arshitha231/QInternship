"""specialize project descriptions for semantic search coverage

Revision ID: 9f31c0d7ae64
Revises: c4a7e1f93b02
Create Date: 2026-08-15

A DATA migration, same channel and justification as abb9a2a3ab1e (the
original description backfill): the deployed database is only reachable
from inside Azure's network boundary, and the App Service already runs
`alembic upgrade head` at startup.

WHY, given abb9a2a3ab1e already replaced every placeholder with real prose:
that migration fixed description QUALITY. It did not fix corpus DIVERSITY.
seed.py's GENERIC_PROJECT_DESCRIPTION_TEMPLATES are keyed by project TYPE,
so all 15 "{dept} Tooling Upgrade" projects share three sentences with only
{dept}/{tech1}/{tech2} swapped. To a human that reads fine; to an embedding
model those 15 vectors sit almost on top of each other, and nothing in the
corpus was actually ABOUT any specific engineering problem.

Measured before this migration: "our kubernetes cluster keeps dropping pods
and networking is flaky" retrieved "Infrastructure Onboarding Revamp" and
"IT Operations Onboarding Revamp" -- plausible people (SREs), but matched on
the word "Infrastructure", because no project in the corpus mentioned
Kubernetes, containers, clusters or networking at all.

These eight rewrites give the corpus real coverage of the problem shapes
mode 3 (app/project_search.py) is most likely to be asked about: container
platform and cluster networking, observability and alerting, deploy
pipelines and rollbacks, ETL failures and schema drift, data quality
contracts, flaky test suites, identity/SSO, and payment latency. Membership,
ownership and classification are untouched -- only description text changes,
so the project->employee hop still lands on the people already assigned.

Each row updates only if the current description matches one of its known
prior texts (the post-abb9a2a3ab1e text, or the original placeholder for a
database that somehow missed it). Idempotent by inspection, and it never
overwrites a description it doesn't recognize -- same discipline as
abb9a2a3ab1e.

Run `python build_project_embeddings.py` afterwards against whichever
database this ran on: project_embeddings stores a source_hash per row, so
only these eight re-embed, but they will not re-embed on their own.
`python build_search_index.py` too, for the employee-side index.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "9f31c0d7ae64"
down_revision: Union[str, Sequence[str], None] = "c4a7e1f93b02"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


DESCRIPTIONS = [
    (
        'Infrastructure Tooling Upgrade',
        # current text, and the pre-abb9a2a3ab1e placeholder, so this
        # lands on a database that missed that backfill too.
        ("Replaces the aging toolchain Infrastructure had outgrown with a stack built around AWS and Azure, chosen after the old tools stopped scaling with the team's headcount. Migration ran in parallel with day-to-day work rather than a hard cutover. A subset of historical records didn't map cleanly to the new schema and needed manual reconciliation after go-live.",
         'Infrastructure Tooling Upgrade — owned by Infrastructure.'),
        'Migrated the last of the self-managed virtual machines onto a managed Kubernetes platform, standardizing container builds, pod scheduling, and cluster networking across every service instead of per-team snowflake deployments. Ingress, service discovery, and the CNI plugin were the hardest parts: DNS resolution inside the cluster failed intermittently under load, and pods were being evicted during node drains because resource limits had been copied between services without being re-measured. Autoscaling now handles traffic spikes that used to need someone manually adding capacity at 3am.',
    ),
    (
        'Infrastructure Reporting Overhaul',
        # current text, and the pre-abb9a2a3ab1e placeholder, so this
        # lands on a database that missed that backfill too.
        ("Replaces Infrastructure's manually assembled monthly reporting deck with a live dashboard pulling directly from source systems, after recurring discrepancies between the deck and the underlying data eroded trust in the numbers. Built around AWS, with a single agreed definition for every metric that previously had two or three quietly different versions floating around. The first version was too slow to load with a full quarter of history, which needed a follow-up pass on the underlying queries.",
         'Infrastructure Reporting Overhaul — owned by Infrastructure.'),
        'Replaced a wall of unowned dashboards with a single observability stack covering metrics, distributed tracing, and structured logs, after an incident where three engineers spent forty minutes disagreeing about whether latency had actually regressed. Alert rules were rewritten around symptoms customers feel rather than individual host CPU, which cut paging volume enough that on-call stopped ignoring the channel. Trace sampling was initially too aggressive to debug rare failures, so the tail-based sampler had to be retuned after the first month.',
    ),
    (
        'Infrastructure Workflow Automation',
        # current text, and the pre-abb9a2a3ab1e placeholder, so this
        # lands on a database that missed that backfill too.
        ("Automates a workflow inside Infrastructure that previously required someone to manually shepherd a request through several handoffs, using AWS to trigger the next step automatically instead of waiting on someone to notice an email. Turnaround time improved noticeably once live. The automation didn't originally handle the exception path for rejected requests, which routed silently nowhere until a teammate happened to notice a backlog.",
         'Infrastructure Workflow Automation — owned by Infrastructure.'),
        'Rebuilt the deployment pipeline after releases routinely timed out, hung waiting on manual approval, or half-applied and left environments in an inconsistent state with no clean way back. Deploys are now blue-green with automated rollback on health-check failure, and every environment is provisioned from the same infrastructure-as-code definitions instead of drifting apart over months. The rollback path itself went untested for the first two releases, which is exactly when it was first needed.',
    ),
    (
        'Data & AI Workflow Automation',
        # current text, and the pre-abb9a2a3ab1e placeholder, so this
        # lands on a database that missed that backfill too.
        ("Automates a workflow inside Data & AI that previously required someone to manually shepherd a request through several handoffs, using Python to trigger the next step automatically instead of waiting on someone to notice an email. Turnaround time improved noticeably once live. The automation didn't originally handle the exception path for rejected requests, which routed silently nowhere until a teammate happened to notice a backlog.",
         'Data & AI Workflow Automation — owned by Data & AI.'),
        "Rewrote the nightly ETL pipeline that had been silently failing and back-filling itself for weeks, leaving downstream dashboards confidently reporting stale numbers. The orchestration DAG now fails loudly, retries with backoff, and refuses to publish a partition that didn't pass its row-count and freshness checks. A long-tail problem was upstream schema drift: a source system renamed a column with no notice and every job depending on it failed at 2am for three nights running before anyone was paged.",
    ),
    (
        'Data & AI Tooling Upgrade',
        # current text, and the pre-abb9a2a3ab1e placeholder, so this
        # lands on a database that missed that backfill too.
        ("Replaces the aging toolchain Data & AI had outgrown with a stack built around Python and SQL, chosen after the old tools stopped scaling with the team's headcount. Migration ran in parallel with day-to-day work rather than a hard cutover. A subset of historical records didn't map cleanly to the new schema and needed manual reconciliation after go-live.",
         'Data & AI Tooling Upgrade — owned by Data & AI.'),
        'Introduced data quality contracts between producing and consuming teams after a quarter in which two separate executive dashboards disagreed about revenue and neither owner could explain why. Schema changes now break a contract test in CI instead of breaking a dashboard in production, and every table has a declared owner and freshness expectation. Backfilling historical partitions to satisfy the new checks took considerably longer than building the checks themselves.',
    ),
    (
        'Quality Engineering Tooling Upgrade',
        # current text, and the pre-abb9a2a3ab1e placeholder, so this
        # lands on a database that missed that backfill too.
        ("Replaces the aging toolchain Quality Engineering had outgrown with a stack built around Python and Java, chosen after the old tools stopped scaling with the team's headcount. Migration ran in parallel with day-to-day work rather than a hard cutover. A subset of historical records didn't map cleanly to the new schema and needed manual reconciliation after go-live.",
         'Quality Engineering Tooling Upgrade — owned by Quality Engineering.'),
        'Attacked a test suite that had become slow and flaky enough that engineers were re-running failed builds on reflex rather than reading them, which meant real regressions were being merged behind noise. Flaky tests are now quarantined automatically on repeat non-deterministic failure, and the suite runs in parallel shards that cut wall-clock time substantially. Most of the flakiness turned out to be shared mutable fixtures and hard-coded waits rather than genuine race conditions in the product code.',
    ),
    (
        'IT Operations Tooling Upgrade',
        # current text, and the pre-abb9a2a3ab1e placeholder, so this
        # lands on a database that missed that backfill too.
        ("Replaces the aging toolchain IT Operations had outgrown with a stack built around Network Administration and Cybersecurity, chosen after the old tools stopped scaling with the team's headcount. Migration ran in parallel with day-to-day work rather than a hard cutover. A subset of historical records didn't map cleanly to the new schema and needed manual reconciliation after go-live.",
         'IT Operations Tooling Upgrade — owned by IT Operations.'),
        "Consolidated identity onto single sign-on (SSO) with hardware-backed multi-factor authentication (MFA), retiring a long tail of local accounts and shared credentials nobody would admit to using. Covers VPN access, device enrolment, and the joiner-mover-leaver process, so login rights are revoked on the last working day rather than whenever someone remembers. The rollout's worst week involved conditional-access rules locking staff out mid-flight, which needed an emergency exception path.",
    ),
    (
        'Billing API',
        # current text, and the pre-abb9a2a3ab1e placeholder, so this
        # lands on a database that missed that backfill too.
        ("Consolidates invoicing and usage metering for every product line behind one versioned API, replacing per-product billing logic that had quietly diverged over several years of feature work. Idempotent webhook delivery means a retried event can't double-charge a customer. The initial rollout under-provisioned for spiky end-of-month invoice runs and needed a follow-up scaling pass within the first quarter.",
         'Billing API — owned by Platform Engineering.'),
        "Consolidates invoicing and usage metering for every product line behind one versioned API, replacing per-product billing logic that had quietly diverged over several years of feature work. Idempotent webhook delivery means a retried event can't double-charge a customer. Payment latency was the long-running problem: p99 response times crept up for months until connection-pool exhaustion under end-of-month invoice runs started causing gateway timeouts and stuck transactions, and a slow memory leak in the settlement worker meant it had to be restarted nightly before anyone found the retained-reference bug behind it.",
    ),
]


projects_table = sa.table(
    "projects",
    sa.column("name", sa.String),
    sa.column("description", sa.Text),
)


def upgrade() -> None:
    conn = op.get_bind()
    for name, old_forms, new in DESCRIPTIONS:
        conn.execute(
            projects_table.update()
            .where(
                projects_table.c.name == name,
                projects_table.c.description.in_(list(old_forms)),
            )
            .values(description=new)
        )


def downgrade() -> None:
    """Restores the first (canonical post-abb9a2a3ab1e) prior text."""
    conn = op.get_bind()
    for name, old_forms, new in DESCRIPTIONS:
        conn.execute(
            projects_table.update()
            .where(
                projects_table.c.name == name,
                projects_table.c.description == new,
            )
            .values(description=old_forms[0])
        )
