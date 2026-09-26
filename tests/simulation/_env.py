"""Environment knobs shared by the simulation suite."""

import os

# ARQ Redis (DB 1). Defaults to the docker-compose hostname; CI and
# non-Docker runs point it at their own Redis, e.g.
# SIM_REDIS_URL=redis://localhost:6379/1
SIM_REDIS_URL = os.getenv("SIM_REDIS_URL", "redis://redis:6379/1")
