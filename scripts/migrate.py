"""Apply every pending Alembic migration. The one-off step in the deployment
runbook — never run by the API or a worker at start-up.

Run it as a one-off ECS Fargate task built from the API image (it already
bundles `migrations/` and `alembic` -- see backend/Dockerfile and
backend/requirements.txt), using the same VPC/security-group access as the
API and workers, so it can reach RDS without any new infrastructure.
DEPLOY.md's ECS section shows the exact `aws ecs run-task` invocation:

    python -m scripts.migrate

Locally, against SQLite, this is a no-op that prints why: schema there is
still created by `shared.db.init_db()` for laptop convenience (see its
docstring), not by Alembic.
"""
import logging
import sys

from alembic import command
from alembic.config import Config

from shared.db import _is_sqlite

logging.basicConfig(level="INFO", format="%(levelname)-7s %(message)s")
log = logging.getLogger("migrate")


def main() -> int:
    if _is_sqlite():
        log.info("DATABASE_URL is sqlite; nothing to migrate -- "
                 "shared.db.init_db() handles local dev schema instead")
        return 0

    cfg = Config("migrations/alembic.ini")
    log.info("applying migrations up to head")
    command.upgrade(cfg, "head")
    log.info("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
