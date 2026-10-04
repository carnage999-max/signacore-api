"""A load test shaped like a signer reading a document, not like a benchmark.

What costs anything here is drawing a page. The portal asks for a thumbnail and a full image of
every page, lazily, as the reader scrolls - so the load a real signer makes is a burst when the
document opens and then a trickle as they work down it. A script hammering one URL would answer a
question nobody asked.

Two things are modelled carefully because they decide whether the numbers mean anything:

Conditional requests. A browser holds the tag it was given and offers it back, so a page it has
already seen costs a 304 rather than a redraw. Ignoring that would overstate the load by roughly
the ratio of those two, which is about fifty to one. Pages already seen are re-read with
``If-None-Match``, and the 304s are counted separately so the cache can be seen working.

One signer per user. The rate limit is keyed on the address and the signing request together, so
two virtual users sharing a seeded signer would share a bucket and throttle each other - which
would measure the limiter rather than the server. Users beyond the number of seeded signers are
refused with an explanation rather than quietly sharing.

    python manage.py seed_load_test --signers 50 --pages 8 > signers.csv   # on the server
    SIGNACORE_LOADTEST_CSV=signers.csv locust -f loadtest/locustfile.py \
        --host https://api-staging.mysignacore.com

Run it from a machine that is not the server. A load generator competing with its target for CPU
measures neither.
"""

from __future__ import annotations

import csv
import itertools
import os
import random
import threading
from dataclasses import dataclass, field

from locust import HttpUser, between, events, task

CSV_PATH = os.environ.get("SIGNACORE_LOADTEST_CSV", "signers.csv")
SESSION_COOKIE = "signacore_signer_session"
THUMBNAIL_WIDTH = 150


@dataclass
class SeededSigner:
    token: str
    cookie: str
    field_id: str
    pages: int


@dataclass
class ReadingState:
    """Where this signer has got to, and which pages their browser already holds."""

    signer: SeededSigner
    page: int = 1
    etags: dict[str, str] = field(default_factory=dict)


_signers: list[SeededSigner] = []
_next_signer = itertools.count()
_lock = threading.Lock()


@events.test_start.add_listener
def load_seeded_signers(environment, **_kwargs) -> None:
    """Read the file the seeding command wrote, and stop early if it is not usable."""
    global _signers
    try:
        with open(CSV_PATH, newline="") as handle:
            _signers = [
                SeededSigner(
                    token=row["token"],
                    cookie=row["session_cookie"],
                    field_id=row["field_id"],
                    pages=int(row["pages"]),
                )
                for row in csv.DictReader(handle)
            ]
    except FileNotFoundError:
        raise SystemExit(
            f"No seeded signers at {CSV_PATH}. Run `manage.py seed_load_test` on the server first, "
            "and point SIGNACORE_LOADTEST_CSV at what it wrote."
        ) from None

    if not _signers:
        raise SystemExit(f"{CSV_PATH} has no rows.")

    print(f"Loaded {len(_signers)} seeded signer(s), {_signers[0].pages} page(s) each.")


class Signer(HttpUser):
    """One person, with one document open, reading it at human speed."""

    # Long enough to be a person rather than a script. Shorten it to find the ceiling sooner, but
    # know that you are then measuring a burst rather than a working day.
    wait_time = between(2, 6)

    def on_start(self) -> None:
        with _lock:
            index = next(_next_signer)
        if index >= len(_signers):
            raise SystemExit(
                f"Only {len(_signers)} seeded signer(s) for more users than that. Seed at least as "
                "many as you intend to run, or users will share a rate-limit bucket and throttle "
                "each other."
            )

        self.state = ReadingState(signer=_signers[index])
        self.client.cookies.set(SESSION_COOKIE, self.state.signer.cookie)
        self.open_document()

    def open_document(self) -> None:
        """What happens in the first second: the page, its state, and the page rail."""
        self.client.get(f"/sign/{self.state.signer.token}/", name="portal page")
        self.client.get(f"/api/sign/{self.state.signer.token}/", name="context")
        for page in range(1, self.state.signer.pages + 1):
            self.fetch_page(page, width=THUMBNAIL_WIDTH, name="thumbnail")

    @task(9)
    def read_the_next_page(self) -> None:
        """The dominant request, and the expensive one."""
        self.fetch_page(self.state.page, name="page preview")
        self.state.page += 1
        if self.state.page > self.state.signer.pages:
            self.state.page = 1

    @task(3)
    def scroll_back(self) -> None:
        """Re-reading something above, which should cost a 304 rather than a redraw."""
        if self.state.page <= 1:
            return
        self.fetch_page(random.randint(1, self.state.page - 1), name="page preview")

    @task(1)
    def check_progress(self) -> None:
        self.client.get(f"/api/sign/{self.state.signer.token}/", name="context")

    def fetch_page(self, page: int, *, width: int | None = None, name: str = "page preview") -> None:
        url = f"/api/sign/{self.state.signer.token}/pages/{page}/preview/"
        if width:
            url = f"{url}?width={width}"

        headers = {}
        held = self.state.etags.get(url)
        if held:
            headers["If-None-Match"] = held

        with self.client.get(url, headers=headers, name=name, catch_response=True) as response:
            if response.status_code == 304:
                # Counted apart from a redraw, because the difference between them is the point.
                response.success()
                self.environment.stats.log_request("GET", f"{name} (304)", response.elapsed.total_seconds() * 1000, 0)
                return
            if response.status_code == 429:
                # Not a server failure. If these appear the test is measuring the rate limiter, so
                # raise SIGNACORE_THROTTLE_SIGNER_PREVIEW on the target and start again.
                response.failure("throttled")
                return
            if response.status_code >= 500:
                response.failure(f"server fault {response.status_code}")
                return
            if response.status_code != 200:
                response.failure(f"unexpected {response.status_code}")
                return

            tag = response.headers.get("ETag")
            if tag:
                self.state.etags[url] = tag
            response.success()
