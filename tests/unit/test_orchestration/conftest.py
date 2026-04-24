import os

import pytest


@pytest.fixture(autouse=True, scope="session")
def isolate_lobster_inbox_dir(tmp_path_factory):
    """Redirect LOBSTER_INBOX_DIR to a temp dir for all tests in this package.

    Prevents _write_steward_trigger() from leaking steward_trigger messages to
    the live ~/messages/inbox/ during pytest runs (issue #912).

    Per-test patch.dict calls (e.g. TestWriteStewardTrigger) take precedence
    over this session fixture, which is the correct behaviour.
    """
    inbox_dir = tmp_path_factory.mktemp("inbox")
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("LOBSTER_INBOX_DIR", str(inbox_dir))
        yield
